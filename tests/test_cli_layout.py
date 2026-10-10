# -*- coding: utf-8 -*-
"""CLI 级路径布局回归：实弹布局 data/<pair>/<deck_id>，命令行须接 pair 层。

10-09 实弹教训：interp_check 库函数收"片平铺目录"（与 qc/engine 同构），
cmd_interp_check 却把 --data 原样传入、漏了 /pair——离线单测全打库函数照不
住这一段，六枪实弹到账反而暴露。此测用 subprocess 走真 CLI，锁死该路径。
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from collector import engine  # noqa: E402
from collector.profile import load_profile  # noqa: E402
import test_interp_check as t4c  # noqa: E402


class TestCliInterpLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prof_path = ROOT / "profiles" / "EXAMPLE_generic_mos_spectre.json"
        prof = load_profile(self.prof_path)
        data_pair = self.tmp / "data" / "pair_01"          # 实弹布局：pair 一层
        coarse = engine.make_plan(prof, "pair_01", "n", ["1u"], ["1u", "2u"],
                                  [1.5, 2.5], vstep=0.1)
        probe = engine.make_plan(prof, "pair_01", "n", ["1u"], ["1u", "2u"],
                                 [2.0], vstep=0.1)
        t4c.write_exp(data_pair, coarse, vstep=0.1)
        t4c.write_exp(data_pair, probe, vstep=0.1)
        for plans, name in ((coarse, "c.plan.jsonl"), (probe, "p.plan.jsonl")):
            f = self.tmp / name
            f.write_text("".join(json.dumps(p) + "\n" for p in plans),
                         encoding="utf-8")
        self.cplan, self.pplan = self.tmp / "c.plan.jsonl", self.tmp / "p.plan.jsonl"

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "cli.py"), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(self.tmp))

    def test_interp_check_cli_finds_paired_layout(self):
        rfp = self.tmp / "out"
        r = self.run_cli("interp-check", "--profile", str(self.prof_path),
                         "--coarse-plan", str(self.cplan),
                         "--probe-plan", str(self.pplan),
                         "--data", str(self.tmp / "data"),
                         "--out", str(rfp), "--tag", "lay", "--no-convert",
                         "--queue", str(self.tmp / "q.jsonl"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rep = json.loads((rfp / "lay.interp.json").read_text(encoding="utf-8"))
        self.assertEqual(rep["verdict"], "PASS", rep["hard_hits"] + rep["soft_notes"])
        self.assertEqual(rep["counts"]["pairs_checked"], 1)
        self.assertEqual(rep["counts"]["coarse_loaded"], 2)


if __name__ == "__main__":
    unittest.main()
