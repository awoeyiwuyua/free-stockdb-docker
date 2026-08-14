#!/usr/bin/env python3
"""paper_db — 模拟盘持久层（任务B：SQLite 标准库，固定机器策略合同版）。

单进程、单账户、单标的（159915）目标仓位模型的落库层：情绪信号快照、均线趋势
快照、策略决策、订单意图、券商委托、成交回报、组合快照、日终对账、系统事件共
9 张表全部在本模块维护。只依赖 Python 标准库 sqlite3/datetime/os，零第三方依赖；
不碰 pybao/ 与 mcp 目录。

设计约定：
  1. 单写进程：SQLite WAL 模式下同一时刻只允许一个写事务，本模块即该唯一写者
     （连接对象由 PaperDB 实例独占；busy_timeout=5000 兜底读-写/写-写争用）。
  2. 追加式修订（append-only revision）：任何记录不物理删除；对「每日快照」型
     自然键（trade_date 唯一）重复投递采用 REPLACE——当日最新版本生效、旧版本被
     当日新版本覆盖（修订即覆盖当日行，历史日期行永不改动）；对「事件」型键
     （decision_id / intent_key / fill_id）采用 IGNORE——不可覆盖、重复投递直接
     去重返回 False，保证决策与成交历史的不可篡改性。
  3. 中文注释：9 张表每列均带中文注释；异常一律抛中文 ValueError。
  4. 幂等键语义（与冻结契约一致，值见本模块底部常量）：
       order_intent_key = f"{STRATEGY_ID}:{STRATEGY_VERSION}:{trade_date}:{SYMBOL}:{desired}"
       decision_id      = f"{STRATEGY_ID}:{trade_date}"（唯一、不可覆盖）
  5. 全部读取方法返回原生 dict / list（sqlite3.Row → dict）；写入方法返回
     bool 或 True；任何参数/状态校验失败抛中文 ValueError。

数据库路径解析（优先级从高到低）：
  1. PaperDB.connect(db_path=...) / init(db_path=...) 显式参数（如 ":memory:" 或临时文件）
  2. 环境变量 PAPER_DB（完整文件路径）
  3. 环境变量 DATA_DIR/paper.sqlite3（DATA_DIR 缺省 "/data"）
  文件型路径自动创建父目录。

PRAGMA（连接即生效）：
  journal_mode = WAL（:memory: 库不支持 WAL，返回 "memory" 属预期，非错误）
  busy_timeout = 5000（毫秒）
  foreign_keys = ON

自测：
    python paper_db.py   # :memory: 库跑全流程 CRUD + 重复 create_decision /
                         # create_intent 返回 False + 状态转换 WHERE 守卫
                         # （输出见 _self_test）
"""

from __future__ import annotations

import datetime
import os
import sqlite3

# ==================== 冻结契约常量（与 paper_core 同值，模块自包含） ====================
STRATEGY_ID = "emotion-trend-159915-v1"     # 策略标识（固定机器策略合同版）
STRATEGY_VERSION = "1.0.0"                  # 策略版本
SYMBOL = "159915"                           # 交易标的（创业板 ETF，T+1）
POSITION_STATES = (0.0, 0.5, 1.0)           # 合法目标仓位集合（空仓 / 半仓 / 满仓）
LOT_SIZE = 100                              # A 股一手 = 100 股
DECISION_TIME = "09:27"                     # 每日决策时点
EXEC_WINDOW_START = "14:50:00"              # 下单窗口起点
EXEC_CUTOFF = "14:56:30"                    # 下单窗口截止
STOP_CHASE = "14:57:00"                     # 追价停止时点
RECONCILE = "15:05:00"                      # 对账时点

# 生命周期状态常量（决策 / 订单状态机流转用）
SIGNAL_READY = "SIGNAL_READY"
DECIDED = "DECIDED"
EXECUTION_PENDING = "EXECUTION_PENDING"
SUBMITTED = "SUBMITTED"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
FILLED = "FILLED"
UNFILLED = "UNFILLED"
UNFILLED_AT_CUTOFF = "UNFILLED_AT_CUTOFF"
RECONCILED = "RECONCILED"
DATA_NOT_QUALIFIED = "DATA_NOT_QUALIFIED"

# 状态机动作常量（写入 order_intents.action 的取值，与冻结契约文案一致）
ACTION_BUY_HALF = "BUY_HALF"
ACTION_BUY_FULL = "BUY_FULL"
ACTION_SELL_ALL = "SELL_ALL"
ACTION_NO_ORDER = "NO_ORDER"

# 非终态意图集合：get_open_intents 的过滤口径
# （EXECUTION_PENDING/SUBMITTED/PARTIALLY_FILLED 均视为「尚未完结」的开放意图）
_OPEN_INTENT_STATUSES = (EXECUTION_PENDING, SUBMITTED, PARTIALLY_FILLED)

# 事件级别（add_event 校验用）
_EVENT_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")

# 默认策略参数（可被调用方覆盖后传入）
DEFAULT_MODEL_NAV = 100000.0                # 模型净值默认值


def _now() -> str:
    """当前本地时间字符串（YYYY-MM-DD HH:MM:SS），落库 created_at 用。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ==================== 9 张表 DDL（列含中文注释） ====================
_SCHEMA_SQL = """
-- 1) 情绪信号快照：研究流程每日 09:25 后投递，trade_date 唯一，当日重复投递覆盖
CREATE TABLE IF NOT EXISTS signal_snapshots (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,   -- 主键，自增
    trade_date               TEXT NOT NULL UNIQUE,                -- 交易日期 YYYYMMDD，每日唯一
    current_rank             REAL NOT NULL,                       -- 当日情绪强度排名 [0,1]
    previous_rank            REAL,                                -- 上一合格信号强度排名（文件缺省时由库内回填）
    metric_value             REAL NOT NULL,                       -- 情绪指标原始值
    history_count            INTEGER NOT NULL,                    -- 情绪历史样本数（合格要求 ==60）
    formal_usable            INTEGER NOT NULL,                    -- 是否正式可用 1/0（合格要求 ==1）
    source_contract_version  TEXT NOT NULL,                       -- 信号源契约版本（合格要求为受支持版本）
    known_at                 TEXT NOT NULL,                       -- 信号生成时点（合格要求 >= 当日 09:25）
    run_id                   TEXT,                                -- 研究流程运行实例号（可空）
    data_hash                TEXT,                                -- 原始 JSON 哈希（可空，防篡改比对）
    created_at               TEXT NOT NULL                        -- 本记录落库时间
);

-- 2) 均线趋势快照：决策所需的 ma5/ma10/ma20，trade_date 唯一，当日重复投递覆盖
CREATE TABLE IF NOT EXISTS trend_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,             -- 主键，自增
    trade_date     TEXT NOT NULL UNIQUE,                          -- 交易日期 YYYYMMDD，每日唯一
    ma5            REAL NOT NULL,                                 -- 5 日均线（收盘价口径）
    ma10           REAL NOT NULL,                                 -- 10 日均线（收盘价口径）
    ma20           REAL NOT NULL,                                 -- 20 日均线（收盘价口径）
    bar_count      INTEGER NOT NULL,                              -- 参与计算的 K 线根数
    last_bar_date  TEXT NOT NULL,                                 -- 最后一根 K 线日期 YYYYMMDD
    known_at       TEXT NOT NULL,                                 -- 快照生成时点
    created_at     TEXT NOT NULL                                  -- 本记录落库时间
);

-- 3) 策略决策：decision_id 主键唯一且不可覆盖（INSERT OR IGNORE，重复返回 False）
CREATE TABLE IF NOT EXISTS strategy_decisions (
    decision_id      TEXT PRIMARY KEY,                            -- 幂等键 f"{STRATEGY_ID}:{trade_date}"，唯一不可覆盖
    strategy_id      TEXT NOT NULL,                               -- 策略标识
    strategy_version TEXT NOT NULL,                               -- 策略版本
    trade_date       TEXT NOT NULL,                               -- 交易日期 YYYYMMDD
    symbol           TEXT NOT NULL,                               -- 交易标的代码
    previous_rank    REAL,                                        -- 决策输入：上一合格信号强度排名
    current_rank     REAL,                                        -- 决策输入：当日情绪强度排名
    ma5              REAL,                                        -- 决策输入：5 日均线
    ma10             REAL,                                        -- 决策输入：10 日均线
    ma20             REAL,                                        -- 决策输入：20 日均线
    previous_target  REAL,                                        -- 决策输入：上一目标仓位
    desired_target   REAL,                                        -- 决策输出：本日目标仓位（∈ POSITION_STATES）
    reason_code      TEXT,                                        -- 决策输出：规则理由码（WEAK_STATE_CONFIRMED 等）
    signal_known_at  TEXT,                                        -- 决策输入：信号生成时点（可空）
    status           TEXT NOT NULL DEFAULT 'DECIDED',             -- 决策生命周期状态
    created_at       TEXT NOT NULL                                -- 决策落库时间
);

-- 4) 订单意图：intent_key 主键唯一（去重下单），状态机流转由 transition_intent_status 守卫
CREATE TABLE IF NOT EXISTS order_intents (
    intent_key    TEXT PRIMARY KEY,                               -- 幂等键 f"{STRATEGY_ID}:{VER}:{date}:{symbol}:{desired}"
    decision_id   TEXT NOT NULL,                                  -- 所属决策 ID
    trade_date    TEXT NOT NULL,                                  -- 交易日期 YYYYMMDD
    symbol        TEXT NOT NULL,                                  -- 交易标的代码
    desired_target REAL NOT NULL,                                 -- 目标仓位（∈ POSITION_STATES）
    action        TEXT NOT NULL,                                  -- 状态机动作（BUY_HALF/BUY_FULL/SELL_ALL）
    target_qty    INTEGER NOT NULL,                               -- 目标股数（100 股向下取整）
    delta_qty     INTEGER NOT NULL,                               -- 差额股数（>0 买 / <0 卖）
    price_type    TEXT NOT NULL,                                  -- 价格类型（如 market/limit）
    status        TEXT NOT NULL DEFAULT 'EXECUTION_PENDING',      -- 意图生命周期状态
    created_at    TEXT NOT NULL,                                  -- 意图创建时间
    updated_at    TEXT NOT NULL                                   -- 意图最后更新时间
);

-- 5) 券商委托：order_id 主键，同一委托的状态/回报修订走 upsert（覆盖当日行）
CREATE TABLE IF NOT EXISTS broker_orders (
    order_id     TEXT PRIMARY KEY,                                -- 券商委托号（主键）
    intent_key   TEXT NOT NULL,                                   -- 关联订单意图键
    trade_date   TEXT NOT NULL,                                   -- 交易日期 YYYYMMDD
    symbol       TEXT NOT NULL,                                   -- 交易标的代码
    action       TEXT NOT NULL,                                   -- 买卖方向（buy/sell）
    quantity     INTEGER NOT NULL,                                -- 委托数量（股）
    price_type   TEXT NOT NULL,                                   -- 价格类型（market/limit）
    price        REAL,                                            -- 委托价格（市价单可空）
    status       TEXT NOT NULL,                                   -- 委托状态（SUBMITTED/PARTIALLY_FILLED/FILLED 等）
    submitted_at TEXT NOT NULL,                                   -- 委托提交时间
    raw_response TEXT                                             -- 券商原始响应 JSON（可空，落盘审计）
);

-- 6) 成交回报：fill_id 主键唯一（幂等去重，重复回报忽略），成交历史不可覆盖
CREATE TABLE IF NOT EXISTS fills (
    fill_id    TEXT PRIMARY KEY,                                  -- 成交回报号（主键，幂等去重）
    order_id   TEXT NOT NULL,                                     -- 关联券商委托号
    trade_date TEXT NOT NULL,                                     -- 交易日期 YYYYMMDD
    symbol     TEXT NOT NULL,                                     -- 交易标的代码
    fill_qty   INTEGER NOT NULL,                                  -- 成交数量（股）
    fill_price REAL NOT NULL,                                     -- 成交价格
    fee        REAL,                                              -- 手续费（可空）
    fill_time  TEXT,                                              -- 成交时间（可空）
    raw        TEXT                                               -- 券商原始回报 JSON（可空）
);

-- 7) 组合快照：trade_date 唯一，当日重复快照覆盖（保留最新）
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,      -- 主键，自增
    trade_date            TEXT NOT NULL UNIQUE,                   -- 交易日期 YYYYMMDD，每日唯一
    nav                   REAL NOT NULL,                          -- 组合净值
    position_qty          INTEGER NOT NULL,                       -- 当前持仓股数（含当日不可卖部分）
    position_mv           REAL,                                   -- 持仓市值（可空）
    available_cash        REAL,                                   -- 可用资金（可空）
    available_to_sell_qty INTEGER,                                -- 当日可卖数量（T+1，可空）
    created_at            TEXT NOT NULL                           -- 快照落库时间
);

-- 8) 日终对账：trade_date 唯一，当日重复对账覆盖（保留最新）
CREATE TABLE IF NOT EXISTS daily_reconciliations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,                -- 主键，自增
    trade_date  TEXT NOT NULL UNIQUE,                             -- 交易日期 YYYYMMDD，每日唯一
    target_qty  INTEGER NOT NULL,                                 -- 目标持仓股数
    actual_qty  INTEGER NOT NULL,                                 -- 实际持仓股数
    deviation   INTEGER NOT NULL,                                 -- 偏差股数（target - actual）
    filled_qty  INTEGER NOT NULL,                                 -- 当日累计成交股数
    unfilled_qty INTEGER NOT NULL,                                -- 当日未成交股数
    notes       TEXT,                                             -- 备注（可空，如 UNFILLED_AT_CUTOFF 原因）
    created_at  TEXT NOT NULL                                     -- 对账记录落库时间
);

-- 9) 系统事件：仅追加（append-only），全生命周期审计轨迹
CREATE TABLE IF NOT EXISTS system_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,                 -- 主键，自增
    ts         TEXT NOT NULL,                                     -- 事件发生时间
    trade_date TEXT,                                              -- 关联交易日期（可空）
    timepoint  TEXT,                                              -- 时点标识（如 09:27/14:50/15:05，可空）
    level      TEXT NOT NULL,                                     -- 级别 DEBUG/INFO/WARN/ERROR
    event      TEXT NOT NULL,                                     -- 事件名称（短英文标识）
    detail     TEXT                                               -- 事件详情（可空，中文描述）
);
"""


def init(db_path=None) -> "PaperDB":
    """按环境变量解析数据库路径并打开（任务 B API：init）。

    路径优先级：显式 db_path > 环境变量 PAPER_DB > DATA_DIR/paper.sqlite3
    （DATA_DIR 缺省 "/data"）。文件型路径自动创建父目录。

    参数：
      db_path: 显式路径（如 ":memory:" 或临时文件）；None 时按环境变量解析。
    返回：
      PaperDB 实例（已建表）。
    """
    return PaperDB.connect(db_path)


def _require_nonempty_str(value, field: str) -> str:
    """校验必填非空字符串，失败抛中文 ValueError。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须为非空字符串，收到 {value!r}")
    return value


def _require_target(value, field: str) -> float:
    """校验目标仓位 ∈ POSITION_STATES，失败抛中文 ValueError。"""
    if value not in POSITION_STATES:
        raise ValueError(
            f"{field}={value!r} 不在合法仓位状态 POSITION_STATES={POSITION_STATES}"
        )
    return float(value)


def _require_int(value, field: str) -> int:
    """校验整数（int 或整数值浮点均可，拒绝 bool），失败抛中文 ValueError。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or int(value) != value:
        raise ValueError(f"{field} 必须为整数，收到 {value!r}")
    return int(value)


class PaperDB:
    """模拟盘持久层（SQLite WAL，单写进程约定，追加式修订）。

    每个实例独占一个 sqlite3 连接；同一时刻仅允许一个写事务（单写进程约定），
    busy_timeout=5000 兜底短暂争用。所有公开方法返回原生 dict/list（读取）或
    bool/True（写入），异常一律为中文 ValueError。
    """

    @classmethod
    def connect(cls, db_path=None) -> "PaperDB":
        """打开（或新建）数据库并初始化 9 张表（任务 B API：connect）。

        参数：
          db_path: 显式路径（":memory:" / 临时文件 / 文件路径）；None 时按
                   环境变量解析（PAPER_DB > DATA_DIR/paper.sqlite3，DATA_DIR
                   缺省 "/data"）。
        """
        if db_path is None:
            db_path = os.environ.get("PAPER_DB") or os.path.join(
                os.environ.get("DATA_DIR", "/data"), "paper.sqlite3"
            )
        if db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_path))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
        return cls(db_path)

    def __init__(self, db_path: str):
        """构造并连接：设置 PRAGMA（WAL/busy_timeout/foreign_keys）后建表。

        单写进程约定：本连接由当前进程独占，不允许跨进程并发写；需要跨线程
        使用时以单写线程为准（check_same_thread=False 仅为调度线程便利）。
        """
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")          # 写锁等待 5 秒
        self._conn.execute("PRAGMA foreign_keys = ON")            # 外键约束开启
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")       # WAL 模式（:memory: 返回 memory，属预期）
        except sqlite3.DatabaseError as exc:
            # :memory: 等不支持 WAL 的库不视为错误，仅记录
            self.add_event(level="WARN", event="WAL_UNAVAILABLE",
                           detail=f"journal_mode=WAL 设置失败：{exc}（:memory: 库属预期）")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    # ---------------- 内部工具 ----------------
    def _fetch_one(self, sql: str, params=()) -> dict | None:
        """执行查询并返回单行 dict（无结果返回 None）。"""
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def _fetch_all(self, sql: str, params=()) -> list:
        """执行查询并返回 list[dict]（无结果返回空列表）。"""
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _count(self, table: str) -> int:
        """内部辅助：统计表行数（自测与审计用）。"""
        return self._conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    # ---------------- 1) 情绪信号快照 ----------------
    def insert_signal(self, trade_date: str, current_rank: float, metric_value: float,
                      history_count: int, formal_usable: int,
                      source_contract_version: str, known_at: str,
                      previous_rank: float | None = None,
                      run_id: str | None = None, data_hash: str | None = None,
                      created_at: str | None = None) -> bool:
        """写入（或覆盖当日）情绪信号快照（追加式修订：trade_date 唯一，当日最新生效）。

        参数：
          trade_date: 交易日期 YYYYMMDD
          current_rank: 当日情绪强度排名 [0,1]
          previous_rank: 上一合格信号强度排名（可缺 None）
          metric_value: 情绪指标原始值
          history_count: 情绪历史样本数（合格要求 ==60）
          formal_usable: 是否正式可用（1/0 或 True/False）
          source_contract_version: 信号源契约版本（如 "emotion-v1"）
          known_at: 信号生成时点（须 >= 当日 09:25，由执行层校验）
          run_id: 研究流程运行实例号（可空）
          data_hash: 原始 JSON 哈希（可空）
          created_at: 落库时间（缺省当前时间）
        返回：
          True（写入成功）。
        """
        trade_date = _require_nonempty_str(trade_date, "trade_date")
        current_rank = float(current_rank)
        metric_value = float(metric_value)
        history_count = _require_int(history_count, "history_count")
        if formal_usable not in (0, 1, False, True):
            raise ValueError(f"formal_usable 必须为 1/0 或 True/False，收到 {formal_usable!r}")
        formal_usable = 1 if formal_usable in (1, True) else 0
        _require_nonempty_str(source_contract_version, "source_contract_version")
        _require_nonempty_str(known_at, "known_at")
        self._conn.execute(
            "INSERT OR REPLACE INTO signal_snapshots "
            "(trade_date, current_rank, previous_rank, metric_value, history_count, "
            " formal_usable, source_contract_version, known_at, run_id, data_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (trade_date, current_rank, previous_rank, metric_value, history_count,
             formal_usable, source_contract_version, known_at, run_id, data_hash,
             created_at or _now()),
        )
        self._conn.commit()
        return True

    def get_latest_qualified_signal(self, before_date: str) -> dict | None:
        """查询最近一条合格信号（previous_rank 派生用，任务 B API）。

        合格口径（本层）：formal_usable == 1 且 trade_date < before_date
        （严格早于；不包含当日）。执行层以返回记录的 current_rank 作为
        previous_rank 回填决策输入；无记录返回 None。
        """
        _require_nonempty_str(before_date, "before_date")
        return self._fetch_one(
            "SELECT * FROM signal_snapshots "
            "WHERE formal_usable = 1 AND trade_date < ? "
            "ORDER BY trade_date DESC LIMIT 1",
            (before_date,),
        )

    # ---------------- 2) 均线趋势快照 ----------------
    def insert_trend(self, trade_date: str, ma5: float, ma10: float, ma20: float,
                     bar_count: int, last_bar_date: str, known_at: str,
                     created_at: str | None = None) -> bool:
        """写入（或覆盖当日）均线趋势快照（trade_date 唯一，当日最新生效）。

        返回 True（写入成功）；ma5/ma10/ma20 任一非法抛中文 ValueError。
        """
        trade_date = _require_nonempty_str(trade_date, "trade_date")
        ma5 = float(ma5)
        ma10 = float(ma10)
        ma20 = float(ma20)
        if not (ma5 > 0 and ma10 > 0 and ma20 > 0):
            raise ValueError(f"ma5/ma10/ma20 必须为正数，收到 ({ma5}, {ma10}, {ma20})")
        bar_count = _require_int(bar_count, "bar_count")
        _require_nonempty_str(last_bar_date, "last_bar_date")
        _require_nonempty_str(known_at, "known_at")
        self._conn.execute(
            "INSERT OR REPLACE INTO trend_snapshots "
            "(trade_date, ma5, ma10, ma20, bar_count, last_bar_date, known_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (trade_date, ma5, ma10, ma20, bar_count, last_bar_date, known_at,
             created_at or _now()),
        )
        self._conn.commit()
        return True

    def get_trend(self, trade_date: str) -> dict | None:
        """查询指定交易日的均线趋势快照；无记录返回 None。"""
        _require_nonempty_str(trade_date, "trade_date")
        return self._fetch_one(
            "SELECT * FROM trend_snapshots WHERE trade_date = ?", (trade_date,)
        )

    # ---------------- 3) 策略决策 ----------------
    def create_decision(self, decision_id: str, trade_date: str,
                        strategy_id: str = STRATEGY_ID,
                        strategy_version: str = STRATEGY_VERSION,
                        symbol: str = SYMBOL,
                        previous_rank: float | None = None,
                        current_rank: float | None = None,
                        ma5: float | None = None, ma10: float | None = None,
                        ma20: float | None = None,
                        previous_target: float | None = None,
                        desired_target: float | None = None,
                        reason_code: str | None = None,
                        signal_known_at: str | None = None,
                        status: str = DECIDED,
                        created_at: str | None = None) -> bool:
        """创建策略决策记录（INSERT OR IGNORE 语义，不可覆盖）。

        decision_id 已存在 → 返回 False 且不产生任何修改（同日重复投递去重）。
        新增成功 → 返回 True。

        参数：
          decision_id: 幂等键 f"{STRATEGY_ID}:{trade_date}"（唯一、不可覆盖）
          trade_date: 交易日期 YYYYMMDD
          strategy_id/strategy_version/symbol: 策略标识（缺省冻结契约值）
          previous_rank/current_rank: 决策输入情绪排名（可空，与信号快照解耦）
          ma5/ma10/ma20: 决策输入均线（可空）
          previous_target/desired_target: 上一目标仓位 / 本日目标仓位
            （非空时须 ∈ POSITION_STATES）
          reason_code: 决策理由码（如 WEAK_STATE_CONFIRMED）
          signal_known_at: 信号生成时点（可空）
          status: 决策生命周期状态（缺省 DECIDED）
        """
        _require_nonempty_str(decision_id, "decision_id")
        trade_date = _require_nonempty_str(trade_date, "trade_date")
        _require_nonempty_str(strategy_id, "strategy_id")
        _require_nonempty_str(strategy_version, "strategy_version")
        _require_nonempty_str(symbol, "symbol")
        if previous_target is not None:
            previous_target = _require_target(previous_target, "previous_target")
        if desired_target is not None:
            desired_target = _require_target(desired_target, "desired_target")
        _require_nonempty_str(status, "status")
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO strategy_decisions "
            "(decision_id, strategy_id, strategy_version, trade_date, symbol, "
            " previous_rank, current_rank, ma5, ma10, ma20, previous_target, "
            " desired_target, reason_code, signal_known_at, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, strategy_id, strategy_version, trade_date, symbol,
             previous_rank, current_rank, ma5, ma10, ma20, previous_target,
             desired_target, reason_code, signal_known_at, status,
             created_at or _now()),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ---------------- 4) 订单意图 ----------------
    def create_intent(self, intent_key: str, decision_id: str, trade_date: str,
                      symbol: str, desired_target: float, action: str,
                      target_qty: int, delta_qty: int, price_type: str,
                      status: str = EXECUTION_PENDING,
                      created_at: str | None = None,
                      updated_at: str | None = None) -> bool:
        """创建订单意图（intent_key 唯一，INSERT OR IGNORE 语义去重下单）。

        intent_key 已存在 → 返回 False 且不产生任何修改（网络重试/重复投递
        按幂等键去重）。新增成功 → 返回 True。
        """
        _require_nonempty_str(intent_key, "intent_key")
        _require_nonempty_str(decision_id, "decision_id")
        trade_date = _require_nonempty_str(trade_date, "trade_date")
        _require_nonempty_str(symbol, "symbol")
        desired_target = _require_target(desired_target, "desired_target")
        if action not in (ACTION_BUY_HALF, ACTION_BUY_FULL, ACTION_SELL_ALL):
            raise ValueError(
                f"action={action!r} 非法；合法下单动作仅 {ACTION_BUY_HALF}/"
                f"{ACTION_BUY_FULL}/{ACTION_SELL_ALL}（NO_ORDER 不产生意图）"
            )
        target_qty = _require_int(target_qty, "target_qty")
        delta_qty = _require_int(delta_qty, "delta_qty")
        _require_nonempty_str(price_type, "price_type")
        _require_nonempty_str(status, "status")
        ts = created_at or _now()
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO order_intents "
            "(intent_key, decision_id, trade_date, symbol, desired_target, action, "
            " target_qty, delta_qty, price_type, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (intent_key, decision_id, trade_date, symbol, desired_target, action,
             target_qty, delta_qty, price_type, status, ts, updated_at or ts),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def transition_intent_status(self, intent_key: str, old: str, new: str) -> bool:
        """意图状态转换（WHERE status = old 守卫，任务 B API）。

        UPDATE ... SET status = ?, updated_at = ? WHERE intent_key = ? AND status = ?
        仅当当前状态恰为 old 时才会更新；old 不匹配（并发/乱序/重复调用）
        → 返回 False 且不产生任何修改。
        """
        _require_nonempty_str(intent_key, "intent_key")
        _require_nonempty_str(old, "old")
        _require_nonempty_str(new, "new")
        cur = self._conn.execute(
            "UPDATE order_intents SET status = ?, updated_at = ? "
            "WHERE intent_key = ? AND status = ?",
            (new, _now(), intent_key, old),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_open_intents(self, trade_date: str) -> list:
        """查询指定交易日尚未完结（开放）的订单意图。

        开放口径：status ∈ {EXECUTION_PENDING, SUBMITTED, PARTIALLY_FILLED}，
        按 created_at / id 升序返回 list[dict]。
        """
        _require_nonempty_str(trade_date, "trade_date")
        placeholders = ",".join("?" * len(_OPEN_INTENT_STATUSES))
        return self._fetch_all(
            f"SELECT * FROM order_intents WHERE trade_date = ? AND status IN ({placeholders}) "
            "ORDER BY created_at, intent_key",
            (trade_date, * _OPEN_INTENT_STATUSES),
        )

    # ---------------- 5) 券商委托 ----------------
    def upsert_broker_order(self, order_id: str, intent_key: str, trade_date: str,
                            symbol: str, action: str, quantity: int,
                            price_type: str, status: str, submitted_at: str,
                            price: float | None = None,
                            raw_response: str | None = None) -> bool:
        """写入（或修订）券商委托（追加式修订：order_id 主键，最新状态覆盖当日行）。

        同一 order_id 重复投递（状态更新/回报修订）走 REPLACE，行数保持为 1。
        返回 True（写入成功）。
        """
        _require_nonempty_str(order_id, "order_id")
        _require_nonempty_str(intent_key, "intent_key")
        trade_date = _require_nonempty_str(trade_date, "trade_date")
        _require_nonempty_str(symbol, "symbol")
        _require_nonempty_str(action, "action")
        quantity = _require_int(quantity, "quantity")
        _require_nonempty_str(price_type, "price_type")
        _require_nonempty_str(status, "status")
        _require_nonempty_str(submitted_at, "submitted_at")
        self._conn.execute(
            "INSERT OR REPLACE INTO broker_orders "
            "(order_id, intent_key, trade_date, symbol, action, quantity, price_type, "
            " price, status, submitted_at, raw_response) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, intent_key, trade_date, symbol, action, quantity, price_type,
             price, status, submitted_at, raw_response),
        )
        self._conn.commit()
        return True

    # ---------------- 6) 成交回报 ----------------
    def insert_fill(self, fill_id: str, order_id: str, trade_date: str, symbol: str,
                    fill_qty: int, fill_price: float, fee: float | None = None,
                    fill_time: str | None = None, raw: str | None = None) -> bool:
        """写入成交回报（fill_id 唯一幂等，重复回报忽略）。

        fill_id 已存在 → 返回 False（成交历史不可覆盖）；新增成功 → 返回 True。
        """
        _require_nonempty_str(fill_id, "fill_id")
        _require_nonempty_str(order_id, "order_id")
        trade_date = _require_nonempty_str(trade_date, "trade_date")
        _require_nonempty_str(symbol, "symbol")
        fill_qty = _require_int(fill_qty, "fill_qty")
        fill_price = float(fill_price)
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO fills "
            "(fill_id, order_id, trade_date, symbol, fill_qty, fill_price, fee, "
            " fill_time, raw) VALUES (?,?,?,?,?,?,?,?,?)",
            (fill_id, order_id, trade_date, symbol, fill_qty, fill_price, fee,
             fill_time, raw),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_fills(self, trade_date: str) -> list:
        """查询指定交易日的全部成交回报（按 fill_time / id 升序）。"""
        _require_nonempty_str(trade_date, "trade_date")
        return self._fetch_all(
            "SELECT * FROM fills WHERE trade_date = ? ORDER BY fill_time, fill_id",
            (trade_date,),
        )

    # ---------------- 7) 组合快照 ----------------
    def snapshot_portfolio(self, trade_date: str, nav: float, position_qty: int,
                           position_mv: float | None = None,
                           available_cash: float | None = None,
                           available_to_sell_qty: int | None = None,
                           created_at: str | None = None) -> bool:
        """写入（或覆盖当日）组合快照（trade_date 唯一，当日最新生效）。

        返回 True（写入成功）。
        """
        trade_date = _require_nonempty_str(trade_date, "trade_date")
        nav = float(nav)
        if nav <= 0:
            raise ValueError(f"nav 必须为正数，收到 {nav!r}")
        position_qty = _require_int(position_qty, "position_qty")
        if available_to_sell_qty is not None:
            available_to_sell_qty = _require_int(available_to_sell_qty,
                                                 "available_to_sell_qty")
        self._conn.execute(
            "INSERT OR REPLACE INTO portfolio_snapshots "
            "(trade_date, nav, position_qty, position_mv, available_cash, "
            " available_to_sell_qty, created_at) VALUES (?,?,?,?,?,?,?)",
            (trade_date, nav, position_qty, position_mv, available_cash,
             available_to_sell_qty, created_at or _now()),
        )
        self._conn.commit()
        return True

    def get_snapshots(self, limit: int) -> list:
        """查询最近 limit 条组合快照（按 trade_date 降序）。"""
        limit = _require_int(limit, "limit")
        if limit <= 0:
            raise ValueError(f"limit 必须为正整数，收到 {limit!r}")
        return self._fetch_all(
            "SELECT * FROM portfolio_snapshots ORDER BY trade_date DESC LIMIT ?",
            (limit,),
        )

    # ---------------- 8) 日终对账 ----------------
    def upsert_reconciliation(self, trade_date: str, target_qty: int, actual_qty: int,
                              deviation: int, filled_qty: int, unfilled_qty: int,
                              notes: str | None = None,
                              created_at: str | None = None) -> bool:
        """写入（或覆盖当日）日终对账（trade_date 唯一，当日最新生效）。

        返回 True（写入成功）。
        """
        trade_date = _require_nonempty_str(trade_date, "trade_date")
        target_qty = _require_int(target_qty, "target_qty")
        actual_qty = _require_int(actual_qty, "actual_qty")
        deviation = _require_int(deviation, "deviation")
        filled_qty = _require_int(filled_qty, "filled_qty")
        unfilled_qty = _require_int(unfilled_qty, "unfilled_qty")
        self._conn.execute(
            "INSERT OR REPLACE INTO daily_reconciliations "
            "(trade_date, target_qty, actual_qty, deviation, filled_qty, "
            " unfilled_qty, notes, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (trade_date, target_qty, actual_qty, deviation, filled_qty,
             unfilled_qty, notes, created_at or _now()),
        )
        self._conn.commit()
        return True

    # ---------------- 9) 系统事件（仅追加） ----------------
    def add_event(self, event: str, ts: str | None = None,
                  trade_date: str | None = None, timepoint: str | None = None,
                  level: str = "INFO", detail: str | None = None) -> bool:
        """追加系统事件（append-only，不覆盖不删除）。

        参数：
          event: 事件名称（短英文标识，如 DECISION_CREATED）
          ts: 事件时间（缺省当前时间）
          trade_date/timepoint: 关联交易日期 / 时点（可空）
          level: 级别 DEBUG/INFO/WARN/ERROR（缺省 INFO）
          detail: 事件详情（可空，中文描述）
        """
        _require_nonempty_str(event, "event")
        if level not in _EVENT_LEVELS:
            raise ValueError(f"level={level!r} 非法；合法级别 {_EVENT_LEVELS}")
        self._conn.execute(
            "INSERT INTO system_events (ts, trade_date, timepoint, level, event, detail) "
            "VALUES (?,?,?,?,?,?)",
            (ts or _now(), trade_date, timepoint, level, event, detail),
        )
        self._conn.commit()
        return True

    # ---------------- 生命周期 ----------------
    def close(self) -> None:
        """关闭数据库连接（幂等，可重复调用）。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def db_path(self) -> str:
        """当前数据库路径（":memory:" 或文件路径）。"""
        return self._db_path

    @property
    def is_closed(self) -> bool:
        """连接是否已关闭。"""
        return self._conn is None


# ==================== 自测（:memory: 全流程 CRUD） ====================
def _self_test() -> None:
    """:memory: 库跑全流程 CRUD + 幂等去重 + 状态转换 WHERE 守卫。"""
    db = PaperDB.connect(":memory:")
    print(f"[0] 建库（:memory:）journal_mode={db._conn.execute('PRAGMA journal_mode').fetchone()[0]}"
          f"（memory 属预期）；busy_timeout="
          f"{db._conn.execute('PRAGMA busy_timeout').fetchone()[0]}；foreign_keys="
          f"{db._conn.execute('PRAGMA foreign_keys').fetchone()[0]}")

    # --- 1) signal_snapshots：投递 + 覆盖 + previous_rank 派生 ---
    db.insert_signal(trade_date="20260801", current_rank=0.35, previous_rank=None,
                     metric_value=58.2, history_count=60, formal_usable=1,
                     source_contract_version="emotion-v1", known_at="2026-08-01 09:26:10",
                     run_id="run-0801", data_hash="h1")
    db.insert_signal(trade_date="20260802", current_rank=0.55, previous_rank=0.35,
                     metric_value=66.4, history_count=60, formal_usable=1,
                     source_contract_version="emotion-v1", known_at="2026-08-02 09:26:05",
                     run_id="run-0802", data_hash="h2")
    db.insert_signal(trade_date="20260803", current_rank=0.30, previous_rank=None,
                     metric_value=41.0, history_count=60, formal_usable=0,  # 不合格
                     source_contract_version="emotion-v1", known_at="2026-08-03 09:26:00")
    # 当日重复投递 → 覆盖（追加式修订：当日行被新版本替换）
    db.insert_signal(trade_date="20260803", current_rank=0.31, previous_rank=None,
                     metric_value=42.0, history_count=60, formal_usable=1,  # 修订为合格
                     source_contract_version="emotion-v1", known_at="2026-08-03 09:27:20",
                     run_id="run-0803b", data_hash="h3b")
    assert db._count("signal_snapshots") == 3, "signal 行数应为 3（当日覆盖非新增）"
    latest = db.get_latest_qualified_signal("20260804")
    assert latest is not None and latest["trade_date"] == "20260803", latest
    assert latest["current_rank"] == 0.31, latest  # 覆盖后的最新值生效
    prev = db.get_latest_qualified_signal("20260802")
    assert prev is not None and prev["trade_date"] == "20260801", prev  # 严格早于，不含当日
    assert prev["current_rank"] == 0.35  # previous_rank 派生口径
    print("[1] signal_snapshots：投递/当日覆盖/合格过滤/严格早于派生 previous_rank ✓")

    # --- 2) trend_snapshots：写入 + 覆盖 + 读取 ---
    db.insert_trend(trade_date="20260803", ma5=1.21, ma10=1.19, ma20=1.17,
                    bar_count=120, last_bar_date="20260803", known_at="2026-08-03 09:25:40")
    db.insert_trend(trade_date="20260803", ma5=1.22, ma10=1.19, ma20=1.17,  # 当日修订
                    bar_count=120, last_bar_date="20260803", known_at="2026-08-03 09:26:00")
    trend = db.get_trend("20260803")
    assert trend is not None and trend["ma5"] == 1.22, trend
    assert db.get_trend("20260801") is None  # 无记录 -> None
    print("[2] trend_snapshots：写入/当日覆盖/读取 ✓")

    # --- 3) strategy_decisions：create_decision 幂等不可覆盖 ---
    did = f"{STRATEGY_ID}:20260803"
    ok = db.create_decision(decision_id=did, trade_date="20260803",
                            previous_rank=0.55, current_rank=0.31,
                            ma5=1.22, ma10=1.19, ma20=1.17,
                            previous_target=0.0, desired_target=0.0,
                            reason_code="WEAK_STATE_CONFIRMED",
                            signal_known_at="2026-08-03 09:27:20")
    assert ok is True, "首次 create_decision 应返回 True"
    dup = db.create_decision(decision_id=did, trade_date="20260803",
                             previous_rank=0.55, current_rank=0.99,
                             ma5=9.9, ma10=9.9, ma20=9.9,
                             previous_target=0.0, desired_target=1.0,
                             reason_code="HACKED")
    assert dup is False, "重复 create_decision 应返回 False"
    row = db._fetch_one("SELECT * FROM strategy_decisions WHERE decision_id = ?", (did,))
    assert row is not None and row["desired_target"] == 0.0, row  # 原记录未被覆盖
    assert row["reason_code"] == "WEAK_STATE_CONFIRMED", row
    print("[3] strategy_decisions：create_decision 首次 True / 重复 False / 内容不可覆盖 ✓")

    # --- 4) order_intents：create_intent 幂等去重 ---
    ik = f"{STRATEGY_ID}:{STRATEGY_VERSION}:20260803:{SYMBOL}:0.0"
    ok = db.create_intent(intent_key=ik, decision_id=did, trade_date="20260803",
                          symbol=SYMBOL, desired_target=0.0, action=ACTION_SELL_ALL,
                          target_qty=0, delta_qty=-50000, price_type="market")
    assert ok is True, "首次 create_intent 应返回 True"
    dup = db.create_intent(intent_key=ik, decision_id=did, trade_date="20260803",
                           symbol=SYMBOL, desired_target=1.0, action=ACTION_BUY_FULL,
                           target_qty=60000, delta_qty=60000, price_type="market")
    assert dup is False, "重复 create_intent（同 intent_key）应返回 False"
    print("[4] order_intents：create_intent 首次 True / 重复 False（幂等去重）✓")

    # --- 5) transition_intent_status：WHERE status = old 守卫 ---
    assert db.transition_intent_status(ik, old=EXECUTION_PENDING, new=SUBMITTED) is True
    assert db.transition_intent_status(ik, old=EXECUTION_PENDING, new=SUBMITTED) is False, \
        "old 不匹配（已为 SUBMITTED）→ False"
    assert db.transition_intent_status(ik, old=SUBMITTED, new=FILLED) is True
    assert db.transition_intent_status(ik, old=FILLED, new=RECONCILED) is True
    assert db.transition_intent_status(ik, old=UNFILLED, new=RECONCILED) is False, \
        "old=UNFILLED 与当前 FILLED 不匹配 → False"
    intent_row = db._fetch_one("SELECT * FROM order_intents WHERE intent_key = ?", (ik,))
    assert intent_row["status"] == RECONCILED, intent_row
    print("[5] transition_intent_status：WHERE 守卫（old 不匹配返回 False）✓")

    # --- 6) broker_orders：upsert 修订同 order_id 保持单行 ---
    db.upsert_broker_order(order_id="MX20260803001", intent_key=ik, trade_date="20260803",
                           symbol=SYMBOL, action="sell", quantity=50000,
                           price_type="market", status=SUBMITTED,
                           submitted_at="2026-08-03 14:52:10",
                           raw_response='{"orderId":"MX20260803001"}')
    db.upsert_broker_order(order_id="MX20260803001", intent_key=ik, trade_date="20260803",
                           symbol=SYMBOL, action="sell", quantity=50000,
                           price_type="market", status=FILLED,
                           submitted_at="2026-08-03 14:52:10",
                           raw_response='{"orderId":"MX20260803001","status":"filled"}')
    assert db._count("broker_orders") == 1, "同 order_id 修订后行数应保持 1"
    order_row = db._fetch_one("SELECT * FROM broker_orders WHERE order_id = ?",
                              ("MX20260803001",))
    assert order_row["status"] == FILLED, order_row  # 最新状态生效
    print("[6] broker_orders：upsert 修订（同 order_id 覆盖当日行，行数不变）✓")

    # --- 7) fills：fill_id 幂等去重 + 按日查询 ---
    assert db.insert_fill(fill_id="FL20260803001", order_id="MX20260803001",
                          trade_date="20260803", symbol=SYMBOL,
                          fill_qty=50000, fill_price=1.18, fee=5.9,
                          fill_time="2026-08-03 14:53:02",
                          raw='{"fillId":"FL20260803001"}') is True
    assert db.insert_fill(fill_id="FL20260803001", order_id="MX20260803001",
                          trade_date="20260803", symbol=SYMBOL,
                          fill_qty=99999, fill_price=9.9) is False, \
        "重复 fill_id 应被忽略（成交历史不可覆盖）"
    fills = db.get_fills("20260803")
    assert len(fills) == 1 and fills[0]["fill_qty"] == 50000, fills
    assert db.get_fills("20260804") == [], "无成交日返回空列表"
    print("[7] fills：fill_id 幂等去重 / get_fills 按日查询 ✓")

    # --- 8) portfolio_snapshots：当日覆盖 + 取最近 N 条 ---
    db.snapshot_portfolio(trade_date="20260803", nav=100500.0, position_qty=0,
                          position_mv=0.0, available_cash=100500.0,
                          available_to_sell_qty=0)
    db.snapshot_portfolio(trade_date="20260803", nav=100800.0, position_qty=0,  # 当日修订
                          position_mv=0.0, available_cash=100800.0,
                          available_to_sell_qty=0)
    db.snapshot_portfolio(trade_date="20260804", nav=101000.0, position_qty=0,
                          position_mv=0.0, available_cash=101000.0,
                          available_to_sell_qty=0)
    snaps = db.get_snapshots(1)
    assert len(snaps) == 1 and snaps[0]["trade_date"] == "20260804", snaps
    snaps2 = db.get_snapshots(10)
    assert len(snaps2) == 2 and snaps2[0]["trade_date"] == "20260804", snaps2
    day3 = db._fetch_one("SELECT * FROM portfolio_snapshots WHERE trade_date = '20260803'")
    assert day3["nav"] == 100800.0, day3  # 当日覆盖生效
    print("[8] portfolio_snapshots：当日覆盖 / get_snapshots(limit) 降序 ✓")

    # --- 9) daily_reconciliations：当日覆盖 ---
    db.upsert_reconciliation(trade_date="20260803", target_qty=0, actual_qty=0,
                             deviation=0, filled_qty=50000, unfilled_qty=0,
                             notes="SELL_ALL 已成交")
    db.upsert_reconciliation(trade_date="20260803", target_qty=0, actual_qty=0,
                             deviation=0, filled_qty=50000, unfilled_qty=0,
                             notes="对账通过")  # 当日修订
    rc = db._fetch_one("SELECT * FROM daily_reconciliations WHERE trade_date = '20260803'")
    assert rc["notes"] == "对账通过", rc
    assert db._count("daily_reconciliations") == 1
    print("[9] daily_reconciliations：upsert 当日覆盖 ✓")

    # --- 10) system_events：仅追加 ---
    db.add_event(event="DECISION_CREATED", trade_date="20260803",
                 timepoint="09:27", level="INFO", detail="决策已落库")
    db.add_event(event="ORDER_SUBMITTED", trade_date="20260803",
                 timepoint="14:52", level="INFO", detail="委托已提交")
    db.add_event(event="RECONCILE_OK", trade_date="20260803",
                 timepoint="15:05", level="INFO", detail="对账一致")
    try:
        db.add_event(event="BAD_LEVEL", level="FATAL")  # 非法级别
        raise AssertionError("非法级别应抛 ValueError")
    except ValueError:
        pass
    assert db._count("system_events") == 3
    print("[10] system_events：仅追加 / 非法级别 ValueError ✓")

    # --- 11) get_open_intents 过滤口径 ---
    db.create_intent(intent_key=f"{STRATEGY_ID}:{STRATEGY_VERSION}:20260804:{SYMBOL}:1.0",
                     decision_id=f"{STRATEGY_ID}:20260804", trade_date="20260804",
                     symbol=SYMBOL, desired_target=1.0, action=ACTION_BUY_FULL,
                     target_qty=60000, delta_qty=60000, price_type="market")
    open_intents = db.get_open_intents("20260804")
    assert len(open_intents) == 1 and open_intents[0]["status"] == EXECUTION_PENDING, \
        open_intents
    assert db.get_open_intents("20260803") == []  # 已 RECONCILED，不再开放
    print("[11] get_open_intents：非终态过滤（EXECUTION_PENDING/SUBMITTED/PARTIALLY_FILLED）✓")

    # --- 12) 中文 ValueError 与关闭 ---
    try:
        db.create_decision(decision_id="", trade_date="20260805")
        raise AssertionError("空 decision_id 应抛 ValueError")
    except ValueError as exc:
        assert "decision_id" in str(exc)
    db.close()
    assert db.is_closed
    print("[12] 中文 ValueError / close 幂等 ✓")

    print("== paper_db 自测全部通过 ✓ ==")


if __name__ == "__main__":
    _self_test()
