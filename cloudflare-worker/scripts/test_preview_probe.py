import json
import unittest

from preview_probe import wait_for_health


class PreviewHealthTests(unittest.TestCase):
    def health(self, **overrides):
        return dict(status="ok", service="taoyuan-rental-yungching-cpu-preview",
                    isolated=True, production_handlers=False, cron=False,
                    kv=False, line=False, commit="expected", **overrides)

    def test_retries_only_health_during_route_propagation(self):
        responses = iter([(404, {}, "route pending"), (200, {}, json.dumps(self.health()))])
        paths, sleeps = [], []

        def call(path):
            paths.append(path)
            return next(responses)

        result, attempts = wait_for_health(call, sleeps.append, "expected")
        self.assertTrue(result["isolated"])
        self.assertEqual(paths, ["/health", "/health"])
        self.assertEqual(sleeps, [2])
        self.assertEqual(len(attempts), 2)

    def test_rejects_stale_commit_and_bound_retries(self):
        sleeps = []
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            wait_for_health(lambda _: (200, {}, json.dumps(self.health())), sleeps.append, "new")
        self.assertEqual(sleeps, [2, 4, 8, 12])

    def test_rejects_production_capabilities(self):
        health = self.health()
        health["line"] = True
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            wait_for_health(lambda _: (200, {}, json.dumps(health)), lambda _: None)


if __name__ == "__main__":
    unittest.main()
