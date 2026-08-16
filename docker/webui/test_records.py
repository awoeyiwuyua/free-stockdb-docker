"""test_records — storage.records 日检记录存储单测（0.9.2 批次 7）。

离线：patch config.DATA_DIR 到临时目录；jsonl 追加/读取/上限滚动。
"""
import json
import os
import tempfile
import unittest
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

    def test_append_and_recent(self):
        records.append({"date": "20260814", "task": "close", "ok": True, "metrics": {"n_samples": 47}})
        records.append({"date": "20260813", "task": "collect", "ok": False, "reason": "x"})
        recs = records.recent(10)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["date"], "20260813")  # 最新在前
        self.assertEqual(recs[1]["metrics"]["n_samples"], 47)

    def test_recent_limit(self):
        for i in range(5):
            records.append({"date": f"2026081{i}", "task": "close", "ok": True})
        recs = records.recent(2)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["date"], "20260814")

    def test_corrupt_line_skipped(self):
        p = Path(self.tmp) / records.RECORDS_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            f.write("not-json\n")
            f.write(json.dumps({"date": "20260814", "ok": True}) + "\n")
        recs = records.recent(10)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["date"], "20260814")

    def test_trim_keeps_tail(self):
        old_max = records.MAX_LINES
        records.MAX_LINES = 5
        try:
            for i in range(8):
                records.append({"i": i})
            recs = records.recent(100)
            self.assertEqual(len(recs), 5)
            self.assertEqual(recs[0]["i"], 7)
        finally:
            records.MAX_LINES = old_max

    def test_missing_file_empty(self):
        self.assertEqual(records.recent(10), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
