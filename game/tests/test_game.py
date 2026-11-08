"""Offline tests: the AI and image models are replaced with stubs, so no GPU and no open ports.

    python3 -m unittest discover -s game/tests
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import zipfile
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rules  # noqa: E402
import server  # noqa: E402

QUEUED = []


def stub_hatch(desc, plan):
    time.sleep(0.05)
    return {"name": "Sparky", "elements": ["fire"], "parts": [], "palette": ["red"], "ignored_claims": [], "image_prompt": "x", "llm_tokens": 412}


def stub_skill(monster, desc):
    time.sleep(0.2)
    return {"name": "Bite", "template": "strike", "element": "normal", "fit": "good", "fit_reason": "x", "requires_flight": False,
            "source": "mouth", "ignored_claims": [], "llm_tokens": 180}


def stub_grow(monster, desc):
    time.sleep(0.2)
    return {"elements": monster["elements"], "parts": monster["parts"], "palette": monster["palette"], "edit_instruction": "x",
            "ignored_claims": [], "llm_tokens": 250}


def monster(mid, owner):
    return {"id": mid, "owner": owner, "name": mid.upper(), "level": 5, "xp": 0, "body_plan": "biped", "elements": ["fire"], "parts": [],
            "palette": ["red"], "skills": [], "wins": 0, "losses": 0,
            "versions": [{"v": 1, "kind": "Hatch", "desc": "x", "image": f"{mid}_v1.png", "glb": None, "ts": 0, "ignored_claims": []}]}


def call(fn, body):
    """Returns (result, error), so a test can look at both."""
    try:
        return fn(body), None
    except Exception as e:  # noqa: BLE001
        return None, e


def later(delay, fn, body):
    t = threading.Thread(target=lambda: (time.sleep(delay), call(fn, body)))
    t.start()
    return t


def fight_to_end(name):
    for _ in range(200):
        b = server.BATTLES.get(name)
        if not b or b["winner"]:
            return None
        _, e = call(server.act_battle_move, {"player": name, "idx": 0})
        if e:
            return e
    return None


class GameCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.DATA = cls.tmp.name
        server.MEDIA = os.path.join(cls.tmp.name, "media")
        server.STATE_FILE = os.path.join(cls.tmp.name, "state.json")
        os.makedirs(server.MEDIA)
        server.queue_3d = lambda m: QUEUED.append(m["id"])
        server.gen_image = lambda text, dest: time.sleep(0.05)
        server.edit_image = lambda src, text, dest: time.sleep(0.3)
        server.ai.hatch = stub_hatch
        server.ai.skill = stub_skill
        server.ai.grow = stub_grow

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setup_world(self, **owners):
        """owners: player name -> monster ids. The last id is the active monster."""
        server.BATTLES.clear()
        server.PENDING.clear()
        server.HATCHING.clear()
        server.GROWING.clear()
        QUEUED.clear()
        server.MAX_MONSTERS = 0
        server.DEMO = False
        server.STATE = {"players": {}, "monsters": {"wild1": monster("wild1", None)}, "next_id": 50}
        for name, ids in owners.items():
            server.STATE["players"][name] = {"name": name, "tokens": 100, "monster": ids[-1] if ids else None}
            for i in ids:
                server.STATE["monsters"][i] = monster(i, name)

    def tokens(self, name="Alice"):
        return server.STATE["players"][name]["tokens"]

    def ledger(self, name="Alice"):
        return server.STATE["players"][name].get("ledger", [])


class TokenEconomy(GameCase):
    def test_new_player_gets_a_starting_ledger_row(self):
        self.setup_world()
        p = server.player("Newbie")
        self.assertEqual(p["tokens"], rules.START_TOKENS)
        self.assertEqual([(r["delta"], r["why"]) for r in p["ledger"]], [(rules.START_TOKENS, "Starting Tokens")])

    def test_first_hatch_is_free_and_the_next_one_costs_tokens(self):
        self.setup_world(Alice=[])
        r1, _ = call(server.act_hatch, {"player": "Alice", "body_plan": "biped", "desc": "a small dragon"})
        self.assertEqual((r1["cost"], self.tokens()), (0, 100))
        r2, _ = call(server.act_hatch, {"player": "Alice", "body_plan": "biped", "desc": "another dragon"})
        self.assertEqual((r2["cost"], self.tokens()), (rules.COST_HATCH, 100 - rules.COST_HATCH))

    def test_hatch_receipt_shows_the_real_ai_usage(self):
        self.setup_world(Alice=[])
        r, _ = call(server.act_hatch, {"player": "Alice", "body_plan": "biped", "desc": "a small dragon"})
        self.assertEqual(r["llm_tokens"], 412)
        row = self.ledger()[-1]
        self.assertEqual((row["llm_tokens"], row["why"]), (412, "Hatch Sparky"))
        self.assertIsNotNone(row["image_seconds"])

    def test_hatch_without_enough_tokens_is_refused_before_any_ai_runs(self):
        self.setup_world(Alice=["m1"])
        server.STATE["players"]["Alice"]["tokens"] = rules.COST_HATCH - 1
        calls = []
        orig = server.ai.hatch
        server.ai.hatch = lambda d, p: (calls.append(1), orig(d, p))[1]
        try:
            _, e = call(server.act_hatch, {"player": "Alice", "body_plan": "biped", "desc": "a small dragon"})
        finally:
            server.ai.hatch = orig
        self.assertIsInstance(e, server.GameError)
        self.assertEqual(calls, [])

    def test_failed_hatch_gives_the_tokens_back(self):
        self.setup_world(Alice=["m1"])

        def boom(desc, plan):
            raise RuntimeError("model down")
        orig = server.ai.hatch
        server.ai.hatch = boom
        try:
            call(server.act_hatch, {"player": "Alice", "body_plan": "biped", "desc": "a small dragon"})
        finally:
            server.ai.hatch = orig
        self.assertEqual((self.tokens(), self.ledger()), (100, []))
        self.assertEqual(server.HATCHING, {})

    def test_learning_and_growing_charge_the_price_and_log_the_usage(self):
        self.setup_world(Alice=["m1"])
        r, _ = call(server.act_learn, {"player": "Alice", "desc": "a big bite"})
        self.assertEqual((r["cost"], r["llm_tokens"]), (rules.COST_SKILL["good"], 180))
        call(server.act_grow_preview, {"player": "Alice", "desc": "bigger wings"})
        r, e = call(server.act_grow_confirm, {"player": "Alice"})
        self.assertIsNone(e)
        self.assertEqual((r["cost"], r["llm_tokens"]), (rules.COST_GROW, 250))
        self.assertEqual(self.tokens(), 100 - rules.COST_SKILL["good"] - rules.COST_GROW)
        self.assertEqual([x["delta"] for x in self.ledger()], [-rules.COST_SKILL["good"], -rules.COST_GROW])

    def test_battle_rewards_land_in_the_ledger(self):
        self.setup_world(Alice=["m1"])
        call(server.act_battle_start, {"player": "Alice", "opponent": "wild1"})
        fight_to_end("Alice")
        row = self.ledger()[-1]
        self.assertIn(row["delta"], (rules.REWARD[True]["tokens"], rules.REWARD[False]["tokens"]))
        self.assertEqual(self.tokens(), 100 + row["delta"])

    def test_ledger_view_is_newest_first_and_picks_up_3d_time(self):
        self.setup_world(Alice=[])
        call(server.act_hatch, {"player": "Alice", "body_plan": "biped", "desc": "a small dragon"})
        mid = server.STATE["players"]["Alice"]["monster"]
        server.STATE["monsters"][mid]["versions"][0].update(glb_seconds=151, rig_seconds=58)
        view = server.view("Alice")
        self.assertEqual(view["ledger"][0]["glb_seconds"], 151)
        self.assertEqual(view["ledger"][0]["rig_seconds"], 58)
        self.assertNotIn("ledger", view["player"])

    def test_ledger_is_capped(self):
        self.setup_world(Alice=[])
        p = server.STATE["players"]["Alice"]
        for i in range(rules.LEDGER_KEEP + 5):
            server.ledger(p, 1, f"row {i}")
        self.assertEqual(len(p["ledger"]), rules.LEDGER_KEEP)
        self.assertEqual(p["ledger"][-1]["why"], f"row {rules.LEDGER_KEEP + 4}")

    def test_view_quotes_the_next_hatch_price(self):
        self.setup_world(Alice=[], Bob=["m1"])
        self.assertEqual(server.view("Alice")["hatch_cost"], 0)
        self.assertEqual(server.view("Bob")["hatch_cost"], rules.COST_HATCH)


class AiNeverSetsNumbers(GameCase):
    def test_power_claims_change_nothing(self):
        self.setup_world(Alice=[])

        def cheat(desc, plan):
            return {**stub_hatch(desc, plan), "ignored_claims": ["attack 9999"], "attack": 9999, "stats": {"attack": 9999}}
        orig = server.ai.hatch
        server.ai.hatch = cheat
        try:
            call(server.act_hatch, {"player": "Alice", "body_plan": "biped", "desc": "invincible, attack 9999"})
        finally:
            server.ai.hatch = orig
        m = server.view("Alice")["monster"]
        self.assertEqual(m["versions"][0]["ignored_claims"], ["attack 9999"])
        self.assertLess(m["stats"]["attack"], 200)

    def test_unknown_parts_and_elements_are_dropped(self):
        self.assertEqual(rules.clean_parts([{"part": "laser_cannon", "size": "large"}, {"part": "wings", "size": "small"}]),
                         [{"part": "wings", "size": "small"}])
        self.assertEqual(rules.clean_elements(["plasma", "water"]), ["water"])


class NoPaidShortcuts(GameCase):
    def test_flight_and_second_element_only_come_from_levels(self):
        m = monster("m1", "Alice")
        m["elements"] = ["fire", "rock"]
        m["parts"] = [{"part": "wings", "size": "small"}]
        self.assertEqual((rules.can_fly(m), rules.active_elements(m)), (False, ["fire"]))
        m["level"] = rules.UNLOCK_LEVEL
        self.assertEqual((rules.can_fly(m), rules.active_elements(m)), (True, ["fire", "rock"]))

    def test_there_is_no_way_to_buy_tokens_or_unlocks(self):
        self.assertNotIn("premium", server.ACTIONS)
        self.assertEqual(self.player_keys(), {"name", "tokens", "monster", "ledger"})

    def player_keys(self):
        self.setup_world()
        return set(server.player("Newbie"))


class Concurrency(GameCase):
    def test_concurrent_hatches_respect_the_cap(self):
        self.setup_world(Alice=[])
        server.MAX_MONSTERS = 2
        ts = [threading.Thread(target=call, args=(server.act_hatch, {"player": "Alice", "body_plan": "biped", "desc": f"monster {i}"})) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(server.roster(server.STATE["players"]["Alice"])), 2)
        self.assertEqual(server.HATCHING, {})

    def test_no_limit_by_default(self):
        self.setup_world(Alice=[])
        server.STATE["players"]["Alice"]["tokens"] = 1000
        for i in range(8):
            _, e = call(server.act_hatch, {"player": "Alice", "body_plan": "biped", "desc": f"monster {i}"})
            self.assertIsNone(e)
        self.assertEqual(len(server.roster(server.STATE["players"]["Alice"])), 8)

    def test_hatch_clears_a_stale_pending_growth_but_the_battle_still_rewards_the_old_monster(self):
        self.setup_world(Alice=["m1"])
        server.PENDING["Alice"] = {"mid": "m1", "desc": "x", "new": {}, "edit_instruction": "x", "ignored_claims": []}
        call(server.act_battle_start, {"player": "Alice", "opponent": "wild1"})
        call(server.act_hatch, {"player": "Alice", "body_plan": "biped", "desc": "another one"})
        self.assertNotIn("Alice", server.PENDING)
        fight_to_end("Alice")
        m1 = server.STATE["monsters"]["m1"]
        self.assertEqual(m1["wins"] + m1["losses"], 1)

    def test_release_during_learn_charges_nothing(self):
        self.setup_world(Alice=["m1", "m2"])
        t = later(0.05, server.act_release, {"player": "Alice"})
        call(server.act_learn, {"player": "Alice", "desc": "a big bite"})
        t.join()
        self.assertEqual(self.tokens(), 100)

    def test_switch_during_grow_preview_leaves_nothing_pending(self):
        self.setup_world(Alice=["m1", "m2"])
        t = later(0.05, server.act_select, {"player": "Alice", "id": "m1"})
        call(server.act_grow_preview, {"player": "Alice", "desc": "bigger wings"})
        t.join()
        self.assertNotIn("Alice", server.PENDING)

    def test_release_during_grow_confirm_refunds_and_skips_3d(self):
        self.setup_world(Alice=["m1", "m2"])
        server.PENDING["Alice"] = {"mid": "m2", "desc": "x", "new": {}, "edit_instruction": "x", "ignored_claims": []}
        t = later(0.05, server.act_release, {"player": "Alice"})
        _, e = call(server.act_grow_confirm, {"player": "Alice"})
        t.join()
        self.assertIsInstance(e, server.GameError)
        self.assertEqual(self.tokens(), 100)
        self.assertNotIn("m2", QUEUED)
        self.assertEqual(self.ledger(), [])

    def test_concurrent_learns_respect_the_skill_cap(self):
        self.setup_world(Alice=["m1"])
        server.STATE["monsters"]["m1"]["skills"] = [{"name": "s", "template": "strike", "element": "normal", "fit": "good"}] * (rules.MAX_SKILLS - 1)
        ts = [threading.Thread(target=call, args=(server.act_learn, {"player": "Alice", "desc": "a big bite"})) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(server.STATE["monsters"]["m1"]["skills"]), rules.MAX_SKILLS)
        self.assertEqual(self.tokens(), 100 - rules.COST_SKILL["good"])


class Battles(GameCase):
    def test_own_monster_gone_mid_battle_leaves_no_half_finished_battle(self):
        self.setup_world(Alice=["m1"])
        call(server.act_battle_start, {"player": "Alice", "opponent": "wild1"})
        server.STATE["monsters"].pop("m1")
        fight_to_end("Alice")
        b = server.BATTLES.get("Alice")
        self.assertFalse(b and b["winner"] and not b.get("reward"))

    def test_foe_released_mid_battle_still_has_art(self):
        self.setup_world(Alice=["m1"], Bob=["m3", "m4"])
        call(server.act_battle_start, {"player": "Alice", "opponent": "m4"})
        call(server.act_release, {"player": "Bob"})
        self.assertEqual(server.view("Alice")["battle"]["image"]["foe"], "m4_v1.png")


class Names(GameCase):
    def test_clean_name(self):
        for raw, want in ((123, ""), (["a"], ""), ("​", ""), ("  a​b \n c ", "ab c"), ("Demon Bunny", "Demon Bunny"), ("x" * 30, "x" * 24)):
            self.assertEqual(server.clean_name(raw), want)

    def test_bad_name_type_is_not_a_server_error(self):
        self.setup_world(Alice=[])
        _, e = call(server.act_hatch, {"player": "Alice", "body_plan": "biped", "desc": "a cat", "name": ["x"]})
        self.assertIsNone(e)

    def test_zero_width_only_rename_is_rejected(self):
        self.setup_world(Alice=["m1"])
        _, e = call(server.act_rename, {"player": "Alice", "name": "​​"})
        self.assertIsInstance(e, server.GameError)
        self.assertEqual(server.STATE["monsters"]["m1"]["name"], "M1")


class DemoMode(GameCase):
    def setup_demo(self):
        self.setup_world(Alice=[], Bob=["m3"])
        server.DEMO = True
        sample = monster("m7", None)
        sample["adoptable"] = True
        sample["versions"][0].update(llm_tokens=412, image_seconds=9.1)
        server.STATE["monsters"]["m7"] = sample

    def test_hatching_adopts_a_ready_made_monster_with_its_real_receipt(self):
        self.setup_demo()
        r, e = call(server.act_hatch, {"player": "Alice", "name": "Pal"})
        self.assertIsNone(e)
        self.assertEqual((r["cost"], r["llm_tokens"]), (0, 412))
        m = server.STATE["monsters"]["m7"]
        self.assertEqual((m["owner"], m["name"]), ("Alice", "Pal"))
        self.assertNotIn("adoptable", m)

    def test_samples_are_hidden_from_the_opponent_list_until_adopted(self):
        self.setup_demo()
        self.assertNotIn("m7", [o["id"] for o in server.view("Bob")["opponents"]])
        call(server.act_hatch, {"player": "Alice"})
        self.assertIn("m7", [o["id"] for o in server.view("Bob")["opponents"]])

    def test_running_out_of_samples_is_a_clear_error(self):
        self.setup_demo()
        call(server.act_hatch, {"player": "Alice"})
        _, e = call(server.act_hatch, {"player": "Alice"})
        self.assertIsInstance(e, server.GameError)

    def test_ai_actions_explain_why_they_are_off(self):
        self.setup_demo()
        call(server.act_hatch, {"player": "Alice"})
        for fn in (server.act_learn, server.act_grow_preview):
            _, e = call(fn, {"player": "Alice", "desc": "a big bite"})
            self.assertEqual(str(e), server.DEMO_NO_AI)

    def test_battles_work_and_earn_tokens(self):
        self.setup_demo()
        call(server.act_hatch, {"player": "Alice"})
        call(server.act_battle_start, {"player": "Alice", "opponent": "m3"})
        self.assertIsNone(fight_to_end("Alice"))
        self.assertGreater(self.tokens(), 100)


class HttpGuards(GameCase):
    """These go through the real request handler, on a port the system picks."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.servers = {name: ThreadingHTTPServer(("127.0.0.1", 0), handler) for name, handler in (("main", server.Handler), ("guest", server.GuestHandler))}
        for srv in cls.servers.values():
            threading.Thread(target=srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for srv in cls.servers.values():
            srv.shutdown()
            srv.server_close()
        super().tearDownClass()

    def send(self, method, path, body=None, headers=None, via="main"):
        conn = http.client.HTTPConnection("127.0.0.1", self.servers[via].server_address[1], timeout=10)
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
        conn.request(method, path, body, {"Content-Type": "application/json", **(headers or {})} if method == "POST" else (headers or {}))
        r = conn.getresponse()
        r.read()
        conn.close()
        return r.status

    def here(self, via="main"):
        return f"127.0.0.1:{self.servers[via].server_address[1]}"

    def test_a_broken_character_in_a_name_cannot_stop_the_game_saving(self):
        self.setup_world(Alice=["m1"])
        self.assertEqual(self.send("POST", "/api/grow/cancel", b'{"player": "\\ud800x"}'), 400)
        self.assertEqual(self.send("POST", "/api/grow/cancel", {"player": "Carol"}), 200)
        with open(server.STATE_FILE, encoding="utf-8") as f:
            self.assertIn("Carol", json.load(f)["players"])

    def test_other_sites_cant_post_from_the_players_browser(self):
        self.setup_world(Alice=["m1"])
        self.assertEqual(self.send("POST", "/api/release", b'{"player": "Alice"}', {"Content-Type": "text/plain"}), 415)
        self.assertEqual(self.send("POST", "/api/release", {"player": "Alice"}, {"Origin": "https://evil.example"}), 403)
        self.assertIn("m1", server.STATE["monsters"])
        self.assertEqual(self.send("POST", "/api/release", {"player": "Alice"}, {"Origin": "http://" + self.here()}), 200)
        self.assertNotIn("m1", server.STATE["monsters"])

    def test_a_hostile_name_pointed_at_this_machine_is_refused(self):
        self.setup_world(Alice=["m1"])
        self.assertEqual(self.send("GET", "/api/state?player=Alice", headers={"Host": "rebind.evil.example:8191"}), 403)
        self.assertEqual(self.send("POST", "/api/release", {"player": "Alice"}, {"Host": "rebind.evil.example:8191"}), 403)
        self.assertEqual(self.send("GET", "/api/state?player=Alice", headers={"Host": "localhost:8191"}), 200)
        self.assertEqual(self.send("GET", "/api/state?player=Guest", headers={"Host": "10.0.0.5:8191"}, via="guest"), 200)  # guests arrive over a forward

    def test_a_get_changes_nothing(self):
        # A web page can make the browser send a GET with no Origin header (an <img> is enough), so only the player's first action may create the account
        self.setup_world(Alice=["m1"])
        if os.path.exists(server.STATE_FILE):
            os.remove(server.STATE_FILE)
        self.assertEqual(self.send("GET", "/api/state?player=Ghost"), 200)
        self.assertNotIn("Ghost", server.STATE["players"])
        self.assertFalse(os.path.exists(server.STATE_FILE))
        self.assertEqual(self.send("POST", "/api/grow/cancel", {"player": "Ghost"}), 200)
        self.assertIn("Ghost", server.STATE["players"])

    def test_oversized_requests_are_refused(self):
        self.setup_world(Alice=["m1"])
        self.assertEqual(self.send("POST", "/api/grow/cancel", b'{"player": "Alice", "x": "' + b"a" * 70000 + b'"}'), 413)

    def test_malformed_requests_are_a_400_not_a_server_error(self):
        self.setup_world(Alice=["m1"])
        for body in (b"[]", b"123", b"not json", {"player": "Alice", "idx": "abc"}):
            with self.subTest(body=body):
                self.assertEqual(self.send("POST", "/api/skill/forget", body), 400)
        self.assertEqual(self.send("POST", "/api/battle/move", {"player": "Alice", "idx": "abc"}), 400)


class Guests(GameCase):
    def setUp(self):
        server.GUEST_GPU[0] = 0
        server.GUEST_OPEN.clear()
        server.CTX.guest = True

    def tearDown(self):
        server.CTX.guest = False

    def test_guests_cant_reach_an_account_made_elsewhere_even_by_lookalike_names(self):
        self.setup_world(Alice=["m1"])
        for name in ("Alice", " Alice ", "Alice​", "​Alice"):
            with self.subTest(name=name):
                _, e = call(server.act_release, {"player": name})
                self.assertIsInstance(e, server.GameError)
        self.assertIn("m1", server.STATE["monsters"])

    def test_guests_dont_see_internal_error_text(self):
        self.setup_world()
        p = server.player("Gina")
        m = monster("m9", "Gina")
        m["versions"][0].update(glb_status="error", glb_error="/secret/path", rig_status="error", rig_error="ssh gpu-box failed")
        server.STATE["monsters"]["m9"] = m
        p["monster"] = "m9"
        v = server.view("Gina")["monster"]["versions"][0]
        self.assertEqual(v["glb_status"], "error")
        self.assertNotIn("glb_error", v)
        self.assertNotIn("rig_error", v)
        self.assertEqual(m["versions"][0]["glb_error"], "/secret/path")  # still there for the server's owner
        server.CTX.guest = False
        self.assertEqual(server.view("Gina")["monster"]["versions"][0]["glb_error"], "/secret/path")

    def test_being_short_on_tokens_keeps_the_preview_and_the_guest_limit(self):
        self.setup_world()
        p = server.player("Gina")
        server.STATE["monsters"]["m9"] = monster("m9", "Gina")
        p["monster"] = "m9"
        self.assertIsNone(call(server.act_grow_preview, {"player": "Gina", "desc": "bigger"})[1])
        p["tokens"] = 0
        _, e = call(server.act_grow_confirm, {"player": "Gina"})
        self.assertIn("Not enough Tokens", str(e))
        self.assertIn("Gina", server.PENDING)
        self.assertEqual(server.GUEST_GPU[0], 0)

    def test_guests_cant_read_an_account_made_elsewhere_either(self):
        self.setup_world(Alice=["m1"])
        _, e = call(server.view, "Alice")
        self.assertIsInstance(e, server.GameError)

    def test_a_failed_job_gives_the_guest_limit_back(self):
        self.setup_world()
        p = server.player("Gina")
        server.STATE["monsters"]["m9"] = monster("m9", "Gina")
        p["monster"] = "m9"
        def boom(*a):
            raise RuntimeError("image server down")
        for name in ("edit_image", "gen_image"):
            self.addCleanup(setattr, server, name, getattr(server, name))
        self.assertIsNone(call(server.act_grow_preview, {"player": "Gina", "desc": "bigger"})[1])
        good = server.edit_image
        server.edit_image = server.gen_image = boom
        self.assertIsInstance(call(server.act_grow_confirm, {"player": "Gina"})[1], RuntimeError)
        self.assertEqual(server.GUEST_GPU[0], 0)
        self.assertIsInstance(call(server.act_hatch, {"player": "Gina", "desc": "a small fox", "body_plan": "biped"})[1], RuntimeError)
        self.assertEqual(server.GUEST_GPU[0], 0)
        server.edit_image = good  # and one that works still counts
        self.assertIsNone(call(server.act_grow_confirm, {"player": "Gina"})[1])
        self.assertEqual(server.GUEST_GPU[0], 1)

    def test_guest_limit_also_stops_growth_previews(self):
        self.setup_world()
        p = server.player("Gina")
        server.STATE["monsters"]["m9"] = monster("m9", "Gina")
        p["monster"] = "m9"
        server.GUEST_GPU[0] = server.GUEST_GPU_LIMIT
        asked = []
        server.ai.grow, saved = (lambda m, d: asked.append(d)), server.ai.grow
        self.addCleanup(setattr, server.ai, "grow", saved)
        _, e = call(server.act_grow_preview, {"player": "Gina", "desc": "bigger"})
        self.assertIn("limit", str(e))
        self.assertEqual(asked, [])


class AiCallsNeedTheMoney(GameCase):
    def test_no_ai_runs_for_a_player_who_cant_pay(self):
        self.setup_world(Alice=["m1"])
        server.STATE["players"]["Alice"]["tokens"] = 0
        asked = []
        saved = server.ai.grow, server.ai.skill
        self.addCleanup(lambda: (setattr(server.ai, "grow", saved[0]), setattr(server.ai, "skill", saved[1])))
        server.ai.grow = server.ai.skill = lambda m, d: asked.append(d)
        for fn in (server.act_grow_preview, server.act_learn):
            _, e = call(fn, {"player": "Alice", "desc": "something"})
            self.assertIn("Not enough Tokens", str(e))
        self.assertEqual(asked, [])

    def test_very_long_descriptions_are_refused_before_any_ai_runs(self):
        self.setup_world(Alice=["m1"])
        for fn in (server.act_hatch, server.act_grow_preview, server.act_learn):
            _, e = call(fn, {"player": "Alice", "body_plan": "biped", "desc": "x" * (server.DESC_MAX + 1)})
            self.assertIn("too long", str(e))
        self.assertEqual(self.tokens(), 100)

    def test_a_move_the_game_cant_use_is_refused_before_charging(self):
        self.setup_world(Alice=["m1"])
        saved = server.ai.skill
        self.addCleanup(setattr, server.ai, "skill", saved)
        for bad in ({"template": "laser"}, {"fit": "perfect"}, {"element": "plasma"}):
            with self.subTest(bad=bad):
                server.ai.skill = lambda m, d, bad=bad: {**stub_skill(m, d), **bad}
                _, e = call(server.act_learn, {"player": "Alice", "desc": "a move"})
                self.assertIsInstance(e, server.GameError)
                self.assertEqual(self.tokens(), 100)
                self.assertEqual(server.STATE["monsters"]["m1"]["skills"], [])


class GrowthPictures(GameCase):
    def setUp(self):
        self.addCleanup(setattr, server, "edit_image", server.edit_image)
        self.setup_world(Alice=["m1", "m2"])
        self.assertIsNone(call(server.act_grow_preview, {"player": "Alice", "desc": "bigger"})[1])

    def test_a_failed_picture_can_be_confirmed_again(self):
        # the error tells the player to try again in 30 seconds, so what they confirmed has to still be there
        good = server.edit_image
        def boom(*a):
            raise RuntimeError("image server down")
        server.edit_image = boom
        self.assertIsInstance(call(server.act_grow_confirm, {"player": "Alice"})[1], RuntimeError)
        self.assertEqual(self.tokens(), 100)
        server.edit_image = good
        self.assertIsNone(call(server.act_grow_confirm, {"player": "Alice"})[1])
        self.assertEqual(self.tokens(), 100 - rules.COST_GROW)

    def test_a_failed_picture_doesnt_bring_a_preview_back_after_a_switch(self):
        def boom(*a):
            server.STATE["players"]["Alice"]["monster"] = "m1"
            raise RuntimeError("image server down")
        server.edit_image = boom
        self.assertIsInstance(call(server.act_grow_confirm, {"player": "Alice"})[1], RuntimeError)
        self.assertNotIn("Alice", server.PENDING)

    def test_a_monster_cant_start_a_second_growth_while_one_is_being_drawn(self):
        # both would take the same version number and picture file, and the second would never get a 3D model
        gate = threading.Event()
        server.edit_image = lambda *a: gate.wait(5)
        t = threading.Thread(target=call, args=(server.act_grow_confirm, {"player": "Alice"}))
        self.addCleanup(t.join)  # cleanups run last-added first: the gate opens, then the thread finishes
        self.addCleanup(gate.set)
        t.start()
        while "Alice" in server.PENDING:
            time.sleep(0.01)
        asked = []
        server.ai.grow, saved = (lambda m, d: asked.append(d)), server.ai.grow
        self.addCleanup(setattr, server.ai, "grow", saved)
        self.assertIsInstance(call(server.act_grow_preview, {"player": "Alice", "desc": "bigger still"})[1], server.GameError)
        self.assertEqual(asked, [])  # refused before any AI work
        server.ai.grow = saved
        gate.set()
        t.join()
        self.assertEqual([v["v"] for v in server.STATE["monsters"]["m2"]["versions"]], [1, 2])
        self.assertEqual(self.tokens(), 100 - rules.COST_GROW)
        self.assertIsNone(call(server.act_grow_preview, {"player": "Alice", "desc": "bigger still"})[1])  # free again once it's done


class SharedStateUnderTheLock(GameCase):
    def test_a_new_player_is_saved_under_the_lock(self):
        self.setup_world()
        held = []
        saved = server.save
        self.addCleanup(setattr, server, "save", saved)
        server.save = lambda: held.append(server.LOCK._is_owned())
        server.player("Newcomer")
        self.assertEqual(held, [True])

    def test_two_requests_on_the_last_turn_pay_the_reward_once(self):
        self.setup_world(Alice=["m1"], Bob=["m2"])
        self.assertIsNone(call(server.act_battle_start, {"player": "Alice", "opponent": "m2"})[1])
        server.BATTLES["Alice"]["foe"]["hp"] = 1
        server.BATTLES["Alice"]["me"]["stats"]["speed"] = 999
        real = rules.play_turn
        self.addCleanup(setattr, rules, "play_turn", real)
        rules.play_turn = lambda b, i, *a: (time.sleep(0.2), real(b, i, *a))[1]
        threads = [threading.Thread(target=call, args=(server.act_battle_move, {"player": "Alice", "idx": 0})) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(server.STATE["monsters"]["m1"]["wins"], 1)
        self.assertEqual(self.tokens(), 100 + rules.REWARD[True]["tokens"])


class DemoPack(GameCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name, value in (("DATA", self.dir.name), ("STATE_FILE", os.path.join(self.dir.name, "state.json"))):
            self.addCleanup(setattr, server, name, getattr(server, name))
            setattr(server, name, value)

    def pack(self, damage=False):
        path = os.path.join(self.dir.name, "pack.zip")
        with zipfile.ZipFile(path, "w") as z:  # state.json goes first, the way make_demo_pack.py writes it
            z.writestr("state.json", "{}")
            z.writestr("media/a.png", "PICTURE-BYTES")
        if damage:
            with open(path, "rb") as f:
                data = f.read()
            with open(path, "wb") as f:
                f.write(data.replace(b"PICTURE-BYTES", b"PICTURE-BYTEZ"))
        return path

    def use(self, path, url="file:///x"):
        self.addCleanup(setattr, server, "DEMO_PACK_URL", server.DEMO_PACK_URL)
        self.addCleanup(setattr, server, "download", server.download)
        server.DEMO_PACK_URL = url
        server.download = lambda u, dest: shutil.copyfile(path, dest)

    def test_a_good_pack_unpacks_and_is_only_fetched_once(self):
        self.use(self.pack())
        server.fetch_demo_pack()
        self.assertTrue(os.path.exists(server.STATE_FILE))
        self.assertTrue(os.path.exists(os.path.join(server.DATA, "media", "a.png")))
        self.assertFalse(os.path.exists(os.path.join(server.DATA, "demo-pack.zip")))

    def test_an_unpack_that_dies_midway_is_redone_next_time(self):
        self.use(self.pack(damage=True))
        with self.assertRaises(SystemExit) as ctx:
            server.fetch_demo_pack()
        self.assertIn("Couldn't set up", str(ctx.exception))
        self.assertFalse(os.path.exists(server.STATE_FILE))
        self.assertFalse(os.path.exists(os.path.join(server.DATA, "demo-pack.zip")))

    def test_the_released_pack_is_checked_against_its_hash(self):
        self.use(self.pack(), url=server.DEMO_PACK_DEFAULT)
        with self.assertRaises(SystemExit) as ctx:
            server.fetch_demo_pack()
        self.assertIn("damaged", str(ctx.exception))
        self.assertFalse(os.path.exists(server.STATE_FILE))

    def test_a_failed_download_is_one_clear_message(self):
        self.use(self.pack())
        def fail(url, dest):
            raise urllib.error.URLError("no route")
        server.download = fail
        with self.assertRaises(SystemExit) as ctx:
            server.fetch_demo_pack()
        self.assertIn("Couldn't set up", str(ctx.exception))

    def test_a_download_that_breaks_off_leaves_nothing_behind(self):
        self.use(self.pack())
        def fail(url, dest):
            with open(dest, "wb") as f:
                f.write(b"half")
            raise http.client.IncompleteRead(b"half")
        server.download = fail
        with self.assertRaises(SystemExit) as ctx:
            server.fetch_demo_pack()
        self.assertIn("Couldn't set up", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(server.DATA, "demo-pack.zip")))

    def test_a_bad_address_is_one_clear_message(self):
        self.addCleanup(setattr, server, "DEMO_PACK_URL", server.DEMO_PACK_URL)
        server.DEMO_PACK_URL = "not a web address"
        with self.assertRaises(SystemExit) as ctx:
            server.fetch_demo_pack()
        self.assertIn("Couldn't set up", str(ctx.exception))

    def test_a_pack_with_no_save_file_is_one_clear_message(self):
        path = os.path.join(self.dir.name, "pack.zip")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("media/a.png", "x")
        self.use(path)
        with self.assertRaises(SystemExit) as ctx:
            server.fetch_demo_pack()
        self.assertIn("Couldn't set up", str(ctx.exception))


class Startup(GameCase):
    def test_failed_3d_jobs_get_another_try_at_the_next_start(self):
        self.setup_world(Alice=["m1", "m2"])
        server.STATE["monsters"]["m1"]["versions"][-1]["glb_status"] = "error"
        server.STATE["monsters"]["m2"]["versions"][-1]["glb_status"] = "done"
        while not server.JOBS.empty():
            server.JOBS.get()
        server.requeue_unfinished()
        self.assertEqual(server.JOBS.get_nowait()[0], "m1")
        self.assertTrue(server.JOBS.empty())

    def test_a_job_waiting_for_its_retry_says_queued_and_drops_the_old_error(self):
        self.setup_world(Alice=["m1", "m2"])
        for name, value in (("RIG", True), ("TUNNEL_HOST", "gpu-box")):
            self.addCleanup(setattr, server, name, getattr(server, name))
            setattr(server, name, value)
        for q in (server.JOBS, server.RIGS):
            while not q.empty():
                q.get()
        failed_3d = server.STATE["monsters"]["m1"]["versions"][-1]
        failed_3d.update(glb_status="error", glb_error="old")
        failed_rig = server.STATE["monsters"]["m2"]["versions"][-1]
        failed_rig.update(glb="m2_v1.glb", glb_status="done", rig_status="error", rig_error="old")
        server.requeue_unfinished()
        self.assertEqual(failed_3d["glb_status"], "queued")
        self.assertNotIn("glb_error", failed_3d)
        self.assertEqual(failed_rig["rig_status"], "queued")
        self.assertNotIn("rig_error", failed_rig)
        self.assertEqual(server.JOBS.get_nowait()[0], "m1")
        self.assertEqual(server.RIGS.get_nowait()[0], "m2")

    def test_an_unknown_argument_stops_before_anything_starts(self):
        with tempfile.TemporaryDirectory() as d, socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            env = {**os.environ, "GAME_DATA": d, "GAME_PORT": str(probe.getsockname()[1]), "GAME_TUNNEL_HOST": ""}
            script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py")
            r = subprocess.run([sys.executable, script, "--help"], env=env, capture_output=True, text=True, timeout=20)
            self.assertIn("Usage", r.stderr + r.stdout)
            self.assertEqual(os.listdir(d), [])


if __name__ == "__main__":
    unittest.main()
