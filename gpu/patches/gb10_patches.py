"""Patch for the DGX Spark (GB10) only: drop it in ComfyUI/custom_nodes/ and it takes effect, with no change to ComfyUI itself.

UnwrapMesh's batched LSCM solve sends fp64 dense systems to the GPU by default (torch.linalg.solve).
On GB10 this step hangs: 0% GPU utilization, 11 W power draw, the process spinning on a libcublas → libcuda mutex.
A standalone repro (B=20, N=256) didn't return in 5 minutes; numpy solves the same data on the CPU instantly.
This forces device to None so it takes the existing CPU branch in the same function (numpy batched dgemm + solve).
"""
import logging

import comfy_extras.mesh3d.uv_unwrap.parameterize as _param

_orig_lscm_charts_batch = _param.lscm_charts_batch


def _lscm_charts_batch_cpu(*args, **kwargs):
    kwargs["device"] = None
    return _orig_lscm_charts_batch(*args, **kwargs)


_param.lscm_charts_batch = _lscm_charts_batch_cpu
logging.info("[gb10-patches] UnwrapMesh LSCM batch solve forced to CPU (GB10 fp64 GPU solve hangs)")

NODE_CLASS_MAPPINGS = {}
