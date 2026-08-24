#!/usr/bin/env python3
"""Inspect and select fresh rental-validation artifacts for GitHub Actions."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import shutil
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


SOURCE_ORDER = ("591", "FB", "樂屋網", "Threads", "信義房屋", "永慶房屋")
MAX_ATTEMPT_SPAN = timedelta(hours=1)
MAX_PREVALIDATED_AGE = timedelta(hours=2)
SOURCE_FRESHNESS_DAYS = 7
SOURCE_FRESHNESS_DAYS_BY_SOURCE = {"591": 2}


def source_freshness_days(source: str) -> int:
    return SOURCE_FRESHNESS_DAYS_BY_SOURCE.get(source, SOURCE_FRESHNESS_DAYS)


def source_freshness_window(source: str) -> timedelta:
    return timedelta(days=source_freshness_days(source))


def load_payload(root: Path) -> dict[str, Any]:
    latest = root / "rental-data" / "latest.json"
    if not latest.is_file():
        raise FileNotFoundError(f"missing validation payload: {latest}")
    payload = json.loads(latest.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid validation payload: {latest}")
    return payload


def source_591(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["stats"]["sources"]["591"]


def assert_fresh_591(payload: dict[str, Any]) -> None:
    row = source_591(payload)
    if row.get("snapshot_used"):
        raise AssertionError("591 不得以舊快照通過本輪驗證")
    if row.get("fallback"):
        raise AssertionError("591 不得以舊快照補入本輪發布清單")


def rate_limited_591(payload: dict[str, Any]) -> bool:
    """Only retry an explicit 403/429 block, not a legitimate empty result."""
    row = source_591(payload)
    if row.get("blocked_after_queries") or row.get("partial_refresh"):
        return True
    if int(row.get("validated", 0) or 0) > 0:
        return False
    statuses = row.get("http_statuses", {})
    for channel in statuses.values() if isinstance(statuses, dict) else ():
        if isinstance(channel, dict) and any(
            str(code) in {"403", "429"} and int(count or 0) > 0
            for code, count in channel.items()
        ):
            return True
    explicitly_limited = any(
        "403/429" in str(message) or "出口IP" in str(message)
        for message in row.get("errors", [])
    )
    if explicitly_limited:
        return True
    # A completed crawl with zero rows is a legitimate empty result. Older
    # payloads may not have crawl_complete, so only use it when explicitly set.
    return row.get("crawl_complete") is False


def validated_source_items(
    payload: dict[str, Any],
    source: str,
    final_generated_at: datetime,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Validate one attempt's published rows and apply the final time boundary."""

    row = payload["stats"]["sources"][source]
    source_items = [
        copy.deepcopy(item)
        for item in payload.get("items", [])
        if item.get("source") == source
    ]
    if len(source_items) != int(row.get("published", 0) or 0):
        raise ValueError(f"published count does not match items for {source}")

    generated_at = parse_timestamp(payload.get("generated_at"))
    freshness_window = source_freshness_window(source)
    retained: list[dict[str, Any]] = []
    boundary_rejected: set[str] = set()
    for item in source_items:
        validated_at = parse_timestamp(item.get("validated_at"))
        source_timestamp = parse_timestamp(item.get("source_timestamp"))
        if not timedelta(0) <= generated_at - source_timestamp <= freshness_window:
            raise ValueError(
                f"non-fresh source timestamp for {source}:{item.get('source_id')}"
            )
        if abs(generated_at - validated_at) > MAX_ATTEMPT_SPAN:
            raise ValueError(
                f"stale validation timestamp for {source}:{item.get('source_id')}"
            )
        if final_generated_at - source_timestamp > freshness_window:
            boundary_rejected.add(
                str(item.get("source_id") or item.get("url") or "unknown")
            )
            continue
        retained.append(item)
    return retained, boundary_rejected


def validation_score(payload: dict[str, Any]) -> tuple[int, int, int, str]:
    """Prefer a run with fresh 591 rows, then the strongest total result."""
    assert_fresh_591(payload)
    row = source_591(payload)
    validated_591 = int(row.get("validated", 0) or 0)
    total_validated = int(payload.get("stats", {}).get("validated", 0) or 0)
    return (
        int(validated_591 > 0),
        validated_591,
        total_validated,
        str(payload.get("generated_at", "")),
    )


def select_attempt(roots: Iterable[Path]) -> tuple[Path, dict[str, Any]]:
    available: list[tuple[Path, dict[str, Any]]] = []
    for root in roots:
        if (root / "rental-data" / "latest.json").is_file():
            available.append((root, load_payload(root)))
    if not available:
        raise FileNotFoundError("no fresh validation artifact is available")
    return max(available, key=lambda row: validation_score(row[1]))


def parse_timestamp(value: Any) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value}")
    return parsed


def source_score(payload: dict[str, Any], source: str) -> tuple[int, int, int, str]:
    row = payload.get("stats", {}).get("sources", {}).get(source, {})
    if source == "591":
        assert_fresh_591(payload)
    return (
        int(row.get("published", 0) or 0),
        int(row.get("validated", 0) or 0),
        int(row.get("candidate_links", 0) or 0),
        str(payload.get("generated_at", "")),
    )


def merge_source_payloads(
    named_payloads: Iterable[tuple[str, dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Combine only fresh rows, choosing the strongest attempt per source.

    All attempts must belong to the same validation window. This prevents an old
    successful source snapshot from being used to fill a currently blocked run.
    """

    attempts = list(named_payloads)
    if not attempts:
        raise FileNotFoundError("no fresh validation artifact is available")
    generated = [parse_timestamp(payload.get("generated_at")) for _, payload in attempts]
    if max(generated) - min(generated) > MAX_ATTEMPT_SPAN:
        raise ValueError("validation attempts are outside the one-hour merge window")

    comparisons = {
        str(payload.get("stats", {}).get("comparison_source", ""))
        for _, payload in attempts
    }
    if not comparisons or any(not value.startswith("delivery:") for value in comparisons):
        raise ValueError("validation attempts must use a successful delivery comparison")

    final_generated_at = max(generated)
    merged_sources: dict[str, Any] = {}
    merged_items: list[dict[str, Any]] = []
    choices: dict[str, str] = {}
    for source in SOURCE_ORDER:
        freshness_days = source_freshness_days(source)
        freshness_window = source_freshness_window(source)
        available = [
            (name, payload)
            for name, payload in attempts
            if source in payload.get("stats", {}).get("sources", {})
            and not payload["stats"]["sources"][source].get("validation_skipped")
        ]
        if not available:
            raise ValueError(f"missing source diagnostics: {source}")
        name, selected = max(available, key=lambda row: source_score(row[1], source))
        selected_row = copy.deepcopy(selected["stats"]["sources"][source])
        boundary_keys: set[str] = set()

        if source == "591":
            complete = [
                (attempt_name, payload)
                for attempt_name, payload in available
                if payload["stats"]["sources"]["591"].get("crawl_complete") is True
            ]
            if complete:
                # A complete later crawl is authoritative; do not re-add rows that
                # appeared in an earlier partial checkpoint but disappeared later.
                name, selected = max(
                    complete,
                    key=lambda row: (
                        parse_timestamp(row[1]["generated_at"]),
                        source_score(row[1], source),
                    ),
                )
                selected_row = copy.deepcopy(selected["stats"]["sources"][source])
                source_items, boundary_keys = validated_source_items(
                    selected, source, final_generated_at
                )
                selected_row["checkpoint_attempts"] = [name]
            else:
                # Every row is still from this validation window. Union partial
                # checkpoints by stable source id so a later runner can continue
                # where the first runner was rate-limited, without using old data.
                checkpoint_items: dict[str, dict[str, Any]] = {}
                contributing: list[str] = []
                for attempt_name, payload in sorted(
                    available, key=lambda row: parse_timestamp(row[1]["generated_at"])
                ):
                    retained, rejected = validated_source_items(
                        payload, source, final_generated_at
                    )
                    boundary_keys.update(rejected)
                    if retained:
                        contributing.append(attempt_name)
                    for item in retained:
                        key = str(item.get("source_id") or item.get("url") or "")
                        if not key:
                            raise ValueError("591 checkpoint item is missing source identity")
                        previous = checkpoint_items.get(key)
                        if previous is None or parse_timestamp(
                            item.get("validated_at")
                        ) > parse_timestamp(previous.get("validated_at")):
                            checkpoint_items[key] = item
                source_items = sorted(
                    checkpoint_items.values(),
                    key=lambda item: parse_timestamp(item.get("source_timestamp")),
                    reverse=True,
                )
                selected_row["crawl_complete"] = False
                selected_row["partial_refresh"] = True
                selected_row["checkpoint_attempts"] = contributing
                selected_row["checkpoint_union_items"] = len(source_items)
                selected_row["candidate_links"] = max(
                    len(source_items),
                    max(
                        int(payload["stats"]["sources"][source].get("candidate_links", 0) or 0)
                        for _, payload in available
                    ),
                )
                selected_row.setdefault("notices", []).append(
                    f"同一驗證時窗合併{len(contributing)}次591 checkpoint，"
                    f"共{len(source_items)}筆不重複且本輪重新驗證成功的物件。"
                )
                name = (
                    contributing[0]
                    if len(contributing) == 1
                    else "checkpoint-union:" + ",".join(contributing)
                )
        else:
            source_items, boundary_keys = validated_source_items(
                selected, source, final_generated_at
            )

        boundary_rejected = len(boundary_keys)
        if boundary_rejected:
            rejects = selected_row.setdefault("rejects", {})
            reason = f"source_older_than_{freshness_days}_days"
            rejects[reason] = int(rejects.get(reason, 0) or 0) + boundary_rejected
            selected_row["freshness_rejected"] = (
                int(selected_row.get("freshness_rejected", 0) or 0) + boundary_rejected
            )
            fresh_key = f"fresh_within_{freshness_days}_days"
            selected_row[fresh_key] = max(
                int(selected_row.get(fresh_key, len(source_items) + boundary_rejected) or 0)
                - boundary_rejected,
                0,
            )
            selected_row["validated"] = max(
                int(selected_row.get("validated", 0) or 0) - boundary_rejected,
                0,
            )
            selected_row.setdefault("notices", []).append(
                f"逐來源合併時另排除{boundary_rejected}筆已跨過{freshness_days}天邊界的物件。"
            )
        selected_row["freshness_window_days"] = freshness_days
        selected_row[f"fresh_within_{freshness_days}_days"] = len(source_items)
        selected_row["validated"] = len(source_items)
        selected_row["published"] = len(source_items)
        merged_sources[source] = selected_row
        merged_items.extend(source_items)
        choices[source] = name

    newest_name, newest = max(attempts, key=lambda row: parse_timestamp(row[1]["generated_at"]))
    stats = copy.deepcopy(newest.get("stats", {}))
    stats["sources"] = merged_sources
    stats["candidates"] = sum(
        int(row.get("candidate_links", 0) or 0) for row in merged_sources.values()
    )
    stats["validated"] = sum(
        int(row.get("validated", 0) or 0) for row in merged_sources.values()
    )
    stats["published"] = len(merged_items)
    stats["new_listings"] = sum(bool(item.get("new_listing")) for item in merged_items)
    stats["freshness_rejected"] = sum(
        int(row.get("freshness_rejected", 0) or 0) for row in merged_sources.values()
    )
    stats["duplicates"] = sum(
        max(
            int(row.get("validated", 0) or 0) - int(row.get("published", 0) or 0),
            0,
        )
        for row in merged_sources.values()
    )
    stats["comparison_source"] = max(comparisons)
    stats["validation_comparison_sources"] = sorted(comparisons)
    stats["validation_attempts"] = choices
    stats.pop("freshness_window_hours", None)
    stats["default_freshness_window_hours"] = SOURCE_FRESHNESS_DAYS * 24
    stats["freshness_window_hours_by_source"] = {
        source: source_freshness_days(source) * 24 for source in SOURCE_ORDER
    }
    return {
        "generated_at": newest["generated_at"],
        "edition_id": newest.get("edition_id", ""),
        "edition_url": newest.get("edition_url", ""),
        "stats": stats,
        "items": merged_items,
    }, choices


def load_digest_module() -> Any:
    module_path = Path(__file__).with_name("generate_rental_digest.py")
    spec = importlib.util.spec_from_file_location("rental_digest_for_merge", module_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load renderer: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_merged_edition(
    payload: dict[str, Any], output: Path, edition_id: str | None, site_url: str
) -> dict[str, Any]:
    digest = load_digest_module()
    final_edition = edition_id or str(payload.get("edition_id", ""))
    edition_url = digest.archive_url_for_edition(final_edition, site_url)
    final_payload = copy.deepcopy(payload)
    final_payload["edition_id"] = final_edition
    final_payload["edition_url"] = edition_url
    listings = [digest.Listing(**item) for item in final_payload["items"]]
    previous_keys, comparison_source = digest.load_previous_edition_keys()
    listings = digest.assign_previous_edition_new_flags(listings, previous_keys)
    final_payload["items"] = [asdict(item) for item in listings]
    final_payload["stats"]["comparison_source"] = comparison_source
    final_payload["stats"]["new_listings"] = sum(item.new_listing for item in listings)
    rendered = digest.render_html(listings, final_payload["stats"])
    payload_text = json.dumps(final_payload, ensure_ascii=False, indent=2)

    data = output / "rental-data"
    archive = output / "archive"
    editions = data / "editions"
    data.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)
    editions.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(rendered, encoding="utf-8")
    (data / "latest.json").write_text(payload_text, encoding="utf-8")
    (archive / f"{final_edition}.html").write_text(rendered, encoding="utf-8")
    (editions / f"{final_edition}.json").write_text(payload_text, encoding="utf-8")
    return final_payload


def write_github_output(name: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT", "").strip()
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def inspect_command(latest: Path) -> int:
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert_fresh_591(payload)
    row = source_591(payload)
    retry = rate_limited_591(payload)
    print("591", json.dumps(row, ensure_ascii=False, indent=2))
    print(f"591 delayed retry required: {str(retry).lower()}")
    write_github_output("retry_591", str(retry).lower())
    return 0


def select_command(initial: Path, retry: Path, output: Path) -> int:
    selected, payload = select_attempt((initial, retry))
    selected_name = "retry" if selected.resolve() == retry.resolve() else "initial"
    if output.exists():
        shutil.rmtree(output)
    shutil.copytree(selected, output)
    print(
        "Selected validation attempt:",
        selected_name,
        json.dumps(validation_score(payload), ensure_ascii=False),
    )
    write_github_output("selected_attempt", selected_name)
    return 0


def merge_command(
    attempts: list[str], output: Path, edition_id: str | None, site_url: str
) -> int:
    available: list[tuple[str, Path, dict[str, Any]]] = []
    for raw in attempts:
        if "=" not in raw:
            raise ValueError(f"attempt must use NAME=PATH: {raw}")
        name, path_text = raw.split("=", 1)
        path = Path(path_text)
        if (path / "rental-data" / "latest.json").is_file():
            available.append((name, path, load_payload(path)))
    if not available:
        raise FileNotFoundError("no fresh validation artifact is available")

    for _, path, payload in sorted(
        available, key=lambda row: parse_timestamp(row[2]["generated_at"])
    ):
        if path.resolve() != output.resolve():
            shutil.copytree(path, output, dirs_exist_ok=True)
    merged, choices = merge_source_payloads((name, payload) for name, _, payload in available)
    final_payload = write_merged_edition(merged, output, edition_id, site_url)
    print("Merged validation sources:", json.dumps(choices, ensure_ascii=False))
    print("Merged edition:", final_payload["edition_url"])
    write_github_output("selected_attempt", "source-wise-merge")
    return 0


def verify_command(latest: Path, docs: Path) -> int:
    payload = json.loads(latest.read_text(encoding="utf-8"))
    generated_at = parse_timestamp(payload.get("generated_at"))
    now = datetime.now(generated_at.tzinfo)
    if not timedelta(0) <= now - generated_at <= MAX_PREVALIDATED_AGE:
        raise ValueError("prevalidated bundle is older than two hours")
    edition_id = str(payload.get("edition_id", ""))
    edition_url = str(payload.get("edition_url", ""))
    if not edition_id or not edition_url.endswith(f"/archive/{edition_id}.html"):
        raise ValueError("prevalidated bundle must use an immutable edition URL")
    if not edition_id.startswith(now.date().isoformat()):
        raise ValueError("prevalidated edition must use today's date")

    sources = payload.get("stats", {}).get("sources", {})
    if set(SOURCE_ORDER) - set(sources):
        raise ValueError("prevalidated bundle is missing source diagnostics")
    assert_fresh_591(payload)
    items = payload.get("items", [])
    if len(items) != int(payload.get("stats", {}).get("published", -1)):
        raise ValueError("prevalidated published count does not match items")
    for source in SOURCE_ORDER:
        freshness_window = source_freshness_window(source)
        source_items = [item for item in items if item.get("source") == source]
        if len(source_items) != int(sources[source].get("published", 0) or 0):
            raise ValueError(f"prevalidated source count does not match: {source}")
        for item in source_items:
            validated_at = parse_timestamp(item.get("validated_at"))
            source_timestamp = parse_timestamp(item.get("source_timestamp"))
            if not timedelta(0) <= generated_at - source_timestamp <= freshness_window:
                raise ValueError(f"non-fresh source timestamp for {source}:{item.get('source_id')}")
            if abs(generated_at - validated_at) > MAX_ATTEMPT_SPAN:
                raise ValueError(f"stale validation timestamp for {source}:{item.get('source_id')}")

    archive = docs / "archive" / f"{edition_id}.html"
    edition = docs / "rental-data" / "editions" / f"{edition_id}.json"
    if not archive.is_file() or not edition.is_file():
        raise FileNotFoundError("prevalidated immutable edition files are missing")
    archive_text = archive.read_text(encoding="utf-8")
    if archive_text.count('class="card') != len(items):
        raise ValueError("prevalidated archive card count does not match items")
    if any(str(item.get("url", "")) not in archive_text for item in items):
        raise ValueError("prevalidated archive is missing one or more listing URLs")
    print(
        "Verified prevalidated edition:",
        edition_id,
        f"items={len(items)}",
        f"generated_at={payload['generated_at']}",
    )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect")
    inspect.add_argument(
        "--latest",
        type=Path,
        default=Path("docs/rental-data/latest.json"),
    )

    select = commands.add_parser("select")
    select.add_argument("--initial", type=Path, required=True)
    select.add_argument("--retry", type=Path, required=True)
    select.add_argument("--output", type=Path, required=True)
    merge = commands.add_parser("merge")
    merge.add_argument("--attempt", action="append", required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--edition-id")
    merge.add_argument("--site-url", default="https://flyspacesky.github.io/taoyuan-rental-digest/")
    verify = commands.add_parser("verify")
    verify.add_argument("--latest", type=Path, default=Path("docs/rental-data/latest.json"))
    verify.add_argument("--docs", type=Path, default=Path("docs"))
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "inspect":
        return inspect_command(args.latest)
    if args.command == "select":
        return select_command(args.initial, args.retry, args.output)
    if args.command == "merge":
        return merge_command(args.attempt, args.output, args.edition_id, args.site_url)
    return verify_command(args.latest, args.docs)


if __name__ == "__main__":
    raise SystemExit(main())
