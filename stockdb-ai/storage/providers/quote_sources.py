#!/usr/bin/env python3
"""quote_sources — 数据层行情源 provider：打板竞价快照采集（D11 落位，原 auction_collect）

D11（架构决策）：采集执行归数据层（storage/providers/quote_sources.py），
编排归服务层（services/auction_tasks.py 调用 fetch_quotes）。本模块是纯数据层
代码——不知道"调用者是谁"，不承载业务规则。

纯标准库、零依赖；输入股票清单 → 输出开盘竞价快照。
主源：腾讯 qt.gtimg.cn（一次批量 ≤50 只，零鉴权、不封 IP）；
备源：东财 push2（单只逐只拉，主源失败/部分失败时降级）。
限流：每批之间 sleep ≥ RATE_LIMIT_SECONDS（每日仅单次快照，个人研究用量）。

数据口径（用户拍板）：9:25 集合竞价价 = 当日开盘价（open_price）。
停牌/无竞价 → open_price=None，统计剔除并计入 errors。

0.9.9：从 webui 根目录 auction_collect.py 迁入（git mv，契约与行为不变）。
"""

from __future__ import annotations

# ---- 任务A 新增：纯标准库依赖 ----
import json              # 备源东财 JSON 解析
import re                # 从 v_sh600000="..." 行前缀提取 6 位代码
import time              # 批间限流 sleep
import urllib.request    # 主备源 HTTP（模块级导入：自检用 mock patch urlopen 即可替换）
from datetime import datetime

QUOTE_CONTRACT = "auction-snapshot-v1"  # 快照契约版本（写入 raw 信封）
PRIMARY_SOURCE = "tencent"
FALLBACK_SOURCE = "eastmoney"
BATCH_SIZE = 50                     # 主源单批上限
RATE_LIMIT_SECONDS = 1.0            # 批间限流（腾讯友好限频）
DEFAULT_TIMEOUT = 8.0               # 单批网络超时（秒）

# 腾讯字段位（split("~") 后）：
#   1=代码 2=名称 3=今开(9:25竞价价) 4=昨收 6=成交量(手) 7=外盘 37=成交额(万)
# 停牌/无竞价：今开字段为空或 0 时按 None 处理。
TENCENT_FIELD_OPEN = 3
TENCENT_FIELD_PREV_CLOSE = 4
TENCENT_FIELD_VOLUME = 6
TENCENT_FIELD_AMOUNT = 37

# 东财字段：f46=今开 f60=昨收 f47=成交量 f48=成交额；secid 前缀：沪(6/688)→"1."，深(0/3)→"0."
EASTMONEY_FIELDS = "f43,f46,f47,f48,f57,f58,f60"

# ---- 任务A 新增：错误码常量（镜像 8 错误码体系的 NO_DATA，见 paper_core/mx_client） ----
ERROR_NO_DATA = "NO_DATA"  # 无数据：主备源均失败时记入 errors（契约键 code）

# 东财接口对停牌/空值可能返回 "-"（占位符），与空串一样按"无值"处理
_EM_MISSING = {"", "-"}


def _secid(code: str) -> str:
    """6 位代码 → 东财 secid（沪 1. / 深 0.）。"""
    return ("1." if code.startswith(("6", "9")) else "0.") + code


def _to_float(value) -> float | None:
    """宽松数值转换：None/空串/'-'/非数字 → None（不抛异常，供停牌兜底）。

    关键点：两种源在无数据时都可能给空占位（"" 或 "-"），统一在此收敛为
    None，避免下游被 "9.25"（字符串）与 9.25（浮点）混杂的字段弄崩。
    """
    if value is None or value in _EM_MISSING:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tencent_symbol(code: str) -> str:
    """6 位代码 → 腾讯批量 URL 的符号：沪(6/9)→sh，深(0/3)→sz（与 _secid 同口径）。

    关键点：0 打头代码（如 000001）必须保留前导零拼进 URL，
    所以归一化时统一用 zfill(6) 补齐（见 fetch_quotes）。
    """
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def _http_get(url: str, timeout: float) -> bytes:
    """统一 GET 封装：浏览器形态 UA + Referer，规避默认 urllib UA 被源拦截。

    超时由调用方传入（主源 8s/批、备源单只同 8s）；出错直接抛给上层，
    由 fetch_quotes 统一捕获归入 errors——网络异常绝不外泄到业务层。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Referer": "https://gu.qq.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_quotes(codes: list[str], timeout: float = DEFAULT_TIMEOUT) -> dict:
    """批量采集开盘竞价快照。

    入参：codes 股票代码列表（6 位数字字符串）。
    返回：{
      "ok":        [{code, open_price, prev_close, volume, amount, source, raw}, ...],
      "errors":    [{code, symbol, message}, ...],        # 8 错误码体系
      "source_usage": {"tencent": n, "eastmoney": n},     # 各源成功条数（审计）
      "fetched_at": "YYYY-MM-DDTHH:MM:SS",
      "contract":  QUOTE_CONTRACT,
    }
    策略：主源按 BATCH_SIZE 分批（批间 sleep RATE_LIMIT_SECONDS）；
    主源整批失败 → 该批全部走备源逐只拉；单只解析失败 → errors（不阻塞其余）。
    """
    # 归一化：容忍 int 入参（600000）与缺前导零（1→000001），去重保序（幂等重跑）
    codes = list(dict.fromkeys(str(c).strip().zfill(6) for c in codes if str(c).strip()))
    ok: list[dict] = []
    errors: list[dict] = []
    source_usage = {PRIMARY_SOURCE: 0, FALLBACK_SOURCE: 0}
    remaining = set(codes)  # 尚未拿到数据的代码；结束时仍在的 → NO_DATA

    def _fallback(code: str) -> None:
        """单只走备源（东财逐只拉）：成功计入 ok；失败留给末尾统一记 NO_DATA。

        关键点：错误只在末尾统一追加一次，避免这里记一条、兜底又记一条
        造成同一只股票重复报错。
        """
        snap = _fetch_eastmoney(code, timeout)
        if snap is None:
            return
        snap["source"] = FALLBACK_SOURCE
        ok.append(snap)
        source_usage[FALLBACK_SOURCE] += 1
        remaining.discard(code)

    for i in range(0, len(codes), BATCH_SIZE):
        if i > 0:
            # 批间限流：腾讯友好限频 ≤1 req/s（设计文档调度语义；首批发不必等）
            time.sleep(RATE_LIMIT_SECONDS)
        batch = codes[i:i + BATCH_SIZE]

        try:
            lines = _fetch_tencent_batch(batch, timeout)["lines"]
        except Exception:
            # 降级规则（设计文档）：主源失败/超时 → 重试 1 次 → 仍失败才切备源
            try:
                lines = _fetch_tencent_batch(batch, timeout)["lines"]
            except Exception:
                lines = None

        if lines is None:
            # 主源整批失败（含重试）→ 该批全部走备源，逐只拉取
            for code in batch:
                _fallback(code)
            continue

        parsed = {}
        for line in lines:
            snap = _parse_tencent_line(line)
            if snap is not None:
                # 同码重复行后者覆盖（幂等，兼容腾讯偶发重复输出）
                parsed[snap["code"]] = snap

        for code in batch:
            snap = parsed.get(code)
            if snap is None:
                # 单只缺失/解析失败 → 该只走备源（不阻塞本批其余股票）
                _fallback(code)
                continue
            snap["source"] = PRIMARY_SOURCE
            ok.append(snap)
            source_usage[PRIMARY_SOURCE] += 1
            remaining.discard(code)

    # 兜底：主备源都取不到的代码 → NO_DATA errors（按入参顺序，输出稳定）
    for code in codes:
        if code in remaining:
            errors.append({
                "code": ERROR_NO_DATA,
                "symbol": code,
                "message": "主备源均失败，无数据",
            })

    return {
        "ok": ok,
        "errors": errors,
        "source_usage": source_usage,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "contract": QUOTE_CONTRACT,
    }


def _fetch_tencent_batch(codes: list[str], timeout: float) -> dict:
    """单批腾讯请求：返回 {"lines": [...原始文本行...]}；失败抛异常。

    腾讯批量接口用逗号拼接符号（q=sh600000,sz000001），响应是若干
    v_sh600000="..." 片段，可能整段一行、也可能每行一个——统一按
    ";" 与换行切分，两种形态都兼容。
    """
    url = "https://qt.gtimg.cn/q=" + ",".join(_tencent_symbol(c) for c in codes)
    body = _http_get(url, timeout)
    # 腾讯正文是 GBK（含中文名）；自检 mock 可能给 UTF-8 → 先试 UTF-8 再退 GBK
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("gbk", errors="replace")
    chunks = [c.strip() for c in text.replace("\n", ";").split(";") if c.strip()]
    return {"lines": chunks}


def _parse_tencent_line(line: str) -> dict | None:
    """v_sh600000="..." 行 → 快照 dict；解析失败返回 None。

    关键点：代码从行前缀（v_sh600000）用正则提取，比依赖正文第 1/2 位
    稳定（契约注释位序与真实响应存在差异，前缀带代码是唯一可靠来源）；
    停牌/无竞价（今开空或 0）→ open_price=None，仍返回快照（计入 ok，
    供统计剔除），不算解析失败。
    """
    key, _, rest = line.partition("=")
    rest = rest.strip()
    if not rest.startswith('"'):
        return None
    end = rest.find('"', 1)
    if end < 0:
        return None
    fields = rest[1:end].split("~")
    if len(fields) <= TENCENT_FIELD_OPEN:
        # 连今开位都取不到 → 结构坏行，交给备源逐只兜底
        return None
    m = re.search(r"v_(s[hz])(\d{6})", key)
    if m is None:
        return None
    open_raw = _to_float(fields[TENCENT_FIELD_OPEN])
    return {
        "code": m.group(2),
        "open_price": None if open_raw is None or open_raw == 0 else open_raw,
        "prev_close": (_to_float(fields[TENCENT_FIELD_PREV_CLOSE])
                       if len(fields) > TENCENT_FIELD_PREV_CLOSE else None),
        "volume": (_to_float(fields[TENCENT_FIELD_VOLUME])
                   if len(fields) > TENCENT_FIELD_VOLUME else None),
        "amount": (_to_float(fields[TENCENT_FIELD_AMOUNT])
                   if len(fields) > TENCENT_FIELD_AMOUNT else None),
        "raw": line,  # 原始行全文入 raw，可重放审计、口径可重算
    }


def _fetch_eastmoney(code: str, timeout: float) -> dict | None:
    """单只东财请求 → 快照 dict；失败返回 None。

    东财 JSON {data:{f46,f47,f48,f57,f58,f60}}：f46=今开 f60=昨收
    f47=成交量(手) f48=成交额。f46 为空/0 → open_price=None（停牌兜底）。

    注意单位差异：腾讯 amount 是"万元"，东财 f48 是"元"——raw 保留源
    原始值不做换算，单位统一留给业务层（任务B 指标）处理，采集器不越权。
    """
    url = (f"https://push2.eastmoney.com/api/qt/stock/get"
           f"?secid={_secid(code)}&fields={EASTMONEY_FIELDS}")
    try:
        body = _http_get(url, timeout)
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None  # 股票不存在 / 接口异常时 data 为 null
    open_raw = _to_float(data.get("f46"))
    return {
        "code": code,
        "open_price": None if open_raw is None or open_raw == 0 else open_raw,
        "prev_close": _to_float(data.get("f60")),
        "volume": _to_float(data.get("f47")),
        "amount": _to_float(data.get("f48")),
        "raw": data,  # 原始 JSON data（含 f43/f57/f58 等未用字段，通用性由 raw 承载）
    }
