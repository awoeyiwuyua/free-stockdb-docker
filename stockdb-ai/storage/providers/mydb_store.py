"""storage.providers.mydb_store — mydb 私有存储读写（0.9.2 批次 3 从 app.py 搬迁）。

上游 stockdb 内置私有存储 ./mydb：HTTP 层只读，写入须用 pybao 客户端
（stockdb.abi3.so + stock_sdk.py，随发行包分发，PYTHONPATH 注入）。
本机开发若未装 pybao，相关接口优雅降级（A 股功能不受影响）。
行为与 app.py 搬迁前完全一致（0.8.10 起：锁 + 自愈重连 + 值归一化）。

职责（0.9.5 研究成果迁自持 SQLite 后收窄）：hk日k 港股日K、用户自定义表
（/api/data/write 开放命名空间）、RESEARCH_STORE=mydb 回滚写回——
打板指标/序列/清单/竞价快照主线已迁 storage/research_store.py。
"""
from __future__ import annotations

import importlib
import json
import sys
import threading

import config  # 模块引用（config.STOCKDB_HOST/PORT 动态读取，测试 patch 生效）

# 保留表前缀：禁止覆盖上游同步数据，防止与 A 股行情冲突
_RESERVED_TABLES = ("日k", "分钟k", "复权", "股票代码", "周k", "月k", "板块", "行业", "概念")


def _mydb_import():
    """惰性导入 pybao 客户端。未安装/加载失败时抛 ImportError（调用方降级）。

    候选路径：容器内 /opt/stockdb/pybao，本地开发 /tmp/pybao_mac 等；
    本机开发经 PYTHONPATH 注入原生 pybao 目录（见 docs/development-guide.md）。
    """
    candidates = ["/opt/stockdb/pybao", "/tmp/pybao_mac"]
    for p in candidates:
        try:
            sys.path.insert(0, p)
            return importlib.import_module("stockdb")
        except ImportError:
            continue
    raise ImportError("pybao 写库不可用（PYTHONPATH 未注入或平台不兼容）")


_rd_lock = threading.RLock()  # pybao rd 单连接非线程安全：全部 rd 读写持锁串行化（0.8.10）
# 事故背景 2026-08-16：/api/data/* 与回填线程并发用同一 socket，协议帧交错 →
# C 扩展阻塞持 GIL → 全进程冻结。锁保证同一时刻只有一条 rd 请求在线上。
# 0.9.11：改为 RLock 并让 _mydb_rd 初始化自身持锁——连接创建也串行化，且
# interfaces/mcp/pybao_tools 复用本锁（同进程容器与 MCP 共享同一底层连接）。


def _mydb_rd_reset():
    """丢弃缓存的 rd 连接：调用失败后置空，下次调用重新 init（0.8.10 自愈楔死连接）。"""
    with _rd_lock:
        _mydb_rd._rd = None


def _mydb_rd():
    """获取连接 stockdb 的 pybao 客户端（惰性、缓存，函数对象属性持连接）。

    0.9.11：初始化自身持 _rd_lock（RLock 可重入，调用方再持锁不冲突）——
    首次并发访问不再产生双连接（socket 泄漏）；调用方（mydb_read/write/tables
    及 pybao_tools.rd_get/rd_keys）仍应持锁执行请求。
    """
    with _rd_lock:
        if getattr(_mydb_rd, "_rd", None) is None:
            mod = _mydb_import()
            _mydb_rd._rd = mod.init(config.STOCKDB_HOST, int(config.STOCKDB_PORT),
                                    socket_timeout=5)
        return _mydb_rd._rd


def _rd_to_py(v):
    """pybao 返回值归一化（0.8.10 修复）：dict 原样；JSON 字符串解析；QueryResult 转 dict。

    与 mcp._auction_value_to_dict 语义对齐：任何形态一律转 dict，失败按缺失处理——
    旧实现转换失败原样返回 QueryResult，json.dumps 直接崩（"Object of type
    QueryResult is not JSON serializable"，/api/data/read 实证）。
    """
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    if hasattr(v, "keys") and hasattr(v, "all"):
        try:  # pybao QueryResult：dict(value) 即原生数据
            return dict(v)
        except Exception:  # noqa: BLE001 - 转换失败按缺失处理
            return None
    return None


def validate_custom_table(table: str) -> str:
    """校验自定义表名：禁止覆盖上游保留表，禁止危险字符。返回规范化表名。"""
    t = str(table or "").strip().strip(":")
    if not t:
        raise ValueError("表名不能为空")
    if not all(ch.isalnum() or ch in "_:-" for ch in t):
        raise ValueError("表名只能含字母数字与 _:-")
    for r in _RESERVED_TABLES:
        if t == r or t.startswith(r + ":"):
            raise ValueError(f"表名 {t!r} 与上游保留表 {r!r} 冲突，请用自定义命名空间（如 hk日k: / 自定义:）")
    return t


def _has_nan_inf(value) -> bool:
    """递归检查 value 中是否含 NaN/Inf 浮点（0.9.4 写前护栏）。

    pybao 存原生 dict 时 NaN/Inf 会导致序列化失败/脏数据——写入前拦截。
    """
    import math
    if isinstance(value, float):
        return math.isnan(value) or math.isinf(value)
    if isinstance(value, list):
        return any(_has_nan_inf(v) for v in value)
    if isinstance(value, dict):
        return any(_has_nan_inf(v) for v in value.values())
    return False


def mydb_write(table: str, items: list[tuple], batch: bool = False) -> dict:
    """写入 mydb 私有存储。items=[(key, value), ...]。

    注意：pybao 的 rd.set 返回 QueryResult，必须调用 .do() 才真正发送写入
    （否则只是客户端排队，读不到）。batch 参数保留兼容，统一逐条 .do()。
    0.8.10：全程持 _rd_lock；任何 rd 异常 → 丢弃缓存连接（下次调用重连自愈）。
    0.9.4：写前护栏——含 NaN/Inf 的条目剔除并计数（skipped_invalid），不落盘
    （拦截脏数据污染研究资产）；全部被拦截 → ValueError（调用方告警）。
    """
    table = validate_custom_table(table)
    clean: list[tuple] = []
    skipped = 0
    for key, value in items:
        if _has_nan_inf(value):
            skipped += 1
            continue
        clean.append((key, value))
    if not clean:
        raise ValueError(f"没有可写入的数据（{skipped} 条被 NaN/Inf 护栏拦截）")
    with _rd_lock:
        try:
            rd = _mydb_rd()
            result = []
            for key, value in clean:
                result.append(rd.set(table, key, value).do())
            # 回读校验（QueryResult 转原生）
            readback = []
            for key, _ in clean:
                try:
                    readback.append(_rd_to_py(rd.get(table, key)))
                except Exception:  # noqa: BLE001 - 单键回读失败按缺失
                    readback.append(None)
            return {"table": table, "written": len(clean), "skipped_invalid": skipped,
                    "readback": readback, "result": result}
        except Exception:
            _mydb_rd_reset()
            raise


def mydb_read(table: str, key: str = "") -> dict:
    """读取 mydb 自定义表。key 为空时列出表内全部键值。
    0.8.10：持 _rd_lock；值统一 _rd_to_py 归一化；rd 异常 → 丢弃连接自愈。
    0.9.12：全表列取改细粒度持锁——keys 枚举一次持锁，逐键 get 每次独立持锁。
    此前 500 键连续持锁（引擎慢时 25s+），MCP 快速通道/打板任务等全部 rd 访问
    排队 → 点击多时 webui 假死。面板展示对中间态不敏感，可接受。"""
    table = validate_custom_table(table)
    if key:
        with _rd_lock:
            try:
                rd = _mydb_rd()
                val = _rd_to_py(rd.get(table, key))
                return {"table": table, "key": key, "value": val}
            except Exception:
                _mydb_rd_reset()
                raise
    with _rd_lock:
        try:
            rd = _mydb_rd()
            keys = rd.keys(table, "*") or []
        except Exception:
            _mydb_rd_reset()
            raise
    values = {}
    for k in keys:
        # 0.9.11：复合键解析（split(":", 1) 保留代码段）——键形如
        # "hk日k:00700:20250425"，此前 split(":")[-1] 只取日期段，
        # 同一日期多只股票时读出错误记录/读不到（pybao_tools.query_mydb
        # 已修同款，app 侧同步）
        full = str(k)
        lookup_key = full.split(":", 1)[-1] if ":" in full else full
        try:
            with _rd_lock:  # 0.9.12：逐键独立持锁（细粒度，见函数注释）
                values[full] = _rd_to_py(_mydb_rd().get(table, lookup_key))
        except Exception:  # noqa: BLE001 - 单键失败按缺失
            values[full] = None
    return {"table": table, "keys": keys, "values": values}


def mydb_tables() -> list[str]:
    """列出自定义表名（含保留表前缀过滤）。0.8.10：rd.keys 持锁 + 异常自愈。"""
    with _rd_lock:
        try:
            rd = _mydb_rd()
            keys = rd.keys("*") or []
        except Exception:
            _mydb_rd_reset()
            raise
    tables = set()
    for k in keys:
        table = str(k).split(":")[0] if ":" in str(k) else str(k)
        if table and not any(table.startswith(r) for r in _RESERVED_TABLES):
            tables.add(table)
    return sorted(tables)


# ---- 打板序列薄封装（0.9.2 批次 3 迁入；读/写链路由 services 层注入） ----

def auction_series_read(key: str):
    """打板序列读取封装：read_fn(key) -> 原始存储值|None（JSON 解析交给 load_series）。

    mydb 物理布局：表=打板序列:<metric>、子键="series"；mydb_read(key, "") 列出
    {子键: 值}，取任一非 None 值返回。读取异常 → None（冷启动空序列，不阻塞采集）。
    """
    try:
        res = mydb_read(key, "")
        for v in (res.get("values") or {}).values():
            if v is not None:
                return v
    except Exception:  # noqa: BLE001 - 序列缺失/损坏 → 调用方按空序列处理
        return None
    return None


def auction_series_write(key: str, value) -> None:
    """打板序列写入封装：write_fn(key, value)；子键固定 "series"，覆盖写幂等。"""
    mydb_write(key, [("series", value)])  # 0.8.6：pybao 存原生 dict
