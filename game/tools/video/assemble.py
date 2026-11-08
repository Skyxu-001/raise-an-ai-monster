#!/usr/bin/env python3
"""Join the cards and scenes, lay narration and music on top, and write 1080p30 H.264 + AAC (what YouTube recommends).

  python3 assemble.py <take dir> <cards dir> <out.mp4> [--vo <dir>] [--music <file>]

--vo holds one <scene>.wav per scene (title, enter, type, ... end). A scene shorter than its narration
holds its last frame. --music is looped under everything and ducks while the narration plays.
Scene start times go to <out>.timeline.json.
"""
import argparse
import json
import os
import subprocess

ap = argparse.ArgumentParser()
ap.add_argument("take")
ap.add_argument("cards")
ap.add_argument("out")
ap.add_argument("--vo")
ap.add_argument("--music")
ap.add_argument("--voice-lead", type=float, default=0.12, help="seconds into a scene before its narration starts")
a = ap.parse_args()


def run(*cmd, **kw):
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-y", *cmd], check=True, **kw)


def seconds(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    return float(out.stdout)


parts = [os.path.join(a.cards, "00-title")]
parts += sorted(os.path.join(a.take, d) for d in os.listdir(a.take) if d[:2].isdigit() and os.path.isdir(os.path.join(a.take, d)))
parts.append(os.path.join(a.cards, "99-end"))
master = a.out + ".list.txt"
timeline, total = [], 0.0
with open(master, "w") as f:
    for p in parts:
        name = os.path.basename(p)[3:]
        lines = open(os.path.join(p, "list.txt")).read().splitlines()
        # The last "file" line in each list only closes that concat; drop it when joining.
        if lines and lines[-1].startswith("file") and (len(lines) < 2 or not lines[-2].startswith("file")):
            lines = lines[:-1]
        length = sum(float(x.split()[1]) for x in lines if x.startswith("duration"))
        vo = os.path.join(a.vo, name + ".wav") if a.vo else None
        vo_len = seconds(vo) if vo and os.path.exists(vo) else 0
        extra = max(0.0, vo_len + a.voice_lead + 0.15 - length)
        if extra:
            lines[-1] = f"duration {float(lines[-1].split()[1]) + extra:.5f}"
        for ln in lines:
            f.write(f"file '{os.path.abspath(os.path.join(p, ln[6:-1]))}'\n" if ln.startswith("file '") else ln + "\n")
        timeline.append({"scene": name, "start": round(total, 3), "seconds": round(length + extra, 3), "narration": round(vo_len, 3), "held": round(extra, 3)})
        total += length + extra
        print(f"{name:>10}  start {timeline[-1]['start']:6.1f}  {length + extra:5.1f}s  narration {vo_len:5.1f}s" + (f"  held {extra:.1f}s" if extra else ""))
    f.write(f"file '{os.path.abspath(os.path.join(parts[-1], sorted(x for x in os.listdir(parts[-1]) if x.endswith('.jpg'))[-1]))}'\n")
print(f"{'total':>10}  {total:6.1f}s")
json.dump(timeline, open(a.out + ".timeline.json", "w"), indent=1)

# ---------- audio ----------
inputs, chains, labels = [], [], []
for t in timeline:
    vo = os.path.join(a.vo, t["scene"] + ".wav") if a.vo else None
    if vo and os.path.exists(vo):
        n = len(inputs) // 2
        inputs += ["-i", vo]
        delay = int((t["start"] + a.voice_lead) * 1000)
        chains.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={delay}|{delay}[v{n}]")
        labels.append(f"[v{n}]")
audio = a.out + ".mix.wav"
if labels:
    graph = ";".join(chains) + f";{''.join(labels)}amix=inputs={len(labels)}:normalize=0,apad=whole_dur={total:.3f}[voice]"
    if a.music:
        n = len(labels)
        inputs += ["-stream_loop", "-1", "-i", a.music]
        graph += (f";[voice]asplit[voice1][key];[{n}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.3f},volume=0.5,"
                  f"afade=t=out:st={total - 2.5:.3f}:d=2.5[bed];"
                  "[bed][key]sidechaincompress=threshold=0.03:ratio=6:attack=40:release=450[ducked];"
                  "[voice1][ducked]amix=inputs=2:normalize=0[out]")
    else:
        graph += ";[voice]anull[out]"
    run(*inputs, "-filter_complex", graph, "-map", "[out]", "-t", f"{total:.3f}", "-ar", "48000", "-c:a", "pcm_s24le", audio, "-loglevel", "error")
    # Two-pass loudness: YouTube plays back at about -14 LUFS.
    probe = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", audio, "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"], capture_output=True, text=True, check=True).stderr
    m = json.loads(probe[probe.rindex("{"):probe.rindex("}") + 1])
    print("loudness before:", m["input_i"], "LUFS, peak", m["input_tp"])
    final = a.out + ".final.wav"
    run("-i", audio, "-af", f"loudnorm=I=-14:TP=-1.5:LRA=11:linear=true:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']},aresample=48000",
        "-c:a", "pcm_s24le", final, "-loglevel", "error")
    audio_in = ["-i", final]
else:
    audio_in = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

run("-loglevel", "error", "-f", "concat", "-safe", "0", "-i", master, *audio_in,
    "-vf", "fps=30,format=gbrp,scale=1920:1080:flags=lanczos:out_range=tv:out_color_matrix=bt709,format=yuv420p",
    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
    "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-profile:v", "high", "-bf", "2", "-g", "15",
    "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", a.out)
for leftover in (master, a.out + ".mix.wav", a.out + ".final.wav"):
    if os.path.exists(leftover):
        os.remove(leftover)
print(a.out, os.path.getsize(a.out) // 1024 // 1024, "MB")
