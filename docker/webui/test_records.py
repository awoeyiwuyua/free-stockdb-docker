"""test_records — storage.records 日检记录存储单测（0.9.2 引入；0.9.4 日度 Rotate）。

离线：patch config.DATA_DIR 到临时目录；按天文件追加/跨天读取/保留期清理/损坏容错。
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import config
from storage import records


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_records_")
        p = mock.patch.object(config, "DATA_DIR", Path(self.tmp))
        p.start()
        self.addCleanup(p.stop)

    def _daily(self, day):
        return Path(self.tmp) / records.RECORDS_DIR / f"{day}.jsonl"

    def test_append_and_recent(self):
        records.append({"date": "20260814", "task": "close", "ok": True, "metrics": {"n_samples": 47}})
        records.append({"date": "20260813", "task": "collect", "ok": False, "reason": "x"})
        recs = records.recent(10)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["date"], "20260814")  # 日期最新在前
        self.assertEqual(recs[0]["metrics"]["n_samples"], 47)
        # 按天分文件
        self.assertTrue(self._daily("20260814").exists())
        self.assertTrue(self._daily("20260813").exists())

    def test_append_defaults_to_today(self):
        records.append({"task": "close", "ok": True})
        today = datetime.now().strftime("%Y%m%d")
        self.assertTrue(self._daily(today).exists())

    def test_recent_limit_cross_days(self):
        today = datetime.now().strftime("%Y%m%d")
        for i in range(3):
            records.append({"date": today, "task": "close", "ok": True, "i": i})
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        for i in range(2):
            records.append({"date": yesterday, "task": "close", "ok": True, "i": i})
        recs = records.recent(4)
        self.assertEqual(len(recs), 4)
        self.assertEqual(recs[0]["i"], 2)      # 今日最新在前
        self.assertEqual(recs[3]["i"], 1)      # 昨日最新（时间序第 4 条）

    def test_corrupt_line_skipped(self):
        today = datetime.now().strftime("%Y%m%d")
        p = self._daily(today)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            f.write("not-json\n")
            f.write(json.dumps({"date": today, "ok": True}) + "\n")
        recs = records.recent(10)
        self.assertEqual(len(recs), 1)

    def test_legacy_file_compat(self):
        """0.9.2 单文件（auction_daily.jsonl）仍被 recent 读取（兼容）。"""
        p = Path(self.tmp) / records.LEGACY_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"date": "20260810", "ok": True}) + "\n")
        recs = records.recent(10)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["date"], "20260810")

    def test_cleanup_removes_expired(self):
        old_retention = records.RETENTION_DAYS
        records.RETENTION_DAYS = 2
        try:
            today = datetime.now().strftime("%Y%m%d")
            expired = (datetime.now() - timedelta(days=5)).strftime("%Y%m%d")
            records.append({"date": expired, "ok": True})
            records.append({"date": today, "ok": True})
            self.assertFalse(self._daily(expired).exists())  # 过期已清理
            self.assertTrue(self._daily(today).exists())
        finally:
            records.RETENTION_DAYS = old_retention

    def test_missing_dir_empty(self):
        self.assertEqual(records.recent(10), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
