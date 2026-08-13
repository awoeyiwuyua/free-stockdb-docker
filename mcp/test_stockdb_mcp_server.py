import json
import unittest
from unittest import mock

from mcp import stockdb_mcp_server as server


class StockdbMcpServerTests(unittest.TestCase):
    @staticmethod
    def _daily_row(code: str, *, is_st=False) -> dict:
        return {
            "date": 20260105,
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "pre_close": 10.0,
            "amount": 1,
            "is_st": is_st,
            "code": code,
        }

    def test_long_ranges_use_year_prefixes(self):
        self.assertEqual(
            server._range_prefixes("20230101", "20260807"),
            ["2023", "2024", "2025", "2026"],
        )

    @mock.patch.object(server, "_http_get")
    def test_daily_range_ignores_null_items(self, http_get):
        http_get.return_value = [
            None,
            {"date": 20260102, "open": 10.0, "close": 10.5},
        ]

        rows = server.query_daily_kline(
            "000001", "20260101", "20260131", None
        )

        self.assertEqual(rows, [{"date": 20260102, "open": 10.0, "close": 10.5}])
        self.assertEqual(
            server._range_prefixes("20260601", "20260807"),
            ["202606", "202607", "202608"],
        )

    def test_tools_include_market_level_board_open_effect(self):
        names = {tool["name"] for tool in server.TOOLS}
        self.assertIn("get_board_open_effect_history", names)

    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "query_daily_kline")
    def test_limit_marks_scope_partial_and_formal_unusable(
        self, query_daily_kline, query_stock_list
    ):
        query_stock_list.return_value = {
            "total": 2,
            "codes": ["600001", "600002"],
        }
        query_daily_kline.side_effect = (
            lambda code, *_: [self._daily_row(code)]
        )

        _, metadata = server.query_fullmarket_daily_snapshot(
            "20260105", "20260105", limit=5, workers=1
        )

        self.assertTrue(metadata["scope_is_partial"])
        self.assertTrue(metadata["coverage_is_complete"])
        self.assertFalse(metadata["formal_usable"])
        self.assertIn("LIMIT_APPLIED", metadata["partial_reasons"])

    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "query_daily_kline")
    def test_explicit_partial_codes_are_never_formal(
        self, query_daily_kline, query_stock_list
    ):
        query_stock_list.return_value = {
            "total": 2,
            "codes": ["600001", "600002"],
        }
        query_daily_kline.side_effect = (
            lambda code, *_: [self._daily_row(code)]
        )

        _, metadata = server.query_fullmarket_daily_snapshot(
            "20260105", "20260105", codes=["600001"], workers=1
        )

        self.assertTrue(metadata["scope_is_partial"])
        self.assertFalse(metadata["formal_usable"])
        self.assertIn("EXPLICIT_CODES_PARTIAL", metadata["partial_reasons"])

    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "query_daily_kline")
    def test_one_request_failure_blocks_complete_coverage(
        self, query_daily_kline, query_stock_list
    ):
        query_stock_list.return_value = {
            "total": 2,
            "codes": ["600001", "600002"],
        }

        def fetch(code, *_):
            if code == "600002":
                raise OSError("timeout")
            return [self._daily_row(code)]

        query_daily_kline.side_effect = fetch
        _, metadata = server.query_fullmarket_daily_snapshot(
            "20260105", "20260105", workers=1
        )

        self.assertFalse(metadata["coverage_is_complete"])
        self.assertFalse(metadata["formal_usable"])
        self.assertIn("SOURCE_REQUEST_FAILED", metadata["partial_reasons"])

    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "query_daily_kline")
    def test_unclassified_empty_code_blocks_complete_coverage(
        self, query_daily_kline, query_stock_list
    ):
        query_stock_list.return_value = {
            "total": 2,
            "codes": ["600001", "600002"],
        }
        query_daily_kline.side_effect = lambda code, *_: (
            [] if code == "600002" else [self._daily_row(code)]
        )

        _, metadata = server.query_fullmarket_daily_snapshot(
            "20260105", "20260105", workers=1
        )

        self.assertFalse(metadata["coverage_is_complete"])
        self.assertFalse(metadata["formal_usable"])
        self.assertIn("EMPTY_CODE_UNCLASSIFIED", metadata["partial_reasons"])

    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "query_daily_kline")
    def test_raw_non_a_share_instruments_do_not_make_full_a_share_scope_partial(
        self, query_daily_kline, query_stock_list
    ):
        query_stock_list.return_value = {
            "total": 5,
            "codes": ["510300", "920001", "430001", "600001", "300001"],
        }
        query_daily_kline.side_effect = (
            lambda code, *_: [self._daily_row(code)]
        )

        _, metadata = server.query_fullmarket_daily_snapshot(
            "20260105", "20260105", workers=1
        )

        requested = {call.args[0] for call in query_daily_kline.call_args_list}
        self.assertEqual(requested, {"600001", "300001"})
        self.assertFalse(metadata["scope_is_partial"])
        self.assertTrue(metadata["coverage_is_complete"])
        self.assertTrue(metadata["formal_usable"])
        self.assertEqual(metadata["raw_instrument_count"], 5)
        self.assertEqual(metadata["universe_count"], 2)

    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "query_daily_kline")
    def test_unknown_point_in_time_state_blocks_formal_use(
        self, query_daily_kline, query_stock_list
    ):
        query_stock_list.return_value = {"total": 1, "codes": ["600001"]}
        row = self._daily_row("600001")
        row.pop("is_st")
        query_daily_kline.return_value = [row]

        _, metadata = server.query_fullmarket_daily_snapshot(
            "20260105", "20260105", workers=1
        )

        self.assertTrue(metadata["coverage_is_complete"])
        self.assertFalse(metadata["formal_usable"])
        self.assertIn("POINT_IN_TIME_STATE_UNKNOWN", metadata["partial_reasons"])

    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "query_daily_kline")
    def test_board_open_effect_reports_partial_scope_and_uses_raw_pre_close(
        self, query_daily_kline, query_stock_list
    ):
        query_stock_list.return_value = {"total": 2, "codes": ["600001", "600002"]}
        rows = {
            "600001": [
                {
                    "date": 20260102, "open": 10.2, "high": 11.0,
                    "low": 10.1, "close": 11.0, "pre_close": 10.0,
                    "amount": 1, "is_st": False,
                },
                {
                    "date": 20260105, "open": 11.55, "high": 11.6,
                    "low": 11.0, "close": 11.2, "pre_close": 11.0,
                    "amount": 1, "is_st": False,
                },
            ]
        }
        query_daily_kline.side_effect = lambda code, start, end, fields: rows.get(code, [])

        result = server.query_board_open_effect_history(
            "20260105", "20260105", codes=["600001"], workers=1,
            include_distribution=True,
        )

        self.assertTrue(result["is_partial"])
        self.assertEqual(result["requested_code_count"], 1)
        self.assertEqual(result["fetched_code_count"], 1)
        self.assertEqual(result["days"][0]["eligible_count"], 1)
        self.assertAlmostEqual(result["days"][0]["average_open_return_pct"], 5.0)
        self.assertEqual(result["days"][0]["distribution"]["open_return_pct"], [5.0])

    @mock.patch.object(server, "query_board_open_effect_history")
    def test_mcp_dispatch_returns_json(self, query_history):
        query_history.return_value = {"days": [], "is_partial": False}

        response = server._call_tool(
            "get_board_open_effect_history",
            {"start": "20260101", "end": "20260131"},
        )

        payload = json.loads(response["content"][0]["text"])
        self.assertFalse(payload["is_partial"])

    # === HTTP 传输冒烟测（直接走 dispatch 层，不起 socket） ===

    def test_http_dispatch_tools_list(self):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        })

        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        tool_names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(len(tool_names), 5)
        self.assertIn("get_stock_list", tool_names)
        self.assertIn("get_board_open_effect_history", tool_names)

    @mock.patch.object(server, "query_stock_list")
    def test_http_dispatch_tools_call(self, query_stock_list):
        query_stock_list.return_value = {"total": 0, "codes": []}

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_stock_list", "arguments": {}},
        })

        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 2)
        content = response["result"]["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "text")
        payload = json.loads(content[0]["text"])
        self.assertEqual(payload, {"total": 0, "codes": []})

    def test_dispatch_notification_returns_none(self):
        self.assertIsNone(server.dispatch({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }))
        self.assertIsNone(server.dispatch({
            "jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": 1},
        }))


if __name__ == "__main__":
    unittest.main()
