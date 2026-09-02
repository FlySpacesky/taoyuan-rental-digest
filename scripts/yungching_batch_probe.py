"""Run the production Yungching parser against an isolated render endpoint."""

import json
from pathlib import Path

import generate_rental_digest as digest


def main() -> None:
    stats = digest.empty_source_stats()
    rendered = digest.load_yungching_render_feed(stats)
    items = list((rendered or {}).values())
    current = digest.retain_current_source_inventory(
        items, {"sources": {"永慶房屋": stats}}
    )
    report = {
        "transport": stats.get("transport"),
        "candidate_links": stats.get("candidate_links", 0),
        "pages_read": stats.get("pages_read", 0),
        "details_fetched": stats.get("details_fetched", 0),
        "validated_before_inventory_check": len(items),
        "current_inventory": len(current),
        "browser_render_requests": stats.get("browser_render_requests", 0),
        "browser_render_ms_used": stats.get("browser_render_ms_used", 0),
        "crawl_complete": stats.get("crawl_complete", False),
        "notices": stats.get("notices", []),
        "sample": [
            {
                "source_id": item.source_id,
                "title": item.title,
                "updated": item.updated,
                "photo_count": int(bool(item.image)) + len(item.images),
            }
            for item in current[:5]
        ],
        "production_modified": False,
        "line_sent": False,
    }
    out = Path("preview-audit")
    out.mkdir(exist_ok=True)
    out.joinpath("full-batch-result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    assert report["candidate_links"] > 0, "Yungching search returned no candidates"
    assert report["crawl_complete"] is True, "Not every candidate detail was fetched"
    assert report["details_fetched"] == report["candidate_links"]
    assert report["validated_before_inventory_check"] > 0
    assert report["current_inventory"] > 0


if __name__ == "__main__":
    main()
