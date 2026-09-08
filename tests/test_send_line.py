from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "send_line",
    ROOT / "scripts" / "send_line.py",
)
assert SPEC and SPEC.loader
SEND_LINE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SEND_LINE
SPEC.loader.exec_module(SEND_LINE)


class LineDeliveryTests(unittest.TestCase):
    def test_same_delivery_slot_has_stable_retry_key(self) -> None:
        first = SEND_LINE.delivery_retry_key(
            "2026-08-09T09:30+08:00",
            "2026-08-09T09:31:00+08:00",
        )
        second = SEND_LINE.delivery_retry_key(
            "2026-08-09T09:30+08:00",
            "2026-08-09T09:40:00+08:00",
        )
        self.assertEqual(first, second)

    def test_transient_line_failures_retry_with_the_same_request_headers(self) -> None:
        limited = Mock(status_code=429, headers={"Retry-After": "1"})
        unavailable = Mock(status_code=503, headers={})
        accepted = Mock(status_code=200, headers={})
        headers = {
            "Authorization": "Bearer test-token",
            "X-Line-Retry-Key": "stable-key",
        }
        with (
            patch.object(
                SEND_LINE.requests,
                "post",
                side_effect=[limited, unavailable, accepted],
            ) as post,
            patch.object(SEND_LINE.time, "sleep") as sleep,
        ):
            result = SEND_LINE.post_line_json(
                "https://api.line.me/v2/bot/message/broadcast",
                headers=headers,
                body={"messages": []},
                operation="廣播",
            )

        self.assertIs(result, accepted)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_args_list[0].args, (1.0,))
        self.assertEqual(sleep.call_args_list[1].args, (10.0,))
        for call in post.call_args_list:
            self.assertEqual(call.kwargs["headers"]["X-Line-Retry-Key"], "stable-key")

    def test_network_timeout_can_resolve_as_already_accepted(self) -> None:
        conflict = Mock(status_code=409, headers={})
        with (
            patch.object(
                SEND_LINE.requests,
                "post",
                side_effect=[SEND_LINE.requests.Timeout("timeout"), conflict],
            ) as post,
            patch.object(SEND_LINE.time, "sleep") as sleep,
        ):
            result = SEND_LINE.post_line_json(
                "https://api.line.me/v2/bot/message/broadcast",
                headers={"X-Line-Retry-Key": "stable-key"},
                body={"messages": []},
                operation="廣播",
            )

        self.assertEqual(result.status_code, 409)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(5.0)

    def test_line_conflict_is_safe_success_and_saves_previous_edition(self) -> None:
        edition_id = "2026-08-09-0930"
        edition_url = (
            "https://example.test/archive/2026-08-09-0930.html"
        )
        payload = {
            "generated_at": "2026-08-09T09:31:00+08:00",
            "edition_id": edition_id,
            "edition_url": edition_url,
            "items": [
                {
                    "source": "591",
                    "source_id": "123",
                    "category": "owner",
                    "new_listing": True,
                }
            ],
            "stats": {
                "current_inventory": 1,
                "freshness_rejected": 2,
                "source_time_filter_enabled": True,
            },
        }
        validate_response = Mock(status_code=200, text="", headers={})
        conflict_response = Mock(
            status_code=409,
            text="retry key already accepted",
            headers={"x-line-accepted-request-id": "accepted-123"},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            latest = root / "latest.json"
            archive_dir = root / "archive"
            delivery_dir = root / "delivery"
            last_delivery = root / "last-delivery.json"
            archive_dir.mkdir()
            (archive_dir / f"{edition_id}.html").write_text(
                "fixed edition",
                encoding="utf-8",
            )
            latest.write_text(json.dumps(payload), encoding="utf-8")
            with (
                patch.object(SEND_LINE, "LATEST", latest),
                patch.object(SEND_LINE, "ARCHIVE_DIR", archive_dir),
                patch.object(SEND_LINE, "DELIVERY_DIR", delivery_dir),
                patch.object(SEND_LINE, "LAST_DELIVERY_FILE", last_delivery),
                patch.dict(
                    os.environ,
                    {
                        "LINE_CHANNEL_ACCESS_TOKEN": "test-token",
                        "LINE_DELIVERY_SLOT": "2026-08-09T09:30+08:00",
                    },
                    clear=False,
                ),
                patch.object(
                    SEND_LINE.requests,
                    "post",
                    side_effect=[validate_response, conflict_response],
                ) as post,
            ):
                result = SEND_LINE.main()

            receipt = json.loads(last_delivery.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(receipt["edition_id"], edition_id)
        self.assertEqual(receipt["edition_url"], edition_url)
        self.assertEqual(receipt["item_keys"], ["591:123"])
        self.assertEqual(receipt["status"], "already_accepted")
        broadcast_headers = post.call_args_list[1].kwargs["headers"]
        self.assertEqual(
            broadcast_headers["X-Line-Retry-Key"],
            SEND_LINE.delivery_retry_key(
                "2026-08-09T09:30+08:00",
                payload["generated_at"],
            ),
        )
        message = post.call_args_list[0].kwargs["json"]["messages"][0]["text"]
        self.assertIn(edition_url, message)
        self.assertIn("新房源：1筆", message)

    def test_mutable_homepage_is_rejected(self) -> None:
        payload = {
            "edition_id": "2026-08-09-0930",
            "edition_url": "https://example.test/",
            "items": [],
        }
        with self.assertRaisesRegex(ValueError, "永久快報網址"):
            SEND_LINE.validate_edition_payload(payload)

    def test_mismatched_worker_manual_edition_never_calls_line(self):
        with (
            patch.dict(os.environ, {"LINE_CHANNEL_ACCESS_TOKEN": "test-token", "LINE_DELIVERY_SLOT": "2026-09-08T09:30+08:00", "LINE_EDITION_ID": ""}),
            patch.object(Path, "read_text", return_value='{}'),
            patch.object(SEND_LINE, "validate_edition_payload", return_value=("2026-09-08-1100-manual-123", "https://example.test/archive/test.html", [])),
            patch.object(SEND_LINE, "load_receipt", return_value=None),
            patch.object(SEND_LINE.requests, "post") as post,
        ):
            self.assertEqual(SEND_LINE.main(), 2)
            post.assert_not_called()

    def test_saved_receipt_skips_line_without_even_validating_message(self):
        with (
            patch.dict(os.environ, {"LINE_CHANNEL_ACCESS_TOKEN": "test-token", "LINE_DELIVERY_SLOT": "2026-09-08T09:30+08:00", "LINE_EDITION_ID": ""}),
            patch.object(Path, "read_text", return_value='{}'),
            patch.object(SEND_LINE, "validate_edition_payload", return_value=("2026-09-08-0930", "https://example.test/archive/test.html", [])),
            patch.object(SEND_LINE, "load_receipt", return_value={"status": "accepted"}),
            patch.object(SEND_LINE, "report_receipt") as report,
            patch.object(SEND_LINE.requests, "post") as post,
        ):
            self.assertEqual(SEND_LINE.main(), 0)
            post.assert_not_called()
            self.assertEqual(report.call_args.args[0].name, "2026-09-08-0930.json")

    def test_401_is_not_retried(self):
        with (patch.object(SEND_LINE.requests, "post", return_value=Mock(status_code=401)) as post,
              patch.object(SEND_LINE.time, "sleep") as sleep):
            self.assertEqual(SEND_LINE.post_line_json("test", headers={}, body={}, operation="test").status_code, 401)
            post.assert_called_once()
            sleep.assert_not_called()

    def test_resume_reads_fixed_edition_instead_of_mutable_latest(self):
        edition = "2026-09-08-0930"
        with (
            patch.dict(os.environ, {"LINE_CHANNEL_ACCESS_TOKEN": "test-token", "LINE_DELIVERY_SLOT": "2026-09-08T09:30+08:00", "LINE_EDITION_ID": edition}),
            patch.object(Path, "read_text", autospec=True, return_value='{}') as read,
            patch.object(SEND_LINE, "validate_edition_payload", return_value=(edition, f"https://example.test/archive/{edition}.html", [])),
            patch.object(SEND_LINE, "load_receipt", return_value=None),
            patch.object(SEND_LINE, "write_delivery_receipt", return_value=Path(f"{edition}.json")),
            patch.object(SEND_LINE, "report_receipt"),
            patch.object(SEND_LINE.requests, "post", side_effect=[Mock(status_code=200), Mock(status_code=200, headers={"x-line-request-id": "test-request"})]) as post,
        ):
            self.assertEqual(SEND_LINE.main(), 0)
            self.assertEqual(read.call_args.args[0], SEND_LINE.DATA_DIR / "editions" / f"{edition}.json")
            self.assertEqual(post.call_count, 2)

    def test_409_without_accepted_request_id_is_not_a_receipt(self):
        with (
            patch.dict(os.environ, {"LINE_CHANNEL_ACCESS_TOKEN": "test-token", "LINE_DELIVERY_SLOT": "2026-09-08T09:30+08:00", "LINE_EDITION_ID": ""}),
            patch.object(Path, "read_text", return_value='{}'),
            patch.object(SEND_LINE, "validate_edition_payload", return_value=("2026-09-08-0930", "https://example.test/archive/test.html", [])),
            patch.object(SEND_LINE, "load_receipt", return_value=None),
            patch.object(SEND_LINE, "write_delivery_receipt") as write,
            patch.object(SEND_LINE.requests, "post", side_effect=[Mock(status_code=200), Mock(status_code=409, headers={}, text="conflict")]),
        ):
            self.assertEqual(SEND_LINE.main(), 1)
            write.assert_not_called()

    def test_persistent_503_exhausts_bounded_retry(self):
        with (patch.object(SEND_LINE.requests, "post", return_value=Mock(status_code=503, headers={})) as post,
              patch.object(SEND_LINE.time, "sleep") as sleep):
            self.assertEqual(SEND_LINE.post_line_json("test", headers={}, body={}, operation="test").status_code, 503)
            self.assertEqual(post.call_count, 4)
            self.assertEqual(sleep.call_count, 3)


if __name__ == "__main__":
    unittest.main()
