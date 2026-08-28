import json
import unittest
from unittest.mock import MagicMock, patch

from preview_probe import call, wait_for_health, wait_for_probe_ready


class PreviewHealthTests(unittest.TestCase):
    def test_waits_for_rotated_secret_without_source_requests(self):
        responses = iter([(401, {}, "Unauthorized"), (200, {}, '{"ready":true}')])
        paths = []

        def call_fn(path, *, token):
            paths.append(path)
            self.assertEqual(token, "test-token")
            return next(responses)

        self.assertEqual(wait_for_probe_ready("test-token", call_fn, lambda _: None), [401, 200])
        self.assertEqual(paths, ["/ready", "/ready"])

    def test_identifies_client_and_only_authenticates_post(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status, response.headers = 200, {}
        response.read.return_value = b"{}"
        with patch("preview_probe.urllib.request.urlopen", return_value=response) as urlopen:
            call("/health")
            request = urlopen.call_args.args[0]
            self.assertEqual(request.get_header("User-agent"), "taoyuan-rental-isolated-cpu-probe/1.0")
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.get_header("Authorization"))
            call("/probe-fetch", token="test-token")
            request = urlopen.call_args.args[0]
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-token")

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
