from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "rental-digest.yml"
RETRY_WORKFLOW = ROOT / ".github" / "workflows" / "rental-591-retry.yml"


class WorkflowReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.retry_workflow = RETRY_WORKFLOW.read_text(encoding="utf-8")

    def test_production_does_not_wait_for_personal_offline_runner(self) -> None:
        self.assertIn("group: taoyuan-rental-digest-live-v2", self.workflow)
        self.assertNotIn(
            "runs-on: [self-hosted, Windows, X64, rental-validation]",
            self.workflow,
        )
        self.assertIn("VALIDATION_EGRESS: github-hosted-ubuntu", self.workflow)
        self.assertIn("591 若限制 GitHub-hosted Runner", self.workflow)

    def test_hosted_validation_keeps_social_sources_and_line_delivery(self) -> None:
        self.assertGreaterEqual(self.workflow.count("THREADS_ACCESS_TOKEN:"), 2)
        self.assertIn(
            "LINE_CHANNEL_ACCESS_TOKEN: ${{ secrets.LINE_CHANNEL_ACCESS_TOKEN }}",
            self.workflow,
        )
        self.assertIn("run: python scripts/send_line.py", self.workflow)

    def test_non_delivery_events_do_not_consume_live_source_quota(self) -> None:
        self.assertIn(
            "github.ref == 'refs/heads/main' && !inputs.publish_prevalidated",
            self.workflow,
        )
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
        self.assertIn("retry_591_validation_4:", self.workflow)
        self.assertIn("REQUESTED_DELAY:", self.retry_workflow)
        self.assertIn('sleep "${DELAY}"', self.retry_workflow)
        self.assertIn("fresh-rental-validation-initial", self.workflow)
        self.assertIn("fresh-rental-validation-retry-4", self.workflow)
        self.assertIn("select_validation_attempt.py merge", self.workflow)
        self.assertIn("--attempt initial=.validation/initial", self.workflow)
        self.assertIn("--attempt retry4=.validation/retry4", self.workflow)
        self.assertIn("--require-complete-591", self.workflow)
        self.assertIn("needs.retry_591_validation.result", self.workflow)
        self.assertIn('RENTAL_SOURCE_ONLY: "591"', self.retry_workflow)
        self.assertIn("RENTAL_591_RESUME_PATH:", self.retry_workflow)
        self.assertIn("RENTAL_591_RETRY_RUNNER || 'ubuntu-latest'", self.workflow)
        combined = self.workflow + self.retry_workflow
        self.assertGreaterEqual(combined.count("RENTAL_591_REQUEST_INTERVAL_SECONDS:"), 2)

    def test_stable_591_proxy_is_optional_and_secret_backed(self) -> None:
        combined = self.workflow + self.retry_workflow
        self.assertGreaterEqual(combined.count("RENTAL_591_PROXY_SERVER:"), 2)
        self.assertIn(
            "RENTAL_591_PROXY_SERVER: ${{ secrets.RENTAL_591_PROXY_SERVER }}",
            combined,
        )
        self.assertIn(
            "RENTAL_591_PROXY_PASSWORD: ${{ secrets.RENTAL_591_PROXY_PASSWORD }}",
            combined,
        )

    def test_prevalidated_publish_is_time_bounded_and_explicit(self) -> None:
        self.assertIn("publish_prevalidated:", self.workflow)
        self.assertIn("select_validation_attempt.py verify", self.workflow)
        self.assertIn("inputs.publish_prevalidated", self.workflow)

    def test_build_installs_dependencies_before_loading_digest_for_merge(self) -> None:
        build = self.workflow.split("  build_deploy_broadcast:", 1)[1]
        self.assertLess(
            build.index("- name: 安裝 Python 套件"),
            build.index("- name: 逐來源合併最佳本輪 fresh validation"),
        )


if __name__ == "__main__":
    unittest.main()
