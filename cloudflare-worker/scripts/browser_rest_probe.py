"""Probe Cloudflare Browser Rendering REST without invoking a Worker.

This diagnostic deliberately renders one fixed public Yungching detail page.
It never deploys a Worker, reads repository data, publishes an edition, or sends LINE.
"""

import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request


SOURCE_URL = "https://rent.yungching.com.tw/house/2415719"
OUT = Path("browser-rest-audit")
MAX_RESPONSE_BYTES = 6_000_000


def browser_content(account_id: str, api_token: str, source_url: str = SOURCE_URL) -> tuple[int, dict[str, str], str]:
    endpoint = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{account_id}/browser-rendering/content"
    )
    body = json.dumps(
        {
            "url": source_url,
            "gotoOptions": {"waitUntil": "networkidle2", "timeout": 30_000},
            "rejectResourceTypes": ["image", "font", "media"],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "User-Agent": "taoyuan-rental-browser-rest-probe/1.0",
        },
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=45)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("Browser Rendering response exceeded audit size cap")
        return (
            response.status,
            {key.lower(): value for key, value in response.headers.items()},
            raw.decode("utf-8", errors="replace"),
        )


def analyze_html(raw: str) -> dict[str, object]:
    heading = re.search(r"<h1\b[^>]*>(.*?)</h1>", raw, re.S | re.I)
    title = html.unescape(re.sub(r"<[^>]+>", " ", heading.group(1))) if heading else ""
    plain = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    updated = re.search(
        r"更新日期\s*[:：]?\s*(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?",
        plain,
    )
    photos = set(re.findall(r'https://yccdn\.yungching\.com\.tw/[^\s"<>]+', raw))
    source_date = dt.date(*map(int, updated.groups())) if updated else None
    return {
        "html_chars": len(raw),
        "title": " ".join(title.split()),
        "source_date": str(source_date) if source_date else None,
        "photo_url_count": len(photos),
        "javascript_shell": "JavaScript is disabled" in raw,
    }


def main() -> None:
    OUT.mkdir(exist_ok=True)
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not account_id or not api_token:
        raise RuntimeError("Cloudflare account id or API token is missing")

    status, headers, raw = browser_content(account_id, api_token)
    report = {
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "transport": "cloudflare_browser_rendering_rest",
        "status": status,
        "cf_ray": headers.get("cf-ray"),
        "source_url": SOURCE_URL,
        "worker_invoked": False,
        "production_modified": False,
        "line_sent": False,
    }
    if status == 200:
        report.update(analyze_html(raw))
        report["source_read_verified"] = bool(
            report["title"]
            and report["source_date"]
            and report["photo_url_count"]
            and not report["javascript_shell"]
        )
    else:
        report["error_excerpt"] = raw[:1000]
        report["source_read_verified"] = False

    OUT.joinpath("result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    assert report["source_read_verified"], (
        "Browser Rendering REST did not return a complete live Yungching detail page"
    )


if __name__ == "__main__":
    main()
