"""storage.research_factory — 研究成果仓储工厂（0.9.5，M5）。

按环境变量 RESEARCH_STORE 选择实现（默认 sqlite；mydb = 回滚旧行为）：
  - sqlite：自建 SQLite（WAL）——主线（架构 D8：引擎死不影响研究成果可读）
  - mydb：引擎 mydb 薄适配——回滚预案（行为与 0.9.4 完全一致）

惰性单例；app.py 组合根调用 get_research_store() 注入 services。
"""
from __future__ import annotations

import os
import threading

from storage.research_store import (
    MydbResearchStore,
    ResearchStore,
    SqliteResearchStore,
)

_singleton: ResearchStore | None = None
_singleton_lock = threading.Lock()  # 0.9.11：单例惰性创建加锁（防多线程首次并发双实例）


def get_research_store() -> ResearchStore:
    """研究成果仓储单例（RESEARCH_STORE=sqlite 默认 / mydb 回滚）。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:  # 双检：锁内二次确认（首次访问可能来自多线程）
                mode = os.environ.get("RESEARCH_STORE", "sqlite").strip().lower()
                _singleton = (MydbResearchStore() if mode == "mydb"
                              else SqliteResearchStore())
    return _singleton


def reset() -> None:
    """复位单例（测试隔离用）。"""
    global _singleton
    with _singleton_lock:
        if isinstance(_singleton, SqliteResearchStore):
            _singleton.close()
        _singleton = None
