#!/usr/bin/env python3
"""pybao_tools — pybao（技术指标/板块）能力封装，离线可降级。

本模块把外部 pybao（容器镜像自带 /opt/stockdb/pybao；本机开发放
/tmp/pybao_mac 或设置 PYBAO_DIR 环境变量）包装成统一接口，供
stockdb_mcp_server 的 get_indicators / get_board_members / get_kline(增强路径) 使用：

- get_pybao()       加载并返回 pybao 的 zhibiao 模块（含 jisuan/bk 入口），不可用返回 None
- get_sdk_client()  返回 pybao SDK client（复权/1m/1w/1M/批量 K 线数据源），不可用返回 None
- compute_indicators(args)  技术指标计算（参数校验 + 批量计算 + 结果加工）
- query_boards(args)        板块成员查询（双向：板块→成分股 / 股票→所属板块）
- get_mydb_rd()     返回连接当前端点的原始 rd 客户端（mydb 私有库读写用），不可用返回 None
- query_mydb(args)  mydb 私有库只读（表名校验 + 键值/全表读取，支持游标续取）
- screen_stocks(args, universe)  条件选股核心计算（指标交叉/流通市值/ST 过滤）

compute_indicators / query_boards / query_mydb / screen_stocks 统一返回
{"ok": True, "result": {...}} 或 {"ok": False, "error": "中文错误"}。
失败返回带 "code"（INVALID_ARGUMENT=参数校验失败 / DEPENDENCY_UNAVAILABLE=pybao 缺失 /
INTERNAL_ERROR=计算/查询/加工异常），DEPENDENCY_UNAVAILABLE 附加 "hint" 定位说明。

进度通知：set_progress_hook / clear_progress_hook / notify_progress 提供线程级
进度回调（供 server/app 的 SSE 流式进度推送复用），无回调时静默不产生开销。

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
import time
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
_DEFAULT_START_DAYS = 120  # start 缺省 = 今天往前 120 个自然日（~82 交易日；
                           # 上游 zb_core 的 MACD 类 EMA 以首值播种，需 ~80 交易日收敛，
                           # 60 自然日窗口的金叉/死叉信号不可靠，勿改短）

# 板块分类：zhibiao.CATEGORY_MAP = {0:概念, 1:申万一级, 2:申万二级, 3:申万三级}
BOARD_CATEGORIES = {
    0: "概念", 1: "申万一级", 2: "申万二级", 3: "申万三级",
    "0": "概念", "1": "申万一级", "2": "申万二级", "3": "申万三级",
    "概念": "概念", "申万一级": "申万一级", "申万二级": "申万二级", "申万三级": "申万三级",
}
DEFAULT_BOARD_FIELDS = "code,name,type,group,category"
BOARD_FIELDS = ("code", "name", "source", "type", "group", "category", "symbols")
FIELD_ALIASES = {"symbol": "symbols", "symbls": "symbols", "codelist": "symbols"}

# pybao 缺失时的错误文案与定位 hint（error 保留 "pybao 不可用：" 开头文案，
# hint 为独立字段供客户端按 code=DEPENDENCY_UNAVAILABLE 分支展示）
PYBAO_UNAVAILABLE = (
    "pybao 不可用：容器镜像内自动携带（/opt/stockdb/pybao）；本机开发请把 "
    "macOS 版 pybao 放到 /tmp/pybao_mac 或设置 PYBAO_DIR 环境变量。"
)
PYBAO_UNAVAILABLE_HINT = (
    "容器镜像自动携带（/opt/stockdb/pybao）；本机开发请把 macOS 版 pybao 放 "
    "/tmp/pybao_mac 或设 PYBAO_DIR"
)


# === 统一错误码（8 码体系，本批全局） ===
# 契约：所有 {"ok": False, ...} 返回携带 "code" ∈ 下方 8 码常量（值必须与
# stockdb_mcp_server 侧同名常量完全一致）；DEPENDENCY_UNAVAILABLE 附加 "hint"。
# server 层 isError content 透传 code/hint。
# 各码语义：
#   INVALID_ARGUMENT        参数非法（替换原 PARAM_INVALID；未知工具也用本码）
#   NO_DATA                 合法查询但无数据（替换原 DATA_NOT_FOUND）
#   NOT_PUBLISHED           该时点数据尚未入库/尚未发布
#   INVALID_SYMBOL          代码不在股票池
#   DEPENDENCY_UNAVAILABLE  pybao 缺失（替换原 PYBAO_UNAVAILABLE）
#   PARTIAL_RESULT          预留：仅作数据面 is_partial 标志的语义说明，
#                           不作为 isError code 返回
#   RATE_LIMITED            预留常量，当前无配额实现，不返回
#   INTERNAL_ERROR          内部异常（替换原 INTERNAL）
INVALID_ARGUMENT = "INVALID_ARGUMENT"
NO_DATA = "NO_DATA"
NOT_PUBLISHED = "NOT_PUBLISHED"
INVALID_SYMBOL = "INVALID_SYMBOL"
DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
PARTIAL_RESULT = "PARTIAL_RESULT"
RATE_LIMITED = "RATE_LIMITED"
INTERNAL_ERROR = "INTERNAL_ERROR"


def _error(code: str, message: str, *, hint: str | None = None) -> dict:
    """构造统一错误返回 {"ok": False, "error", "code"(, "hint")}。"""
    out: dict = {"ok": False, "error": message, "code": code}
    if hint:
        out["hint"] = hint
    return out


def _param_error(message: str) -> dict:
    """参数校验失败（INVALID_ARGUMENT）。"""
    return _error(INVALID_ARGUMENT, message)


def _pybao_unavailable_error() -> dict:
    """pybao 缺失降级（DEPENDENCY_UNAVAILABLE + 定位 hint）。"""
    return _error(
        DEPENDENCY_UNAVAILABLE, PYBAO_UNAVAILABLE, hint=PYBAO_UNAVAILABLE_HINT
    )


def _internal_error(message: str) -> dict:
    """计算/查询/加工异常（INTERNAL_ERROR）。"""
    return _error(INTERNAL_ERROR, message)


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


def get_fq_cum(code: str, date: str) -> tuple[float, float] | None:
    """返回 (cum_at_date, cum_latest)：股票在判定日 date 的累计复权因子与最新因子。

    用于重建"当日法定涨跌停参考价"（0.8.14 污染修复，见 board_metrics.
    rebuild_limit_reference_price）：引擎历史 pre_close 被最新因子回溯重算，
    需按 cum_D/cum_latest 反推。SDK 客户端 __init__ 已预加载全市场因子表
    （_fq_dates/_fq_cums 平行数组，LevelDB 有序）；不可用/无因子事件 → None
    （调用方按未污染原样降级）。
    """
    try:
        import bisect
        client = get_sdk_client()
        if client is None:
            return None
        dates = getattr(client, "_fq_dates", {}).get(code)
        cums = getattr(client, "_fq_cums", {}).get(code)
        if not dates or not cums or len(dates) != len(cums):
            return None  # 无因子事件（未除权）或数据异常
        idx = bisect.bisect_right(dates, str(date)) - 1
        if idx < 0:
            return 1.0, float(cums[-1])  # 判定日早于首条因子事件：当时因子 = 1.0
        return float(cums[idx]), float(cums[-1])
    except Exception:  # noqa: BLE001 - 因子查询失败按未污染降级
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


# === 进度通知（线程级，供 server/app 流式进度推送复用） ===

_progress_local = threading.local()


def set_progress_hook(fn) -> None:
    """设置当前线程的进度回调；fn(stage: str, detail: str | None)。

    供 server/app 在长任务（如全市场 screen_stocks）期间以 SSE 推送进度事件。
    """
    _progress_local.hook = fn


def clear_progress_hook() -> None:
    """清除当前线程的进度回调（恢复为无回调静默）。"""
    _progress_local.hook = None


def notify_progress(stage: str, detail: str | None = None) -> None:
    """当前线程存在进度回调时调用 fn(stage, detail)；无回调或回调异常均静默。"""
    hook = getattr(_progress_local, "hook", None)
    if hook is None:
        return
    try:
        hook(stage, detail)
    except Exception:  # noqa: BLE001 - 进度回调异常不影响业务
        pass


# === 业务接口 ===


def compute_indicators(args: dict) -> dict:
    """计算技术指标（38 项 + zhishu 指数，共 39 项）。

    参数（与 get_indicators 工具 schema 对应）：
        indicators  指标名列表（1-8 个，白名单校验）
        codes       股票代码列表（1-50 个）
        params      与 indicators 一一对应的参数列表（可选；每项为 "5,10,20" 字符串、
                    整数或 None）
        frequency   周期，枚举 FREQUENCIES（默认 "1d"）
        start       8 位起始日期（缺省 = 今天往前 120 个自然日，保证 MACD 类指标收敛）
        end         8 位结束日期或 "N"（默认 "N"=最新）
        cross       False=原始值 / True=仅金叉死叉信号 / "with_value"=信号+原始值
        fq          "qfq"/"hfq"/None（不复权），默认 "qfq"
        fields      仅基础指标组：计算字段（close/high/low/open/volume/amount）
        method      zhishu 加权方式 1-5，默认 1
        base        zhishu 指数基点，默认 1000
        limit       每码最多返回行数（默认 500，硬上限 1000），截断返回 truncated
        compact     True=列式（dates+字段数组），False=行列表（默认 True）

    返回 {"ok": True, "result": {"source", "frequency", "indicators", "params",
    "compact", "truncated", "truncated_rows", "data"}} 或 {"ok": False, "error": "中文",
    "code": "INVALID_ARGUMENT"/"DEPENDENCY_UNAVAILABLE"/"INTERNAL_ERROR"(+hint)}。
    data 形态：单码 → rows / 列式 dict；批量 → {code: rows / 列式 dict}；zhishu → 单序列。
    """
    # === 参数校验（先于 pybao 加载，离线即可报错） ===
    indicators = args.get("indicators")
    if not isinstance(indicators, list) or not indicators:
        return _param_error("indicators 必须为非空数组")
    if len(indicators) > _INDICATOR_MAX:
        return _param_error(
            f"indicators 最多 {_INDICATOR_MAX} 个，当前 {len(indicators)} 个",
        )
    ind_list: list[str] = []
    for name in indicators:
        name = str(name).strip().lower()
        if name not in SUPPORTED_INDICATORS:
            return _param_error(f"未知指标: {name}")
        if name not in ind_list:
            ind_list.append(name)
    if "zhishu" in ind_list and len(ind_list) > 1:
        return _param_error("zhishu 指数只能单独计算（不可与其它指标混用）")

    raw_codes = args.get("codes")
    if isinstance(raw_codes, str):
        raw_codes = [raw_codes]
    if not isinstance(raw_codes, list) or not raw_codes:
        return _param_error("codes 必须为非空数组")
    if len(raw_codes) > _CODES_MAX:
        return _param_error(
            f"codes 最多 {_CODES_MAX} 个，当前 {len(raw_codes)} 个",
        )
    codes: list[str] = []
    for item in raw_codes:
        code = str(item).strip()
        if len(code) != 6 or not code.isdigit():
            return _param_error(f"股票代码 {code!r} 必须是 6 位数字")
        codes.append(code)

    frequency = str(args.get("frequency") or "1d").strip()
    if frequency not in FREQUENCIES:
        return _param_error(f"不支持的频率: {frequency}")

    start = str(args.get("start") or "").strip() or (
        datetime.now() - timedelta(days=_DEFAULT_START_DAYS)
    ).strftime("%Y%m%d")
    if len(start) != 8 or not start.isdigit():
        return _param_error("start 必须是 8 位日期 YYYYMMDD")
    end = str(args.get("end") or "N").strip()
    if end != "N" and (len(end) != 8 or not end.isdigit()):
        return _param_error('end 必须是 "N" 或 8 位日期 YYYYMMDD')

    params = args.get("params")
    if params is not None:
        if isinstance(params, (str, int)):
            params = [params]  # 单指标允许直接传标量
        if not isinstance(params, list) or len(params) != len(ind_list):
            return _param_error("params 数量必须与 indicators 数量一致")
        for i, param in enumerate(params):
            if param is None or isinstance(param, (str, int)) and not isinstance(param, bool):
                continue
            return _param_error(
                f"params[{i}] 必须是参数串（如 '5,10,20'）、整数或 None",
            )

    cross = args.get("cross", False)
    if isinstance(cross, str):
        lowered = cross.strip().lower()
        if lowered == "with_value":
            cross = "with_value"
        elif lowered in ("true", "false"):
            cross = lowered == "true"
        else:
            return _param_error('cross 必须是 false、true 或 "with_value"')
    elif not isinstance(cross, bool):
        return _param_error('cross 必须是 false、true 或 "with_value"')
    if "zhishu" in ind_list and cross is not False:
        return _param_error("zhishu 指数不支持 cross（必须为 false）")

    fq = args.get("fq", "qfq")
    if isinstance(fq, str):
        fq = fq.strip().lower() or None
        if fq not in ("qfq", "hfq", None):
            return _param_error('fq 必须是 "qfq"、"hfq" 或 None')
    elif fq is not None:
        return _param_error('fq 必须是 "qfq"、"hfq" 或 None')

    fields = args.get("fields")
    fields_list: list[str] | None = None
    if fields is not None:
        if not isinstance(fields, str):
            return _param_error(
                "fields 必须是逗号分隔字符串（open/high/low/close/volume/amount）",
            )
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
                    return _param_error(
                        f"不支持的行情字段 {field!r}（可选 open/high/low/close/volume/amount）",
                    )
            if not all(name in BASIC_INDICATORS for name in ind_list):
                return _param_error(
                    "fields 仅支持 ma/ema/sma/wma/dma/std/sum/hhv/llv/ref 基础指标",
                )
            if cross is not False and len(fields_list) != 1:
                return _param_error(
                    'cross=True/cross="with_value" 时 fields 只能选一个字段',
                )

    limit = _as_int(args.get("limit"), _DEFAULT_LIMIT)
    if limit is None or limit < 1:
        return _param_error("limit 必须是正整数")
    limit = min(limit, _MAX_LIMIT)
    compact = _as_bool(args.get("compact"), default=True)

    try:
        method = int(args.get("method", 1))
        if method not in (1, 2, 3, 4, 5):
            return _param_error(
                "method 必须是 1-5（1平权/2流通市值/3成交额/4成交量/5总市值）",
            )
    except (TypeError, ValueError):
        return _param_error("method 必须是整数 1-5")
    try:
        base = float(args.get("base", 1000.0))
    except (TypeError, ValueError):
        return _param_error("base 必须是数值（指数初始基点）")

    # === 加载 pybao（失败降级） ===
    module = get_pybao()
    if module is None:
        return _pybao_unavailable_error()

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
        return _internal_error(f"指标计算失败: {type(exc).__name__}: {exc}")

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
        return _internal_error(f"指标结果加工失败: {type(exc).__name__}: {exc}")

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
    "board_count", "symbol_count", "truncated", "symbols"?}} 或 {"ok": False,
    "error": "中文", "code": ...}。
    boards 为板块元数据列表（不含 symbols）；board_count = 命中板块数（= len(boards)）；
    symbol_count = 各板块成分股去重合并后的总数（原 total 语义）；
    include_symbols=True 时额外返回去重合并后的 symbols（成分股，超上限截断并标记 truncated）。
    """
    # === 参数校验（先于 pybao 加载） ===
    query = str(args.get("query") or "").strip()
    if not query:
        return _param_error("query 必须提供板块名称/代码/股票代码")

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
            return _param_error(
                f"不支持的板块分类: {category}（可选 概念/申万一级/申万二级/申万三级 或整数 0-3）",
            )

    fields = str(args.get("fields") or DEFAULT_BOARD_FIELDS)
    field_list: list[str] = []
    for item in fields.replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        item = FIELD_ALIASES.get(item, item)
        if item not in BOARD_FIELDS:
            return _param_error(
                f"不支持的字段 {item!r}（可选 {'/'.join(BOARD_FIELDS)}）",
            )
        if item not in field_list:
            field_list.append(item)
    if not field_list:
        field_list = [f.strip() for f in DEFAULT_BOARD_FIELDS.split(",")]

    include_symbols = _as_bool(args.get("include_symbols"), default=False)
    limit = _as_int(args.get("limit"), _DEFAULT_LIMIT)
    if limit is None or limit < 1:
        return _param_error("limit 必须是正整数")
    limit = min(limit, _MAX_SYMBOLS)

    # === 加载 pybao（失败降级） ===
    module = get_pybao()
    if module is None:
        return _pybao_unavailable_error()

    # === 查询（bk.get(x, category)；fields 投影由客户端完成——pybao 的 fields
    # 投影返回无键的扁平值列表，无法可靠归一化） ===
    try:
        with _PYBAO_LOCK:
            boards = module.bk.get(query, category=category_name)
    except Exception as exc:  # noqa: BLE001 - 查询失败转为中文错误
        return _internal_error(f"板块查询失败: {type(exc).__name__}: {exc}")

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
        "symbol_count": total_symbols,
        "truncated": truncated,
    }
    if include_symbols:
        result["symbols"] = kept_symbols
    return {"ok": True, "result": result}


# === mydb 私有库读取 / 条件选股（Phase 2） ===
# 上游 stockdb 内置私有存储 ./mydb：HTTP 层只读，读取走原始 rd 客户端。
# 保留表清单与 docker/webui/app.py 的 _RESERVED_TABLES 保持一致（禁止覆盖上游同步数据）。

_RESERVED_TABLES = ("日k", "分钟k", "复权", "股票代码", "周k", "月k", "板块", "行业", "概念")

_MYDB_KEY_LIMIT_DEFAULT = 100   # query_mydb 未传 key 时默认返回键数
_MYDB_KEY_LIMIT_MAX = 500       # query_mydb 键数硬上限
_UNIVERSE_MAX = 6000            # screen_stocks universe 硬上限
_SCREEN_DEFAULT_LIMIT = 50      # screen_stocks 默认候选上限
_SCREEN_LIMIT_MAX = 200         # screen_stocks 候选硬上限
_BAR_FETCH_CAP = 500            # screen_stocks 单次 bar 拉取上限
_CROSS_WARMUP_DAYS = 120        # 指标交叉预热期（自然日，EMA/累积类指标档）
_CROSS_WARMUP_DAYS_SHORT = 60   # 快速收敛指标预热期（KDJ/RSI/WR/BOLL 等 SMA 滚动类）
# 上游 zb_core 的 EMA 以首值播种、OBV/ASI 类为累积量，需要 ~80 交易日收敛，
# 预热不足会导致金叉/死叉信号失真；SMA 滚动类（kdj/rsi/boll/wr/psy/cci 等）
# 在 n 个周期内收敛，60 自然日足够且显著省时。
_CROSS_WARMUP_LONG = frozenset({
    "macd", "ema", "expma", "trix", "dfma", "dma",
    "obv", "asi", "emv", "ktn", "taq", "mass", "xsii",
})


def _to_py(value: object) -> object:
    """pybao 返回值可能是 QueryResult，转原生 Python 对象（可 JSON 序列化）。

    QueryResult.do() 返回原生 list/dict（优先）；其次 dict(v)；列表递归转换。
    """
    if value is None:
        return None
    if hasattr(value, "do") and callable(value.do):
        try:
            return _to_py(value.do())
        except Exception:  # noqa: BLE001 - 转换失败继续尝试其他形态
            pass
    if hasattr(value, "keys") and hasattr(value, "all"):
        try:
            return dict(value)
        except Exception:  # noqa: BLE001 - 转换失败继续尝试其他形态
            pass
    if isinstance(value, (list, tuple)):
        return [_to_py(item) for item in value]
    return value


def _validate_mydb_table(table: str) -> str:
    """校验 mydb 自定义表名（镜像 app.validate_custom_table），返回规范化表名。

    非空、仅字母数字与 _:-、不得等于保留表或以 "保留表:" 前缀开头。
    """
    t = str(table or "").strip().strip(":")
    if not t:
        raise ValueError("表名不能为空")
    if not all(ch.isalnum() or ch in "_:-" for ch in t):
        raise ValueError("表名只能含字母数字与 _:-")
    for reserved in _RESERVED_TABLES:
        if t == reserved or t.startswith(reserved + ":"):
            raise ValueError(
                f"表名 {t!r} 与上游保留表 {reserved!r} 冲突，请用自定义命名空间"
            )
    return t


def get_mydb_rd() -> object | None:
    """返回连接当前端点的原始 rd 客户端（mydb 读写用），不可用返回 None（不抛异常）。

    新版走 stock_sdk.get_default_raw_rd()（与 jisuan 内部取连接一致）；旧版取
    get_pybao() 重绑定后的 zhibiao.rd。返回对象支持 rd.get / rd.keys / rd.set。
    """
    try:
        module = get_pybao()
        if module is None:
            return None
        stock_sdk = sys.modules.get("stock_sdk")
        if stock_sdk is not None and hasattr(stock_sdk, "get_default_raw_rd"):
            return stock_sdk.get_default_raw_rd()
        return getattr(module, "rd", None)
    except Exception:  # noqa: BLE001 - 获取失败降级为 None
        return None


def query_mydb(args: dict) -> dict:
    """只读 mydb 私有库（镜像 docker/webui/app.py 的 mydb_read 语义，纯读不写）。

    参数：
        table  自定义表名（非空、仅字母数字与 _:-、不得与上游保留表冲突）
        key    可选；传了（非空）则只读该键，未传则列出表内全部键值
        limit  未传 key 时最多返回键数（默认 100，硬上限 500），超限 truncated=True
        cursor 可选字符串；仅未传 key 时生效——作为续取游标，先对键排序、
               再只保留 > cursor 的键（配合 result.next_key 翻页）

    返回 {"ok": True, "result": {...}} 或 {"ok": False, "error": "中文", "code": ...}。
    result：传 key → {"source","table","key","value"}；
    未传 key → {"source","table","keys","values","total","truncated","next_key"}；
    next_key：截断时 = 保留的最后一个原始键（客户端以它为 cursor 续取下一页），
    未截断时 = None。cursor 参数在传 key 时被忽略。
    """
    # === 参数校验（先于 pybao 加载，离线即可报错） ===
    table = str(args.get("table") or "").strip()
    key = str(args.get("key") or "").strip()
    cursor = str(args.get("cursor") or "").strip() or None
    limit = _as_int(args.get("limit"), _MYDB_KEY_LIMIT_DEFAULT)
    if limit is None or limit < 1:
        return _param_error("limit 必须是正整数")
    limit = min(limit, _MYDB_KEY_LIMIT_MAX)

    try:
        table = _validate_mydb_table(table)
    except ValueError as exc:
        return _param_error(str(exc))

    rd = get_mydb_rd()
    if rd is None:
        return _pybao_unavailable_error()

    try:
        if key:
            value = _to_py(rd.get(table, key))
            return {
                "ok": True,
                "result": {
                    "source": "pybao",
                    "table": table,
                    "key": key,
                    "value": value,
                },
            }
        raw_keys = list(rd.keys(table, "*") or [])
        # 排序保证游标续取的有序性（rd.keys 返回顺序不做假设）
        raw_keys.sort(key=str)
        if cursor is not None:
            raw_keys = [k for k in raw_keys if str(k) > cursor]
        total = len(raw_keys)
        kept_keys = raw_keys if len(raw_keys) <= limit else raw_keys[:limit]
        truncated = total > limit
        next_key = kept_keys[-1] if truncated else None
        values: dict[str, object] = {}
        for k in kept_keys:
            # 键形如 "hk日k:00700:20250425"（含表名前缀）或 "custom:20250425"；
            # 去掉首个冒号前的表名段，其余部分整体作为 get 的 key（如 "00700:20250425"）。
            # 不能用 split(":")[-1]：会丢掉港股代码段导致读不到值。
            full = str(k)
            lookup_key = full.split(":", 1)[-1] if ":" in full else full
            try:
                values[full] = _to_py(rd.get(table, lookup_key))
            except Exception:  # noqa: BLE001 - 单键失败不中断整体
                values[full] = None
        return {
            "ok": True,
            "result": {
                "source": "pybao",
                "table": table,
                "keys": kept_keys,
                "values": values,
                "total": total,
                "truncated": truncated,
                "next_key": next_key,
            },
        }
    except Exception as exc:  # noqa: BLE001 - 读取失败转为中文错误
        return _internal_error(f"mydb 读取失败: {type(exc).__name__}: {exc}")


def _as_float_or_none(value: object, label: str) -> float | None:
    """可选数值参数解析：缺省/空串 → None，非法抛 ValueError（中文文案）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} 必须是数值") from None


def _parse_is_st(value: object) -> bool | None:
    """解析 bar 行 is_st：1/true→True，0/false→False，None/未知→None（未知）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "y", "是"):
            return True
        if lowered in ("0", "false", "no", "n", "否", ""):
            return False
    return None


def _date_sort_key(value: object) -> int:
    """YYYYMMDD 日期（int/str）→ 可比较整数；非法/None → -1。"""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return -1


def screen_stocks(args: dict, universe: list[str], universe_source: str = "full_market") -> dict:
    """条件选股核心计算：指标交叉（单/多条件 AND）+ 流通市值 + ST 过滤。

    universe 由 server 侧解析后传入（板块成分 / 全市场），本函数不负责取股票列表；
    universe_source 标识股票池来源（full_market / board:<name> / codes），
    板块来源本身即可构成筛选条件（"列出某板块成分股"）。
    参数：
        universe        候选股票代码列表（6 位数字，非空，上限 6000）
        indicator_cross 可选：单个对象或对象数组（1-3 个），每个条件
                        {"name", "golden"(默认 True), "within_days"(1-60, 默认 5)}；
                        name 必须在 SUPPORTED_INDICATORS 且 != "zhishu"，
                        指标名不允许重复；多条件时取交集（股票须命中全部条件），
                        各条件独立 jisuan 并按各自收敛特性分档预热
        float_mv_min / float_mv_max  可选流通市值边界（与日K float_mv 同单位：元）
        exclude_st      默认 True（宽松布尔解析）；ST 状态未知时保留并标注 is_st=None
        date            可选 8 位日期；缺省 = 最新交易日（由交叉行日期推导）
        limit           默认 50，1-200

    返回 {"ok": True, "result": {...}} 或 {"ok": False, "error": "中文", "code": ...}。
    result 含 source/date/universe_count/matched_count/candidates/dropped/
    truncated/limit/conditions/methodology/known_limitations/elapsed_ms。
    matched_count = 多条件交集数量；candidates 每项含 code/name/close/cross_date/
    signal/crosses/float_mv/is_st/pct_chg；crosses = {指标名: {"date", "signal"}}，
    cross_date = 该股各条件日期最大值，signal = 单条件时的信号值、多条件时 None
    （保留字段向后兼容）；elapsed_ms = 函数入口起计时的整毫秒数。
    进度：计算期间按阶段调用 notify_progress（stock_pool_ready / indicator_done /
    filter_done），供 server/app 的 SSE 流式进度推送复用。
    """
    _t0 = time.time()

    # === 参数校验（先于 pybao 加载，离线即可报错） ===
    if isinstance(universe, str):
        universe = [universe]
    if not isinstance(universe, list) or not universe:
        return _param_error("universe 必须为非空列表")
    if len(universe) > _UNIVERSE_MAX:
        return _param_error(
            f"universe 最多 {_UNIVERSE_MAX} 个，当前 {len(universe)} 个",
        )
    codes: list[str] = []
    for item in universe:
        code = str(item).strip()
        if len(code) != 6 or not code.isdigit():
            return _param_error(f"股票代码 {code!r} 必须是 6 位数字")
        codes.append(code)
    universe = codes

    raw_cross = args.get("indicator_cross")
    cross_conditions: list[dict] | None = None
    if raw_cross is not None:
        if isinstance(raw_cross, dict):
            raw_list = [raw_cross]
        elif isinstance(raw_cross, list):
            if not raw_cross:
                return _param_error("indicator_cross 必须提供至少 1 个条件对象")
            if len(raw_cross) > 3:
                return _param_error(
                    f"indicator_cross 最多 3 个条件，当前 {len(raw_cross)} 个",
                )
            raw_list = raw_cross
        else:
            return _param_error("indicator_cross 必须是对象或对象数组（1-3 个）")
        cross_conditions = []
        seen_names: set[str] = set()
        for item in raw_list:
            if not isinstance(item, dict):
                return _param_error("indicator_cross 每个条件必须是对象")
            name = str(item.get("name") or "").strip().lower()
            if not name:
                return _param_error("indicator_cross.name 必须提供指标名")
            if name not in SUPPORTED_INDICATORS:
                return _param_error(f"未知指标: {name}")
            if name == "zhishu":
                return _param_error("zhishu 不支持 cross 筛选")
            if name in seen_names:
                return _param_error("indicator_cross 不允许重复指标")
            seen_names.add(name)
            golden = _as_bool(item.get("golden"), default=True)
            within_days = _as_int(item.get("within_days"), 5)
            if within_days is None or not 1 <= within_days <= 60:
                return _param_error("within_days 必须是 1-60 的整数")
            cross_conditions.append({
                "name": name, "golden": golden, "within_days": within_days,
            })

    try:
        mv_min = _as_float_or_none(args.get("float_mv_min"), "float_mv_min")
        mv_max = _as_float_or_none(args.get("float_mv_max"), "float_mv_max")
    except ValueError as exc:
        return _param_error(str(exc))
    if mv_min is not None and mv_max is not None and mv_min > mv_max:
        return _param_error("float_mv_min 不能大于 float_mv_max")

    exclude_st = _as_bool(args.get("exclude_st"), default=True)

    date = str(args.get("date") or "").strip()
    if date and (len(date) != 8 or not date.isdigit()):
        return _param_error("date 必须是 8 位日期 YYYYMMDD")

    limit = _as_int(args.get("limit"), _SCREEN_DEFAULT_LIMIT)
    if limit is None or not 1 <= limit <= _SCREEN_LIMIT_MAX:
        return _param_error("limit 必须是 1-200 的整数")

    # 至少一个筛选条件：指标交叉 / 市值边界 / 板块（板块来源本身即条件）
    if (
        cross_conditions is None
        and mv_min is None
        and mv_max is None
        and universe_source == "full_market"
    ):
        return _param_error("至少提供一个筛选条件")

    # === 计算（全程在 _PYBAO_LOCK 内） ===
    try:
        with _PYBAO_LOCK:
            # a) 加载 pybao（失败降级）
            module = get_pybao()
            if module is None:
                return _pybao_unavailable_error()

            # a2) 进度：股票池就绪（jisuan 前）
            notify_progress("stock_pool_ready", f"股票池 {len(universe)} 只")

            # b) 指标交叉：多条件各自 jisuan（预热期按指标收敛特性分档：
            # EMA/累积类 120 自然日、SMA 滚动类 60 自然日），
            # end = date 或 "N"；交集 = 命中全部条件的股票
            cross_window_notes: list[str] = []
            if cross_conditions is not None:
                per_cond_matched: dict[str, dict[str, tuple[object, object]]] = {}
                for cond in cross_conditions:
                    name = cond["name"]
                    golden = cond["golden"]
                    within_days = cond["within_days"]
                    warmup_days = (
                        _CROSS_WARMUP_DAYS
                        if name in _CROSS_WARMUP_LONG
                        else _CROSS_WARMUP_DAYS_SHORT
                    )
                    cross_start = date or (
                        datetime.now() - timedelta(days=warmup_days)
                    ).strftime("%Y%m%d")
                    cross_end = date or "N"
                    jisuan_codes = universe[0] if len(universe) == 1 else universe
                    raw = module.jisuan(
                        name, jisuan_codes, start=cross_start, end=cross_end, cross=True,
                    )
                    # 归一化每码行列表：批量 {code: rows}；单码 list
                    if isinstance(raw, dict):
                        per_code = {code: _normalize_rows(raw.get(code)) for code in universe}
                    else:
                        per_code = {universe[0]: _normalize_rows(raw)}
                    cond_matched: dict[str, tuple[object, object]] = {}
                    for code in universe:
                        rows = per_code.get(code) or []
                        window = rows[-within_days:] if within_days > 0 else rows
                        best: tuple[object, object] | None = None
                        for row in window:
                            sig = row.get("cross")
                            if sig is None:
                                sig = row.get(name + "_cross")
                            is_match = (sig == 1) if golden else (sig == -1)
                            if not is_match:
                                continue
                            d = row.get("date")
                            if best is None or _date_sort_key(d) > _date_sort_key(best[0]):
                                best = (d, sig)
                        if best is not None:
                            cond_matched[code] = best
                    per_cond_matched[name] = cond_matched
                    cross_window_notes.append(
                        f"{name}: 最近 {within_days} 个交易日；指标自 {cross_start}"
                        f"(前{warmup_days}自然日)起算以包含预热期"
                    )
                # 交集：股票须命中全部条件
                matched_codes: set[str] | None = None
                for cond_matched in per_cond_matched.values():
                    if matched_codes is None:
                        matched_codes = set(cond_matched)
                    else:
                        matched_codes &= set(cond_matched)
                matched: list[dict] = []
                for code in sorted(matched_codes or []):
                    crosses = {
                        name: {
                            "date": per_cond_matched[name][code][0],
                            "signal": per_cond_matched[name][code][1],
                        }
                        for name in per_cond_matched
                    }
                    cross_date = max(
                        (crosses[name]["date"] for name in crosses),
                        key=_date_sort_key,
                    )
                    signal = (
                        crosses[cross_conditions[0]["name"]]["signal"]
                        if len(cross_conditions) == 1 else None
                    )
                    matched.append({
                        "code": code,
                        "cross_date": cross_date,
                        "signal": signal,
                        "crosses": crosses,
                    })
                cross_window_note = "；".join(cross_window_notes)
            else:
                # 无指标交叉条件：全部 universe 候选，cross_date/signal 为空
                matched = [
                    {"code": code, "cross_date": None, "signal": None, "crosses": {}}
                    for code in universe
                ]
                cross_window_note = ""

            # b2) 进度：交叉计算完成（交集数量）
            notify_progress("indicator_done", f"满足交叉条件 {len(matched)} 只")

            # c) effective_date = date 参数或 matched 中最大交叉行日期
            # 注意：SDK 的 get_data 对 start/end 做 len() 处理，必须传字符串
            if date:
                effective_date = date
            else:
                dated = [m["cross_date"] for m in matched if m["cross_date"] is not None]
                effective_date = max(dated, key=_date_sort_key) if dated else None
            if effective_date is not None:
                effective_date = str(effective_date)

            # d) 候选 bar 过滤：先按 cross_date 降序、code 升序排序并截取拉取上限，
            # 再用 SDK 批量 pipeline 一次拉取所有候选的单日 bar（单码循环太慢）
            client = get_sdk_client()
            if client is None:
                return _pybao_unavailable_error()

            ordered = sorted(
                matched,
                key=lambda m: (
                    -_date_sort_key(m["cross_date"]) if m["cross_date"] is not None else 1,
                    m["code"],
                ),
            )
            truncated = False
            if len(ordered) > _BAR_FETCH_CAP:
                truncated = True
                known_limitations = [
                    "bar 缺失/ST 状态未知的候选按条件剔除或标注",
                    f"候选数超过 bar 拉取上限 {_BAR_FETCH_CAP}，仅处理前 {_BAR_FETCH_CAP} 只",
                ]
                ordered = ordered[:_BAR_FETCH_CAP]
            else:
                known_limitations = ["bar 缺失/ST 状态未知的候选按条件剔除或标注"]
            dropped = {"missing_bar": 0, "st": 0, "mv": 0}
            st_unknown_seen = False

            # 批量拉取（SDK pipeline mget）；失败时降级为逐只拉取
            fetch_codes = [m["code"] for m in ordered]
            bars_by_code: dict[str, list[dict]] = {}
            if fetch_codes and effective_date:
                try:
                    batch_raw = client.get_data(
                        fetch_codes,
                        start=effective_date,
                        end=effective_date,
                        frequency="1d",
                        fields=None,
                        fq=None,
                        limit=None,
                    )
                    if isinstance(batch_raw, dict):
                        bars_by_code = {
                            code: _normalize_rows(rows)
                            for code, rows in batch_raw.items()
                        }
                except Exception:  # noqa: BLE001 - 批量失败降级逐只
                    bars_by_code = {}
            fallback_single = not bars_by_code

            kept: list[dict] = []
            for item in ordered:
                code = item["code"]
                cross_date = item["cross_date"]
                signal = item["signal"]
                crosses = item["crosses"]
                if fallback_single:
                    try:
                        rows = _normalize_rows(client.get_data(
                            code,
                            start=effective_date,
                            end=effective_date,
                            frequency="1d",
                            fields=None,
                            fq=None,
                            limit=None,
                        ))
                    except Exception:  # noqa: BLE001 - 单只拉取失败不中断整体
                        rows = []
                else:
                    rows = bars_by_code.get(code) or []
                if not rows:
                    dropped["missing_bar"] += 1
                    continue
                bar = None
                for row in rows:
                    if str(row.get("date")) == str(effective_date):
                        bar = row
                        break
                if bar is None:
                    bar = rows[-1]
                raw_mv = bar.get("float_mv")
                try:
                    float_mv = float(raw_mv or 0)
                except (TypeError, ValueError):  # noqa: BLE001 - 非法值按 0 处理
                    float_mv = 0.0
                is_st = _parse_is_st(bar.get("is_st"))
                if is_st is None:
                    st_unknown_seen = True
                if is_st is True and exclude_st:
                    dropped["st"] += 1
                    continue
                if mv_min is not None and float_mv < mv_min:
                    dropped["mv"] += 1
                    continue
                if mv_max is not None and float_mv > mv_max:
                    dropped["mv"] += 1
                    continue
                kept.append({
                    "code": code,
                    "name": bar.get("name"),
                    "close": bar.get("close"),
                    "cross_date": cross_date,
                    "signal": signal,
                    "crosses": crosses,
                    "pct_chg": bar.get("pct_chg"),
                    "float_mv": float_mv,
                    "is_st": is_st,
                })
            # d2) 进度：bar 过滤完成（候选数）
            notify_progress("filter_done", f"候选 {len(kept)} 只")
            if st_unknown_seen:
                known_limitations.append("上市日期无涨跌幅限制期/ST 状态未知")

            # e) 候选组装：cross_date 降序、code 升序，取前 limit
            kept.sort(
                key=lambda c: (
                    -_date_sort_key(c["cross_date"]) if c["cross_date"] is not None else 1,
                    c["code"],
                ),
            )
            if len(kept) > limit:
                truncated = True
                kept = kept[:limit]

            cross_window_text = (
                cross_window_note
                if cross_conditions is not None
                else "未启用指标交叉（候选按板块/市值/ST 条件筛选）"
            )
            return {
                "ok": True,
                "result": {
                    "source": "pybao",
                    "date": effective_date,
                    "universe_source": universe_source,
                    "universe_count": len(universe),
                    "matched_count": len(matched),
                    "candidates": kept,
                    "dropped": {
                        "missing_bar": dropped["missing_bar"],
                        "st": dropped["st"],
                        "mv": dropped["mv"],
                    },
                    "truncated": truncated,
                    "limit": limit,
                    "conditions": {
                        "indicator_cross": cross_conditions,
                        "float_mv_min": mv_min,
                        "float_mv_max": mv_max,
                        "exclude_st": exclude_st,
                    },
                    "methodology": {
                        "cross_window": cross_window_text,
                        "mv_unit": "与日K float_mv 字段同单位（元）",
                    },
                    "known_limitations": known_limitations,
                    "elapsed_ms": int((time.time() - _t0) * 1000),
                },
            }
    except Exception as exc:  # noqa: BLE001 - 计算异常统一转为中文错误
        return _internal_error(f"选股计算失败: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    # 离线自检：pybao 缺失时应得到明确降级错误
    print(compute_indicators({"indicators": ["macd"], "codes": ["600633"]}))
