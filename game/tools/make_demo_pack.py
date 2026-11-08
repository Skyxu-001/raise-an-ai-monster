#!/usr/bin/env python3
"""Build demo-pack.zip for `server.py --demo` from a real save.

  python3 game/tools/make_demo_pack.py game/data demo-pack.zip --adopt m12,m13 --player Mia:m5 --player Leo:m8

--adopt      monsters a demo player can "hatch" (they get them at the normal price)
--player     other players and their monsters, under new names, to battle against
Wild monsters always go in. Only each monster's latest version is kept, and its rigged model doubles as the plain one.
"""
import argparse
import hashlib
import json
import os
import struct
import sys
import zipfile

TEXT_CHUNKS = {b"tEXt", b"iTXt", b"zTXt"}


def strip_png_text(data):
    """Image tools embed the prompt and the whole workflow in PNG text chunks. Keep the picture, drop the rest."""
    out, i = [data[:8]], 8
    while i < len(data):
        length = struct.unpack(">I", data[i:i + 4])[0]
        end = i + 12 + length
        if data[i + 4:i + 8] not in TEXT_CHUNKS:
            out.append(data[i:end])
        i = end
    return b"".join(out)


def trim(m, owner, media, files):
    v = dict(m["versions"][-1])
    v["v"] = 1
    model = v.get("rig") or v.get("glb")
    if not model:
        raise SystemExit(f"{m['id']} has no 3D model yet")
    v["glb"] = model
    files.update(f for f in (v["image"], model, model[:-4] + ".json") if os.path.exists(os.path.join(media, f)))
    return {**m, "owner": owner, "wins": 0, "losses": 0, "xp": 0, "versions": [v]}


def main():
    if sys.argv[1:2] == ["--strip"]:  # python3 make_demo_pack.py --strip a.png b.png
        for path in sys.argv[2:]:
            data = open(path, "rb").read()
            open(path, "wb").write(strip_png_text(data))
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("out")
    ap.add_argument("--adopt", required=True)
    ap.add_argument("--player", action="append", default=[], help="Name:id1,id2")
    a = ap.parse_args()
    state = json.load(open(os.path.join(a.data, "state.json"), encoding="utf-8"))
    media, files = os.path.join(a.data, "media"), set()
    pack = {"players": {}, "monsters": {}, "next_id": state["next_id"]}
    for mid in ("wild1", "wild2"):
        pack["monsters"][mid] = trim(state["monsters"][mid], None, media, files)
    for mid in a.adopt.split(","):
        pack["monsters"][mid] = {**trim(state["monsters"][mid], None, media, files), "adoptable": True}
    for spec in a.player:
        name, ids = spec.split(":")
        ids = ids.split(",")
        for mid in ids:
            pack["monsters"][mid] = trim(state["monsters"][mid], name, media, files)
        pack["players"][name] = {"name": name, "tokens": 100, "monster": ids[-1]}
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("state.json", json.dumps(pack, ensure_ascii=False, indent=1))
        for f in sorted(files):
            data = open(os.path.join(media, f), "rb").read()
            z.writestr("media/" + f, strip_png_text(data) if f.endswith(".png") else data)
    with open(a.out, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    print(f"{a.out}: {len(pack['monsters'])} monsters, {len(files)} files, {os.path.getsize(a.out) / 1e6:.0f} MB\nsha256 {digest} (goes in DEMO_PACK_SHA256 in server.py)")


if __name__ == "__main__":
    main()
