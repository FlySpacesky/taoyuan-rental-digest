import json
import unittest
from unittest.mock import MagicMock, patch

from browser_rest_probe import analyze_html, browser_content


class BrowserRestProbeTests(unittest.TestCase):
    def test_analyzes_complete_rendered_detail(self):
        result = analyze_html(
            '<h1>甲桂林 3+1 房</h1><div>更新日期 2026/08/28</div>'
            '<img src="https://yccdn.yungching.com.tw/abc.jpg">'
        )
        self.assertEqual(result["title"], "甲桂林 3+1 房")
        self.assertEqual(result["source_date"], "2026-08-28")
        self.assertEqual(result["photo_url_count"], 1)
        self.assertFalse(result["javascript_shell"])

    def test_posts_to_account_content_endpoint_without_token_in_body(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers = {"Content-Type": "text/html"}
        response.read.return_value = b"<html></html>"
        with patch("browser_rest_probe.urllib.request.urlopen", return_value=response) as urlopen:
            status, _, _ = browser_content("account-id", "secret-token")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(status, 200)
        self.assertEqual(request.full_url, "https://api.cloudflare.com/client/v4/accounts/account-id/browser-rendering/content")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertNotIn("secret-token", request.data.decode())
        self.assertEqual(payload["url"], "https://rent.yungching.com.tw/house/2415719")


if __name__ == "__main__":
    unittest.main()
