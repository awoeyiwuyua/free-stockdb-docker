"""storage.warehouse.reconcile — 沉淀对账（0.10.0 W4；验收三板斧的数据层执行）。

三板斧：
  a) 行数对账：分区行数 == 当日快照 TRADED 数（sink 拒写数另计，不混入误差）
  b) 字段级对账（同源回读）：抽样点 vs Parquet 分区逐字段相等
  c) 异源对账：抽样点 vs 公网源（腾讯/东财，经 quote_sources）开盘价/昨收核对

纯函数：输入快照行/异源行，输出 issues 列表——编排与告警在服务层。
"""
from __future__ import annotations

from pathlib import Path

from . import layout

# 数值字段（字段级对账口径；引擎原样镜像，无单位换算）
_FIELDS = ("open", "high", "low", "close", "prev_close", "volume", "amount")
# 异源可比字段（公网源只有 open_price/prev_close/volume/amount）
_EXTERNAL_FIELDS = ("open", "prev_close")
_REL_TOL = 1e-6      # 同源回读：二进制往返后相对误差容限
_EXTERNAL_TOL = 5e-3  # 异源：不同上游的舍入/单位差异容限（0.5%）


def _close(a, b, rel_tol) -> bool:
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if a == b:
        return True
    return abs(a - b) <= rel_tol * max(abs(a), abs(b), 1e-12)


def _read_partition_rows(root: Path, date: str, market: str) -> dict[str, dict]:
    """分区文件 → {code: {字段: 值}}（独立连接读，验"落盘可读"）。"""
    import duckdb

    path = layout.daily_partition(root, date, market)
    if not path.exists():
        return {}
    con = duckdb.connect()
    try:
        cols = ", ".join(("code",) + _FIELDS)
        rows = con.execute(
            f"SELECT {cols} FROM read_parquet('{path.as_posix()}')"
        ).fetchall()
    finally:
        con.close()
    names = ("code",) + _FIELDS
    return {r[0]: dict(zip(names[1:], r[1:])) for r in rows}


def reconcile_daily(root: Path, date: str, traded_points: list[dict],
                    sedimented_rows: int, dropped_nonfinite: int,
                    external_rows: list[dict] | None = None,
                    sample: int = 10) -> dict:
    """对账入口。

    traded_points：当日快照 TRADED 行（引擎源）；sedimented_rows/dropped_nonfinite：
    sink 返回值；external_rows：quote_sources 采集行（{code, open_price, prev_close,...}）。
    返回 {ok, issues: [{kind, detail}...], checked, sampled}；issues 空 = 全绿。
    """
    date = layout.normalize_date(date)
    issues: list[dict] = []

    # a) 行数对账（分区合计；dropped（NaN/Inf 拒写）为已知扣减项）
    total = 0
    by_market: dict[str, list[dict]] = {}
    for p in traded_points:
        by_market.setdefault(layout.market_of(p.get("code", "")), []).append(p)
    for market in sorted(by_market):
        total += len(_read_partition_rows(root, date, market))
    if total + dropped_nonfinite != len(traded_points):
        issues.append({
            "kind": "row_count",
            "detail": f"stored={total} + dropped={dropped_nonfinite} "
                      f"!= traded={len(traded_points)}",
        })

    # b) 字段级（同源回读，抽样）
    checked = 0
    step = max(1, len(traded_points) // max(1, sample))
    for p in traded_points[::step][:sample]:
        code = str(p.get("code") or "")
        market = layout.market_of(code)
        stored = _read_partition_rows(root, date, market).get(code)
        if stored is None:
            issues.append({"kind": "missing_code", "detail": f"{code} 未落盘"})
            continue
        for f in _FIELDS:
            if not _close(p.get(f), stored.get(f), _REL_TOL):
                issues.append({
                    "kind": "field_mismatch",
                    "detail": f"{code}.{f}: snapshot={p.get(f)} stored={stored.get(f)}",
                })
        checked += 1

    # c) 异源（公网源可比字段）
    ext_checked = 0
    for e in (external_rows or []):
        code = str(e.get("code") or "")
        market = layout.market_of(code)
        stored = _read_partition_rows(root, date, market).get(code)
        if stored is None or e.get("open_price") is None:
            continue
        for f in _EXTERNAL_FIELDS:
            ext_val = e.get("open_price") if f == "open" else e.get("prev_close")
            if ext_val is None:
                continue
            if not _close(ext_val, stored.get(f), _EXTERNAL_TOL):
                issues.append({
                    "kind": "external_mismatch",
                    "detail": f"{code}.{f}: external={ext_val} stored={stored.get(f)}",
                })
        ext_checked += 1

    return {
        "ok": not issues,
        "issues": issues,
        "checked": checked,
        "external_checked": ext_checked,
        "traded": len(traded_points),
        "stored": total,
        "dropped": dropped_nonfinite,
    }
