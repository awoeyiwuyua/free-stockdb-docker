import json
import unittest
from unittest import mock

from mcp import stockdb_mcp_server as server
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
        self.assertEqual(len(tool_names), 9)
        self.assertIn("get_stock_list", tool_names)
        self.assertIn("get_board_open_effect_history", tool_names)
        self.assertIn("get_indicators", tool_names)
        self.assertIn("get_board_members", tool_names)
        self.assertIn("screen_stocks", tool_names)
        self.assertIn("get_mydb_data", tool_names)

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
        self.assertEqual(result["total"], 600)
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
            "data": [{"name": "半导体", "count": 10}],
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
        self.assertEqual(payload, expected)

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
        self.assertEqual(set(payload["data"].keys()), {"600633", "000001"})
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
        self.assertEqual(payload, expected)

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


if __name__ == "__main__":
    unittest.main()
