# -*- coding: utf-8 -*-
"""M5 GUI 仪表盘（PySide6；发行面可选——核心库零 Qt 依赖，PySide6 只在 gui 层）。

启动: py -3.14 gui.py
职责边界（人签字位的可视化）：
  * 队列台账一眼看全：状态/键/闸/跨度/点名实例/备注；
  * 干跑列账（缺省）、勾选采纳→签字开火、驳回必带理由落事件账；
  * 阈值面板：gm/ids 容差即时生效于本次动作；"写画像覆写"只准写未跟踪画像
    （profiles/ 是 git 面，改前须拷到 profiles_local/——响亮拒）；
  * 证据流摘要：最近一次 qc / interp / drain 报告的判决与计数；
  * 长任务在 worker 线程里跑，日志窗滚动，UI 不冻结。
红线自查：本文件不打凭证、不写 git 面台账（复用 loop/qc_ledger 的零浮点件）；
发射仍走 loop.run_drain 缺省 fire 路径（预算/分包/水位全在发射原语里）。
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QThread, Signal  # noqa: E402
from PySide6.QtGui import QColor, QFont  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)

from collector import loop  # noqa: E402
from collector.profile import load_profile  # noqa: E402

COLS = ("状态", "键", "闸", "跨度", "实例", "备注")


def _latest_json(d: Path) -> Path | None:
    if not d.is_dir():
        return None
    fs = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime)
    return fs[-1] if fs else None


class Worker(QThread):
    """在别的线程里跑一圈闭环；日志走信号，异常响亮回窗。"""
    sig_log = Signal(str)
    sig_done = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            rep = self.fn(self.sig_log.emit)
        except Exception as e:
            self.sig_log.emit(f"[exc] {type(e).__name__}: {e}")
            self.sig_log.emit(traceback.format_exc(limit=6))
            rep = None
        self.sig_done.emit(rep)


class MainWindow(QMainWindow):
    def __init__(self, defaults=None):
        super().__init__()
        d = defaults or {}
        self.fire_fn = d.get("fire_fn")
        self.qc_ledger_path = d.get("qc_ledger",
                                    str(ROOT / "ledgers" / "qc.jsonl"))
        self.setWindowTitle("收集器 M5 闭环调度台（披沙数据集收集器）")
        self.worker: Worker | None = None
        self.busy = False

        top = QFormLayout()
        self.ed_prof = QLineEdit(d.get("prof", str(ROOT / "profiles_local" / "vendor_a.json")))
        self.ed_coarse = QLineEdit(d.get("coarse", str(ROOT / "out" / "plan" / "m2c_coarse.plan.jsonl")))
        self.ed_data = QLineEdit(d.get("data", str(ROOT / "data_charge")))
        for lab, ed in (("画像", self.ed_prof), ("粗格计划", self.ed_coarse),
                        ("数据根", self.ed_data)):
            row = QHBoxLayout()
            row.addWidget(ed)
            b = QPushButton("…")
            b.setFixedWidth(34)
            b.clicked.connect(lambda _=False, e=ed: self._browse(e))
            row.addWidget(b)
            top.addRow(lab, row)

        self.spin_gm = QDoubleSpinBox()
        self.spin_gm.setRange(0.05, 50.0)
        self.spin_gm.setDecimals(2)
        self.spin_gm.setValue(1.00)
        self.spin_gm.setSuffix(" %")
        self.spin_ids = QDoubleSpinBox()
        self.spin_ids.setRange(0.05, 50.0)
        self.spin_ids.setDecimals(2)
        self.spin_ids.setValue(2.00)
        self.spin_ids.setSuffix(" %")
        thr = QHBoxLayout()
        thr.addWidget(QLabel("gm 容差"))
        thr.addWidget(self.spin_gm)
        thr.addWidget(QLabel("ids 容差"))
        thr.addWidget(self.spin_ids)
        b_ovr = QPushButton("写画像覆写")
        b_ovr.clicked.connect(self.write_override)
        thr.addWidget(b_ovr)
        thr.addStretch(1)
        top.addRow("阈值", thr)

        self.sp_vstep = QDoubleSpinBox()
        self.sp_vstep.setRange(0.001, 1.0)
        self.sp_vstep.setDecimals(3)
        self.sp_vstep.setValue(float(d.get("vstep", 0.1)))
        self.sp_vstep.setSuffix(" V")
        self.ck_charge = QCheckBox("charge 列（--charge 同和）")
        self.ck_charge.setChecked(bool(d.get("charge", True)))
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("vstep"))
        row2.addWidget(self.sp_vstep)
        row2.addWidget(self.ck_charge)
        row2.addStretch(1)
        top.addRow("发射形制", row2)

        self.tbl = QTableWidget(0, len(COLS))
        self.tbl.setHorizontalHeaderLabels(COLS)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setSectionResizeMode(len(COLS) - 1,
                                                         QHeaderView.Stretch)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)

        btns = QHBoxLayout()
        for text, slot in (("刷新台账", self.refresh),
                           ("干跑列账", lambda: self.run_drain(dry=True)),
                           ("采纳所选并开火", lambda: self.run_drain(dry=False)),
                           ("全收并开火", lambda: self.run_drain(dry=False, alls=True)),
                           ("驳回所选(理由必录)", self.dismiss)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch(1)

        self.ed_queue = QLineEdit(str(ROOT / "out" / "queue" / "densify.jsonl"))
        self.ed_events = QLineEdit(str(ROOT / "out" / "queue" / "events.jsonl"))
        self.ed_plans = QLineEdit(str(ROOT / "out" / "queue" / "densify_plans.jsonl"))
        acct = QFormLayout()
        acct.addRow("提案队列", self.ed_queue)
        acct.addRow("事件账", self.ed_events)
        acct.addRow("累计加密计划", self.ed_plans)
        g_acct = QGroupBox("out/ 账区（gitignore，派生统计允许）")
        g_acct.setLayout(acct)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        self.ed_evidence = QLabel("证据流：—")
        self.ed_evidence.setWordWrap(True)

        lay = QVBoxLayout()
        lay.addLayout(top)
        lay.addLayout(btns)
        lay.addWidget(self.tbl, 3)
        lay.addWidget(g_acct)
        lay.addWidget(self.ed_evidence)
        lay.addWidget(self.log, 4)
        w = QWidget()
        w.setLayout(lay)
        self.setCentralWidget(w)
        self.resize(1050, 720)
        self.refresh()

    # ---------- 基础件 ----------

    def _browse(self, ed: QLineEdit):
        p, _ = QFileDialog.getOpenFileName(self, "选择文件", ed.text() or ".")
        if p:
            ed.setText(p)

    def _log(self, s: str):
        self.log.appendPlainText(s)

    def _set_busy(self, b: bool):
        self.busy = b
        for child in self.findChildren(QPushButton):
            child.setEnabled(not b)

    # ---------- 台账面 ----------

    def refresh(self):
        props = loop.load_proposals(self.ed_queue.text())
        events = loop.load_last_events(self.ed_events.text())
        openk = loop.open_keys(props, events)
        self.tbl.setRowCount(0)
        for k, row in props.items():
            ev = events.get(k)
            st = ev["status"] if ev else ("在案" if k in openk else "已闭")
            i = self.tbl.rowCount()
            self.tbl.insertRow(i)
            vals = (st, k, row.get("gate", ""),
                    f"{row['span'][0]:g}→{row['span'][1]:g}" if row.get("span") else "",
                    ",".join(row.get("instances") or []),
                    (ev or {}).get("reason") or ("子跨再提案" if (ev or {}).get("requeued") else ""))
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == 0 and st in ("resolved", "dismissed", "已闭"):
                    it.setForeground(QColor("gray"))
                elif c == 0:
                    it.setForeground(QColor("darkRed"))
                self.tbl.setItem(i, c, it)
        self._log(f"[gui] 台账刷新：在案 {len(openk)} / 总 {len(props)}")
        e = []
        for lab, d in (("qc", ROOT / "out" / "qc"), ("interp", ROOT / "out" / "interp"),
                       ("drain", Path(self.ed_queue.text()).parent.parent / "drain")):
            f = _latest_json(Path(d))
            if f:
                try:
                    j = json.loads(f.read_text(encoding="utf-8"))
                    e.append(f"{lab} {f.stem}: {j.get('verdict', j.get('interp', {}).get('verdict', '—'))}"
                             f"@{datetime.fromtimestamp(f.stat().st_mtime).strftime('%m-%d %H:%M')}")
                except Exception:
                    pass
        self.ed_evidence.setText("证据流：" + ("  |  ".join(e) if e else "无报告"))

    def _selected_keys(self) -> list[str]:
        ks = []
        for it in self.tbl.selectedItems():
            if it.column() == 1:
                ks.append(it.text())
        return sorted(set(ks))

    def _ask_reason(self, ks):
        return QInputDialog.getMultiLineText(
            self, "驳回理由（必录，落事件账）", f"键：{', '.join(ks)}", "")

    # ---------- 动作 ----------

    def _common_kw(self):
        prof = load_profile(self.ed_prof.text())
        cp = Path(self.ed_coarse.text())
        rows = [json.loads(l) for l in cp.read_text(encoding="utf-8").splitlines()
                if l.strip()]
        if not rows:
            raise SystemExit(f"[err] 粗格计划为空: {cp}")
        kw = dict(prof=prof, coarse_plans=rows, data_root=self.ed_data.text(),
                  queue_path=self.ed_queue.text(), events_path=self.ed_events.text(),
                  dense_plans_path=self.ed_plans.text(),
                  report_dir=str(Path(self.ed_queue.text()).parent.parent / "drain"),
                  gm_tol=self.spin_gm.value() / 100.0,
                  ids_tol=self.spin_ids.value() / 100.0,
                  qc_ledger_path=self.qc_ledger_path,
                  vstep=self.sp_vstep.value(), charge=self.ck_charge.isChecked())
        return kw

    def run_drain(self, dry: bool, alls: bool = False):
        if self.busy:
            return
        ks = [] if (dry or alls) else self._selected_keys()
        if not dry and not alls and not ks:
            self._log("[gui] 采纳开火需先在表里选行（或按全收）")
            return
        if not dry:
            env = load_profile(self.ed_prof.text()).get("connection", {}) \
                .get("password_env", "COLLECTOR_VM_PWD")
            if not os.environ.get(env):
                self._log(f"[gui][警告] 环境未设 {env}（GUI 里无法应答密码提示，"
                          f"发射大概率连不上——请 setx 后重启本窗口）")

        def task(emit):
            kw = self._common_kw()
            if dry:
                adopt = None
            else:
                adopt = "all" if alls else ks
            kw.update(out=emit, fire_flag=not dry, adopt=adopt,
                      tag=f"gui_{datetime.now():%H%M%S}")
            if self.fire_fn is not None:
                kw["fire_fn"] = self.fire_fn
            rep = loop.run_drain(**kw)
            emit(f"[gui] 动作完：planned={len(rep['planned_decks'])} "
                 f"new_queue={rep['new_queue_rows']}")
            return rep
        self._launch(task)

    def dismiss(self):
        if self.busy:
            return
        ks = self._selected_keys()
        if not ks:
            self._log("[gui] 驳回需先选行")
            return
        txt, ok = self._ask_reason(ks)
        if not ok or not txt.strip():
            self._log("[gui] 未给理由，驳回取消")
            return

        def task(emit):
            loop.run_drain(prof=load_profile(self.ed_prof.text()),
                           coarse_plans=[], data_root=self.ed_data.text(),
                           queue_path=self.ed_queue.text(),
                           events_path=self.ed_events.text(),
                           dense_plans_path=self.ed_plans.text(),
                           vstep=0.1, charge=False, dismiss=ks, reason=txt.strip(),
                           out=emit, tag=f"gui_{datetime.now():%H%M%S}")
            emit(f"[gui] 驳回入账 {len(ks)} 件")
            return None
        self._launch(task)

    def write_override(self):
        pp = Path(self.ed_prof.text())
        try:
            inside = pp.resolve().is_relative_to((ROOT / "profiles").resolve())
        except Exception:
            inside = False
        if inside:
            self._log("[gui] 拒写：profiles/ 是 git 跟踪的示例画像——"
                      "请拷到 profiles_local/ 再覆写")
            return
        d = json.loads(pp.read_text(encoding="utf-8"))
        qc_ = d.setdefault("qc", {})
        itp = qc_.setdefault("interp", {})
        itp["gm_tol"] = round(self.spin_gm.value() / 100.0, 6)
        itp["ids_tol"] = round(self.spin_ids.value() / 100.0, 6)
        pp.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
        self._log(f"[gui] 覆写已落画像：gm={itp['gm_tol']} ids={itp['ids_tol']}（{pp.name}）")

    def _launch(self, task):
        self._set_busy(True)
        self.worker = Worker(task, self)
        self.worker.sig_log.connect(self._log)
        self.worker.sig_done.connect(self._on_done)
        self.worker.start()

    def _on_done(self, _rep):
        self._set_busy(False)
        self.refresh()


def make_app() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv)
    return app


def main() -> int:
    app = make_app()
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
