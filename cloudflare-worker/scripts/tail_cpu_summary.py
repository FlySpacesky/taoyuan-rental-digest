"""Print only non-sensitive CPU fields from a Wrangler JSON tail."""

import json
from pathlib import Path


SAFE_KEYS = {
    "cpuTime",
    "wallTime",
    "outcome",
    "scriptName",
    "eventTimestamp",
    "invocationId",
}


def safe_values(value):
    found = {}
    if isinstance(value, dict):
        for key, child in value.items():
            if key in SAFE_KEYS and isinstance(child, (str, int, float, bool, type(None))):
                found[key] = child
            found.update(safe_values(child))
    elif isinstance(value, list):
        for child in value:
            found.update(safe_values(child))
    return found


def summarize(path: Path):
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        safe = safe_values(value)
        if safe:
            rows.append(safe)
    return rows


def main():
    path = Path("preview-audit/tail.jsonl")
    rows = summarize(path) if path.exists() else []
    output = {"events": rows, "event_count": len(rows)}
    Path("preview-audit/tail-summary.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
