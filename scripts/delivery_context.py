"""Resolve one immutable edition/LINE slot before crawling, using no secrets."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
SCHEDULES = {"30 9 * * *": "09:30", "0 16 * * *": "16:00", "0 22 * * *": "22:00"}
EDITION_PATTERN = r"\d{4}-\d{2}-\d{2}-\d{4}(?:-manual-\d+)?"


def slot_edition_id(slot: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T(?:09:30|16:00|22:00)\+08:00", slot):
        raise ValueError("Invalid scheduled LINE delivery slot")
    return datetime.fromisoformat(slot).strftime("%Y-%m-%d-%H%M")


def receipt_id(slot: str, edition_id: str) -> str:
    if re.fullmatch(r"manual:\d+", slot):
        if not re.fullmatch(EDITION_PATTERN, edition_id):
            raise ValueError("Invalid manual edition")
        return edition_id
    return slot_edition_id(slot)


def receipt_is_accepted(receipt: dict, slot: str, edition_id: str) -> bool:
    canonical = receipt_id(slot, edition_id)
    actual = str(receipt.get("edition_id", ""))
    # Legacy recovery preserves the actual link sent, rather than inventing a new edition.
    legacy = (
        receipt.get("slot_edition_id") == canonical
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{4}-manual-\d+", actual)
        and actual[:10] == canonical[:10]
        and actual.rsplit("-", 1)[-1] == receipt.get("github_run_id")
        and receipt.get("edition_url") ==
        f"https://flyspacesky.github.io/taoyuan-rental-digest/archive/{actual}.html"
    )
    accepted = (
        receipt.get("status") == "accepted" and receipt.get("http_status") == 200
        and bool(receipt.get("request_id"))
    ) or (
        receipt.get("status") == "already_accepted" and receipt.get("http_status") == 409
        and bool(receipt.get("accepted_request_id"))
    )
    return bool(receipt.get("delivery_slot") == slot and (actual == canonical or legacy) and accepted)


def load_receipt(directory: Path, slot: str, edition_id: str) -> dict | None:
    path = directory / f"{receipt_id(slot, edition_id)}.json"
    if not path.exists():
        return None
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict) or not receipt_is_accepted(receipt, slot, edition_id):
        # An invalid receipt is an operational error, never permission to resend blindly.
        raise ValueError(f"Invalid LINE receipt: {path.name}")
    return receipt


def resolve_context(env: dict, docs: Path, now: datetime) -> dict[str, str]:
    now = now.astimezone(TZ)
    requested = env.get("REQUESTED_SLOT", "").strip()
    skip_line = env.get("SKIP_LINE") == "true"
    prevalidated = env.get("PUBLISH_PREVALIDATED") == "true"
    if skip_line and requested:
        raise ValueError("Web-only maintenance must leave delivery_slot empty to preserve sent editions")
    if requested:
        edition = slot_edition_id(requested)
        slot = requested
    elif env.get("GITHUB_EVENT_NAME") == "schedule":
        clock = SCHEDULES[env.get("EVENT_SCHEDULE", "")]
        scheduled = datetime.fromisoformat(f"{now.date()}T{clock}+08:00")
        if scheduled > now:
            scheduled -= timedelta(days=1)
        slot = scheduled.isoformat(timespec="minutes")
        edition = slot_edition_id(slot)
    else:
        run_id = env.get("GITHUB_RUN_ID", "")
        if not run_id.isdigit():
            raise ValueError("Missing valid GitHub run ID")
        slot = f"manual:{run_id}"
        # GitHub reruns retain run ID but may start at a different minute.
        existing = sorted((docs / "rental-data" / "editions").glob(f"*-manual-{run_id}.json"))
        edition = existing[0].stem if existing else f"{now:%Y-%m-%d-%H%M}-manual-{run_id}"
    if prevalidated:
        payload = json.loads((docs / "rental-data" / "latest.json").read_text(encoding="utf-8"))
        if requested and payload["edition_id"] != edition:
            raise ValueError("Prevalidated edition does not match requested delivery slot")
        edition = payload["edition_id"]
    if not re.fullmatch(EDITION_PATTERN, edition):
        raise ValueError("Invalid edition ID")
    mode = "fresh"
    if not skip_line and load_receipt(docs / "rental-data" / "delivery", slot, edition):
        mode = "delivered"
    else:
        if not skip_line and not slot.startswith("manual:"):
            age = now - datetime.fromisoformat(slot)
            if not timedelta(0) <= age < timedelta(hours=24):
                raise ValueError("Unconfirmed LINE slot is outside safe 24-hour retry window")
        if not skip_line and not prevalidated:
            payload_path = docs / "rental-data" / "editions" / f"{edition}.json"
            archive = docs / "archive" / f"{edition}.html"
            if payload_path.exists() and archive.exists():
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
                if payload.get("edition_id") != edition:
                    raise ValueError("Published edition ID mismatch")
                generated = datetime.fromisoformat(payload["generated_at"])
                if generated.tzinfo is None or not timedelta(0) <= now - generated < timedelta(hours=24):
                    raise ValueError("Published edition is outside safe 24-hour retry window")
                from select_validation_attempt import assert_fresh_591
                assert_fresh_591(payload)
                sources = payload["stats"]["sources"]
                if any(sources.get(source, {}).get("crawl_complete") is not True for source in ("591", "永慶房屋")):
                    raise ValueError("Published edition has incomplete source validation")
                if any(row.get("snapshot_used") or row.get("fallback") for row in sources.values()):
                    raise ValueError("Published edition contains unvalidated fallback")
                mode = "resume"
    return {"slot": slot, "edition_id": edition, "mode": mode}


def main() -> None:
    context = resolve_context(os.environ, Path("docs"), datetime.now(TZ))
    for name, value in context.items():
        print(f"{name}={value}")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for name, value in context.items():
            output.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()
