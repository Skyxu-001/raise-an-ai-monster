"""Game rules: all code. The AI touches no numbers.

Stats, element advantages, move power, flight / dual-element unlocks and Token accounting are all here. The same input always gives the same result
(hit rolls and damage spread in battle use random numbers, but the rules themselves are fixed and public).
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parts import BODY_PLANS, ELEMENTS, PARTS, SIZES, STATS, compute_stats  # noqa: E402

LEVEL_START = 5
LEVEL_CAP = 20
UNLOCK_LEVEL = 10          # flight and dual element come only from reaching this level; they can't be bought
XP_PER_LEVEL = 100
START_TOKENS = 100
COST_HATCH = 30           # free while you have no monster, so nobody gets stuck with no Tokens and nothing to battle with
COST_GROW = 20
COST_SKILL = {"good": 10, "ok": 15, "poor": 25}   # "fit" only changes the price, not the power
REWARD = {True: {"tokens": 30, "xp": 60}, False: {"tokens": 10, "xp": 25}}
MAX_SKILLS = 4
MAX_TURNS = 30
LEDGER_KEEP = 30          # Token ledger rows kept per player

# Names shown on the page
ELEMENT_LABEL = {"fire": "Fire", "water": "Water", "grass": "Grass", "electric": "Electric", "ice": "Ice",
                 "rock": "Rock", "wind": "Wind", "shadow": "Shadow", "normal": "Normal"}
BODY_LABEL = {"biped": "Biped", "quadruped": "Quadruped", "winged": "Winged", "serpent": "Serpent", "floating": "Floating"}
SIZE_LABEL = {"small": "small", "medium": "medium", "large": "large"}
STAT_LABEL = {"hp": "HP", "attack": "Attack", "defense": "Defense", "sp_attack": "Sp. Atk", "speed": "Speed"}
PART_LABEL = {"shell": "Shell", "armor_plates": "Armor plates", "wings": "Wings", "horns": "Horns", "claws": "Claws",
              "fangs": "Fangs", "long_tail": "Long tail", "spikes": "Spikes", "fins": "Fins", "thick_fur": "Thick fur",
              "big_body": "Big body", "small_body": "Small body", "glowing_core": "Glowing core", "crystals": "Crystals",
              "antennae": "Antennae", "tentacles": "Tentacles"}

# Element chart: attacker → the two elements it beats. Each of the 8 elements beats exactly 2 and is beaten by 2
BEATS = {
    "fire": ["grass", "ice"],
    "water": ["fire", "rock"],
    "grass": ["water", "rock"],
    "electric": ["water", "wind"],
    "ice": ["wind", "shadow"],
    "rock": ["fire", "electric"],
    "wind": ["grass", "shadow"],
    "shadow": ["electric", "ice"],
}

# Move effect templates: the AI only sorts a description into one of these; power and accuracy are set here
TEMPLATES = {
    "strike":     {"label": "Strike", "kind": "physical", "power": 60, "acc": 95,
                   "note": "Bite, claw, tackle, tail whip; 30% chance to miss a flying foe"},
    "projectile": {"label": "Projectile", "kind": "special", "power": 55, "acc": 100, "note": "Fire breath, water jet, thrown spikes"},
    "beam":       {"label": "Beam", "kind": "special", "power": 85, "acc": 75, "note": "Hits hard, but only 75% accuracy"},
    "shield":     {"label": "Shield", "kind": "status", "acc": 100, "note": "The next hit you take does 60% less damage"},
    "heal":       {"label": "Heal", "kind": "status", "acc": 100, "note": "Restores 30% HP, at most twice per battle"},
    "buff":       {"label": "Buff", "kind": "status", "acc": 100, "note": "Attack and Sp. Atk ×1.3 for 3 turns"},
}
TACKLE = {"name": "Tackle", "template": "strike", "element": "normal", "fit": "good", "builtin": True, "source": "body"}
TACKLE_POWER = 40


# ---------- Monsters ----------

def unlocked(monster):
    return monster["level"] >= UNLOCK_LEVEL


def flight_body(monster):
    return monster["body_plan"] in ("winged", "floating") or any(p["part"] == "wings" for p in monster["parts"])


def can_fly(monster):
    return flight_body(monster) and unlocked(monster)


def active_elements(monster):
    return monster["elements"][:2] if unlocked(monster) else monster["elements"][:1]


def stats_of(monster):
    return compute_stats(monster, monster["level"])[0]


def describe(monster):
    """Full view for the front end: stats, active elements and locked items are all worked out by code."""
    locks = []
    if len(monster["elements"]) > 1 and not unlocked(monster):
        locks.append(f"Second element ({ELEMENT_LABEL[monster['elements'][1]]}) is inactive: reach Lv.{UNLOCK_LEVEL} to unlock it")
    if flight_body(monster) and not unlocked(monster):
        locks.append(f"Wings / floating are just for looks for now: reach Lv.{UNLOCK_LEVEL} to unlock flight")
    stats, total = compute_stats(monster, monster["level"])
    return {**monster, "stats": stats, "stat_total": total, "active_elements": active_elements(monster),
            "can_fly": can_fly(monster), "locks": locks}


def clean_parts(parts):
    """Keep one of each part (the last one seen) and drop part names not in the table."""
    out = {}
    for p in parts:
        if p.get("part") in PARTS and p.get("size") in SIZES:
            out[p["part"]] = p["size"]
    return [{"part": k, "size": v} for k, v in out.items()]


def clean_elements(elements):
    out = []
    for e in elements:
        if e in ELEMENTS and e not in out:
            out.append(e)
    return out[:2] or ["fire"]


def profile_changes(old, new, level):
    """List of profile changes (compared by code, not left to the AI to say what it changed)."""
    ch = []
    op = {p["part"]: p["size"] for p in old["parts"]}
    np_ = {p["part"]: p["size"] for p in new["parts"]}
    for k, v in np_.items():
        if k not in op:
            ch.append(f"+ {PART_LABEL[k]} ({SIZE_LABEL[v]})")
        elif op[k] != v:
            ch.append(f"~ {PART_LABEL[k]}: {SIZE_LABEL[op[k]]} → {SIZE_LABEL[v]}")
    for k in op:
        if k not in np_:
            ch.append(f"− {PART_LABEL[k]}")
    if old["elements"] != new["elements"]:
        ch.append("Element: " + "/".join(ELEMENT_LABEL[e] for e in old["elements"]) + " → "
                  + "/".join(ELEMENT_LABEL[e] for e in new["elements"]))
    if [c.lower() for c in old["palette"]] != [c.lower() for c in new["palette"]]:
        ch.append("Colors: " + ", ".join(new["palette"]))
    so = compute_stats(old, level)[0]
    sn = compute_stats(new, level)[0]
    deltas = {k: sn[k] - so[k] for k in STATS if sn[k] != so[k]}
    return ch, deltas


def hatch_cost(owned):
    return 0 if owned == 0 else COST_HATCH


def gain(monster, player, won):
    r = REWARD[won]
    player["tokens"] += r["tokens"]
    monster["xp"] += r["xp"]
    levels = 0
    while monster["xp"] >= XP_PER_LEVEL and monster["level"] < LEVEL_CAP:
        monster["xp"] -= XP_PER_LEVEL
        monster["level"] += 1
        levels += 1
    if monster["level"] >= LEVEL_CAP:
        monster["xp"] = 0
    if won:
        monster["wins"] = monster.get("wins", 0) + 1
    else:
        monster["losses"] = monster.get("losses", 0) + 1
    return {"tokens": r["tokens"], "xp": r["xp"], "levels": levels, "level": monster["level"]}


# ---------- Battles (turn-based; the program plays the opponent's moves = async battles) ----------

def type_mult(atk_el, def_els):
    """Advantage ×1.6, disadvantage ×0.6; dual elements multiply, then clamp to 0.5–2 so one move can't end a fight."""
    m = 1.0
    if atk_el == "normal":
        return m
    for d in def_els:
        if d in BEATS[atk_el]:
            m *= 1.6
        elif atk_el in BEATS[d]:
            m *= 0.6
    return min(2.0, max(0.5, m))


def skill_power(skill):
    return TACKLE_POWER if skill.get("builtin") else TEMPLATES[skill["template"]].get("power", 0)


def fighter(monster, stats=None):
    stats = stats or stats_of(monster)
    hp = stats["hp"] * 4
    return {"id": monster["id"], "name": monster["name"], "level": monster["level"], "stats": stats,
            "elements": active_elements(monster), "can_fly": can_fly(monster),
            "skills": [TACKLE] + monster["skills"], "max_hp": hp, "hp": hp,
            "heal_left": 2, "shield_until": -1, "buff_turns": 0}


def _act(battle, side, idx, rng):
    me, foe = battle[side], battle["foe" if side == "me" else "me"]
    skill = me["skills"][idx]
    t = TEMPLATES[skill["template"]]
    ev = {"side": side, "skill": skill["name"], "template": skill["template"], "element": skill["element"],
          "source": skill.get("source")}
    who = me["name"]
    if skill.get("requires_flight") and not me["can_fly"]:
        ev.update(fail=True, text=f"{who} tried {skill['name']}, but can't get off the ground")
        return ev
    if t["kind"] == "status":
        if skill["template"] == "heal":
            if me["heal_left"] <= 0:
                ev.update(fail=True, text=f"{who} tried {skill['name']}, but has already healed twice this battle")
                return ev
            me["heal_left"] -= 1
            amt = min(me["max_hp"] - me["hp"], round(me["max_hp"] * 0.3))
            me["hp"] += amt
            ev.update(heal=amt, text=f"{who} used {skill['name']} and recovered {amt} HP")
        elif skill["template"] == "shield":
            me["shield_until"] = battle["turn"] + 1
            ev["text"] = f"{who} used {skill['name']}: the next hit it takes does 60% less damage"
        else:
            me["buff_turns"] = 3
            ev["text"] = f"{who} used {skill['name']}: Attack and Sp. Atk up for 3 turns"
        return ev
    if rng.random() * 100 >= t["acc"]:
        ev.update(miss=True, text=f"{who}'s {skill['name']} missed")
        return ev
    if skill["template"] == "strike" and foe["can_fly"] and rng.random() < 0.3:
        ev.update(miss=True, text=f"{foe['name']} dodged in mid-air, and {who}'s {skill['name']} hit nothing")
        return ev
    stat = "attack" if t["kind"] == "physical" else "sp_attack"
    a = me["stats"][stat] * (1.3 if me["buff_turns"] > 0 else 1)
    d = max(1, foe["stats"]["defense"])
    mult = type_mult(skill["element"], foe["elements"])
    stab = 1.2 if skill["element"] in me["elements"] else 1.0
    dmg = skill_power(skill) * a / d * (me["level"] + 10) / 22 * mult * stab * rng.uniform(0.85, 1.0)
    shielded = foe["shield_until"] >= battle["turn"]
    if shielded:
        dmg *= 0.4
        foe["shield_until"] = -1
    dmg = max(1, round(dmg))
    foe["hp"] = max(0, foe["hp"] - dmg)
    note = "It's super effective!" if mult > 1 else ("It's not very effective…" if mult < 1 else "")
    ev.update(dmg=dmg, mult=mult, text=f"{who} used {skill['name']} for {dmg} damage{' (the shield blocked most of it)' if shielded else ''}. {note}".strip())
    return ev


def ai_pick(me, foe, rng, turn):
    best, best_score = 0, -1.0
    for i, s in enumerate(me["skills"]):
        tpl = s["template"]
        if tpl == "heal":
            score = 150.0 if me["hp"] < me["max_hp"] * 0.35 and me["heal_left"] > 0 else -1
        elif tpl == "shield":
            score = 35.0 if me["shield_until"] < turn else -1
        elif tpl == "buff":
            score = 45.0 if me["buff_turns"] == 0 else -1
        else:
            stat = "attack" if TEMPLATES[tpl]["kind"] == "physical" else "sp_attack"
            score = skill_power(s) * me["stats"][stat] / 60 * TEMPLATES[tpl]["acc"] / 100 * type_mult(s["element"], foe["elements"])
            score *= 1.2 if s["element"] in me["elements"] else 1
            score *= 0.7 if tpl == "strike" and foe["can_fly"] else 1
            score *= 0 if s.get("requires_flight") and not me["can_fly"] else 1
        score *= rng.uniform(0.7, 1.3)
        if score > best_score:
            best, best_score = i, score
    return best


def play_turn(battle, my_idx, rng=None):
    """Play one turn: each side uses one move and the faster goes first (flyers get speed ×1.1). Returns this turn's list of events."""
    rng = rng or random.Random()
    battle["turn"] += 1
    foe_idx = ai_pick(battle["foe"], battle["me"], rng, battle["turn"])
    sp = {s: battle[s]["stats"]["speed"] * (1.1 if battle[s]["can_fly"] else 1) for s in ("me", "foe")}
    order = ["me", "foe"] if sp["me"] > sp["foe"] or (sp["me"] == sp["foe"] and rng.random() < 0.5) else ["foe", "me"]
    events = []
    for side in order:
        events.append(_act(battle, side, my_idx if side == "me" else foe_idx, rng))
        other = "foe" if side == "me" else "me"
        if battle[other]["hp"] <= 0:
            battle["winner"] = side
            events.append({"side": side, "text": f"{battle[other]['name']} is knocked out!"})
            break
    for s in ("me", "foe"):
        battle[s]["buff_turns"] = max(0, battle[s]["buff_turns"] - 1)
    if not battle.get("winner") and battle["turn"] >= MAX_TURNS:
        ratio = {s: battle[s]["hp"] / battle[s]["max_hp"] for s in ("me", "foe")}
        battle["winner"] = "me" if ratio["me"] >= ratio["foe"] else "foe"
        events.append({"side": battle["winner"], "text": f"{MAX_TURNS} turns are up: the winner is decided by remaining HP %"})
    battle["log"].extend(events)
    return events


def public_rules():
    return {"parts": PARTS, "sizes": SIZES, "elements": ELEMENTS, "body_plans": BODY_PLANS, "beats": BEATS,
            "templates": TEMPLATES, "tackle_power": TACKLE_POWER, "label": {"element": ELEMENT_LABEL, "body": BODY_LABEL,
            "size": SIZE_LABEL, "stat": STAT_LABEL, "part": PART_LABEL},
            "numbers": {"level_start": LEVEL_START, "level_cap": LEVEL_CAP, "unlock_level": UNLOCK_LEVEL,
                        "xp_per_level": XP_PER_LEVEL, "start_tokens": START_TOKENS, "cost_hatch": COST_HATCH, "cost_grow": COST_GROW,
                        "cost_skill": COST_SKILL, "reward_win": REWARD[True], "reward_lose": REWARD[False],
                        "max_skills": MAX_SKILLS}}
