#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / "docs" / "rental-data" / "latest.json"
TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
SITE_URL = os.environ.get("SITE_URL", "").strip()


def main() -> int:
    if not TOKEN:
        print("LINE_CHANNEL_ACCESS_TOKEN 未設定，略過LINE發送。")
        return 0
    if not SITE_URL:
        print("SITE_URL 未設定，無法發送LINE。", file=sys.stderr)
        return 1
    try:
        payload = json.loads(LATEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"無法讀取 {LATEST}: {exc}", file=sys.stderr)
        return 1

    items = payload.get("items", [])
    stats = payload.get("stats", {})

    counts: dict[str, int] = {}
    for item in items:
        key = f"{item.get('source', '其他')}／{item.get('category', '一般')}"
        counts[key] = counts.get(key, 0) + 1

    summary = (
        "\n".join(f"• {name}：{count}筆" for name, count in sorted(counts.items()))
        if counts
        else "• 本次沒有新的符合物件"
    )
    text = (
        "🏠 桃園四房以上租屋快報\n\n"
        f"本次新增：{len(items)}筆\n"
        f"48小時重複排除：{stats.get('duplicates', 0)}筆\n\n"
        f"{summary}\n\n"
        f"查看最新快報、照片與物件直達連結：\n{SITE_URL}"
    )
    body = {"messages": [{"type": "text", "text": text[:5000]}]}
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }
    validate = requests.post(
        "https://api.line.me/v2/bot/message/validate/broadcast",
        headers=headers,
        json=body,
        timeout=20,
    )
    if validate.status_code >= 300:
        print(f"LINE驗證失敗 {validate.status_code}: {validate.text}", file=sys.stderr)
        return 1

    generated_at = str(payload.get("generated_at", "rental-digest"))
    headers["X-Line-Retry-Key"] = str(uuid.uuid5(uuid.NAMESPACE_URL, generated_at))
    response = requests.post(
        "https://api.line.me/v2/bot/message/broadcast",
        headers=headers,
        json=body,
        timeout=20,
    )
    if response.status_code >= 300:
        print(f"LINE廣播失敗 {response.status_code}: {response.text}", file=sys.stderr)
        return 1
    print(f"LINE廣播成功：{len(items)}筆，網站 {SITE_URL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
