#!/usr/bin/env python3
"""Game server for the AI monster PoC: a stdlib HTTP server plus one JSON save file, no third-party dependencies.

Runs on any machine and reaches ComfyUI (127.0.0.1:8188) and Ollama (127.0.0.1:11434) on the GPU machine over SSH tunnels. The server opens the tunnels itself, checks them every 30 seconds and reconnects if one drops.
  python3 server.py          # listens on 127.0.0.1:8191 by default
  GAME_HOST=0.0.0.0 python3 server.py    # there is no login, so private networks only (see the README)
Data (save file, pictures, GLBs) lives in game/data/ and stays out of git.
"""
import hashlib
import http.client
import json
import os
import queue
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import comfy_client  # noqa: E402
import ai  # noqa: E402
import rules  # noqa: E402

if __name__ == "__main__" and set(sys.argv[1:]) - {"--demo"}:  # stop here, so a typo or --help doesn't start a server and write a save file
    sys.exit("Usage: python3 game/server.py [--demo]\nEverything else is set with environment variables; the README lists them.")

# Demo mode: no GPU and no AI models. It plays with a pack of monsters made earlier on real hardware
DEMO = "--demo" in sys.argv or os.environ.get("GAME_DEMO") == "1"
DEMO_PACK_DEFAULT = "https://github.com/Skyxu-001/raise-an-ai-monster/releases/download/demo-v1/demo-pack.zip"
DEMO_PACK_SHA256 = "420f3dcee44a5b234ef26fd7a51759959b9f247248eb4eb09d1511fcc070e16e"  # of the pack at DEMO_PACK_DEFAULT; make_demo_pack.py prints this
DEMO_PACK_URL = os.environ.get("GAME_DEMO_PACK", DEMO_PACK_DEFAULT)
DEMO_NO_AI = "This needs the AI models, and demo mode runs without them. The README shows how to connect a GPU machine"
DATA = os.environ.get("GAME_DATA") or os.path.join(HERE, "demo_data" if DEMO else "data")
MEDIA = os.path.join(DATA, "media")
STATE_FILE = os.path.join(DATA, "state.json")
HOST = os.environ.get("GAME_HOST", "127.0.0.1")
PORT = int(os.environ.get("GAME_PORT", "8191"))
DESC_MAX = 1000  # longest description a player can send to the AI
BODY_MAX = 64 * 1024  # largest request the server will read
IDLE_FREE = int(os.environ.get("GAME_IDLE_FREE", "1200"))  # after this many seconds with no image or 3D job, tell ComfyUI to unload its models (the GPU machine may be shared)
LAST_GPU = [0.0]  # 0 = already unloaded or not used yet
TUNNEL_HOST = os.environ.get("GAME_TUNNEL_HOST", "")  # empty = the server leaves tunnels alone (open them yourself)
TUNNELS = [tuple(map(int, t.split(":"))) for t in os.environ.get("GAME_TUNNELS", "8188:8188,11434:11434").split(",")]  # local port → port on the GPU machine: ComfyUI, Ollama
RIG = os.environ.get("GAME_RIG", "0") == "1"  # auto-rig on the GPU machine once the 3D model is done (SkinTokens, see gpu/rig/)
RIG_DIR = os.environ.get("GAME_RIG_DIR", "skintokens")  # SkinTokens repo on the GPU machine, relative to the home directory
RIG_CONTAINER = os.environ.get("GAME_RIG_CONTAINER", "comfyui")  # docker container that runs the rig script
RIG_WORKDIR = os.environ.get("GAME_RIG_WORKDIR", "/workspace/skintokens")  # the same folder as seen inside the container
RIG_PYTHON = os.environ.get("GAME_RIG_PYTHON", "/opt/venv/bin/python")  # Python inside the container
# Guest entrance: bound to 127.0.0.1 only, so nothing outside can reach it. To let someone play for a while, open an SSH forward to this port from another machine; closing the forward cuts them off
GUEST_PORT = int(os.environ.get("GAME_GUEST_PORT", "0"))  # 0 = off
GUEST_OPEN = set(filter(None, os.environ.get("GAME_GUEST_OPEN", "").split(",")))  # existing accounts guests may use; other existing accounts can't be reached through this entrance
GUEST_GPU_LIMIT = int(os.environ.get("GAME_GUEST_GPU_LIMIT", "20"))  # max hatches / grows through this entrance per server start (they use the GPU machine)
GUEST_GPU = [0]
MAX_MONSTERS = int(os.environ.get("GAME_MAX_MONSTERS", "0"))  # max monsters per player; 0 = no limit
CTX = threading.local()  # whether the current request came in through the guest entrance
SSH_OPTS = ["-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

LOCK = threading.RLock()
JOBS = queue.Queue()
RIGS = queue.Queue()
BATTLES = {}   # player name → battle in progress (memory only, gone on restart)
PENDING = {}   # player name → growth waiting for confirmation
GROWING = set()  # monster ids with a growth picture being drawn; versions are numbered by position, so two at once would share a number
HATCHING = {}  # player name → monsters still hatching and not in the save yet (they count toward the limit, so concurrent requests can't all get past it)


class GameError(Exception):
    pass


class RequestTooLarge(Exception):
    pass


# ---------- Save file ----------

def load():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"players": {}, "monsters": {}, "next_id": 1}


STATE = None


def save():
    tmp = STATE_FILE + ".tmp"
    # backslashreplace: text with a broken character (it can't be encoded) must not stop every later save. It reads back unchanged
    with open(tmp, "w", encoding="utf-8", errors="backslashreplace") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def player(name, create=True):
    """create=False gives a new name the page it would see, without making the account: that is for GETs, which any web page can make the browser send."""
    name = clean_name(name)[:20].rstrip()
    if not name:
        raise GameError("Enter a player name first")
    guest = getattr(CTX, "guest", False)
    with LOCK:
        p = STATE["players"].get(name)
        if p and guest and not p.get("guest") and name not in GUEST_OPEN:
            raise GameError("That name is already taken. Pick another one")
        if not p:
            p = {"name": name, "tokens": rules.START_TOKENS, "monster": None}
            ledger(p, rules.START_TOKENS, "Starting Tokens")
            if guest:
                p["guest"] = True
            if create:
                STATE["players"][name] = p
                save()
    return p


def guest_gpu_slot(take=True):
    """Refuse once the guest entrance hits its hatch / grow limit, so the GPU queue doesn't get long. Learning moves and battles have no limit.
    take=False only checks, for a growth preview: the slot is taken when the growth is confirmed."""
    if getattr(CTX, "guest", False):
        with LOCK:
            if GUEST_GPU[0] >= GUEST_GPU_LIMIT:
                raise GameError("This shared demo has reached its hatch / grow limit for now. You can still learn skills and battle")
            GUEST_GPU[0] += take


def guest_gpu_return():
    """The job failed, so it added nothing to the GPU queue."""
    if getattr(CTX, "guest", False):
        with LOCK:
            GUEST_GPU[0] -= 1


def roster(p):
    """All of this player's monsters in hatch order; p["monster"] is the active one."""
    return sorted((m for m in STATE["monsters"].values() if m.get("owner") == p["name"]), key=lambda m: int(m["id"][1:]))


def clean_desc(body):
    desc = body.get("desc")
    desc = desc.strip() if isinstance(desc, str) else ""
    if len(desc) > DESC_MAX:
        raise GameError(f"That description is too long. Keep it under {DESC_MAX} characters")
    return desc


def index_arg(body, default):
    try:
        return int(body.get("idx", default))
    except (TypeError, ValueError):
        raise GameError("No such skill") from None


def clean_name(s):
    """Collapse whitespace to one space; drop zero-width and control characters (a name with only those counts as empty); a non-string counts as empty."""
    s = "".join(c if c.isprintable() or c.isspace() else "" for c in s) if isinstance(s, str) else ""
    return " ".join(s.split())[:24].rstrip()


def my_monster(p):
    m = STATE["monsters"].get(p["monster"]) if p["monster"] else None
    if not m:
        raise GameError("You don't have a monster yet. Hatch one first")
    return m


def can_afford(p, cost):
    if p["tokens"] < cost:
        raise GameError(f"Not enough Tokens: this costs {cost} and you have {p['tokens']}. Win battles to earn more")


def ledger(p, delta, why, **ai_work):
    """Add a row to the player's Token ledger. Returns it, so the AI work can be filled in once it is known."""
    row = {"ts": time.time(), "delta": delta, "why": why, **ai_work}
    rows = p.setdefault("ledger", [])
    rows.append(row)
    del rows[:-rules.LEDGER_KEEP]
    return row


def charge(p, cost, why, **ai_work):
    can_afford(p, cost)
    p["tokens"] -= cost
    return ledger(p, -cost, why, **ai_work)


def refund(p, row):
    p["tokens"] -= row["delta"]
    if row in p.get("ledger", []):
        p["ledger"].remove(row)


def new_monster_id():
    mid = f"m{STATE['next_id']}"
    STATE["next_id"] += 1
    return mid


# ---------- 3D job queue: one thread, one job at a time (ComfyUI is a single queue anyway) ----------

def worker():
    while True:
        mid, ver = JOBS.get()
        with LOCK:
            m = STATE["monsters"].get(mid)
            if not m or ver > len(m["versions"]):
                continue
            v = m["versions"][ver - 1]
            v["glb_status"] = "running"
            v["glb_started"] = time.time()
            save()
            image = os.path.join(MEDIA, v["image"])
        try:
            name = comfy_client.upload_image(image)
            touch_gpu()
            outputs, dt = comfy_client.run(comfy_client.i2m_prompt(name, fast=True))
            touch_gpu()
            files = [f for o in outputs.values() for k, val in o.items() if isinstance(val, list) for f in val
                     if isinstance(f, dict) and str(f.get("filename", "")).endswith(".glb")]
            if not files:
                raise RuntimeError("ComfyUI produced no .glb file")
            glb = f"{mid}_v{ver}.glb"
            comfy_client.download(files[0], os.path.join(MEDIA, glb))
            with LOCK:
                v.update(glb=glb, glb_status="done", glb_seconds=round(dt))
                queue_rig(m, v)
                save()
        except Exception as e:  # noqa: BLE001  any failure is recorded on this version and shown on the page
            traceback.print_exc()
            with LOCK:
                v.update(glb_status="error", glb_error=str(e)[:500])
                save()


def touch_gpu():
    LAST_GPU[0] = time.time()


def idle_free():
    while True:
        time.sleep(60)
        if LAST_GPU[0] and JOBS.empty() and time.time() - LAST_GPU[0] > IDLE_FREE:
            try:
                comfy_client.http("POST", "/free", json.dumps({"unload_models": True, "free_memory": True}).encode(),
                               {"Content-Type": "application/json"})
                LAST_GPU[0] = 0.0
                print(time.strftime("%H:%M:%S"), f"Idle for over {IDLE_FREE} s, told ComfyUI to unload models", flush=True)
            except Exception as e:  # noqa: BLE001
                print("Failed to unload models:", e, flush=True)


# ---------- Auto-rigging: an extra step after the 3D model. If it fails, battles fall back to whole-body motion; it blocks nothing ----------

def queue_rig(m, v):
    if RIG and TUNNEL_HOST and v.get("glb"):
        v["rig_status"] = "queued"
        RIGS.put((m["id"], v["v"]))


def rig_worker():
    while True:
        mid, ver = RIGS.get()
        with LOCK:
            m = STATE["monsters"].get(mid)
            if not m or ver > len(m["versions"]) or not m["versions"][ver - 1].get("glb"):
                continue
            v = m["versions"][ver - 1]
            v["rig_status"] = "running"
            save()
            stem = v["glb"][:-4]
        remote = f"{RIG_DIR}/jobs/{stem}"
        t0 = time.time()
        try:
            def run(cmd, timeout=120):
                subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
            run(["ssh", *SSH_OPTS, TUNNEL_HOST, f"mkdir -p {RIG_DIR}/jobs"])
            run(["scp", "-q", *SSH_OPTS, os.path.join(MEDIA, stem + ".glb"), f"{TUNNEL_HOST}:{remote}.glb"])
            run(["ssh", *SSH_OPTS, TUNNEL_HOST, f"docker exec {RIG_CONTAINER} sh -c 'cd {RIG_WORKDIR} && "
                 f"{RIG_PYTHON} rig_glb.py jobs/{stem}.glb jobs/{stem}_rig.glb'"], timeout=900)
            run(["scp", "-q", *SSH_OPTS, f"{TUNNEL_HOST}:{remote}_rig.glb", f"{TUNNEL_HOST}:{remote}_rig.json", MEDIA + "/"])
            run(["ssh", *SSH_OPTS, TUNNEL_HOST, f"rm -f {remote}.glb {remote}_rig.glb {remote}_rig.json"])
            with LOCK:
                v.update(rig=stem + "_rig.glb", rig_status="done", rig_seconds=round(time.time() - t0))
                save()
            print(time.strftime("%H:%M:%S"), f"{mid} v{ver} rigged ({round(time.time() - t0)} s)", flush=True)
        except Exception as e:  # noqa: BLE001
            err = e.stderr.decode(errors="replace")[-300:] if isinstance(e, subprocess.CalledProcessError) and e.stderr else str(e)
            print(time.strftime("%H:%M:%S"), f"{mid} v{ver} rigging failed: {err}", flush=True)
            with LOCK:
                v.update(rig_status="error", rig_error=err[:300])
                save()


def queue_3d(m):
    v = m["versions"][-1]
    v["glb_status"] = "queued"
    JOBS.put((m["id"], v["v"]))


# ---------- SSH tunnels to the GPU machine ----------

def port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        return True
    except OSError:
        return False


def ensure_tunnels():
    if not TUNNEL_HOST:
        return
    for local, remote in TUNNELS:
        if port_open(local):
            continue
        # A separate connection, not the ControlMaster sharing from ~/.ssh/config: when ControlPersist times out and closes the shared connection,
        # any tunnel on it goes down too (this really happened). stdout isn't piped, or the ssh that -f puts in the background would hold the pipe open
        r = subprocess.run(["ssh", "-f", "-N", "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "BatchMode=yes",
                            "-o", "ConnectTimeout=10", "-o", "ExitOnForwardFailure=yes",
                            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                            "-L", f"127.0.0.1:{local}:127.0.0.1:{remote}", TUNNEL_HOST],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=60)
        print(time.strftime("%H:%M:%S"), f"Tunnel {local} → {TUNNEL_HOST}:{remote}", "reopened" if r.returncode == 0 else
              f"reopen failed (ssh exit code {r.returncode})", flush=True)


def tunnel_watch():
    while True:
        time.sleep(30)
        try:
            ensure_tunnels()
        except Exception as e:  # noqa: BLE001
            print("Tunnel check error:", e, flush=True)


def unreachable(e):
    """Errors from not reaching the GPU machine (tunnel down, service not running), as opposed to game logic errors."""
    return isinstance(e, (urllib.error.URLError, ConnectionError, socket.timeout)) and not isinstance(e, urllib.error.HTTPError)


def spark_health():
    if DEMO:
        return {"image": True, "llm": True, "demo": True}
    out = {}
    for key, url in (("image", comfy_client.BASE + "/system_stats"), ("llm", ai.OLLAMA + "/api/version")):
        try:
            urllib.request.urlopen(url, timeout=3).read()
            out[key] = True
        except Exception:  # noqa: BLE001
            out[key] = False
    return out


# ---------- Images (take seconds, so they jump to the front of the ComfyUI queue) ----------

def gen_image(text, dest):
    touch_gpu()
    outputs, _ = comfy_client.run(comfy_client.t2i_prompt(text, random.randrange(2**31)), front=True)
    comfy_client.download(outputs["save"]["images"][0], os.path.join(MEDIA, dest))


def edit_image(src, text, dest):
    touch_gpu()
    name = comfy_client.upload_image(os.path.join(MEDIA, src))
    outputs, _ = comfy_client.run(comfy_client.edit_prompt(name, text, random.randrange(2**31)), front=True)
    comfy_client.download(outputs["save"]["images"][0], os.path.join(MEDIA, dest))


# ---------- Gameplay ----------

def demo_adopt(p, name):
    """Demo mode has no AI, so hatching hands over a ready-made monster, at the normal price and with its real receipt."""
    with LOCK:
        m = next((m for m in STATE["monsters"].values() if m.get("adoptable")), None)
        if not m:
            raise GameError("All the ready-made monsters are taken. Delete game/demo_data/ to start the demo over")
        cost = rules.hatch_cost(len(roster(p)))
        v = m["versions"][-1]
        charge(p, cost, f"Hatch {name or m['name']}", mid=m["id"], v=len(m["versions"]), llm_tokens=v.get("llm_tokens", 0),
               image_seconds=v.get("image_seconds"))
        del m["adoptable"]
        m["owner"] = p["name"]
        m["name"] = name or m["name"]
        p["monster"] = m["id"]
        save()
    return {"ignored_claims": [], "cost": cost, "llm_tokens": v.get("llm_tokens", 0), "image_seconds": v.get("image_seconds")}


def act_hatch(body):
    p = player(body.get("player"))
    if DEMO:
        return demo_adopt(p, clean_name(body.get("name")))
    plan = body.get("body_plan")
    desc = clean_desc(body)
    if plan not in rules.BODY_PLANS:
        raise GameError("Pick a body type first")
    if len(desc) < 4:
        raise GameError("Write a bit more about how it looks")
    name = clean_name(body.get("name"))
    with LOCK:
        if MAX_MONSTERS and len(roster(p)) + HATCHING.get(p["name"], 0) >= MAX_MONSTERS:
            raise GameError(f"You can keep up to {MAX_MONSTERS} monsters. Release one first")
        cost = rules.hatch_cost(len(roster(p)) + HATCHING.get(p["name"], 0))
        can_afford(p, cost)
        guest_gpu_slot()
        row = charge(p, cost, "Hatch")
        HATCHING[p["name"]] = HATCHING.get(p["name"], 0) + 1
        save()
    done = False
    try:
        prof = ai.hatch(desc, plan)  # no lock during the LLM call; slow work stays outside the lock
        with LOCK:
            mid = new_monster_id()
            m = {"id": mid, "owner": p["name"], "name": name or clean_name(prof["name"]) or "Nameless", "level": rules.LEVEL_START, "xp": 0,
                 "body_plan": plan, "elements": rules.clean_elements(prof["elements"]),
                 "parts": rules.clean_parts(prof["parts"]), "palette": prof["palette"][:4], "skills": [],
                 "wins": 0, "losses": 0, "versions": []}
        img = f"{mid}_v1.png"
        t0 = time.time()
        gen_image(ai.image_text(m, prof["image_prompt"]), img)
        image_seconds = round(time.time() - t0, 1)
        with LOCK:  # earlier monsters stay and the new one becomes active; battles in progress are left alone (rewards go by me_id)
            m["versions"].append({"v": 1, "kind": "Hatch", "desc": desc, "image": img, "glb": None, "ts": time.time(),
                                  "ignored_claims": prof["ignored_claims"], "llm_tokens": prof.get("llm_tokens", 0),
                                  "image_seconds": image_seconds})
            STATE["monsters"][mid] = m
            p["monster"] = mid
            PENDING.pop(p["name"], None)  # cancel the previous monster's pending growth
            queue_3d(m)
            row.update(why=f"Hatch {m['name']}", mid=mid, v=1, llm_tokens=prof.get("llm_tokens", 0), image_seconds=image_seconds)
            save()
        done = True
    finally:
        with LOCK:
            HATCHING[p["name"]] -= 1
            if not HATCHING[p["name"]]:
                del HATCHING[p["name"]]
            if not done:  # the AI or the image model failed: the Tokens go back
                refund(p, row)
                guest_gpu_return()
                save()
    return {"ignored_claims": prof["ignored_claims"], "cost": cost, "llm_tokens": row["llm_tokens"], "image_seconds": image_seconds}


def act_grow_preview(body):
    p = player(body.get("player"))
    if DEMO:
        raise GameError(DEMO_NO_AI)
    desc = clean_desc(body)
    if len(desc) < 2:
        raise GameError("Describe how it should grow")
    m = my_monster(p)
    can_afford(p, rules.COST_GROW)  # no AI work for a growth the player can't pay for
    guest_gpu_slot(take=False)
    if m["id"] in GROWING:  # checked again below: one may start while the AI is thinking
        raise GameError("This monster is still growing. Wait for that to finish")
    out = ai.grow(m, desc)
    new = {**m, "elements": rules.clean_elements(out["elements"]), "parts": rules.clean_parts(out["parts"]),
           "palette": out["palette"][:4] or m["palette"]}
    changes, deltas = rules.profile_changes(m, new, m["level"])
    with LOCK:
        if p["monster"] != m["id"]:
            raise GameError("You switched monsters while the AI was thinking. Try again")
        if m["id"] in GROWING:
            raise GameError("This monster is still growing. Wait for that to finish")
        PENDING[p["name"]] = {"mid": m["id"], "desc": desc, "new": {k: new[k] for k in ("elements", "parts", "palette")},
                              "edit_instruction": out["edit_instruction"], "ignored_claims": out["ignored_claims"],
                              "llm_tokens": out.get("llm_tokens", 0)}
    return {"changes": changes, "deltas": deltas, "cost": rules.COST_GROW, "ignored_claims": out["ignored_claims"],
            "edit_instruction": out["edit_instruction"], "llm_tokens": out.get("llm_tokens", 0)}


def act_grow_confirm(body):
    p = player(body.get("player"))
    with LOCK:
        m = my_monster(p)
        pend = PENDING.get(p["name"])
        if not pend or pend["mid"] != m["id"]:
            raise GameError("There is no growth waiting to be confirmed")
        can_afford(p, rules.COST_GROW)  # before anything else, so a player who is short keeps the preview and the guest limit isn't used up
        guest_gpu_slot()
        del PENDING[p["name"]]
        GROWING.add(m["id"])
        m2 = {**m, **pend["new"]}
        ver = len(m["versions"]) + 1
        row = charge(p, rules.COST_GROW, f"Grow {m['name']}", mid=m["id"], v=ver, llm_tokens=pend.get("llm_tokens", 0))
        save()
    img = f"{m['id']}_v{ver}.png"
    t0 = time.time()
    try:
        edit_image(m["versions"][-1]["image"], ai.edit_text(m2, pend["edit_instruction"]), img)
    except Exception:
        with LOCK:
            GROWING.discard(m["id"])
            refund(p, row)  # refund if the image fails
            guest_gpu_return()
            if p["monster"] == m["id"]:
                PENDING.setdefault(p["name"], pend)  # the error says to try again, so what they confirmed stays
            save()
        raise
    with LOCK:
        GROWING.discard(m["id"])
        if m["id"] not in STATE["monsters"]:  # released while the image was being made
            refund(p, row)
            save()
            raise GameError("That monster was released, so the growth was cancelled and refunded")
        m.update(pend["new"])
        m["versions"].append({"v": ver, "kind": "Growth", "desc": pend["desc"], "image": img, "glb": None,
                              "ts": time.time(), "ignored_claims": pend["ignored_claims"], "llm_tokens": row["llm_tokens"],
                              "image_seconds": round(time.time() - t0, 1)})
        row["image_seconds"] = m["versions"][-1]["image_seconds"]
        queue_3d(m)
        save()
    return {"cost": rules.COST_GROW, "llm_tokens": row["llm_tokens"], "image_seconds": row["image_seconds"]}


def act_grow_cancel(body):
    PENDING.pop(player(body.get("player"))["name"], None)
    return {}


def act_learn(body):
    p = player(body.get("player"))
    m = my_monster(p)
    if DEMO:
        raise GameError(DEMO_NO_AI)
    desc = clean_desc(body)
    if len(desc) < 2:
        raise GameError("Describe what the move looks like")
    if len(m["skills"]) >= rules.MAX_SKILLS:
        raise GameError(f"All {rules.MAX_SKILLS} skill slots are full. Forget one first")
    can_afford(p, min(rules.COST_SKILL.values()))  # the real price depends on the AI's answer, but no AI work for a player who can't pay even the lowest
    s = ai.skill(m, desc)
    if s["template"] not in rules.TEMPLATES or s["fit"] not in rules.COST_SKILL or s["element"] not in rules.ELEMENT_LABEL:
        raise GameError("The AI answered with a move the game can't use. Nothing was charged; try again")  # the schema should stop this; code doesn't rely on it
    els = rules.active_elements(m)
    if s["element"] != "normal" and s["element"] not in els:
        hint = " (its second element unlocks at Lv.%d)" % rules.UNLOCK_LEVEL if s["element"] in m["elements"] else ""
        raise GameError(f"{s['name']} is a {rules.ELEMENT_LABEL[s['element']]} move, but your monster is only "
                        f"{'/'.join(rules.ELEMENT_LABEL[e] for e in els)} right now, so it can't learn it{hint}")
    if s["requires_flight"] and not rules.can_fly(m):
        why = "it has no wings" if not rules.flight_body(m) else f"flight isn't unlocked yet (it unlocks at Lv.{rules.UNLOCK_LEVEL})"
        raise GameError(f"{s['name']} needs flight, but {why}")
    cost = rules.COST_SKILL[s["fit"]]
    with LOCK:  # while the AI was thinking, the player may have switched or released it, or another request may have filled its moves first
        if p["monster"] != m["id"]:
            raise GameError("You switched monsters while the AI was thinking. Nothing was charged; try again")
        if len(m["skills"]) >= rules.MAX_SKILLS:
            raise GameError(f"All {rules.MAX_SKILLS} skill slots are full. Forget one first")
        charge(p, cost, f"Learn {s['name'][:24]}", llm_tokens=s.get("llm_tokens", 0))
        skill = {"name": s["name"][:24], "template": s["template"], "element": s["element"], "fit": s["fit"],
                 "fit_reason": s["fit_reason"], "requires_flight": s["requires_flight"], "source": s["source"],
                 "desc": desc}
        m["skills"].append(skill)
        save()
    return {"skill": skill, "cost": cost, "ignored_claims": s["ignored_claims"], "llm_tokens": s.get("llm_tokens", 0)}


def act_forget(body):
    p = player(body.get("player"))
    m = my_monster(p)
    i = index_arg(body, -1)
    with LOCK:
        if not 0 <= i < len(m["skills"]):
            raise GameError("No such skill")
        m["skills"].pop(i)
        save()
    return {}


def act_release(body):
    """Release the active monster (deleted for good) and switch to the newest one left."""
    p = player(body.get("player"))
    with LOCK:
        if p["monster"]:
            STATE["monsters"].pop(p["monster"], None)
            rest = roster(p)
            p["monster"] = rest[-1]["id"] if rest else None
            save()
        BATTLES.pop(p["name"], None)
        PENDING.pop(p["name"], None)
    return {}


def act_select(body):
    p = player(body.get("player"))
    mid = body.get("id")
    with LOCK:
        if mid not in [m["id"] for m in roster(p)]:
            raise GameError("That monster isn't yours")
        p["monster"] = mid
        save()
        BATTLES.pop(p["name"], None)
        PENDING.pop(p["name"], None)
    return {}


def act_rename(body):
    p = player(body.get("player"))
    name = clean_name(body.get("name"))
    if not name:
        raise GameError("Enter a name")
    with LOCK:
        my_monster(p)["name"] = name
        save()
    return {}


def act_battle_start(body):
    p = player(body.get("player"))
    with LOCK:
        m = my_monster(p)
        foe = STATE["monsters"].get(body.get("opponent"))
        if not foe or foe["id"] == m["id"]:
            raise GameError("Can't find that opponent")
        # mons keeps both monsters themselves: if the opponent's owner releases it mid-battle, the page can still get its picture and 3D model
        BATTLES[p["name"]] = {"turn": 0, "log": [], "winner": None, "me": rules.fighter(m),
                              "foe": rules.fighter(foe), "me_id": m["id"], "mons": {"me": m, "foe": foe}}
    return {}


def act_battle_move(body):
    p = player(body.get("player"))
    idx = index_arg(body, 0)
    with LOCK:  # the whole turn, which is quick: two requests on the last turn must not both collect the reward
        b = BATTLES.get(p["name"])
        if not b or b["winner"]:
            raise GameError("No battle in progress")
        if not 0 <= idx < len(b["me"]["skills"]):
            raise GameError("No such skill")
        out = {"events": rules.play_turn(b, idx)}
        if b["winner"]:
            m = STATE["monsters"].get(b["me_id"])
            if not m:
                BATTLES.pop(p["name"], None)
                raise GameError("Your monster is gone")
            b["reward"] = rules.gain(m, p, b["winner"] == "me")
            ledger(p, b["reward"]["tokens"], f"{'Beat' if b['winner'] == 'me' else 'Lost to'} {b['foe']['name']}")
            save()
    return out


def act_battle_leave(body):
    BATTLES.pop(player(body.get("player"))["name"], None)
    return {}


def view(name):
    with LOCK:
        p = player(name, create=False)
        mine = None
        if p["monster"] and p["monster"] in STATE["monsters"]:
            mine = rules.describe(STATE["monsters"][p["monster"]])
            if getattr(CTX, "guest", False):  # job errors can name paths and hosts; guests only get the status
                mine["versions"] = [{k: x for k, x in v.items() if k not in ("glb_error", "rig_error")} for v in mine["versions"]]
        opps = []
        for m in STATE["monsters"].values():
            if m["id"] == p["monster"] or m.get("adoptable"):
                continue
            d = rules.describe(m)
            o = {k: d[k] for k in ("id", "name", "owner", "level", "active_elements", "elements", "can_fly",
                                   "body_plan", "wins", "losses")}
            o.update(image=d["versions"][-1]["image"], skills=len(d["skills"]))
            opps.append(o)
        b = BATTLES.get(p["name"])
        battle = None
        if b:
            mm, foe = b["mons"]["me"], b["mons"]["foe"]
            battle = {k: b[k] for k in ("turn", "log", "winner", "me", "foe")}
            battle["reward"] = b.get("reward")
            battle["glb"] = {"me": latest_glb(mm), "foe": latest_glb(foe)}
            battle["rig"] = {"me": latest_rig(mm), "foe": latest_rig(foe)}
            battle["body_plan"] = {"me": mm and mm["body_plan"], "foe": foe and foe["body_plan"]}
            battle["image"] = {"me": mm["versions"][-1]["image"] if mm else None,
                               "foe": foe["versions"][-1]["image"] if foe else None}
        pending = PENDING.get(p["name"])
        mons = [{"id": m["id"], "name": m["name"], "level": m["level"], "image": m["versions"][-1]["image"],
                 "active": m["id"] == p["monster"]} for m in roster(p)]
        return {"player": {k: v for k, v in p.items() if k != "ledger"}, "monster": mine, "roster": mons, "max_monsters": MAX_MONSTERS,
                "opponents": opps, "battle": battle, "queue": JOBS.qsize(), "has_pending": bool(pending),
                "hatch_cost": rules.hatch_cost(len(mons)), "ledger": ledger_view(p), "demo": DEMO}


def ledger_view(p):
    """Newest first. 3D and skeleton time land on the monster's version after the payment, so they are looked up here."""
    out = []
    for row in reversed(p.get("ledger", [])):
        r = dict(row)
        m = STATE["monsters"].get(row.get("mid"))
        if m and row.get("v", 0) <= len(m["versions"]):
            v = m["versions"][row["v"] - 1]
            r.update({k: v[k] for k in ("glb_status", "glb_seconds", "rig_status", "rig_seconds") if k in v})
        out.append(r)
    return out


def latest_glb(m):
    if not m:
        return None
    done = [v["glb"] for v in m["versions"] if v.get("glb")]
    return done[-1] if done else None


def latest_rig(m):
    """Skeleton for the latest 3D version; none until that version is rigged (an old skeleton doesn't fit the new model)."""
    if not m:
        return None
    vs = [v for v in m["versions"] if v.get("glb")]
    return vs[-1].get("rig") if vs else None


# ---------- Wild monsters: the two ready-made ones from the PoC, so there's something to fight right away ----------

WILD = [
    {"id": "wild1", "name": "Emberling", "level": 5, "body_plan": "quadruped", "elements": ["fire"],
     "parts": [{"part": "long_tail", "size": "medium"}, {"part": "big_body", "size": "small"},
               {"part": "claws", "size": "small"}],
     "palette": ["orange", "cream", "yellow"],
     "skills": [{"name": "Ember Spray", "template": "projectile", "element": "fire", "fit": "good", "source": "mouth"},
                {"name": "Tail Whip", "template": "strike", "element": "normal", "fit": "good", "source": "tail"}],
     "image": "hatchling_v1.png", "glb": "hatchling_v1_fast.glb",
     "desc": "A chubby little fire lizard with orange scales, a small flame on the tip of its tail, big eyes and four short legs"},
    {"id": "wild2", "name": "Cragwing", "level": 10, "body_plan": "quadruped", "elements": ["fire", "rock"],
     "parts": [{"part": "long_tail", "size": "medium"}, {"part": "big_body", "size": "small"},
               {"part": "claws", "size": "small"}, {"part": "wings", "size": "small"},
               {"part": "armor_plates", "size": "medium"}],
     "palette": ["orange", "dark gray", "cream"],
     "skills": [{"name": "Flame Jet", "template": "projectile", "element": "fire", "fit": "good", "source": "mouth"},
                {"name": "Lava Beam", "template": "beam", "element": "fire", "fit": "ok", "source": "mouth"},
                {"name": "Rock Armor", "template": "shield", "element": "rock", "fit": "good", "source": "body"},
                {"name": "Rock Charge", "template": "strike", "element": "rock", "fit": "good", "source": "body"}],
     "image": "hatchling_v2_edit.png", "glb": "hatchling_v2_fast.glb",
     "desc": "A pair of small bat wings has grown on its back, and dark rock armor covers its shoulders"},
]


def seed_wild():
    src = os.path.join(ROOT, "assets", "wild")
    for w in WILD:
        if w["id"] in STATE["monsters"]:  # already in the save: name, moves and description come from here (old saves are in Chinese with no source); level and record are kept
            m = STATE["monsters"][w["id"]]
            m.update(name=w["name"], skills=[dict(sk) for sk in w["skills"]])
            m["versions"][0].update(kind="Hatch", desc=w["desc"])
            continue
        if not os.path.exists(os.path.join(src, w["image"])):
            print(f"Skipping wild monster {w['id']}: no picture for it in assets/wild/")
            continue
        img = f"{w['id']}.png"
        shutil.copy(os.path.join(src, w["image"]), os.path.join(MEDIA, img))
        v = {"v": 1, "kind": "Hatch", "desc": w["desc"], "image": img, "ts": time.time()}
        if os.path.exists(os.path.join(src, w["glb"])):  # use the ready-made 3D model if there is one
            shutil.copy(os.path.join(src, w["glb"]), os.path.join(MEDIA, f"{w['id']}.glb"))
            v.update(glb=f"{w['id']}.glb", glb_status="done")
        else:  # otherwise make it: main() then puts queued versions on the 3D queue (each GLB is about 10 MB and stays out of git)
            v["glb_status"] = "queued"
        m = {k: w[k] for k in ("id", "name", "level", "body_plan", "elements", "parts", "palette", "skills")}
        m.update(owner=None, xp=0, wins=0, losses=0, versions=[v])
        STATE["monsters"][w["id"]] = m
    save()


# ---------- HTTP ----------

ACTIONS = {"hatch": act_hatch, "grow/preview": act_grow_preview, "grow/confirm": act_grow_confirm,
           "grow/cancel": act_grow_cancel, "skill/learn": act_learn, "skill/forget": act_forget,
           "release": act_release, "monster/select": act_select, "monster/rename": act_rename,
           "battle/start": act_battle_start,
           "battle/move": act_battle_move, "battle/leave": act_battle_leave}
TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".glb": "model/gltf-binary", ".html": "text/html; charset=utf-8",
         ".json": "application/json", ".js": "text/javascript; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    guest = False

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # No cache for pages and scripts: an arena.js change shows up on refresh (with the old one-day cache, changes took until the next day to appear). Image and model file names carry a version, so those can be cached
        self.send_header("Cache-Control", "no-store" if ctype.startswith(("application/json", "text/html", "text/javascript")) else "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _refused(self):
        """Any web page the player has open can send requests to this server from their browser, so requests from other sites are refused."""
        origin, host = self.headers.get("Origin"), self.headers.get("Host") or ""
        if origin and urllib.parse.urlsplit(origin).netloc != host:
            return "Requests from other sites aren't allowed"
        # A hostile site can point its own name at 127.0.0.1, and then its Origin matches. Guests are exempt: they arrive over a forward, under any address
        if not self.guest and HOST in ("127.0.0.1", "localhost") and host and host.split(":")[0] not in ("127.0.0.1", "localhost"):
            return "This server only answers to 127.0.0.1 and localhost. To share it, use the guest entrance (see the README)"
        return None

    def _json_body(self):
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if not 0 <= size <= BODY_MAX:
                self.close_connection = True  # the body is left unread
                raise RequestTooLarge()
            raw = self.rfile.read(size) or b"{}"
            body = json.loads(raw)
            json.dumps(body, ensure_ascii=False).encode()  # a broken character (a lone surrogate) would pass json.loads and then fail on the way out
        except (ValueError, UnicodeError):
            raise GameError("That request isn't valid JSON text") from None
        if not isinstance(body, dict):
            raise GameError("That request isn't a JSON object")
        return body

    def do_GET(self):
        CTX.guest = self.guest
        why = self._refused()
        if why:
            return self._send(403, {"error": why})
        path, _, qs = self.path.partition("?")
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "static", "index.html"), "rb") as f:
                return self._send(200, f.read(), TYPES[".html"])
        if path.startswith("/static/") and path.endswith((".js", ".jpg")):
            parts = path[len("/static/"):].split("/")  # at most one subfolder (backdrops/); no escaping with ..
            fp = os.path.join(HERE, "static", *parts)
            if len(parts) <= 2 and all(x and x not in (".", "..") for x in parts) and os.path.isfile(fp):
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), TYPES[os.path.splitext(fp)[1]])
        if path.startswith("/media/"):
            fn = os.path.basename(path)
            fp = os.path.join(MEDIA, fn)
            if not os.path.isfile(fp):
                return self._send(404, {"error": "not found"})
            with open(fp, "rb") as f:
                return self._send(200, f.read(), TYPES.get(os.path.splitext(fn)[1], "application/octet-stream"))
        if path == "/api/rules":
            return self._send(200, rules.public_rules())
        if path == "/api/health":
            return self._send(200, spark_health())
        if path == "/api/state":
            q = dict(kv.split("=", 1) for kv in qs.split("&") if "=" in kv)
            try:
                return self._send(200, view(urllib.parse.unquote(q.get("player", ""))))
            except GameError as e:
                return self._send(400, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        CTX.guest = self.guest
        why = self._refused()
        if why:
            return self._send(403, {"error": why})
        # Only JSON: a page on another site can't send this content type without the browser asking first (a preflight), and this server doesn't answer those
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._send(415, {"error": "Send JSON"})
        fn = ACTIONS.get(self.path[len("/api/"):]) if self.path.startswith("/api/") else None
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            body = self._json_body()
            t0 = time.time()
            out = fn(body)
            out["seconds"] = round(time.time() - t0, 1)
            self._send(200, out)
        except RequestTooLarge:
            self._send(413, {"error": "Request too large"})
        except GameError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            if unreachable(e):
                threading.Thread(target=ensure_tunnels, daemon=True).start()
                return self._send(503, {"error": "Can't reach the AI server (images or language model). It is reconnecting on its own; "
                                                 "try again in 30 seconds. No Tokens were spent"})
            self._send(500, {"error": "Server error" if self.guest else f"Server error: {e}"[:500]})

    def log_message(self, fmt, *args):
        if "/api/state" not in (str(args[0]) if args else ""):  # on a bad request args[0] is an HTTPStatus
            sys.stderr.write("%s %s%s\n" % (time.strftime("%H:%M:%S"), "[guest] " if self.guest else "", fmt % args))


class GuestHandler(Handler):
    guest = True


def check_upstream():
    ensure_tunnels()
    for name, url in (("ComfyUI", comfy_client.BASE + "/system_stats"), ("Ollama", ai.OLLAMA + "/api/version")):
        try:
            urllib.request.urlopen(url, timeout=5).read()
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ {name} unreachable ({url}): {e}. Starting anyway; tunnels retry every 30 seconds. If the service isn't running on the GPU machine, start it as the README says",
                  flush=True)


def download(url, dest):
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def fetch_demo_pack():
    if os.path.exists(STATE_FILE):
        return
    os.makedirs(DATA, exist_ok=True)
    zip_path = os.path.join(DATA, "demo-pack.zip")
    print(f"Downloading the demo monsters (about 80 MB) from {DEMO_PACK_URL}", flush=True)
    try:
        download(DEMO_PACK_URL, zip_path)
        with open(zip_path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        if DEMO_PACK_URL == DEMO_PACK_DEFAULT and digest != DEMO_PACK_SHA256:
            raise ValueError("the download is damaged or isn't the released pack")
        with zipfile.ZipFile(zip_path) as z:  # state.json last: its presence means the unpack finished, and an interrupted one is redone next time
            z.extractall(DATA, [n for n in z.namelist() if n != "state.json"])
            z.extract("state.json", DATA)
    except (OSError, ValueError, KeyError, http.client.HTTPException, zipfile.BadZipFile) as e:
        sys.exit(f"Couldn't set up the demo monsters from {DEMO_PACK_URL}: {e}. Try again")
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)


def serve(host):
    srv = ThreadingHTTPServer((host, PORT), Handler)
    print(f"Game server running: http://{host}:{PORT}" + ("  (demo mode, no AI models)" if DEMO else f"  (ComfyUI {comfy_client.BASE}, Ollama {ai.OLLAMA}, model {ai.MODEL})"), flush=True)
    srv.serve_forever()


def start_guest_entrance():
    if GUEST_PORT:
        threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", GUEST_PORT), GuestHandler).serve_forever, daemon=True).start()
        print(f"Guest entrance: 127.0.0.1:{GUEST_PORT} (this machine only, use over an SSH forward; hatch / grow limit {GUEST_GPU_LIMIT})", flush=True)


def requeue_unfinished():
    """3D jobs that didn't finish last time, including failures: a GPU machine that wasn't up yet shouldn't leave a monster with no model for good."""
    for m in STATE["monsters"].values():
        v = m["versions"][-1]
        if v.get("glb_status") in ("queued", "running", "error"):
            v["glb_status"] = "queued"  # the page shows this while it waits, and only keeps checking for "queued" and "running"
            v.pop("glb_error", None)
            JOBS.put((m["id"], v["v"]))
        elif v.get("glb") and v.get("rig_status") != "done":  # rig what's missing: old monsters, unfinished jobs, last run's failures (a restart retries them)
            v.pop("rig_error", None)
            queue_rig(m, v)


def main():
    global STATE
    if DEMO:
        fetch_demo_pack()
        STATE = load()
        seed_wild()
        start_guest_entrance()
        return serve(HOST)
    os.makedirs(MEDIA, exist_ok=True)
    check_upstream()
    STATE = load()
    seed_wild()
    requeue_unfinished()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=rig_worker, daemon=True).start()
    touch_gpu()  # ComfyUI may still hold models from last time at startup, so run the idle timer from here too
    threading.Thread(target=idle_free, daemon=True).start()
    threading.Thread(target=tunnel_watch, daemon=True).start()
    start_guest_entrance()
    serve(HOST)


if __name__ == "__main__":
    main()
