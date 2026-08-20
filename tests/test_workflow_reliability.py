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


if __name__ == "__main__":
    unittest.main()
