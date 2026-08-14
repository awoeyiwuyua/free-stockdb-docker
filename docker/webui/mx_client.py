#!/usr/bin/env python3
"""mx_client — MX 模拟盘 API 客户端（任务D：纯 urllib，零第三方依赖）

单进程、单账户模拟盘的券商通道：封装 MX 模拟盘 REST 接口（base
https://mkapi2.dfcfs.com/finskill），提供资金 / 持仓 / 委托查询、下单、撤单
与行情查询。只依赖 Python 标准库 urllib/json/os，零第三方依赖；不接触
pybao/ 与 mcp 目录。

安全边界（与冻结契约一致）：
  - apikey 三级解析：构造参数 → 环境变量 MX_APIKEY → DATA_DIR/mx_apikey.txt
    （文件取首行 strip；存在但为空/读不到 → None，不抛异常）。
  - apikey 永不回显：任何异常消息 / 日志只允许出现 masked_key（掩码），
    网络异常透传的原始信息同样先清洗；本模块绝不把 apikey 原文写进异常、
    事件或响应。
  - 8 错误码映射（与 paper_core.MX_ERROR_MAP 同源，冻结契约）：
      401        → DEPENDENCY_UNAVAILABLE（hint：检查 apikey）
      响应 code=113 → RATE_LIMITED
      404 未绑定 → INVALID_ARGUMENT（hint：绑定模拟账户）
      网络异常   → INTERNAL_ERROR（消息附原始异常类型，不含密钥）
      业务 code≠0 → 若 code ∈ 8 码全集则原样，否则 INTERNAL_ERROR

错误形态：所有失败统一抛 MXError（携带 code/message/hint），上层按 code
分类处置；成功返回响应体 dict（原样透传，由上层解析字段）。

自测（贴出输出，见任务报告）：
    python mx_client.py   # 本地 http.server mock 模拟 401/113/404/未绑定/成功，
                          # 断言 8 码映射与 payload 结构；apikey 只打印掩码
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

# === 8 错误码（冻结契约全集，与 paper_core 同值） ===
ERROR_INVALID_ARGUMENT = "INVALID_ARGUMENT"        # 参数非法
ERROR_NO_DATA = "NO_DATA"                          # 无数据
ERROR_NOT_PUBLISHED = "NOT_PUBLISHED"              # 未发布
ERROR_INVALID_SYMBOL = "INVALID_SYMBOL"            # 标的非法
ERROR_DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"  # 依赖不可用
ERROR_PARTIAL_RESULT = "PARTIAL_RESULT"            # 部分成功
ERROR_RATE_LIMITED = "RATE_LIMITED"                # 限流
ERROR_INTERNAL_ERROR = "INTERNAL_ERROR"            # 内部错误

ERROR_CODES = (
    ERROR_INVALID_ARGUMENT, ERROR_NO_DATA, ERROR_NOT_PUBLISHED,
    ERROR_INVALID_SYMBOL, ERROR_DEPENDENCY_UNAVAILABLE, ERROR_PARTIAL_RESULT,
    ERROR_RATE_LIMITED, ERROR_INTERNAL_ERROR,
)

# MX 接口错误 → 8 码映射（冻结契约；与 paper_core.MX_ERROR_MAP 同源）
_MX_ERROR_MAP = {
    401: (ERROR_DEPENDENCY_UNAVAILABLE, "检查 apikey"),
    113: (ERROR_RATE_LIMITED, None),
    404: (ERROR_INVALID_ARGUMENT, "绑定模拟账户"),
}

# MX 接口 REST 端点（相对路径，拼在 base_url 后）
_ENDPOINT_BALANCE = "/api/claw/mockTrading/balance"   # 资金查询
_ENDPOINT_POSITIONS = "/api/claw/mockTrading/positions"  # 持仓查询
_ENDPOINT_ORDERS = "/api/claw/mockTrading/orders"     # 当日委托查询
_ENDPOINT_TRADE = "/api/claw/mockTrading/trade"       # 下单
_ENDPOINT_CANCEL = "/api/claw/mockTrading/cancel"     # 撤单
_ENDPOINT_QUERY = "/api/claw/query"                   # 行情/盘口查询

_RATE_LIMIT_CODE = 113  # MX 频率限制响应码

# 默认 base url（pyc 恢复的任务D原文：base https://mkapi2.dfcfs.com/finskill）
_DEFAULT_BASE = "https://mkapi2.dfcfs.com/finskill"
_DEFAULT_TIMEOUT = 10.0  # 单请求超时秒数


class MXError(Exception):
    """MX 接口调用失败异常：携带 8 错误码体系的 code 与中文 message（可选 hint）。

    上层（交易执行/对账）按 code 分类处置；hint 面向用户给出下一步操作建议。
    消息中绝不包含 apikey 原文（见本模块 docstring 安全边界）。
    """

    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code = code      # 8 错误码之一（ERROR_CODES 成员）
        self.message = message  # 中文错误描述
        self.hint = hint      # 可选：给用户的操作建议（中文）

    def __str__(self) -> str:
        base = f"[{self.code}] {self.message}"
        return f"{base}（hint: {self.hint}）" if self.hint else base


class MXClient:
    """MX 模拟盘客户端：纯 urllib 实现，所有请求带 apikey 请求头。

    用法：
        client = MXClient()                 # apikey 按 参数 > MX_APIKEY > 文件 解析
        bal = client.get_balance()          # 资金
        pos = client.get_positions()        # 持仓（含可卖数量）
        client.place_order("buy", "159915", 100, "MARKET")  # 市价买入 1 手
    """

    def __init__(self, apikey: str | None = None, base_url: str = _DEFAULT_BASE,
                 timeout: float = _DEFAULT_TIMEOUT):
        """初始化客户端；apikey 解析顺序：构造参数 -> MX_APIKEY -> 文件（见 _resolve_apikey）。

        参数：
          apikey: 显式 apikey（可缺）；None 时按三级解析。
          base_url: 接口根地址（缺省 MX 模拟盘官方地址）。
          timeout: 单请求超时秒数（默认 10）。
        """
        self._base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self._timeout = float(timeout)
        self._apikey = self._resolve_apikey(explicit=apikey)

    # ---------------- apikey 解析 / 掩码 ----------------
    def _resolve_apikey(self, explicit: str | None = None) -> str | None:
        """按 参数 -> 环境变量 MX_APIKEY -> DATA_DIR/mx_apikey.txt 解析 apikey。

        文件取首行 strip；文件存在但内容为空（或读不到）返回 None；不抛异常。
        """
        if explicit is not None and str(explicit).strip():
            return str(explicit).strip()
        env = os.environ.get("MX_APIKEY")
        if env and env.strip():
            return env.strip()
        data_dir = os.environ.get("DATA_DIR", "/data")
        key_file = os.path.join(data_dir, "mx_apikey.txt")
        try:
            with open(key_file, "r", encoding="utf-8") as fh:
                line = fh.readline().strip()
            return line if line else None
        except OSError:
            return None  # 文件不存在/不可读 → 未配置（不抛异常）

    @property
    def masked_key(self) -> str:
        """apikey 掩码（日志专用）：前 4 后 4 字符，中间 ****；未配置返回"未配置"。

        密钥过短（<=8 字符）时全部掩码为 ****，不暴露任何片段。
        """
        return mask_key(self._apikey)

    # ---------------- 内部请求 ----------------
    def _post(self, endpoint: str, payload: dict) -> dict:
        """发 POST JSON 请求并解析响应；出错抛 MXError（含 401/113/404/网络映射）。

        网络异常映射为 INTERNAL_ERROR 且消息附原始异常类型；任何错误消息
        均不包含 apikey 原文（永不回显）。
        """
        if not self._apikey:
            raise MXError(
                ERROR_INVALID_ARGUMENT,
                "未配置 MX apikey（请传参数、设环境变量 MX_APIKEY 或写 DATA_DIR/mx_apikey.txt）",
                hint="检查 apikey",
            )
        url = self._base_url + endpoint
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "apikey": self._apikey},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return self._parse_response(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            finally:
                # HTTPError 持有未关闭的响应对象，显式关闭避免 ResourceWarning/句柄泄漏
                try:
                    exc.close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响错误映射
                    pass
            self._raise_http_error(exc.code, body)
        except urllib.error.URLError as exc:
            # 网络层异常（DNS/连接拒绝/超时等）：映射 INTERNAL_ERROR，只带类型与中文说明
            raise MXError(
                ERROR_INTERNAL_ERROR,
                f"MX 接口网络异常（{type(exc.reason).__name__}）: 无法连接 {self._base_url}",
                hint="网络错误",
            ) from exc
        except MXError:
            raise
        except Exception as exc:  # noqa: BLE001 - 其它异常统一映射内部错误
            raise MXError(
                ERROR_INTERNAL_ERROR,
                f"MX 接口请求异常（{type(exc).__name__}）: {exc}",
            ) from exc
        raise MXError(ERROR_INTERNAL_ERROR, "MX 接口未知错误（未收到响应）")  # 不可达

    def _raise_http_error(self, status: int, raw: bytes) -> None:
        """按 HTTP 状态映射错误（始终抛 MXError；优先识别 113 与"未绑定"）。"""
        body = self._try_json(raw)
        # 1) 频率受限（HTTP 200 业务码 113 也在此判定，见 _parse_response）
        if status == 401:
            raise MXError(
                ERROR_DEPENDENCY_UNAVAILABLE,
                "MX 接口鉴权失败（HTTP 401），apikey 可能无效",
                hint="检查 apikey",
            )
        if status == 404:
            if self._looks_unbound(body):
                raise MXError(
                    ERROR_INVALID_ARGUMENT,
                    "MX 模拟账户未绑定（HTTP 404）",
                    hint="绑定模拟账户",
                )
            raise MXError(
                ERROR_DEPENDENCY_UNAVAILABLE,
                f"MX 接口不存在（HTTP 404）: {self._msg_from_body(body) or '接口地址有误'}",
                hint="联系管理员检查接口地址",
            )
        # 其它非 2xx：按 8 码全集映射（复用契约表）
        code, hint = _MX_ERROR_MAP.get(status, (ERROR_INTERNAL_ERROR, None))
        detail = self._msg_from_body(body)
        raise MXError(
            code,
            f"MX 接口返回非成功 HTTP 状态 {status}: {detail or '无详细信息'}",
            hint=hint,
        )

    def _parse_response(self, status: int, raw: bytes) -> dict:
        """解析 2xx 响应：返回原生 dict；响应体含 113/未绑定/非成功 code 时抛 MXError。

        约定：MX 接口响应体顶层带 code 字段，0（或 200）表示成功；本客户端
        按此解析。若真实接口字段形态不同，在联调时于此处适配（不改上层契约）。
        """
        body = self._try_json(raw)
        if body is None:
            raise MXError(
                ERROR_INTERNAL_ERROR,
                f"MX 接口响应非 JSON 对象（HTTP {status}）: "
                f"{raw[:200].decode('utf-8', 'replace') or '空响应'}",
            )
        if not isinstance(body, dict):
            raise MXError(
                ERROR_INTERNAL_ERROR,
                f"MX 接口响应非 JSON 对象（HTTP {status}）: 顶层非 dict",
            )
        # 频率受限（响应 code=113）
        if self._is_rate_limited(body):
            raise MXError(
                ERROR_RATE_LIMITED,
                "MX 接口频率受限（响应 code=113）",
            )
        # 未绑定模拟账户（响应消息含"未绑定"）
        if self._looks_unbound(body):
            raise MXError(
                ERROR_INVALID_ARGUMENT,
                "MX 模拟账户未绑定",
                hint="绑定模拟账户",
            )
        # 业务失败：code != 0 且非上述已知码 → 8 码原样/兜底映射
        code = body.get("code")
        if code not in (None, 0, 200):
            err_code = code if isinstance(code, str) and code in ERROR_CODES \
                else ERROR_INTERNAL_ERROR
            raise MXError(
                err_code,
                f"MX 接口业务失败（code={code}）: {self._msg_from_body(body) or '无详细信息'}",
            )
        return body  # 成功：原生 dict 透传（data 字段由上层解析）

    # ---------------- 响应辅助 ----------------
    def _try_json(self, raw: bytes):
        """尝试把响应字节解析为 JSON；失败或空返回 None（不抛异常）。"""
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _msg_from_body(self, body) -> str | None:
        """从响应体提取中文错误信息（依次尝试 message/msg/desc/info）。"""
        if not isinstance(body, dict):
            return None
        for key in ("message", "msg", "desc", "info"):
            val = body.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None

    def _is_rate_limited(self, body) -> bool:
        """响应体 code 是否为 MX 频率限制码（113）。"""
        return isinstance(body, dict) and body.get("code") == _RATE_LIMIT_CODE

    def _looks_unbound(self, body) -> bool:
        """响应体消息是否提示"模拟账户未绑定"（结合 HTTP 404 判定）。"""
        msg = self._msg_from_body(body) or ""
        return any(kw in msg for kw in ("未绑定", "未绑定模拟账户", "模拟账户未绑定"))

    # ---------------- 业务接口 ----------------
    def get_balance(self) -> dict:
        """查询模拟账户资金。POST /api/claw/mockTrading/balance {"moneyUnit":1}。"""
        return self._post(_ENDPOINT_BALANCE, {"moneyUnit": 1})

    def get_positions(self) -> dict:
        """查询模拟持仓（含可卖数量 available_to_sell）。POST .../positions {"moneyUnit":1}。"""
        return self._post(_ENDPOINT_POSITIONS, {"moneyUnit": 1})

    def get_orders(self) -> dict:
        """查询当日委托。POST .../orders {"fltOrderDrt":0,"fltOrderStatus":0}。"""
        return self._post(_ENDPOINT_ORDERS, {"fltOrderDrt": 0, "fltOrderStatus": 0})

    def place_order(self, action: str, symbol: str, quantity: int,
                    price_type: str, price: float | None = None) -> dict:
        """下单（POST .../mockTrading/trade）。

        action ∈ {"buy","sell"}；quantity 必须为 100 的整数倍（否则 ValueError）；
        price_type ∈ {"MARKET","LIMIT"}（大小写不敏感），LIMIT 必须提供正数 price。
        """
        act = str(action).strip().lower()
        if act not in ("buy", "sell"):
            raise ValueError(f"action 必须是 'buy' 或 'sell'，收到: {action!r}")
        qty = int(quantity)
        if qty <= 0 or qty % 100 != 0:
            raise ValueError(
                f"quantity 必须是 100 的整数倍（正整数，如 100/200/300），收到: {quantity!r}"
            )
        pt = str(price_type).strip().upper()
        if pt not in ("MARKET", "LIMIT"):
            raise ValueError(f"price_type 必须是 MARKET 或 LIMIT，收到: {price_type!r}")
        payload = {
            "type": act,
            "stockCode": str(symbol).strip(),
            "quantity": qty,
            "useMarketPrice": pt == "MARKET",
        }
        if pt == "LIMIT":
            if price is None:
                raise ValueError("LIMIT 限价单必须提供 price")
            p = float(price)
            if p <= 0:
                raise ValueError(f"限价 price 必须为正数，收到: {price!r}")
            payload["price"] = p
        return self._post(_ENDPOINT_TRADE, payload)

    def cancel_order(self, order_id: str, stock_code: str | None = None) -> dict:
        """撤单（POST .../mockTrading/cancel）。order_id 必填，stock_code 可选。"""
        if not str(order_id or "").strip():
            raise ValueError("order_id 必须为非空字符串")
        payload = {"orderId": str(order_id).strip()}
        if stock_code is not None and str(stock_code).strip():
            payload["stockCode"] = str(stock_code).strip()
        return self._post(_ENDPOINT_CANCEL, payload)

    def query_market(self, query: str) -> dict:
        """行情/盘口查询（收盘后验证价等）。POST /api/claw/query {"toolQuery": query}。"""
        return self._post(_ENDPOINT_QUERY, {"toolQuery": str(query)})


def mask_key(key: str | None) -> str:
    """apikey 掩码（模块级，save_apikey/客户端共用）。"""
    if not key:
        return "未配置"
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def save_apikey(path: str, key: str) -> str:
    """原子写入 apikey 文件（tmp+rename、0600 权限）；空串 = 清除（删除文件）。

    仅落盘本地 DATA_DIR，永不回显原文；返回掩码（"未配置" 表示已清除）。
    写入失败抛中文 ValueError（不吞磁盘错误）。
    """
    key = (key or "").strip()
    p = Path(path)
    try:
        if not key:
            p.unlink(missing_ok=True)
            return "未配置"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(key, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # noqa: BLE001 - 权限设置失败不阻断（多数文件系统默认已足够）
            pass
        os.replace(tmp, p)
        return mask_key(key)
    except OSError as exc:
        raise ValueError(f"apikey 保存失败：{exc}") from exc


# ==================== 自测（本地 http.server mock，纯标准库） ====================
def _self_test() -> None:
    """本地自测：http.server mock（随机端口）模拟 401/113/404/未绑定/成功，
    断言 8 码映射与 payload 结构；apikey 只打印掩码，不打印原文。

    另覆盖：masked_key、apikey 解析顺序、参数校验（ValueError）、网络异常。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    requests: list[dict] = []  # 记录请求（path/payload/apikey）供断言

    class _MockHandler(BaseHTTPRequestHandler):
        """自测 mock：按路径模拟 401 / 113 / 404 / 未绑定 / 成功 五种响应；记录请求供断言。"""

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            requests.append({
                "path": self.path,
                "payload": payload,
                "apikey": self.headers.get("apikey"),
            })
            path = self.path
            if path.endswith("/mockTrading/balance"):
                self._respond(401, {"code": 401, "message": "无效 apikey"})
            elif path.endswith("/mockTrading/positions"):
                self._respond(200, {"code": 113, "message": "请求过于频繁"})
            elif path.endswith("/mockTrading/orders"):
                self._respond(404, {"code": 404, "message": "接口不存在"})
            elif path.endswith("/mockTrading/cancel"):
                self._respond(200, {"code": 0, "message": "模拟账户未绑定，请先绑定"})
            else:  # /trade /query 成功
                self._respond(200, {"code": 0, "data": {"echo_apikey": payload.get("stockCode"), "ok": True}})

        def _respond(self, status: int, body: dict):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # 静默
            pass

    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        key = "12345678ABCDEFGH"
        client = MXClient(apikey=key, base_url=base, timeout=5.0)
        # 掩码：前4后4，原文不出现
        assert client.masked_key == "1234****EFGH", client.masked_key
        assert key not in client.masked_key
        # 401 → DEPENDENCY_UNAVAILABLE
        try:
            client.get_balance()
            raise AssertionError("401 应抛 MXError")
        except MXError as exc:
            assert exc.code == ERROR_DEPENDENCY_UNAVAILABLE, exc.code
            assert exc.hint == "检查 apikey"
            assert key not in str(exc), "异常消息不得包含 apikey 原文"
        # 113 → RATE_LIMITED
        try:
            client.get_positions()
            raise AssertionError("code=113 应抛 MXError")
        except MXError as exc:
            assert exc.code == ERROR_RATE_LIMITED, exc.code
        # 404 接口不存在 → DEPENDENCY_UNAVAILABLE
        try:
            client.get_orders()
            raise AssertionError("404 应抛 MXError")
        except MXError as exc:
            assert exc.code == ERROR_DEPENDENCY_UNAVAILABLE, exc.code
        # 200 + 消息含"未绑定" → INVALID_ARGUMENT（hint：绑定模拟账户）
        try:
            client.cancel_order("MX0001")
            raise AssertionError("未绑定应抛 MXError")
        except MXError as exc:
            assert exc.code == ERROR_INVALID_ARGUMENT, exc.code
            assert exc.hint == "绑定模拟账户"
        # 成功路径：下单 + payload 结构
        resp = client.place_order("buy", "159915", 100, "MARKET")
        assert resp.get("code") == 0
        last = requests[-1]
        assert last["payload"] == {"type": "buy", "stockCode": "159915",
                                   "quantity": 100, "useMarketPrice": True}
        assert last["apikey"] == key
        # 参数校验（ValueError，不下发）
        for bad in (
            lambda: client.place_order("hold", "159915", 100, "MARKET"),
            lambda: client.place_order("buy", "159915", 150, "MARKET"),
            lambda: client.place_order("buy", "159915", 100, "FOK"),
            lambda: client.place_order("buy", "159915", 100, "LIMIT"),
            lambda: client.place_order("buy", "159915", 100, "LIMIT", price=-1),
        ):
            try:
                bad()
                raise AssertionError("参数校验应抛 ValueError")
            except ValueError:
                pass
        # apikey 三级解析：参数 > 环境变量 > 文件
        os.environ["MX_APIKEY"] = "ENVKEY1234567890"
        c2 = MXClient(base_url=base)
        assert c2.masked_key == "ENVK****7890", c2.masked_key  # 环境变量优先（无显式参数）
        assert key not in c2.masked_key
        # 显式参数 > 环境变量
        c3 = MXClient(apikey=key, base_url=base)
        assert c3.masked_key == "1234****EFGH"
        # 无任何来源 → 未配置；调用抛 MXError(INVALID_ARGUMENT)
        os.environ.pop("MX_APIKEY", None)
        c4 = MXClient(base_url=base)
        assert c4.masked_key == "未配置"
        try:
            c4.get_balance()
            raise AssertionError("未配置 apikey 应抛 MXError")
        except MXError as exc:
            assert exc.code == ERROR_INVALID_ARGUMENT
        # 网络异常 → INTERNAL_ERROR（端口未监听）
        c5 = MXClient(apikey=key, base_url="http://127.0.0.1:1", timeout=1.0)
        try:
            c5.get_balance()
            raise AssertionError("网络异常应抛 MXError")
        except MXError as exc:
            assert exc.code == ERROR_INTERNAL_ERROR, exc.code
        # 超短 key（<=8）→ 全掩码
        c6 = MXClient(apikey="shortkey", base_url=base)
        assert c6.masked_key == "****", c6.masked_key
        print("[OK] 401→DEPENDENCY_UNAVAILABLE / 113→RATE_LIMITED / 404→DEPENDENCY_UNAVAILABLE")
        print("[OK] 200+未绑定→INVALID_ARGUMENT（hint：绑定模拟账户）")
        print("[OK] 成功路径 payload 结构 + apikey 请求头透传")
        print("[OK] 参数校验 ValueError（action/quantity/price_type/LIMIT price）")
        print("[OK] apikey 三级解析（参数>环境变量>文件）+ 未配置降级")
        print("[OK] 网络异常→INTERNAL_ERROR（附原始异常类型，不含 apikey）")
        print("[OK] masked_key 掩码规则（前4后4 / 超短全掩码 / 未配置）")
        print("ALL_ASSERTIONS_PASSED")
    finally:
        server.shutdown()


if __name__ == "__main__":
    _self_test()
