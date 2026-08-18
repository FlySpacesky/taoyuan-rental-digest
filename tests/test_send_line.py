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
            "stats": {"freshness_rejected": 2},
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


if __name__ == "__main__":
    unittest.main()
