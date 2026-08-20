#!/usr/bin/env python3
"""Inspect and select fresh rental-validation artifacts for GitHub Actions."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any, Iterable


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
    if int(row.get("validated", 0) or 0) > 0:
        return False
    if row.get("blocked_after_queries"):
        return True
    statuses = row.get("http_statuses", {})
    for channel in statuses.values() if isinstance(statuses, dict) else ():
        if isinstance(channel, dict) and any(
            str(code) in {"403", "429"} and int(count or 0) > 0
            for code, count in channel.items()
        ):
            return True
    return any(
        "403/429" in str(message) or "出口IP" in str(message)
        for message in row.get("errors", [])
    )


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
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "inspect":
        return inspect_command(args.latest)
    return select_command(args.initial, args.retry, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
