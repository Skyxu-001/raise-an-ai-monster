#!/usr/bin/env python3
"""Auto-rig and skin a GLB from TRELLIS (SkinTokens' TokenRig model) and write back a GLB with a skin.

Runs in the ComfyUI container on the GPU machine (DGX Spark), placed in the SkinTokens repo root, without Blender:
  cd <SkinTokens repo> && python rig_glb.py a.glb a_rig.glb [b.glb b_rig.glb ...]
The official flow reads and writes files through Blender (bpy), but the Spark is aarch64 and bpy has no package for it. So this reads through the repo's own npz entry point
and writes the skin with pygltflib. The model loads once and files run one after another; each file tries a few seeds and keeps the best by rig_roles.score.
Also writes <output>.json: joint positions, parents, how many vertices each joint drives, which bone chain is each body part, and where the mouth is (rig_roles.roles).
"""
import argparse
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "pydeps"), HERE]  # pydeps: extra installed packages
os.chdir(HERE)  # the repo's configs and weights are found by relative path

import numpy as np  # noqa: E402
import torch  # noqa: E402
import transformers  # noqa: E402
from pygltflib import GLTF2, Accessor, BufferView, Node, Skin  # noqa: E402

import rig_roles  # noqa: E402

# No flash-attn on GB10: send the language model part through PyTorch's built-in sdpa instead
_from_config = transformers.AutoModelForCausalLM.from_config


def _from_config_sdpa(*a, **kw):
    if kw.get("attn_implementation") == "flash_attention_2":
        kw["attn_implementation"] = "sdpa"
    return _from_config(*a, **kw)


transformers.AutoModelForCausalLM.from_config = _from_config_sdpa

import sdpa_flash_shim  # noqa: E402

sys.modules["flash_attn_interface"] = sdpa_flash_shim
from src.data.dataset import DatasetConfig, RigDatasetModule  # noqa: E402
from src.data.transform import Transform  # noqa: E402
from src.server.spec import get_model  # noqa: E402
from src.tokenizer.parse import get_tokenizer  # noqa: E402
del sys.modules["flash_attn_interface"]  # see the top of sdpa_flash_shim.py

CKPT = "experiments/articulation_xl_quantization_256_token_4/grpo_1400.ckpt"
FLOAT, U16, U32 = 5126, 5123, 5125


# ---------- GLB read and write ----------

def read_glb(path):
    g = GLTF2().load_binary(path)
    assert len(g.meshes) == 1 and len(g.meshes[0].primitives) == 1, "only handles GLBs with one mesh and one primitive (what TRELLIS produces)"
    blob = g.binary_blob()
    prim = g.meshes[0].primitives[0]

    def arr(i, comps, dtype):
        acc = g.accessors[i]
        bv = g.bufferViews[acc.bufferView]
        assert bv.byteStride in (None, 0, comps * np.dtype(dtype).itemsize), "interleaved vertex data is not supported"
        off = (bv.byteOffset or 0) + (acc.byteOffset or 0)
        return np.frombuffer(blob, dtype=dtype, count=acc.count * comps, offset=off).reshape(acc.count, comps)

    pos = arr(prim.attributes.POSITION, 3, np.float32)
    idt = np.uint32 if g.accessors[prim.indices].componentType == U32 else np.uint16
    faces = arr(prim.indices, 1, idt).reshape(-1, 3).astype(np.int64)
    return pos.astype(np.float64), faces


def write_skinned(src, dst, joints, parents, skin):
    """joints: glTF coordinates (Y up); skin: (vertices, joints), in the same vertex order as the original GLB. Bones have no rotation, only translation."""
    g = GLTF2().load_binary(src)
    blob = bytearray(g.binary_blob())

    def add(a, ctype, typ, target=None):
        while len(blob) % 4:
            blob.append(0)
        data = np.ascontiguousarray(a).tobytes()
        g.bufferViews.append(BufferView(buffer=0, byteOffset=len(blob), byteLength=len(data), target=target))
        blob.extend(data)
        g.accessors.append(Accessor(bufferView=len(g.bufferViews) - 1, componentType=ctype, count=len(a), type=typ))
        return len(g.accessors) - 1

    J = len(parents)
    top = np.argsort(-skin, axis=1)[:, :4]
    w = np.clip(np.take_along_axis(skin, top, 1), 0, None)
    s = w.sum(1, keepdims=True)
    w = np.where(s > 1e-8, w / np.maximum(s, 1e-8), np.array([[1, 0, 0, 0]]))
    prim = g.meshes[0].primitives[0]
    prim.attributes.JOINTS_0 = add(top.astype(np.uint16), U16, "VEC4", 34962)
    prim.attributes.WEIGHTS_0 = add(w.astype(np.float32), FLOAT, "VEC4", 34962)

    base = len(g.nodes)
    for j in range(J):
        p = parents[j]
        t = joints[j] - (joints[p] if p >= 0 else 0)
        g.nodes.append(Node(name=f"bone_{j}", translation=[float(x) for x in t], children=[]))
    for j in range(J):
        if parents[j] >= 0:
            g.nodes[base + parents[j]].children.append(base + j)
    roots = [base + j for j in range(J) if parents[j] < 0]
    ibm = np.tile(np.eye(4, dtype=np.float32), (J, 1, 1))
    ibm[:, :3, 3] = -joints
    ibm_acc = add(ibm.transpose(0, 2, 1).reshape(J, 16), FLOAT, "MAT4")  # glTF stores matrices by column
    g.skins.append(Skin(inverseBindMatrices=ibm_acc, joints=list(range(base, base + J)),
                        skeleton=roots[0] if len(roots) == 1 else None))
    mesh_node = next(i for i, n in enumerate(g.nodes) if n.mesh == 0)
    g.nodes[mesh_node].skin = len(g.skins) - 1
    g.scenes[g.scene or 0].nodes.extend(roots)
    g.buffers[0].byteLength = len(blob)
    g.set_binary_blob(bytes(blob))
    g.save_binary(dst)


# ---------- Coordinates: glTF is Y up; the model was trained on data read in through Blender (Z up) ----------

def gltf_to_blender(v):
    return np.stack([v[:, 0], -v[:, 2], v[:, 1]], 1)


def blender_to_gltf(v):
    return np.stack([v[:, 0], v[:, 2], -v[:, 1]], 1)


def fit_scale_shift(a, b):
    """Solve b ≈ s·a + t (inside the model the mesh was scaled and moved into a standard range)."""
    ca, cb = a.mean(0), b.mean(0)
    s = np.sqrt(((b - cb) ** 2).sum() / ((a - ca) ** 2).sum())
    return s, cb - s * ca


# ---------- Inference ----------

def to_original(asset, vb):
    """Move the skeleton and weights from the model's internal coordinates back onto the original GLB: returns joints in glTF coordinates, the parent table, and weights of shape (original vertices, joints)."""
    av = np.asarray(asset.vertices, dtype=np.float64)
    if av.shape == vb.shape:
        s, t = fit_scale_shift(av, vb)
        skin = np.asarray(asset.skin, dtype=np.float64)
    else:  # the model swapped vertices internally (simplify / resample): align by bounding box, then carry weights back to the original vertices by nearest neighbor
        from scipy.spatial import cKDTree
        s = (vb.max(0) - vb.min(0)).max() / (av.max(0) - av.min(0)).max()
        t = (vb.max(0) + vb.min(0)) / 2 - s * (av.max(0) + av.min(0)) / 2
        _, nn = cKDTree(av * s + t).query(vb)
        skin = np.asarray(asset.skin, dtype=np.float64)[nn]
    joints = blender_to_gltf(np.asarray(asset.joints, dtype=np.float64) * s + t)
    parents = [(-1 if p is None or p < 0 else int(p)) for p in asset.parents]
    return joints, parents, skin


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="+", help="input GLB and output GLB, in pairs")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tries", type=int, default=3, help="max seeds to try; keeps the best by coverage score")
    ap.add_argument("--budget", type=float, default=60, help="stop trying new seeds once a file has taken this many seconds (if it already has a result)")
    ap.add_argument("--cls", default="articulation", help="skeleton style: articulation (general objects / animals), vroid, mixamo")
    args = ap.parse_args()
    assert len(args.pairs) % 2 == 0
    pairs = list(zip(args.pairs[::2], args.pairs[1::2]))

    t0 = time.time()
    model = get_model(CKPT)
    model.eval()
    tokenizer = get_tokenizer(**model.tokenizer_config)
    transform = Transform.parse(**model.transform_config["predict_transform"])
    print(f"Model loaded in {time.time() - t0:.1f} s", flush=True)

    tmp = tempfile.mkdtemp(prefix="rig_")
    failed = 0
    for src, dst in pairs:
        t1 = time.time()
        np.random.seed(args.seed)
        pos, faces = read_glb(src)
        vb = gltf_to_blender(pos)
        npz = os.path.join(tmp, os.path.basename(src) + ".npz")
        np.savez(npz, vertices=vb.astype(np.float32), faces=faces)

        ds = DatasetConfig.parse(shuffle=False, batch_size=1, num_workers=0, pin_memory=False, persistent_workers=False,
                                 datapath={"data_name": None, "loader": "npz", "filepaths": {args.cls: [npz]}}).split_by_cls()
        module = RigDatasetModule(predict_dataset_config=ds, predict_transform=transform, tokenizer=tokenizer,
                                  process_fn=model._process_fn)
        batch = next(iter(module.predict_dataloader()[args.cls]))
        batch = {k: v.to("cuda") if isinstance(v, torch.Tensor) else v for k, v in batch.items()}
        batch.pop("skeleton_tokens", None)
        batch.pop("skeleton_mask", None)
        batch["generate_kwargs"] = dict(max_length=2048, top_k=5, top_p=0.95, temperature=1.0, repetition_penalty=2.0,
                                        num_return_sequences=1, num_beams=10, do_sample=True)
        # Skeletons are sampled: with another seed the same monster may gain or lose a tail, and now and then a sequence comes out that can't form a skeleton (seen on a jellyfish)
        best, tried = None, []
        for seed in range(args.seed, args.seed + args.tries):
            if best and time.time() - t1 > args.budget:
                break
            torch.manual_seed(seed)
            try:
                with torch.no_grad():
                    asset = model.predict_step(batch, make_asset=True)["results"][0].asset
            except ValueError as e:
                tried.append({"seed": seed, "error": str(e)[:200]})
                continue
            joints, parents, skin = to_original(asset, vb)
            sc, detail = rig_roles.score(pos, joints, parents, skin.argmax(1))
            tried.append({"seed": seed, "joints": len(parents), "score": round(sc, 4), **detail})
            if best is None or sc < best[0]:
                best = (sc, seed, joints, parents, skin)
        if best is None:
            print(f"{src} all seeds failed: {tried}", flush=True)
            failed += 1
            continue

        sc, seed, joints, parents, skin = best
        write_skinned(src, dst, joints, parents, skin)
        owner = skin.argmax(1)
        info = {"src": os.path.basename(src), "seconds": round(time.time() - t1, 1), "seed": seed, "score": round(sc, 4),
                "tried": tried, "joints": joints.round(4).tolist(), "parents": parents,
                "verts_per_joint": np.bincount(owner, minlength=len(parents)).tolist(),
                "roles": rig_roles.roles(pos, joints, parents, owner)}
        with open(os.path.splitext(dst)[0] + ".json", "w") as f:
            json.dump(info, f)
        print(f"{src} → {dst}: {len(parents)} joints, seed {seed} (score {sc:.3f}), {info['seconds']} s; tried {tried}", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
