import io
import json
import time
import unittest
from unittest import mock

from interfaces.mcp import stockdb_mcp_server as server
import pybao_tools  # noqa: E402  - 与 server 同目录模块（_MCP_DIR 已由 server 插入 sys.path）


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

    # === Phase 3 数据契约：envelope 8 键恒在（Task D） ===

    def _assert_envelope(self, payload, *, source, contract, known_at=None,
                         is_partial=False, truncated=False, total=None):
        """envelope 8 键恒在且类型正确；source/contract/known_at/is_partial/
        truncated 恒等断言；total 仅当显式传入时断言相等（否则只查类型）。"""
        self.assertIsInstance(payload["source"], str)
        self.assertEqual(payload["source"], source)
        self.assertIsInstance(payload["source_contract_version"], str)
        self.assertEqual(payload["source_contract_version"], contract)
        self.assertTrue(
            payload["known_at"] is None or isinstance(payload["known_at"], str),
        )
        self.assertEqual(payload["known_at"], known_at)
        self.assertIsInstance(payload["is_partial"], bool)
        self.assertEqual(payload["is_partial"], is_partial)
        self.assertIsInstance(payload["truncated"], bool)
        self.assertEqual(payload["truncated"], truncated)
        self.assertIsInstance(payload["total"], (int, dict, type(None)))
        if total is not None:
            self.assertEqual(payload["total"], total)
        self.assertIsInstance(payload["errors"], list)
        self.assertIsInstance(payload["known_limitations"], list)

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

    @mock.patch.object(server, "_classify_empty_codes")
    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "query_daily_kline")
    def test_unclassified_empty_code_blocks_complete_coverage(
        self, query_daily_kline, query_stock_list, classify
    ):
        query_stock_list.return_value = {
            "total": 2,
            "codes": ["600001", "600002"],
        }
        query_daily_kline.side_effect = lambda code, *_: (
            [] if code == "600002" else [self._daily_row(code)]
        )
        classify.return_value = {
            "suspended": [], "delisted_or_not_listed": [],
            "not_published": [], "unclassified": ["600002"],
        }

        _, metadata = server.query_fullmarket_daily_snapshot(
            "20260105", "20260105", workers=1
        )

        self.assertFalse(metadata["coverage_is_complete"])
        self.assertFalse(metadata["formal_usable"])
        self.assertFalse(metadata["candidate_coverage"]["complete"])
        self.assertIn("EMPTY_CODE_UNCLASSIFIED", metadata["partial_reasons"])
        classify.assert_called_once_with(["600002"], "20260105")

    @mock.patch.object(server, "_classify_empty_codes")
    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "query_daily_kline")
    def test_suspended_empty_codes_do_not_block_formal_usable(
        self, query_daily_kline, query_stock_list, classify
    ):
        """P2：空代码分类为停牌/退市 → 不否决正式可用性（只记 partial_reasons）。"""
        query_stock_list.return_value = {
            "total": 2,
            "codes": ["600001", "600002"],
        }
        query_daily_kline.side_effect = lambda code, *_: (
            [] if code == "600002" else [self._daily_row(code)]
        )
        classify.return_value = {
            "suspended": ["600002"], "delisted_or_not_listed": [],
            "not_published": [], "unclassified": [],
        }

        _, metadata = server.query_fullmarket_daily_snapshot(
            "20260105", "20260105", workers=1
        )

        self.assertFalse(metadata["coverage_is_complete"])  # 审计语义不变
        self.assertTrue(metadata["candidate_coverage"]["complete"])
        self.assertTrue(metadata["formal_usable"])           # 新语义：停牌不否决
        self.assertIn("EMPTY_SUSPENDED", metadata["partial_reasons"])
        self.assertNotIn("EMPTY_CODE_UNCLASSIFIED", metadata["partial_reasons"])
        self.assertEqual(
            metadata["empty_code_breakdown"]["suspended"], ["600002"],
        )

    @mock.patch.object(server, "_classify_empty_codes")
    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "query_daily_kline")
    def test_delisted_empty_codes_do_not_block_formal_usable(
        self, query_daily_kline, query_stock_list, classify
    ):
        query_stock_list.return_value = {
            "total": 2,
            "codes": ["600001", "600002"],
        }
        query_daily_kline.side_effect = lambda code, *_: (
            [] if code == "600002" else [self._daily_row(code)]
        )
        classify.return_value = {
            "suspended": [], "delisted_or_not_listed": ["600002"],
            "not_published": [], "unclassified": [],
        }

        _, metadata = server.query_fullmarket_daily_snapshot(
            "20260105", "20260105", workers=1
        )

        self.assertTrue(metadata["formal_usable"])
        self.assertIn("EMPTY_DELISTED_OR_NOT_LISTED", metadata["partial_reasons"])

    @mock.patch.object(server, "query_point_snapshot")
    def test_classify_empty_codes_maps_point_snapshot_categories(self, point_snapshot):
        """P2：分类映射——INVALID_SYMBOL→退市未上市 / NO_DATA→停牌 / 未发布→否决类。"""
        point_snapshot.return_value = {
            "points": [],
            "errors": [
                {"code": server.ERROR_INVALID_SYMBOL, "symbol": "000001",
                 "message": "代码不在股票池"},
                {"code": server.ERROR_NO_DATA, "symbol": "000002",
                 "message": "交易日无 bar"},
                {"code": server.ERROR_NOT_PUBLISHED, "symbol": "000003",
                 "message": "尚未发布"},
                {"code": server.ERROR_INTERNAL_ERROR, "symbol": "000004",
                 "message": "timeout"},
            ],
        }

        breakdown = server._classify_empty_codes(
            ["000001", "000002", "000003", "000004"], "20260105")

        self.assertEqual(breakdown["delisted_or_not_listed"], ["000001"])
        self.assertEqual(breakdown["suspended"], ["000002"])
        self.assertEqual(breakdown["not_published"], ["000003"])
        self.assertEqual(breakdown["unclassified"], ["000004"])
        point_snapshot.assert_called_once_with(
            {"date": "20260105", "codes": ["000001", "000002", "000003", "000004"],
             "limit": 0},
        )

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
        self.assertEqual(len(tool_names), 12)
        self.assertIn("get_stock_list", tool_names)
        self.assertIn("get_board_open_effect_history", tool_names)
        self.assertIn("get_indicators", tool_names)
        self.assertIn("get_board_members", tool_names)
        self.assertIn("screen_stocks", tool_names)
        self.assertIn("get_mydb_data", tool_names)
        self.assertIn("get_trading_days", tool_names)
        self.assertIn("get_data_status", tool_names)
        self.assertIn("get_point_snapshot", tool_names)

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
        # envelope 8 键逐键断言，业务键 total/codes 不变
        self._assert_envelope(
            payload, source="http", contract="stock-list-v1",
            known_at=None, total=0,
        )
        self.assertEqual(payload["codes"], [])

    def test_dispatch_notification_returns_none(self):
        self.assertIsNone(server.dispatch({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }))
        self.assertIsNone(server.dispatch({
            "jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": 1},
        }))

    # === get_indicators / get_board_members 工具 schema ===

    def test_tools_list_get_indicators_schema(self):
        tool = next(t for t in server.TOOLS if t["name"] == "get_indicators")
        schema = tool["inputSchema"]
        self.assertEqual(schema["required"], ["indicators", "codes"])
        frequency = schema["properties"]["frequency"]
        self.assertEqual(len(frequency["enum"]), 8)
        self.assertEqual(frequency["default"], "1d")
        self.assertEqual(schema["properties"]["limit"]["default"], 500)
        self.assertTrue(schema["properties"]["compact"]["default"])

    def test_tools_list_get_board_members_schema(self):
        tool = next(t for t in server.TOOLS if t["name"] == "get_board_members")
        self.assertEqual(tool["inputSchema"]["required"], ["query"])
        self.assertEqual(
            tool["inputSchema"]["properties"]["limit"]["default"], 500,
        )

    # === dispatch get_indicators 失败路径 ===

    @mock.patch.object(pybao_tools, "compute_indicators")
    def test_http_dispatch_get_indicators_error_is_error(self, compute):
        compute.return_value = {"ok": False, "error": "X"}

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "get_indicators",
                "arguments": {"indicators": ["macd"], "codes": ["600633"]},
            },
        })

        result = response["result"]
        self.assertTrue(result["content"][0])
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertIn("error", payload)
        self.assertEqual(payload["error"], "X")

    # === compute_indicators 参数校验（直接调 pybao_tools，全部离线） ===

    def test_compute_indicators_degraded_without_pybao(self):
        with mock.patch.object(pybao_tools, "get_pybao", return_value=None):
            outcome = pybao_tools.compute_indicators(
                {"indicators": ["macd"], "codes": ["600633"]},
            )

        self.assertFalse(outcome["ok"])
        self.assertIn("pybao 不可用", outcome["error"])

    def test_compute_indicators_validates_arguments(self):
        fake = mock.Mock()
        fake.jisuan.return_value = []
        with mock.patch.object(pybao_tools, "get_pybao", return_value=fake):
            # 未知指标名：错误信息含指标名
            outcome = pybao_tools.compute_indicators(
                {"indicators": ["not_a_real_indicator"], "codes": ["600633"]},
            )
            self.assertFalse(outcome["ok"])
            self.assertIn("not_a_real_indicator", outcome["error"])

            # codes 超 50 个
            outcome = pybao_tools.compute_indicators({
                "indicators": ["macd"],
                "codes": [f"{i:06d}" for i in range(51)],
            })
            self.assertFalse(outcome["ok"])
            self.assertIn("50", outcome["error"])

            # codes 为空
            outcome = pybao_tools.compute_indicators(
                {"indicators": ["macd"], "codes": []},
            )
            self.assertFalse(outcome["ok"])

            # params 与 indicators 数量不匹配
            outcome = pybao_tools.compute_indicators({
                "indicators": ["macd", "kdj"],
                "codes": ["600633"],
                "params": [{"fast": 12}],
            })
            self.assertFalse(outcome["ok"])

            # indicators 超 8 个
            outcome = pybao_tools.compute_indicators({
                "indicators": ["ma"] * 9,
                "codes": ["600633"],
            })
            self.assertFalse(outcome["ok"])
            self.assertIn("8", outcome["error"])

        # 校验先行：以上非法输入不应触发 jisuan
        fake.jisuan.assert_not_called()

    # === compact 列式 / 行列表 ===

    def test_compute_indicators_compact_columnar(self):
        fake = mock.Mock()
        fake.jisuan.return_value = [
            {"date": 20260102, "macd": 0.5},
            {"date": 20260103, "macd": 0.7},
        ]
        with mock.patch.object(pybao_tools, "get_pybao", return_value=fake):
            outcome = pybao_tools.compute_indicators({
                "indicators": ["macd"], "codes": ["600633"], "compact": True,
            })

        self.assertTrue(outcome["ok"])
        data = outcome["result"]["data"]
        self.assertIsInstance(data, dict)
        self.assertEqual(data["dates"], [20260102, 20260103])
        self.assertEqual(data["macd"], [0.5, 0.7])
        self.assertFalse(outcome["result"]["truncated"])

    def test_compute_indicators_row_mode(self):
        fake = mock.Mock()
        fake.jisuan.return_value = [{"date": 20260102, "macd": 0.5}]
        with mock.patch.object(pybao_tools, "get_pybao", return_value=fake):
            outcome = pybao_tools.compute_indicators({
                "indicators": ["macd"], "codes": ["600633"], "compact": False,
            })

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["result"]["data"], [
            {"date": 20260102, "macd": 0.5},
        ])

    def test_compute_indicators_limit_truncates(self):
        fake = mock.Mock()
        fake.jisuan.return_value = [
            {"date": 20260101 + i, "macd": float(i)} for i in range(600)
        ]
        with mock.patch.object(pybao_tools, "get_pybao", return_value=fake):
            outcome = pybao_tools.compute_indicators({
                "indicators": ["macd"], "codes": ["600633"],
                "limit": 500, "compact": False,
            })

        result = outcome["result"]
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncated_rows"], 100)
        self.assertEqual(len(result["data"]), 500)
        # 保留最新：第一行应为第 101 行（日期 20260201）
        self.assertEqual(result["data"][0]["date"], 20260201)

    # === query_boards ===

    def test_query_boards_invalid_category(self):
        outcome = pybao_tools.query_boards({
            "query": "半导体", "category": "申万四级",
        })

        self.assertFalse(outcome["ok"])
        self.assertIn("申万四级", outcome["error"])

    def test_query_boards_category_int_truncates_symbols(self):
        fake = mock.Mock()
        fake.bk.get.return_value = {
            "name": "半导体",
            "symbols": [f"600{i:03d}" for i in range(600)],
        }
        with mock.patch.object(pybao_tools, "get_pybao", return_value=fake):
            outcome = pybao_tools.query_boards({
                "query": "半导体", "category": 3, "include_symbols": True,
            })

        self.assertTrue(outcome["ok"])
        self.assertEqual(fake.bk.get.call_count, 1)
        self.assertEqual(fake.bk.get.call_args.kwargs["category"], "申万三级")
        result = outcome["result"]
        # Task B/C：query_boards 结果以 board_count/symbol_count 计数（原 total 语义）
        self.assertEqual(result["board_count"], 1)
        self.assertEqual(result["symbol_count"], 600)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["symbols"]), 500)

    # === get_kline dispatch（增强路径与参数校验） ===

    @mock.patch.object(pybao_tools, "get_sdk_client")
    def test_http_dispatch_get_kline_fq_requires_pybao(self, get_sdk_client):
        get_sdk_client.return_value = None

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "get_kline",
                "arguments": {"code": "600633", "fq": "qfq"},
            },
        })

        result = response["result"]
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertIn("pybao", payload["error"])

    def test_http_dispatch_get_kline_codes_too_many(self):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {
                "name": "get_kline",
                "arguments": {"codes": [f"{i:06d}" for i in range(51)]},
            },
        })

        self.assertTrue(response["result"]["isError"])

    def test_http_dispatch_get_kline_requires_code(self):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "get_kline", "arguments": {}},
        })

        self.assertTrue(response["result"]["isError"])

    @mock.patch.object(server, "_http_get")
    def test_http_dispatch_get_kline_http_limit(self, http_get):
        http_get.return_value = [
            {"date": 20260102, "open": 10.0, "close": 10.5},
            {"date": 20260103, "open": 10.5, "close": 11.0},
            {"date": 20260105, "open": 11.0, "close": 10.8},
        ]

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {
                "name": "get_kline",
                "arguments": {
                    "code": "600633", "frequency": "1d", "limit": 2,
                    "start": "20260101", "end": "20260131",
                },
            },
        })

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(len(payload["data"]), 2)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["source"], "http")

    # === dispatch get_board_members 成功路径 ===

    @mock.patch.object(pybao_tools, "query_boards")
    def test_http_dispatch_get_board_members_success(self, query_boards):
        expected = {
            "source": "pybao",
            "category": None,
            "query": "半导体",
            "boards": [{"name": "半导体", "count": 10}],
            "board_count": 1,
            "symbol_count": 10,
            "truncated": False,
        }
        query_boards.return_value = {"ok": True, "result": expected}

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {
                "name": "get_board_members",
                "arguments": {"query": "半导体"},
            },
        })

        result = response["result"]
        self.assertIsNone(result.get("isError"))
        payload = json.loads(result["content"][0]["text"])
        # envelope 8 键 + 业务键不变（boards/board_count/symbol_count）
        self._assert_envelope(
            payload, source="pybao", contract="boards-v1", known_at=None,
        )
        self.assertIsNone(payload["total"])
        self.assertEqual(payload["boards"], expected["boards"])
        self.assertEqual(payload["board_count"], 1)
        self.assertEqual(payload["symbol_count"], 10)
        self.assertFalse(payload["truncated"])

    # === QC 锁定：compute_indicators 增强参数校验 ===

    def test_compute_indicators_rejects_bad_cross_fq(self):
        fake = mock.Mock()
        fake.jisuan.return_value = []
        with mock.patch.object(pybao_tools, "get_pybao", return_value=fake):
            outcome = pybao_tools.compute_indicators({
                "indicators": ["macd"], "codes": ["600633"], "cross": "bad",
            })
            self.assertFalse(outcome["ok"])
            self.assertIn("cross", outcome["error"])

            outcome = pybao_tools.compute_indicators({
                "indicators": ["macd"], "codes": ["600633"], "fq": "bad",
            })
            self.assertFalse(outcome["ok"])
            self.assertIn("fq", outcome["error"])

            # zhishu 不能与其他指标混用、不支持 cross
            outcome = pybao_tools.compute_indicators({
                "indicators": ["zhishu", "macd"], "codes": ["600633"],
            })
            self.assertFalse(outcome["ok"])
            self.assertIn("zhishu", outcome["error"])
            outcome = pybao_tools.compute_indicators({
                "indicators": ["zhishu"], "codes": ["600633"], "cross": True,
            })
            self.assertFalse(outcome["ok"])

        fake.jisuan.assert_not_called()

    def test_compute_indicators_batch_shape(self):
        fake = mock.Mock()
        fake.jisuan.return_value = {
            "600633": [{"date": 20260102, "macd": 0.5}],
            "000001": [{"date": 20260102, "macd": 0.3}],
        }
        with mock.patch.object(pybao_tools, "get_pybao", return_value=fake):
            outcome = pybao_tools.compute_indicators({
                "indicators": ["macd"], "codes": ["600633", "000001"],
                "compact": False,
            })

        self.assertTrue(outcome["ok"])
        self.assertEqual(
            set(outcome["result"]["data"].keys()), {"600633", "000001"},
        )
        self.assertEqual(outcome["result"]["indicators"], ["macd"])

    # === QC 锁定：get_kline SDK 路径（批量 + 最新行截断） ===

    @mock.patch.object(pybao_tools, "get_sdk_client")
    def test_http_dispatch_get_kline_sdk_batch(self, get_sdk_client):
        fake_client = mock.Mock()
        fake_client.get_data.return_value = {
            "600633": [
                {"date": 20260101 + i, "close": float(i)} for i in range(10)
            ],
            "000001": [
                {"date": 20260101 + i, "close": float(i)} for i in range(10)
            ],
        }
        get_sdk_client.return_value = fake_client

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {
                "name": "get_kline",
                "arguments": {
                    "codes": ["600633", "000001"], "fq": "qfq", "limit": 3,
                },
            },
        })

        self.assertIsNone(response["result"].get("isError"))
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["source"], "pybao")
        self.assertEqual(payload["source_contract_version"], "kline-v2")
        self.assertEqual(payload["mode"], "point")
        self.assertEqual(payload["fq"], "qfq")
        for key in ("price_unit", "volume_unit", "amount_unit"):
            self.assertIsInstance(payload[key], str)
        self.assertEqual(set(payload["data"].keys()), {"600633", "000001"})
        # 批量 total 为 {code: n} 形态（截断前数量）
        self.assertEqual(payload["total"], {"600633": 10, "000001": 10})
        self.assertTrue(payload["truncated"])
        # 保留最新 3 行：日期应为最后 3 个交易日
        self.assertEqual(
            [row["date"] for row in payload["data"]["600633"]],
            [20260108, 20260109, 20260110],
        )
        # SDK 以列表批量调用（一次 get_data），而非逐只
        self.assertEqual(fake_client.get_data.call_count, 1)
        self.assertEqual(fake_client.get_data.call_args.args[0], ["600633", "000001"])


    # === Phase 2：screen_stocks / get_mydb_data 工具 schema（Task D） ===

    def test_tools_list_screen_stocks_schema(self):
        tool = next(t for t in server.TOOLS if t["name"] == "screen_stocks")
        schema = tool["inputSchema"]
        self.assertEqual(schema["required"], [])
        properties = schema["properties"]
        for key in (
            "board", "indicator_cross", "float_mv_min", "float_mv_max",
            "exclude_st", "date", "limit", "codes",
        ):
            self.assertIn(key, properties)
        self.assertEqual(properties["limit"]["default"], 50)
        self.assertEqual(
            properties["indicator_cross"]["properties"]["golden"]["default"], True,
        )
        self.assertEqual(
            properties["indicator_cross"]["properties"]["within_days"]["default"], 5,
        )
        self.assertTrue(properties["exclude_st"]["default"])
        self.assertEqual(
            properties["codes"]["description"],
            "调试用：限定股票池（1-200；传入即 is_partial=true）",
        )

    def test_tools_list_get_mydb_data_schema(self):
        tool = next(t for t in server.TOOLS if t["name"] == "get_mydb_data")
        schema = tool["inputSchema"]
        self.assertEqual(schema["required"], ["table"])
        self.assertEqual(
            set(schema["properties"].keys()), {"table", "key", "limit"},
        )
        self.assertEqual(schema["properties"]["limit"]["default"], 100)

    # === query_mydb（pybao_tools 直接调用，全离线） ===

    def test_query_mydb_degraded_without_pybao(self):
        with mock.patch.object(pybao_tools, "get_pybao", return_value=None):
            outcome = pybao_tools.query_mydb({"table": "hk日k"})

        self.assertFalse(outcome["ok"])
        self.assertIn("pybao 不可用", outcome["error"])

    def test_query_mydb_validates_table_names(self):
        # 空表名
        outcome = pybao_tools.query_mydb({"table": ""})
        self.assertFalse(outcome["ok"])
        self.assertIn("表名不能为空", outcome["error"])

        # 非法字符（含空格）
        outcome = pybao_tools.query_mydb({"table": "abc 表"})
        self.assertFalse(outcome["ok"])
        self.assertIn("只能含字母数字", outcome["error"])

        # 保留表名与保留表前缀，均含"保留"字样
        outcome = pybao_tools.query_mydb({"table": "日k"})
        self.assertFalse(outcome["ok"])
        self.assertIn("保留", outcome["error"])
        outcome = pybao_tools.query_mydb({"table": "日k:x"})
        self.assertFalse(outcome["ok"])
        self.assertIn("保留", outcome["error"])

    @mock.patch.object(pybao_tools, "get_mydb_rd")
    def test_query_mydb_key_gets_value(self, get_mydb_rd):
        fake_rd = mock.Mock()
        fake_rd.get.return_value = {"date": 20260813, "close": 12.5}
        get_mydb_rd.return_value = fake_rd

        outcome = pybao_tools.query_mydb({
            "table": "hk日k", "key": "00700:20260813",
        })

        self.assertTrue(outcome["ok"])
        self.assertEqual(
            outcome["result"]["value"], {"date": 20260813, "close": 12.5},
        )
        fake_rd.get.assert_called_once_with("hk日k", "00700:20260813")

    @staticmethod
    def _fake_rd_600_keys() -> mock.Mock:
        fake_rd = mock.Mock()
        fake_rd.keys.return_value = [f"600633:{20260101 + i}" for i in range(600)]
        fake_rd.get.return_value = {"date": 20260101, "close": 1.0}
        return fake_rd

    @mock.patch.object(pybao_tools, "get_mydb_rd")
    def test_query_mydb_list_truncates_at_limit(self, get_mydb_rd):
        fake_rd = self._fake_rd_600_keys()
        get_mydb_rd.return_value = fake_rd

        outcome = pybao_tools.query_mydb({"table": "my_table", "limit": 500})

        self.assertTrue(outcome["ok"])
        result = outcome["result"]
        self.assertEqual(result["total"], 600)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["keys"]), 500)
        self.assertEqual(len(result["values"]), 500)
        self.assertEqual(fake_rd.get.call_count, 500)

    @mock.patch.object(pybao_tools, "get_mydb_rd")
    def test_query_mydb_list_default_limit_100(self, get_mydb_rd):
        fake_rd = self._fake_rd_600_keys()
        get_mydb_rd.return_value = fake_rd

        outcome = pybao_tools.query_mydb({"table": "my_table"})

        self.assertTrue(outcome["ok"])
        result = outcome["result"]
        self.assertEqual(result["total"], 600)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["keys"]), 100)

    class _FakeQueryResult:
        """带 .keys/.all 的假 QueryResult：dict() 转换应得到普通 dict。"""

        def keys(self):
            return ["date", "close"]

        def all(self):
            return None

        def __getitem__(self, key):
            return {"date": 20260813, "close": 12.5}[key]

    @mock.patch.object(pybao_tools, "get_mydb_rd")
    def test_query_mydb_converts_query_result(self, get_mydb_rd):
        fake_rd = mock.Mock()
        fake_rd.get.return_value = self._FakeQueryResult()
        get_mydb_rd.return_value = fake_rd

        outcome = pybao_tools.query_mydb({
            "table": "hk日k", "key": "00700:20260813",
        })

        self.assertTrue(outcome["ok"])
        self.assertEqual(
            outcome["result"]["value"], {"date": 20260813, "close": 12.5},
        )

    # === screen_stocks 参数校验（patch get_pybao → fake module） ===

    def test_screen_stocks_validation(self):
        fake = mock.Mock()
        with mock.patch.object(pybao_tools, "get_pybao", return_value=fake):
            # universe 为空（调用方未提供任何筛选条件）→ 报错
            outcome = pybao_tools.screen_stocks({}, [])
            self.assertFalse(outcome["ok"])
            self.assertIn("universe", outcome["error"])

            # universe 项非 6 位数字
            outcome = pybao_tools.screen_stocks({}, ["60000"])
            self.assertFalse(outcome["ok"])
            self.assertIn("6 位", outcome["error"])

            # indicator_cross.name == "zhishu"：不支持 cross 筛选
            outcome = pybao_tools.screen_stocks(
                {"indicator_cross": {"name": "zhishu"}}, ["600001"],
            )
            self.assertFalse(outcome["ok"])
            self.assertIn("zhishu", outcome["error"])

            # 未知指标名
            outcome = pybao_tools.screen_stocks(
                {"indicator_cross": {"name": "nope"}}, ["600001"],
            )
            self.assertFalse(outcome["ok"])
            self.assertIn("未知指标", outcome["error"])

            # within_days 超出 1-60
            outcome = pybao_tools.screen_stocks(
                {"indicator_cross": {"name": "macd", "within_days": 61}},
                ["600001"],
            )
            self.assertFalse(outcome["ok"])
            self.assertIn("1-60", outcome["error"])

            # float_mv_min > float_mv_max
            outcome = pybao_tools.screen_stocks(
                {"float_mv_min": 9e8, "float_mv_max": 1e8}, ["600001"],
            )
            self.assertFalse(outcome["ok"])
            self.assertIn("float_mv_min", outcome["error"])

        # 校验先行：以上非法输入不应触发 jisuan
        fake.jisuan.assert_not_called()

    # === screen_stocks 交叉筛选（fake.jisuan 造金叉/死叉/无信号） ===

    @mock.patch.object(pybao_tools, "get_sdk_client")
    @mock.patch.object(pybao_tools, "get_pybao")
    def test_screen_stocks_cross_golden(self, get_pybao, get_sdk_client):
        fake = mock.Mock()
        fake.jisuan.return_value = {
            "600001": [
                {"date": 20260101, "cross": 0},
                {"date": 20260105, "cross": 1},   # 最近金叉
            ],
            "600002": [{"date": 20260105, "cross": -1}],   # 死叉
            "600003": [{"date": 20260105, "cross": 0}],    # 无信号
        }
        get_pybao.return_value = fake
        fake_client = mock.Mock()
        fake_client.get_data.return_value = [
            {"date": 20260105, "float_mv": 5e8, "is_st": False,
             "name": "测试", "close": 10.2},
        ]
        get_sdk_client.return_value = fake_client

        outcome = pybao_tools.screen_stocks(
            {
                "indicator_cross": {"name": "macd", "golden": True, "within_days": 5},
                "date": "20260105",
            },
            ["600001", "600002", "600003"],
        )

        self.assertTrue(outcome["ok"])
        result = outcome["result"]
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual([c["code"] for c in result["candidates"]], ["600001"])
        self.assertEqual(result["candidates"][0]["cross_date"], 20260105)
        self.assertEqual(result["candidates"][0]["signal"], 1)
        # jisuan 以 cross=True 批量调用
        fake.jisuan.assert_called_once()
        call = fake.jisuan.call_args
        self.assertEqual(call.args[0], "macd")
        self.assertEqual(call.args[1], ["600001", "600002", "600003"])
        self.assertTrue(call.kwargs["cross"])

    @mock.patch.object(pybao_tools, "get_sdk_client")
    @mock.patch.object(pybao_tools, "get_pybao")
    def test_screen_stocks_cross_dead(self, get_pybao, get_sdk_client):
        fake = mock.Mock()
        fake.jisuan.return_value = {
            "600001": [{"date": 20260105, "cross": 1}],
            "600002": [{"date": 20260105, "cross": -1}],
            "600003": [{"date": 20260105, "cross": 0}],
        }
        get_pybao.return_value = fake
        fake_client = mock.Mock()
        fake_client.get_data.return_value = [
            {"date": 20260105, "float_mv": 5e8, "is_st": False,
             "name": "测试", "close": 10.2},
        ]
        get_sdk_client.return_value = fake_client

        # golden=False → 死叉码；未传 date → effective_date 由交叉行日期推导
        outcome = pybao_tools.screen_stocks(
            {"indicator_cross": {"name": "macd", "golden": False, "within_days": 5}},
            ["600001", "600002", "600003"],
        )

        self.assertTrue(outcome["ok"])
        result = outcome["result"]
        # effective_date 统一归一化为字符串（SDK get_data 要求 str）
        self.assertEqual(result["date"], "20260105")
        self.assertEqual([c["code"] for c in result["candidates"]], ["600002"])
        self.assertEqual(result["candidates"][0]["signal"], -1)

    @mock.patch.object(pybao_tools, "get_sdk_client")
    @mock.patch.object(pybao_tools, "get_pybao")
    def test_screen_stocks_cross_legacy_key(self, get_pybao, get_sdk_client):
        """旧版 jisuan 信号键为 "<name>_cross"（无 "cross" 键）也应识别。"""
        fake = mock.Mock()
        fake.jisuan.return_value = {
            "600001": [{"date": 20260105, "macd_cross": 1}],
            "600002": [{"date": 20260105, "macd_cross": 0}],
        }
        get_pybao.return_value = fake
        fake_client = mock.Mock()
        fake_client.get_data.return_value = [
            {"date": 20260105, "float_mv": 5e8, "is_st": False,
             "name": "测试", "close": 10.2},
        ]
        get_sdk_client.return_value = fake_client

        outcome = pybao_tools.screen_stocks(
            {"indicator_cross": {"name": "macd", "golden": True}},
            ["600001", "600002"],
        )

        self.assertTrue(outcome["ok"])
        self.assertEqual(
            [c["code"] for c in outcome["result"]["candidates"]], ["600001"],
        )

    # === screen_stocks mv/ST 过滤 ===

    @mock.patch.object(pybao_tools, "get_sdk_client")
    @mock.patch.object(pybao_tools, "get_pybao")
    def test_screen_stocks_mv_st_filters(self, get_pybao, get_sdk_client):
        fake = mock.Mock()
        get_pybao.return_value = fake
        bars = {
            "600001": [{"date": 20260105, "float_mv": 2e8, "is_st": False,
                        "name": "边界下限", "close": 10.0}],
            "600002": [{"date": 20260105, "float_mv": 1.5e8, "is_st": False,
                        "name": "低于下限", "close": 10.0}],
            "600003": [{"date": 20260105, "float_mv": 8.5e8, "is_st": False,
                        "name": "高于上限", "close": 10.0}],
            "600004": [{"date": 20260105, "float_mv": 5e8, "is_st": True,
                        "name": "ST股", "close": 10.0}],
            "600005": [],   # bar 缺失
        }
        fake_client = mock.Mock()
        fake_client.get_data.side_effect = lambda code, **kw: bars.get(code, [])
        get_sdk_client.return_value = fake_client

        outcome = pybao_tools.screen_stocks(
            {"float_mv_min": 2e8, "float_mv_max": 8e8},
            ["600001", "600002", "600003", "600004", "600005"],
        )

        self.assertTrue(outcome["ok"])
        result = outcome["result"]
        # 边界值（==min / ==max）保留；越界 / ST / 缺 bar 剔除
        self.assertEqual([c["code"] for c in result["candidates"]], ["600001"])
        self.assertEqual(result["candidates"][0]["float_mv"], 2e8)
        self.assertEqual(
            result["dropped"], {"missing_bar": 1, "st": 1, "mv": 2},
        )
        fake.jisuan.assert_not_called()

    @mock.patch.object(pybao_tools, "get_sdk_client")
    @mock.patch.object(pybao_tools, "get_pybao")
    def test_screen_stocks_exclude_st_false_keeps_st(self, get_pybao, get_sdk_client):
        fake = mock.Mock()
        get_pybao.return_value = fake
        fake_client = mock.Mock()
        fake_client.get_data.return_value = [
            {"date": 20260105, "float_mv": 5e8, "is_st": True,
             "name": "ST股", "close": 10.0},
        ]
        get_sdk_client.return_value = fake_client

        outcome = pybao_tools.screen_stocks(
            # float_mv_min=0 为恒真条件（契约要求至少一个筛选条件），
            # 用于验证 exclude_st=False 时 ST 股被保留且不计数 st 剔除
            {"exclude_st": False, "float_mv_min": 0}, ["600004"],
        )

        self.assertTrue(outcome["ok"])
        candidates = outcome["result"]["candidates"]
        self.assertEqual([c["code"] for c in candidates], ["600004"])
        self.assertTrue(candidates[0]["is_st"])
        self.assertEqual(outcome["result"]["dropped"]["st"], 0)

    # === dispatch：screen_stocks / get_mydb_data 成功与失败路径 ===

    @mock.patch.object(pybao_tools, "screen_stocks")
    @mock.patch.object(server, "query_stock_list")
    def test_http_dispatch_screen_stocks_success(self, query_stock_list, screen_stocks):
        query_stock_list.return_value = {"total": 2, "codes": ["600001", "600002"]}
        screen_stocks.return_value = {
            "ok": True,
            "result": {
                "source": "pybao",
                "date": 20260105,
                "universe_count": 2,
                "matched_count": 1,
                "candidates": [{"code": "600001", "cross_date": 20260105, "signal": 1}],
                "dropped": {"missing_bar": 0, "st": 0, "mv": 0},
                "truncated": False,
                "limit": 50,
            },
        }

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {
                "name": "screen_stocks",
                "arguments": {"indicator_cross": {"name": "macd"}},
            },
        })

        result = response["result"]
        self.assertIsNone(result.get("isError"))
        payload = json.loads(result["content"][0]["text"])
        self.assertIn("candidates", payload)
        self.assertEqual(payload["universe"]["source"], "full_market")
        self.assertEqual(payload["universe"]["count"], 2)
        self.assertFalse(payload["is_partial"])
        # A 股过滤后的 universe 已传给 screen_stocks
        universe = screen_stocks.call_args.args[1]
        self.assertEqual(universe, ["600001", "600002"])

    def test_http_dispatch_screen_stocks_codes_invalid(self):
        # codes 项非 6 位：在 server.query_screen 校验，dispatch 层即报错
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 11, "method": "tools/call",
            "params": {
                "name": "screen_stocks",
                "arguments": {"codes": ["12345"]},
            },
        })

        result = response["result"]
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertIn("6 位", payload["error"])

        # codes 空数组：数量必须在 1-200
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 12, "method": "tools/call",
            "params": {
                "name": "screen_stocks",
                "arguments": {"codes": []},
            },
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertIn("1-200", payload["error"])

    @mock.patch.object(pybao_tools, "screen_stocks")
    @mock.patch.object(pybao_tools, "query_boards")
    def test_http_dispatch_screen_stocks_board_universe(
        self, query_boards, screen_stocks
    ):
        query_boards.return_value = {
            "ok": True,
            "result": {"name": "算力", "total": 2, "symbols": ["600001", "600002"]},
        }
        screen_stocks.return_value = {
            "ok": True,
            "result": {"candidates": [], "matched_count": 0, "dropped": {}},
        }

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 13, "method": "tools/call",
            "params": {
                "name": "screen_stocks",
                "arguments": {
                    "board": {"name": "算力", "category": 0},
                    "indicator_cross": {"name": "macd"},
                },
            },
        })

        result = response["result"]
        self.assertIsNone(result.get("isError"))
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["universe"]["source"], "board:算力")
        self.assertEqual(payload["universe"]["count"], 2)
        self.assertFalse(payload["is_partial"])
        # 板块成分股作为 universe 传入 screen_stocks
        universe = screen_stocks.call_args.args[1]
        self.assertEqual(universe, ["600001", "600002"])
        # query_boards 以 include_symbols=True 拉成分股
        self.assertTrue(query_boards.call_args.args[0]["include_symbols"])

    @mock.patch.object(pybao_tools, "screen_stocks")
    def test_http_dispatch_screen_stocks_codes_partial(self, screen_stocks):
        screen_stocks.return_value = {
            "ok": True,
            "result": {"candidates": [{"code": "600001"}], "matched_count": 1},
        }

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 14, "method": "tools/call",
            "params": {
                "name": "screen_stocks",
                "arguments": {"codes": ["600001", "600002"]},
            },
        })

        result = response["result"]
        self.assertIsNone(result.get("isError"))
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload["is_partial"])
        self.assertEqual(payload["partial_reasons"], ["EXPLICIT_CODES_DEBUG"])
        self.assertEqual(payload["universe"]["source"], "codes")
        self.assertEqual(payload["universe"]["count"], 2)
        universe = screen_stocks.call_args.args[1]
        self.assertEqual(universe, ["600001", "600002"])

    @mock.patch.object(pybao_tools, "query_mydb")
    def test_http_dispatch_get_mydb_data_success(self, query_mydb):
        expected = {
            "source": "pybao",
            "table": "hk日k",
            "keys": ["00700:20260813"],
            "values": {"00700:20260813": {"close": 12.5}},
            "total": 1,
            "truncated": False,
        }
        query_mydb.return_value = {"ok": True, "result": expected}

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 15, "method": "tools/call",
            "params": {
                "name": "get_mydb_data",
                "arguments": {"table": "hk日k"},
            },
        })

        result = response["result"]
        self.assertIsNone(result.get("isError"))
        payload = json.loads(result["content"][0]["text"])
        # envelope 8 键 + 业务键不变（table/keys/values）
        self._assert_envelope(
            payload, source="pybao", contract="mydb-v1",
            known_at=None, total=1,
        )
        self.assertEqual(payload["table"], "hk日k")
        self.assertEqual(payload["keys"], ["00700:20260813"])
        self.assertEqual(
            payload["values"], {"00700:20260813": {"close": 12.5}},
        )
        self.assertFalse(payload["truncated"])

    @mock.patch.object(pybao_tools, "query_mydb")
    def test_http_dispatch_get_mydb_data_error(self, query_mydb):
        query_mydb.return_value = {"ok": False, "error": "X"}

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 16, "method": "tools/call",
            "params": {
                "name": "get_mydb_data",
                "arguments": {"table": "hk日k"},
            },
        })

        result = response["result"]
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["error"], "X")


# === Phase 2.5：统一错误码 / 交易日历 / get_trading_days / get_data_status / TTL ===

    def test_error_code_pybao_unavailable_with_hint(self):
        with mock.patch.object(pybao_tools, "get_pybao", return_value=None):
            outcome = pybao_tools.compute_indicators(
                {"indicators": ["macd"], "codes": ["600633"]},
            )

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["code"], "DEPENDENCY_UNAVAILABLE")
        self.assertIn("/tmp/pybao_mac", outcome["hint"])

    def test_error_code_unknown_indicator_param_invalid(self):
        outcome = pybao_tools.compute_indicators(
            {"indicators": ["not_an_indicator"], "codes": ["600633"]},
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["code"], "INVALID_ARGUMENT")

    def test_error_code_unknown_tool(self):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 130, "method": "tools/call",
            "params": {"name": "definitely_not_a_tool", "arguments": {}},
        })

        result = response["result"]
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        # 未知工具按契约同样归入 INVALID_ARGUMENT（参数非法）
        self.assertEqual(payload["code"], "INVALID_ARGUMENT")

    # === calendar_xshg（2026 休市表） ===

    def test_calendar_is_trading_day(self):
        self.assertFalse(server.calendar_xshg.is_trading_day("20260101"))  # 元旦休市
        self.assertFalse(server.calendar_xshg.is_trading_day("20260815"))  # 周六
        self.assertTrue(server.calendar_xshg.is_trading_day("20260813"))   # 周四交易日

    def test_calendar_trading_days_between(self):
        self.assertEqual(
            server.calendar_xshg.trading_days_between("20260810", "20260814"),
            ["20260810", "20260811", "20260812", "20260813", "20260814"],
        )

    def test_calendar_nearest_trading_day(self):
        self.assertEqual(
            server.calendar_xshg.nearest_trading_day("20260815"), "20260814",
        )

    # === get_trading_days dispatch ===

    def test_http_dispatch_get_trading_days_range(self):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 140, "method": "tools/call",
            "params": {
                "name": "get_trading_days",
                "arguments": {"start": "20260810", "end": "20260814"},
            },
        })

        self.assertIsNone(response["result"].get("isError"))
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(
            payload["trading_days"],
            ["20260810", "20260811", "20260812", "20260813", "20260814"],
        )
        self.assertEqual(payload["count"], 5)
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["calendar_through"], "2026-12-31")

    def test_http_dispatch_get_trading_days_limit_truncates(self):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 141, "method": "tools/call",
            "params": {
                "name": "get_trading_days",
                "arguments": {"start": "20260810", "end": "20260814", "limit": 2},
            },
        })

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["trading_days"], ["20260810", "20260811"])
        self.assertEqual(payload["count"], 2)

    def test_http_dispatch_get_trading_days_invalid_start(self):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 142, "method": "tools/call",
            "params": {
                "name": "get_trading_days",
                "arguments": {"start": "2026-08-10"},
            },
        })

        result = response["result"]
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["code"], "INVALID_ARGUMENT")

    # === get_data_status（pair 形态 / pybao 标志 / TTL 去重） ===

    def test_get_data_status_pair_form_and_pybao_flag(self):
        with mock.patch.object(server, "_TTL", server._TTLCache()):
            with mock.patch.object(server, "_http_get") as http_get:
                http_get.return_value = [["日k:000001:20260813", {"date": 20260813}]]
                with mock.patch.object(pybao_tools, "get_pybao", return_value=None):
                    response = server.dispatch({
                        "jsonrpc": "2.0", "id": 150, "method": "tools/call",
                        "params": {"name": "get_data_status", "arguments": {}},
                    })

        self.assertIsNone(response["result"].get("isError"))
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["latest_trade_date"], "20260813")
        self.assertFalse(payload["pybao_available"])
        self.assertEqual(payload["tool_count"], 12)

    def test_get_data_status_ttl_dedup_single_http_round(self):
        with mock.patch.object(server, "_TTL", server._TTLCache()):
            with mock.patch.object(server, "_http_get") as http_get:
                http_get.return_value = [["日k:000001:20260813", {"date": 20260813}]]
                with mock.patch.object(pybao_tools, "get_pybao", return_value=None):
                    first = server.get_data_status()
                    second = server.get_data_status()

        self.assertEqual(first["latest_trade_date"], "20260813")
        self.assertEqual(second["latest_trade_date"], "20260813")
        # 探针覆盖 3 个 YYYYMM 前缀，但 TTL 缓存使第二次调用不再发起任何 HTTP
        self.assertEqual(http_get.call_count, 3)

    def test_get_data_status_without_ttl_reprobes(self):
        cache = server._TTLCache()
        with mock.patch.object(server, "_TTL", cache):
            with mock.patch.object(cache, "set"):
                with mock.patch.object(server, "_http_get") as http_get:
                    http_get.return_value = [["日k:000001:20260813", {"date": 20260813}]]
                    server.get_data_status()
                    server.get_data_status()

        # 缓存写入失效 → 每次调用都重新探针（3 前缀 × 2 次）
        self.assertEqual(http_get.call_count, 6)

    # === _TTLCache 类 ===

    def test_ttl_cache_hit_and_expiry(self):
        cache = server._TTLCache(ttl=0.01)
        self.assertIsNone(cache.get("k"))
        cache.set("k", "v")
        self.assertEqual(cache.get("k"), "v")  # 命中
        time.sleep(0.02)
        self.assertIsNone(cache.get("k"))  # 过期后惰性删除

    # === get_market_snapshot date 缺省 = 最新交易日 ===

    @mock.patch.object(server, "_http_get")
    @mock.patch.object(server, "_latest_trade_date")
    def test_http_dispatch_get_market_snapshot_default_date(self, latest, http_get):
        latest.return_value = "20260813"
        http_get.return_value = [{"date": 20260813, "close": 12.5, "code": "600633"}]

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 160, "method": "tools/call",
            "params": {
                "name": "get_market_snapshot",
                "arguments": {"codes": ["600633"]},
            },
        })

        self.assertIsNone(response["result"].get("isError"))
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["results"][0]["date"], 20260813)
        tables = [call.args[1] for call in http_get.call_args_list]
        self.assertIn("日k:600633:20260813", tables)
        self.assertEqual(latest.call_count, 1)

    # === get_kline 空结果 + 非交易日 hint ===

    @mock.patch.object(server, "_http_get")
    def test_http_dispatch_get_kline_non_trading_day_hint(self, http_get):
        http_get.return_value = []
        with mock.patch.object(
            server.calendar_xshg, "is_trading_day", return_value=False
        ):
            with mock.patch.object(
                server.calendar_xshg, "nearest_trading_day", return_value="20260814"
            ):
                response = server.dispatch({
                    "jsonrpc": "2.0", "id": 170, "method": "tools/call",
                    "params": {
                        "name": "get_kline",
                        "arguments": {
                            "code": "600633", "frequency": "1d", "start": "20260815",
                        },
                    },
                })

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["data"], [])
        self.assertIn("非交易日", payload["hint"])
        self.assertIn("20260814", payload["hint"])

    # === query_mydb 游标续取 / lookup key 复合键 ===

    @mock.patch.object(pybao_tools, "get_mydb_rd")
    def test_query_mydb_cursor_pagination(self, get_mydb_rd):
        fake_rd = mock.Mock()
        fake_rd.keys.return_value = [
            "hk日k:00700:20250425",
            "hk日k:00700:20250426",
            "hk日k:00700:20250427",
        ]
        fake_rd.get.return_value = {"close": 12.5}
        get_mydb_rd.return_value = fake_rd

        page1 = pybao_tools.query_mydb({"table": "hk日k", "limit": 2})
        self.assertTrue(page1["ok"])
        r1 = page1["result"]
        self.assertEqual(
            r1["keys"], ["hk日k:00700:20250425", "hk日k:00700:20250426"],
        )
        self.assertEqual(r1["total"], 3)
        self.assertTrue(r1["truncated"])
        self.assertEqual(r1["next_key"], "hk日k:00700:20250426")

        page2 = pybao_tools.query_mydb({
            "table": "hk日k", "limit": 2, "cursor": r1["next_key"],
        })
        r2 = page2["result"]
        self.assertEqual(r2["keys"], ["hk日k:00700:20250427"])
        self.assertFalse(r2["truncated"])
        self.assertIsNone(r2["next_key"])  # 未截断 → next_key None
        # 游标语义：第二页所有键严格大于 cursor
        self.assertTrue(all(str(k) > r1["next_key"] for k in r2["keys"]))

    @mock.patch.object(pybao_tools, "get_mydb_rd")
    def test_query_mydb_lookup_key_keeps_composite(self, get_mydb_rd):
        fake_rd = mock.Mock()
        fake_rd.keys.return_value = ["hk日k:00700:20250425"]
        fake_rd.get.return_value = {"close": 12.5}
        get_mydb_rd.return_value = fake_rd

        outcome = pybao_tools.query_mydb({"table": "hk日k"})

        self.assertTrue(outcome["ok"])
        # lookup key 取首个冒号后的全部（00700:20250425），而非最后一段（20250425）
        lookup_keys = [call.args[1] for call in fake_rd.get.call_args_list]
        self.assertEqual(lookup_keys, ["00700:20250425"])
        self.assertEqual(
            outcome["result"]["values"]["hk日k:00700:20250425"], {"close": 12.5},
        )

    # === screen_stocks 多条件交集 ===

    @mock.patch.object(pybao_tools, "get_sdk_client")
    @mock.patch.object(pybao_tools, "get_pybao")
    def test_screen_stocks_multi_condition_intersection(self, get_pybao, get_sdk_client):
        fake = mock.Mock()

        def fake_jisuan(name, codes, **kwargs):
            if name == "macd":
                return {
                    "600001": [{"date": 20260105, "cross": 1}],
                    "600002": [{"date": 20260105, "cross": 0}],
                    "600003": [{"date": 20260105, "cross": 0}],
                }
            return {
                "600001": [{"date": 20260105, "cross": 1}],
                "600002": [{"date": 20260105, "cross": 1}],
                "600003": [{"date": 20260105, "cross": 0}],
            }

        fake.jisuan.side_effect = fake_jisuan
        get_pybao.return_value = fake
        fake_client = mock.Mock()
        fake_client.get_data.return_value = [
            {"date": 20260105, "float_mv": 5e8, "is_st": False,
             "name": "测试", "close": 10.2, "pct_chg": 3.5},
        ]
        get_sdk_client.return_value = fake_client

        outcome = pybao_tools.screen_stocks(
            {
                "indicator_cross": [
                    {"name": "macd", "golden": True, "within_days": 5},
                    {"name": "kdj", "golden": True, "within_days": 5},
                ],
                "date": "20260105",
            },
            ["600001", "600002", "600003"],
        )

        self.assertTrue(outcome["ok"])
        result = outcome["result"]
        # 交集：600001 命中两条件；600002 只命中 kdj；600003 都不命中
        self.assertEqual(result["matched_count"], 1)
        cand = result["candidates"][0]
        self.assertEqual(cand["code"], "600001")
        self.assertIsNone(cand["signal"])  # 多条件 → signal None
        self.assertEqual(
            cand["crosses"],
            {"macd": {"date": 20260105, "signal": 1},
             "kdj": {"date": 20260105, "signal": 1}},
        )
        self.assertEqual(cand["cross_date"], 20260105)  # 各条件日期最大值
        self.assertEqual(cand["pct_chg"], 3.5)
        self.assertIsInstance(result["elapsed_ms"], int)
        # 每条件独立 jisuan（两次，各自交叉）
        self.assertEqual(fake.jisuan.call_count, 2)
        self.assertEqual(fake.jisuan.call_args_list[0].args[0], "macd")
        self.assertEqual(fake.jisuan.call_args_list[1].args[0], "kdj")

    def test_screen_stocks_duplicate_indicator_rejected(self):
        outcome = pybao_tools.screen_stocks(
            {"indicator_cross": [{"name": "macd"}, {"name": "macd"}]},
            ["600001"],
        )

        self.assertFalse(outcome["ok"])
        self.assertIn("重复", outcome["error"])

    @mock.patch.object(pybao_tools, "get_sdk_client")
    @mock.patch.object(pybao_tools, "get_pybao")
    def test_screen_stocks_single_object_backward_compat(self, get_pybao, get_sdk_client):
        fake = mock.Mock()
        fake.jisuan.return_value = {
            "600001": [{"date": 20260105, "cross": 1}],
            "600002": [{"date": 20260105, "cross": 0}],
        }
        get_pybao.return_value = fake
        fake_client = mock.Mock()
        fake_client.get_data.return_value = [
            {"date": 20260105, "float_mv": 5e8, "is_st": False,
             "name": "测试", "close": 10.2},
        ]
        get_sdk_client.return_value = fake_client

        # 单对象（非数组）保持向后兼容
        outcome = pybao_tools.screen_stocks(
            {"indicator_cross": {"name": "macd", "golden": True, "within_days": 5},
             "date": "20260105"},
            ["600001", "600002"],
        )

        self.assertTrue(outcome["ok"])
        result = outcome["result"]
        self.assertEqual([c["code"] for c in result["candidates"]], ["600001"])
        self.assertEqual(result["candidates"][0]["signal"], 1)  # 单条件 → 信号值

    # === prompts 能力 ===

    def test_http_dispatch_prompts_list(self):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 180, "method": "prompts/list",
        })

        names = {prompt["name"] for prompt in response["result"]["prompts"]}
        self.assertEqual(names, {"screen-workflow", "limit-up-review"})

    def test_http_dispatch_prompts_get_and_unknown(self):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 181, "method": "prompts/get",
            "params": {"name": "screen-workflow"},
        })

        messages = response["result"]["messages"]
        self.assertTrue(messages)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"]["type"], "text")
        self.assertTrue(messages[0]["content"]["text"].strip())

        unknown = server.dispatch({
            "jsonrpc": "2.0", "id": 182, "method": "prompts/get",
            "params": {"name": "no-such-prompt"},
        })
        self.assertEqual(unknown["error"]["code"], -32602)

    # === tools/call stderr 日志（mcp_tool_call 一行 JSON） ===

    @mock.patch.object(server, "query_stock_list")
    def test_tools_call_logs_mcp_tool_call_line(self, query_stock_list):
        query_stock_list.return_value = {"total": 0, "codes": []}

        with mock.patch("sys.stderr", new_callable=io.StringIO) as fake_stderr:
            server.dispatch({
                "jsonrpc": "2.0", "id": 190, "method": "tools/call",
                "params": {"name": "get_stock_list", "arguments": {}},
            })
            captured = fake_stderr.getvalue()

        self.assertIn("mcp_tool_call", captured)
        self.assertIn("get_stock_list", captured)

    # === 进度钩子（线程级 set/clear/notify） ===

    def test_progress_hook_lifecycle(self):
        calls: list[tuple[str, str | None]] = []
        pybao_tools.set_progress_hook(
            lambda stage, detail: calls.append((stage, detail))
        )
        try:
            pybao_tools.notify_progress("a", "b")
            self.assertEqual(calls, [("a", "b")])
            pybao_tools.clear_progress_hook()
            pybao_tools.notify_progress("c", "d")
            self.assertEqual(calls, [("a", "b")])  # clear 后不再触发
        finally:
            pybao_tools.clear_progress_hook()
        # 未设 hook 时 notify 不抛异常
        pybao_tools.notify_progress("e")
        self.assertEqual(calls, [("a", "b")])


    # === Phase 3：envelope 8 键逐工具断言（Task D，≥8 工具） ===

    @mock.patch.object(server, "query_stock_list")
    def test_envelope_get_stock_list(self, query_stock_list):
        query_stock_list.return_value = {"total": 2, "codes": ["600001", "600002"]}
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 300, "method": "tools/call",
            "params": {"name": "get_stock_list", "arguments": {}},
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self._assert_envelope(
            payload, source="http", contract="stock-list-v1",
            known_at=None, total=2,
        )
        self.assertEqual(payload["codes"], ["600001", "600002"])

    @mock.patch.object(server, "_http_get")
    def test_envelope_get_adjust_factors(self, http_get):
        # dispatch 成功路径：list 结果被包装为 {"data": [...]} 并注入 envelope
        with mock.patch.object(server, "_TTL", server._TTLCache()):
            http_get.return_value = [
                ["复权:600633:20260813",
                 {"div": 1.0, "give": 0.0, "trans": 0.0, "mult": 1.0, "cum": 1.0}],
            ]
            response = server.dispatch({
                "jsonrpc": "2.0", "id": 301, "method": "tools/call",
                "params": {
                    "name": "get_adjust_factors",
                    "arguments": {"code": "600633", "date_pattern": "20260813"},
                },
            })
        payload = json.loads(response["result"]["content"][0]["text"])
        self._assert_envelope(
            payload, source="http", contract="adjust-factors-v1",
            known_at=None, total=1,
        )
        self.assertEqual(payload["data"], [{
            "date": "20260813", "div": 1.0, "give": 0.0, "trans": 0.0,
            "mult": 1.0, "cum": 1.0,
        }])

    @mock.patch.object(server, "_http_get")
    def test_envelope_get_market_snapshot(self, http_get):
        http_get.return_value = [
            {"date": 20260813, "open": 10.0, "close": 10.5, "code": "600633"},
        ]
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 302, "method": "tools/call",
            "params": {
                "name": "get_market_snapshot",
                "arguments": {"date": "20260813", "codes": ["600633"]},
            },
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self._assert_envelope(
            payload, source="http", contract="market-snapshot-v1",
            known_at="20260813",
        )
        self.assertEqual(payload["results"][0]["date"], 20260813)
        self.assertEqual(payload["errors"], [])

    @mock.patch.object(server, "query_board_open_effect_history")
    def test_envelope_get_board_open_effect_history(self, query_history):
        query_history.return_value = {
            "days": [{"date": "2026-01-05", "eligible_count": 1}],
            "is_partial": False,
            "known_limitations": ["测试限制"],
        }
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 303, "method": "tools/call",
            "params": {
                "name": "get_board_open_effect_history",
                "arguments": {"start": "20260101", "end": "20260131"},
            },
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self._assert_envelope(
            payload, source="http", contract="board-open-effect-v1",
            known_at=None,
        )
        self.assertIsNone(payload["total"])
        self.assertEqual(payload["days"][0]["date"], "2026-01-05")
        self.assertEqual(payload["known_limitations"], ["测试限制"])

    @mock.patch.object(pybao_tools, "compute_indicators")
    def test_envelope_get_indicators(self, compute):
        compute.return_value = {
            "ok": True,
            "result": {
                "source": "pybao", "frequency": "1d", "indicators": ["macd"],
                "params": None, "compact": True, "truncated": False,
                "truncated_rows": 0,
                "data": {"dates": [20260102, 20260105], "macd": [0.5, 0.7]},
            },
        }
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 304, "method": "tools/call",
            "params": {
                "name": "get_indicators",
                "arguments": {"indicators": ["macd"], "codes": ["600633"]},
            },
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self._assert_envelope(
            payload, source="pybao", contract="indicators-v1",
            known_at="20260105", total={"dates": 2, "macd": 2},
        )
        self.assertEqual(payload["data"]["dates"], [20260102, 20260105])

    @mock.patch.object(pybao_tools, "screen_stocks")
    @mock.patch.object(server, "query_stock_list")
    def test_envelope_screen_stocks(self, query_stock_list, screen_stocks):
        query_stock_list.return_value = {"total": 2, "codes": ["600001", "600002"]}
        screen_stocks.return_value = {
            "ok": True,
            "result": {
                "source": "pybao", "date": 20260105, "candidates": [],
                "matched_count": 0, "dropped": {"missing_bar": 0, "st": 0, "mv": 0},
            },
        }
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 305, "method": "tools/call",
            "params": {
                "name": "screen_stocks",
                "arguments": {"indicator_cross": {"name": "macd"}},
            },
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self._assert_envelope(
            payload, source="pybao", contract="screen-v1",
            known_at="20260105",
        )
        self.assertEqual(payload["universe"]["source"], "full_market")
        self.assertEqual(payload["candidates"], [])

    @mock.patch.object(pybao_tools, "query_mydb")
    def test_envelope_get_mydb_data(self, query_mydb):
        query_mydb.return_value = {
            "ok": True,
            "result": {
                "source": "pybao", "table": "hk日k",
                "keys": ["00700:20260813"],
                "values": {"00700:20260813": {"close": 12.5}},
                "total": 1, "truncated": False,
            },
        }
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 306, "method": "tools/call",
            "params": {"name": "get_mydb_data", "arguments": {"table": "hk日k"}},
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self._assert_envelope(
            payload, source="pybao", contract="mydb-v1",
            known_at=None, total=1,
        )
        self.assertEqual(payload["keys"], ["00700:20260813"])

    def test_envelope_get_trading_days(self):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 307, "method": "tools/call",
            "params": {
                "name": "get_trading_days",
                "arguments": {"start": "20260810", "end": "20260814"},
            },
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self._assert_envelope(
            payload, source="static", contract="calendar-v1", known_at=None,
        )
        self.assertIsNone(payload["total"])
        self.assertEqual(payload["count"], 5)
        self.assertEqual(len(payload["trading_days"]), 5)

    @mock.patch.object(pybao_tools, "get_pybao")
    @mock.patch.object(server, "_latest_trade_date")
    def test_envelope_get_data_status(self, latest, get_pybao):
        latest.return_value = "20260813"
        get_pybao.return_value = None
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 308, "method": "tools/call",
            "params": {"name": "get_data_status", "arguments": {}},
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self._assert_envelope(
            payload, source="http", contract="status-v1", known_at="20260813",
        )
        self.assertEqual(payload["tool_count"], 12)
        self.assertFalse(payload["pybao_available"])

    @mock.patch.object(server, "_latest_trade_date")
    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "_http_get")
    def test_envelope_get_point_snapshot(self, http_get, query_stock_list, latest):
        query_stock_list.return_value = {"total": 1, "codes": ["600001"]}
        latest.return_value = "20260813"
        http_get.return_value = {"date": 20260813, "open": 10.0, "close": 10.5}
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 309, "method": "tools/call",
            "params": {"name": "get_point_snapshot", "arguments": {"date": "20260813"}},
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self._assert_envelope(
            payload, source="http", contract="snapshot-v1",
            known_at="20260813",
        )
        self.assertIsNone(payload["total"])
        self.assertEqual(payload["points"][0]["status"], "TRADED")

    # === Phase 3：kline-v2（mode/units/total 截断前语义） ===

    @mock.patch.object(server, "_http_get")
    def test_kline_v2_mode_point_and_units(self, http_get):
        http_get.return_value = [
            {"date": 20260105, "open": 10.0, "close": 10.2},
        ]
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 320, "method": "tools/call",
            "params": {
                "name": "get_kline",
                "arguments": {"code": "600633", "frequency": "1d", "start": "20260105"},
            },
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self._assert_envelope(
            payload, source="http", contract="kline-v2",
            known_at="20260105", total=1,
        )
        self.assertEqual(payload["mode"], "point")  # 无 end → point
        self.assertEqual(payload["fq"], "none")
        for key in ("price_unit", "volume_unit", "amount_unit"):
            self.assertIsInstance(payload[key], str)
            self.assertTrue(payload[key])

    @mock.patch.object(server, "_http_get")
    def test_kline_v2_limit_total_is_pre_truncation(self, http_get):
        http_get.return_value = [
            {"date": 20260101 + i, "close": float(i)} for i in range(5)
        ]
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 321, "method": "tools/call",
            "params": {
                "name": "get_kline",
                "arguments": {
                    "code": "600633", "frequency": "1d",
                    "start": "20260101", "end": "20260131", "limit": 2,
                },
            },
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self._assert_envelope(
            payload, source="http", contract="kline-v2",
            known_at="20260105", truncated=True, total=5,
        )
        self.assertEqual(payload["mode"], "range")  # 有 end → range
        self.assertEqual(len(payload["data"]), 2)   # data 截断变短
        self.assertEqual(payload["total"], 5)       # total 仍是截断前数量

    # === Phase 3：闭区间 _bump_end 与 SDK 路径 ===

    def test_bump_end_daily(self):
        self.assertEqual(server._bump_end("20260813", "1d"), "20260814")

    def test_bump_end_minute_8digit_date(self):
        self.assertEqual(server._bump_end("20260813", "5m"), "20260814000000")

    def test_bump_end_minute_14digit_ts(self):
        self.assertEqual(
            server._bump_end("20260813150000", "5m"), "20260813150100",
        )

    @mock.patch.object(pybao_tools, "get_sdk_client")
    def test_kline_sdk_range_includes_end_day(self, get_sdk_client):
        fake_client = mock.Mock()
        fake_client.get_data.return_value = [
            {"date": 20260812, "close": 10.0},
            {"date": 20260813, "close": 10.5},   # end 当日行（闭区间必须保留）
        ]
        get_sdk_client.return_value = fake_client

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 322, "method": "tools/call",
            "params": {
                "name": "get_kline",
                "arguments": {
                    "code": "600633", "fq": "qfq", "frequency": "1d",
                    "start": "20260810", "end": "20260813",
                },
            },
        })

        payload = json.loads(response["result"]["content"][0]["text"])
        # 闭区间生效：含 end 当日行
        self.assertEqual(
            [row["date"] for row in payload["data"]], [20260812, 20260813],
        )
        # SDK 以 bumped end（end+1 日）查询实现闭区间
        self.assertEqual(fake_client.get_data.call_args.kwargs["end"], "20260814")

    # === Phase 3：get_point_snapshot 分类 / 覆盖率 / 部分性 ===

    @mock.patch.object(server, "_latest_trade_date")
    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "_http_get")
    def test_point_snapshot_traded_classification(self, http_get, query_stock_list, latest):
        query_stock_list.return_value = {"total": 1, "codes": ["600001"]}
        latest.return_value = "20260813"
        http_get.return_value = {"date": 20260813, "open": 10.0, "close": 10.5}

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 330, "method": "tools/call",
            "params": {"name": "get_point_snapshot", "arguments": {"date": "20260813"}},
        })

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["points"][0]["status"], "TRADED")
        self.assertEqual(payload["coverage"]["universe"], 1)
        self.assertEqual(payload["coverage"]["requested"], 1)
        self.assertEqual(payload["coverage"]["traded"], 1)
        self.assertFalse(payload["is_partial"])
        self.assertTrue(payload["coverage"]["formal_usable"])

    @mock.patch.object(server, "_latest_trade_date")
    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "_http_get")
    def test_point_snapshot_invalid_symbol(self, http_get, query_stock_list, latest):
        query_stock_list.return_value = {"total": 1, "codes": ["600001"]}
        latest.return_value = "20260813"
        http_get.return_value = None

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 331, "method": "tools/call",
            "params": {
                "name": "get_point_snapshot",
                "arguments": {"date": "20260813", "codes": ["999999"]},
            },
        })

        payload = json.loads(response["result"]["content"][0]["text"])
        err = payload["errors"][0]
        self.assertEqual(err["code"], "INVALID_SYMBOL")
        self.assertEqual(err["symbol"], "999999")
        self.assertEqual(err["message"], "代码不在股票池")
        self.assertEqual(payload["coverage"]["invalid_symbol"], 1)

    @mock.patch.object(server, "_latest_trade_date")
    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "_http_get")
    def test_point_snapshot_not_published(self, http_get, query_stock_list, latest):
        query_stock_list.return_value = {"total": 1, "codes": ["600001"]}
        latest.return_value = "20260813"
        http_get.return_value = None

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 332, "method": "tools/call",
            "params": {
                "name": "get_point_snapshot",
                "arguments": {"date": "20260814", "codes": ["600001"]},
            },
        })

        payload = json.loads(response["result"]["content"][0]["text"])
        err = payload["errors"][0]
        self.assertEqual(err["code"], "NOT_PUBLISHED")
        self.assertEqual(err["message"], "该时点数据尚未入库/尚未发布")
        self.assertEqual(payload["coverage"]["not_published"], 1)

    @mock.patch.object(server, "_latest_trade_date")
    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "_http_get")
    def test_point_snapshot_suspended(self, http_get, query_stock_list, latest):
        query_stock_list.return_value = {"total": 1, "codes": ["600001"]}
        latest.return_value = "20260813"
        http_get.return_value = None

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 333, "method": "tools/call",
            "params": {
                "name": "get_point_snapshot",
                "arguments": {"date": "20260813", "codes": ["600001"]},
            },
        })

        payload = json.loads(response["result"]["content"][0]["text"])
        err = payload["errors"][0]
        self.assertEqual(err["code"], "NO_DATA")
        self.assertIn("停牌", err["message"])
        self.assertEqual(payload["coverage"]["suspended"], 1)

    def test_point_snapshot_non_trading_day_invalid_argument(self):
        response = server.dispatch({
            "jsonrpc": "2.0", "id": 334, "method": "tools/call",
            "params": {"name": "get_point_snapshot", "arguments": {"date": "20260815"}},
        })

        result = response["result"]
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["code"], "INVALID_ARGUMENT")
        self.assertIn("非交易日", payload["error"])
        self.assertIn("20260814", payload["hint"])  # 最近交易日提示

    @mock.patch.object(server, "_latest_trade_date")
    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "_http_get")
    def test_point_snapshot_explicit_codes_partial(self, http_get, query_stock_list, latest):
        query_stock_list.return_value = {"total": 2, "codes": ["600001", "600002"]}
        latest.return_value = "20260813"
        http_get.return_value = {"date": 20260813, "open": 10.0, "close": 10.5}

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 335, "method": "tools/call",
            "params": {
                "name": "get_point_snapshot",
                "arguments": {"date": "20260813", "codes": ["600001"]},
            },
        })

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(payload["is_partial"])
        self.assertFalse(payload["coverage"]["formal_usable"])
        self.assertIn("EXPLICIT_CODES_DEBUG", payload["coverage"]["partial_reasons"])

    @mock.patch.object(server, "_latest_trade_date")
    @mock.patch.object(server, "query_stock_list")
    @mock.patch.object(server, "_http_get")
    def test_point_snapshot_full_market_partial_failure(self, http_get, query_stock_list, latest):
        query_stock_list.return_value = {"total": 2, "codes": ["600001", "300001"]}
        latest.return_value = "20260813"

        def fake_get(cmd, table):
            if "300001" in table:
                raise OSError("timeout")
            return {"date": 20260813, "open": 10.0, "close": 10.5}

        http_get.side_effect = fake_get

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 336, "method": "tools/call",
            "params": {"name": "get_point_snapshot", "arguments": {"date": "20260813"}},
        })

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["coverage"]["universe"], 2)
        self.assertEqual(payload["coverage"]["requested"], 2)
        self.assertEqual(payload["coverage"]["traded"], 1)
        self.assertEqual(payload["coverage"]["failed"], 1)
        self.assertFalse(payload["coverage"]["formal_usable"])
        self.assertIn("SOURCE_REQUEST_FAILED", payload["coverage"]["partial_reasons"])
        err = payload["errors"][0]
        self.assertEqual(err["code"], "INTERNAL_ERROR")
        self.assertEqual(err["symbol"], "300001")
        self.assertIn("timeout", err["message"])

    # === Phase 3：errors 元素契约形态 {"code","symbol","message"} ===

    @mock.patch.object(server, "_http_get")
    def test_market_snapshot_errors_contract_shape(self, http_get):
        def fake_get(cmd, table):
            if "600002" in table:
                raise OSError("boom")
            return None  # 600001 合法查询但无数据

        http_get.side_effect = fake_get

        response = server.dispatch({
            "jsonrpc": "2.0", "id": 340, "method": "tools/call",
            "params": {
                "name": "get_market_snapshot",
                "arguments": {"date": "20260813", "codes": ["600001", "600002"]},
            },
        })

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(len(payload["errors"]), 2)
        for err in payload["errors"]:
            self.assertEqual(set(err.keys()), {"code", "symbol", "message"})
            self.assertIsInstance(err["code"], str)
            self.assertIsInstance(err["symbol"], str)
            self.assertIsInstance(err["message"], str)
        self.assertEqual(payload["errors"][0]["code"], "NO_DATA")
        self.assertEqual(payload["errors"][0]["symbol"], "600001")
        self.assertEqual(payload["errors"][1]["code"], "INTERNAL_ERROR")
        self.assertEqual(payload["errors"][1]["symbol"], "600002")

    # === Phase 3：_apply_contract 派生规则（list 包装 / total / known_at） ===

    def test_apply_contract_wraps_list_result(self):
        out = server._apply_contract(
            "get_adjust_factors", [{"date": "20260813", "div": 1.0}],
        )
        self.assertEqual(out["data"], [{"date": "20260813", "div": 1.0}])
        self.assertEqual(out["source"], "http")
        self.assertEqual(out["source_contract_version"], "adjust-factors-v1")
        self.assertIsNone(out["known_at"])
        self.assertEqual(out["total"], 1)  # list → len
        self.assertFalse(out["truncated"])
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["known_limitations"], [])

    def test_apply_contract_total_derived_batch(self):
        result = {
            "source": "pybao",
            "data": {
                "600633": [{"date": 20260102, "close": 1.0}],
                "000001": [{"date": 20260102, "close": 1.0},
                           {"date": 20260103, "close": 1.1}],
            },
        }
        out = server._apply_contract("get_kline", result)
        self.assertEqual(out["total"], {"600633": 1, "000001": 2})

    def test_apply_contract_known_at_derived_from_data(self):
        result = {
            "source": "http",
            "data": [
                {"date": 20260102, "close": 1.0},
                {"date": 20260105, "close": 1.2},
                {"date": 20260103, "close": 1.1},
            ],
        }
        out = server._apply_contract("get_kline", result)
        self.assertEqual(out["known_at"], "20260105")  # data 最大 date
        self.assertEqual(out["total"], 3)
        self.assertEqual(out["source_contract_version"], "kline-v2")


if __name__ == "__main__":
    unittest.main()

# === 0.8.x 连接卫生回归（全市场快照节流：每请求 sleep + limit=0 全量） ===
class _SnapshotPacingTests(unittest.TestCase):
    """0.8.7 全市场快照双路径：SDK 批量快路径 + HTTP 回退（节流/全量契约）。"""

    CODES = [f"6000{i:02d}" for i in range(60)]  # 60 只 > 截断上限 50：让截断 bug 无所遁形
    BAR = {"date": 20260814, "open": 10.0, "pre_close": 10.0, "close": 11.0,
           "high": 11.0, "low": 9.9, "volume": 100, "amount": 1000, "is_st": False}

    def _patch_universe(self):
        # query_stock_list 有 300s TTL 缓存：测试隔离必须先清缓存再打桩
        server._TTL._entries.pop("stock_list", None)
        return mock.patch.object(server, "query_stock_list",
                                 return_value={"codes": self.CODES})

    def test_sdk_batch_path_fq_none_and_no_truncation(self):
        """SDK 可用 → 走 pipeline 批量；fq=None 取不复权原始价；limit=0 全量不截断。"""
        calls = []
        bar_row = self.BAR  # 闭包捕获外层 BAR：嵌套类内 self 指向 FakeSDK 实例
        class FakeSDK:
            def get_data(self, codes, **kw):
                calls.append(kw)
                return {c: [dict(bar_row, code=c)] for c in codes}
        with self._patch_universe(), \
             mock.patch.object(server.pybao_tools, "get_sdk_client", return_value=FakeSDK()):
            result = server.query_point_snapshot({"date": "20260814", "limit": 0})
        self.assertEqual(len(result["points"]), 60)  # limit=0 必须全量，不截断
        self.assertFalse(result["truncated"])
        self.assertTrue(all(kw.get("fq") is None for kw in calls))   # 原始价口径
        self.assertGreaterEqual(len(calls), 2)                       # 60 只分两块（50+10）                              # 一批一次往返（20 只 < 1000）

    def test_sdk_unavailable_falls_back_with_pacing(self):
        """SDK 不可用 → HTTP 回退：每请求 50ms 节流 + 全量不截断。"""
        with self._patch_universe(), \
             mock.patch.object(server.pybao_tools, "get_sdk_client", return_value=None), \
             mock.patch.object(server, "_http_get", return_value=self.BAR), \
             mock.patch.object(server.time, "sleep") as m_sleep:
            result = server.query_point_snapshot({"date": "20260814", "limit": 0})
        self.assertEqual(m_sleep.call_count, 60)      # 每只一次节流
        self.assertEqual(len(result["points"]), 60)
        self.assertFalse(result["truncated"])


class _AuctionKeyContractTests(unittest.TestCase):
    """0.9.10 键契约回归：写端键形（表=打板指标:<date>，键=metrics）必须可读。"""

    @staticmethod
    def _payload(**overrides) -> dict:
        payload = {
            "metrics": {"premium_mean": 1.21, "success_rate": 0.49, "n_samples": 57},
            "rank_60d": {"premium_mean": 0.5, "success_rate": 0.5},
            "strength_60d": {"premium_mean": "neutral", "success_rate": "neutral"},
            "window": 60, "n_samples": 57,
            "computed_at": "2026-01-05T09:26:03", "value_source": "auction",
            "contract": "auction-metric-v1",
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def _daily_payload() -> dict:
        """写端 0.9.10 真实载荷形态：打板指标:<date> 表 + metrics 键，payload 含 daily。

        daily 结构 = core.auction_metrics.build_daily_row 输出（统计/分布/指标/溯源）。
        """
        metrics = {"premium_mean": 1.21, "success_rate": 0.4912, "n_samples": 57}
        daily = {
            "trade_date": "2026-01-05",
            "matched_count": 57,
            "positive_count": 28, "flat_count": 1, "negative_count": 28,
            "success_rate": 0.4912, "average_open_return_pct": 1.2113,
            "p10_open_return_pct": -2.0, "p25_open_return_pct": -1.0,
            "median_open_return_pct": 0.5, "p75_open_return_pct": 2.0,
            "p90_open_return_pct": 3.5,
            "distribution": {
                "open_return_pct": [-2.0, -1.0, 0.5, 2.0, 3.5],
                "sample_codes": ["600001", "600002", "600003", "600004", "600005"],
            },
            "metrics": dict(metrics),
            "rank_60d": {"premium_mean": 0.5, "success_rate": 0.5},
            "strength_60d": {"premium_mean": "neutral", "success_rate": "neutral"},
            "window": 60, "n_samples": 57,
            "computed_at": "2026-01-05T09:26:03", "value_source": "auction",
            "contract": "auction-metric-v1",
            "coverage": {"codes_requested": 59, "fetched": 57,
                         "fetch_errors": 2, "missing_open": 0},
            "known_at": "2026-01-05T09:25:00",
        }
        return _AuctionKeyContractTests._payload(metrics=metrics, daily=daily)

    def test_read_metrics_new_key_contract(self):
        """新契约：rd.get("打板指标:20260105", "metrics") 必须命中（0.9.10 键契约修复）。"""
        fake_rd = mock.Mock()
        fake_rd.get.return_value = self._payload()
        row = {"trade_date": "2026-01-05"}

        server._attach_auction_metrics(row, fake_rd, "2026-01-05")

        fake_rd.get.assert_called_once_with("打板指标:20260105", "metrics")
        self.assertEqual(row["metrics"]["premium_mean"], 1.21)
        self.assertEqual(row["metrics"]["n_samples"], 57)
        self.assertEqual(row["metrics"]["value_source"], "auction")

    def test_read_metrics_legacy_key_fallback(self):
        """旧契约回退：新键形 miss 时 rd.get("打板指标", "20260105")（0.8.x 遗留数据）。"""
        fake_rd = mock.Mock()
        fake_rd.get.side_effect = [None, self._payload()]

        row = {"trade_date": "2026-01-05"}
        server._attach_auction_metrics(row, fake_rd, "2026-01-05")

        self.assertEqual(
            fake_rd.get.call_args_list,
            [mock.call("打板指标:20260105", "metrics"),
             mock.call("打板指标", "20260105")],
        )
        self.assertEqual(row["metrics"]["n_samples"], 57)

    def test_read_metrics_prefers_research_store(self):
        """双通道优先级：research_store（写端同源）命中时引擎 rd 不被触碰。"""
        fake_rd = mock.Mock()
        store = mock.Mock()
        store.read_metrics.return_value = self._payload()

        row = {"trade_date": "2026-01-05"}
        server._attach_auction_metrics(row, fake_rd, "2026-01-05", store=store)

        store.read_metrics.assert_called_once_with("20260105")
        fake_rd.get.assert_not_called()
        self.assertEqual(row["metrics"]["premium_mean"], 1.21)

    def test_precomputed_row_reads_daily_payload(self):
        store = mock.Mock()
        store.read_metrics.return_value = self._daily_payload()

        daily = server._precomputed_row(None, "2026-01-05", store)

        self.assertIsNotNone(daily)
        self.assertEqual(daily["matched_count"], 57)
        self.assertEqual(
            daily["distribution"]["sample_codes"],
            ["600001", "600002", "600003", "600004", "600005"],
        )

    def test_row_from_daily_is_homomorphic_with_kline_rows(self):
        """daily 行 → days 行：审计计数置 None 占位（不伪造 0），分布/指标原样。"""
        daily = self._daily_payload()["daily"]
        row = server._row_from_daily(daily, "2026-01-05", include_distribution=True)

        self.assertEqual(row["trade_date"], "2026-01-05")
        self.assertEqual(row["matched_count"], 57)
        for field in server.BOARD_OPEN_COUNTER_FIELDS:
            self.assertIsNone(row[field])  # 采集器侧不可得：null 而非 0
        self.assertEqual(row["distribution"], daily["distribution"])
        self.assertIn("metrics", row)
        self.assertEqual(row["metrics"]["n_samples"], 57)

    def test_row_from_daily_strips_distribution_when_excluded(self):
        daily = self._daily_payload()["daily"]
        row = server._row_from_daily(daily, "2026-01-05", include_distribution=False)
        self.assertNotIn("distribution", row)


class _AuctionFastPathTests(unittest.TestCase):
    """0.9.10 预计算快速通道：全覆盖直读 mydb/SQLite，不扫全市场。"""

    def test_fast_path_full_precomputed_skips_market_scan(self):
        """区间内全部交易日有 daily 行 → 直读返回：不调 stock_list/日K，cache_hit=true。"""
        store = mock.Mock()
        store.read_metrics.side_effect = (
            lambda d8: _AuctionKeyContractTests._daily_payload()
            if d8 == "20260105" else None)
        store.read_snapshots.return_value = {}
        with mock.patch.object(server, "_get_research_store", return_value=store), \
             mock.patch.object(server.pybao_tools, "get_mydb_rd", return_value=None), \
             mock.patch.object(server, "query_stock_list",
                               side_effect=AssertionError("快速通道不应拉全市场")) as qsl, \
             mock.patch.object(server, "query_daily_kline",
                               side_effect=AssertionError("快速通道不应拉日K")) as qdk:
            result = server.query_board_open_effect_history(
                "20260105", "20260105", include_distribution=True)

        qsl.assert_not_called()
        qdk.assert_not_called()
        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["load_path"], "mydb")
        self.assertEqual(result["precomputed_days"], 1)
        self.assertIsNone(result["fallback_reason"])
        self.assertEqual(len(result["days"]), 1)
        day = result["days"][0]
        self.assertEqual(day["trade_date"], "2026-01-05")
        self.assertEqual(day["matched_count"], 57)
        self.assertIn("distribution", day)  # include_distribution=true 保留分布
        self.assertEqual(result["known_at"], "20260105 09:25 预计算(auction)")
        self.assertEqual(result["sample_coverage"]["codes_requested"], 59)
        self.assertTrue(result["sample_coverage"]["complete"])

    def test_fast_path_strips_distribution_when_excluded(self):
        store = mock.Mock()
        store.read_metrics.side_effect = (
            lambda d8: _AuctionKeyContractTests._daily_payload()
            if d8 == "20260105" else None)
        store.read_snapshots.return_value = {}
        with mock.patch.object(server, "_get_research_store", return_value=store), \
             mock.patch.object(server.pybao_tools, "get_mydb_rd", return_value=None):
            result = server.query_board_open_effect_history(
                "20260105", "20260105", include_distribution=False)

        self.assertNotIn("distribution", result["days"][0])
        self.assertEqual(result["sample_coverage"]["n_samples"], 57)

    def test_fast_path_partial_precomputed_falls_back(self):
        """区间部分交易日无预计算 → 全市场重算慢路径（cache_hit=false + fallback 标记）。"""
        store = mock.Mock()
        store.read_metrics.side_effect = (
            lambda d8: _AuctionKeyContractTests._daily_payload()
            if d8 == "20260105" else None)  # 01-06 无预计算
        store.read_snapshots.return_value = {}
        with mock.patch.object(server, "_get_research_store", return_value=store), \
             mock.patch.object(server.pybao_tools, "get_mydb_rd", return_value=None), \
             mock.patch.object(server, "query_stock_list",
                               return_value={"codes": ["600001", "600002"]}), \
             mock.patch.object(server, "query_daily_kline",
                               side_effect=lambda code, *_: [
                                   {"date": 20260106, "open": 10.0, "high": 10.5,
                                    "low": 9.8, "close": 10.2, "pre_close": 10.0,
                                    "amount": 1, "is_st": 0, "code": code}]):
            result = server.query_board_open_effect_history(
                "20260105", "20260106", include_distribution=False)

        self.assertFalse(result["cache_hit"])
        self.assertEqual(result["load_path"], "kline")
        self.assertEqual(result["precomputed_days"], 0)
        self.assertIn("已全市场重算", result["fallback_reason"])

    def test_merge_prefers_daily_row_when_present(self):
        """慢路径合并：有预计算 daily 的日期优先用完整日级行（写端同源）。"""
        fake_rd = mock.Mock()
        fake_rd.keys.return_value = ["竞价快照:20260105:600001"]
        fake_rd.get.return_value = {"open_price": 10.3, "prev_close": 10.0,
                                    "source": "tencent", "fetched_at": "2026-01-05T09:26:03"}
        store = mock.Mock()
        store.read_metrics.side_effect = (
            lambda d8: _AuctionKeyContractTests._daily_payload()
            if d8 == "20260105" else None)
        with mock.patch.object(server, "_get_research_store", return_value=store), \
             mock.patch.object(server.pybao_tools, "get_mydb_rd", return_value=fake_rd):
            days = []
            known_at, error, stats = server._merge_auction_days(
                {}, days, "2026-01-05", "2026-01-05", include_distribution=True)

        self.assertEqual(stats["precomputed_dates"], ["2026-01-05"])
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0]["matched_count"], 57)  # daily 行（非快照重组）
        self.assertIn("预计算(auction)", known_at)
        self.assertIsNone(error)

    def test_merge_falls_back_to_snapshot_row_without_daily(self):
        """无预计算但有快照：T-1 日K 判定 + 快照溢价重组（现逻辑，引擎键形兼容）。"""
        fake_rd = mock.Mock()
        fake_rd.keys.return_value = ["竞价快照:20260105:600001"]
        fake_rd.get.return_value = {"open_price": 10.3, "prev_close": 10.0,
                                    "source": "tencent", "fetched_at": "2026-01-05T09:26:03"}
        store = mock.Mock()
        store.read_metrics.return_value = None
        store.read_snapshots.return_value = {}
        with mock.patch.object(server, "_get_research_store", return_value=store), \
             mock.patch.object(server.pybao_tools, "get_mydb_rd", return_value=fake_rd):
            # T-1（2026-01-02）非一字板涨停：close=涨停价 11.0，open=10.5 < 11.0
            snapshot = {"2026-01-02": [server.DailyBar(
                code="600001", close=11.0, high=11.0, low=10.5,
                amount=1, prev_close=10.0, open=10.5, is_st=False)]}
            days = []
            known_at, error, stats = server._merge_auction_days(
                snapshot, days, "2026-01-05", "2026-01-05", include_distribution=True)

        self.assertEqual(stats["precomputed_dates"], [])
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0]["prior_limit_up_count"], 1)
        self.assertEqual(days[0]["matched_count"], 1)
        # 溢价 = 快照 open_price / T-1 收盘（11.0 涨停价）- 1（0.8.13 分母口径）
        self.assertAlmostEqual(days[0]["average_open_return_pct"],
                               (10.3 / 11.0 - 1) * 100)
        self.assertIn("竞价采集(source=tencent)", known_at)

    def test_fast_path_engine_rd_legacy_keys(self):
        """快速通道引擎通道：旧键形（表=打板指标，键=<date>）也能读（0.8.x 数据兼容）。"""
        fake_rd = mock.Mock()
        fake_rd.get.side_effect = [None, _AuctionKeyContractTests._daily_payload()]
        store = None
        with mock.patch.object(server, "_get_research_store", return_value=store), \
             mock.patch.object(server.pybao_tools, "get_mydb_rd", return_value=fake_rd), \
             mock.patch.object(server, "query_stock_list",
                               side_effect=AssertionError("不应拉全市场")), \
             mock.patch.object(server, "query_daily_kline",
                               side_effect=AssertionError("不应拉日K")):
            result = server.query_board_open_effect_history(
                "20260105", "20260105", include_distribution=False)

        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["days"][0]["matched_count"], 57)
        fake_rd.get.assert_any_call("打板指标:20260105", "metrics")
        fake_rd.get.assert_any_call("打板指标", "20260105")


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()

# === 0.8.x 连接卫生回归（全市场快照节流：每请求 sleep + limit=0 全量） ===
