"""storage.warehouse.layout — 分区路径与市场归类（0.10.0 W2，D12）。

磁盘布局：
  <root>/facts/daily/year=YYYY/market=sh/date=YYYYMMDD.parquet   日K（按日一文件，内按 code 排序）
  <root>/facts/adjust/snapshot=YYYYMMDD.parquet                  复权因子（低频全量快照，版本化追加）
  <root>/warehouse.duckdb                                        视图/宏 + research schema + meta 表
  <root>/backups/                                                warehouse.duckdb 备份

文件粒度选「年/市场/日」而非「每标的一文件」：全市场日K约 5000 行/日，
按日成文件既保持只增不改的追加语义，又避免每年数千小文件拖慢全表扫描；
单标的时序查询靠文件内 code 排序 + Parquet 行组统计裁剪。

市场归类与 app._classify_code 同域（交易所维度 sh/sz/bj；hk 留作将来港股数据集）。
"""
from __future__ import annotations

import re
from pathlib import Path

import config

_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")

# 交易所前缀表（2-3 位代码段 → 市场）
_SH_PREFIXES = ("50", "51", "52", "56", "58", "60", "68")  # 沪主板/科创板/沪 ETF·LOF
_SZ_PREFIXES = ("00", "15", "16", "18", "30")  # 深主板/创业板/深 ETF·LOF·REITs
_BJ_PREFIXES = ("43", "83", "87", "88", "92")  # 北交所


def root_dir() -> Path:
    """仓库根（动态读 config，测试可 patch config.WAREHOUSE_DIR）。"""
    return Path(config.WAREHOUSE_DIR)


def normalize_date(value) -> str:
    """"2026-08-22" / "20260822" / 20260822 → "20260822"（8 位，布局与 watermark 统一口径）。"""
    s = str(value).replace("-", "").strip()
    if not _DATE_RE.match(s):
        raise ValueError(f"invalid date: {value!r}")
    return s


def iso_date(value) -> str:
    """任意合法日期输入 → "YYYY-MM-DD"（DuckDB DATE 列格式）。"""
    d = normalize_date(value)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def market_of(code) -> str:
    """代码 → 市场分区名（sh/sz/bj/hk/other）。"""
    c = str(code).strip().lower()
    if c.startswith("hk"):
        c = c[2:]
    if c.isdigit() and len(c) == 5:
        return "hk"
    if c[:2] in _SH_PREFIXES:
        return "sh"
    if c[:2] in _SZ_PREFIXES:
        return "sz"
    if c[:3] == "430" or c[:2] in _BJ_PREFIXES:
        return "bj"
    return "other"


def facts_dir(root: Path) -> Path:
    return Path(root) / "facts"


def duckdb_path(root: Path) -> Path:
    return Path(root) / "warehouse.duckdb"


def backups_dir(root: Path) -> Path:
    return Path(root) / "backups"


def daily_partition(root: Path, date, market: str) -> Path:
    """日K分区文件路径：facts/daily/year=YYYY/market=xx/date=YYYYMMDD.parquet（按市场分目录）。"""
    d = normalize_date(date)
    code_dir = facts_dir(root) / "daily" / f"year={d[:4]}" / f"market={market}"
    return code_dir / f"date={d}.parquet"


def adjust_snapshot_path(root: Path, date) -> Path:
    """复权因子快照路径：facts/adjust/snapshot=YYYYMMDD.parquet（全量刷新、版本化追加）。"""
    d = normalize_date(date)
    return facts_dir(root) / "adjust" / f"snapshot={d}.parquet"


def list_daily_dates(root: Path) -> list[str]:
    """已沉淀的交易日清单（按日期去重——同日多市场多文件；升序；无分区返回 []）。"""
    daily = facts_dir(root) / "daily"
    if not daily.is_dir():
        return []
    return sorted({f.stem[len("date="):] for f in daily.rglob("date=*.parquet")})
