#!/usr/bin/env python3
"""paper_engine — 模拟盘时间轴编排引擎（任务C）

在 paper_core（纯逻辑）/ paper_db（持久层）/ mx_client（券商接口）之上，
按冻结契约时间轴（08:45 / 09:27 / 09:28 / 14:45 / 14:50 / 14:57 / 15:05）
编排模拟盘全流程：盘前均线准备 → 情绪信号冻结 → 目标决策 → 执行前风控 →
窗口下单 → 停止追价 → 收盘对账，并保证：

  - 信号不合格 → DATA_NOT_QUALIFIED：目标保持、不生成订单（原始 8 码失败原样入库）；
  - decision / intent 幂等：decision_id 与 intent_key 已存在时不覆盖、不重复下单；
  - T+1 可卖约束：卖出量 = max(0, min(-delta, available_to_sell_qty)) 整手向下取整；
  - 执行窗口外（< 14:50:00 或 > 14:56:30）不下新单；
  - 下单异常 → 事件记录原始 8 码 + 意图置 FAILED（可同 intent 重试）；
  - 收盘对账以券商回报为真值：fills 落库 / 组合快照 / 日终对账 / 决策重放校验。

设计约束：
  - 仅标准库；中文注释 / 文案；不接触 pybao/ 与 mcp 目录现有文件；
  - 本模块不含任何 apikey / 敏感信息（apikey 只在 mx_client 内三级解析，
    日志只打掩码，本模块永不回显、永不读取）；
  - 时间全部来自注入的 clock 可调用（测试可控时钟，生产 datetime.now）；
  - fetch_daily 由生产接线注入（连 stockdb HTTP），测试注入 mock；
  - 只读访问 paper_db 未公开的查询走其内部 _fetch_one/_fetch_all 工具
    （决策/意图/委托/组合快照等读取；不修改 paper_db 源码）。

MXClient 接口契约（与仓库 mx_client.py 实际签名对齐；引擎仅调用以下成员，
绝不触碰 apikey）：
  get_balance()            -> dict{"code":0,"data":{"availableCash":float,...}}
  get_positions()          -> dict{"code":0,"data":{"list":[{symbol/stockCode,
                              position/quantity, available_to_sell/...}]}}
  get_orders()             -> dict{"code":0,"data":{"list":[{orderId,stockCode,
                              type,quantity,filledQuantity,price,status,fillList}]}}
  place_order(action, symbol, quantity, price_type, price=None)
                           -> dict{"code":0,"data":{"orderId":...}}；失败抛
                              mx_client.MXError（.code 8 码 / .message）
  cancel_order(order_id, stock_code=None)   -> （引擎预留，本版本未调用）
  query_market(query)      -> dict{"code":0,"data":{price,bid1,ask1,halted,...}}
  MXError: 异常类，含 .code（8 错误码之一）/ .message（中文）。
  说明：price_type 传大写 "MARKET"/"LIMIT"（MARKET 即 useMarketPrice=True
  兜底语义，由 mx_client 写入 payload）；本引擎对信封响应做归一化读取，
  也兼容裸结构（测试直接注入简化 fake 亦可）。
  （mx_client.py 缺失时本模块提供同构兜底 MXError，保证可导入可测试。）

fetch_daily 注入契约：
  fetch_daily(code, start, end) -> dict{"bars":[{date,close},...]}（升序）或
  {"data":[...]}/{"klines":[...]}/裸 list；date 兼容 YYYYMMDD / YYYY-MM-DD，
  close 为收盘价 float。生产接线连 stockdb HTTP（研究层 kline 契约），
  测试注入 mock。

情绪文件契约（signal_dir/<trade_date>.json，UTF-8）：
  {"current_rank":float, "previous_rank":float|缺省, "metric_value":float,
   "history_count":int, "formal_usable":bool, "source_contract_version":str,
   "known_at":"YYYY-MM-DD HH:MM:SS"}
  校验（冻结契约）：current_rank 必填 ∈[0,1]；previous_rank 可缺（缺省从库
  派生，库中亦无 → 保守取 current_rank，首日不触发规则3）；metric_value 必填；
  history_count==60；formal_usable==true；source_contract_version ∈
  config["supported_signal_contracts"]（默认 ["emotion-v1"]）；
  known_at >= 当日 09:25。任一不满足 → add_event(DATA_NOT_QUALIFIED) 并返回
  DATA_NOT_QUALIFIED（目标保持、不生成订单）；文件缺失 → 原始码 NOT_PUBLISHED
  原样入库；解析失败 → INVALID_ARGUMENT 原样入库。

生命周期状态（与 paper_core 冻结 10 常量一致；另加任务C 瞬时状态 FAILED，
不在冻结 10 常量内，仅用于「下单异常可同 intent 重试」的意图流转标记）：
  SIGNAL_READY / DECIDED / EXECUTION_PENDING / SUBMITTED / PARTIALLY_FILLED /
  FILLED / UNFILLED / UNFILLED_AT_CUTOFF / RECONCILED / DATA_NOT_QUALIFIED / FAILED

自测（全 mock：内存 DB、fake mx、fake fetch_daily、可控 clock）：
    python paper_engine.py                 # 全量断言（输出见任务报告）
    python -m unittest paper_engine -v     # 同套件 unittest 发现模式
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import unittest

# === 依赖：paper_core（纯逻辑）/ paper_db（持久层） ===
from paper_core import (
    SYMBOL,
    DECISION_TIME,
    EXEC_WINDOW_START,
    EXEC_CUTOFF,
    STOP_CHASE,
    RECONCILE,
    MODEL_NAV_DEFAULT,
    EXECUTION_PENDING,
    SUBMITTED,
    PARTIALLY_FILLED,
    FILLED,
    UNFILLED,
    UNFILLED_AT_CUTOFF,
    RECONCILED,
    DATA_NOT_QUALIFIED,
    BUY_HALF,
    BUY_FULL,
    SELL_ALL,
    NO_ORDER,
    ERROR_INVALID_ARGUMENT,
    ERROR_NOT_PUBLISHED,
    ERROR_INTERNAL_ERROR,
    decide_target,
    state_transition,
    compute_delta,
    sell_quantity,
    order_intent_key,
    decision_id,
    replay_decision,
)
from paper_db import PaperDB  # noqa: F401  （仅类型标注；读取经 _fetch_one/_fetch_all）

# === mx_client 依赖（任务D 提供；缺失时用同构兜底，保证本模块可导入可测试） ===
try:  # pragma: no cover — 任务D 落地后走真实实现
    from mx_client import MXError
except ImportError:  # pragma: no cover

    class MXError(Exception):
        """兜底实现（mx_client 未落地前）：与 mx_client.MXError 同构（.code/.message）。

        code 为冻结 8 错误码之一（INVALID_ARGUMENT/NO_DATA/NOT_PUBLISHED/
        INVALID_SYMBOL/DEPENDENCY_UNAVAILABLE/PARTIAL_RESULT/RATE_LIMITED/
        INTERNAL_ERROR），message 为中文说明。
        """

        def __init__(self, code: str = ERROR_INTERNAL_ERROR, message: str = "MX 模拟盘接口错误"):
            super().__init__(f"[{code}] {message}")
            self.code = code
            self.message = message


# 任务C 瞬时状态：下单异常后的意图标记（可同 intent 重试；不在冻结 10 常量内）
FAILED = "FAILED"

# 默认配置（可被调用方覆盖后传入）
DEFAULT_CONFIG = {
    "model_nav": MODEL_NAV_DEFAULT,                      # 模型名义本金
    "trading_enabled": False,                            # 模拟盘总开关
    "paused": False,                                     # 暂停标记
    "supported_signal_contracts": ["emotion-v1"],        # 受支持的情绪契约版本
}

# 券商委托状态 → 生命周期状态映射（收盘对账用；任务D 按此对齐状态文案）
_BROKER_STATUS_MAP = {
    "filled": FILLED,
    "filled_all": FILLED,
    "complete": FILLED,
    "completed": FILLED,
    "done": FILLED,
    "partial_filled": PARTIALLY_FILLED,
    "partially_filled": PARTIALLY_FILLED,
    "partial": PARTIALLY_FILLED,
    "partial_fill": PARTIALLY_FILLED,
    "submitted": SUBMITTED,
    "pending": SUBMITTED,
    "new": SUBMITTED,
    "open": SUBMITTED,
    "working": SUBMITTED,
    "canceled": UNFILLED,
    "cancelled": UNFILLED,
    "rejected": UNFILLED,
    "expired": UNFILLED,
    "unfilled": UNFILLED,
}


class PaperEngine:
    """模拟盘时间轴编排引擎（单进程、单账户、单标的 159915）。"""

    def __init__(self, db, mx, fetch_daily, signal_dir,
                 clock=datetime.datetime.now, config=None):
        """构造引擎。

        参数：
          db:         PaperDB 实例（SQLite WAL 持久层）
          mx:         MXClient 实例（券商模拟盘接口，见模块 docstring 契约）
          fetch_daily: 可调用 fetch_daily(code, start, end) -> dict（K 线注入）
          signal_dir:  情绪信号文件目录（<trade_date>.json）
          clock:       可调用 -> datetime（测试可控时钟，生产 datetime.now）
          config:      覆盖默认配置的 dict（model_nav/trading_enabled/paused/
                       supported_signal_contracts/holidays）
        """
        if not callable(fetch_daily):
            raise ValueError("fetch_daily 必须为可调用对象")
        if not callable(clock):
            raise ValueError("clock 必须为可调用对象")
        if not isinstance(signal_dir, str) or not signal_dir.strip():
            raise ValueError("signal_dir 必须为非空路径字符串")
        cfg = dict(DEFAULT_CONFIG)
        if config:
            cfg.update(config)
        self.config = cfg
        self.db = db
        self.mx = mx
        self.fetch_daily = fetch_daily
        self.signal_dir = signal_dir
        self.clock = clock
        model_nav = float(cfg["model_nav"])
        if model_nav <= 0:
            raise ValueError(f"model_nav 必须为正数，收到 {cfg['model_nav']!r}")
        self.model_nav = model_nav
        supported = cfg.get("supported_signal_contracts") or []
        self._supported_contracts = set(str(x) for x in supported)
        # 节假日集合（YYYYMMDD 字符串）；引擎本身不依赖 mcp 日历，由生产注入
        self._holidays = set()
        for h in cfg.get("holidays") or ():
            d = h if isinstance(h, datetime.date) else self._parse_date(h)
            self._holidays.add(d.strftime("%Y%m%d"))
        # 14:45 参考价快照（执行窗口行情不可用时的回退价；内存 + 事件审计）
        self._ref_price_snapshot = None

    # ---------------- 内部工具 ----------------

    @staticmethod
    def _require_trade_date(trade_date) -> str:
        """规范化交易日（YYYYMMDD 8 位数字串）；非法抛中文 ValueError。"""
        s = str(trade_date).strip()
        if len(s) != 8 or not s.isdigit():
            raise ValueError(f"trade_date 非法：{trade_date!r}（要求 YYYYMMDD）")
        return s

    @staticmethod
    def _parse_date(s) -> datetime.date:
        """日期解析：兼容 YYYYMMDD / YYYY-MM-DD（带时间部分时取前 10 位）。"""
        s = str(s).strip()
        if len(s) >= 8 and s[:8].isdigit() and (len(s) == 8 or s[8] not in "0123456789"):
            if len(s) == 8:
                return datetime.datetime.strptime(s, "%Y%m%d").date()
            return datetime.datetime.strptime(s[:8], "%Y%m%d").date()
        return datetime.date.fromisoformat(s[:10])

    # ---- MX 响应归一化（兼容 mx_client 信封 {"code":0,"data":{...}} 与裸结构） ----
    @staticmethod
    def _first_num(d: dict, keys) -> float | None:
        """取 dict 中首个可转 float 的字段值；无返回 None。"""
        for k in keys:
            v = d.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    @classmethod
    def _quote_info(cls, quote) -> tuple:
        """提取行情 (最新价, 买一, 卖一)；兼容信封 data 嵌套；缺失为 None。"""
        if not isinstance(quote, dict):
            return (None, None, None)
        price = cls._first_num(quote, ("price", "latest", "last", "close"))
        bid1 = cls._first_num(quote, ("bid1", "bid_1"))
        ask1 = cls._first_num(quote, ("ask1", "ask_1"))
        data = quote.get("data")
        if isinstance(data, dict):
            price = price if price is not None \
                else cls._first_num(data, ("price", "latest", "last", "close"))
            bid1 = bid1 if bid1 is not None else cls._first_num(data, ("bid1", "bid_1"))
            ask1 = ask1 if ask1 is not None else cls._first_num(data, ("ask1", "ask_1"))
        return (price, bid1, ask1)

    @classmethod
    def _quote_price(cls, quote):
        """最新价（缺失/非法 None）。"""
        return cls._quote_info(quote)[0]

    @staticmethod
    def _is_halted(quote) -> bool:
        """是否停牌（兼容信封 data 嵌套）。"""
        if not isinstance(quote, dict):
            return False
        if quote.get("halted"):
            return True
        data = quote.get("data")
        return bool(isinstance(data, dict) and data.get("halted"))

    @staticmethod
    def _position_rows(positions) -> list:
        """归一化持仓响应为 [{"symbol","quantity","available_to_sell_qty"}, ...]。"""
        data = positions
        if isinstance(data, dict):
            data = data.get("data", data)
        if isinstance(data, dict):
            rows = data.get("list") or data.get("positions") or data.get("items") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            code = r.get("symbol") or r.get("stockCode") or r.get("stock_code")
            qty = (r.get("quantity") or r.get("position") or r.get("positionQty")
                   or r.get("qty") or 0)
            avail = (r.get("available_to_sell_qty") or r.get("available_to_sell")
                     or r.get("availableToSell") or r.get("availableSellQty")
                     or r.get("sellable_qty") or 0)
            out.append({"symbol": str(code).strip() if code is not None else None,
                        "quantity": int(qty), "available_to_sell_qty": int(avail)})
        return out

    @classmethod
    def _position_qty(cls, positions) -> int:
        """159915 持仓数量（符号字段缺失且仅一行时按单标的账户取该行）。"""
        rows = cls._position_rows(positions)
        for r in rows:
            if r["symbol"] == SYMBOL:
                return r["quantity"]
        if len(rows) == 1:
            return rows[0]["quantity"]
        return 0

    @classmethod
    def _available_to_sell(cls, positions) -> int:
        """159915 可卖数量（T+1；口径同 _position_qty）。"""
        rows = cls._position_rows(positions)
        for r in rows:
            if r["symbol"] == SYMBOL:
                return r["available_to_sell_qty"]
        if len(rows) == 1:
            return rows[0]["available_to_sell_qty"]
        return 0

    @staticmethod
    def _normalize_order(o) -> dict:
        """归一化券商委托为规范 dict（order_id/symbol/side/quantity/...）。"""
        if not isinstance(o, dict):
            return {}
        fills = o.get("fills") or o.get("fillList") or o.get("fill_list") or []
        if isinstance(fills, dict):
            fills = fills.get("list") or []
        return {
            "order_id": o.get("order_id") or o.get("orderId") or "",
            "symbol": o.get("symbol") or o.get("stockCode") or o.get("stock_code"),
            "side": str(o.get("side") or o.get("type") or o.get("action") or "").lower(),
            "quantity": int(o.get("quantity") or o.get("orderQuantity")
                            or o.get("order_qty") or 0),
            "filled_quantity": int(o.get("filled_quantity") or o.get("filledQty")
                                   or o.get("filledQuantity") or o.get("filled") or 0),
            "price": o.get("price"),
            "status": o.get("status") or o.get("orderStatus"),
            "fills": fills if isinstance(fills, list) else [],
        }

    @classmethod
    def _orders_rows(cls, orders) -> list:
        """归一化当日委托响应为规范 dict 列表（兼容信封）。"""
        data = orders
        if isinstance(data, dict):
            data = data.get("data", data)
        if isinstance(data, dict):
            rows = data.get("list") or data.get("orders") or data.get("items") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        return [cls._normalize_order(o) for o in rows]

    @staticmethod
    def _balance_cash(balance) -> float:
        """从资金响应取可用资金（兼容信封与常见字段名）。"""
        if isinstance(balance, dict):
            data = balance.get("data")
            src = data if isinstance(data, dict) else balance
            for k in ("cash", "availableCash", "available_cash", "available", "money"):
                v = src.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
        return 0.0

    @staticmethod
    def _extract_order_id(resp) -> str:
        """从 place_order 响应提取委托号（兼容信封 data 嵌套）。"""
        if isinstance(resp, str):
            return resp
        if isinstance(resp, dict):
            oid = resp.get("order_id") or resp.get("orderId")
            if not oid and isinstance(resp.get("data"), dict):
                oid = resp["data"].get("order_id") or resp["data"].get("orderId")
            if oid:
                return str(oid)
        return ""

    @staticmethod
    def _map_broker_status(bstatus) -> str:
        """券商委托状态文案 → 生命周期状态（未识别默认 SUBMITTED，保守不丢单）。"""
        s = str(bstatus or "").strip().lower()
        return _BROKER_STATUS_MAP.get(s, SUBMITTED)

    def _fetch_quote(self) -> dict:
        """查询 159915 行情（契约示例调用形式：query_market("159915 最新价")）。"""
        return self.mx.query_market(f"{SYMBOL} 最新价")

    def _latest_snapshot(self):
        """最近一条组合快照（本地账本）；无记录返回 None。"""
        return self.db._fetch_one(
            "SELECT * FROM portfolio_snapshots ORDER BY trade_date DESC LIMIT 1"
        )

    def _today_decision(self, trade_date):
        """读取当日决策（decision_id 幂等键查询）；无记录返回 None。"""
        return self.db._fetch_one(
            "SELECT * FROM strategy_decisions WHERE decision_id = ?",
            (decision_id(trade_date),),
        )

    def _actual_qty(self) -> int:
        """当前实际持仓：本地账本优先，券商持仓兜底（14:45 风控已校验二者一致）。"""
        local = self._latest_snapshot()
        if local is not None:
            return int(local["position_qty"])
        try:
            return self._position_qty(self.mx.get_positions())
        except Exception:
            return 0

    def _broker_available_to_sell(self) -> int:
        """券商侧 159915 可卖数量（T+1 上限）。"""
        try:
            return self._available_to_sell(self.mx.get_positions())
        except Exception:
            return 0

    def _normalize_bars(self, raw):
        """规范化 fetch_daily 返回为 [{"date": date, "close": float}, ...]（升序）。

        接受 dict{"bars"|"data"|"klines"|"list"|"items": [...]} 或裸 list；
        任意一行缺 date/close 或无法解析 → 返回 None（数据不合格）。
        """
        if isinstance(raw, dict):
            bars = None
            for key in ("bars", "data", "klines", "list", "items"):
                if isinstance(raw.get(key), list):
                    bars = raw[key]
                    break
            if bars is None:
                return None
        elif isinstance(raw, list):
            bars = raw
        else:
            return None
        out = []
        for b in bars:
            if not isinstance(b, dict):
                return None
            d = b.get("date", b.get("trade_date", b.get("datetime", b.get("day"))))
            c = b.get("close", b.get("close_price"))
            if d is None or c is None:
                return None
            try:
                out.append({"date": self._parse_date(d), "close": float(c)})
            except (ValueError, TypeError):
                return None
        if not out:
            return None
        out.sort(key=lambda x: x["date"])
        return out

    def _expected_last_trading_day(self, trade_date) -> datetime.date:
        """T 前最近一个有效交易日（从 T-1 起回退，跳过周末与节假日）。"""
        d = datetime.datetime.strptime(trade_date, "%Y%m%d").date() \
            - datetime.timedelta(days=1)
        while d.weekday() >= 5 or d.strftime("%Y%m%d") in self._holidays:
            d -= datetime.timedelta(days=1)
        return d

    def _is_contiguous_trading(self, prev: datetime.date, nxt: datetime.date) -> bool:
        """相邻两根 K 线之间不得夹有有效交易日（周末/节假日允许跳过）。"""
        d = prev + datetime.timedelta(days=1)
        while d < nxt:
            if d.weekday() < 5 and d.strftime("%Y%m%d") not in self._holidays:
                return False
            d += datetime.timedelta(days=1)
        return True

    # ---------------- 步骤方法（可单独调用） ----------------

    def step_prepare_trend(self, trade_date: str) -> bool:
        """盘前均线准备（时间轴 08:45）。

        fetch_daily("159915", T-60 自然日, T-1)；校验：行数 >= 20、最后一行日期
        == 最近有效交易日（T-1 或回退）、行内日期连续有效交易日（跳过周末/
        节假日）；任一不满足 → add_event(DATA_NOT_QUALIFIED) + 返回 False；
        满足 → 计算 MA5/MA10/MA20（收盘简单均线）→ insert_trend + 返回 True。
        """
        td = self._require_trade_date(trade_date)
        now = self.clock()
        t_date = datetime.datetime.strptime(td, "%Y%m%d").date()
        end = t_date - datetime.timedelta(days=1)
        start = t_date - datetime.timedelta(days=60)
        try:
            raw = self.fetch_daily(SYMBOL, start, end)
        except Exception as exc:  # 注入层异常 → 内部错误事件
            self.db.add_event(event=ERROR_INTERNAL_ERROR, level="ERROR",
                              trade_date=td, timepoint="08:45",
                              detail=f"fetch_daily 异常：{exc}")
            return False
        bars = self._normalize_bars(raw)
        if bars is None:
            self.db.add_event(event=DATA_NOT_QUALIFIED, level="ERROR",
                              trade_date=td, timepoint="08:45",
                              detail="fetch_daily 返回格式无法识别（缺 date/close 或非列表）")
            return False
        if len(bars) < 20:
            self.db.add_event(event=DATA_NOT_QUALIFIED, level="ERROR",
                              trade_date=td, timepoint="08:45",
                              detail=f"K 线行数不足：{len(bars)} < 20 → DATA_NOT_QUALIFIED")
            return False
        expected = self._expected_last_trading_day(td)
        last_date = bars[-1]["date"]
        if last_date != expected:
            self.db.add_event(
                event=DATA_NOT_QUALIFIED, level="ERROR", trade_date=td,
                timepoint="08:45",
                detail=f"最后一行日期 {last_date.strftime('%Y%m%d')} != 最近交易日 "
                       f"{expected.strftime('%Y%m%d')}（T-1 或回退）→ DATA_NOT_QUALIFIED")
            return False
        for i in range(len(bars) - 1):
            if not self._is_contiguous_trading(bars[i]["date"], bars[i + 1]["date"]):
                self.db.add_event(
                    event=DATA_NOT_QUALIFIED, level="ERROR", trade_date=td,
                    timepoint="08:45",
                    detail=f"K 线日期不连续：{bars[i]['date'].strftime('%Y%m%d')} → "
                           f"{bars[i + 1]['date'].strftime('%Y%m%d')} 之间夹有有效交易日"
                           " → DATA_NOT_QUALIFIED")
                return False
        closes = [b["close"] for b in bars]
        ma5 = sum(closes[-5:]) / 5.0
        ma10 = sum(closes[-10:]) / 10.0
        ma20 = sum(closes[-20:]) / 20.0
        known_at = now.strftime("%Y-%m-%d %H:%M:%S")
        self.db.insert_trend(trade_date=td, ma5=ma5, ma10=ma10, ma20=ma20,
                             bar_count=len(bars),
                             last_bar_date=last_date.strftime("%Y%m%d"),
                             known_at=known_at)
        self.db.add_event(event="TREND_READY", level="INFO", trade_date=td,
                          timepoint="08:45",
                          detail=f"趋势就绪：{len(bars)} 根 K 线，last="
                                 f"{last_date.strftime('%Y%m%d')}，"
                                 f"ma5/10/20={ma5:.4f}/{ma10:.4f}/{ma20:.4f}")
        return True

    def step_freeze_signal(self, trade_date: str):
        """情绪信号冻结（时间轴 09:27）。

        读取 signal_dir/<trade_date>.json 并按冻结契约校验；previous_rank 缺省
        时从库 get_latest_qualified_signal 派生（库中亦无 → 保守取当日，首日
        不触发规则3）；任一不满足 → 原始 8 码失败原样入库 + add_event
        (DATA_NOT_QUALIFIED) + 返回 DATA_NOT_QUALIFIED（不抛）；成功 →
        insert_signal + 返回信号 dict。
        """
        td = self._require_trade_date(trade_date)
        path = os.path.join(self.signal_dir, f"{td}.json")
        if not os.path.isfile(path):
            # 原始 8 码失败原样入库（NOT_PUBLISHED），再记 DATA_NOT_QUALIFIED
            self.db.add_event(event=ERROR_NOT_PUBLISHED, level="ERROR",
                              trade_date=td, timepoint=DECISION_TIME,
                              detail=f"情绪文件未发布：{path}")
            self.db.add_event(event=DATA_NOT_QUALIFIED, level="WARN",
                              trade_date=td, timepoint=DECISION_TIME,
                              detail="信号文件缺失 → DATA_NOT_QUALIFIED，目标保持、不生成订单")
            return DATA_NOT_QUALIFIED
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as exc:
            self.db.add_event(event=ERROR_INVALID_ARGUMENT, level="ERROR",
                              trade_date=td, timepoint=DECISION_TIME,
                              detail=f"情绪文件读取/解析失败（原始码 INVALID_ARGUMENT）：{exc}")
            self.db.add_event(event=DATA_NOT_QUALIFIED, level="WARN",
                              trade_date=td, timepoint=DECISION_TIME,
                              detail="信号文件解析失败 → DATA_NOT_QUALIFIED")
            return DATA_NOT_QUALIFIED
        if not isinstance(raw, dict):
            self.db.add_event(event=ERROR_INVALID_ARGUMENT, level="ERROR",
                              trade_date=td, timepoint=DECISION_TIME,
                              detail="情绪文件根节点必须为 JSON 对象")
            self.db.add_event(event=DATA_NOT_QUALIFIED, level="WARN",
                              trade_date=td, timepoint=DECISION_TIME,
                              detail="信号文件格式非法 → DATA_NOT_QUALIFIED")
            return DATA_NOT_QUALIFIED

        errors = []
        # current_rank 必填（0~1）
        try:
            cr = float(raw.get("current_rank"))
        except (TypeError, ValueError):
            cr = None
        if cr is None or not (0.0 <= cr <= 1.0):
            errors.append(f"current_rank 非法：{raw.get('current_rank')!r}（必填，要求 0~1）")
        # metric_value 必填
        try:
            mv = float(raw.get("metric_value"))
        except (TypeError, ValueError):
            mv = None
        if mv is None:
            errors.append(f"metric_value 缺失或非法：{raw.get('metric_value')!r}")
        # history_count == 60
        hc = raw.get("history_count")
        try:
            hc_ok = (int(hc) == 60)
        except (TypeError, ValueError):
            hc_ok = False
        if not hc_ok:
            errors.append(f"history_count 必须 == 60，收到 {hc!r}")
        # formal_usable == true
        if raw.get("formal_usable") is not True:
            errors.append(f"formal_usable 必须为 true，收到 {raw.get('formal_usable')!r}")
        # source_contract_version 受支持
        scv = raw.get("source_contract_version")
        if scv not in self._supported_contracts:
            errors.append(f"source_contract_version {scv!r} 不受支持"
                          f"（支持：{sorted(self._supported_contracts)}）")
        # known_at >= 当日 09:25
        ka = raw.get("known_at")
        try:
            ka_dt = datetime.datetime.strptime(str(ka).strip(), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            ka_dt = None
        if ka_dt is None:
            errors.append(f"known_at 非法：{ka!r}（要求 YYYY-MM-DD HH:MM:SS）")
        else:
            td_dt = datetime.datetime.strptime(td, "%Y%m%d")
            if ka_dt < td_dt.replace(hour=9, minute=25, second=0):
                errors.append(f"known_at {ka_dt} 早于当日 09:25")
        if errors:
            self.db.add_event(event=DATA_NOT_QUALIFIED, level="ERROR",
                              trade_date=td, timepoint=DECISION_TIME,
                              detail="；".join(errors) + " → DATA_NOT_QUALIFIED，目标保持、不生成订单")
            return DATA_NOT_QUALIFIED

        # previous_rank：文件缺省 → 从库派生；库中亦无 → 保守取 current_rank
        pr = raw.get("previous_rank")
        if pr is None:
            prev = self.db.get_latest_qualified_signal(td)
            pr = prev["current_rank"] if prev else cr
        try:
            pr = float(pr)
        except (TypeError, ValueError):
            pr = None
        if pr is None or not (0.0 <= pr <= 1.0):
            self.db.add_event(event=DATA_NOT_QUALIFIED, level="ERROR",
                              trade_date=td, timepoint=DECISION_TIME,
                              detail=f"previous_rank 非法：{raw.get('previous_rank')!r}"
                                     " → DATA_NOT_QUALIFIED")
            return DATA_NOT_QUALIFIED

        self.db.insert_signal(trade_date=td, current_rank=cr, previous_rank=pr,
                              metric_value=mv, history_count=60, formal_usable=1,
                              source_contract_version=scv, known_at=ka)
        self.db.add_event(event="SIGNAL_FROZEN", level="INFO", trade_date=td,
                          timepoint=DECISION_TIME,
                          detail=f"信号冻结：rank={cr} prev={pr} version={scv}")
        return {"trade_date": td, "current_rank": cr, "previous_rank": pr,
                "metric_value": mv, "history_count": 60, "formal_usable": True,
                "source_contract_version": scv, "known_at": ka}

    def step_decide(self, trade_date: str):
        """目标仓位决策（时间轴 09:27，紧跟信号冻结）。

        trend + signal + 上一决策 desired_target（无历史 → 0.0）→ decide_target
        → create_decision（重复 → add_event 告警，禁止覆盖）→ 返回决策 dict；
        依赖缺失（趋势/信号）→ DATA_NOT_QUALIFIED。
        """
        td = self._require_trade_date(trade_date)
        trend = self.db.get_trend(td)
        signal = self.db._fetch_one(
            "SELECT * FROM signal_snapshots WHERE trade_date = ?", (td,))
        if trend is None or signal is None:
            self.db.add_event(event=DATA_NOT_QUALIFIED, level="ERROR",
                              trade_date=td, timepoint=DECISION_TIME,
                              detail=f"趋势/信号快照缺失（trend={'有' if trend else '无'}，"
                                     f"signal={'有' if signal else '无'}）→ 无法决策，"
                                     "目标保持、不生成订单")
            return DATA_NOT_QUALIFIED
        prev_dec = self.db._fetch_one(
            "SELECT * FROM strategy_decisions WHERE trade_date < ? "
            "ORDER BY trade_date DESC LIMIT 1", (td,))
        previous_target = float(prev_dec["desired_target"]) if prev_dec else 0.0
        # 防御：previous_rank 为 None（外部直插信号）时取 current_rank
        prev_rank = signal["previous_rank"]
        if prev_rank is None:
            prev_rank = signal["current_rank"]
        desired, reason = decide_target(
            current_rank=signal["current_rank"], previous_rank=prev_rank,
            ma5=trend["ma5"], ma10=trend["ma10"], ma20=trend["ma20"],
            current_target=previous_target)
        did = decision_id(td)
        created = self.db.create_decision(
            decision_id=did, trade_date=td,
            previous_rank=prev_rank, current_rank=signal["current_rank"],
            ma5=trend["ma5"], ma10=trend["ma10"], ma20=trend["ma20"],
            previous_target=previous_target, desired_target=desired,
            reason_code=reason, signal_known_at=signal["known_at"])
        if not created:
            self.db.add_event(event="DECISION_DUPLICATE", level="WARN",
                              trade_date=td, timepoint=DECISION_TIME,
                              detail=f"决策已存在，禁止覆盖：{did}")
            return self._today_decision(td)
        self.db.add_event(event="DECISION_CREATED", level="INFO",
                          trade_date=td, timepoint=DECISION_TIME,
                          detail=f"决策落库：{previous_target} → {desired}（{reason}）")
        return self._today_decision(td)

    def _confirm_decision(self, trade_date: str):
        """09:28 决策确认时点（仅事件审计，不产生业务写入）。

        当日决策存在 → DECISION_CONFIRMED；缺失（09:27 未决策）→ DECISION_MISSING 告警。
        """
        td = self._require_trade_date(trade_date)
        row = self._today_decision(td)
        if row is not None:
            self.db.add_event(event="DECISION_CONFIRMED", level="INFO",
                              trade_date=td, timepoint="09:28",
                              detail=f"决策确认：desired={row['desired_target']}"
                                     f"（{row['reason_code']}）")
            return row
        self.db.add_event(event="DECISION_MISSING", level="WARN",
                          trade_date=td, timepoint="09:28",
                          detail="09:27 未形成决策（信号不合格或依赖缺失），目标保持、不生成订单")
        return None

    def step_pre_exec_risk(self):
        """执行前风控（时间轴 14:45）。

        调用 mx.get_balance/get_positions/get_orders + query_market 检查：
        交易开关/暂停/账户连通/本地账本与券商 159915 数量一致/资金够（买入）/
        available_to_sell_qty 够（卖出）/无未处理同策略订单/无停牌行情异常。
        同时保存 14:45 参考价快照（self._ref_price_snapshot + 事件审计）。

        返回：(ok: bool, report: dict)。
        """
        now = self.clock()
        td = now.strftime("%Y%m%d")
        report = {"ok": False, "trade_date": td, "checks": [], "quote": None,
                  "balance": None, "positions": None, "broker_qty": 0,
                  "available_to_sell_qty": 0}

        def add_check(name, ok, detail):
            report["checks"].append({"name": name, "ok": ok, "detail": detail})

        if not self.config.get("trading_enabled"):
            add_check("trading_enabled", False, "模拟盘未启用 trading_enabled=False")
            self.db.add_event(event="RISK_BLOCKED", level="WARN", trade_date=td,
                              timepoint="14:45", detail="模拟盘未启用，执行前风控不放行")
            return (False, report)
        if self.config.get("paused"):
            add_check("paused", False, "模拟盘已暂停 paused=True")
            self.db.add_event(event="RISK_BLOCKED", level="WARN", trade_date=td,
                              timepoint="14:45", detail="模拟盘已暂停，执行前风控不放行")
            return (False, report)
        try:
            balance = self.mx.get_balance()
            positions = self.mx.get_positions()
            orders = self.mx.get_orders()
            quote = self._fetch_quote()
        except MXError as exc:
            code = getattr(exc, "code", ERROR_INTERNAL_ERROR)
            msg = getattr(exc, "message", str(exc))
            self.db.add_event(event=code, level="ERROR", trade_date=td,
                              timepoint="14:45",
                              detail=f"账户接口失败（原始码 {code}）：{msg}")
            add_check("account", False, f"账户连通失败 [{code}]")
            return (False, report)
        report.update(balance=balance, positions=positions, quote=quote)
        add_check("account", True, "账户接口连通（balance/positions/orders/quote）")

        broker_qty = self._position_qty(positions)
        avail = self._available_to_sell(positions)
        report["broker_qty"] = broker_qty
        report["available_to_sell_qty"] = avail

        local = self._latest_snapshot()
        if local is not None and int(local["position_qty"]) != broker_qty:
            add_check("ledger_match", False,
                      f"本地账本 {local['position_qty']} ≠ 券商 {broker_qty}")
            self.db.add_event(event="RISK_BLOCKED", level="ERROR", trade_date=td,
                              timepoint="14:45",
                              detail="本地账本与券商 159915 持仓数量不一致，不放行")
            return (False, report)
        add_check("ledger_match", True,
                  "本地账本与券商持仓一致" if local is not None
                  else "无本地账本（首日，跳过一致性比对）")

        price = self._quote_price(quote)
        if price is None or price <= 0 or self._is_halted(quote):
            self.db.add_event(event=ERROR_INTERNAL_ERROR, level="ERROR",
                              trade_date=td, timepoint="14:45",
                              detail=f"行情异常/停牌：price={price!r} halted="
                                     f"{self._is_halted(quote)} → INTERNAL_ERROR 不放行")
            add_check("quote", False, "行情异常或停牌")
            return (False, report)
        self._ref_price_snapshot = price
        self.db.add_event(event="REF_PRICE_SNAPSHOT", level="INFO", trade_date=td,
                          timepoint="14:45",
                          detail=f"参考价快照：{price}（quote 最新价，供执行窗口回退）")
        add_check("quote", True, f"行情正常，最新价 {price}")

        decision = self._today_decision(td)
        action_name = None
        if decision is not None:
            prev = float(decision["previous_target"]) \
                if decision["previous_target"] is not None else 0.0
            desired = decision["desired_target"]
            if desired != prev:
                action_name = state_transition(prev, desired)
                target, delta = compute_delta(self.model_nav, desired, price, broker_qty)
                if action_name in (BUY_HALF, BUY_FULL):
                    need_cash = delta * price
                    cash = self._balance_cash(balance)
                    if cash < need_cash:
                        self.db.add_event(event="RISK_BLOCKED", level="ERROR",
                                          trade_date=td, timepoint="14:45",
                                          detail=f"资金不足：需 {need_cash:.2f}，"
                                                 f"可用 {cash:.2f}")
                        add_check("cash", False, f"资金不足（需 {need_cash:.2f} / 有 {cash:.2f}）")
                        return (False, report)
                    add_check("cash", True, f"资金充足（需 {need_cash:.2f} / 有 {cash:.2f}）")
                elif action_name == SELL_ALL:
                    sell_qty = sell_quantity(delta, avail)
                    if avail <= 0:
                        self.db.add_event(event="RISK_BLOCKED", level="ERROR",
                                          trade_date=td, timepoint="14:45",
                                          detail="需要卖出但 available_to_sell_qty=0"
                                                 "（T+1 不可卖），不放行")
                        add_check("sell_available", False, "可卖数量为 0（T+1）")
                        return (False, report)
                    add_check("sell_available", True,
                              f"可卖 {avail} 股（T+1 上限，本次至多卖 {sell_qty}）")

        open_intents = self.db.get_open_intents(td)
        if open_intents:
            self.db.add_event(event="RISK_BLOCKED", level="ERROR", trade_date=td,
                              timepoint="14:45",
                              detail=f"存在未处理同策略订单：{len(open_intents)} 条（"
                                     f"{', '.join(i['status'] for i in open_intents)}）")
            add_check("open_intents", False,
                      "存在未处理订单（SUBMITTED/EXECUTION_PENDING/PARTIALLY_FILLED）")
            return (False, report)
        add_check("open_intents", True, "无未处理订单")

        report["ok"] = True
        self.db.add_event(event="RISK_OK", level="INFO", trade_date=td,
                          timepoint="14:45",
                          detail=f"执行前风控通过（action={action_name}，持仓="
                                 f"{broker_qty}，可卖={avail}）")
        return (True, report)

    def step_execute(self, now):
        """执行窗口下单（时间轴 14:50；仅 14:50:00 <= now <= 14:56:30）。

        读当日决策（desired != previous_target 才动作）；delta 用 model_nav、
        ref_price（quote 最新价或 14:45 快照价）；create_intent 幂等去重；
        盘口可用 → 限价（买=卖一、卖=买一），限价非法 → INTERNAL_ERROR 不重发；
        盘口不可用 → useMarketPrice=True 市价兜底（intent 备注见事件）；
        place_order → upsert_broker_order(SUBMITTED)；异常 → 事件记原始码 +
        意图 FAILED 可同 intent 重试。截止后不发新单。

        返回：(ok: bool, summary)。
        """
        t = now.strftime("%H:%M:%S")
        td = now.strftime("%Y%m%d")
        if not (EXEC_WINDOW_START <= t <= EXEC_CUTOFF):
            self.db.add_event(event="EXEC_SKIPPED", level="INFO", trade_date=td,
                              timepoint="14:50",
                              detail=f"当前 {t} 不在执行窗口"
                                     f"（{EXEC_WINDOW_START}~{EXEC_CUTOFF}），不发新单")
            return (False, f"执行窗口外：{t}")
        if not self.config.get("trading_enabled"):
            return (False, "模拟盘未启用")
        if self.config.get("paused"):
            return (False, "模拟盘已暂停")
        decision = self._today_decision(td)
        if decision is None:
            self.db.add_event(event="EXEC_SKIPPED", level="WARN", trade_date=td,
                              timepoint="14:50",
                              detail="今日无决策（信号不合格或未决策），不下单")
            return (False, "无决策")
        prev = float(decision["previous_target"]) \
            if decision["previous_target"] is not None else 0.0
        desired = decision["desired_target"]
        if desired == prev:
            self.db.add_event(event="NO_ORDER", level="INFO", trade_date=td,
                              timepoint="14:50",
                              detail=f"目标不变（{desired}），不下单")
            return (False, "目标不变，无需下单")
        action = state_transition(prev, desired)

        actual = self._actual_qty()
        avail = self._broker_available_to_sell()

        # 参考价：quote 最新价，失败/缺失回退 14:45 快照价
        quote = None
        try:
            quote = self._fetch_quote()
        except MXError as exc:
            code = getattr(exc, "code", ERROR_INTERNAL_ERROR)
            self.db.add_event(event=code, level="WARN", trade_date=td,
                              timepoint="14:50",
                              detail=f"下单前行情查询失败（原始码 {code}）："
                                     f"{getattr(exc, 'message', str(exc))}（回退 14:45 快照价）")
        price = self._quote_price(quote)
        if price is None or price <= 0:
            price = self._ref_price_snapshot
        if price is None or price <= 0:
            self.db.add_event(event=ERROR_INTERNAL_ERROR, level="ERROR",
                              trade_date=td, timepoint="14:50",
                              detail="无参考价（行情与 14:45 快照均不可用），不下单")
            return (False, "无参考价")

        target, delta = compute_delta(self.model_nav, desired, price, actual)
        if delta == 0:
            return (False, "无需调仓")
        if delta < 0:
            qty = sell_quantity(delta, avail)
            if qty <= 0:
                self.db.add_event(event="EXEC_SKIPPED", level="WARN",
                                  trade_date=td, timepoint="14:50",
                                  detail=f"需卖出但可卖数量不足（available_to_sell_qty="
                                         f"{avail}），不下单")
                return (False, "可卖数量不足（T+1）")
        else:
            qty = delta

        # 价格模式：盘口可用 → 限价；盘口不可用 → 市价兜底（useMarketPrice=True 语义）
        price_type, px = "limit", None
        _p, bid1, ask1 = self._quote_info(quote)
        if bid1 is not None and ask1 is not None:
            try:
                px = float(ask1) if action in (BUY_HALF, BUY_FULL) else float(bid1)
            except (TypeError, ValueError):
                px = None
            if px is None or not (px > 0):
                self.db.add_event(event=ERROR_INTERNAL_ERROR, level="ERROR",
                                  trade_date=td, timepoint="14:50",
                                  detail=f"限价非法（{px}）→ INTERNAL_ERROR，不重发")
                return (False, "限价非法，不重发")
        else:
            price_type = "market"
            self.db.add_event(event="MARKET_FALLBACK", level="INFO",
                              trade_date=td, timepoint="14:50",
                              detail="盘口无买一/卖一 → useMarketPrice=True 市价单兜底"
                                     "（intent 备注见本事件）")

        side = "buy" if action in (BUY_HALF, BUY_FULL) else "sell"
        ik = order_intent_key(td, desired)
        existing = self.db._fetch_one(
            "SELECT * FROM order_intents WHERE intent_key = ?", (ik,))
        if existing is None:
            self.db.create_intent(intent_key=ik, decision_id=decision_id(td),
                                  trade_date=td, symbol=SYMBOL,
                                  desired_target=desired, action=action,
                                  target_qty=target, delta_qty=delta,
                                  price_type=price_type)
            self.db.add_event(event="INTENT_CREATED", level="INFO", trade_date=td,
                              timepoint="14:50",
                              detail=f"意图创建：{ik} action={action} "
                                     f"target={target} delta={delta}（{price_type}）")
        else:
            st = existing["status"]
            if st == SUBMITTED:
                self.db.add_event(event="EXEC_DEDUP", level="INFO", trade_date=td,
                                  timepoint="14:50",
                                  detail=f"意图已提交（{ik}），幂等不重发")
                return (True, {"intent_key": ik, "dedup": True})
            if st in (FILLED, PARTIALLY_FILLED, UNFILLED,
                      UNFILLED_AT_CUTOFF, RECONCILED):
                return (False, f"意图已终态：{st}")
            if st == FAILED:
                self.db.transition_intent_status(ik, FAILED, EXECUTION_PENDING)
                self.db.add_event(event="EXEC_RETRY", level="INFO", trade_date=td,
                                  timepoint="14:50",
                                  detail=f"FAILED → EXECUTION_PENDING，同 intent 重试：{ik}")
            # st == EXECUTION_PENDING（或刚重置）→ 走提交
        return self._submit_order(ik, side, qty, price_type, px, td, now)

    def _submit_order(self, ik, side, qty, price_type, px, td, now):
        """提交委托：place_order → upsert_broker_order(SUBMITTED) + 意图转 SUBMITTED。

        调用约定（mx_client.MXClient.place_order(action, symbol, quantity,
        price_type, price)）：action ∈ {buy,sell}；price_type 传大写
        MARKET/LIMIT（MARKET 即 useMarketPrice=True 兜底语义）。
        异常 → 事件记原始 8 码 + 意图 EXECUTION_PENDING→FAILED（可同 intent 重试）。
        """
        try:
            order = self.mx.place_order(side, SYMBOL, qty,
                                        price_type.upper(), px)
        except MXError as exc:
            code = getattr(exc, "code", ERROR_INTERNAL_ERROR)
            msg = getattr(exc, "message", str(exc))
            self.db.add_event(event=code, level="ERROR", trade_date=td,
                              timepoint="14:50",
                              detail=f"下单失败（原始码 {code}）：{msg}；"
                                     "意图置 FAILED，可同 intent 重试")
            self.db.transition_intent_status(ik, EXECUTION_PENDING, FAILED)
            return (False, f"下单失败[{code}]：{msg}")
        except Exception as exc:  # 非 MXError 异常 → INTERNAL_ERROR
            self.db.add_event(event=ERROR_INTERNAL_ERROR, level="ERROR",
                              trade_date=td, timepoint="14:50",
                              detail=f"下单异常：{exc}；意图置 FAILED，可同 intent 重试")
            self.db.transition_intent_status(ik, EXECUTION_PENDING, FAILED)
            return (False, f"下单异常[{ERROR_INTERNAL_ERROR}]：{exc}")

        order_id = self._extract_order_id(order)
        if not order_id:
            order_id = f"MX-{td}-{now.strftime('%H%M%S')}"
        try:
            raw_json = json.dumps(order, ensure_ascii=False) \
                if not isinstance(order, str) else order
        except (TypeError, ValueError):
            raw_json = None
        self.db.upsert_broker_order(order_id=order_id, intent_key=ik,
                                    trade_date=td, symbol=SYMBOL, action=side,
                                    quantity=qty, price_type=price_type, price=px,
                                    status=SUBMITTED,
                                    submitted_at=now.strftime("%Y-%m-%d %H:%M:%S"),
                                    raw_response=raw_json)
        self.db.transition_intent_status(ik, EXECUTION_PENDING, SUBMITTED)
        self.db.add_event(event="ORDER_SUBMITTED", level="INFO", trade_date=td,
                          timepoint="14:50",
                          detail=f"委托已提交：{order_id} {side} {qty} 股（{price_type}）")
        return (True, {"intent_key": ik, "order_id": order_id, "side": side,
                       "quantity": qty, "price_type": price_type})

    def step_stop_chase(self):
        """停止追价（时间轴 14:57）。

        调 mx.get_orders 核对券商回报；今日 SUBMITTED 且未成交的意图 →
        UNFILLED_AT_CUTOFF（事件注明）；EXECUTION_PENDING（窗口内未提交）→
        同样标记；PARTIALLY_FILLED 留给收盘对账。
        """
        now = self.clock()
        td = now.strftime("%Y%m%d")
        try:
            orders = self.mx.get_orders()
        except MXError as exc:
            code = getattr(exc, "code", ERROR_INTERNAL_ERROR)
            self.db.add_event(event=code, level="ERROR", trade_date=td,
                              timepoint=STOP_CHASE,
                              detail=f"停止追价时查询委托失败（原始码 {code}）："
                                     f"{getattr(exc, 'message', str(exc))}")
            return (False, f"查询委托失败[{code}]")
        broker = {}
        for o in self._orders_rows(orders):
            if o.get("symbol") == SYMBOL and o.get("order_id"):
                broker[o["order_id"]] = o
        marked = 0
        for intent in self.db.get_open_intents(td):
            ik = intent["intent_key"]
            st = intent["status"]
            if st == EXECUTION_PENDING:
                self.db.transition_intent_status(ik, EXECUTION_PENDING,
                                                 UNFILLED_AT_CUTOFF)
                self.db.add_event(event="UNFILLED_AT_CUTOFF", level="WARN",
                                  trade_date=td, timepoint=STOP_CHASE,
                                  detail=f"意图 {ik} 窗口内未提交，截止仍完全未成交")
                marked += 1
            elif st == SUBMITTED:
                row = self.db._fetch_one(
                    "SELECT * FROM broker_orders WHERE intent_key = ?", (ik,))
                bo = broker.get(row["order_id"]) if row else None
                filled = int(bo.get("filled_quantity") or 0) if bo else 0
                if filled == 0:
                    self.db.transition_intent_status(ik, SUBMITTED,
                                                     UNFILLED_AT_CUTOFF)
                    self.db.add_event(event="UNFILLED_AT_CUTOFF", level="WARN",
                                      trade_date=td, timepoint=STOP_CHASE,
                                      detail=f"意图 {ik} 截止未成交"
                                             f"（委托 {row['order_id'] if row else '?'}）")
                    marked += 1
                # filled > 0 → 留给收盘对账
            # PARTIALLY_FILLED → 留给收盘对账
        self.db.add_event(event="STOP_CHASE_DONE", level="INFO", trade_date=td,
                          timepoint=STOP_CHASE,
                          detail=f"停止追价完成，标记 {marked} 条 UNFILLED_AT_CUTOFF")
        return (True, {"trade_date": td, "marked_unfilled": marked})

    def step_reconcile(self):
        """收盘对账（时间轴 15:05）。

        以 mx.get_orders/get_positions 回报为真值：fills 落库（fill_id 幂等）、
        broker_orders 状态修订、portfolio_snapshots 更新（nav/position_qty/
        available_to_sell_qty）、daily_reconciliations（target vs actual 偏差）、
        intents → FILLED/PARTIALLY_FILLED/UNFILLED → 终态 RECONCILED；
        重放校验：当日 decision 字段重放 decide_target 应同 desired，
        不一致 → 事件告警。
        """
        now = self.clock()
        td = now.strftime("%Y%m%d")
        try:
            orders = self.mx.get_orders()
            positions = self.mx.get_positions()
            balance = self.mx.get_balance()
            quote = self._fetch_quote()
        except MXError as exc:
            code = getattr(exc, "code", ERROR_INTERNAL_ERROR)
            self.db.add_event(event=code, level="ERROR", trade_date=td,
                              timepoint=RECONCILE,
                              detail=f"收盘对账接口失败（原始码 {code}）："
                                     f"{getattr(exc, 'message', str(exc))}")
            return (False, f"对账接口失败[{code}]")
        orders = self._orders_rows(orders)

        # 1) 券商委托状态 + 成交回报落库（fill_id 幂等去重）
        fills_added = 0
        for o in orders:
            if o.get("symbol") != SYMBOL or not o.get("order_id"):
                continue
            oid = o["order_id"]
            bstatus = self._map_broker_status(o.get("status"))
            row = self.db._fetch_one(
                "SELECT * FROM broker_orders WHERE order_id = ?", (oid,))
            if row is not None:
                try:
                    raw_json = json.dumps(o, ensure_ascii=False)
                except (TypeError, ValueError):
                    raw_json = None
                self.db.upsert_broker_order(
                    order_id=oid, intent_key=row["intent_key"], trade_date=td,
                    symbol=SYMBOL, action=row["action"], quantity=row["quantity"],
                    price_type=row["price_type"], price=o.get("price"),
                    status=bstatus, submitted_at=row["submitted_at"],
                    raw_response=raw_json)
            for f in (o.get("fills") or []):
                fid = (f.get("fill_id") or f.get("fillId")
                       or f"{oid}:{f.get('fill_time') or f.get('fillTime') or ''}:"
                          f"{f.get('fill_qty') or f.get('fillQty') or f.get('filled_qty')}")
                fill_qty = int(f.get("fill_qty", f.get("fillQty",
                                                       f.get("filled_qty", 0))))
                fill_price = float(f.get("fill_price", f.get("fillPrice", 0.0)))
                try:
                    f_raw = json.dumps(f, ensure_ascii=False) if isinstance(f, dict) else None
                except (TypeError, ValueError):
                    f_raw = None
                if self.db.insert_fill(fill_id=str(fid), order_id=oid,
                                       trade_date=td, symbol=SYMBOL,
                                       fill_qty=fill_qty, fill_price=fill_price,
                                       fee=f.get("fee"), fill_time=f.get("fill_time"),
                                       raw=f_raw):
                    fills_added += 1

        # 2) 持仓 / 资金 / 行情 → 组合快照
        broker_qty = self._position_qty(positions)
        avail = self._available_to_sell(positions)
        cash = self._balance_cash(balance)
        price = self._quote_price(quote)
        if price is None or price <= 0:
            price = self._ref_price_snapshot
        if price is None or price <= 0:
            price = 0.0  # 无价 → 市值记 0（deviation 仍可算）
        nav = cash + broker_qty * price
        self.db.snapshot_portfolio(trade_date=td, nav=nav, position_qty=broker_qty,
                                   position_mv=broker_qty * price,
                                   available_cash=cash, available_to_sell_qty=avail)

        # 3) 日终对账（target vs actual 偏差）
        decision = self._today_decision(td)
        target_qty = broker_qty
        filled_total = sum(f["fill_qty"] for f in self.db.get_fills(td))
        unfilled_total = 0
        if decision is not None:
            intent_row = self.db._fetch_one(
                "SELECT * FROM order_intents WHERE decision_id = ?",
                (decision_id(td),))
            if intent_row is not None:
                target_qty = int(intent_row["target_qty"])
                order_row = self.db._fetch_one(
                    "SELECT * FROM broker_orders WHERE intent_key = ?",
                    (intent_row["intent_key"],))
                if order_row is not None:
                    filled_for_intent = sum(
                        f["fill_qty"] for f in self.db.get_fills(td)
                        if f["order_id"] == order_row["order_id"])
                    unfilled_total = max(0, int(order_row["quantity"]) - filled_for_intent)
        deviation = target_qty - broker_qty
        notes = f"desired={decision['desired_target']}" if decision else "无决策"
        self.db.upsert_reconciliation(trade_date=td, target_qty=target_qty,
                                      actual_qty=broker_qty, deviation=deviation,
                                      filled_qty=filled_total,
                                      unfilled_qty=unfilled_total, notes=notes)

        # 4) intents → FILLED/PARTIALLY_FILLED/UNFILLED → 终态 RECONCILED
        for intent in self.db._fetch_all(
                "SELECT * FROM order_intents WHERE trade_date = ?", (td,)):
            ik = intent["intent_key"]
            order_row = self.db._fetch_one(
                "SELECT * FROM broker_orders WHERE intent_key = ?", (ik,))
            filled = 0
            if order_row is not None:
                filled = sum(f["fill_qty"] for f in self.db.get_fills(td)
                             if f["order_id"] == order_row["order_id"])
                order_qty = int(order_row["quantity"])
            else:
                order_qty = 0
            if filled == 0:
                final = (UNFILLED_AT_CUTOFF
                         if intent["status"] == UNFILLED_AT_CUTOFF else UNFILLED)
            elif filled >= order_qty:
                final = FILLED
            else:
                final = PARTIALLY_FILLED
            for old in (EXECUTION_PENDING, SUBMITTED, PARTIALLY_FILLED,
                        UNFILLED, UNFILLED_AT_CUTOFF, FAILED):
                if self.db.transition_intent_status(ik, old, final):
                    break
            for old in (FILLED, PARTIALLY_FILLED, UNFILLED, UNFILLED_AT_CUTOFF):
                if self.db.transition_intent_status(ik, old, RECONCILED):
                    break

        # 5) 重放校验：当日 decision 字段重放 decide_target 应同 desired
        replay_ok = None
        if decision is not None:
            snap = {"current_rank": decision["current_rank"],
                    "previous_rank": decision["previous_rank"],
                    "ma5": decision["ma5"], "ma10": decision["ma10"],
                    "ma20": decision["ma20"],
                    "current_target": decision["previous_target"]}
            try:
                rdesired, _rreason = replay_decision(snap)
                replay_ok = (rdesired == decision["desired_target"])
            except ValueError:
                replay_ok = False
            if replay_ok:
                self.db.add_event(event="RECONCILE_OK", level="INFO",
                                  trade_date=td, timepoint=RECONCILE,
                                  detail=f"重放校验一致：desired="
                                         f"{decision['desired_target']}；"
                                         f"target={target_qty} actual={broker_qty} "
                                         f"deviation={deviation}")
            else:
                self.db.add_event(event="REPLAY_MISMATCH", level="WARN",
                                  trade_date=td, timepoint=RECONCILE,
                                  detail=f"重放校验不一致：决策 desired="
                                         f"{decision['desired_target']}，重放结果未对齐"
                                         f"（输入快照 {snap}）")
        self.db.add_event(event="RECONCILED", level="INFO", trade_date=td,
                          timepoint=RECONCILE,
                          detail=f"收盘对账完成：nav={nav:.2f} 持仓={broker_qty} "
                                 f"可卖={avail} 成交={filled_total} 未成交={unfilled_total}")
        return (True, {"trade_date": td, "nav": nav, "position_qty": broker_qty,
                       "deviation": deviation, "fills_added": fills_added,
                       "replay_ok": replay_ok})

    # ---------------- 时间轴分发 ----------------

    def run_timepoint(self, tp):
        """按冻结时间轴分发到各步骤（08:45/09:27/09:28/14:45/14:50/14:57/15:05）。

        09:27 = 信号冻结 + 决策；09:28 = 决策确认（仅事件审计）；
        14:45 = 执行前风控（含 14:45 参考价快照）；14:50 = 窗口下单；
        14:57 = 停止追价；15:05 = 收盘对账。
        """
        now = self.clock()
        td = now.strftime("%Y%m%d")
        if tp == "08:45":
            return self.step_prepare_trend(td)
        if tp == "09:27":
            frozen = self.step_freeze_signal(td)
            if frozen == DATA_NOT_QUALIFIED:
                return frozen
            return self.step_decide(td)
        if tp == "09:28":
            return self._confirm_decision(td)
        if tp == "14:45":
            return self.step_pre_exec_risk()
        if tp == "14:50":
            return self.step_execute(now)
        if tp == "14:57":
            return self.step_stop_chase()
        if tp == "15:05":
            return self.step_reconcile()
        raise ValueError(f"未知时间轴时点：{tp!r}"
                         "（合法：08:45/09:27/09:28/14:45/14:50/14:57/15:05）")


# ==================== 自测（全 mock） ====================

class _FakeClock:
    """可控时钟：set(dt) / __call__() -> dt。"""

    def __init__(self, dt):
        self._dt = dt

    def set(self, dt):
        self._dt = dt

    def __call__(self):
        return self._dt


class _FakeMX:
    """内存假券商：模拟 mx_client.MXClient 响应形态（信封 {"code":0,"data":{...}}，
    place_order(action, symbol, quantity, price_type, price)）；可选 auto_fill /
    注入异常；记录调用供断言。"""

    def __init__(self, quote=None, balance=None, positions=None, auto_fill=False):
        self.quote = quote or {"symbol": SYMBOL, "price": 1.0,
                               "bid1": 1.0, "ask1": 1.0, "halted": False}
        self.balance = balance or {"cash": 100000.0, "available_cash": 100000.0}
        self.positions = positions or []
        self.orders = []
        self.auto_fill = auto_fill
        self.place_order_error = None  # 可注入 MXError
        self.place_order_calls = []
        self._oid = 0
        self._fid = 0

    def query_market(self, query):
        return {"code": 0, "data": dict(self.quote)}

    def get_balance(self):
        return {"code": 0, "data": {"availableCash": float(self.balance.get("cash", 0.0))}}

    def get_positions(self):
        rows = [{"symbol": p.get("symbol"), "position": p.get("quantity", 0),
                 "available_to_sell": p.get("available_to_sell_qty", 0)}
                for p in self.positions]
        return {"code": 0, "data": {"list": rows}}

    def get_orders(self):
        return {"code": 0, "data": {"list": [dict(o) for o in self.orders]}}

    def place_order(self, action, symbol, quantity, price_type="MARKET",
                    price=None):
        self.place_order_calls.append(
            {"action": action, "symbol": symbol, "quantity": quantity,
             "price_type": price_type, "price": price})
        if self.place_order_error is not None:
            raise self.place_order_error
        self._oid += 1
        oid = f"MX{self._oid:08d}"
        fill_price = price if price else self.quote["price"]
        if self.auto_fill:
            self._fid += 1
            fills = [{"fillId": f"FL{self._fid:08d}", "fillQty": quantity,
                      "fillPrice": fill_price, "fillTime": "14:53:00", "fee": 0.0}]
            status, fq = "filled", quantity
            self._apply_fill(symbol, action, quantity, fill_price)
        else:
            fills, status, fq = [], "submitted", 0
        order = {"orderId": oid, "stockCode": symbol, "type": action,
                 "quantity": quantity, "filledQuantity": fq,
                 "price": fill_price, "status": status, "fillList": fills}
        self.orders.append(order)
        return {"code": 0, "data": {"orderId": oid, "ok": True}}

    def _apply_fill(self, symbol, side, quantity, fill_price):
        for p in self.positions:
            if p.get("symbol") == symbol:
                if side == "buy":
                    p["quantity"] = int(p.get("quantity", 0)) + quantity
                    self.balance["cash"] = float(self.balance.get("cash", 0.0)) \
                        - quantity * fill_price
                else:
                    p["quantity"] = max(0, int(p.get("quantity", 0)) - quantity)
                    p["available_to_sell_qty"] = max(
                        0, int(p.get("available_to_sell_qty", 0)) - quantity)
                    self.balance["cash"] = float(self.balance.get("cash", 0.0)) \
                        + quantity * fill_price
                return
        self.positions.append({"symbol": symbol,
                               "quantity": quantity if side == "buy" else 0,
                               "available_to_sell_qty": 0})
        if side == "buy":
            self.balance["cash"] = float(self.balance.get("cash", 0.0)) \
                - quantity * fill_price


class _FakeFetchDaily:
    """按自然日生成工作日 K 线（跳过周末），可反向（递减收盘）或截断。"""

    def __init__(self, start_close=1.0, step=0.01, reverse=False, max_bars=None):
        self.start_close = start_close
        self.step = step
        self.reverse = reverse
        self.max_bars = max_bars
        self.calls = []

    def __call__(self, code, start, end):
        self.calls.append((code, start, end))
        bars = []
        d = start
        i = 0
        while d <= end:
            if d.weekday() < 5:
                c = self.start_close + i * self.step
                if self.reverse:
                    c = self.start_close - i * self.step
                bars.append({"date": d.strftime("%Y%m%d"), "close": round(c, 4)})
                if self.max_bars is not None and len(bars) >= self.max_bars:
                    break
            i += 1
            d += datetime.timedelta(days=1)
        return {"bars": bars, "code": code}


class PaperEngineSelfTest(unittest.TestCase):
    """任务C 自测：全 mock（内存 DB / fake mx / fake fetch_daily / 可控 clock）。

    覆盖：happy path 全时间轴 + DATA_NOT_QUALIFIED 三失败 + 防重 + 幂等 +
    T+1 可卖 + 执行窗外不执行（+ FAILED 重试 / 市价兜底 / 限价非法不重发 /
    previous_rank 派生等补充分支）。
    """

    T = "20260804"  # 周二；T-1 = 20260803（周一，交易日）
    T_DT = datetime.datetime(2026, 8, 4, 8, 45, 0)

    def _make_env(self, config=None, quote=None, positions=None, balance=None,
                  auto_fill=False, fetch=None, reverse=False):
        """构造 (db, mx, clock, engine)：内存 DB + fake mx + fake fetch + 可控时钟。"""
        tmpdir = tempfile.mkdtemp(prefix="paper_engine_test_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        db = PaperDB.connect(":memory:")
        mx = _FakeMX(quote=quote, positions=positions, balance=balance,
                     auto_fill=auto_fill)
        clock = _FakeClock(self.T_DT)
        fetch_daily = fetch or _FakeFetchDaily(reverse=reverse)
        engine = PaperEngine(db=db, mx=mx, fetch_daily=fetch_daily,
                             signal_dir=tmpdir, clock=clock,
                             config={"trading_enabled": True, **(config or {})})
        return db, mx, clock, engine

    def _write_signal(self, engine, trade_date, **over):
        """写情绪文件（默认合格；over 可覆盖字段）。"""
        data = {"current_rank": 0.55, "previous_rank": 0.40, "metric_value": 66.4,
                "history_count": 60, "formal_usable": True,
                "source_contract_version": "emotion-v1",
                "known_at": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 09:26:05"}
        data.update(over)
        with open(os.path.join(engine.signal_dir, f"{trade_date}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def _run_to_decision(self, db, mx, clock, engine, over=None):
        """跑到 09:27 决策（08:45 趋势 + 写信号 + 09:27 冻结决策）。"""
        clock.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine.run_timepoint("08:45"))
        self._write_signal(engine, "20260804", **(over or {}))
        clock.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        return engine.run_timepoint("09:27")

    # ---------------- 1) happy path 全时间轴 ----------------
    def test_01_happy_path_full_timeline(self):
        db, mx, clock, engine = self._make_env(auto_fill=True)
        # 08:45 趋势
        self.assertTrue(engine.run_timepoint("08:45"))
        trend = db.get_trend("20260804")
        self.assertIsNotNone(trend)
        self.assertGreater(trend["ma5"], trend["ma10"])  # 递增收盘 → ma5 > ma10
        self.assertGreaterEqual(trend["bar_count"], 20)
        self.assertEqual(trend["last_bar_date"], "20260803")  # T-1 周一
        # 09:27 冻结 + 决策（规则3：0→0.5）
        self._write_signal(engine, "20260804")
        clock.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        decision = engine.run_timepoint("09:27")
        self.assertIsInstance(decision, dict)
        self.assertEqual(decision["desired_target"], 0.5)
        self.assertEqual(decision["reason_code"], "P50_UPCROSS_PROBE")
        self.assertEqual(decision["previous_target"], 0.0)  # 无历史 → 0.0
        # 09:28 决策确认
        clock.set(datetime.datetime(2026, 8, 4, 9, 28, 0))
        self.assertEqual(engine.run_timepoint("09:28")["decision_id"],
                         decision["decision_id"])
        # 14:45 执行前风控（含参考价快照）
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertTrue(ok, report)
        self.assertEqual(engine._ref_price_snapshot, 1.0)
        # 14:50 窗口下单
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 30))
        res = engine.run_timepoint("14:50")
        self.assertTrue(res[0], res)
        intents = db._fetch_all("SELECT * FROM order_intents WHERE trade_date='20260804'")
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["status"], SUBMITTED)
        self.assertEqual(intents[0]["action"], BUY_HALF)
        orders = db._fetch_all("SELECT * FROM broker_orders WHERE trade_date='20260804'")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["quantity"], intents[0]["delta_qty"])
        self.assertEqual(orders[0]["status"], SUBMITTED)
        # 14:57 停止追价（auto_fill 已成交 → 不标 UNFILLED_AT_CUTOFF）
        clock.set(datetime.datetime(2026, 8, 4, 14, 57, 0))
        sr = engine.run_timepoint("14:57")
        self.assertTrue(sr[0])
        self.assertEqual(sr[1]["marked_unfilled"], 0)
        # 15:05 收盘对账
        clock.set(datetime.datetime(2026, 8, 4, 15, 5, 0))
        rr = engine.run_timepoint("15:05")
        self.assertTrue(rr[0], rr)
        self.assertEqual(rr[1]["replay_ok"], True)
        final = db._fetch_one("SELECT * FROM order_intents WHERE intent_key=?",
                              (intents[0]["intent_key"],))
        self.assertEqual(final["status"], RECONCILED)
        brow = db._fetch_one("SELECT * FROM broker_orders WHERE order_id=?",
                             (orders[0]["order_id"],))
        self.assertEqual(brow["status"], FILLED)
        fills = db.get_fills("20260804")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["fill_qty"], orders[0]["quantity"])
        snap = db._fetch_one("SELECT * FROM portfolio_snapshots WHERE trade_date='20260804'")
        self.assertGreater(snap["nav"], 0)
        self.assertEqual(snap["position_qty"], orders[0]["quantity"])
        rc = db._fetch_one("SELECT * FROM daily_reconciliations WHERE trade_date='20260804'")
        self.assertEqual(rc["deviation"], 0)  # 全部成交 → target == actual
        # 组合快照 nav ≈ 初始资金（无手续费）
        self.assertAlmostEqual(snap["nav"], 100000.0, delta=1.0)

    # ---------------- 2) DATA_NOT_QUALIFIED 三失败 ----------------
    def test_02_data_not_qualified_signal_missing(self):
        db, mx, clock, engine = self._make_env()
        clock.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine.run_timepoint("08:45"))  # 趋势合格
        clock.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        r = engine.run_timepoint("09:27")  # 无信号文件
        self.assertEqual(r, DATA_NOT_QUALIFIED)
        codes = [e["event"] for e in db._fetch_all(
            "SELECT * FROM system_events WHERE trade_date='20260804'")]
        self.assertIn(ERROR_NOT_PUBLISHED, codes)  # 原始 8 码原样入库
        self.assertIn(DATA_NOT_QUALIFIED, codes)
        self.assertIsNone(db._fetch_one(
            "SELECT * FROM strategy_decisions WHERE trade_date='20260804'"))
        self.assertEqual(db.get_open_intents("20260804"), [])  # 不生成订单

    def test_03_data_not_qualified_signal_invalid(self):
        db, mx, clock, engine = self._make_env()
        clock.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine.run_timepoint("08:45"))
        # history_count=59 不合格
        self._write_signal(engine, "20260804", history_count=59)
        clock.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        r = engine.run_timepoint("09:27")
        self.assertEqual(r, DATA_NOT_QUALIFIED)
        ev = db._fetch_all("SELECT * FROM system_events WHERE trade_date='20260804'"
                           " AND event='DATA_NOT_QUALIFIED'")
        self.assertTrue(any("history_count" in (e["detail"] or "") for e in ev), ev)
        self.assertIsNone(db._fetch_one(
            "SELECT * FROM strategy_decisions WHERE trade_date='20260804'"))
        # 不支持的契约版本
        db2, mx2, clock2, engine2 = self._make_env()
        clock2.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine2.run_timepoint("08:45"))
        self._write_signal(engine2, "20260804",
                           source_contract_version="emotion-v9")
        clock2.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        self.assertEqual(engine2.run_timepoint("09:27"), DATA_NOT_QUALIFIED)
        self.assertIsNone(db2._fetch_one(
            "SELECT * FROM strategy_decisions WHERE trade_date='20260804'"))
        # known_at 早于 09:25
        db3, mx3, clock3, engine3 = self._make_env()
        clock3.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine3.run_timepoint("08:45"))
        self._write_signal(engine3, "20260804",
                           known_at="2026-08-04 09:24:59")
        clock3.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        self.assertEqual(engine3.run_timepoint("09:27"), DATA_NOT_QUALIFIED)

    def test_04_data_not_qualified_trend(self):
        # 行数 < 20
        db, mx, clock, engine = self._make_env(
            fetch=lambda code, s, e: {"bars": [{"date": s.strftime("%Y%m%d"),
                                                "close": 1.0} for _ in range(5)]})
        clock.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertFalse(engine.run_timepoint("08:45"))
        ev = db._fetch_all("SELECT * FROM system_events WHERE trade_date='20260804'"
                           " AND event='DATA_NOT_QUALIFIED'")
        self.assertTrue(any("行数不足" in (e["detail"] or "") for e in ev), ev)
        # 最后一行 != 最近交易日（缺最后一天）
        def fetch_short(code, s, e):
            return {"bars": _FakeFetchDaily()(code, s, e)["bars"][:-1]}
        db2, mx2, clock2, engine2 = self._make_env(fetch=fetch_short)
        clock2.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertFalse(engine2.run_timepoint("08:45"))
        ev2 = db2._fetch_all("SELECT * FROM system_events WHERE trade_date='20260804'"
                             " AND event='DATA_NOT_QUALIFIED'")
        self.assertTrue(any("最后一行" in (e["detail"] or "") for e in ev2), ev2)
        # K 线中间缺一个有效交易日（不连续）
        def fetch_hole(code, s, e):
            bars = _FakeFetchDaily()(code, s, e)["bars"]
            return {"bars": [b for i, b in enumerate(bars) if i != 30]}
        db3, mx3, clock3, engine3 = self._make_env(fetch=fetch_hole)
        clock3.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertFalse(engine3.run_timepoint("08:45"))
        ev3 = db3._fetch_all("SELECT * FROM system_events WHERE trade_date='20260804'"
                             " AND event='DATA_NOT_QUALIFIED'")
        self.assertTrue(any("不连续" in (e["detail"] or "") for e in ev3), ev3)

    # ---------------- 3) 防重（决策幂等不可覆盖） ----------------
    def test_05_decision_dedupe(self):
        db, mx, clock, engine = self._make_env()
        clock.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine.run_timepoint("08:45"))
        self._write_signal(engine, "20260804")
        clock.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        d1 = engine.run_timepoint("09:27")
        self.assertIsInstance(d1, dict)
        # 再次 09:27 → 冻结覆盖 + 决策重复告警（禁止覆盖）
        d2 = engine.run_timepoint("09:27")
        self.assertEqual(d2["decision_id"], d1["decision_id"])
        self.assertEqual(d2["desired_target"], d1["desired_target"])
        rows = db._fetch_all("SELECT * FROM strategy_decisions WHERE trade_date='20260804'")
        self.assertEqual(len(rows), 1)  # 不产生第二行
        codes = [e["event"] for e in db._fetch_all(
            "SELECT * FROM system_events WHERE trade_date='20260804'")]
        self.assertIn("DECISION_DUPLICATE", codes)

    # ---------------- 4) 幂等（意图去重 / 委托不重发） ----------------
    def test_06_intent_idempotency(self):
        db, mx, clock, engine = self._make_env(auto_fill=False)
        self._run_to_decision(db, mx, clock, engine)
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertTrue(ok, report)
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 0))
        r1 = engine.run_timepoint("14:50")
        self.assertTrue(r1[0], r1)
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 20))
        r2 = engine.run_timepoint("14:50")  # 重复投递
        self.assertTrue(r2[0], r2)
        self.assertEqual(r2[1].get("dedup"), True)
        self.assertEqual(len(mx.place_order_calls), 1)  # 只下了一单
        self.assertEqual(len(db._fetch_all("SELECT * FROM broker_orders"
                                           " WHERE trade_date='20260804'")), 1)
        self.assertEqual(len(db._fetch_all("SELECT * FROM order_intents"
                                           " WHERE trade_date='20260804'")), 1)

    # ---------------- 5) T+1 可卖约束 ----------------
    def test_07_t_plus_1_sell(self):
        db, mx, clock, engine = self._make_env(
            positions=[{"symbol": SYMBOL, "quantity": 60000,
                        "available_to_sell_qty": 20000}],
            reverse=True)  # 递减收盘 → ma5 < ma10
        # 昨日决策满仓（1.0）
        db.create_decision(decision_id=decision_id("20260803"), trade_date="20260803",
                           previous_rank=0.4, current_rank=0.9,
                           ma5=3.0, ma10=2.0, ma20=1.0,
                           previous_target=0.0, desired_target=1.0,
                           reason_code="STRONG_TREND_CONFIRMED")
        # 今日规则1 → 清仓
        d = self._run_to_decision(db, mx, clock, engine,
                                  over={"current_rank": 0.30, "previous_rank": 0.60})
        self.assertIsInstance(d, dict)
        self.assertEqual(d["desired_target"], 0.0)
        self.assertEqual(d["reason_code"], "WEAK_STATE_CONFIRMED")
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertTrue(ok, report)
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 0))
        res = engine.run_timepoint("14:50")
        self.assertTrue(res[0], res)
        intent = db._fetch_one("SELECT * FROM order_intents WHERE trade_date='20260804'")
        self.assertEqual(intent["action"], SELL_ALL)
        order = db._fetch_one("SELECT * FROM broker_orders WHERE trade_date='20260804'")
        self.assertEqual(order["action"], "sell")  # broker_orders 列名为 action（方向）
        self.assertEqual(order["quantity"], 20000)  # T+1：仅可卖 20000（delta 为 -60000）
        # 14:57 未成交 → UNFILLED_AT_CUTOFF
        clock.set(datetime.datetime(2026, 8, 4, 14, 57, 0))
        sr = engine.run_timepoint("14:57")
        self.assertTrue(sr[0])
        self.assertEqual(sr[1]["marked_unfilled"], 1)
        st = db._fetch_one("SELECT * FROM order_intents WHERE trade_date='20260804'")
        self.assertEqual(st["status"], UNFILLED_AT_CUTOFF)

    def test_07b_t_plus_1_no_available_sell(self):
        # available_to_sell_qty = 0 → 风控不放行，不下单
        db, mx, clock, engine = self._make_env(
            positions=[{"symbol": SYMBOL, "quantity": 60000,
                        "available_to_sell_qty": 0}],
            reverse=True)
        db.create_decision(decision_id=decision_id("20260803"), trade_date="20260803",
                           previous_rank=0.4, current_rank=0.9,
                           ma5=3.0, ma10=2.0, ma20=1.0,
                           previous_target=0.0, desired_target=1.0,
                           reason_code="STRONG_TREND_CONFIRMED")
        self._run_to_decision(db, mx, clock, engine,
                              over={"current_rank": 0.30, "previous_rank": 0.60})
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertFalse(ok)
        self.assertFalse([c for c in report["checks"] if c["name"] == "sell_available"][0]["ok"])
        self.assertEqual(db.get_open_intents("20260804"), [])

    # ---------------- 6) 执行窗外不执行 ----------------
    def test_08_outside_exec_window(self):
        db, mx, clock, engine = self._make_env()
        self._run_to_decision(db, mx, clock, engine)
        # 窗口前 14:40 / 截止后 14:57（> 14:56:30）→ 不执行、不发单
        r1 = engine.step_execute(datetime.datetime(2026, 8, 4, 14, 40, 0))
        self.assertFalse(r1[0])
        self.assertIn("执行窗口外", r1[1])
        r2 = engine.step_execute(datetime.datetime(2026, 8, 4, 14, 57, 0))
        self.assertFalse(r2[0])
        self.assertIn("执行窗口外", r2[1])
        self.assertEqual(len(db._fetch_all("SELECT * FROM order_intents"
                                           " WHERE trade_date='20260804'")), 0)
        self.assertEqual(len(db._fetch_all("SELECT * FROM broker_orders"
                                           " WHERE trade_date='20260804'")), 0)
        self.assertEqual(mx.place_order_calls, [])

    # ---------------- 7) 补充分支：FAILED 重试 / 市价兜底 / 限价非法 / previous_rank 派生 ----------------
    def test_09_failed_retry_same_intent(self):
        db, mx, clock, engine = self._make_env()
        self._run_to_decision(db, mx, clock, engine)
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertTrue(ok, report)
        mx.place_order_error = MXError(code="RATE_LIMITED", message="模拟限流")
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 0))
        r1 = engine.run_timepoint("14:50")
        self.assertFalse(r1[0])
        self.assertIn("RATE_LIMITED", r1[1])
        st = db._fetch_one("SELECT * FROM order_intents WHERE trade_date='20260804'")
        self.assertEqual(st["status"], FAILED)  # 原始码事件 + FAILED
        codes = [e["event"] for e in db._fetch_all(
            "SELECT * FROM system_events WHERE trade_date='20260804'")]
        self.assertIn("RATE_LIMITED", codes)
        # 同 intent 重试成功
        mx.place_order_error = None
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 30))
        r2 = engine.run_timepoint("14:50")
        self.assertTrue(r2[0], r2)
        st2 = db._fetch_one("SELECT * FROM order_intents WHERE trade_date='20260804'")
        self.assertEqual(st2["status"], SUBMITTED)
        self.assertEqual(len(db._fetch_all("SELECT * FROM broker_orders"
                                           " WHERE trade_date='20260804'")), 1)

    def test_10_market_fallback_when_no_book(self):
        # 盘口无买一/卖一 → useMarketPrice=True 市价兜底
        db, mx, clock, engine = self._make_env(
            quote={"symbol": SYMBOL, "price": 1.0, "halted": False})
        self._run_to_decision(db, mx, clock, engine)
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertTrue(ok, report)
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 0))
        res = engine.run_timepoint("14:50")
        self.assertTrue(res[0], res)
        last = mx.place_order_calls[-1]
        self.assertEqual(last["price_type"], "MARKET")  # MARKET == useMarketPrice=True 语义
        self.assertIsNone(last["price"])
        order = db._fetch_one("SELECT * FROM broker_orders WHERE trade_date='20260804'")
        self.assertEqual(order["price_type"], "market")
        codes = [e["event"] for e in db._fetch_all(
            "SELECT * FROM system_events WHERE trade_date='20260804'")]
        self.assertIn("MARKET_FALLBACK", codes)

    def test_11_illegal_limit_no_resend(self):
        # 卖一价为 0（限价非法）→ INTERNAL_ERROR 不重发、不产生意图/委托
        db, mx, clock, engine = self._make_env(
            quote={"symbol": SYMBOL, "price": 1.0, "bid1": 0.0, "ask1": 0.0,
                   "halted": False})
        self._run_to_decision(db, mx, clock, engine)
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertTrue(ok, report)
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 0))
        res = engine.run_timepoint("14:50")
        self.assertFalse(res[0])
        self.assertIn("限价非法", res[1])
        codes = [e["event"] for e in db._fetch_all(
            "SELECT * FROM system_events WHERE trade_date='20260804'")]
        self.assertIn(ERROR_INTERNAL_ERROR, codes)
        self.assertEqual(db.get_open_intents("20260804"), [])
        self.assertEqual(mx.place_order_calls, [])

    def test_12_previous_rank_derivation(self):
        # previous_rank 缺省 → 从库派生（前一日合格信号）
        db, mx, clock, engine = self._make_env()
        db.insert_signal(trade_date="20260803", current_rank=0.40,
                         previous_rank=None, metric_value=55.0, history_count=60,
                         formal_usable=1, source_contract_version="emotion-v1",
                         known_at="2026-08-03 09:26:00")
        clock.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine.run_timepoint("08:45"))
        self._write_signal(engine, "20260804", previous_rank=None)  # 文件缺省
        clock.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        r = engine.run_timepoint("09:27")
        self.assertIsInstance(r, dict)
        sig = db._fetch_one("SELECT * FROM signal_snapshots WHERE trade_date='20260804'")
        self.assertEqual(sig["previous_rank"], 0.40)  # 从库派生
        self.assertEqual(r["desired_target"], 0.5)  # 规则3 触发
        # 库中亦无历史 → 保守取 current_rank（首日不触发规则3）
        db2, mx2, clock2, engine2 = self._make_env()
        clock2.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine2.run_timepoint("08:45"))
        self._write_signal(engine2, "20260804", previous_rank=None)
        clock2.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        r2 = engine2.run_timepoint("09:27")
        self.assertIsInstance(r2, dict)
        sig2 = db2._fetch_one("SELECT * FROM signal_snapshots WHERE trade_date='20260804'")
        self.assertEqual(sig2["previous_rank"], 0.55)  # == current_rank（保守）
        self.assertEqual(r2["desired_target"], 0.0)  # 首日规则4 → 保持 0.0

    def test_13_no_order_when_target_unchanged(self):
        # desired == previous_target → 不下单（NO_ORDER）
        db, mx, clock, engine = self._make_env()
        db.create_decision(decision_id=decision_id("20260803"), trade_date="20260803",
                           previous_rank=0.55, current_rank=0.55,
                           ma5=1.22, ma10=1.19, ma20=1.17,
                           previous_target=0.0, desired_target=0.0,
                           reason_code="HOLD")
        # 今日同样不触发规则1/2/3 → desired = previous_target = 0.0
        d = self._run_to_decision(db, mx, clock, engine,
                                  over={"current_rank": 0.55, "previous_rank": 0.55})
        self.assertIsInstance(d, dict)
        self.assertEqual(d["desired_target"], 0.0)
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertTrue(ok, report)
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 0))
        res = engine.run_timepoint("14:50")
        self.assertFalse(res[0])
        self.assertIn("目标不变", res[1])
        self.assertEqual(db.get_open_intents("20260804"), [])
        self.assertEqual(mx.place_order_calls, [])

    def test_14_reconcile_replay_mismatch_warns(self):
        # 决策输入字段被篡改 → 重放不一致 → REPLAY_MISMATCH 告警
        db, mx, clock, engine = self._make_env(auto_fill=True)
        self._run_to_decision(db, mx, clock, engine)
        # 篡改决策输入 current_rank（正常应为 0.55，desired=0.5 保持）→ 重放得 0.0，不一致
        did = decision_id("20260804")
        db._conn.execute("UPDATE strategy_decisions SET current_rank = 0.20"
                         " WHERE decision_id = ?", (did,))
        db._conn.commit()
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 0))
        self.assertTrue(engine.run_timepoint("14:50")[0])
        clock.set(datetime.datetime(2026, 8, 4, 15, 5, 0))
        rr = engine.run_timepoint("15:05")
        self.assertTrue(rr[0], rr)
        self.assertEqual(rr[1]["replay_ok"], False)
        codes = [e["event"] for e in db._fetch_all(
            "SELECT * FROM system_events WHERE trade_date='20260804'")]
        self.assertIn("REPLAY_MISMATCH", codes)

    def test_15_unknown_timepoint(self):
        _, _, _, engine = self._make_env()
        with self.assertRaises(ValueError):
            engine.run_timepoint("12:00")


def _self_test():
    """任务C 全 mock 自测入口（等价 `python -m unittest paper_engine -v`）。"""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PaperEngineSelfTest)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("ALL_ASSERTIONS_PASSED")


if __name__ == "__main__":
    _self_test()
