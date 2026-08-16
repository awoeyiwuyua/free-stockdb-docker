"""test_research_store — 0.9.5 M5 研究成果仓储单测。

覆盖：SQLite CRUD（metrics/series/lists/snapshots）/ NaN 护栏 / WAL 模式 /
migrate_from_engine（mock 引擎 mydb）/ backup 保留 / 工厂切换（sqlite/mydb）/
接口层 query_mydb 研究表路由。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from storage import research_factory
from storage.research_store import MydbResearchStore, SqliteResearchStore


class _FakeRd:
    """引擎 mydb 替身：三段键（表:日期/名:子键）语义。"""

    def __init__(self):
        self.data = {}  # {(table, key): value}

    def get(self, table, key):
        return self.data.get((table, key))

    def set(self, table, key, value):
        self.data[(table, key)] = value
        return self

    def do(self):
        return self

    def keys(self, table, pattern="*"):
        # 前缀匹配表名（引擎实测语义：table 参数按前缀匹配命名空间）
        out = []
        for (t, k) in self.data:
            if t.startswith(table):
                out.append(f"{t}:{k}")
        return out


class SqliteStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_research_")
        p = mock.patch.object(config, "DATA_DIR", Path(self.tmp))
        p.start()
        self.addCleanup(p.stop)
        self.store = SqliteResearchStore(Path(self.tmp) / "research.db")
        self.addCleanup(self.store.close)
        research_factory.reset()

    def test_crud_roundtrip(self):
        payload = {"metrics": {"n_samples": 47}, "rank_60d": None}
        self.store.write_metrics("20260814", payload)
        self.assertEqual(self.store.read_metrics("20260814"), payload)
        self.assertIsNone(self.store.read_metrics("20260813"))
        self.store.write_series("premium_mean", {"values": [0.01]})
        self.assertEqual(self.store.read_series("premium_mean")["values"], [0.01])
        self.store.write_list("20260815", {"codes": ["600004"]})
        self.assertEqual(self.store.read_list("20260815")["codes"], ["600004"])
        self.store.write_snapshots("20260814", {"600004": {"open": 11.0}})
        self.assertEqual(self.store.read_snapshots("20260814")["600004"]["open"], 11.0)
        self.assertEqual(self.store.read_snapshots("20260814"), {"600004": {"open": 11.0}})

    def test_nan_guardrail(self):
        self.store.write_metrics("20260814", {"open": float("nan")})
        self.assertIsNone(self.store.read_metrics("20260814"))  # 未落盘
        self.store.write_metrics("20260814", {"close": float("inf")})
        self.assertIsNone(self.store.read_metrics("20260814"))
        self.store.write_metrics("20260814", {"ok": True})
        self.assertIsNotNone(self.store.read_metrics("20260814"))

    def test_wal_mode(self):
        conn = self.store._connect()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")

    def test_migrate_from_engine(self):
        """从引擎 mydb 三段键全量导入（幂等）。"""
        rd = _FakeRd()
        rd.data[("打板指标:20260814", "metrics")] = {"n_samples": 47}
        rd.data[("打板指标:20260813", "metrics")] = {"n_samples": 45}
        rd.data[("打板序列:premium_mean", "series")] = {"values": [0.01]}
        rd.data[("清单:20260815", "limitup_non_yizi")] = {"codes": ["600004"]}
        rd.data[("竞价快照:20260814", "600004")] = {"open_price": 11.0}
        from storage.providers import mydb_store as ms
        with mock.patch.object(ms, "_mydb_rd", return_value=rd):
            with mock.patch.object(ms, "_rd_to_py",
                                   side_effect=lambda v: v if isinstance(v, dict) else v):
                res = self.store.migrate_from_engine()
        self.assertTrue(res["ok"])
        self.assertEqual(res["counts"]["metrics"], 2)
        self.assertEqual(res["counts"]["series"], 1)
        self.assertEqual(res["counts"]["lists"], 1)
        self.assertEqual(res["counts"]["snapshots"], 1)
        # 读回核对
        self.assertEqual(self.store.read_metrics("20260814")["n_samples"], 47)
        self.assertEqual(self.store.read_series("premium_mean")["values"], [0.01])
        self.assertEqual(self.store.read_list("20260815")["codes"], ["600004"])
        self.assertEqual(self.store.read_snapshots("20260814")["600004"]["open_price"], 11.0)

    def test_backup_creates_and_keeps(self):
        import time
        self.store.write_metrics("20260814", {"n": 1})
        b1 = self.store.backup()
        self.assertIsNotNone(b1)
        self.assertTrue(Path(b1).exists())
        time.sleep(1.1)  # 备份文件名秒级精度，错开避免同秒覆盖
        self.store.write_metrics("20260814", {"n": 2})
        b2 = self.store.backup()
        self.assertNotEqual(b1, b2)
        backups = sorted((Path(self.tmp) / "backups").glob("research-*.db"))
        self.assertEqual(len(backups), 2)

    def test_factory_sqlite_default(self):
        store = research_factory.get_research_store()
        self.assertIsInstance(store, SqliteResearchStore)
        self.assertIs(store, research_factory.get_research_store())  # 单例

    def test_factory_mydb_rollback(self):
        research_factory.reset()
        with mock.patch.dict(os.environ, {"RESEARCH_STORE": "mydb"}):
            store = research_factory.get_research_store()
        self.assertIsInstance(store, MydbResearchStore)
        research_factory.reset()

    def test_mydb_rollback_adapter(self):
        """回滚适配：语义方法映射回引擎 mydb 读写（旧行为）。"""
        store = MydbResearchStore()
        from storage.providers import mydb_store as ms
        rd = _FakeRd()
        with mock.patch.object(ms, "_mydb_rd", return_value=rd):
            store.write_metrics("20260814", {"n": 47})
            self.assertEqual(rd.data[("打板指标:20260814", "metrics")], {"n": 47})
            self.assertEqual(store.read_metrics("20260814"), {"n": 47})


class QueryMydbResearchRouteTest(unittest.TestCase):
    """接口层 query_mydb 研究表路由（对外契约不变）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_qmydb_")
        p = mock.patch.object(config, "DATA_DIR", Path(self.tmp))
        p.start()
        self.addCleanup(p.stop)
        research_factory.reset()
        self.store = SqliteResearchStore(Path(self.tmp) / "research.db")
        self.addCleanup(self.store.close)
        research_factory._singleton = self.store

    def test_query_metrics_by_key(self):
        self.store.write_metrics("20260814", {"metrics": {"n_samples": 47}})
        from mcp import pybao_tools as pt
        out = pt.query_mydb({"table": "打板指标:20260814", "key": "metrics"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"]["source"], "research")
        self.assertEqual(out["result"]["value"]["metrics"]["n_samples"], 47)

    def test_query_snapshots_listing(self):
        self.store.write_snapshots("20260814", {"600004": {"open": 11.0}})
        from mcp import pybao_tools as pt
        out = pt.query_mydb({"table": "竞价快照:20260814"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"]["values"]["600004"]["open"], 11.0)
        self.assertEqual(out["result"]["total"], 1)

    def test_query_unknown_prefix_still_engine(self):
        """非研究表前缀仍走引擎路径（兼容）。"""
        from mcp import pybao_tools as pt
        with mock.patch.object(pt, "get_mydb_rd", return_value=None):
            out = pt.query_mydb({"table": "hk日k:00700"})
        self.assertFalse(out["ok"])  # 引擎不可用 → pybao 降级错误（非 research 路由）


if __name__ == "__main__":
    unittest.main(verbosity=2)
