"""storage.records — 日检/运行记录存储（0.9.2 批次 7 可观测性三件套之 A）。

打板链路日检：每日采集/收口执行后写一条结构化记录（jsonl 追加，文件上限滚动），
供 /api/auction/daily 查询与偏差监测（n_samples 环比突变 → 告警信号）。
纯 jsonl 读写，不依赖引擎。
"""
from __future__ import annotations

import json
from pathlib import Path

import config  # 模块引用（config.DATA_DIR 动态读取，测试 patch 生效）

RECORDS_FILE = "auction_daily.jsonl"
MAX_LINES = 2000  # 文件行数上限（超出保留尾部，与 mcp_calls 同策略）


def _path() -> Path:
    return Path(config.DATA_DIR) / RECORDS_FILE


def append(record: dict) -> None:
    """追加一条日检记录（jsonl）；目录缺失自动创建；失败静默（不阻塞业务）。

    0.9.3：自动附加 trace_id（uuid4 前 12 位）——AI 客户端可凭响应/日志中的
    trace_id 在日检记录中定位同一次执行。
    """
    try:
        import uuid
        record.setdefault("trace_id", uuid.uuid4().hex[:12])
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        _trim(p)
    except OSError:
        pass  # 日检落盘失败不阻塞采集/收口主流程


def _trim(p: Path) -> None:
    """行数超上限 → 保留尾部（临时文件 + 替换，防并发半截）。"""
    try:
        with p.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) <= MAX_LINES:
            return
        tmp = p.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.writelines(lines[-MAX_LINES:])
        tmp.replace(p)
    except OSError:
        pass


def recent(n: int = 30) -> list[dict]:
    """最近 n 条日检记录（最新在前）；缺失/损坏行跳过。"""
    try:
        with _path().open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines[-n * 2:]):  # 多读防损坏行稀释
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
