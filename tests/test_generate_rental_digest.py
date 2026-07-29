from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_rental_digest",
    ROOT / "scripts" / "generate_rental_digest.py",
)
assert SPEC and SPEC.loader
DIGEST = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DIGEST
SPEC.loader.exec_module(DIGEST)


def list_card(
    *,
    item_id: str = "21700001",
    layout: str = "4房2廳2衛",
    publisher: str = "屋主: 林先生",
    title: str = "桃園區四房整層住家",
    price_text: str = "32,000 元/月",
) -> str:
    return f"""
    <div class="item" data-id="{item_id}">
      <a class="link v-middle" href="https://rent.591.com.tw/{item_id}"
         title="{title}">
        <img src="https://images.example.test/{item_id}.jpg" alt="{title}">
      </a>
      <div class="item-info-txt">
        <i class="ic-house house-home"></i>
        <span>整層住家</span>
        <span class="line">{layout}</span>
        <span class="line">35坪</span>
        <span class="line">6F/12F</span>
      </div>
      <div>桃園區 - 中正路 電梯大樓</div>
      <div class="role-name">
        <span>{publisher}</span><span>1小時內更新</span>
      </div>
      <div class="item-info-price">{price_text}</div>
    </div>
    """


def detail_html(*, layout: str, publisher: str, badge: str = "") -> str:
    padding = "x" * 1200
    return f"""
    <html>
      <head>
        <meta property="og:title" content="桃園區四房整層住家">
        <meta property="og:image" content="https://images.example.test/detail.jpg">
        <meta property="og:description" content="桃園區中正路整層住家">
        <script type="application/ld+json">
          {{"@type":"Product","offers":{{"price":"32000"}}}}
        </script>
      </head>
      <body>
        <div class="info-board">
          <h1>桃園區四房整層住家</h1>
          <div class="house-label">{badge}</div>
          <div class="pattern"><span>{layout}</span></div>
          <div>整層住家 桃園區中正路 35坪 6F/12F 電梯大樓</div>
        </div>
        <div class="house-condition-content">最短租期 一年 冰箱 冷氣</div>
        <div class="contact-info">
          <div class="base-info-pc"><div class="name">{publisher}</div></div>
        </div>
        <aside class="recommendation">屋主直租 5房2廳</aside>
        <!-- {padding} -->
      </body>
    </html>
    """


class Extract591Tests(unittest.TestCase):
    def setUp(self) -> None:
        DIGEST._591_LIST_CACHE.clear()
        DIGEST._591_BFF_CACHE_IDS.clear()
        DIGEST._591_REJECTS.clear()

    def test_extracts_only_rental_card_ids(self) -> None:
        raw = (
            '<a href="https://market.591.com.tw/12345678">社區</a>'
            + list_card()
            + '<footer><a href="/87654321">頁尾</a></footer>'
        )
        self.assertEqual(DIGEST.extract_591_ids(raw), ["21700001"])

    def test_broker_title_cannot_be_classified_as_owner(self) -> None:
        cards = DIGEST.parse_591_list_cards(
            list_card(
                publisher="仲介: 王先生",
                title="屋主自租免仲介費四房",
            )
        )
        item = cards["21700001"]
        self.assertEqual(item.publisher, "仲介: 王先生")
        self.assertNotEqual(item.category_hint, "owner")
        self.assertEqual(item.url, "https://rent.591.com.tw/21700001")

    def test_explicit_owner_role_is_owner(self) -> None:
        item = DIGEST.parse_591_list_cards(list_card())["21700001"]
        self.assertEqual(item.category_hint, "owner")

    def test_main_rent_is_not_replaced_by_extra_fee(self) -> None:
        item = DIGEST.parse_591_list_cards(
            list_card(price_text="58,000 元/月 (額外費用 8,400元/月)")
        )["21700001"]

        self.assertEqual(item.rent, 58_000)
        self.assertEqual(item.address, "桃園區 - 中正路 電梯大樓")

    def test_crawler_uses_owner_filter_and_canonical_detail_url(self) -> None:
        requested: list[str] = []

        def fake_fetch(url: str, **_: object) -> tuple[None, str]:
            requested.append(url)
            if (
                "section=73" in url
                and "shType=host" in url
                and "page=1" in url
            ):
                return None, list_card()
            return None, "<html>" + ("x" * 900) + "</html>"

        stats = DIGEST.empty_source_stats()
        with (
            patch.object(DIGEST, "fetch_html", side_effect=fake_fetch),
            patch.object(DIGEST, "fetch_591_bff_cards", return_value=(None, 0, {})),
            patch.object(DIGEST.browser, "html", return_value=""),
            patch.object(DIGEST.time, "sleep", return_value=None),
        ):
            links = DIGEST.crawl_591_links(stats)

        self.assertEqual(links, ["https://rent.591.com.tw/21700001"])
        self.assertTrue(
            any(
                "shType=host" in url and "page=1" in url
                for url in requested
            )
        )
        self.assertTrue(any("page=2" in url for url in requested))
        self.assertFalse(any("firstRow=" in url for url in requested))

    def test_bff_cards_use_explicit_role_and_official_discount(self) -> None:
        payload = {
            "status": 1,
            "data": {
                "items": [
                    {
                        "id": 21700001,
                        "kind_name": "整層住家",
                        "title": "屋主自租字樣但實際為仲介",
                        "price": "28,000",
                        "diff_price": 2_000,
                        "floor_name": "6F/12F",
                        "area_name": "35坪",
                        "layoutStr": "4房2廳",
                        "address": "桃園區-中正路",
                        "role_name": "仲介王先生",
                        "refresh_time": "1小時內更新",
                        "browse_count": 12,
                        "cover": "https://img1.591.com.tw/house/example.jpg",
                        "tags": ["屋主直租"],
                    },
                    {
                        "id": 21700002,
                        "kind_name": "整層住家",
                        "title": "四房整層住家",
                        "price": "32,000",
                        "diff_price": 0,
                        "floor_name": "整棟/3F",
                        "area_name": "45坪",
                        "layoutStr": "4房2廳",
                        "address": "中壢區-中央路",
                        "role_name": "屋主林先生",
                        "refresh_time": "2小時內更新",
                        "browse_count": 8,
                        "cover": "https://img1.591.com.tw/house/owner.jpg",
                        "tags": [],
                    },
                ]
            },
        }

        cards = DIGEST.parse_591_bff_cards(payload)

        broker = cards["21700001"]
        self.assertEqual(broker.publisher, "仲介王先生")
        self.assertEqual(broker.category_hint, "discount")
        self.assertEqual(broker.old_rent, 30_000)
        self.assertNotEqual(broker.category_hint, "owner")
        owner = cards["21700002"]
        self.assertEqual(owner.category_hint, "owner")
        self.assertEqual(owner.url, "https://rent.591.com.tw/21700002")

    def test_bff_request_maps_page_to_first_row(self) -> None:
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {"status": 1, "data": {"items": []}},
        )
        with patch.object(DIGEST.session, "get", return_value=response) as get:
            status, first_row, cards = DIGEST.fetch_591_bff_cards(
                {
                    "kind": 1,
                    "layout": 4,
                    "region": 6,
                    "section": "73",
                    "shType": "host",
                    "page": 3,
                }
            )

        self.assertEqual(status, 200)
        self.assertEqual(first_row, 60)
        self.assertEqual(cards, {})
        self.assertEqual(get.call_args.kwargs["params"]["firstRow"], "60")
        self.assertEqual(get.call_args.kwargs["params"]["shType"], "host")

    def test_crawler_prefers_bff_over_html(self) -> None:
        stats = DIGEST.empty_source_stats()
        bff_item = DIGEST.parse_591_bff_cards(
            {
                "status": 1,
                "data": {
                    "items": [
                        {
                            "id": 21700001,
                            "kind_name": "整層住家",
                            "title": "桃園區四房整層住家",
                            "price": "32,000",
                            "diff_price": 0,
                            "floor_name": "6F/12F",
                            "area_name": "35坪",
                            "layoutStr": "4房2廳",
                            "address": "桃園區-中正路",
                            "role_name": "屋主林先生",
                            "refresh_time": "1小時內更新",
                            "browse_count": 12,
                            "cover": "https://img1.591.com.tw/house/example.jpg",
                            "tags": [],
                        }
                    ]
                },
            }
        )
        calls = 0

        def fake_bff(
            _: dict[str, object],
        ) -> tuple[int, int, dict[str, DIGEST.Listing]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return 200, 0, bff_item
            return 200, 30, {}

        with (
            patch.object(DIGEST, "fetch_591_bff_cards", side_effect=fake_bff),
            patch.object(DIGEST, "fetch_html") as fetch,
            patch.object(DIGEST.time, "sleep", return_value=None),
        ):
            links = DIGEST.crawl_591_links(stats)

        self.assertEqual(links, ["https://rent.591.com.tw/21700001"])
        fetch.assert_not_called()

    def test_crawler_stops_after_two_fully_blocked_queries(self) -> None:
        stats = DIGEST.empty_source_stats()
        forbidden = SimpleNamespace(status_code=403)
        with (
            patch.object(
                DIGEST,
                "fetch_591_bff_cards",
                return_value=(403, 0, {}),
            ),
            patch.object(
                DIGEST,
                "fetch_html",
                return_value=(forbidden, "<html>access denied</html>"),
            ) as fetch,
            patch.object(
                DIGEST.browser,
                "html",
                return_value="<html>captcha</html>",
            ) as browser_fetch,
        ):
            links = DIGEST.crawl_591_links(stats)

        self.assertEqual(links, [])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(browser_fetch.call_count, 2)
        self.assertEqual(stats["blocked_after_queries"], 2)
        self.assertEqual(stats["http_statuses"]["bff"]["403"], 2)
        self.assertIn("不是Chromium未安裝", stats["errors"][0])


class Detail591Tests(unittest.TestCase):
    def setUp(self) -> None:
        DIGEST._591_LIST_CACHE.clear()
        DIGEST._591_BFF_CACHE_IDS.clear()
        DIGEST._591_REJECTS.clear()

    def test_detail_broker_overrides_owner_words_elsewhere(self) -> None:
        DIGEST._591_LIST_CACHE["21700001"] = DIGEST.parse_591_list_cards(
            list_card()
        )["21700001"]
        raw = detail_html(layout="4房2廳2衛", publisher="仲介: 王先生")

        with patch.object(DIGEST, "fetch_html", return_value=(None, raw)):
            item = DIGEST.parse_591_detail("https://rent.591.com.tw/21700001")

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.publisher, "仲介: 王先生")
        self.assertNotEqual(item.category_hint, "owner")
        self.assertEqual(item.url, "https://rent.591.com.tw/21700001")

    def test_detail_explicit_owner_role_is_owner(self) -> None:
        raw = detail_html(
            layout="4房2廳2衛",
            publisher="屋主: 林先生",
            badge='<span class="host">屋主直租</span>',
        )
        with patch.object(DIGEST, "fetch_html", return_value=(None, raw)):
            item = DIGEST.parse_591_detail("https://rent.591.com.tw/21700001")

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.category_hint, "owner")

    def test_detail_cannot_override_cached_main_rent(self) -> None:
        cached = DIGEST.parse_591_list_cards(
            list_card(price_text="58,000 元/月 (額外費用 8,400元/月)")
        )["21700001"]
        DIGEST._591_LIST_CACHE["21700001"] = cached
        raw = detail_html(
            layout="4房2廳2衛",
            publisher="仲介: 王先生",
        ).replace('"price":"32000"', '"price":"8400"')

        with patch.object(DIGEST, "fetch_html", return_value=(None, raw)):
            item = DIGEST.parse_591_detail("https://rent.591.com.tw/21700001")

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.rent, 58_000)

    def test_recommended_four_room_does_not_rescue_three_room_detail(self) -> None:
        raw = detail_html(layout="3房2廳2衛", publisher="仲介: 王先生")
        with patch.object(DIGEST, "fetch_html", return_value=(None, raw)):
            item = DIGEST.parse_591_detail("https://rent.591.com.tw/21700001")

        self.assertIsNone(item)
        self.assertEqual(DIGEST._591_REJECTS.get("not_4_rooms"), 1)

    def test_bff_cache_skips_all_detail_network_requests(self) -> None:
        cached = DIGEST.parse_591_bff_cards(
            {
                "status": 1,
                "data": {
                    "items": [
                        {
                            "id": 21700001,
                            "kind_name": "整層住家",
                            "title": "桃園區四房整層住家",
                            "price": "32,000",
                            "diff_price": 0,
                            "floor_name": "6F/12F",
                            "area_name": "35坪",
                            "layoutStr": "4房2廳",
                            "address": "桃園區-中正路",
                            "role_name": "仲介王先生",
                            "refresh_time": "1小時內更新",
                            "browse_count": 12,
                            "cover": "https://img1.591.com.tw/house/example.jpg",
                            "tags": [],
                        }
                    ]
                },
            }
        )["21700001"]
        DIGEST._591_LIST_CACHE["21700001"] = cached
        DIGEST._591_BFF_CACHE_IDS.add("21700001")

        with (
            patch.object(DIGEST, "get_requests") as get,
            patch.object(DIGEST, "fetch_html") as fetch,
        ):
            item = DIGEST.parse_591_detail("https://rent.591.com.tw/21700001")

        self.assertIs(item, cached)
        get.assert_not_called()
        fetch.assert_not_called()


class Snapshot591Tests(unittest.TestCase):
    def setUp(self) -> None:
        DIGEST._591_LIST_CACHE.clear()
        DIGEST._591_BFF_CACHE_IDS.clear()
        DIGEST._591_REJECTS.clear()

    @staticmethod
    def listing() -> object:
        item = DIGEST.Listing(
            source="591",
            source_id="21700001",
            url="https://rent.591.com.tw/21700001",
            title="桃園區四房整層住家",
            district="桃園區",
            address="桃園區中正路",
            house_type="整層住家",
            layout="4房2廳",
            rent=32_000,
            publisher="屋主林先生",
            image="https://img1.591.com.tw/house/example.jpg",
            validated_at=DIGEST.NOW.isoformat(),
            raw_text="桃園區四房整層住家",
        )
        item.fingerprint = DIGEST.fingerprint(item)
        return item

    def test_recent_snapshot_avoids_refresh_rate_limit(self) -> None:
        stats = DIGEST.empty_source_stats()
        item = self.listing()
        with (
            patch.object(
                DIGEST,
                "load_591_snapshot",
                return_value=([item], DIGEST.NOW.isoformat(), 0.5, ""),
            ),
            patch.object(DIGEST, "crawl_591_links") as crawl,
        ):
            result = DIGEST.collect_591_listings(stats)

        self.assertEqual(result, [item])
        crawl.assert_not_called()
        self.assertEqual(stats["candidate_links"], 1)
        self.assertEqual(stats["fresh_candidate_links"], 0)
        self.assertEqual(stats["validated"], 1)
        self.assertEqual(stats["fallback"], "refresh_cooldown")

    def test_blocked_refresh_uses_recent_real_snapshot(self) -> None:
        stats = DIGEST.empty_source_stats()
        item = self.listing()

        def blocked_crawl(row: dict[str, object]) -> list[str]:
            row["errors"].append("591 BFF 403")
            row["candidate_links"] = 0
            return []

        with (
            patch.object(
                DIGEST,
                "load_591_snapshot",
                return_value=([item], DIGEST.NOW.isoformat(), 3.0, ""),
            ),
            patch.object(DIGEST, "crawl_591_links", side_effect=blocked_crawl),
        ):
            result = DIGEST.collect_591_listings(stats)

        self.assertEqual(result, [item])
        self.assertEqual(stats["candidate_links"], 1)
        self.assertEqual(stats["fresh_candidate_links"], 0)
        self.assertEqual(stats["validated"], 1)
        self.assertEqual(stats["fallback"], "source_blocked")
        self.assertTrue(any("未重新驗證" in value for value in stats["errors"]))

    def test_successful_refresh_updates_snapshot(self) -> None:
        stats = DIGEST.empty_source_stats()
        item = self.listing()
        with (
            patch.object(
                DIGEST,
                "load_591_snapshot",
                return_value=([], "", None, ""),
            ),
            patch.object(
                DIGEST,
                "crawl_591_links",
                return_value=[item.url],
            ),
            patch.object(DIGEST, "parse_591_detail", return_value=item),
            patch.object(DIGEST, "save_591_snapshot") as save,
        ):
            result = DIGEST.collect_591_listings(stats)

        self.assertEqual(result, [item])
        self.assertEqual(stats["validated"], 1)
        save.assert_called_once_with([item])


class RakuyaFallbackTests(unittest.TestCase):
    def test_blocked_search_records_error_after_browser_fallback(self) -> None:
        stats = DIGEST.empty_source_stats()
        blocked = "<html>captcha" + ("x" * 900) + "</html>"
        with patch.object(
            DIGEST,
            "fetch_html",
            return_value=(None, blocked),
        ) as fetch:
            categories = DIGEST.crawl_rakuya_links(stats)

        self.assertEqual(categories["general"], set())
        self.assertEqual(stats["candidate_links"], 0)
        self.assertEqual(len(stats["errors"]), 1)
        self.assertTrue(fetch.call_args.kwargs["browser_fallback"])


class FacebookImportTests(unittest.TestCase):
    def test_missing_file_and_secret_reports_actionable_error(self) -> None:
        stats = DIGEST.empty_source_stats()
        missing = ROOT / "data" / "__missing_facebook_posts__.json"
        with (
            patch.object(DIGEST, "FB_IMPORT", missing),
            patch.dict(
                os.environ,
                {
                    DIGEST.FB_IMPORT_ENV: "",
                    DIGEST.FB_IMPORT_URL_ENV: "",
                },
                clear=False,
            ),
        ):
            items = DIGEST.load_facebook_import(stats)

        self.assertEqual(items, [])
        self.assertEqual(stats["candidate_links"], 0)
        self.assertIn(DIGEST.FB_IMPORT_ENV, stats["errors"][0])

    def test_empty_actions_secret_is_a_valid_empty_import(self) -> None:
        stats = DIGEST.empty_source_stats()
        missing = ROOT / "data" / "__missing_facebook_posts__.json"
        with (
            patch.object(DIGEST, "FB_IMPORT", missing),
            patch.dict(
                os.environ,
                {
                    DIGEST.FB_IMPORT_ENV: "[]",
                    DIGEST.FB_IMPORT_URL_ENV: "",
                },
                clear=False,
            ),
        ):
            items = DIGEST.load_facebook_import(stats)

        self.assertEqual(items, [])
        self.assertEqual(stats["candidate_links"], 0)
        self.assertEqual(stats["validated"], 0)
        self.assertEqual(stats["errors"], [])

    def test_https_feed_can_supply_an_empty_import(self) -> None:
        stats = DIGEST.empty_source_stats()
        missing = ROOT / "data" / "__missing_facebook_posts__.json"
        response = SimpleNamespace(status_code=200)
        with (
            patch.object(DIGEST, "FB_IMPORT", missing),
            patch.dict(
                os.environ,
                {
                    DIGEST.FB_IMPORT_ENV: "",
                    DIGEST.FB_IMPORT_URL_ENV: "https://feed.example.test/posts.json",
                },
                clear=False,
            ),
            patch.object(DIGEST, "get_requests", return_value=(response, "[]")),
        ):
            items = DIGEST.load_facebook_import(stats)

        self.assertEqual(items, [])
        self.assertEqual(stats["candidate_links"], 0)
        self.assertIn(DIGEST.FB_IMPORT_URL_ENV, stats["import_source"])


class CurrentListingDisplayTests(unittest.TestCase):
    @staticmethod
    def listing(
        source_id: str,
        *,
        publisher: str = "仲介: 王先生",
        category_hint: str = "",
        category: str = "general",
        rent: int = 32000,
        old_rent: int = 0,
    ) -> object:
        item = DIGEST.Listing(
            source="591",
            source_id=source_id,
            url=f"https://rent.591.com.tw/{source_id}",
            title=f"桃園區四房 {source_id}",
            district="桃園區",
            address=f"桃園區中正路{source_id}",
            layout="4房2廳2衛",
            rent=rent,
            old_rent=old_rent,
            publisher=publisher,
            image="https://images.example.test/current.jpg",
            category_hint=category_hint,
            category=category,
        )
        item.fingerprint = DIGEST.fingerprint(item)
        return item

    def test_recent_history_does_not_hide_current_valid_listing(self) -> None:
        item = self.listing("21700001")
        state = {
            "sent": [
                {
                    "source_key": "591:21700001",
                    "fingerprint": item.fingerprint,
                    "sent_at": DIGEST.NOW.isoformat(),
                    "title": item.title,
                    "url": item.url,
                }
            ],
            "prices": {},
        }

        output, duplicate_count = DIGEST.filter_recent_duplicates([item], state)

        self.assertEqual(output, [item])
        self.assertEqual(duplicate_count, 1)

    def test_all_section_contains_owner_discount_and_general(self) -> None:
        owner = self.listing(
            "21700001",
            publisher="屋主: 林先生",
            category_hint="owner",
            category="owner",
        )
        discount = self.listing(
            "21700002",
            category_hint="discount",
            category="discount",
            old_rent=35000,
        )
        general = self.listing("21700003")
        items = [owner, discount, general]

        self.assertEqual(DIGEST.section_items(items, "591", "all"), items)
        self.assertEqual(DIGEST.section_items(items, "591", "owner"), [owner])
        self.assertEqual(
            DIGEST.section_items(items, "591", "discount"),
            [discount],
        )

    def test_owner_price_drop_stays_owner_and_appears_in_discount(self) -> None:
        owner = self.listing(
            "21700001",
            publisher="屋主: 林先生",
            category_hint="owner",
            category="",
            rent=30000,
        )
        state = {"sent": [], "prices": {"591:21700001": 33000}}

        DIGEST.apply_categories([owner], state)

        self.assertEqual(owner.category, "owner")
        self.assertEqual(owner.old_rent, 33000)
        self.assertEqual(
            DIGEST.section_items([owner], "591", "discount"),
            [owner],
        )


if __name__ == "__main__":
    unittest.main()
