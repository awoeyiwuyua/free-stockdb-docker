"""ops.logging — 同步/运行日志（横切关注点，0.9.2 批次 2 从 app.py 搬迁）。

内容：log/tail_log/now——同步日志文件（DATA_DIR/sync.log）的追加与读取。
0.9.1 起路径动态读 config.DATA_DIR（修复：旧 SYNC_LOG 常量在测试 patch DATA_DIR
后仍写默认 /data 的缺陷——CI Linux 实证）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import config  # 模块引用（config.DATA_DIR 动态读取，测试 patch config 生效）


def _sync_log_path() -> "Path":
    """同步日志路径：动态读 config.DATA_DIR（部署/测试可注入）。"""
    return Path(config.DATA_DIR) / "sync.log"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(line: str) -> None:
    p = _sync_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"{now()}  {line}\n")


def tail_log(n: int = 200) -> str:
    p = _sync_log_path()
    if not p.exists():
        return "（暂无同步日志）"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])
