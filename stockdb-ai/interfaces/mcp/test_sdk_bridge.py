"""test_sdk_bridge — 0.9.0 M4 SDK 桥单测（离线，mock 上游 stockdb_full_mcp，无 pybao 依赖）。

覆盖：降级语义 / 参数 schema 生成 / 调用封装（df 强制 / 参数过滤 / 结果解析）/
错误码映射 / 服务器集成（_call_tool 路由 + sdk 契约信封）。
"""
import json
import sys
import unittest
from unittest import mock

from interfaces.mcp import stockdb_mcp_server as server  # noqa: E402 - 先导入（_MCP_DIR 入 path）
import sdk_bridge  # noqa: E402 - 顶层模块实例（与 server 内部引用同一实例，勿用 interfaces.mcp.sdk_bridge）


class _FakeMCPModule:
    """mock 上游 stockdb_full_mcp：6 个代表性函数（签名对齐上游）。"""

    def stockdb_get_bars(self, security: list, count: int = 30, unit: str = "1d",
                         fields: list = ("date", "open", "high", "low", "close"),
                         include_now: bool = False, end_dt: str = None,
                         fq_ref_date: str = None, df: bool = True,
                         skip_paused: bool = True, panel: bool = False):
        if df:
            return "<DataFrame object>"
        return str([{"date": "2026-08-12", "open": 11.26, "close": 11.1}])

    def stockdb_get_call_auction(self, security: list, start_date: str, end_date: str,
                                 fields: list = None):
        return str([{"code": security[0], "time": f"{start_date}T09:25:00"}])

    def stockdb_get_security_info(self, code: str, date: str = None):
        return "{'code': '000001', 'display_name': '\\u5e73\\u5b89\\u94f6\\u884c', 'type': 'stock'}"

    def stockdb_get_ticks(self, security: str, start_dt: str = None, end_dt: str = None,
                          count: int = None, fields: list = None, skip: bool = True,
                          df: bool = True):
        if df:
            return "<DataFrame object>"
        return "[1, 2, 3]"

    def stockdb_broken_tool(self, x: str = None):
        return "调用broken_tool失败: boom"

    def stockdb_raise_tool(self, x: str = None):
        raise ValueError("bad param value")


def _activate_fake():
    """把 fake 上游注入 sys.modules 并重置 sdk_bridge 懒加载状态。"""
    patcher = mock.patch.dict(sys.modules, {"stockdb_full_mcp": _FakeMCPModule()})
    patcher.start()
    sdk_bridge._full_mcp = None
    sdk_bridge._IMPORT_ERROR = None
    return patcher


class SdkBridgeDegradeTest(unittest.TestCase):
    """无上游模块（离线/无 pybao）：工具已知但不可用 → DEPENDENCY_UNAVAILABLE。"""

    def setUp(self):
        # 确保本测试环境确实无上游（真实环境可能已加载——用假失败重置）
        sdk_bridge._full_mcp = None
        sdk_bridge._IMPORT_ERROR = "test-degraded"

    def test_known_names_static_41(self):
        """静态已知清单 = 41 个上游工具名（与 stockdb_full_mcp 对齐）。"""
        self.assertEqual(len(sdk_bridge.KNOWN_SDK_TOOL_NAMES), 41)
        for name in ("get_bars", "get_call_auction", "run_query", "alpha", "MACD",
                     "get_fundamentals", "list_query_tables"):
            self.assertIn(name, sdk_bridge.KNOWN_SDK_TOOL_NAMES)

    def test_tool_specs_empty_when_unavailable(self):
        """上游不可用 → tool_specs() 为空（服务器不注册 SDK 工具）。"""
        self.assertEqual(sdk_bridge.tool_specs(), [])

    def test_call_tool_unavailable_raises(self):
        """上游不可用 → call_tool 抛 ValueError（服务器映射 DEPENDENCY_UNAVAILABLE）。"""
        with self.assertRaises(ValueError):
            sdk_bridge.call_tool("get_bars", {"security": ["000001"]})


class SdkBridgeSchemaTest(unittest.TestCase):
    """参数 schema 生成：类型映射 / required / df 排除 / 默认值。"""

    def _activate(self):
        p = _activate_fake()
        self.addCleanup(p.stop)

    def test_build_schema_excludes_df_and_required(self):
        self._activate()
        specs = {s["name"]: s for s in sdk_bridge.tool_specs()}
        self.assertEqual(len(specs), 6)  # fake 模块 6 个 stockdb_* 函数
        bars = specs["get_bars"]["inputSchema"]
        self.assertIn("security", bars["properties"])
        self.assertNotIn("df", bars["properties"])  # JSON 形态强制，不暴露
        self.assertEqual(bars["required"], ["security"])
        self.assertEqual(bars["properties"]["security"]["type"], "array")

    def test_call_auction_required(self):
        self._activate()
        specs = {s["name"]: s for s in sdk_bridge.tool_specs()}
        ca = specs["get_call_auction"]["inputSchema"]
        self.assertEqual(ca["required"], ["security", "start_date", "end_date"])


class SdkBridgeCallTest(unittest.TestCase):
    """调用封装：df 强制 / 参数过滤 / repr 解析 / 错误映射。"""

    def _activate(self):
        p = _activate_fake()
        self.addCleanup(p.stop)

    def test_df_forced_false_and_repr_parsed(self):
        """上游 df 参数被强制 false；str(repr) 三级解析还原 dict。"""
        self._activate()
        result = sdk_bridge.call_tool("get_bars", {"security": ["000001"], "count": 2})
        self.assertIsInstance(result, list)  # 原生解析结果；信封由 _apply_contract 包装
        self.assertEqual(result[0]["date"], "2026-08-12")
        self.assertEqual(result[0]["close"], 11.1)

    def test_repr_single_quotes_parsed(self):
        """单引号 repr（get_security_info 形态）经 literal_eval 还原。"""
        self._activate()
        result = sdk_bridge.call_tool("get_security_info", {"code": "000001"})
        self.assertEqual(result["code"], "000001")
        self.assertEqual(result["display_name"], "平安银行")

    def test_df_true_rejected(self):
        """用户显式传 df=true → INVALID_ARGUMENT 语义（ValueError）。"""
        self._activate()
        with self.assertRaises(ValueError):
            sdk_bridge.call_tool("get_bars", {"security": ["000001"], "df": True})

    def test_panel_true_rejected(self):
        self._activate()
        with self.assertRaises(ValueError):
            sdk_bridge.call_tool("get_bars", {"security": ["000001"], "panel": True})

    def test_unknown_param_ignored(self):
        """schema 之外的参数被忽略（防御），不影响调用。"""
        self._activate()
        result = sdk_bridge.call_tool("get_bars", {"security": ["000001"], "bogus": 1})
        self.assertEqual(result[0]["date"], "2026-08-12")

    def test_upstream_error_string_maps_runtime_error(self):
        """上游返回"调用X失败: ..." → RuntimeError（服务器映射 INTERNAL_ERROR）。"""
        self._activate()
        with self.assertRaises(RuntimeError):
            sdk_bridge.call_tool("broken_tool", {})

    def test_upstream_value_error_propagates(self):
        """上游函数抛 ValueError（参数归一化失败）→ 原样抛（→ INVALID_ARGUMENT）。"""
        self._activate()
        with self.assertRaises(ValueError):
            sdk_bridge.call_tool("raise_tool", {})

    def test_truncate_cap(self):
        """大列表结果截断 + truncated 标记。"""
        self._activate()
        big = str([[i] for i in range(sdk_bridge.RESULT_CAP + 10)])
        with mock.patch.object(_FakeMCPModule, "stockdb_get_ticks",
                               return_value=big, create=True):
            result = sdk_bridge.call_tool("get_ticks", {"security": "000001"})
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["data"]), sdk_bridge.RESULT_CAP)
        self.assertEqual(result["total"], sdk_bridge.RESULT_CAP + 10)


class SdkBridgeServerIntegrationTest(unittest.TestCase):
    """服务器集成：_call_tool 路由 + sdk 契约信封。"""

    def _activate(self):
        p = _activate_fake()
        self.addCleanup(p.stop)

    def test_call_tool_route_and_envelope(self):
        """SDK 工具经 _call_tool → source=sdk 信封 + 数据。"""
        self._activate()
        r = server._call_tool("get_bars", {"security": ["000001"], "count": 2})
        self.assertIsNone(r.get("isError"))
        payload = json.loads(r["content"][0]["text"])
        self.assertEqual(payload["source"], "sdk")
        self.assertEqual(payload["source_contract_version"], "sdk-bridge-v1")
        self.assertEqual(payload["known_at"], "20260812")
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["data"][0]["close"], 11.1)

    def test_degraded_route_returns_dependency_error(self):
        """上游不可用（mock 重置）→ DEPENDENCY_UNAVAILABLE。"""
        sdk_bridge._full_mcp = None
        sdk_bridge._IMPORT_ERROR = "test-degraded"
        r = server._call_tool("get_bars", {"security": ["000001"]})
        self.assertTrue(r.get("isError"))
        payload = json.loads(r["content"][0]["text"])
        self.assertEqual(payload["code"], "DEPENDENCY_UNAVAILABLE")
        self.assertIn("hint", payload)

    def test_unknown_sdk_name_not_route(self):
        """非 SDK 工具名不进入 SDK 分支（未知工具语义不变）。"""
        self._activate()
        r = server._call_tool("definitely_not_a_tool", {})
        self.assertTrue(r.get("isError"))
        payload = json.loads(r["content"][0]["text"])
        self.assertEqual(payload["code"], "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()
