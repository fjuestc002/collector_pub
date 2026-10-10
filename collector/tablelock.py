# -*- coding: utf-8 -*-
"""tablelock.py — PDK 表目录写锁（P4.2 收集器侧，10-10 设计 §4）。

协议真源在优化器仓 engine/table_lock.py（读写租约全功能），本文件只实现
**写者半边**：collect pack 覆写表目录前抢写锁、完成后释放；活读者
（优化器 run 的租约）在场 ⇒ 拒单点名 pid/run_id/digest。
两仓的锁文件字段名与语义必须同字（改一侧必同步另一侧）：

    <pdk_dir>/.tablelock  {"writers":[{"pid","tool","ts"}],
                           "readers":[{"pid","tool","run_id","digest","ts"}]}

死租约自愈：判活前按 pid 活性清理（崩溃/断电不留僵尸锁）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

LOCK_NAME = ".tablelock"
_STILL_ACTIVE = 259


class TableLocked(RuntimeError):
    """写锁抢不到（活读者/他写者在场）。"""


def pid_alive(pid: int) -> bool:
    """跨平台判活。Windows 禁用 os.kill(pid,0)（CPython 会 TerminateProcess）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if k.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == _STILL_ACTIVE
                return True
            finally:
                k.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _lock_path(pdk_dir) -> Path:
    return Path(pdk_dir) / LOCK_NAME


def _load(pdk_dir) -> dict:
    try:
        doc = json.loads(_lock_path(pdk_dir).read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            doc = {}
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("writers", [])
    doc.setdefault("readers", [])
    for side in ("writers", "readers"):
        doc[side] = [e for e in doc[side] if isinstance(e, dict)]
    return doc


def _save(pdk_dir, doc: dict) -> None:
    f = _lock_path(pdk_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(f) + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, f)


def _prune_dead(pdk_dir) -> tuple:
    doc = _load(pdk_dir)
    w = [e for e in doc["writers"] if pid_alive(e.get("pid", -1))]
    r = [e for e in doc["readers"] if pid_alive(e.get("pid", -1))]
    if len(w) != len(doc["writers"]) or len(r) != len(doc["readers"]):
        doc["writers"], doc["readers"] = w, r
        _save(pdk_dir, doc)
    return w, r


def _fmt(entries: list) -> str:
    parts = []
    for e in entries:
        tag = "run=%s" % e.get("run_id") if e.get("run_id") else e.get("tool", "")
        dg = " 表 %s" % str(e.get("digest", ""))[-8:] if e.get("digest") else ""
        parts.append("pid=%s %s%s" % (e.get("pid"), tag, dg))
    return "、".join(parts) or "（无 pid 信息）"


def begin_write(pdk_dir, tool: str = "collector pack",
                force_unlock: bool = False, out=print) -> None:
    """抢写锁。活读者=优化器在跑（覆写=群里两套真值，绝对禁止）；
    force_unlock=True 人工强解：清场后抢锁，动作呈报留痕。"""
    pdk_dir = Path(pdk_dir)
    w, r = _prune_dead(pdk_dir)
    if r or w:
        if not force_unlock:
            raise TableLocked(
                "%s 表锁在场——拒写。读者：%s%s。"
                "优化器 run 在跑时请待其结束；确认对方进程已死可 --force-unlock"
                % (pdk_dir.name, _fmt(r),
                   ("（另有写者 %s）" % _fmt(w)) if w else ""))
        out("[pack][表锁] --force-unlock：清除既有租约 %s（人工强解，留痕）"
            % _fmt(w + r))
    doc = _load(pdk_dir)
    if force_unlock:
        doc["writers"] = []
        doc["readers"] = []
    else:
        doc["writers"] = [e for e in doc["writers"]
                          if pid_alive(e.get("pid", -1))]
        doc["readers"] = [e for e in doc["readers"]
                          if pid_alive(e.get("pid", -1))]
    doc["writers"].append({"pid": os.getpid(), "tool": tool, "ts": time.time()})
    _save(pdk_dir, doc)


def end_write(pdk_dir) -> None:
    pdk_dir = Path(pdk_dir)
    doc = _load(pdk_dir)
    pid = os.getpid()
    doc["writers"] = [e for e in doc["writers"] if e.get("pid") != pid]
    _save(pdk_dir, doc)
