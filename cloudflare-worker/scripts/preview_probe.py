"""Bounded public-source smoke test. Never calls the production Worker or LINE."""
import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

BASE = "https://taoyuan-rental-yungching-cpu-preview.flysky3345678.workers.dev"
SOURCE_URL = "https://rent.yungching.com.tw/house/2415719"
OUT = Path("preview-audit")


def call(path, *, token=None):
    # workers.dev rejects the generic Python-urllib signature with 1010.
    # Identify this diagnostic client; do not weaken account security settings.
    headers = {"User-Agent": "taoyuan-rental-isolated-cpu-probe/1.0", "Accept": "application/json,text/html"}
    if token:
        headers.update({"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    request = urllib.request.Request(
        BASE + path,
        data=b"{}" if token else None,
        headers=headers,
    )
    try:
        response = urllib.request.urlopen(request, timeout=35)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise RuntimeError("Preview response exceeded audit size cap")
        return response.status, {key.lower(): value for key, value in response.headers.items()}, raw.decode("utf-8", errors="replace")


def analyze_html(raw):
    # This is a smoke check, not a production listing parser or publication step.
    heading = re.search(r"<h1\b[^>]*>(.*?)</h1>", raw, re.S | re.I)
    title = html.unescape(re.sub(r"<[^>]+>", " ", heading.group(1))) if heading else ""
    plain = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    updated = re.search(r"更新日期\s*[:：]?\s*(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?", plain)
    photos = set(re.findall(r'https://yccdn\.yungching\.com\.tw/[^\s"<>]+', raw))
    age_days = None
    source_date = None
    if updated:
        source_date = dt.date(*map(int, updated.groups()))
        age_days = (dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date() - source_date).days
    return {
        "title": " ".join(title.split()),
        "source_date": str(source_date) if source_date else None,
        "source_age_days": age_days,
        "photo_url_count": len(photos),
        "recent_source_date": age_days is not None and 0 <= age_days <= 7,
    }


def wait_for_health(call_fn=call, sleep=time.sleep, expected_commit=None):
    attempts = []
    # New workers.dev routes and secret versions can take a few seconds to
    # propagate. Retry only this inert GET, never a source/browser probe.
    for delay in (0, 2, 4, 8, 12):
        if delay:
            sleep(delay)
        try:
            status, _, body = call_fn("/health")
            try:
                health = json.loads(body)
            except json.JSONDecodeError:
                health = {}
            attempts.append({"status": status, "body_excerpt": body[:400]})
            if (status == 200 and health.get("isolated") is True
                    and health.get("service") == "taoyuan-rental-yungching-cpu-preview"
                    and all(health.get(key) is False for key in ("production_handlers", "cron", "kv", "line"))
                    and (not expected_commit or health.get("commit") == expected_commit)):
                return health, attempts
        except (urllib.error.URLError, TimeoutError) as error:
            attempts.append({"error": str(error)[:400]})
    raise RuntimeError("Preview health not ready: " + json.dumps(attempts))


def wait_for_probe_ready(token, call_fn=call, sleep=time.sleep):
    statuses = []
    for delay in (0, 2, 4, 8, 12):
        if delay:
            sleep(delay)
        status, _, body = call_fn("/ready", token=token)
        statuses.append(status)
        if status == 200 and json.loads(body).get("ready") is True:
            return statuses
    raise RuntimeError("Preview credential not ready; statuses=" + json.dumps(statuses))


def call_probe(mode, token, call_fn=call, sleep=time.sleep):
    for delay in (0, 2, 4, 8, 12):
        if delay:
            sleep(delay)
        result = call_fn("/probe-" + mode, token=token)
        status, headers, _ = result
        # Routing may hit an older secret version even after /ready succeeds.
        # Only retry a definitive pre-handler auth rejection. Timeouts, 5xx,
        # browser errors, and upstream 401 must NEVER acquire a second browser.
        if status != 401 or headers.get("x-preview-probe-started") != "false":
            return result
    return result


def main():
    OUT.mkdir(exist_ok=True)
    try:
        health, health_attempts = wait_for_health(expected_commit=os.environ.get("PREVIEW_EXPECTED_COMMIT"))
        credential_attempts = wait_for_probe_ready(os.environ["PREVIEW_PROBE_TOKEN"])
    except RuntimeError as error:
        OUT.joinpath("health-error.txt").write_text(str(error), encoding="utf-8")
        raise
    for path in ("/yungching-feed", "/facebook-inbox", "/probe-fetch"):
        assert call(path)[0] == 404, path  # GET cannot trigger a source crawl
    assert call("/probe-fetch", token="unauthorized")[0] == 401

    mode = os.environ.get("PREVIEW_PROBE_MODE", "fetch")
    assert mode in ("fetch", "browser")
    status, headers, body = call_probe(mode, os.environ["PREVIEW_PROBE_TOKEN"])
    report = {
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preview": BASE,
        "mode": mode,
        "health": health,
        "health_attempts": health_attempts,
        "credential_attempts": credential_attempts,
        "status": status,
        "cf_ray": headers.get("cf-ray"),
        "source_url": SOURCE_URL,
        "production_modified": False,
        "line_sent": False,
    }
    if mode == "fetch":
        OUT.joinpath("source.html").write_text(body, encoding="utf-8")
        report.update(analyze_html(body))
        report["observed_at"] = headers.get("x-preview-observed-at")
        report["source_read_verified"] = (
            status == 200 and bool(report["title"]) and report["recent_source_date"]
            and report["photo_url_count"] > 0
            and headers.get("x-preview-source-url") == SOURCE_URL
        )
    else:
        try:
            report["response"] = json.loads(body)
        except json.JSONDecodeError:
            report["response_excerpt"] = body[:1000]
        report["source_read_verified"] = status == 200 and report.get("response", {}).get("validated_count") == 1
    OUT.joinpath("result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # A successful deployment is not a successful source validation.
    assert report["source_read_verified"], "Isolated Worker deployed, but source probe did not validate a live listing"


if __name__ == "__main__":
    main()
