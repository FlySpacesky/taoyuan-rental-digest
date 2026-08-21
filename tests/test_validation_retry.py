from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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


def merge_payload(
    *,
    generated_at: str,
    source_rows: dict[str, tuple[int, int]],
) -> dict[str, object]:
    sources: dict[str, object] = {}
    items: list[dict[str, object]] = []
    for source in RETRY.SOURCE_ORDER:
        candidate_count, published = source_rows.get(source, (0, 0))
        freshness_days = RETRY.source_freshness_days(source)
        sources[source] = {
            "candidate_links": candidate_count,
            "validated_before_freshness": candidate_count,
            f"fresh_within_{freshness_days}_days": published,
            "freshness_window_days": freshness_days,
            "validated": published,
            "published": published,
            "freshness_rejected": max(candidate_count - published, 0),
        }
        for index in range(published):
            items.append(
                {
                    "source": source,
                    "source_id": f"{source}-{index}",
                    "url": f"https://example.com/{source}/{index}",
                    "validated_at": generated_at,
                    "source_timestamp": generated_at,
                    "new_listing": index == 0,
                }
            )
    return {
        "generated_at": generated_at,
        "edition_id": "2026-08-21-0930",
        "edition_url": "https://example.com/archive/2026-08-21-0930.html",
        "stats": {
            "sources": sources,
            "comparison_source": "delivery:2026-08-21-0800",
            "default_freshness_window_hours": 168,
            "freshness_window_hours_by_source": {
                source: RETRY.source_freshness_days(source) * 24
                for source in RETRY.SOURCE_ORDER
            },
        },
        "items": items,
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

    def test_source_merge_keeps_hosted_rakuya_and_stable_591(self) -> None:
        hosted = merge_payload(
            generated_at="2026-08-21T09:30:00+08:00",
            source_rows={"591": (0, 0), "樂屋網": (57, 56)},
        )
        stable = merge_payload(
            generated_at="2026-08-21T09:40:00+08:00",
            source_rows={"591": (144, 135), "樂屋網": (0, 0), "永慶房屋": (25, 1)},
        )

        merged, choices = RETRY.merge_source_payloads(
            (("hosted", hosted), ("stable", stable))
        )

        self.assertEqual(choices["591"], "stable")
        self.assertEqual(choices["樂屋網"], "hosted")
        self.assertEqual(choices["永慶房屋"], "stable")
        self.assertEqual(merged["stats"]["published"], 192)
        self.assertEqual(len(merged["items"]), 192)

    def test_source_merge_rejects_non_delivery_comparison(self) -> None:
        first = merge_payload(
            generated_at="2026-08-21T09:30:00+08:00",
            source_rows={"591": (1, 1)},
        )
        second = merge_payload(
            generated_at="2026-08-21T09:40:00+08:00",
            source_rows={"樂屋網": (1, 1)},
        )
        second["stats"]["comparison_source"] = "latest:migration"

        with self.assertRaises(ValueError):
            RETRY.merge_source_payloads((("first", first), ("second", second)))

    def test_source_merge_drops_591_item_that_crosses_final_two_day_boundary(self) -> None:
        first = merge_payload(
            generated_at="2026-08-21T09:30:00+08:00",
            source_rows={"591": (1, 1)},
        )
        first["items"][0]["source_timestamp"] = "2026-08-19T09:30:00+08:00"
        second = merge_payload(
            generated_at="2026-08-21T09:40:00+08:00",
            source_rows={"樂屋網": (1, 1)},
        )

        merged, _ = RETRY.merge_source_payloads((("first", first), ("second", second)))

        row = merged["stats"]["sources"]["591"]
        self.assertEqual(row["published"], 0)
        self.assertEqual(row["validated"], 0)
        self.assertEqual(row["rejects"]["source_older_than_2_days"], 1)

    def test_source_merge_keeps_non_591_item_inside_seven_day_boundary(self) -> None:
        first = merge_payload(
            generated_at="2026-08-21T09:30:00+08:00",
            source_rows={"樂屋網": (1, 1)},
        )
        rakuya_item = next(item for item in first["items"] if item["source"] == "樂屋網")
        rakuya_item["source_timestamp"] = "2026-08-15T09:30:00+08:00"
        second = merge_payload(
            generated_at="2026-08-21T09:40:00+08:00",
            source_rows={"591": (1, 1)},
        )

        merged, _ = RETRY.merge_source_payloads((('first', first), ('second', second)))

        row = merged["stats"]["sources"]["樂屋網"]
        self.assertEqual(row["published"], 1)
        self.assertEqual(row["freshness_window_days"], 7)

    def test_prevalidated_bundle_expires_after_two_hours(self) -> None:
        old_time = datetime.now(timezone(timedelta(hours=8))) - timedelta(hours=3)
        data = merge_payload(
            generated_at=old_time.isoformat(),
            source_rows={"591": (1, 1)},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            latest = root / "rental-data" / "latest.json"
            latest.parent.mkdir(parents=True)
            latest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                RETRY.verify_command(latest, root)


if __name__ == "__main__":
    unittest.main()
