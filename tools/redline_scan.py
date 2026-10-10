# -*- coding: utf-8 -*-
"""redline_scan.py — 发表红线扫描器（M4-4d，demo 三层判据收集器改编）

纪律：**必须跑在 git add 之后**。扫描集 = git ls-files（已跟踪面）
∪ git diff --cached --name-only --diff-filter=ACMR（本次暂存面）——
未 add 的文件扫描器看不见 ⇒ "add 前扫过了、add 时夹带"这类时序漏防被堵死。
（10-09 两连自首教训的代码化。）

三层（对收集器仓收紧，非 demo 的说明面豁免制——用户裁定=发行物零指纹，
叙述性提及也不允许，仓库里没有"正当提及别家厂商"的场景）：
  ① 指纹硬零：内置词（公共商业厂商名，源码内以拼接写出防自扫）
     + profiles_local/fingerprint_terms.txt（gitignore，一行一词；
     清单本身也是情报，发行环境无此文件⇒只扫内置词）。命中=FAIL。
  ② 凭证与探测面：口令字面赋值（环境变量名式大写值与 YOUR_* 占位符豁免）、
     PRIVATE KEY 块、license 文件探测引用、私网 IP —— 命中=FAIL。
  ③ 忽略护栏：.gitignore 必须仍覆盖 data/ out/ profiles_local/ ——
     任一失守=FAIL（红线基础设施回归闸，防"顺手删一行 ignore"酿大祸）。

词边界规则 = (?<![0-9A-Za-z])term(?![0-9A-Za-z]) 不分大小写。
与 demo 版 (?![\\w.:]) 的差异（口径定案入册）：排除 "." 会让
"path/任意件名.lib" 里的器件名漏检；纯字母数字边界仍保住数字串规则
（"123456" 在 "0123456789" 中不算命中）。

用法: python tools/redline_scan.py [--root .] [--json out/redline_scan.json] [--show 10]
退出码: 0=CLEAN, 1=DIRTY, 3=环境不满足(非 git 仓)。
JSON 留档只准落 gitignore 区（写出前自校验），防留档件本身成为泄露载体。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

BUILTIN_TERMS = ["smi" + "c", "tsm" + "c"]          # 公共商业厂商名(拼接防自扫)
LOCAL_TERMS = "profiles_local/fingerprint_terms.txt"
SELF_PREFIX = "redline_scan"
BINARY_EXTS = {".png", ".pdf", ".zip", ".gz", ".7z", ".pyd", ".so", ".dll",
               ".exe", ".dc", ".raw", ".npz", ".npy", ".o", ".a", ".xlsx"}
IGNORE_REQUIRED = ["data/", "out/", "profiles_local/"]

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")      # COLLECTOR_SSH_PWD 式=变量名非口令
PLACEHOLDER = re.compile(r"^YOUR", re.I)
PWD_RE = re.compile(r"(?i)pass\w*\s*[:=]\s*[\"']([^\"']*)[\"']")
KEY_RE = re.compile("BEGIN" + r"\s[A-Z ]*PRIVATE\s+[A-Z ]*KEY")
LIC_PATS = [("li" + "cense.dat", "lic_probe"), ("lms" + "tat", "lic_probe"),
            ("flex" + "lm", "lic_probe"), ("vendor" + ".dat", "lic_probe")]
IP_RE = re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                   r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
                   r"|192\.168\.\d{1,3}\.\d{1,3})\b")


def term_pat(term: str) -> re.Pattern:
    return re.compile(r"(?<![0-9A-Za-z])" + re.escape(term) + r"(?![0-9A-Za-z])", re.I)


def load_terms(root) -> list[str]:
    terms = list(BUILTIN_TERMS)
    f = os.path.join(root, LOCAL_TERMS)
    if os.path.isfile(f):
        for ln in open(f, encoding="utf-8"):
            t = ln.strip()
            if t and t.lower() not in [x.lower() for x in terms]:
                terms.append(t)
    return terms


def _git(root, *args) -> list[str]:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return [x.strip() for x in r.stdout.splitlines() if x.strip()]


def collect_scan_set(root) -> list[str]:
    """扫描集 = 已跟踪 ∪ 暂存(ACMR)，交集去重；未 add 的工作树文件不在面上。"""
    tracked = set(_git(root, "ls-files"))
    staged = set(_git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR"))
    out = []
    for rel in sorted(tracked | staged):
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            continue                      # 暂存后已删(D 被 filter 挡, 此处兜并发)
        if os.path.splitext(rel)[1].lower() in BINARY_EXTS:
            continue
        if os.path.basename(rel).lower().startswith(SELF_PREFIX):
            continue                      # 自指污染豁免(demo 先例)
        out.append(rel)
    return out


def scan_text(rel: str, text: str, terms: list[tuple[str, re.Pattern]]) -> list[dict]:
    hits = []
    for i, ln in enumerate(text.splitlines(), 1):
        for t, pat in terms:
            if pat.search(ln):
                hits.append({"file": rel, "line": i, "kind": "fingerprint",
                             "detail": t, "text": ln.strip()[:160]})
        m = PWD_RE.search(ln)
        if m and not (ENV_NAME.match(m.group(1)) or PLACEHOLDER.match(m.group(1))):
            hits.append({"file": rel, "line": i, "kind": "credential",
                         "detail": "pwd_literal", "text": ln.strip()[:160]})
        if KEY_RE.search(ln):
            hits.append({"file": rel, "line": i, "kind": "credential",
                         "detail": "private_key_block", "text": ln.strip()[:160]})
        for frag, kind in LIC_PATS:
            if frag.lower() in ln.lower():
                hits.append({"file": rel, "line": i, "kind": "probe",
                             "detail": kind, "text": ln.strip()[:160]})
                break
        if IP_RE.search(ln):
            hits.append({"file": rel, "line": i, "kind": "network",
                         "detail": "private_ip", "text": ln.strip()[:160]})
    return hits


def ignore_guard(root) -> list[str]:
    """层③：.gitignore 必须仍覆盖三目录。返回失守项（空=过）。"""
    gi = os.path.join(root, ".gitignore")
    if not os.path.isfile(gi):
        return [".gitignore 缺失"]
    covered = {ln.strip() for ln in open(gi, encoding="utf-8") if ln.strip()}
    return [d for d in IGNORE_REQUIRED if d not in covered]


def run_scan(root: str) -> dict:
    terms = [(t, term_pat(t)) for t in load_terms(root)]
    files = collect_scan_set(root)
    hits = []
    for rel in files:
        p = os.path.join(root, rel)
        try:
            text = open(p, encoding="utf-8", errors="strict").read()
        except (UnicodeDecodeError, OSError):
            text = open(p, "rb").read().decode("utf-8", errors="replace")
        hits.extend(scan_text(rel, text, terms))
    guard = ignore_guard(root)
    for d in guard:
        hits.append({"file": ".gitignore", "line": 0, "kind": "ignore_guard",
                     "detail": d, "text": ""})
    return {"scanned": len(files), "terms": len(terms),
            "hits": hits, "clean": not hits}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
    ap.add_argument("--json", default=None)
    ap.add_argument("--show", type=int, default=10)
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                       cwd=a.root, capture_output=True, text=True)
    if r.returncode != 0:
        print("[redline][err] 非 git 仓：扫描集无从确定（纪律=后置 git add）")
        return 3
    rep = run_scan(a.root)
    kinds = {}
    for h in rep["hits"]:
        kinds[h["kind"]] = kinds.get(h["kind"], 0) + 1
    print(f"[redline] 扫 {rep['scanned']} 文件 × {rep['terms']} 词"
          f"（含本地词表）; 违规 {len(rep['hits'])} 条 分类 {json.dumps(kinds, ensure_ascii=False)}")
    for h in rep["hits"][:a.show]:
        print(f"  {h['file']}:{h['line']} [{h['kind']}/{h['detail']}] {h['text']}")
    if a.json:
        jp = os.path.abspath(a.json)
        relj = os.path.relpath(jp, os.path.abspath(a.root)).replace("\\", "/")
        if not relj.startswith("out/"):
            # 留档件会带词与命中原文，只准落 gitignore 区，防成为新泄露载体
            print(f"[redline][err] --json 目标须为 out/ 下路径（gitignore 区）: {a.json}")
            return 1
        os.makedirs(os.path.dirname(jp), exist_ok=True)
        json.dump(rep, open(jp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"  [写] {a.json}（gitignore 区，勿提交）")
    print(f"[结论] {'CLEAN' if rep['clean'] else 'DIRTY——见明细'}")
    return 0 if rep["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
