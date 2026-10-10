# -*- coding: utf-8 -*-
"""远程执行运输层（paramiko 短命连接 + shell 包裹 + sftp）。

发表纪律（10-09 用户裁定）：零凭证入库——连接参数从画像读，
认证三级 = password_env 环境变量 → 运行时交互输入(getpass 不留盘) → SSH key+agent。
无硬编码缺省密码；不探测 license 文件。

引擎测试不碰本模块：engine 只依赖 run/put_text/get 等鸭子接口，离线测试注入 Fake。
"""
from __future__ import annotations

import getpass
import io
import os
import posixpath
import sys


class Remote:
    """paramiko SSHClient+SFTP 的薄封装。每个工作线程应持有一条独立连接。"""

    def __init__(self, cli, sftp, shell_wrap="bash -lc"):
        self.cli = cli
        self.sftp = sftp
        self.shell_wrap = shell_wrap

    def run(self, cmd: str, timeout: int = 900):
        """执行命令，返回 (rc, stdout+stderr 文本)。"""
        _, so, se = self.cli.exec_command(cmd, timeout=timeout)
        out = so.read().decode("utf-8", "replace")
        err = se.read().decode("utf-8", "replace")
        rc = so.channel.recv_exit_status()
        return rc, (out + ("\n" + err if err.strip() else ""))

    def run_b(self, inner: str, timeout: int = 900):
        """非交互 ssh 须包 login shell（VM 实证：bash -lc 才吃得到环境）。"""
        return self.run(f"{self.shell_wrap} '{inner}'", timeout=timeout)

    def put_text(self, text: str, rpath: str) -> None:
        with self.sftp.open(rpath, "w") as f:
            f.write(text)

    def get(self, rpath: str, lpath: str) -> int:
        self.sftp.get(rpath, lpath)
        return os.path.getsize(lpath)

    def close(self) -> None:
        try:
            self.sftp.close()
        finally:
            self.cli.close()


def connect(conn: dict, host: str | None = None) -> Remote:
    """按画像 connection 建连。凭证三级，无缺省回退。"""
    import paramiko  # 延迟导入：离线测试/引擎逻辑不依赖 paramiko
    host = host or conn["host"]
    user = conn["user"]
    env_name = conn.get("password_env", "")
    pwd = os.environ.get(env_name) if env_name else None
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if pwd:
        cli.connect(host, username=user, password=pwd, timeout=20,
                    allow_agent=False, look_for_keys=False)
    elif sys.stdin.isatty():
        pwd = getpass.getpass(f"{user}@{host} 密码(不留盘): ")
        cli.connect(host, username=user, password=pwd, timeout=20,
                    allow_agent=False, look_for_keys=False)
    else:
        cli.connect(host, username=user, timeout=20,
                    allow_agent=True, look_for_keys=True)  # 发布版推荐 key+agent
    return Remote(cli, cli.open_sftp(), conn.get("shell_wrap", "bash -lc"))
