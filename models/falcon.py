from openvino.runtime import Core, Model, Tensor, PartialShape, Type, serialize, opset_utils
from openvino.runtime import opset10 as opset
import numpy as np
import sys, os
import argparse
import time
from utils import show_model, make_mha, make_fc, make_mvn

def layer(configs, consts, layer_idx, hidden_states, kv_cache, beam_table, attn_mask, cos_tab, sin_tab):
    name_suffix = f'.layer{layer_idx}'
    name_prefix = 'transformer.h'
    # layerNorm operation
    attention_layernorm_out = make_mvn('transformer.h.ln_attn', hidden_states, consts['layers'][layer_idx], configs, name_suffix)
    mlp_layernorm_out = make_mvn('transformer.h.ln_mlp', hidden_states, consts['layers'][layer_idx], configs, name_suffix)

    qkv = make_fc('transformer.h.self_attention.query_key_value', attention_layernorm_out, consts['layers'][layer_idx], name_suffix)

    # custom op
    attn_output = make_mha([qkv], kv_cache, beam_table, attn_mask, cos_tab, sin_tab,
                           layer_idx, configs['rotary_dims'], configs['hidden_size'], configs['head_num'],
                           name=f'{name_prefix}.mha{name_suffix}', num_kv_heads=configs['num_kv_heads'])

    attn_output = make_fc('transformer.h.self_attention.dense', attn_output, consts['layers'][layer_idx], name_suffix)

    # mlp
    def mlp(states):
        dense_h_to_4h = make_fc('transformer.h.mlp.dense_h_to_4h', states, consts['layers'][layer_idx], name_suffix)
        gelu = opset.gelu(dense_h_to_4h, approximation_mode=configs['gelu_mode'], name=f'{name_prefix}.mlp.gelu{name_suffix}')
        dense_4h_to_h = make_fc('transformer.h.mlp.dense_4h_to_h', gelu, consts['layers'][layer_idx], name_suffix)
        return dense_4h_to_h

    mlp_output = mlp(mlp_layernorm_out)
    # residual connection.
    output = opset.add(hidden_states,
                       opset.add(mlp_output, attn_output, auto_broadcast="numpy", name=f'{name_prefix}.add0{name_suffix}'),
                       auto_broadcast="numpy", name=f'{name_prefix}.add1{name_suffix}')
    return output

def create_model(configs, consts):
    print(f'start generate ov model...')
    beg = time.time()
    # [batch, query_len]
    input_ids = opset.parameter([-1, -1], Type.i32, name='input_ids')
    # [2 * n_layers, batch, n_head, max_kv_len, head_size]
    kv_cache = opset.parameter([2 * configs['layer_num'], -1, configs['head_num'], -1, configs['head_size']], Type.f32, name='kv_cache')
    # [batch, max_kv_len]
    beam_table = opset.parameter([-1, -1], Type.i32, name='beam_table')
    # [batch, query_len+past_len]
    attn_mask = opset.parameter([-1, -1], Type.f32, name='attn_mask')
    # [max_kv_len, rotary_dims//2]
    cos_tab = opset.parameter([-1, configs['rotary_dims'] // 2], Type.f32, name='cos_tab')
    sin_tab = opset.parameter([-1, configs['rotary_dims'] // 2], Type.f32, name='sin_tab')

    key = 'transformer.word_embeddings.weight'
    embed_in_const = opset.constant(consts[key], Type.f32, name=key)
    inputs_embeds = opset.gather(embed_in_const, indices=input_ids, axis=0)
    hidden_states = inputs_embeds

    for i in range(configs['layer_num']):
        hidden_states = layer(configs, consts, i, hidden_states, kv_cache, beam_table, attn_mask, cos_tab, sin_tab)
    # final_layernorm
    final_layernorm = make_mvn('transformer.ln_f', hidden_states, consts, configs)
    # embed_out
    embed_out = make_fc('lm_head', final_layernorm, consts)
    embed_out_result = opset.result(embed_out, name='logits')
    cost = time.time() - beg
    print(f'generate ov model done, cost {cost:.2f} seconds.')
    return Model([embed_out_result],
                 [input_ids, kv_cache, beam_table, attn_mask, cos_tab, sin_tab])

def get_params_from_model(path):
    print(f'extracting from model "{path}"...')
    beg = time.time()
    from transformers import AutoModelForCausalLM
    model = AutoModelForCausalLM.from_pretrained(path, trust_remote_code=True).to('cpu').eval()
    assert(model.config.new_decoder_architecture == True)
    assert(model.config.parallel_attn == True)
    assert(model.config.rotary == True)

    configs = {
        'layer_num': model.config.num_hidden_layers,
        'head_num': model.config.num_attention_heads,
        'head_size': model.config.hidden_size // model.config.num_attention_heads,
        'hidden_size': model.config.hidden_size,
        'layer_norm_eps': model.config.layer_norm_epsilon,
        'num_kv_heads': model.config.num_kv_heads,
        'rotary_dims': model.config.hidden_size // model.config.num_attention_heads,
        'gelu_mode': 'erf'
    }
    consts = {
        'transformer.word_embeddings.weight': model.transformer.word_embeddings.weight.detach().numpy(),
        'transformer.ln_f.bias': model.transformer.ln_f.bias.detach().numpy(),
        'transformer.ln_f.weight': model.transformer.ln_f.weight.detach().numpy(),
        'lm_head.weight': model.lm_head.weight.detach().numpy(),
        'lm_head.bias': model.lm_head.bias.detach().numpy() if model.lm_head.bias is not None else None,
        'layers': [
            {
                'transformer.h.ln_attn.bias': l.ln_attn.bias.detach().numpy(),
                'transformer.h.ln_attn.weight': l.ln_attn.weight.detach().numpy(),
                'transformer.h.ln_mlp.bias': l.ln_mlp.bias.detach().numpy(),
                'transformer.h.ln_mlp.weight': l.ln_mlp.weight.detach().numpy(),
                'transformer.h.self_attention.query_key_value.bias': l.self_attention.query_key_value.bias.detach().numpy() if l.self_attention.query_key_value.bias is not None else None,
                'transformer.h.self_attention.query_key_value.weight': l.self_attention.query_key_value.weight.detach().numpy(),
                'transformer.h.self_attention.dense.bias': l.self_attention.dense.bias.detach().numpy() if l.self_attention.dense.bias is not None else None,
                'transformer.h.self_attention.dense.weight': l.self_attention.dense.weight.detach().numpy(),
                'transformer.h.mlp.dense_h_to_4h.bias': l.mlp.dense_h_to_4h.bias.detach().numpy() if l.mlp.dense_h_to_4h.bias is not None else None,
                'transformer.h.mlp.dense_h_to_4h.weight': l.mlp.dense_h_to_4h.weight.detach().numpy(),
                'transformer.h.mlp.dense_4h_to_h.bias': l.mlp.dense_4h_to_h.bias.detach().numpy() if l.mlp.dense_4h_to_h.bias is not None else None,
                'transformer.h.mlp.dense_4h_to_h.weight': l.mlp.dense_4h_to_h.weight.detach().numpy()
            } for l in model.transformer.h
        ],
    }
    cost = time.time() - beg
    print(f'extracting done, cost {cost:.2f} seconds.\nmodel configs:')
    for k, v in configs.items():
        print(f'	{k}: {v}')
    return configs, consts

if __name__ == "__main__":
    parser = argparse.ArgumentParser('')
    parser.add_argument('org_model_path', type=str, nargs='?', default='/home/openvino-ci-68/falcon-40b/')
    parser.add_argument('ov_model_path', type=str, nargs='?', default='./gen/falcon_40b.xml')
    args = parser.parse_args()

    configs, consts = get_params_from_model(args.org_model_path)
    model = create_model(configs, consts)
    show_model(model)
    print(f'serialize ov model to "{args.ov_model_path}"...')
    beg = time.time()
    serialize(model, args.ov_model_path)
    cost = time.time() - beg
    print(f'serialize done, cost {cost:.2f} seconds.')