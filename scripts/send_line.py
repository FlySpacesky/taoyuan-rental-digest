#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "docs" / "rental-data"
LATEST = DATA_DIR / "latest.json"
ARCHIVE_DIR = ROOT / "docs" / "archive"
DELIVERY_DIR = DATA_DIR / "delivery"
LAST_DELIVERY_FILE = DATA_DIR / "last-delivery.json"


def delivery_retry_key(delivery_slot: str, generated_at: str) -> str:
    """同一投遞時段永遠產生相同 Retry Key，避免備援重複廣播。"""
    source = delivery_slot.strip() or generated_at.strip() or "rental-digest"
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"taoyuan-rental-digest:line-broadcast:{source}",
        )
    )


def listing_key(row: dict[str, Any]) -> str:
    source = str(row.get("source", "")).strip()
    source_id = str(row.get("source_id", "")).strip()
    prefix = {
        "591": "591",
        "FB": "fb",
        "樂屋網": "rakuya",
        "Threads": "threads",
        "信義房屋": "sinyi",
        "永慶房屋": "yungching",
    }.get(source, source.lower())
    return f"{prefix}:{source_id}" if prefix and source_id else ""


def validate_edition_payload(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    edition_id = str(payload.get("edition_id", "")).strip()
    edition_url = str(payload.get("edition_url", "")).strip()
    items = payload.get("items", [])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{4}(?:-manual-\d+)?", edition_id):
        raise ValueError(f"缺少有效的日期時段快報版本：{edition_id}")
    parsed = urllib.parse.urlparse(edition_url)
    expected_suffix = f"/archive/{edition_id}.html"
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.path.endswith(expected_suffix)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "LINE 必須使用含日期與時段的永久快報網址："
            f"{edition_url}"
        )
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError("租屋快報 items 格式不正確。")
    archive_file = ARCHIVE_DIR / f"{edition_id}.html"
    if not archive_file.exists():
        raise ValueError(f"找不到本版永久快報：{archive_file}")
    return edition_id, edition_url, items


def write_delivery_receipt(
    *,
    payload: dict[str, Any],
    edition_id: str,
    edition_url: str,
    delivery_slot: str,
    retry_key: str,
    status: str,
    http_status: int,
    request_id: str,
    accepted_request_id: str,
) -> Path:
    DELIVERY_DIR.mkdir(parents=True, exist_ok=True)
    item_keys = sorted(
        {
            key
            for row in payload.get("items", [])
            if isinstance(row, dict)
            if (key := listing_key(row))
        }
    )
    receipt = {
        "edition_id": edition_id,
        "edition_url": edition_url,
        "generated_at": str(payload.get("generated_at", "")),
        "accepted_at": datetime.now(TZ).isoformat(),
        "status": status,
        "http_status": http_status,
        "request_id": request_id,
        "accepted_request_id": accepted_request_id,
        "delivery_slot": delivery_slot,
        "retry_key": retry_key,
        "item_count": len(item_keys),
        "item_keys": item_keys,
        "github_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
    }
    receipt_file = DELIVERY_DIR / f"{edition_id}.json"
    temporary = receipt_file.with_suffix(".json.tmp")
    text = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(receipt_file)
    last_temporary = LAST_DELIVERY_FILE.with_suffix(".json.tmp")
    last_temporary.write_text(text, encoding="utf-8")
    last_temporary.replace(LAST_DELIVERY_FILE)
    return receipt_file


def main() -> int:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    delivery_slot = os.environ.get("LINE_DELIVERY_SLOT", "").strip()

    if not token:
        print("LINE_CHANNEL_ACCESS_TOKEN 未設定，無法發送LINE。", file=sys.stderr)
        return 2
    if not delivery_slot:
        print("LINE_DELIVERY_SLOT 未設定，無法安全發送LINE。", file=sys.stderr)
        return 2
    try:
        payload = json.loads(LATEST.read_text(encoding="utf-8"))
        edition_id, edition_url, items = validate_edition_payload(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"無法讀取可發送的永久快報：{exc}", file=sys.stderr)
        return 2

    stats = payload.get("stats", {})
    counts: dict[str, int] = {}
    for item in items:
        key = f"{item.get('source', '其他')}／{item.get('category', '一般')}"
        counts[key] = counts.get(key, 0) + 1
    summary = (
        "\n".join(f"• {name}：{count}筆" for name, count in sorted(counts.items()))
        if counts
        else "• 本次沒有符合物件"
    )
    new_count = sum(bool(item.get("new_listing")) for item in items)
    text = (
        "🏠 桃園四房以上租屋快報\n\n"
        f"本版符合：{len(items)}筆\n"
        f"新房源：{new_count}筆\n"
        f"超過14天／無來源時間排除：{stats.get('freshness_rejected', 0)}筆\n\n"
        f"{summary}\n\n"
        f"查看本版永久快報、照片與物件直達連結：\n{edition_url}"
    )
    body = {"messages": [{"type": "text", "text": text[:5000]}]}
    headers = {
        "Authorization": f"Bearer {token}",
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
    retry_key = delivery_retry_key(delivery_slot, generated_at)
    headers["X-Line-Retry-Key"] = retry_key
    response = requests.post(
        "https://api.line.me/v2/bot/message/broadcast",
        headers=headers,
        json=body,
        timeout=20,
    )
    request_id = response.headers.get("x-line-request-id", "")
    accepted_request_id = response.headers.get("x-line-accepted-request-id", "")
    if response.status_code == 409:
        status = "already_accepted"
    elif response.status_code >= 300:
        print(f"LINE廣播失敗 {response.status_code}: {response.text}", file=sys.stderr)
        return 1
    else:
        status = "accepted"

    receipt_path = write_delivery_receipt(
        payload=payload,
        edition_id=edition_id,
        edition_url=edition_url,
        delivery_slot=delivery_slot,
        retry_key=retry_key,
        status=status,
        http_status=response.status_code,
        request_id=request_id,
        accepted_request_id=accepted_request_id,
    )
    if status == "already_accepted":
        print(
            "LINE此投遞時段先前已成功接受；本次安全略過重複廣播。"
            f" slot={delivery_slot} accepted_request_id={accepted_request_id}"
        )
    else:
        print(
            f"LINE廣播成功：{len(items)}筆，永久快報 {edition_url}，"
            f"投遞時段 {delivery_slot} request_id={request_id}"
        )
    print(f"LINE投遞紀錄：{receipt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
