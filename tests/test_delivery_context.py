import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from delivery_context import resolve_context, receipt_is_accepted


class DeliveryContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.docs = Path(self.temp.name)
        self.now = datetime.fromisoformat("2026-09-08T11:01:00+08:00")
        self.env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_RUN_ID": "123",
                    "REQUESTED_SLOT": "2026-09-08T09:30+08:00"}
        self.receipt = {"edition_id": "2026-09-08-0930", "delivery_slot": self.env["REQUESTED_SLOT"],
                        "status": "accepted", "http_status": 200, "request_id": "test-request"}

    def write(self, name, data):
        path = self.docs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def resolve(self):
        return resolve_context(self.env, self.docs, self.now)

    def test_worker_slot_not_current_manual_time(self):
        self.assertEqual(self.resolve(), {"slot": self.env["REQUESTED_SLOT"],
                                        "edition_id": "2026-09-08-0930", "mode": "fresh"})

    def test_all_native_slots_match_worker(self):
        self.env.update(GITHUB_EVENT_NAME="schedule", REQUESTED_SLOT="")
        for cron, time in [("30 9 * * *", "0930"), ("0 16 * * *", "1600"), ("0 22 * * *", "2200")]:
            self.env["EVENT_SCHEDULE"] = cron
            self.now = datetime.fromisoformat("2026-09-08T23:00:00+08:00")
            self.assertEqual(self.resolve()["edition_id"], f"2026-09-08-{time}")

    def test_after_midnight_delayed_evening_uses_previous_day(self):
        self.env.update(GITHUB_EVENT_NAME="schedule", REQUESTED_SLOT="", EVENT_SCHEDULE="0 22 * * *")
        self.now = datetime.fromisoformat("2026-09-09T00:10:00+08:00")
        self.assertEqual(self.resolve()["edition_id"], "2026-09-08-2200")

    def test_receipt_stops_both_crawling_and_sending_even_after_24h(self):
        self.write("rental-data/delivery/2026-09-08-0930.json", self.receipt)
        self.now = datetime.fromisoformat("2026-09-10T11:00:00+08:00")
        self.assertEqual(self.resolve()["mode"], "delivered")

    def test_published_edition_retries_same_body_without_crawling(self):
        self.write("rental-data/editions/2026-09-08-0930.json", {
            "edition_id": "2026-09-08-0930", "generated_at": "2026-09-08T10:00:00+08:00",
            "stats": {"sources": {"591": {"crawl_complete": True, "crawl_policy": "active-all-v1"},
                                   "永慶房屋": {"crawl_complete": True}}}})
        self.write("archive/2026-09-08-0930.html", "published HTML")
        self.assertEqual(self.resolve()["mode"], "resume")

    def test_resume_rejects_incomplete_source_or_unvalidated_snapshot(self):
        self.write("archive/2026-09-08-0930.html", "HTML")
        for change in [{"crawl_complete": False}, {"crawl_complete": True, "snapshot_used": True}]:
            self.write("rental-data/editions/2026-09-08-0930.json", {
                "edition_id": "2026-09-08-0930", "generated_at": "2026-09-08T10:00:00+08:00",
                "stats": {"sources": {"591": {"crawl_complete": True, "crawl_policy": "active-all-v1"},
                                       "永慶房屋": change}}})
            with self.assertRaises(ValueError):
                self.resolve()

    def test_manual_resume_after_retry_key_expiration_is_rejected(self):
        self.env["REQUESTED_SLOT"] = ""
        self.write("rental-data/editions/2026-09-07-1000-manual-123.json", {
            "edition_id": "2026-09-07-1000-manual-123", "generated_at": "2026-09-07T10:00:00+08:00"})
        self.write("archive/2026-09-07-1000-manual-123.html", "HTML")
        with self.assertRaisesRegex(ValueError, "24-hour"):
            self.resolve()

    def test_json_without_published_html_is_not_reused(self):
        self.write("rental-data/editions/2026-09-08-0930.json", {"edition_id": "2026-09-08-0930"})
        self.assertEqual(self.resolve()["mode"], "fresh")

    def test_invalid_receipt_fails_closed(self):
        self.receipt["delivery_slot"] = "2026-09-08T16:00+08:00"
        self.write("rental-data/delivery/2026-09-08-0930.json", self.receipt)
        with self.assertRaisesRegex(ValueError, "Invalid LINE receipt"):
            self.resolve()

    def test_unknown_or_expired_slot_is_not_blindly_resent(self):
        for slot in ["2026-09-08T10:00+08:00", "2026-09-07T09:30+08:00", "../bad", "2026-09-08T22:00+08:00"]:
            self.env["REQUESTED_SLOT"] = slot
            with self.assertRaises(ValueError):
                self.resolve()

    def test_manual_rerun_keeps_committed_edition(self):
        self.env["REQUESTED_SLOT"] = ""
        self.write("rental-data/editions/2026-09-08-1000-manual-123.json", {})
        self.assertEqual(self.resolve()["edition_id"], "2026-09-08-1000-manual-123")

    def test_web_only_does_not_suppress_refresh(self):
        self.write("rental-data/delivery/2026-09-08-0930.json", self.receipt)
        self.env.update(SKIP_LINE="true", REQUESTED_SLOT="")
        self.assertEqual(self.resolve()["mode"], "fresh")

    def test_prevalidated_wrong_slot_is_rejected(self):
        self.env["PUBLISH_PREVALIDATED"] = "true"
        self.write("rental-data/latest.json", {"edition_id": "2026-09-08-1600"})
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.resolve()

    def test_legacy_recovery_preserves_actual_sent_link(self):
        self.receipt.update(edition_id="2026-09-08-1100-manual-123", slot_edition_id="2026-09-08-0930",
                            github_run_id="123", edition_url="https://flyspacesky.github.io/taoyuan-rental-digest/archive/2026-09-08-1100-manual-123.html")
        self.assertTrue(receipt_is_accepted(self.receipt, self.env["REQUESTED_SLOT"], "2026-09-08-0930"))
        self.receipt["github_run_id"] = "999"
        self.assertFalse(receipt_is_accepted(self.receipt, self.env["REQUESTED_SLOT"], "2026-09-08-0930"))

    def test_success_requires_matching_http_status_and_request_id(self):
        for change in [{"request_id": ""}, {"http_status": 409}, {"status": "failed"}]:
            self.assertFalse(receipt_is_accepted(self.receipt | change, self.env["REQUESTED_SLOT"], "2026-09-08-0930"))


if __name__ == "__main__":
    unittest.main()
