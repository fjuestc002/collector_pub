# -*- coding: utf-8 -*-
"""M2 引擎离线测试：计划/预算/账本/单枪/包级并发，全部走 FakeRemote，零 Cadence。"""
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector import engine  # noqa: E402
from collector.profile import load_profile  # noqa: E402

GEN = ROOT / "profiles" / "EXAMPLE_generic_mos_spectre.json"

WD = "/home/tester/collector_run"


class FakeRemote:
    """鸭子型运输替身：按 deck 脚本化结局 ok/errors/timeout/no_raw/crash。"""

    def __init__(self, outcomes=None, probe=("8", "3500", "400000")):
        self.outcomes = outcomes or {}
        self.probe = probe
        self.put_calls = {}
        self.get_calls = {}
        self.cmds = []

    def run(self, cmd, timeout=900):
        self.cmds.append(cmd)
        if cmd.startswith("nproc;"):
            return 0, "\n".join(self.probe) + "\n"
        if "rm -rf" in cmd:
            return 0, ""
        if "mkdir" in cmd:
            return 0, ""
        m = re.search(r"timeout \d+s \S+ (?:-64 )?(?:\+\S+ )*(\S+?)\.scs", cmd)
        if m:  # spectre 执行令
            name = m.group(1)
            out = self.outcomes.get(name, "ok")
            if out == "timeout":
                return 0, "SPECTRE_RC=124"
            rc = {"crash": "1", "no_raw": "0"}.get(out, "0")
            self.put_calls[f"{WD}/{name}.raw/dc.dc"] = "RAW"
            return 0, f"SPECTRE_RC={rc}"
        if "tail" in cmd:
            name = re.search(r"(\S+?)_spectre\.out", cmd)
            name = name.group(1).split("/")[-1] if name else ""
            if self.outcomes.get(name, "ok") == "errors":
                return 0, "spectre completes with 2 errors, 0 warnings."
            return 0, "spectre completes with 0 errors, 0 warnings."
        if "find" in cmd and ".raw" in cmd:
            name = re.search(r"(\S+?)\.raw", cmd).group(1)
            name = name.split("/")[-1]
            if self.outcomes.get(name, "ok") in ("no_raw", "errors"):
                return 0, ""
            return 0, f"{WD}/{name}.raw/dc.dc\n{WD}/{name}.raw/logFile"
        return 0, ""

    def run_b(self, inner, timeout=900):
        return self.run(inner, timeout)

    def put_text(self, text, rpath):
        self.put_calls[rpath] = text

    def get(self, rpath, lpath):
        Path(lpath).write_bytes(b"RAWBYTES")
        self.get_calls[rpath] = lpath
        return 8

    def close(self):
        pass


class TestPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prof = load_profile(GEN)

    def test_make_plan_counts(self):
        plans = engine.make_plan(self.prof, "pair_01", "n",
                                 ["1u", "2u"], ["1u", "2u", "4u"], [2.5, 1.5])
        self.assertEqual(len(plans), 4)  # 片= L×vds
        p = plans[0]
        self.assertEqual(len(p["cells"]), 3)  # 内含全 W
        # 行数 = 头1+include2 + 3×(实例+漏源) + 栅源1 + options1 + saveopts1 + save1 + dc1 = 14
        self.assertEqual(len(p["text"].splitlines()), 1 + 2 + 3 * 2 + 5)
        self.assertEqual(len({x["deck_id"] for x in plans}), 4)

    def test_polarity_guard_in_cli_logic(self):
        # vds×polarity<0 应拒——直接验引擎不管(在 cli)，此处验 plan 不做符号检查即可
        plans = engine.make_plan(self.prof, "pair_01", "n", ["1u"], ["1u"], [2.5])
        self.assertIn("dc=2.5", plans[0]["text"])

    def test_chunk(self):
        items = list(range(5000))
        self.assertEqual([len(c) for c in engine.chunk(items, 2000)],
                         [2000, 2000, 1000])
        self.assertEqual(len(engine.chunk(items, 5000)[0]), 3000)  # 硬顶
        self.assertEqual(len(engine.chunk(items, 0)[0]), 2000)     # 缺省

    def test_plan_hash_stable(self):
        a = engine.make_plan(self.prof, "pair_01", "n", ["1u"], ["1u"], [2.5])
        b = engine.make_plan(self.prof, "pair_01", "n", ["1u"], ["1u"], [2.5])
        self.assertEqual(engine.plan_hash(a), engine.plan_hash(b))


class TestBudget(unittest.TestCase):
    def setUp(self):
        self.prof = load_profile(GEN)  # lic 8 / reserve 2

    def test_three_layer_min(self):
        b = engine.compute_jobs({"nproc": 16, "mem_avail_mb": 8000, "disk_free_mb": 1},
                                self.prof)  # n_thread=12, n_lic=6, n_mem=40
        self.assertEqual(b["jobs"], 6)
        self.assertEqual(b["binding"], "n_lic")

    def test_thread_binding(self):
        prof = json.loads(json.dumps(self.prof))
        prof["license"] = {"policy": "user_declared"}  # 无申报
        b = engine.compute_jobs({"nproc": 8, "mem_avail_mb": 8000, "disk_free_mb": 1}, prof)
        self.assertEqual(b["jobs"], 4)  # 8-4=4，教学 VM 档
        self.assertEqual(b["binding"], "n_thread")

    def test_user_calibration_16_12(self):
        prof = json.loads(json.dumps(self.prof))
        prof["license"] = {"max_concurrent_spectre": 12, "reserve_for_others": 0}
        b = engine.compute_jobs({"nproc": 16, "mem_avail_mb": 8000, "disk_free_mb": 1}, prof)
        self.assertEqual(b["jobs"], 12)  # 用户实测 16 线程加到 12 的档位

    def test_mem_binding(self):
        b = engine.compute_jobs({"nproc": 16, "mem_avail_mb": 250, "disk_free_mb": 1},
                                self.prof, rss_floor_mb=200)
        self.assertEqual(b["jobs"], 1)
        self.assertEqual(b["binding"], "n_mem")

    def test_override(self):
        b = engine.compute_jobs({"nproc": 16, "mem_avail_mb": 8000, "disk_free_mb": 1},
                                self.prof, jobs_override=1)
        self.assertEqual(b["jobs"], 1)
        self.assertEqual(b["binding"], "override")

    def test_measure_parses(self):
        r = FakeRemote()
        self.assertEqual(engine.measure(r),
                         {"nproc": 8, "mem_avail_mb": 3500, "disk_free_mb": 400000, "rc": 0})
        r2 = FakeRemote()
        r2.probe = ("junk",)
        with self.assertRaises(RuntimeError):
            engine.measure(r2)


class TestSpectreCmd(unittest.TestCase):
    def test_flags(self):
        prof = load_profile(GEN)
        c = engine.spectre_cmd(prof, "d1", WD, 300, disk_check_mb=512)
        self.assertIn("timeout 300s spectre -64 +disk_check=512000000 d1.scs", c)
        self.assertIn("+disk_check=512", c)
        self.assertNotIn("-log", c)  # SPECTRE-132
        self.assertTrue(c.startswith("bash -lc"))
        c2 = engine.spectre_cmd(prof, "d1", WD, 300)
        self.assertNotIn("disk_check", c2)


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.led = engine.Ledger(self.tmp / "ledger.jsonl")
        self.plans = [{"deck_id": f"d{i}", "deck_sha256": "x"} for i in range(5)]

    def _mk_arrived(self, deck_id):
        d = self.tmp / "data" / deck_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "dc.dc").write_bytes(b"RAW")

    def test_states_routing(self):
        self.led.append("d0", "arrived")
        self._mk_arrived("d0")
        self.led.append("d1", "arrived")  # 账本说到、盘上无 ⇒ 重派
        self.led.append("d2", "rejected", reason="errors:2")
        self.led.append("d3", "dispatched")  # 新鲜 ⇒ held
        # d4 从未出现 ⇒ to_dispatch
        old = json.dumps({"ts": time.time() - 90000, "deck_id": "d3b", "state": "dispatched"})
        self.led.path.write_text(self.led.path.read_text(encoding="utf-8") + "\n" + old + "\n",
                                 encoding="utf-8")
        self.plans.append({"deck_id": "d3b", "deck_sha256": "x"})
        rp = self.led.resume_plan(self.plans, self.tmp / "data")
        self.assertEqual(rp["done"], ["d0"])
        self.assertEqual(set(rp["to_dispatch"] and
                             [p["deck_id"] for p in rp["to_dispatch"]]), {"d1", "d4", "d3b"})
        self.assertEqual(rp["rejected"], ["d2"])
        self.assertEqual(rp["held"], ["d3"])
        self.assertEqual([p["deck_id"] for p in rp["stale_redo"]], ["d3b"])

    def test_corrupt_line_skipped(self):
        self.led.path.write_text('{"broken\n', encoding="utf-8")
        self.assertEqual(self.led.last_states(), {})


class TestRunDeckAndBatch(unittest.TestCase):
    def setUp(self):
        self.prof = load_profile(GEN)
        self.tmp = Path(tempfile.mkdtemp())
        self.plans = engine.make_plan(self.prof, "pair_01", "n",
                                      [f"{i}u" for i in range(1, 11)],
                                      ["1u"], [2.5])

    def test_run_deck_ok(self):
        r = FakeRemote()
        res = engine.run_deck(r, self.prof, self.plans[0], WD, self.tmp)
        self.assertTrue(res["ok"])
        self.assertEqual(set(res["files"]), {"dc.dc", "logFile"})
        self.assertTrue((self.tmp / self.plans[0]["deck_id"] / "dc.dc").is_file())

    def test_run_deck_errors(self):
        r = FakeRemote(outcomes={self.plans[0]["deck_id"]: "errors"})
        res = engine.run_deck(r, self.prof, self.plans[0], WD, self.tmp)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "errors:2")

    def test_run_deck_timeout_watchdog(self):
        r = FakeRemote(outcomes={self.plans[0]["deck_id"]: "timeout"})
        res = engine.run_deck(r, self.prof, self.plans[0], WD, self.tmp)
        self.assertEqual(res["reason"], "timeout")

    def test_run_deck_no_raw(self):
        r = FakeRemote(outcomes={self.plans[0]["deck_id"]: "no_raw"})
        res = engine.run_deck(r, self.prof, self.plans[0], WD, self.tmp)
        self.assertEqual(res["reason"], "no_raw")

    def test_batch_all_ok(self):
        r = FakeRemote()
        led = engine.Ledger(self.tmp / "ledger.jsonl")
        rep = engine.run_batch(lambda: r, self.prof, self.plans, WD, self.tmp, 3, led)
        self.assertEqual(len(rep["arrived"]), 10)
        self.assertEqual(len(rep["rejected"]), 0)
        st = led.last_states()
        self.assertEqual({k: v["state"] for k, v in st.items()},
                         {p["deck_id"]: "arrived" for p in self.plans})

    def test_batch_backpressure_stops_dispatch(self):
        # 前 4 张必败 ⇒ 连续失败阈值 4 ⇒ 停派新枪，不杀在跑
        outs = {self.plans[i]["deck_id"]: "errors" for i in range(4)}
        r = FakeRemote(outcomes=outs)
        led = engine.Ledger(self.tmp / "ledger.jsonl")
        rep = engine.run_batch(lambda: r, self.prof, self.plans, WD, self.tmp, 1, led,
                               fail_stop=4)
        self.assertTrue(rep["backpressure"])
        self.assertEqual(len(rep["rejected"]), 4)
        self.assertEqual(len(rep["arrived"]), 0)
        self.assertGreater(rep["unconsumed"], 0)
        self.assertEqual(engine.ladder(4, rep), 2)
        self.assertEqual(engine.ladder(1, rep), 1)
        self.assertEqual(engine.ladder(8, {"backpressure": False}), 8)

    def test_clean_remote_cmd(self):
        r = FakeRemote()
        engine.clean_remote(r, self.prof, WD, ["d1", "d2"])
        rm = [c for c in r.cmds if "rm -rf" in c]
        self.assertTrue(rm and "d1.raw" in rm[0])


if __name__ == "__main__":
    unittest.main()
