#!/usr/bin/env python3
"""pybao_tools — pybao（技术指标/板块）能力封装，离线可降级。

本模块把外部 pybao（容器镜像自带 /opt/stockdb/pybao；本机开发放
/tmp/pybao_mac 或设置 PYBAO_DIR 环境变量）包装成统一接口，供
stockdb_mcp_server 的 get_indicators / get_board_members / get_kline(增强路径) 使用：

- get_pybao()       加载并返回 pybao 的 zhibiao 模块（含 jisuan/bk 入口），不可用返回 None
- get_sdk_client()  返回 pybao SDK client（复权/1m/1w/1M/批量 K 线数据源），不可用返回 None
- compute_indicators(args)  技术指标计算（参数校验 + 批量计算 + 结果加工）
- query_boards(args)        板块成员查询（双向：板块→成分股 / 股票→所属板块）

compute_indicators / query_boards 统一返回
{"ok": True, "result": {...}} 或 {"ok": False, "error": "中文错误"}。

依赖：纯 Python 标准库；pybao 为可选外部依赖，缺失时上述能力返回明确降级错误，
不影响 stockdb_mcp_server 其余 HTTP 工具。

双版本兼容：
- 新版（macOS /tmp/pybao_mac、容器 /opt/stockdb/pybao）：stock_sdk 提供
  init(host, port, warm=) 与 get_default_client()；zhibiao 提供
  reset_default_connection()。加载后调用 init 绑定当前端点。
- 旧版（仓库 pybao/ 参考拷贝）：stock_sdk.init 为旧签名，zhibiao 使用模块级
  client/rd/bk —— 加载后重绑定这三个符号到当前端点。
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

# 模块级 pybao 缓存：已加载的 zhibiao 模块。测试可直接注入假对象或
# mock.patch.object(pybao_tools, "get_pybao", return_value=fake)。
_zhibiao: object | None = None
_load_error: str | None = None  # 最近一次加载失败原因（诊断用）

# pybao 调用串行化锁：stock_sdk 的惰性连接初始化（_default_client 懒建）与
# C 扩展调用不做并发假设，webui 的 ThreadingHTTPServer 每请求一线程，
# 全部 pybao 业务调用持锁执行。
_PYBAO_LOCK = threading.Lock()

# 支持的技术指标白名单（39 项，与 pybao zb.get 支持集合一致；含 zhishu 指数）。
# 未知指标离线即可报错，不依赖 pybao 是否可用。
SUPPORTED_INDICATORS = frozenset({
    "macd", "kdj", "rsi", "wr", "bias", "boll", "psy", "cci", "atr",
    "bbi", "dmi", "taq", "ktn", "trix", "vr", "cr", "emv", "dpo",
    "brar", "dfma", "mtm", "mass", "roc", "expma", "obv", "mfi", "asi",
    "xsii", "ma", "ema", "sma", "wma", "dma", "std", "sum", "hhv", "llv",
    "ref", "zhishu",
})
# fields 仅对基础均线类指标生效（与 zhibiao.BASIC_INDICATORS 一致）
BASIC_INDICATORS = frozenset({
    "ma", "ema", "sma", "wma", "dma", "std", "sum", "hhv", "llv", "ref",
})
DATA_FIELDS = frozenset({"open", "high", "low", "close", "volume", "amount"})

# K 线/指标周期枚举（8 项），与 stockdb_mcp_server 的 get_kline / get_indicators 共用
FREQUENCIES = ("1d", "5m", "15m", "30m", "60m", "1m", "1w", "1M")

_INDICATOR_MAX = 8        # 单次最多指标数
_CODES_MAX = 50           # 单次最多股票数
_DEFAULT_LIMIT = 500      # 默认最多返回行数（每码）
_MAX_LIMIT = 1000         # 每码行数硬上限
_MAX_SYMBOLS = 500        # 板块成分股硬上限
_DEFAULT_START_DAYS = 60  # start 缺省 = 今天往前 60 个自然日

# 板块分类：zhibiao.CATEGORY_MAP = {0:概念, 1:申万一级, 2:申万二级, 3:申万三级}
BOARD_CATEGORIES = {
    0: "概念", 1: "申万一级", 2: "申万二级", 3: "申万三级",
    "0": "概念", "1": "申万一级", "2": "申万二级", "3": "申万三级",
    "概念": "概念", "申万一级": "申万一级", "申万二级": "申万二级", "申万三级": "申万三级",
}
DEFAULT_BOARD_FIELDS = "code,name,type,group,category"
BOARD_FIELDS = ("code", "name", "source", "type", "group", "category", "symbols")
FIELD_ALIASES = {"symbol": "symbols", "symbls": "symbols", "codelist": "symbols"}

PYBAO_UNAVAILABLE = (
    "pybao 不可用：容器镜像内自动携带（/opt/stockdb/pybao）；本机开发请把 "
    "macOS 版 pybao 放到 /tmp/pybao_mac 或设置 PYBAO_DIR 环境变量。"
)


# === pybao 定位与加载 ===


def _candidate_pybao_dirs() -> list[str]:
    """pybao 候选目录：PYBAO_DIR > 容器 /opt/stockdb/pybao > 本机 /tmp/pybao_mac > 仓库参考拷贝。"""
    dirs: list[str] = []
    env_dir = os.environ.get("PYBAO_DIR", "").strip()
    if env_dir:
        dirs.append(env_dir)
    dirs.append("/opt/stockdb/pybao")
    dirs.append("/tmp/pybao_mac")
    dirs.append(str(Path(__file__).resolve().parents[3] / "pybao"))
    return dirs


def _load_pybao() -> object | None:
    """定位并加载 pybao 的 zhibiao 模块，绑定当前端点；任何失败返回 None（不抛异常）。

    - 新版：stock_sdk.init(host, port, warm=False) 绑定端点 + zhibiao.reset_default_connection()
    - 旧版：重绑定 zhibiao.client / zhibiao.rd / zhibiao.bk
    """
    global _load_error
    for base in _candidate_pybao_dirs():
        zhibiao_path = Path(base) / "zhibiao.py"
        if not zhibiao_path.is_file():
            continue
        if str(base) not in sys.path:  # pybao 依赖同级模块（stock_sdk / C 扩展）
            sys.path.insert(0, str(base))
        try:
            import stock_sdk  # noqa: PLC0415 - 惰性导入，无 pybao 时本模块仍可 import
            import zhibiao  # noqa: PLC0415
            host = os.environ.get("STOCKDB_HOST", "127.0.0.1")
            port = int(os.environ.get("STOCKDB_PORT", "7899"))
            if hasattr(stock_sdk, "init"):
                try:
                    stock_sdk.init(host=host, port=port, warm=False)
                except TypeError:
                    stock_sdk.init(host=host, port=port)  # 旧版签名
            reset = getattr(zhibiao, "reset_default_connection", None)
            if callable(reset):
                reset()  # 丢弃旧端点板块缓存
            if not hasattr(zhibiao, "get_default_client"):
                # 旧版：模块级 client/rd/bk 重绑定到当前端点
                zhibiao.client = stock_sdk.StockDBClient(host=host, port=port)
                zhibiao.rd = zhibiao.client.rd
                zhibiao.bk = zhibiao.BoardIndex()
            _load_error = None
            return zhibiao
        except Exception as exc:  # noqa: BLE001 - 单个候选失败换下一个
            _load_error = f"{base}: {type(exc).__name__}: {exc}"
            continue
    return None


def get_pybao() -> object | None:
    """返回已加载的 pybao zhibiao 模块（幂等缓存），不可用返回 None。"""
    global _zhibiao
    if _zhibiao is not None:
        return _zhibiao
    _zhibiao = _load_pybao()
    return _zhibiao


def get_sdk_client() -> object | None:
    """返回绑定当前端点的 pybao SDK 客户端，不可用返回 None（不抛异常）。

    新版走 stock_sdk.get_default_client()（与 jisuan 内部取连接一致）；旧版取
    get_pybao() 重绑定后的 zhibiao.client。
    """
    module = get_pybao()
    if module is None:
        return None
    try:
        stock_sdk = sys.modules.get("stock_sdk")
        if stock_sdk is not None and hasattr(stock_sdk, "get_default_client"):
            return stock_sdk.get_default_client()
        return getattr(module, "client", None)
    except Exception:  # noqa: BLE001 - 客户端获取失败降级为 None
        return None


# === 通用加工 ===


def _as_int(value: object, default: int) -> int | None:
    """把 limit 等整型参数转 int；缺省返回 default，非法返回 None。"""
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: object, default: bool) -> bool:
    """宽松布尔转换：bool 原样；字符串按 1/true/yes/是 判定。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "是")
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _normalize_rows(data: object) -> list[dict]:
    """归一化 pybao 行数据 → dict 行列表。"""
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _to_columnar(rows: list[dict]) -> dict:
    """行列表 → 列式：{"dates": [...], <字段>: [...], ...}（省 token）。

    字段名按首次出现顺序收集；date 统一进 dates 列；跳过值为 None 的键。
    """
    field_order: list[str] = []
    for row in rows:
        for key, value in row.items():
            if value is None:
                continue
            if key not in field_order:
                field_order.append(key)
    out: dict = {"dates": [row.get("date") for row in rows]}
    for key in field_order:
        if key == "date":
            continue
        out[key] = [row.get(key) for row in rows]
    return out


def _normalize_boards(boards: object) -> list[dict]:
    """归一化 bk.get 返回值 → [{"name": str, "symbols": [str,...]}, ...]。

    bk.get 返回形态多样：dict（单板块/映射）、list[dict]（多板块）、
    list[list]（fields 投影）、扁平 values 列表（板块精确匹配单行）。这里只做
    保守归一化，保证 name/symbols 两键存在。
    """
    if isinstance(boards, dict):
        if "name" in boards or "symbols" in boards:
            item = dict(boards)
            item.setdefault("name", "")
            item.setdefault("symbols", [])
            return [item]
        return [
            {"name": str(name), "symbols": (
                [str(x) for x in items] if isinstance(items, list) else [str(items)]
            )}
            for name, items in boards.items()
        ]
    if isinstance(boards, list):
        if not boards:
            return []
        if isinstance(boards[0], dict):
            return [dict(item) for item in boards if isinstance(item, dict)]
        return [{"name": "", "symbols": [str(item) for item in boards]}]
    return []


# === 业务接口 ===


def compute_indicators(args: dict) -> dict:
    """计算技术指标（38 项 + zhishu 指数，共 39 项）。

    参数（与 get_indicators 工具 schema 对应）：
        indicators  指标名列表（1-8 个，白名单校验）
        codes       股票代码列表（1-50 个）
        params      与 indicators 一一对应的参数列表（可选；每项为 "5,10,20" 字符串、
                    整数或 None）
        frequency   周期，枚举 FREQUENCIES（默认 "1d"）
        start       8 位起始日期（缺省 = 今天往前 60 个自然日）
        end         8 位结束日期或 "N"（默认 "N"=最新）
        cross       False=原始值 / True=仅金叉死叉信号 / "with_value"=信号+原始值
        fq          "qfq"/"hfq"/None（不复权），默认 "qfq"
        fields      仅基础指标组：计算字段（close/high/low/open/volume/amount）
        method      zhishu 加权方式 1-5，默认 1
        base        zhishu 指数基点，默认 1000
        limit       每码最多返回行数（默认 500，硬上限 1000），截断返回 truncated
        compact     True=列式（dates+字段数组），False=行列表（默认 True）

    返回 {"ok": True, "result": {"source", "frequency", "indicators", "params",
    "compact", "truncated", "truncated_rows", "data"}} 或 {"ok": False, "error": "中文"}。
    data 形态：单码 → rows / 列式 dict；批量 → {code: rows / 列式 dict}；zhishu → 单序列。
    """
    # === 参数校验（先于 pybao 加载，离线即可报错） ===
    indicators = args.get("indicators")
    if not isinstance(indicators, list) or not indicators:
        return {"ok": False, "error": "indicators 必须为非空数组"}
    if len(indicators) > _INDICATOR_MAX:
        return {"ok": False, "error": f"indicators 最多 {_INDICATOR_MAX} 个，当前 {len(indicators)} 个"}
    ind_list: list[str] = []
    for name in indicators:
        name = str(name).strip().lower()
        if name not in SUPPORTED_INDICATORS:
            return {"ok": False, "error": f"未知指标: {name}"}
        if name not in ind_list:
            ind_list.append(name)
    if "zhishu" in ind_list and len(ind_list) > 1:
        return {"ok": False, "error": "zhishu 指数只能单独计算（不可与其它指标混用）"}

    raw_codes = args.get("codes")
    if isinstance(raw_codes, str):
        raw_codes = [raw_codes]
    if not isinstance(raw_codes, list) or not raw_codes:
        return {"ok": False, "error": "codes 必须为非空数组"}
    if len(raw_codes) > _CODES_MAX:
        return {"ok": False, "error": f"codes 最多 {_CODES_MAX} 个，当前 {len(raw_codes)} 个"}
    codes: list[str] = []
    for item in raw_codes:
        code = str(item).strip()
        if len(code) != 6 or not code.isdigit():
            return {"ok": False, "error": f"股票代码 {code!r} 必须是 6 位数字"}
        codes.append(code)

    frequency = str(args.get("frequency") or "1d").strip()
    if frequency not in FREQUENCIES:
        return {"ok": False, "error": f"不支持的频率: {frequency}"}

    start = str(args.get("start") or "").strip() or (
        datetime.now() - timedelta(days=_DEFAULT_START_DAYS)
    ).strftime("%Y%m%d")
    if len(start) != 8 or not start.isdigit():
        return {"ok": False, "error": "start 必须是 8 位日期 YYYYMMDD"}
    end = str(args.get("end") or "N").strip()
    if end != "N" and (len(end) != 8 or not end.isdigit()):
        return {"ok": False, "error": 'end 必须是 "N" 或 8 位日期 YYYYMMDD'}

    params = args.get("params")
    if params is not None:
        if isinstance(params, (str, int)):
            params = [params]  # 单指标允许直接传标量
        if not isinstance(params, list) or len(params) != len(ind_list):
            return {"ok": False, "error": "params 数量必须与 indicators 数量一致"}
        for i, param in enumerate(params):
            if param is None or isinstance(param, (str, int)) and not isinstance(param, bool):
                continue
            return {"ok": False, "error": f"params[{i}] 必须是参数串（如 '5,10,20'）、整数或 None"}

    cross = args.get("cross", False)
    if isinstance(cross, str):
        lowered = cross.strip().lower()
        if lowered == "with_value":
            cross = "with_value"
        elif lowered in ("true", "false"):
            cross = lowered == "true"
        else:
            return {"ok": False, "error": 'cross 必须是 false、true 或 "with_value"'}
    elif not isinstance(cross, bool):
        return {"ok": False, "error": 'cross 必须是 false、true 或 "with_value"'}
    if "zhishu" in ind_list and cross is not False:
        return {"ok": False, "error": "zhishu 指数不支持 cross（必须为 false）"}

    fq = args.get("fq", "qfq")
    if isinstance(fq, str):
        fq = fq.strip().lower() or None
        if fq not in ("qfq", "hfq", None):
            return {"ok": False, "error": 'fq 必须是 "qfq"、"hfq" 或 None'}
    elif fq is not None:
        return {"ok": False, "error": 'fq 必须是 "qfq"、"hfq" 或 None'}

    fields = args.get("fields")
    fields_list: list[str] | None = None
    if fields is not None:
        if not isinstance(fields, str):
            return {"ok": False, "error": "fields 必须是逗号分隔字符串（open/high/low/close/volume/amount）"}
        fields_list = [
            item.strip().lower()
            for item in fields.replace("，", ",").split(",")
            if item.strip()
        ]
        if not fields_list:
            fields_list = None
        else:
            for field in fields_list:
                if field not in DATA_FIELDS:
                    return {"ok": False, "error": f"不支持的行情字段 {field!r}（可选 open/high/low/close/volume/amount）"}
            if not all(name in BASIC_INDICATORS for name in ind_list):
                return {"ok": False, "error": "fields 仅支持 ma/ema/sma/wma/dma/std/sum/hhv/llv/ref 基础指标"}
            if cross is not False and len(fields_list) != 1:
                return {"ok": False, "error": 'cross=True/cross="with_value" 时 fields 只能选一个字段'}

    limit = _as_int(args.get("limit"), _DEFAULT_LIMIT)
    if limit is None or limit < 1:
        return {"ok": False, "error": "limit 必须是正整数"}
    limit = min(limit, _MAX_LIMIT)
    compact = _as_bool(args.get("compact"), default=True)

    try:
        method = int(args.get("method", 1))
        if method not in (1, 2, 3, 4, 5):
            return {"ok": False, "error": "method 必须是 1-5（1平权/2流通市值/3成交额/4成交量/5总市值）"}
    except (TypeError, ValueError):
        return {"ok": False, "error": "method 必须是整数 1-5"}
    try:
        base = float(args.get("base", 1000.0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "base 必须是数值（指数初始基点）"}

    # === 加载 pybao（失败降级） ===
    module = get_pybao()
    if module is None:
        return {"ok": False, "error": PYBAO_UNAVAILABLE}

    # === 一次批量计算（jisuan 原生支持多指标×多股票） ===
    jisuan_codes: str | list[str] = codes[0] if len(codes) == 1 else codes
    # 单指标时把 params 解包为标量（如 "5,10"）：zhibiao 单指标路径的 _int_list
    # 会把标量字符串拆成 [5,10]，但列表元素会直接 int("5,10") 报错
    jisuan_n = params[0] if params is not None and len(ind_list) == 1 else params
    try:
        with _PYBAO_LOCK:
            raw = module.jisuan(
                ",".join(ind_list),
                jisuan_codes,
                start=start,
                end=end,
                frequency=frequency,
                method=method,
                base=base,
                fq=fq,
                fields=fields_list,
                n=jisuan_n,
                cross=cross,
            )
    except Exception as exc:  # noqa: BLE001 - 计算失败转为中文错误
        return {"ok": False, "error": f"指标计算失败: {type(exc).__name__}: {exc}"}

    # === 结果加工：每码保留最后 limit 行（最新数据），compact 时列式化 ===
    try:
        truncated = False
        truncated_rows = 0

        def _trim(rows: list[dict]) -> list[dict]:
            nonlocal truncated, truncated_rows
            if len(rows) > limit:
                truncated = True
                truncated_rows += len(rows) - limit
                return rows[-limit:]  # 保留最新
            return rows

        if "zhishu" in ind_list:
            rows = _trim(_normalize_rows(raw))
            data = _to_columnar(rows) if compact else rows
        elif isinstance(raw, dict):
            batches: dict[str, object] = {}
            for code in codes:
                rows = _trim(_normalize_rows(raw.get(code)))
                batches[code] = _to_columnar(rows) if compact else rows
            data = batches
        else:
            rows = _trim(_normalize_rows(raw))
            data = _to_columnar(rows) if compact else rows
    except Exception as exc:  # noqa: BLE001 - 加工失败转为中文错误
        return {"ok": False, "error": f"指标结果加工失败: {type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "result": {
            "source": "pybao",
            "frequency": frequency,
            "indicators": ind_list,
            "params": params,
            "compact": compact,
            "truncated": truncated,
            "truncated_rows": truncated_rows,
            "data": data,
        },
    }


def query_boards(args: dict) -> dict:
    """板块成员查询（板块 → 成分股；query 传股票代码时由 pybao 反向解析所属板块）。

    参数（与 get_board_members 工具 schema 对应）：
        query           板块名称（支持模糊）/ 板块代码（如 801760.SL）/ 6 位股票代码
        category        板块分类：整数 0-3 或中文（概念/申万一级/申万二级/申万三级），
                        不传 = 全部
        fields          投影字段，逗号分隔，默认 "code,name,type,group,category"
        include_symbols 是否返回成分股代码列表（默认 False；单板块成分上限 500）
        limit           结果条目上限（默认 500，硬上限 500）

    返回 {"ok": True, "result": {"source", "category", "query", "boards",
    "total", "truncated", "symbols"?}} 或 {"ok": False, "error": "中文"}。
    boards 为板块元数据列表（不含 symbols）；include_symbols=True 时额外返回
    去重合并后的 symbols（成分股，超上限截断并标记 truncated）。
    """
    # === 参数校验（先于 pybao 加载） ===
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query 必须提供板块名称/代码/股票代码"}

    category = args.get("category")
    category_name: str | None = None
    if category is not None and not isinstance(category, bool):
        if isinstance(category, int) or (
            isinstance(category, str) and category.strip().isdigit()
        ):
            category_name = BOARD_CATEGORIES.get(
                int(category) if isinstance(category, str) else category
            )
        elif isinstance(category, str):
            category_name = BOARD_CATEGORIES.get(category.strip())
        if category_name is None:
            return {"ok": False, "error": f"不支持的板块分类: {category}（可选 概念/申万一级/申万二级/申万三级 或整数 0-3）"}

    fields = str(args.get("fields") or DEFAULT_BOARD_FIELDS)
    field_list: list[str] = []
    for item in fields.replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        item = FIELD_ALIASES.get(item, item)
        if item not in BOARD_FIELDS:
            return {"ok": False, "error": f"不支持的字段 {item!r}（可选 {'/'.join(BOARD_FIELDS)}）"}
        if item not in field_list:
            field_list.append(item)
    if not field_list:
        field_list = [f.strip() for f in DEFAULT_BOARD_FIELDS.split(",")]

    include_symbols = _as_bool(args.get("include_symbols"), default=False)
    limit = _as_int(args.get("limit"), _DEFAULT_LIMIT)
    if limit is None or limit < 1:
        return {"ok": False, "error": "limit 必须是正整数"}
    limit = min(limit, _MAX_SYMBOLS)

    # === 加载 pybao（失败降级） ===
    module = get_pybao()
    if module is None:
        return {"ok": False, "error": PYBAO_UNAVAILABLE}

    # === 查询（bk.get(x, category)；fields 投影由客户端完成——pybao 的 fields
    # 投影返回无键的扁平值列表，无法可靠归一化） ===
    try:
        with _PYBAO_LOCK:
            boards = module.bk.get(query, category=category_name)
    except Exception as exc:  # noqa: BLE001 - 查询失败转为中文错误
        return {"ok": False, "error": f"板块查询失败: {type(exc).__name__}: {exc}"}

    items = _normalize_boards(boards)
    # 板块元数据（客户端按 field_list 投影，去掉成分股避免结果爆炸）
    meta: list[dict] = []
    symbols: list[str] = []
    for item in items:
        symbols.extend(str(s) for s in (item.get("symbols") or []))
        meta_item = {k: item.get(k) for k in field_list}
        if include_symbols and "symbols" in field_list:
            meta_item["symbols"] = item.get("symbols") or []
        meta.append(meta_item)
    symbols = sorted(set(symbols))
    total_symbols = len(symbols)
    truncated = total_symbols > limit
    kept_symbols = symbols[-limit:] if truncated else symbols

    result: dict = {
        "source": "pybao",
        "category": category_name,
        "query": query,
        "boards": meta,
        "board_count": len(meta),
        "total": total_symbols,
        "truncated": truncated,
    }
    if include_symbols:
        result["symbols"] = kept_symbols
    return {"ok": True, "result": result}


if __name__ == "__main__":
    # 离线自检：pybao 缺失时应得到明确降级错误
    print(compute_indicators({"indicators": ["macd"], "codes": ["600633"]}))
