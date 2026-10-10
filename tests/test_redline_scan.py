# -*- coding: utf-8 -*-
"""M4-4d 红线扫描器测试。

注意：本文件自身是 git 跟踪件 ⇒ 会被扫描器扫。所有触发字面量必须运行期拼接
构造——若哪天有人把它们写成明文，test_self_repo_clean 会当场变红（纪律自咬）。
"""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("redline_scan", ROOT / "tools" / "redline_scan.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

ZX = "z" + "xq1"             # 明确合成的词（不取真器件名形，防本地词表自咬）
FAKE_PW = "s3" + "cr3t"


def one(term: str, text: str):
    return rs.scan_text("t.py", text, [(term, rs.term_pat(term))])


class TestTermBoundary(unittest.TestCase):
    def test_plain_and_case_hit(self):
        self.assertTrue(one(ZX, "M1 (d g 0 0) " + ZX + " w=1u"))
        # 明文厂商字面=扫描器当场会咬(本行开发史实证: 曾漏拼接触发 DIRTY)
        self.assertTrue(one("smi" + "c", "upper " + "SM" + "IC" + " mention"))

    def test_path_dot_still_hits(self):
        # 口径差异主案: 排除"."的 demo 版会漏路径内器件名，纯字母数字边界须命中
        self.assertTrue(one(ZX, "include /pdk/model" + "." + ZX + ".lib"))

    def test_alnum_glue_reject(self):
        self.assertFalse(one(ZX, ZX + "2 and x" + ZX))            # zxq12 / xzxq1
        self.assertFalse(one("12" + "3456", "0123456789"))        # 数字串规则保住

    def test_underscore_boundary_hits(self):
        self.assertTrue(one(ZX, "M_" + ZX + "_1"))


class TestCredentialProbes(unittest.TestCase):
    def _cred(self, text):
        return [h for h in rs.scan_text("t", text, []) if h["kind"] == "credential"]

    def test_password_literal_hits_env_and_placeholder_exempt(self):
        self.assertTrue(self._cred("pass" + "word = \"" + FAKE_PW + "\""))
        self.assertFalse(self._cred('"password_env": "COLLECTOR_SSH_PWD"'))  # 变量名=豁免
        self.assertFalse(self._cred('"password": "YOUR_' + 'PWD"'))          # 占位符=豁免

    def test_key_block_and_license_and_ip(self):
        allh = rs.scan_text("t", "-----BEGIN RSA PR" + "IVATE KEY-----", [])
        self.assertTrue([h for h in allh if h["kind"] == "credential"])
        allh = rs.scan_text("t", "cat " + "lice" + "nse.dat", [])
        self.assertTrue([h for h in allh if h["kind"] == "probe"])
        allh = rs.scan_text("t", "license 申报制，不探测文件", [])
        self.assertFalse([h for h in allh if h["kind"] == "probe"])
        allh = rs.scan_text("t", "host = 192." + "168.1.7", [])
        self.assertTrue([h for h in allh if h["kind"] == "network"])
        allh = rs.scan_text("t", "sim=20.1.0.534.isr17", [])
        self.assertFalse([h for h in allh if h["kind"] == "network"])


class TestIgnoreGuard(unittest.TestCase):
    def test_repo_guard_passes(self):
        self.assertEqual(rs.ignore_guard(str(ROOT)), [])

    def test_missing_entry_detected(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / ".gitignore").write_text("data/\nout/\n", encoding="utf-8")
        self.assertEqual(rs.ignore_guard(str(tmp)), ["profiles_local/"])


class TestScanAfterAddDiscipline(unittest.TestCase):
    """未 add 的脏文件扫描器看不见；git add 后立刻可见=时序漏防堵死。"""

    def setUp(self):
        if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
            self.skipTest("无 git")
        self.tmp = Path(tempfile.mkdtemp())
        self.git("init", "-q")
        (self.tmp / ".gitignore").write_text("data/\nout/\nprofiles_local/\n",
                                             encoding="utf-8")
        (self.tmp / "profiles_local").mkdir()
        (self.tmp / "profiles_local" / "fingerprint_terms.txt").write_text(
            ZX + "\n", encoding="utf-8")          # 词表=发行环境缺失项，此处模拟生产形态
        (self.tmp / "clean.py").write_text("print(1)\n", encoding="utf-8")
        self.git("add", "clean.py")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.tmp, capture_output=True, text=True)

    def test_dirty_only_visible_after_add(self):
        dirty = self.tmp / "dirty.scs"
        dirty.write_text("M1 (d g 0 0) " + ZX + " w=1u\n", encoding="utf-8")
        rep = rs.run_scan(str(self.tmp))
        self.assertTrue(rep["clean"], rep["hits"])                # 未 add ⇒ 看不见
        self.git("add", "dirty.scs")
        rep2 = rs.run_scan(str(self.tmp))
        self.assertFalse(rep2["clean"])
        self.assertEqual(rep2["hits"][0]["detail"], ZX)           # add 后必见


@unittest.skipUnless((ROOT / ".git").is_dir(), "非 git 环境 ⇒ skip")
class TestSelfRepo(unittest.TestCase):
    def test_self_repo_clean(self):
        rep = rs.run_scan(str(ROOT))
        self.assertTrue(rep["clean"], rep["hits"])

    def test_public_profile_password_line_exempt(self):
        text = (ROOT / "profiles" / "EXAMPLE_generic_mos_spectre.json").read_text(encoding="utf-8")
        hits = [h for h in rs.scan_text("p.json", text, []) if h["kind"] == "credential"]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
