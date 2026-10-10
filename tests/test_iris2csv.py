# -*- coding: utf-8 -*-
"""M4-4b iris2csv 降序扫段回归。

三层: ①sweep_match 纯函数判据（升/降/非单调/端点/容差）；
②真样本端到端——p 管 0→−5 降序件必须 rc=0 且 sweep 列自动识别
  （v0.3 在此件报 [WARN] rc=2，即 10-09 入册"待实机验证④"的复现件）；
  零扰动铁证=新旧 CSV 数值区逐位一致、仅列名 col26→sweep；
③双仓同字镜像闸：collector/tools 与 gsieve/tools/ocn_probes 逐字节等
  （镜像在 gsieve 系工作树件未入库，故只校文件本体，无 git 提交面）。
真样本住 gitignore 的 out//data/，公开环境自动 skip。
"""
import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("iris2csv", ROOT / "tools" / "iris2csv.py")
iris2csv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iris2csv)

P_DEV = ROOT / "out" / "samples" / "p_dev" / "dc.dc"          # 降序 0→−5（rc2 复现件）
P_DEV_OLD_CSV = ROOT / "out" / "samples" / "p_dev" / "dc.csv"  # v0.3 实跑存档
N_ASC = ROOT / "data" / "pair_01" / "pair_01.n.L1u.V1.5" / "dc.dc"
MIRROR = ROOT.parent / "gsieve" / "tools" / "ocn_probes" / "iris2csv.py"


class TestSweepMatch(unittest.TestCase):
    def test_ascending(self):
        self.assertTrue(iris2csv.sweep_match([0.0, 0.5, 1.0], 0.0, 1.0))

    def test_descending_start_end_semantics(self):
        # 实证: PSF 降序件 min=0.0 max=-5.0 系"起/止"非算术 min/max
        self.assertTrue(iris2csv.sweep_match([0.0, -2.5, -5.0], 0.0, -5.0))

    def test_non_monotonic_reject(self):
        self.assertFalse(iris2csv.sweep_match([0.0, 1.0, 0.5, 2.0], 0.0, 2.0))

    def test_endpoint_mismatch_reject(self):
        self.assertFalse(iris2csv.sweep_match([0.0, 0.5, 1.0], 0.0, 0.99))
        self.assertFalse(iris2csv.sweep_match([0.0, -2.5, -5.0], 0.0, -4.9))

    def test_tolerance_boundary(self):
        self.assertTrue(iris2csv.sweep_match([1e-11, 0.5, 1.0], 0.0, 1.0))
        self.assertFalse(iris2csv.sweep_match([1e-6, 0.5, 1.0], 0.0, 1.0))

    def test_degenerate_inputs(self):
        self.assertFalse(iris2csv.sweep_match([0.0], 0.0, 0.0))
        self.assertFalse(iris2csv.sweep_match([0.0, 1.0], None, 1.0))
        self.assertFalse(iris2csv.sweep_match([0.0, 1.0], 0.0, None))


class TestE2ERealSamples(unittest.TestCase):
    def _run(self, dc: Path):
        out = Path(tempfile.mkdtemp()) / "dc.csv"
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "iris2csv.py"),
                            str(dc), str(out)], capture_output=True)
        return r.returncode, r.stdout.decode("utf-8", "replace"), out

    @unittest.skipUnless(P_DEV.is_file(), "本地无 p 管降序实样(非实验环境) ⇒ skip")
    def test_descending_slice_now_rc0(self):
        rc, so, out = self._run(P_DEV)
        self.assertEqual(rc, 0, so)
        self.assertIn("[CHECK]", so)
        self.assertNotIn("WARN", so)
        with out.open(encoding="utf-8") as f:
            rows = list(csv.reader(f))
        hdr, body = rows[0], rows[1:]
        gs, gg = hdr.index("sweep"), hdr.index("g")
        sv = [float(r[gs]) for r in body]
        self.assertEqual(sv, [float(r[gg]) for r in body])   # b=s=0 ⇒ Vg=Vgs
        self.assertTrue(all(sv[i + 1] < sv[i] for i in range(len(sv) - 1)))
        self.assertEqual((sv[0], sv[-1]), (0.0, -5.0))

    @unittest.skipUnless(P_DEV.is_file() and P_DEV_OLD_CSV.is_file(),
                         "本地无 v0.3 存档 CSV ⇒ skip")
    def test_zero_data_perturbation_vs_v03(self):
        _, _, out = self._run(P_DEV)
        with P_DEV_OLD_CSV.open(encoding="utf-8") as f:
            old = list(csv.reader(f))
        with out.open(encoding="utf-8") as f:
            new = list(csv.reader(f))
        self.assertEqual(old[1:], new[1:])                     # 数值区逐位等
        diffs = [(i, a, b) for i, (a, b) in enumerate(zip(old[0], new[0])) if a != b]
        self.assertEqual(diffs, [(0, "col26", "sweep")])       # 唯一变化=列名

    @unittest.skipUnless(N_ASC.is_file(), "本地无 n 管实样 ⇒ skip")
    def test_ascending_unchanged(self):
        rc, so, _ = self._run(N_ASC)
        self.assertEqual(rc, 0, so)
        self.assertIn("sweep", so)


@unittest.skipUnless(MIRROR.is_file(), "本机无 gsieve 镜像 ⇒ skip")
class TestMirrorSameWords(unittest.TestCase):
    def test_byte_identical(self):
        self.assertEqual((ROOT / "tools" / "iris2csv.py").read_bytes(),
                         MIRROR.read_bytes(),
                         "双仓同字镜像被单边私改（G63 镜像闸先例口径）")


if __name__ == "__main__":
    unittest.main()
