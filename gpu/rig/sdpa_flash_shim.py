"""Stand-in for flash_attn_interface: there is no flash-attn on GB10 (sm_121), and several SkinTokens imports look for this name first
and use it if found. This implements the same interface with PyTorch's built-in scaled_dot_product_attention; return values follow the flash-attn 3 (out, lse) convention.
rig_glb.py registers it as flash_attn_interface only while importing SkinTokens and removes it right after: transformers 5, seeing this name
with no install record, raises an error.
"""
import torch.nn.functional as F


def flash_attn_func(q, k, v, softmax_scale=None, causal=False, **kwargs):
    # flash-attn's layout is (B, L, H, D); SDPA wants (B, H, L, D)
    q, k, v = (t.to(q.dtype).transpose(1, 2) for t in (q, k, v))
    if k.shape[1] != q.shape[1]:  # GQA: fewer kv heads than q heads
        r = q.shape[1] // k.shape[1]
        k, v = k.repeat_interleave(r, dim=1), v.repeat_interleave(r, dim=1)
    out = F.scaled_dot_product_attention(q, k, v, is_causal=causal, scale=softmax_scale)
    return out.transpose(1, 2), None
