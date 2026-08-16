"""storage.records — 日检/运行记录存储（0.9.2 批次 7 引入；0.9.4 日度 Rotate）。

打板链路日检：每日采集/收口执行后写一条结构化记录。**按天分文件**
（records/YYYY-MM-DD.jsonl），天然按日期索引（Trace ID 检索友好），
保留 90 天自动清理。纯 jsonl 读写，不依赖引擎。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import config  # 模块引用（config.DATA_DIR 动态读取，测试 patch 生效）

RECORDS_DIR = "records"       # 日检子目录（按天文件）
LEGACY_FILE = "auction_daily.jsonl"  # 0.9.2 单文件兼容（recent 一并读取）
RETENTION_DAYS = 90           # 按天文件保留天数（超出清理）


def _dir() -> Path:
    return Path(config.DATA_DIR) / RECORDS_DIR


def _daily_path(day: str) -> Path:
    return _dir() / f"{day}.jsonl"


def _legacy_path() -> Path:
    return Path(config.DATA_DIR) / LEGACY_FILE


def append(record: dict) -> None:
    """追加一条日检记录（按天文件）；目录缺失自动创建；失败静默（不阻塞业务）。

    0.9.3：自动附加 trace_id（uuid4 前 12 位）；0.9.4：写入当日文件并清理过期。
    """
    try:
        import uuid
        record.setdefault("trace_id", uuid.uuid4().hex[:12])
        day = str(record.get("date") or "")[:8] or datetime.now().strftime("%Y%m%d")
        p = _daily_path(day)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        _cleanup()
    except OSError:
        pass  # 日检落盘失败不阻塞采集/收口主流程


def _cleanup() -> None:
    """清理超保留期的按天文件（保留最近 RETENTION_DAYS 天）。"""
    try:
        cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y%m%d")
        for f in _dir().glob("*.jsonl"):
            day = f.stem
            if len(day) == 8 and day.isdigit() and day < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def recent(n: int = 30) -> list[dict]:
    """最近 n 条日检记录（最新在前，跨按天文件 + 兼容旧单文件）；损坏行跳过。"""
    out: list[dict] = []
    today = datetime.now().strftime("%Y%m%d")
    # 按天文件从今天往回扫，直到凑够 n 条（每文件至多读 n 条尾部，防损坏行稀释）
    for offset in range(RETENTION_DAYS):
        day = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
        p = _daily_path(day)
        if not p.exists():
            if day < today and out:
                break  # 已过最新有数据日，不再向后找
            continue
        out.extend(_read_tail(p, n))
        if len(out) >= n:
            break
    if len(out) < n:
        out.extend(_read_tail(_legacy_path(), n))  # 0.9.2 单文件兼容
    return out[:n]


def _read_tail(p: Path, n: int) -> list[dict]:
    """单文件尾部至多 n 条记录（最新在前，损坏行跳过）。"""
    try:
        with p.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines[-n * 2:]):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
        if len(out) >= n:
            break
    return out
