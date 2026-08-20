from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "rental-digest.yml"


class WorkflowReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_production_does_not_wait_for_personal_offline_runner(self) -> None:
        self.assertIn("group: taoyuan-rental-digest-live-v2", self.workflow)
        self.assertNotIn(
            "runs-on: [self-hosted, Windows, X64, rental-validation]",
            self.workflow,
        )
        self.assertIn("VALIDATION_EGRESS: github-hosted-ubuntu", self.workflow)
        self.assertIn("591 若封鎖 GitHub-hosted Runner", self.workflow)

    def test_hosted_validation_keeps_social_sources_and_line_delivery(self) -> None:
        self.assertGreaterEqual(self.workflow.count("THREADS_ACCESS_TOKEN:"), 2)
        self.assertIn(
            "LINE_CHANNEL_ACCESS_TOKEN: ${{ secrets.LINE_CHANNEL_ACCESS_TOKEN }}",
            self.workflow,
        )
        self.assertIn("run: python scripts/send_line.py", self.workflow)

    def test_non_delivery_events_do_not_consume_live_source_quota(self) -> None:
        delivery_condition = (
            "github.event_name == 'schedule' || "
            "(github.event_name == 'workflow_dispatch' && "
            "github.ref == 'refs/heads/main')"
        )
        self.assertGreaterEqual(self.workflow.count(delivery_condition), 2)
        self.assertNotIn(
            "github.event_name != 'workflow_dispatch' || "
            "github.ref == 'refs/heads/main'",
            self.workflow,
        )
        self.assertIn(
            "- name: 抓取、驗證並產生分支預覽\n"
            "        if: ${{ github.event_name == 'workflow_dispatch' }}",
            self.workflow,
        )

    def test_rate_limit_uses_delayed_fresh_runner_retry(self) -> None:
        self.assertIn("retry_591_validation:", self.workflow)
        self.assertIn("REQUESTED_DELAY:", self.workflow)
        self.assertIn('sleep "${DELAY}"', self.workflow)
        self.assertIn("fresh-rental-validation-initial", self.workflow)
        self.assertIn("fresh-rental-validation-retry", self.workflow)
        self.assertIn("select_validation_attempt.py select", self.workflow)
        self.assertIn("needs.retry_591_validation.result", self.workflow)

    def test_stable_591_proxy_is_optional_and_secret_backed(self) -> None:
        self.assertGreaterEqual(self.workflow.count("RENTAL_591_PROXY_SERVER:"), 2)
        self.assertIn(
            "RENTAL_591_PROXY_SERVER: ${{ secrets.RENTAL_591_PROXY_SERVER }}",
            self.workflow,
        )
        self.assertIn(
            "RENTAL_591_PROXY_PASSWORD: ${{ secrets.RENTAL_591_PROXY_PASSWORD }}",
            self.workflow,
        )


if __name__ == "__main__":
    unittest.main()
