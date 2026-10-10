# -*- coding: utf-8 -*-
"""M5 闭环调度器离线测试：假发射落片、幂等重放、拆锚一层、子跨再提案、预算截断、驳回留痕、孤案、qc 同行。

布景复刻实弹：粗格 1.5/2.5 到账 + 外来中点 2.0 到账（fire6c 同款），队列挂 L1u/L2u
两提案（span 1.5→2.5）。假 fire_fn 把加密锚落成合成片（test_interp_check.write_exp），
复验用"升格锚"语义把外来 2.0 升格为粗格、以本轮新锚作探针——拆层→真仿→再检验
的闭环语义在此原样复现，不碰 ssh。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from collector import engine, loop  # noqa: E402
from collector.profile import load_profile  # noqa: E402
from test_interp_check import write_exp  # noqa: E402

GEN = ROOT / "profiles" / "EXAMPLE_generic_mos_spectre.json"


class FakeFire:
    """发射替身：把批落成真片（可埋 gm 雷），返回同 fire 契约的 arrived/rejected/rc。"""

    def __init__(self, scale=None, fail=()):
        self.calls = []
        self.scale = scale or {}
        self.fail = set(fail)

    def __call__(self, prof, plans, data_dir, fire_kw, emit):
        ids = [p["deck_id"] for p in plans]
        self.calls.append(ids)
        ok = [p for p in plans if p["deck_id"] not in self.fail]
        write_exp(Path(data_dir), ok, scale_by_vds=self.scale)
        arrived = [i for i in ids if i not in self.fail]
        return {"rc": 0, "arrived": arrived,
                "rejected": [[i, "fake"] for i in ids if i in self.fail]}


class Base(unittest.TestCase):
    def setUp(self):
        self.prof = load_profile(GEN)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "data"
        self.pdir = self.root / "pair_01"
        self.q = self.tmp / "out" / "queue" / "densify.jsonl"
        self.ev = self.tmp / "out" / "queue" / "events.jsonl"
        self.dp = self.tmp / "out" / "queue" / "densify_plans.jsonl"
        self.rdir = self.tmp / "out" / "drain"
        self.coarse = engine.make_plan(self.prof, "pair_01", "n", ["1u", "2u"],
                                       ["1u", "2u"], [1.5, 2.5], vstep=0.1)
        write_exp(self.pdir, self.coarse)
        mid = engine.make_plan(self.prof, "pair_01", "n", ["1u", "2u"],
                               ["1u", "2u"], [2.0], vstep=0.1)
        write_exp(self.pdir, mid)
        loop.append_rows(self.q, [
            {"ev": "densify", "ts": 100, "reason": "interp_gm", "pair": "pair_01",
             "fet": "n", "L": L, "vds": 2.0, "span": [1.5, 2.5], "gate": "gm",
             "instances": ["M1"],
             "decks": [f"pair_01.n.L{L}.V1.5", f"pair_01.n.L{L}.V2.5",
                       f"pair_01.n.L{L}.V2"]}
            for L in ("1u", "2u")])
        self.k1 = loop.key_str("pair_01", "n", "1u", 1.5, 2.5)
        self.k2 = loop.key_str("pair_01", "n", "2u", 1.5, 2.5)

    def drain(self, fake, adopt=None, fire_flag=False, **kw):
        opt = dict(prof=self.prof, coarse_plans=self.coarse, data_root=self.root,
                   queue_path=self.q, events_path=self.ev,
                   dense_plans_path=self.dp, vstep=0.1, charge=False,
                   adopt=adopt, fire_flag=fire_flag, fire_fn=fake,
                   no_qc=True, no_convert=True, min_gap=0.05, max_anchors=4,
                   max_slots=64, report_dir=self.rdir, tag="t",
                   out=lambda s: None)
        opt.update(kw)
        return loop.run_drain(**opt)


class TestPure(Base):
    def test_key_and_loaders(self):
        self.assertEqual(self.k1, "pair_01|n|1u|1.5|2.5")
        self.assertEqual(set(loop.load_proposals(self.q)), {self.k1, self.k2})
        self.assertEqual(loop.load_last_events(self.ev), {})

    def test_next_anchors_single_layer_per_round(self):
        pres = lambda v: False  # noqa: E731
        self.assertEqual(loop.next_anchors({1.5, 2.5}, 1.5, 2.5, 0.05, 4, pres),
                         [2.0])                                  # 一轮只劈一层
        self.assertEqual(loop.next_anchors({1.5, 2.0, 2.5}, 1.5, 2.5, 0.05, 4, pres),
                         [1.75, 2.25])                          # 外来 2.0 已在锚集
        self.assertEqual(loop.next_anchors({1.5, 1.6, 2.5}, 1.5, 2.5, 0.05, 1, pres),
                         [2.05])                                # cap=1 从最大跨先劈

    def test_next_anchors_gap_guard_and_skip(self):
        self.assertEqual(loop.next_anchors({1.5, 1.55, 1.6}, 1.5, 1.6,
                                           0.05, 4, lambda v: False), [])
        self.assertEqual(loop.next_anchors({1.5, 2.5}, 1.5, 2.5,
                                           0.05, 4, lambda v: True), [])

    def test_polarity_negative_span(self):
        out = loop.next_anchors({-2.5, -1.5}, -2.5, -1.5, 0.05, 4, lambda v: False)
        self.assertEqual(out, [-2.0])

    def test_anchor_rows_promote_foreign(self):
        rows = loop._anchor_rows(loop._family_views(self.coarse), self.root,
                                 [], self.coarse)
        self.assertEqual({r["deck_id"] for r in rows},
                         {"pair_01.n.L1u.V2", "pair_01.n.L2u.V2"})
        self.assertEqual(rows[0]["vds"], 2.0)

    def test_reopen_on_fresher_proposal(self):
        loop.append_rows(self.ev, [{"ev": "status", "ts": 500, "key": self.k1,
                                    "status": "resolved"}])
        props = loop.load_proposals(self.q)
        events = loop.load_last_events(self.ev)
        self.assertNotIn(self.k1, loop.open_keys(props, events))
        fresh = props[self.k1] | {"ts": 700}
        loop.append_rows(self.q, [fresh])
        self.assertIn(self.k1, loop.open_keys(loop.load_proposals(self.q), events))


class TestDrain(Base):
    def test_dry_lists_without_writing(self):
        fake = FakeFire()
        rep = self.drain(fake)
        self.assertEqual(fake.calls, [])
        self.assertFalse(Path(self.ev).exists())
        self.assertFalse(Path(self.dp).exists())
        rep2 = self.drain(fake, adopt="all", fire_flag=False)
        self.assertEqual(sorted(rep2["planned_decks"]),
                         ["pair_01.n.L1u.V1.75", "pair_01.n.L1u.V2.25",
                          "pair_01.n.L2u.V1.75", "pair_01.n.L2u.V2.25"])
        self.assertEqual(fake.calls, [])            # dry 不发射
        self.assertFalse(Path(self.dp).exists())    # dry 不落账
        self.assertEqual(rep2["statuses"][self.k1]["status"], "open")

    def test_fire_resolves_clean(self):
        fake = FakeFire()
        rep = self.drain(fake, adopt="all", fire_flag=True)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(sorted(fake.calls[0]),
                         ["pair_01.n.L1u.V1.75", "pair_01.n.L1u.V2.25",
                          "pair_01.n.L2u.V1.75", "pair_01.n.L2u.V2.25"])
        evs = loop.load_last_events(self.ev)
        self.assertEqual(evs[self.k1]["status"], "resolved")
        self.assertFalse(evs[self.k1].get("requeued"))
        self.assertEqual(rep["interp"]["verdict"], "PASS")
        self.assertEqual(rep["interp"]["pairs_checked"], 4)   # 升格外来 2.0 后两跨对两 L
        self.assertEqual(rep["interp"]["anchor_upgraded"], 2)
        self.assertEqual(rep["new_queue_rows"], 0)
        self.assertEqual(len(loop.load_rows(self.dp)), 4)
        self.assertTrue((self.rdir / "t.drain.json").is_file())
        self.assertTrue((self.rdir / "t.interp.json").is_file())

    def test_idempotent_rerun(self):
        fake = FakeFire()
        self.drain(fake, adopt="all", fire_flag=True)
        rep = self.drain(fake, adopt="all", fire_flag=True)
        self.assertEqual(len(fake.calls), 1)                 # 不重火
        self.assertEqual(rep["planned_decks"], [])
        self.assertEqual(len(loop.load_rows(self.dp)), 4)    # 不重记
        self.assertEqual(loop.load_last_events(self.ev)[self.k1]["status"], "resolved")

    def test_dirty_quarter_requeues(self):
        fake = FakeFire(scale={1.75: {"M1": (1.08, 1.0)}})
        rep = self.drain(fake, adopt="all", fire_flag=True)
        self.assertEqual(rep["new_queue_rows"], 2)           # 两 L 的 1.5→2.0 跨超差
        self.assertIn(loop.key_str("pair_01", "n", "1u", 1.5, 2.0),
                      loop.load_proposals(self.q))
        self.assertEqual(rep["statuses"][self.k1]["status"], "resolved")
        self.assertTrue(loop.load_last_events(self.ev)[self.k1]["requeued"])
        self.assertEqual(rep["statuses"][self.k1]["note"], "子跨已再提案")

    def test_fire_incomplete_then_debt_first_fix(self):
        miss = "pair_01.n.L1u.V2.25"
        fake = FakeFire(fail={miss})
        self.drain(fake, adopt="all", fire_flag=True)
        self.assertEqual(loop.load_last_events(self.ev)[self.k1]["status"],
                         "fire_incomplete")
        fake2 = FakeFire()
        self.drain(fake2, adopt="all", fire_flag=True)
        self.assertEqual(fake2.calls, [[miss]])              # 欠账优先，不拆新层
        self.assertEqual(loop.load_last_events(self.ev)[self.k1]["status"],
                         "resolved")

    def test_slots_cap_defers_next_round(self):
        fake = FakeFire()
        rep = self.drain(fake, adopt="all", fire_flag=True, max_slots=2)
        self.assertTrue(rep["capped"])
        self.assertEqual(len(rep["planned_decks"]), 2)
        self.assertNotIn(self.k2, loop.load_last_events(self.ev))  # k2 零发射无账
        self.assertEqual(rep["statuses"][self.k2]["status"], "open")
        self.drain(fake, adopt="all", fire_flag=True)        # 下轮（预算放开）补上
        self.assertEqual(sorted(fake.calls[1]),
                         ["pair_01.n.L2u.V1.75", "pair_01.n.L2u.V2.25"])

    def test_dismiss_requires_reason_and_closes(self):
        with self.assertRaises(SystemExit):
            self.drain(FakeFire(), dismiss=[self.k2], reason="")
        fake = FakeFire()
        rep = self.drain(fake, adopt="all", fire_flag=True,
                         dismiss=[self.k2], reason="小跨暂压后，随 W 加密批复查")
        self.assertEqual(loop.load_last_events(self.ev)[self.k2]["status"],
                         "dismissed")
        self.assertEqual(sorted(rep["planned_decks"]),
                         ["pair_01.n.L1u.V1.75", "pair_01.n.L1u.V2.25"])

    def test_orphan_loud_no_fire(self):
        loop.append_rows(self.q, [{"ev": "densify", "ts": 999, "reason": "interp_gm",
                                   "pair": "pair_01", "fet": "n", "L": "9u",
                                   "vds": 2.0, "span": [1.5, 2.5], "gate": "gm",
                                   "instances": ["M1"], "decks": []}])
        fake = FakeFire()
        rep = self.drain(fake, adopt="all", fire_flag=True)
        k9 = loop.key_str("pair_01", "n", "9u", 1.5, 2.5)
        self.assertIn(k9, rep["orphan"])
        self.assertFalse(any("L9u" in d for d in rep["planned_decks"]))
        self.assertEqual(len(fake.calls), 1)                 # 其余照常

    def test_qc_ride_and_ledger_rows(self):
        led = self.tmp / "ledgers" / "qc.jsonl"
        fake = FakeFire()
        rep = self.drain(fake, adopt="all", fire_flag=True, no_qc=False,
                         qc_ledger_path=str(led))
        self.assertIsNotNone(rep["qc"])
        self.assertIn(rep["qc"].get("verdict"), ("PASS", "WARN", "FAIL"))
        rows = loop.load_rows(led)
        self.assertTrue(rows and rows[0]["ev"] == "qc_run")


class TestCliWiring(Base):
    def test_drain_dry_empty_queue_rc0(self):
        """cli.py drain 参数接线冒烟：队列缺位=空账，rc=0（不落任何 out 账）。"""
        planf = self.tmp / "coarse.plan.jsonl"
        with planf.open("w", encoding="utf-8") as f:
            for p in self.coarse:
                f.write(json.dumps({k: v for k, v in p.items() if k != "text"},
                                   ensure_ascii=False) + "\n")
        fresh = self.tmp / "x" / "q.jsonl"
        r = subprocess.run(
            [sys.executable, str(ROOT / "cli.py"), "drain",
             "--profile", str(GEN), "--coarse-plan", str(planf),
             "--data", str(self.root), "--queue", str(fresh),
             "--events", str(self.tmp / "x" / "ev.jsonl"),
             "--plans", str(self.tmp / "x" / "dp.jsonl"),
             "--out", str(self.tmp / "x" / "drain"), "--tag", "smoke"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(r.returncode, 0, (r.stdout or "") + (r.stderr or ""))
        self.assertIn("队列空", r.stdout)
        self.assertFalse(fresh.exists())


if __name__ == "__main__":
    unittest.main()
