# -*- coding: utf-8 -*-
"""M4-4d QC 指纹台账：只存指纹/计数/状态，零物理数值 ⇒ 台账本身可进 git。

（10-09 实弹自首：run 行曾带 profile 原名——vendor 档案名= PDK 指纹，当场被
redline 自咬。改 sha16 摘要：本地对账用 `sha256(profile名)[:16]` 复算即可锚回，
git 面上零指纹。tag/deck_id 属用户输入面，责任在输入方。）

（10-04 第三轮代决的"代决三参数"红利兑现：账不随执行策略变形、
"为什么采"的证据链可公开审计，而器件数值仍被 data/ 红线锁死。）

行形制（append-only JSONL，一次 run = 1 行 qc_run + N 行 qc_deck）：
  {"ev":"qc_run",  ts, tag, profile_digest, plan_hash, ft_source, thresholds_sha,
   verdict, planned, loaded, missing, no_csv, corrupt, short, hard, soft}
  {"ev":"qc_deck", ts, deck_id, deck_sha256, status, rows,
   gates_hard:[...], gates_soft:[...]}
  status ∈ inspected|missing|no_csv|no_sweep|nan_reject|short
  gates ∈ {I1,I1dir,I2,I4q,I4qr,I4qdS,I4ft,I4ftS,I4cfg,I5,I5drift,SENT}

自守闸：序列化前递归禁 float（计数/行数皆 int，阈值只落 sha16，统计量
一律不入台账——完整报告留 out/ 的 qc.json）。违反=响亮抛，不静默。
台账文件请置于 git 跟踪区（缺省 ledgers/qc.jsonl）；报告/数据留 out//data/。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from collector.qc import qc_thresholds


def thresholds_sha(t: dict) -> str:
    return hashlib.sha256(json.dumps(t, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _no_float(obj, path: str = "$") -> None:
    """零物理数值自守：任何 float 出现在台账行即响亮拒。"""
    if isinstance(obj, float):
        raise ValueError(f"台账禁浮点（物理数值红线）: {path} = {obj!r}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _no_float(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _no_float(v, f"{path}[{i}]")


def build_rows(prof: dict, plans: list[dict], report: dict, tag: str,
               plan_hash: str = "") -> list[dict]:
    """一次质检 → 台账行集。ts 取本次调用时刻（同一 run 内一致）。"""
    ts = datetime.now().isoformat(timespec="seconds")
    t = qc_thresholds(prof)
    c = report["completeness"]
    run = {"ev": "qc_run", "ts": ts, "tag": tag,
           "profile_digest": hashlib.sha256(
               (report.get("profile") or prof.get("name") or "")
               .encode("utf-8")).hexdigest()[:16],
           "plan_hash": plan_hash, "ft_source": t["ft_source"],
           "thresholds_sha": thresholds_sha({k: t[k] for k in t if k != "note"}),
           "verdict": report["verdict"],
           "planned": int(c["planned"]), "loaded": int(c["loaded"]),
           "missing": len(c["missing"]), "no_csv": len(c["no_csv"]),
           "corrupt": len(c["nan_reject"]), "short": len(c["short"]),
           "hard": len(report["hard_hits"]), "soft": len(report["soft_notes"])}
    rows = [run]
    flags = report.get("deck_flags", {})
    for p in plans:
        df = flags.get(p["deck_id"], {"status": "unjudged", "hard": [], "soft": []})
        rows.append({"ev": "qc_deck", "ts": ts,
                     "deck_id": p["deck_id"], "deck_sha256": p["deck_sha256"],
                     "status": df["status"], "rows": df.get("rows"),
                     "gates_hard": sorted(set(df["hard"])),
                     "gates_soft": sorted(set(df["soft"]))})
    return rows


def append(path: Path | str, rows: list[dict]) -> int:
    """逐行自守后追加；返回写入行数。目录不存在则创建。"""
    p = Path(path)
    for r in rows:
        _no_float(r)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)
