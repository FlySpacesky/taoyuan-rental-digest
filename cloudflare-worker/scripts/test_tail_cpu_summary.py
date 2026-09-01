import json
from pathlib import Path
import tempfile
import unittest

from tail_cpu_summary import summarize


class TailCpuSummaryTests(unittest.TestCase):
    def test_keeps_cpu_fields_and_discards_request_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "tail.jsonl")
            path.write_text(
                json.dumps({
                    "outcome": "ok",
                    "cpuTime": 4,
                    "wallTime": 900,
                    "event": {"request": {"headers": {"authorization": "Bearer secret"}}},
                }),
                encoding="utf-8",
            )
            rows = summarize(path)
        self.assertEqual(rows, [{"outcome": "ok", "cpuTime": 4, "wallTime": 900}])
        self.assertNotIn("secret", json.dumps(rows))


if __name__ == "__main__":
    unittest.main()
