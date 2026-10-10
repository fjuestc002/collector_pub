# -*- coding: utf-8 -*-
"""M5 GUI 冒烟测试（offscreen 不弹窗；假发射注入，绝不上 ssh）。

覆盖：表列账、干跑不落账、选行采纳→销案闭环、驳回必录理由、画像覆写拒写
git 面 profiles/ 且能写本地副本、核心包零 Qt 纪律闸（PySide6 只许住在 gui.py）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

try:
    from PySide6.QtWidgets import QApplication  # noqa: E402
    HAS_QT = True
except ImportError:
    HAS_QT = False

from collector import engine, loop  # noqa: E402
from collector.profile import load_profile  # noqa: E402
from test_interp_check import write_exp  # noqa: E402
from test_loop import FakeFire  # noqa: E402

GEN = ROOT / "profiles" / "EXAMPLE_generic_mos_spectre.json"


@unittest.skipUnless(HAS_QT, "PySide6 未装（发行面可选）")
class GuiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.prof = load_profile(GEN)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "data"
        self.pdir = self.root / "pair_01"
        self.q = self.tmp / "out" / "queue" / "densify.jsonl"
        self.ev = self.tmp / "out" / "queue" / "events.jsonl"
        self.dp = self.tmp / "out" / "queue" / "densify_plans.jsonl"
        self.coarse = engine.make_plan(self.prof, "pair_01", "n", ["1u", "2u"],
                                       ["1u", "2u"], [1.5, 2.5], vstep=0.1)
        write_exp(self.pdir, self.coarse)
        write_exp(self.pdir, engine.make_plan(self.prof, "pair_01", "n",
                                              ["1u", "2u"], ["1u", "2u"],
                                              [2.0], vstep=0.1))
        loop.append_rows(self.q, [
            {"ev": "densify", "ts": 100, "reason": "interp_gm", "pair": "pair_01",
             "fet": "n", "L": L, "vds": 2.0, "span": [1.5, 2.5], "gate": "gm",
             "instances": ["M1"],
             "decks": [f"pair_01.n.L{L}.V1.5", f"pair_01.n.L{L}.V2.5",
                       f"pair_01.n.L{L}.V2"]} for L in ("1u", "2u")])
        self.planf = self.tmp / "coarse.plan.jsonl"
        with self.planf.open("w", encoding="utf-8") as f:
            for p in self.coarse:
                f.write(json.dumps({k: v for k, v in p.items() if k != "text"},
                                   ensure_ascii=False) + "\n")
        self.proff = self.tmp / "prof.json"
        self.proff.write_text(GEN.read_text(encoding="utf-8"), encoding="utf-8")
        self.fake = FakeFire()

    def mkwin(self):
        import gui
        w = gui.MainWindow(defaults={
            "prof": str(self.proff), "coarse": str(self.planf),
            "data": str(self.root), "vstep": 0.1, "charge": False,
            "fire_fn": self.fake, "qc_ledger": str(self.tmp / "qc_ledger.jsonl")})
        w.ed_queue.setText(str(self.q))
        w.ed_events.setText(str(self.ev))
        w.ed_plans.setText(str(self.dp))
        w.refresh()
        return w

    def settle(self, w):
        if w.worker is not None:
            self.assertTrue(w.worker.wait(30000))
        self.app.processEvents()
        self.app.processEvents()


class TestGui(GuiBase):
    def test_core_packages_stay_qt_free(self):
        """纪律闸：PySide6 只许住在 gui.py，核心库/测试外零引用（发行面可选）。"""
        for f in sorted((ROOT / "collector").glob("*.py")):
            t = f.read_text(encoding="utf-8")
            self.assertNotIn("import PySide6", t, f.name)
            self.assertNotIn("from PySide6", t, f.name)

    def test_table_lists_queue(self):
        w = self.mkwin()
        self.assertEqual(w.tbl.rowCount(), 2)
        keys = {w.tbl.item(r, 1).text() for r in range(2)}
        self.assertEqual(keys, {loop.key_str("pair_01", "n", "1u", 1.5, 2.5),
                               loop.key_str("pair_01", "n", "2u", 1.5, 2.5)})
        self.assertIn("在案", {w.tbl.item(r, 0).text() for r in range(2)})

    def test_dry_run_fires_nothing(self):
        w = self.mkwin()
        w.tbl.selectRow(0)
        w.run_drain(dry=True)
        self.settle(w)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(Path(self.ev).exists())
        self.assertFalse(Path(self.dp).exists())

    def test_adopt_selected_fires_and_resolves(self):
        w = self.mkwin()
        w.tbl.selectRow(0)
        w.run_drain(dry=False)
        self.settle(w)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(sorted(self.fake.calls[0]),
                         ["pair_01.n.L1u.V1.75", "pair_01.n.L1u.V2.25"])
        k1 = loop.key_str("pair_01", "n", "1u", 1.5, 2.5)
        self.assertEqual(loop.load_last_events(self.ev)[k1]["status"], "resolved")
        st = {w.tbl.item(r, 1).text(): w.tbl.item(r, 0).text()
              for r in range(w.tbl.rowCount())}
        self.assertEqual(st[k1], "resolved")
        self.assertTrue(Path(self.tmp / "qc_ledger.jsonl").is_file())

    def test_dismiss_needs_reason_and_closes(self):
        w = self.mkwin()
        w._ask_reason = lambda ks: ("", False)          # 人没给理由=取消
        w.tbl.selectRow(1)
        w.dismiss()
        self.settle(w)
        self.assertFalse(Path(self.ev).exists())
        w._ask_reason = lambda ks: ("跨太小，随 W 加密批复查", True)
        w.tbl.selectRow(1)
        w.dismiss()
        self.settle(w)
        k2 = loop.key_str("pair_01", "n", "2u", 1.5, 2.5)
        self.assertEqual(loop.load_last_events(self.ev)[k2]["status"], "dismissed")
        self.assertEqual(self.fake.calls, [])           # 驳回不动枪

    def test_override_writes_local_copy_only(self):
        w = self.mkwin()
        w.spin_gm.setValue(1.5)
        w.spin_ids.setValue(3.0)
        w.write_override()
        j = json.loads(self.proff.read_text(encoding="utf-8"))
        self.assertAlmostEqual(j["qc"]["interp"]["gm_tol"], 0.015, places=9)
        self.assertAlmostEqual(j["qc"]["interp"]["ids_tol"], 0.03, places=9)
        # git 面画像：拒写
        w.ed_prof.setText(str(GEN))
        before = GEN.read_text(encoding="utf-8")
        w.write_override()
        self.assertIn("拒写", w.log.toPlainText())
        self.assertEqual(GEN.read_text(encoding="utf-8"), before)

    def test_threshold_flows_into_drain(self):
        """阈值面板的百分数 → run_drain 的分数口径接线。"""
        w = self.mkwin()
        w.spin_gm.setValue(0.5)
        w.tbl.selectRow(0)
        w.run_drain(dry=False)
        self.settle(w)
        rep = json.loads((self.tmp / "out" / "drain" /
                          sorted((self.tmp / "out" / "drain").glob("gui_*.interp.json"))[0]
                          .name).read_text(encoding="utf-8"))
        self.assertAlmostEqual(rep["gm_tol"], 0.005, places=9)


if __name__ == "__main__":
    unittest.main()
