"""ops.alerts — 告警中心（横切关注点，0.9.2 批次 2 从 app.py 搬迁）。

内容：Alerts 类（JSON 持久化 DATA_DIR/alerts.json + 内存镜像）、模块级单例、
notify_alert 生产接线点。行为与 app.py 搬迁前完全一致（0.8.x 测试基线）。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime

import config  # 模块引用（config.DATA_DIR 动态读取，测试 patch config 生效）

MAX_ALERTS = 200                      # 告警中心滚动上限（保留最新 200 条）
ALERT_LEVELS = ("info", "warning", "error")   # 合法告警级别（小写）
ALERT_LEVEL_ALIASES = {"warn": "warning"}     # 级别别名归一化


def _now_iso() -> str:
    """当前本地时间 ISO（秒级）：2026-08-14T21:52:30。"""
    return datetime.now().isoformat(timespec="seconds")


def _warn(msg: str) -> None:
    """stderr 提示（落盘失败等降级场景；不抛）。"""
    print(f"ops: {msg}", file=sys.stderr)


class Alerts:
    """面板内告警中心：JSON 持久化 DATA_DIR/alerts.json + 内存镜像。

    文件格式：JSON 数组 [{ts, level, source, message}, ...]，按时间升序存储；
    list() 返回最新在前。同 (date=ts[:10], source, message) 当日去重：同日重复
    add 返回既有条目（幂等），跨日允许再次出现。超出 MAX_ALERTS=200 时滚动
    保留最新。线程安全：读写均持锁；落盘用「临时文件 + os.replace」原子替换，
    避免并发/崩溃产生半截文件。
    """

    def __init__(self, path: str):
        """构造并加载既有告警（文件缺失 / 损坏 → 空列表，不抛）。"""
        self.path = path
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._load()

    @classmethod
    def init(cls, path: str) -> "Alerts":
        """按路径构造告警中心（等价 __init__，命名与任务简报一致）。"""
        return cls(path)

    # ---------------- 内部 ----------------
    def _load(self) -> None:
        """从 JSON 文件加载既有告警（容错：缺失/损坏/非数组均视为空）。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, list):
            return
        items = []
        for e in data:
            if isinstance(e, dict) and all(isinstance(e.get(k), str)
                                           for k in ("ts", "level", "source", "message")):
                items.append(e)
        self._items = items[-MAX_ALERTS:]  # 加载即夹到上限（防御外部写入超长）

    def _save(self) -> None:
        """原子落盘（临时文件 + os.replace）；失败仅 stderr 提示，不影响内存镜像。"""
        try:
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._items, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as exc:
            _warn(f"告警落盘失败：{self.path}（{exc}）")

    @staticmethod
    def _normalize_level(level) -> str:
        """级别归一化并校验：小写 + 别名（warn→warning）；非法抛中文 ValueError。"""
        s = str(level).strip().lower()
        s = ALERT_LEVEL_ALIASES.get(s, s)
        if s not in ALERT_LEVELS:
            raise ValueError(
                f"告警级别 {level!r} 非法；合法级别：{', '.join(ALERT_LEVELS)}"
            )
        return s

    # ---------------- 任务 API ----------------
    def add(self, level: str, source: str, message: str) -> dict:
        """新增告警。

        返回告警 dict {ts, level, source, message}（ts=ISO 本地时间）；
        同 (当日日期, source, message) 去重：重复投递返回既有条目、不新增。
        级别非法 / source、message 为空 → 中文 ValueError。
        """
        level = self._normalize_level(level)
        source = str(source).strip()
        message = str(message).strip()
        if not source:
            raise ValueError("告警 source 必须为非空字符串")
        if not message:
            raise ValueError("告警 message 必须为非空字符串")
        ts = _now_iso()
        with self._lock:
            for e in self._items:  # 当日去重：同 (date, source, message) 幂等
                if e["ts"][:10] == ts[:10] and e["source"] == source \
                        and e["message"] == message:
                    return e
            entry = {"ts": ts, "level": level, "source": source, "message": message}
            self._items.append(entry)
            if len(self._items) > MAX_ALERTS:  # 滚动：保留最新 200 条
                self._items = self._items[-MAX_ALERTS:]
            self._save()
            return entry

    def list(self, limit: int = 50) -> list:
        """告警列表（最新在前）；limit 缺省 50。"""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        if limit < 1:
            limit = 50
        with self._lock:
            return [dict(e) for e in reversed(self._items)][:limit]

    def count(self) -> int:
        """当前告警条数。"""
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        """清空全部告警并落盘（文件写 '[]'，保持文件存在）。"""
        with self._lock:
            self._items = []
            self._save()


# ---- 模块级告警单例（绑定 DATA_DIR，惰性创建） ----
_alerts_singleton: "Alerts | None" = None
_alerts_singleton_lock = threading.Lock()


def _get_alerts() -> Alerts:
    """模块级告警单例：首次调用时按 DATA_DIR 惰性创建（进程内复用）。"""
    global _alerts_singleton
    with _alerts_singleton_lock:
        if _alerts_singleton is None:
            _alerts_singleton = Alerts.init(str(config.DATA_DIR / "alerts.json"))
        return _alerts_singleton


def notify_alert(level: str, source: str, message: str) -> dict:
    """模块级告警助手：等价 _get_alerts().add（生产接线点，看门狗/调度侧零配置调用）。

    迁移自 test_ops.py 可执行规格（行为基线一致）：级别校验/当日去重/200 条滚动
    均由 Alerts.add 承担；不含 apikey 等敏感信息。
    """
    return _get_alerts().add(level, source, message)
