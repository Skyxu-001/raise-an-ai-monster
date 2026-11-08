#!/usr/bin/env python3
"""One-off generator for battle backdrops (FLUX.2 klein on the GPU machine, about 10 seconds each), saved as static/backdrops/<name>.jpg.

In battle the backdrop is picked by the opponent's first element (a wild battle = fighting on the opponent's turf); the mapping is BACKDROP in static/index.html.
Usage (the tunnel must be up, which it is while the game server runs): python3 game/make_backdrops.py [name ...] [--seed N]
With no names, redraws them all. JPG conversion uses sips, which ships with macOS.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import comfy_client  # noqa: E402

OUT = os.path.join(HERE, "static", "backdrops")
COMMON = ("Wide empty battle arena landscape for a cute creature-battle video game, seen from a low camera at eye level. "
          "The flat open ground fills the lower half of the picture and stays clear and empty in the middle so creatures can stand on it; "
          "the horizon sits a little above the middle. Bright, cheerful, soft daylight, stylized 3D game environment art, "
          "gentle colors, slight depth of field on the far background. No creatures, no people, no animals, no text, no UI.")
SCENES = {
    "meadow": "A sunny grassy meadow with short green grass and a few small flowers, rolling green hills and a forest far away, "
              "blue sky with fluffy white clouds.",
    "volcano": "A warm rocky volcanic plain with dark brown and reddish stone ground and small glowing lava cracks at the edges, "
               "a smoking volcano far away, warm orange sky.",
    "canyon": "A sandy desert canyon floor with smooth orange sand, tall layered red rock cliffs and mesas in the distance, clear pale blue sky.",
    "beach": "A bright sandy beach by calm turquoise water, gentle waves on the left and right far behind, small palm trees, "
             "light blue sky with a few clouds.",
    "snow": "A snowy field with smooth white snow ground, frosted pine trees and icy blue mountains in the distance, pale blue sky, light snowfall.",
    "storm": "A high grassy plateau under dramatic purple-gray storm clouds with distant lightning, wet green grass, misty mountains far away.",
    "twilight": "A quiet forest clearing at dusk with soft mossy ground, tall dark trees and ancient stone ruins far behind, "
                "purple and pink twilight sky, a few glowing fireflies.",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="redraw only these; default is all: " + " ".join(SCENES))
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    for name in args.names or SCENES:
        png = os.path.join(OUT, name + ".png")
        outputs, dt = comfy_client.run(comfy_client.t2i_prompt(SCENES[name] + " " + COMMON, args.seed, width=1344, height=768), front=True)
        comfy_client.download(outputs["save"]["images"][0], png)
        jpg = png[:-4] + ".jpg"
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "82", png, "--out", jpg], check=True, capture_output=True)
        os.remove(png)
        print(f"{name}: {dt:.1f} s → {jpg} ({os.path.getsize(jpg) // 1024} KB)", flush=True)


if __name__ == "__main__":
    main()
