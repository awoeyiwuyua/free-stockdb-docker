"""test_tool_groups_trace — 0.9.3 工具分组（Gateway）+ Trace ID 单测。

覆盖：tools/list 按 group 过滤（全量向后兼容）、工具均带 group 元数据、
tools/call 响应与日志携带 trace_id、日检记录自动 trace_id。
"""
import json
import sys
import unittest
from unittest import mock

from interfaces.mcp import stockdb_mcp_server as server


def _dispatch(method, params=None, group=None):
    return server.dispatch({"jsonrpc": "2.0", "id": 1, "method": method,
                            "params": params or {}}, group=group)


class ToolGroupTest(unittest.TestCase):
    def test_all_tools_have_group(self):
        """全部已注册工具均带 group 元数据且组名合法（数量随环境：无 pybao 时 SDK
        工具不注册——降级行为，组检查只针对已注册工具）。"""
        for t in server.TOOLS:
            g = t.get("group")
            self.assertIn(g, server.TOOL_GROUPS, t["name"])
        self.assertGreaterEqual(len(server.TOOLS), 12)

    def test_list_all_backward_compatible(self):
        """不传 group → 全量（向后兼容）。"""
        res = _dispatch("tools/list")
        self.assertEqual(len(res["result"]["tools"]), len(server.TOOLS))

    def test_list_filtered_by_group(self):
        """group=market_data → 仅行情组；组内工具 group 一致（SDK 工具注册与否
        不影响过滤语义）。"""
        res = _dispatch("tools/list", group="market_data")
        tools = res["result"]["tools"]
        self.assertGreater(len(tools), 0)
        for t in tools:
            self.assertEqual(t["group"], "market_data")
        names = {t["name"] for t in tools}
        self.assertIn("get_kline", names)   # 现有工具必在行情组
        self.assertIn("get_trading_days", names)
        self.assertNotIn("get_data_status", names)  # 系统组不在行情组
        if "get_bars" in {t["name"] for t in server.TOOLS}:  # SDK 注册时（有 pybao）
            self.assertIn("get_bars", names)

    def test_list_unknown_group_empty(self):
        res = _dispatch("tools/list", group="no_such_group")
        self.assertEqual(res["result"]["tools"], [])

    def test_sdk_group_mapping_complete(self):
        """41 个 SDK 工具全部有分组映射（sdk_bridge.SDK_TOOL_GROUPS）。"""
        import sdk_bridge
        for name in sdk_bridge.KNOWN_SDK_TOOL_NAMES:
            self.assertIn(name, sdk_bridge.SDK_TOOL_GROUPS, name)


class TraceIdTest(unittest.TestCase):
    def test_tool_call_response_has_trace_id(self):
        """tools/call 成功响应携带 trace_id（JSON-RPC result 顶层附加键，信封不变）。"""
        ok_result = {"content": [{"type": "text", "text": json.dumps({
            "source": "http", "source_contract_version": "stock-list-v1", "known_at": None,
            "is_partial": False, "truncated": False, "total": 2, "errors": [],
            "known_limitations": [], "data": [1, 2]})}], "isError": False}
        with mock.patch.object(server, "_call_tool", return_value=ok_result):
            res = _dispatch("tools/call", {"name": "get_stock_list", "arguments": {}})
        result = res["result"]
        self.assertIn("trace_id", result)
        self.assertEqual(len(result["trace_id"]), 12)
        # 信封 8 键恒在（对外契约未被破坏）
        payload = json.loads(result["content"][0]["text"])
        for key in ("source", "source_contract_version", "known_at", "is_partial",
                    "truncated", "total", "errors", "known_limitations"):
            self.assertIn(key, payload)

    def test_error_response_has_trace_id(self):
        """工具错误响应同样携带 trace_id（可按键查日检诊断）。"""
        with mock.patch.object(server, "_call_tool",
                               return_value=server._error_result("boom",
                                                                 server.ERROR_INTERNAL_ERROR)):
            res = _dispatch("tools/call", {"name": "get_stock_list", "arguments": {}})
        self.assertIn("trace_id", res["result"])
        self.assertEqual(len(res["result"]["trace_id"]), 12)

    def test_tool_call_log_has_trace_id(self):
        """_log_tool_call 日志行携带 trace_id。"""
        trace_id = "abc123def456"
        with mock.patch("sys.stderr") as stderr:
            server._log_tool_call("get_stock_list", {"ok": True}, 5, trace_id=trace_id)
        written = stderr.write.call_args[0][0]
        line = json.loads(written)
        self.assertEqual(line["trace_id"], trace_id)

    def test_records_append_auto_trace_id(self):
        """日检记录自动附加 trace_id。"""
        import tempfile
        from pathlib import Path
        import config
        from storage import records
        tmp = tempfile.mkdtemp(prefix="test_trace_")
        with mock.patch.object(config, "DATA_DIR", Path(tmp)):
            records.append({"date": "20260816", "task": "close", "ok": True})
            recs = records.recent(1)
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(recs[0]["trace_id"]), 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
