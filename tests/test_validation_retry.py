from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
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
        "crawl_policy": "active-all-v1",
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
            "crawl_policy": "active-all-v1" if source == "591" else "",
            "candidate_links": candidate_count,
            "validated_before_freshness": candidate_count,
            ("active_validated" if freshness_days is None else f"fresh_within_{freshness_days}_days"): published,
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
                source: RETRY.source_freshness_days(source) * 24 if RETRY.source_freshness_days(source) is not None else None
                for source in RETRY.SOURCE_ORDER
            },
        },
        "items": items,
    }
class ValidationRetryTests(unittest.TestCase):
    def test_merge_requires_complete_yungching_when_requested(self) -> None:
        data = merge_payload(
            generated_at="2026-08-21T09:30:00+08:00",
            source_rows={"591": (1, 1), "永慶房屋": (2, 1)},
        )
        data["stats"]["sources"]["591"]["crawl_complete"] = True
        data["stats"]["sources"]["永慶房屋"]["crawl_complete"] = False
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            attempt = root / "attempt"
            latest = attempt / "rental-data" / "latest.json"
            latest.parent.mkdir(parents=True)
            latest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "永慶房屋尚未完成"):
                RETRY.merge_command(
                    [f"initial={attempt}"],
                    root / "output",
                    None,
                    "https://example.com/",
                    require_complete_591=True,
                    require_complete_yungching=True,
                )

    def test_old_two_day_policy_is_not_accepted_as_full_inventory(self) -> None:
        old = payload(validated_591=1)
        old["stats"]["sources"]["591"].pop("crawl_policy")
        with self.assertRaises(AssertionError):
            RETRY.assert_fresh_591(old)

    def test_merged_edition_preserves_new_badge_without_comparing_to_itself(self) -> None:
        now = datetime.now(timezone(timedelta(hours=8)))
        data = merge_payload(generated_at=now.isoformat(), source_rows={"591": (1, 1)})
        data["items"][0]["new_since_at"] = now.isoformat()
        data["stats"].update(candidates=1, validated=1, published=1, duplicates=0)
        with tempfile.TemporaryDirectory() as temp_dir:
            digest = RETRY.load_digest_module()
            with (
                patch.object(RETRY, "load_digest_module", return_value=digest),
                patch.object(digest, "load_previous_edition_keys", side_effect=AssertionError("must not recompare")),
            ):
                result = RETRY.write_merged_edition(data, Path(temp_dir), now.strftime("%Y-%m-%d-0930"), "https://example.com/")
            self.assertTrue(result["items"][0]["new_listing"])
            self.assertEqual(result["stats"]["comparison_source"], data["stats"]["comparison_source"])

    def test_explicit_403_requests_delayed_retry(self) -> None:
        blocked = payload(statuses={"bff": {"403": 2}, "html": {"403": 2}})
        self.assertTrue(RETRY.rate_limited_591(blocked))

    def test_fresh_591_result_does_not_retry(self) -> None:
        fresh = payload(validated_591=3, statuses={"bff": {"200": 2, "403": 1}})
        self.assertFalse(RETRY.rate_limited_591(fresh))

    def test_partial_fresh_591_result_still_retries(self) -> None:
        partial = payload(validated_591=3, statuses={"bff": {"200": 2, "403": 2}})
        RETRY.source_591(partial)["partial_refresh"] = True
        RETRY.source_591(partial)["blocked_after_queries"] = 2
        self.assertTrue(RETRY.rate_limited_591(partial))

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

    def test_source_merge_unions_partial_591_checkpoints(self) -> None:
        initial = merge_payload(
            generated_at="2026-08-21T09:30:00+08:00",
            source_rows={"591": (1, 1)},
        )
        retry = merge_payload(
            generated_at="2026-08-21T09:40:00+08:00",
            source_rows={"591": (1, 1)},
        )
        for data in (initial, retry):
            row = data["stats"]["sources"]["591"]
            row["partial_refresh"] = True
            row["blocked_after_queries"] = 2
            row["crawl_complete"] = False
        retry_item = next(item for item in retry["items"] if item["source"] == "591")
        retry_item["source_id"] = "591-retry"
        retry_item["url"] = "https://example.com/591/retry"

        merged, choices = RETRY.merge_source_payloads(
            (("initial", initial), ("retry", retry))
        )

        row = merged["stats"]["sources"]["591"]
        self.assertEqual(row["published"], 2)
        self.assertEqual(row["checkpoint_union_items"], 2)
        self.assertEqual(row["checkpoint_attempts"], ["initial", "retry"])
        self.assertEqual(choices["591"], "checkpoint-union:initial,retry")

    def test_complete_591_attempt_supersedes_partial_checkpoint(self) -> None:
        partial = merge_payload(
            generated_at="2026-08-21T09:30:00+08:00",
            source_rows={"591": (1, 1)},
        )
        complete = merge_payload(
            generated_at="2026-08-21T09:40:00+08:00",
            source_rows={"591": (1, 1)},
        )
        partial_row = partial["stats"]["sources"]["591"]
        partial_row["partial_refresh"] = True
        partial_row["crawl_complete"] = False
        complete_row = complete["stats"]["sources"]["591"]
        complete_row["crawl_complete"] = True
        complete_item = next(item for item in complete["items"] if item["source"] == "591")
        complete_item["source_id"] = "591-complete"
        complete_item["url"] = "https://example.com/591/complete"

        merged, choices = RETRY.merge_source_payloads(
            (("partial", partial), ("complete", complete))
        )

        items = [item for item in merged["items"] if item["source"] == "591"]
        self.assertEqual([item["source_id"] for item in items], ["591-complete"])
        self.assertEqual(choices["591"], "complete")
        self.assertTrue(merged["stats"]["sources"]["591"]["crawl_complete"])

    def test_resumed_complete_591_unions_exact_checkpoint_chain(self) -> None:
        initial = merge_payload(
            generated_at="2026-08-21T09:30:00+08:00",
            source_rows={"591": (1, 1)},
        )
        retry = merge_payload(
            generated_at="2026-08-21T09:40:00+08:00",
            source_rows={"591": (1, 1)},
        )
        initial_row = initial["stats"]["sources"]["591"]
        initial_row.update(
            {
                "crawl_complete": False,
                "partial_refresh": True,
                "validated_candidate_ids": ["591-0"],
            }
        )
        retry_row = retry["stats"]["sources"]["591"]
        retry_row.update(
            {
                "crawl_complete": True,
                "resumed_from_checkpoint": True,
                "checkpoint_chain": ["initial", "retry1"],
                "validated_candidate_ids": ["591-retry"],
            }
        )
        retry_item = next(item for item in retry["items"] if item["source"] == "591")
        retry_item["source_id"] = "591-retry"
        retry_item["url"] = "https://example.com/591/retry"

        merged, choices = RETRY.merge_source_payloads(
            (("initial", initial), ("retry1", retry))
        )

        row = merged["stats"]["sources"]["591"]
        items = [item for item in merged["items"] if item["source"] == "591"]
        self.assertEqual({item["source_id"] for item in items}, {"591-0", "591-retry"})
        self.assertEqual(row["candidate_links"], 2)
        self.assertEqual(row["checkpoint_attempts"], ["initial", "retry1"])
        self.assertTrue(row["crawl_complete"])
        self.assertEqual(choices["591"], "checkpoint-complete:initial,retry1")

    def test_source_only_retry_cannot_replace_other_source_diagnostics(self) -> None:
        initial = merge_payload(
            generated_at="2026-08-21T09:30:00+08:00",
            source_rows={"591": (1, 1)},
        )
        retry = merge_payload(
            generated_at="2026-08-21T09:40:00+08:00",
            source_rows={"591": (1, 1)},
        )
        for source in RETRY.SOURCE_ORDER:
            if source != "591":
                retry["stats"]["sources"][source]["validation_skipped"] = True
        initial["stats"]["sources"]["FB"]["errors"] = ["initial diagnostic"]

        merged, choices = RETRY.merge_source_payloads(
            (("initial", initial), ("retry", retry))
        )

        self.assertEqual(choices["FB"], "initial")
        self.assertEqual(
            merged["stats"]["sources"]["FB"]["errors"],
            ["initial diagnostic"],
        )

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

    def test_source_merge_keeps_live_591_item_regardless_of_source_age(self) -> None:
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
        self.assertEqual(row["published"], 1)
        self.assertEqual(row["validated"], 1)
        self.assertEqual(row["active_validated"], 1)
        self.assertIsNone(row["freshness_window_days"])

    def test_591_missing_source_date_is_valid_but_stale_validation_is_not(self) -> None:
        data = merge_payload(generated_at="2026-08-21T09:30:00+08:00", source_rows={"591": (1, 1)})
        data["items"][0]["source_timestamp"] = ""
        generated_at = RETRY.parse_timestamp(data["generated_at"])
        retained, rejected = RETRY.validated_source_items(data, "591", generated_at)
        self.assertEqual(len(retained), 1)
        self.assertFalse(rejected)
        data["items"][0]["validated_at"] = "2026-08-20T09:30:00+08:00"
        with self.assertRaisesRegex(ValueError, "stale validation"):
            RETRY.validated_source_items(data, "591", generated_at)

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
