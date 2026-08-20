from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "select_validation_attempt",
    ROOT / "scripts" / "select_validation_attempt.py",
)
assert SPEC and SPEC.loader
RETRY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RETRY
SPEC.loader.exec_module(RETRY)


def payload(
    *,
    validated_591: int = 0,
    total_validated: int = 0,
    statuses: dict[str, dict[str, int]] | None = None,
    snapshot_used: bool = False,
    generated_at: str = "2026-08-21T09:30:00+08:00",
) -> dict[str, object]:
    row: dict[str, object] = {
        "validated": validated_591,
        "published": validated_591,
        "http_statuses": statuses or {},
    }
    if snapshot_used:
        row["snapshot_used"] = True
    return {
        "generated_at": generated_at,
        "stats": {
            "validated": total_validated,
            "sources": {"591": row},
        },
        "items": [],
    }


class ValidationRetryTests(unittest.TestCase):
    def test_explicit_403_requests_delayed_retry(self) -> None:
        blocked = payload(statuses={"bff": {"403": 2}, "html": {"403": 2}})
        self.assertTrue(RETRY.rate_limited_591(blocked))

    def test_fresh_591_result_does_not_retry(self) -> None:
        fresh = payload(validated_591=3, statuses={"bff": {"200": 2, "403": 1}})
        self.assertFalse(RETRY.rate_limited_591(fresh))

    def test_selector_prefers_retry_with_fresh_591_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            initial = root / "initial"
            retry = root / "retry"
            for folder, data in (
                (initial, payload(total_validated=20)),
                (retry, payload(validated_591=4, total_validated=4)),
            ):
                latest = folder / "rental-data" / "latest.json"
                latest.parent.mkdir(parents=True)
                latest.write_text(json.dumps(data), encoding="utf-8")

            selected, selected_payload = RETRY.select_attempt((initial, retry))

        self.assertEqual(selected, retry)
        self.assertEqual(RETRY.source_591(selected_payload)["validated"], 4)

    def test_selector_rejects_old_snapshot(self) -> None:
        with self.assertRaises(AssertionError):
            RETRY.validation_score(payload(snapshot_used=True))


if __name__ == "__main__":
    unittest.main()
