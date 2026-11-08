#!/usr/bin/env python3
"""PoC: call ComfyUI (127.0.0.1:8188) on the GPU machine over an SSH tunnel.

Subcommands:
  t2i   "<description>" -o out.png   FLUX.2 klein 4B distilled, text to image
  edit  in.png "<change>" -o out.png   FLUX.2 klein image edit (growth: stays the same creature)
  i2m   in.png -o out.glb [--pixal] [--fast]   TRELLIS.2 (default) / Pixal3D, image to 3D
  convert template.json -o api.json   Convert a ComfyUI UI-format workflow to API format

Stdlib only, runs directly on this machine. Tunnel: ssh -f -N -L 127.0.0.1:8188:127.0.0.1:8188 <GPU machine>
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
import uuid

BASE = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
HERE = os.path.dirname(os.path.abspath(__file__))
TRELLIS_TEMPLATE = os.path.join(os.path.dirname(HERE), "gpu", "templates", "3d_pixal3d_trellis2_image_to_model.json")

# Apart from "socket" types, which take no slot in widgets_values, these types are the input widgets in the UI
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "COLOR", "LOAD_3D", "COMFY_DYNAMICCOMBO_V3"}
SKIP_TYPES = {"Note", "MarkdownNote"}
UI_ONLY_OUTPUTS = {"PreviewImage", "MaskPreview", "Preview3DAdvanced", "GetMeshInfo", "RenderUVAtlas"}


def http(method, path, data=None, headers=None, timeout=60):
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {path}: {e.read().decode(errors='replace')[:3000]}") from None


def get_json(path):
    return json.loads(http("GET", path))


# ---------- UI format → API format ----------

def _spec_type(spec):
    t = spec[0]
    return "COMBO" if isinstance(t, list) else t


def _consume_widgets(spec_inputs, order, values, prefix, out):
    """Hand out widgets_values to the input widgets, in the input order of the node definition. Returns the values left over."""
    for name in order:
        spec = spec_inputs.get(name)
        if spec is None:
            continue
        typ = _spec_type(spec)
        opts = spec[1] if len(spec) > 1 else {}
        if typ not in WIDGET_TYPES:
            continue
        if not values:
            break
        val, values = values[0], values[1:]
        out[prefix + name] = val
        if typ == "INT" and opts.get("control_after_generate"):
            values = values[1:]  # skip "fixed" / "randomize"
        if typ == "COMFY_DYNAMICCOMBO_V3":
            opt = next(o for o in opts["options"] if o["key"] == val)
            sub = {**(opt["inputs"].get("required") or {}), **(opt["inputs"].get("optional") or {})}
            values = _consume_widgets(sub, list(sub), values, prefix + name + ".", out)
    return values


def convert(ui, object_info, drop_ui_outputs=True):
    links = {l[0]: (l[1], l[2]) for l in ui["links"]}
    prompt = {}
    for n in ui["nodes"]:
        t = n["type"]
        if t in SKIP_TYPES or n.get("mode", 0) != 0:
            continue
        # newer PreviewImage passes the image through unchanged, so a preview node with something downstream can't be dropped
        if drop_ui_outputs and t in UI_ONLY_OUTPUTS and not any(o.get("links") for o in n.get("outputs") or []):
            continue
        info = object_info[t]
        spec_inputs = {**(info["input"].get("required") or {}), **(info["input"].get("optional") or {})}
        order = info["input_order"].get("required", []) + info["input_order"].get("optional", [])
        inputs = {}
        wv = n.get("widgets_values") or []
        if isinstance(wv, dict):
            inputs.update(wv)
        else:
            _consume_widgets(spec_inputs, order, list(wv), "", inputs)
        for inp in n.get("inputs", []):
            if inp.get("link") is not None:
                src, slot = links[inp["link"]]
                inputs[inp["name"]] = [str(src), slot]
        prompt[str(n["id"])] = {"class_type": t, "inputs": inputs}
    return prompt


# ---------- Run ----------

def upload_image(path):
    boundary = uuid.uuid4().hex
    name = os.path.basename(path)
    with open(path, "rb") as f:
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
            f"Content-Type: image/png\r\n\r\n"
        ).encode() + f.read() + f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode()
    r = json.loads(http("POST", "/upload/image", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}))
    return r["name"]


def run(prompt, timeout=3600, front=False):
    """front=True puts the job at the front of the ComfyUI queue (without interrupting the running one), so images that take seconds don't wait behind 3D jobs that take minutes."""
    pid = json.loads(http("POST", "/prompt", json.dumps({"prompt": prompt, "client_id": "poc", "front": front}).encode(),
                          {"Content-Type": "application/json"}))["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = get_json(f"/history/{pid}")
        if pid in h:
            st = h[pid].get("status", {})
            if st.get("status_str") == "error":
                msgs = [m for m in st.get("messages", []) if m[0] == "execution_error"]
                raise RuntimeError(json.dumps(msgs, ensure_ascii=False)[:3000])
            if st.get("completed"):
                return h[pid]["outputs"], time.time() - t0
        time.sleep(2)
    raise TimeoutError(pid)


def download(file_info, dest):
    q = urllib.parse.urlencode({k: file_info.get(k, "") for k in ("filename", "subfolder", "type")})
    data = http("GET", f"/view?{q}", timeout=300)
    with open(dest, "wb") as f:
        f.write(data)
    return len(data)


def t2i_prompt(text, seed, width=1024, height=1024, steps=4):
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux-2-klein-4b.safetensors", "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "flux2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae.safetensors"}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": text}},
        "neg": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}},
        "guider": {"class_type": "CFGGuider", "inputs": {"model": ["unet", 0], "positive": ["pos", 0], "negative": ["neg", 0], "cfg": 1}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "sched": {"class_type": "Flux2Scheduler", "inputs": {"steps": steps, "width": width, "height": height}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "latent": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "sca": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0], "sigmas": ["sched", 0], "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sca", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "monster/t2i"}},
    }


def edit_prompt(image_name, instruction, seed, steps=4):
    """FLUX.2 klein image edit: the reference image is VAE-encoded and attached to both the positive and negative conditioning (follows the official 4B distilled edit template)."""
    p = t2i_prompt(instruction, seed, steps=steps)
    p.update({
        "load": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "scale": {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["load", 0], "upscale_method": "nearest-exact", "megapixels": 1, "resolution_steps": 1}},
        "size": {"class_type": "GetImageSize", "inputs": {"image": ["scale", 0]}},
        "ref": {"class_type": "VAEEncode", "inputs": {"pixels": ["scale", 0], "vae": ["vae", 0]}},
        "pos_ref": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["pos", 0], "latent": ["ref", 0]}},
        "neg_ref": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["neg", 0], "latent": ["ref", 0]}},
    })
    p["guider"]["inputs"].update(positive=["pos_ref", 0], negative=["neg_ref", 0])
    for k in ("sched", "latent"):
        p[k]["inputs"].update(width=["size", 0], height=["size", 1])
    p["save"]["inputs"]["filename_prefix"] = "monster/edit"
    return p


# Game-asset settings: the template defaults to film quality (1536 voxels, 700k faces, 4096 textures), and the single-threaded UV unwrap takes a very long time
FAST = {
    ("94", "target_resolution"): 1024,   # Trellis2UpsampleStage, the smallest allowed value
    ("241", "resolution"): 384,          # RemeshMesh, 256≈100k faces
    ("186", "target_face_count"): 60000, # DecimateMesh
    ("288", "value"): 2048,              # base color texture + UV atlas resolution
    ("224", "resolution"): 1024,         # normal map
    ("233", "resolution"): 512,          # AO map
    ("233", "samples"): 32,
}


def i2m_prompt(image_name, use_pixal=False, bf16=True, fast=False, vcolor=False):
    ui = json.load(open(TRELLIS_TEMPLATE))
    p = convert(ui, get_json("/object_info"))
    p["122"]["inputs"]["image"] = image_name
    p["316"]["inputs"]["value"] = not use_pixal  # PrimitiveBoolean: true = TRELLIS.2, false = Pixal3D
    if bf16:  # the Spark has enough memory: use the original bf16 weights, not the int8 ones made for small GPUs
        p["40"]["inputs"]["unet_name"] = "trellis_2_bf16.safetensors"
        p["319"]["inputs"]["unet_name"] = "pixal3d_bf16.safetensors"
    if fast:
        for (node, key), val in FAST.items():
            p[node]["inputs"][key] = val
    if vcolor:  # paint colors straight onto the vertices (PaintMesh → 282), skipping the UV unwrap and texture baking
        p["322"]["inputs"]["model_3d"] = ["282", 0]
    p["322"]["inputs"]["filename_prefix"] = "3d/monster"
    return p


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("t2i"); a.add_argument("text"); a.add_argument("-o", required=True); a.add_argument("--seed", type=int)
    b = sub.add_parser("i2m"); b.add_argument("image"); b.add_argument("-o", required=True); b.add_argument("--pixal", action="store_true"); b.add_argument("--int8", action="store_true"); b.add_argument("--fast", action="store_true"); b.add_argument("--vcolor", action="store_true")
    e = sub.add_parser("edit"); e.add_argument("image"); e.add_argument("instruction"); e.add_argument("-o", required=True); e.add_argument("--seed", type=int)
    c = sub.add_parser("convert"); c.add_argument("template"); c.add_argument("-o", required=True)
    args = ap.parse_args()

    if args.cmd == "convert":
        p = convert(json.load(open(args.template)), get_json("/object_info"))
        json.dump(p, open(args.o, "w"), indent=1, ensure_ascii=False)
        print(f"{len(p)} nodes -> {args.o}")
        return

    if args.cmd == "t2i":
        seed = args.seed if args.seed is not None else random.randrange(2**31)
        outputs, dt = run(t2i_prompt(args.text, seed))
        img = outputs["save"]["images"][0]
    elif args.cmd == "edit":
        seed = args.seed if args.seed is not None else random.randrange(2**31)
        outputs, dt = run(edit_prompt(upload_image(args.image), args.instruction, seed))
        img = outputs["save"]["images"][0]
    else:
        name = upload_image(args.image)
        outputs, dt = run(i2m_prompt(name, use_pixal=args.pixal, bf16=not args.int8, fast=args.fast, vcolor=args.vcolor))
        files = [f for o in outputs.values() for k, v in o.items() if isinstance(v, list) for f in v
                 if isinstance(f, dict) and str(f.get("filename", "")).endswith(".glb")]
        if not files:
            sys.exit(f"no .glb in outputs: {json.dumps(outputs)[:1000]}")
        img = files[0]
    size = download(img, args.o)
    print(json.dumps({"out": args.o, "bytes": size, "seconds": round(dt, 1)}))


if __name__ == "__main__":
    main()
