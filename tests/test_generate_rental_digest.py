from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


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

    @staticmethod
    def _bff_listing(item_id: str, refresh_time: str) -> dict[str, DIGEST.Listing]:
        return DIGEST.parse_591_bff_cards(
            {
                "status": 1,
                "data": {
                    "items": [
                        {
                            "id": item_id,
                            "kind_name": "整層住家",
                            "title": "桃園區四房整層住家",
                            "price": "32,000",
                            "floor_name": "6F/12F",
                            "area_name": "35坪",
                            "layoutStr": "4房2廳",
                            "address": "桃園區-中正路",
                            "role_name": "屋主林先生",
                            "refresh_time": refresh_time,
                            "cover": "https://img1.591.com.tw/house/example.jpg",
                            "tags": [],
                        }
                    ]
                },
            }
        )

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

    def test_crawler_uses_general_list_once_and_canonical_detail_url(self) -> None:
        requested: list[str] = []

        def fake_fetch(url: str, **_: object) -> tuple[None, str]:
            requested.append(url)
            if (
                "section=73" in url
                and "shType" not in url
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
        self.assertTrue(any("section=73" in url and "page=1" in url for url in requested))
        self.assertFalse(any("shType=" in url for url in requested))
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
                        "extra_fee": "3,300",
                        "diff_price": 2_000,
                        "preferred": 1,
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
        self.assertEqual(broker.total_cost, 31_300)
        self.assertTrue(DIGEST.is_591_featured(broker))
        self.assertNotEqual(broker.category_hint, "owner")
        owner = cards["21700002"]
        self.assertEqual(owner.category_hint, "owner")
        self.assertEqual(owner.url, "https://rent.591.com.tw/21700002")

    def test_bff_request_maps_page_to_first_row(self) -> None:
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {"status": 1, "data": {"items": []}},
        )
        with patch.object(DIGEST.session_591, "get", return_value=response) as get:
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

    def test_bff_request_preserves_final_429_status(self) -> None:
        response = SimpleNamespace(status_code=429)
        with (
            patch.object(DIGEST.session_591, "get", return_value=response),
            patch.object(DIGEST.time, "sleep"),
        ):
            status, first_row, cards = DIGEST.fetch_591_bff_cards(
                {
                    "kind": 1,
                    "layout": 4,
                    "region": 6,
                    "section": "73",
                    "page": 1,
                }
            )

        self.assertEqual(status, 429)
        self.assertEqual(first_row, 0)
        self.assertEqual(cards, {})

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
        self.assertTrue(stats["crawl_complete"])
        self.assertEqual(set(stats["completed_sections"]), set(DIGEST.DISTRICTS_591))

    def test_sorted_stale_page_is_a_complete_freshness_boundary(self) -> None:
        fresh = self._bff_listing("21700001", "1小時內更新")
        stale = self._bff_listing("21700002", "3天前更新")

        self.assertFalse(DIGEST._591_page_is_outside_freshness(fresh))
        self.assertTrue(DIGEST._591_page_is_outside_freshness(stale))

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
        fetch.assert_not_called()
        browser_fetch.assert_not_called()
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

    def test_recent_snapshot_does_not_skip_fresh_validation(self) -> None:
        stats = DIGEST.empty_source_stats()
        item = self.listing()

        def fresh_crawl(row: dict[str, object]) -> list[str]:
            row["candidate_links"] = 1
            return [item.url]

        with (
            patch.object(
                DIGEST,
                "load_591_snapshot",
                return_value=([item], DIGEST.NOW.isoformat(), 0.5, ""),
            ),
            patch.object(DIGEST, "crawl_591_links", side_effect=fresh_crawl) as crawl,
            patch.object(DIGEST, "parse_591_detail", return_value=item),
            patch.object(DIGEST, "save_591_snapshot") as save,
        ):
            result = DIGEST.collect_591_listings(stats)

        self.assertEqual(result, [item])
        crawl.assert_called_once_with(stats)
        save.assert_called_once_with([item])
        self.assertEqual(stats["candidate_links"], 1)
        self.assertEqual(stats["validated"], 1)
        self.assertNotIn("fallback", stats)

    def test_blocked_refresh_does_not_publish_recent_snapshot(self) -> None:
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

        self.assertEqual(result, [])
        self.assertEqual(stats["candidate_links"], 0)
        self.assertEqual(stats["validated"], 0)
        self.assertNotIn("fallback", stats)
        self.assertTrue(any("不沿用上次快照" in value for value in stats["errors"]))

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

    def test_partial_blocked_refresh_publishes_only_freshly_validated_items(self) -> None:
        stats = DIGEST.empty_source_stats()
        refreshed = self.listing()
        prior_only = self.listing()
        prior_only.source_id = "21700002"
        prior_only.url = "https://rent.591.com.tw/21700002"
        prior_only.title = "中壢區四房整層住家"
        prior_only.address = "中壢區中華路"
        prior_only.fingerprint = DIGEST.fingerprint(prior_only)

        def partial_crawl(row: dict[str, object]) -> list[str]:
            row["candidate_links"] = 1
            row["blocked_after_queries"] = 2
            return [refreshed.url]

        with (
            patch.object(
                DIGEST,
                "load_591_snapshot",
                return_value=(
                    [self.listing(), prior_only],
                    DIGEST.NOW.isoformat(),
                    3.0,
                    "",
                ),
            ),
            patch.object(DIGEST, "crawl_591_links", side_effect=partial_crawl),
            patch.object(DIGEST, "parse_591_detail", return_value=refreshed),
            patch.object(DIGEST, "save_591_snapshot") as save,
        ):
            result = DIGEST.collect_591_listings(stats)

        self.assertEqual([item.source_id for item in result], ["21700001"])
        self.assertEqual(stats["fresh_validated"], 1)
        self.assertEqual(stats["candidate_links"], 1)
        self.assertNotIn("fallback", stats)
        self.assertTrue(any("不沿用上次快照" in value for value in stats["errors"]))
        save.assert_not_called()


class RakuyaFallbackTests(unittest.TestCase):
    def test_source_time_uses_json_ld_before_visible_text(self) -> None:
        soup = DIGEST.BeautifulSoup(
            """
            <script type="application/ld+json">
            {"@type":"Apartment","dateModified":"2026-08-21T09:15:00+08:00"}
            </script>
            <div>頁尾版權日期 2020/01/01</div>
            """,
            "html.parser",
        )

        self.assertEqual(
            DIGEST.source_time_text_from_page(soup, soup.get_text(" ")),
            "2026-08-21T09:15:00+08:00",
        )

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

    def test_search_uses_current_tabs_and_includes_bade(self) -> None:
        stats = DIGEST.empty_source_stats()
        response = SimpleNamespace(url="https://rent.rakuya.com.tw/result")
        finished = "<html>符合條件的房屋已瀏覽完畢" + ("x" * 900) + "</html>"
        with patch.object(
            DIGEST,
            "fetch_html",
            return_value=(response, finished),
        ) as fetch:
            categories = DIGEST.crawl_rakuya_links(stats)

        requested = [call.args[0] for call in fetch.call_args_list]
        self.assertEqual(
            set(categories),
            {"general", "owner", "friendly", "discount"},
        )
        self.assertTrue(all("334" in url for url in requested))
        self.assertTrue(any("tab=rkp" in url for url in requested))
        self.assertTrue(any("tab=frd" in url for url in requested))
        self.assertTrue(any("tab=low" in url for url in requested))
        self.assertFalse(any("usecode=7" in url for url in requested))

    def test_friendly_filter_can_overlap_owner_and_discount(self) -> None:
        item = DIGEST.Listing(
            source="樂屋網",
            source_id="rakuya-1",
            url="https://rent.rakuya.com.tw/item/abc",
            title="桃園區四房可租補",
            district="桃園區",
            layout="4房2廳",
            rent=30_000,
            old_rent=32_000,
            category="owner",
            category_hint="owner",
            filter_tags=["owner", "friendly", "discount"],
        )

        self.assertEqual(
            DIGEST.listing_filter_tokens(item),
            ["rent", "owner", "friendly", "discount"],
        )

    def test_owner_filter_requires_official_owner_tab_hint(self) -> None:
        item = DIGEST.Listing(
            source="樂屋網",
            source_id="rakuya-2",
            url="https://rent.rakuya.com.tw/item/def",
            title="免仲介費四房出租",
            district="桃園區",
            layout="4房2廳",
            rent=30_000,
            category="general",
            raw_text="免仲介費，但未出現在樂屋網屋主頁籤。",
        )

        self.assertEqual(DIGEST.listing_filter_tokens(item), ["rent"])


class FacebookImportTests(unittest.TestCase):
    def test_resolved_group_permalink_is_accepted_as_public_group_post(self) -> None:
        permalink = (
            "https://www.facebook.com/groups/4091621327828556/"
            "permalink/4623380861319264/?rdid=tracking"
        )

        self.assertIn(
            "https://www.facebook.com/groups/4091621327828556",
            DIGEST.FB_GROUPS,
        )
        self.assertEqual(
            DIGEST.normalize_facebook_post_url(permalink),
            "https://www.facebook.com/groups/4091621327828556/"
            "permalink/4623380861319264/",
        )

    def test_mobile_posts_and_permalink_share_one_stable_key(self) -> None:
        mobile = (
            "https://m.facebook.com/groups/4091621327828556/"
            "posts/4623380861319264/?tracking=1"
        )
        permalink = (
            "https://www.facebook.com/groups/4091621327828556/"
            "permalink/4623380861319264/"
        )

        self.assertEqual(
            DIGEST.normalize_facebook_post_url(mobile),
            "https://www.facebook.com/groups/4091621327828556/"
            "posts/4623380861319264/",
        )
        self.assertEqual(
            DIGEST.facebook_post_key(mobile),
            DIGEST.facebook_post_key(permalink),
        )
        self.assertFalse(
            DIGEST.is_safe_facebook_image_download(
                "http://127.0.0.1/internal.jpg"
            )
        )
        self.assertFalse(
            DIGEST.is_safe_facebook_image_download(
                "https://images.example.test/untrusted.jpg"
            )
        )
        self.assertTrue(
            DIGEST.is_safe_facebook_image_download(
                "https://scontent.ftpe8-1.fna.fbcdn.net/real.jpg"
            )
        )

    def test_github_issue_form_body_becomes_a_sparse_verified_candidate(self) -> None:
        issue = {
            "number": 12,
            "title": "[FB房源] 桃園區四房",
            "body": """
### Facebook 永久貼文網址

https://www.facebook.com/groups/4091621327828556/posts/4623380861319264/

### 完整貼文文字

桃園區大有路，4房2廳2衛，租金23,000元。

### 公開照片網址

https://www.facebook.com/photo?fbid=27910753271851144
""",
        }

        row = DIGEST.parse_facebook_issue_body(issue)

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(
            row["url"],
            "https://www.facebook.com/groups/4091621327828556/"
            "posts/4623380861319264/",
        )
        self.assertIn("4房2廳2衛", row["post_text"])
        self.assertIn("facebook.com/photo", row["image"])
        self.assertEqual(row["_submission_source"], "GitHub issue #12")

    def test_anonymous_public_metadata_fills_fields_and_archives_real_image(
        self,
    ) -> None:
        post_url = (
            "https://www.facebook.com/groups/4091621327828556/"
            "posts/4623380861319264/"
        )
        public_html = """
<html><head>
<meta property="og:url" content="https://www.facebook.com/groups/4091621327828556/posts/4623380861319264/">
<meta property="og:image" content="https://scontent.ftpe8-1.fna.fbcdn.net/real.jpg">
<meta property="og:description" content="🏡【桃園區｜冠倫大國｜46坪大四房】
📍地點：桃園區大有路｜冠倫大國社區
💰租金：23,000元／月
💰管理費：2,000元／月
🚗停車位：3,000元／月
4房2廳2衛，約46坪，家具家電全配，可租補、可入戶籍、可養寵物">
</head><body>
<script>
{"node_v2":{"actors":[{"name":"林思妤"}],"creation_time":1783950237,
"seo_title":"桃園區｜冠倫大國｜46坪大四房",
"post_id":"4623380861319264"}}
</script>
<!-- xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx -->
</body></html>
"""
        page_response = Mock(
            status_code=200,
            text=public_html,
            url=post_url,
        )
        image_response = Mock(
            status_code=200,
            headers={"Content-Type": "image/jpeg"},
            content=b"\xff\xd8" + (b"x" * 1200),
        )
        stats = DIGEST.empty_source_stats()

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(DIGEST, "FB_ASSET_DIR", Path(directory)),
                patch.object(
                    DIGEST.requests,
                    "get",
                    side_effect=[page_response, image_response],
                ),
            ):
                row = DIGEST.enrich_facebook_row(
                    {
                        "url": post_url,
                        "_submission_source": "GitHub issue #12",
                    },
                    stats,
                )
                archived = Path(directory) / "4623380861319264.jpg"
                self.assertTrue(archived.exists())

        self.assertEqual(row["publisher"], "林思妤")
        self.assertEqual(row["updated"], "2026/07/13 21:43刊登")
        self.assertEqual(row["district"], "桃園區")
        self.assertEqual(row["layout"], "4房2廳2衛")
        self.assertEqual(row["size"], "46坪")
        self.assertEqual(row["rent"], 23_000)
        self.assertEqual(row["total_cost"], 28_000)
        self.assertTrue(row["image"].endswith("/4623380861319264.jpg"))
        self.assertEqual(stats["public_metadata_enriched"], 1)
        self.assertEqual(stats["images_archived"], 1)
        self.assertEqual(DIGEST.facebook_row_reject_reasons(row), [])

    def test_github_open_issue_source_uses_token_without_facebook_session(
        self,
    ) -> None:
        issue = {
            "number": 7,
            "title": "[FB房源] 桃園四房",
            "body": """
### Facebook 永久貼文網址

https://www.facebook.com/groups/4091621327828556/posts/4623380861319264/
""",
        }
        response = Mock(status_code=200)
        response.json.return_value = [issue]
        stats = DIGEST.empty_source_stats()
        with (
            patch.dict(
                os.environ,
                {
                    DIGEST.GITHUB_REPOSITORY_ENV: "FlySpacesky/taoyuan-rental-digest",
                    DIGEST.GITHUB_TOKEN_ENV: "test-github-token",
                },
                clear=False,
            ),
            patch.object(DIGEST.requests, "get", return_value=response) as get,
        ):
            rows = DIGEST.load_github_facebook_issue_rows(stats)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["_submission_source"], "GitHub issue #7")
        self.assertTrue(stats["issue_source_enabled"])
        self.assertEqual(stats["issue_submissions_seen"], 1)
        headers = get.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-github-token")

    def test_private_inbox_uses_bearer_read_token_and_requires_republish_consent(
        self,
    ) -> None:
        allowed = {
            "url": "https://www.facebook.com/groups/987654321/posts/1234567890123/",
            "post_text": "桃園區4房2廳屋主出租",
            "published_at": DIGEST.NOW.isoformat(),
            "republish_authorized": True,
        }
        denied = {
            "url": "https://www.facebook.com/groups/987654321/posts/1234567890124/",
            "post_text": "中壢區4房2廳屋主出租",
            "published_at": DIGEST.NOW.isoformat(),
            "republish_authorized": False,
        }
        payload = {"posts": [allowed, denied]}
        response = Mock(status_code=200)
        response.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        response.json.return_value = payload
        stats = DIGEST.empty_source_stats()

        with (
            patch.dict(
                os.environ,
                {DIGEST.FB_PRIVATE_INBOX_TOKEN_ENV: "private-read-test-token"},
                clear=False,
            ),
            patch.object(DIGEST.requests, "get", return_value=response) as get,
        ):
            rows = DIGEST.load_private_facebook_inbox_rows(stats)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["_import_source"], "Cloudflare私人收件匣")
        self.assertEqual(stats["private_inbox_rows"], 1)
        self.assertTrue(stats["private_inbox_reachable"])
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"],
            "Bearer private-read-test-token",
        )
        self.assertNotIn("private-read-test-token", " ".join(stats["errors"]))

    def test_private_inbox_is_optional_when_read_token_is_absent(self) -> None:
        stats = DIGEST.empty_source_stats()
        with patch.dict(
            os.environ,
            {DIGEST.FB_PRIVATE_INBOX_TOKEN_ENV: ""},
            clear=False,
        ):
            rows = DIGEST.load_private_facebook_inbox_rows(stats)

        self.assertEqual(rows, [])
        self.assertFalse(stats["private_inbox_enabled"])
        self.assertEqual(stats["errors"], [])

    def test_supplied_taoyuan_four_room_post_accepts_archived_public_image(
        self,
    ) -> None:
        row = {
            "url": (
                "https://www.facebook.com/groups/4091621327828556/"
                "permalink/4623380861319264/"
            ),
            "title": "桃園區冠倫大國46坪大四房",
            "district": "桃園區",
            "address": "桃園區大有路｜冠倫大國社區",
            "house_type": "整層住家",
            "layout": "4房2廳2衛",
            "size": "46坪",
            "equipment": "家具家電全配、可養寵物",
            "rent": "23000",
            "total_cost": "28000",
            "image": (
                "https://flyspacesky.github.io/taoyuan-rental-digest/"
                "assets/facebook/4623380861319264.jpg"
            ),
            "summary": "可租補、可入戶籍，管理費2000元，停車位3000元。",
        }

        self.assertEqual(DIGEST.facebook_row_reject_reasons(row), [])
        item = DIGEST.parse_social_row(row, "FB")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.rent, 23_000)
        self.assertEqual(item.total_cost, 28_000)
        self.assertEqual(item.category_hint, "general")

    def test_repo_facebook_import_keeps_current_public_post_and_excludes_old_post(self) -> None:
        stats = DIGEST.empty_source_stats()
        public_url = (
            "https://www.facebook.com/groups/468627751712411/"
            "posts/1578861454022363/"
        )
        metadata = {
            "url": public_url,
            "canonical_url": public_url,
            "post_text": (
                "桃園區藝文特區電梯3房出租，3房2廳2衛，34.03坪，"
                "租金20000元，一般房屋仲介刊登。"
            ),
            "image_origin": "https://scontent.ftpe8-1.fna.fbcdn.net/current.jpg",
            "publisher": "一般房仲",
            "creation_time": int(DIGEST.NOW.timestamp()),
            "title": "桃園區藝文特區電梯三房出租",
            "publicly_readable": True,
        }
        with (
            patch.object(DIGEST, "fetch_public_facebook_metadata", return_value=metadata),
            patch.object(
                DIGEST,
                "archive_facebook_image",
                return_value=(
                    "https://flyspacesky.github.io/taoyuan-rental-digest/"
                    "assets/facebook/1578861454022363.jpg"
                ),
            ),
        ):
            items = DIGEST.load_facebook_import(stats)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, public_url)
        self.assertEqual(items[0].fb_lead_grade, "B")
        self.assertEqual(stats["import_source"], "data/facebook_posts.json")
        self.assertEqual(stats["discovery_groups"], len(DIGEST.FB_GROUPS))
        self.assertEqual(stats["anonymous_verified_posts"], 1)
        self.assertEqual(stats["candidate_links"], 2)
        self.assertEqual(stats["rejects"]["outside_collection_window"], 1)
        self.assertIn("不是社團全部貼文數", stats["notices"][0])

    def test_file_and_actions_secret_are_merged_instead_of_shadowed(self) -> None:
        def row(post_id: str, district: str) -> dict[str, str]:
            return {
                "url": f"{DIGEST.FB_GROUPS[0]}/posts/{post_id}/",
                "title": f"{district}四房整層住家",
                "district": district,
                "address": f"{district}中正路",
                "house_type": "整層住家",
                "layout": "4房2廳",
                "rent": "32000",
                "publisher": "屋主林先生",
                "published_at": DIGEST.NOW.isoformat(),
                "updated": "剛剛刊登",
                "image": f"https://images.example.test/{post_id}.jpg",
                "post_text": f"{district} 4房2廳 租金32000 屋主自租",
            }

        file_row = row("12345678901", "桃園區")
        secret_row = row("12345678902", "中壢區")
        stats = DIGEST.empty_source_stats()
        with tempfile.TemporaryDirectory() as directory:
            import_file = Path(directory) / "facebook.json"
            import_file.write_text(
                json.dumps([file_row], ensure_ascii=False),
                encoding="utf-8",
            )
            with (
                patch.object(DIGEST, "FB_IMPORT", import_file),
                patch.dict(
                    os.environ,
                    {
                        DIGEST.FB_IMPORT_ENV: json.dumps(
                            [secret_row],
                            ensure_ascii=False,
                        ),
                        DIGEST.FB_IMPORT_URL_ENV: "",
                        DIGEST.GITHUB_REPOSITORY_ENV: "",
                        DIGEST.GITHUB_TOKEN_ENV: "",
                    },
                    clear=False,
                ),
            ):
                items = DIGEST.load_facebook_import(stats)

        self.assertEqual(len(items), 2)
        self.assertEqual(stats["input_rows"], 2)
        self.assertEqual(stats["candidate_links"], 2)
        self.assertIn("data/facebook_posts.json", stats["import_source"])
        self.assertIn(DIGEST.FB_IMPORT_ENV, stats["import_source"])

    def test_real_row_supports_listing_sort_fields(self) -> None:
        row = {
            "url": f"{DIGEST.FB_GROUPS[0]}/posts/1234567890/",
            "title": "桃園區四房整層住家",
            "district": "桃園區",
            "address": "桃園區中正路",
            "house_type": "整層住家",
            "layout": "4房2廳",
            "size": "35坪",
            "rent": "32000",
            "old_rent": "35000",
            "updated": "2小時前更新",
            "views": "88人瀏覽",
            "publisher": "屋主林先生",
            "image": "https://images.example.test/real-authorized-photo.jpg",
            "summary": "屋主自租，仲介勿擾。",
        }

        item = DIGEST.parse_social_row(row, "FB")

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.size, "35坪")
        self.assertEqual(item.old_rent, 35_000)
        self.assertEqual(item.updated, "2小時前更新")
        self.assertEqual(item.views, "88人瀏覽")
        self.assertEqual(item.publisher, "屋主林先生")

    def test_public_unlisted_group_three_room_post_is_accepted_without_optional_fields(
        self,
    ) -> None:
        row = {
            "url": "https://www.facebook.com/groups/987654321/posts/1234567890123/",
            "title": "八德區三房整層出租",
            "district": "八德區",
            "layout": "3房2廳2衛",
            "post_text": "八德區3房2廳2衛整層出租，仲介勿擾",
        }

        self.assertEqual(DIGEST.facebook_row_reject_reasons(row), [])
        item = DIGEST.parse_social_row(row, "FB", DIGEST.NOW)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.rent, 0)
        self.assertEqual(item.image, "")
        self.assertIn("needs_info", item.filter_tags)

    def test_facebook_property_management_industry_is_excluded(self) -> None:
        row = {
            "url": "https://www.facebook.com/groups/987654321/posts/1234567890124/",
            "title": "桃園區四房出租",
            "district": "桃園區",
            "layout": "4房2廳2衛",
            "post_text": "桃園區4房出租，包租代管公司，歡迎房東委託",
        }

        self.assertIn("excluded_industry", DIGEST.facebook_row_reject_reasons(row))
        self.assertIsNone(DIGEST.parse_social_row(row, "FB", DIGEST.NOW))

    def test_facebook_ordinary_broker_listing_is_kept_as_c_grade(self) -> None:
        row = {
            "url": "https://www.facebook.com/groups/987654321/posts/1234567890128/",
            "title": "桃園區藝文特區電梯三房出租",
            "district": "桃園區",
            "layout": "3房2廳2衛",
            "size": "34.03坪",
            "rent": "20000",
            "publisher": "中信房屋桃園中正捷運加盟店",
            "post_text": (
                "桃園區藝文特區電梯3房出租，3房2廳2衛，34.03坪，租金20000元。"
                "一般房屋仲介刊登，成交後收取仲介服務費，附不動產經紀營業員證號。"
            ),
        }

        self.assertNotIn("excluded_industry", DIGEST.facebook_row_reject_reasons(row))
        item = DIGEST.parse_social_row(row, "FB", DIGEST.NOW)

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.fb_lead_grade, "C")
        self.assertEqual(DIGEST.listing_filter_tokens(item), ["all", "lead_c"])

    def test_owner_seeking_management_is_kept_and_ranked_a(self) -> None:
        row = {
            "url": "https://www.facebook.com/groups/987654321/posts/1234567890125/",
            "title": "中壢區四房屋主自租",
            "district": "中壢區",
            "layout": "4房2廳2衛",
            "size": "38坪",
            "rent": "28000",
            "publisher": "屋主林先生",
            "image": "https://scontent.ftpe8-1.fna.fbcdn.net/owner-home.jpg",
            "post_text": (
                "屋主自租，中壢區4房整層出租，前房客剛搬走，房東人在外地，"
                "沒時間管理，想找包租代管協助。"
            ),
        }

        self.assertFalse(
            DIGEST.facebook_industry_listing(
                row["post_text"],
                row["publisher"],
            )
        )
        self.assertEqual(DIGEST.facebook_row_reject_reasons(row), [])
        item = DIGEST.parse_social_row(row, "FB", DIGEST.NOW)

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.fb_lead_grade, "A")
        self.assertIn("lead_a", item.filter_tags)
        self.assertIn("management_need", item.filter_tags)
        self.assertIn("vacant", item.filter_tags)
        self.assertEqual(DIGEST.listing_filter_tokens(item), ["all", "lead_a"])
        self.assertIn("A級房源", DIGEST.render_card(item))
        self.assertIn("屋主尋求代管", DIGEST.render_card(item))

    def test_fb_owner_without_management_pain_is_ranked_b(self) -> None:
        row = {
            "url": "https://www.facebook.com/groups/987654321/posts/1234567890126/",
            "title": "平鎮區三房出租",
            "district": "平鎮區",
            "layout": "3房2廳",
            "rent": "23000",
            "publisher": "屋主陳小姐",
            "image": "https://scontent.ftpe8-1.fna.fbcdn.net/owner-home-b.jpg",
            "post_text": "屋主自租，平鎮區3房出租，仲介勿擾。",
        }

        item = DIGEST.parse_social_row(row, "FB", DIGEST.NOW)

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.fb_lead_grade, "B")
        self.assertEqual(DIGEST.listing_filter_tokens(item), ["all", "lead_b"])

    def test_fb_non_owner_verified_rental_is_ranked_c(self) -> None:
        row = {
            "url": "https://www.facebook.com/groups/987654321/posts/1234567890127/",
            "title": "八德區四房出租",
            "district": "八德區",
            "layout": "4房2廳",
            "size": "35坪",
            "rent": "26000",
            "image": "https://scontent.ftpe8-1.fna.fbcdn.net/home-c.jpg",
            "post_text": "八德區4房出租，近大湳商圈。",
        }

        item = DIGEST.parse_social_row(row, "FB", DIGEST.NOW)

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.fb_lead_grade, "C")
        self.assertEqual(DIGEST.listing_filter_tokens(item), ["all", "lead_c"])

    def test_fb_property_management_signature_stays_excluded(self) -> None:
        text = (
            "桃園區4房出租，包租代管公司，歡迎房東委託。"
            "租賃住宅服務業，提供代租代管服務。"
        )

        self.assertTrue(DIGEST.facebook_industry_listing(text, "安心包租代管公司"))

    def test_supplied_share_post_reports_every_rejection_reason(self) -> None:
        row = {
            "url": "https://www.facebook.com/share/p/1EDpLMRgBC/",
            "title": "中壢區環中東路二段大空間出租",
            "district": "中壢區",
            "size": "50坪",
            "rent": "28000",
            "image": (
                "https://www.facebook.com/photo?"
                "fbid=122316970610218602&set=pcb.4332511403559617"
            ),
            "summary": "可自住可辦公，成交後收半個月服務費，歡迎房東委託包租代管。",
        }

        reasons = set(DIGEST.facebook_row_reject_reasons(row))

        self.assertIn("invalid_permanent_url", reasons)
        self.assertIn("not_three_rooms", reasons)
        self.assertIn("excluded_industry", reasons)
        self.assertNotIn("image_not_direct_public", reasons)
        self.assertIsNone(DIGEST.parse_social_row(row, "FB"))

        stats = DIGEST.empty_source_stats()
        missing = ROOT / "data" / "__missing_facebook_posts__.json"
        with (
            patch.object(DIGEST, "FB_IMPORT", missing),
            patch.dict(
                os.environ,
                {
                    DIGEST.FB_IMPORT_ENV: json.dumps([row], ensure_ascii=False),
                    DIGEST.FB_IMPORT_URL_ENV: "",
                },
                clear=False,
            ),
        ):
            items = DIGEST.load_facebook_import(stats)

        self.assertEqual(items, [])
        self.assertEqual(stats["candidate_links"], 0)
        self.assertEqual(stats["input_rows"], 1)
        self.assertEqual(stats["rejects"]["not_three_rooms"], 1)
        self.assertEqual(stats["rejects"]["excluded_industry"], 1)
        self.assertEqual(len(stats["errors"]), 1)

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


class ThreadsImportTests(unittest.TestCase):
    def test_reply_permission_probe_records_unavailable_without_content(self) -> None:
        stats = DIGEST.empty_source_stats()
        response = Mock(status_code=403)
        response.json.return_value = {"error": {"message": "missing permission"}}
        with patch.object(DIGEST.requests, "get", return_value=response):
            DIGEST.probe_threads_reply_access("test-token", stats)

        self.assertEqual(stats["reply_permission"], "unavailable_http_403")
        self.assertIn("HTTP 403", stats["reply_permission_error"])
        self.assertTrue(any("threads_read_replies" in row for row in stats["notices"]))

    def test_missing_access_token_reports_actionable_error(self) -> None:
        stats = DIGEST.empty_source_stats()
        with (
            patch.object(DIGEST, "THREADS_IMPORT", ROOT / "data" / "__missing_threads__.json"),
            patch.dict(
                os.environ,
                {
                    DIGEST.THREADS_ACCESS_TOKEN_ENV: "",
                    DIGEST.THREADS_IMPORT_ENV: "",
                    DIGEST.THREADS_IMPORT_URL_ENV: "",
                    DIGEST.GITHUB_REPOSITORY_ENV: "",
                    DIGEST.GITHUB_TOKEN_ENV: "",
                },
                clear=False,
            ),
        ):
            items = DIGEST.load_threads_listings(stats)

        self.assertEqual(items, [])
        self.assertEqual(stats["candidate_links"], 0)
        self.assertEqual(stats["validated"], 0)
        self.assertIn(DIGEST.THREADS_ACCESS_TOKEN_ENV, stats["errors"][0])
        self.assertIn("threads_keyword_search", stats["errors"][0])

    def test_manual_public_threads_feed_accepts_three_rooms_without_rent_or_photo(
        self,
    ) -> None:
        stats = DIGEST.empty_source_stats()
        row = {
            "permalink": "https://www.threads.com/@owner.home/post/THREE_ROOM",
            "text": "中壢區整層出租，3房2廳2衛，室內約32坪，仲介勿擾",
            "published_at": DIGEST.NOW.isoformat(),
        }
        with (
            patch.object(DIGEST, "THREADS_IMPORT", ROOT / "data" / "__missing_threads__.json"),
            patch.dict(
                os.environ,
                {
                    DIGEST.THREADS_ACCESS_TOKEN_ENV: "",
                    DIGEST.THREADS_IMPORT_ENV: json.dumps([row], ensure_ascii=False),
                    DIGEST.THREADS_IMPORT_URL_ENV: "",
                    DIGEST.GITHUB_REPOSITORY_ENV: "",
                    DIGEST.GITHUB_TOKEN_ENV: "",
                },
                clear=False,
            ),
        ):
            items = DIGEST.load_threads_listings(stats, {})

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].district, "中壢區")
        self.assertEqual(items[0].layout, "3房2廳2衛")
        self.assertEqual(items[0].rent, 0)
        self.assertEqual(items[0].image, "")
        self.assertIn("spacious", items[0].filter_tags)
        self.assertIn("needs_info", items[0].filter_tags)
        self.assertEqual(stats["missing_rent_accepted"], 1)
        self.assertEqual(stats["missing_photo_accepted"], 1)

    def test_manual_threads_feed_excludes_property_management_industry(self) -> None:
        stats = DIGEST.empty_source_stats()
        row = {
            "permalink": "https://www.threads.com/@agency/post/MANAGED_HOME",
            "text": "八德區4房2廳出租，40坪，包租代管公司，歡迎房東委託",
            "published_at": DIGEST.NOW.isoformat(),
        }
        with (
            patch.object(DIGEST, "THREADS_IMPORT", ROOT / "data" / "__missing_threads__.json"),
            patch.dict(
                os.environ,
                {
                    DIGEST.THREADS_ACCESS_TOKEN_ENV: "",
                    DIGEST.THREADS_IMPORT_ENV: json.dumps([row], ensure_ascii=False),
                    DIGEST.THREADS_IMPORT_URL_ENV: "",
                    DIGEST.GITHUB_REPOSITORY_ENV: "",
                    DIGEST.GITHUB_TOKEN_ENV: "",
                },
                clear=False,
            ),
        ):
            items = DIGEST.load_threads_listings(stats, {})

        self.assertEqual(items, [])
        self.assertEqual(stats["rejects"]["excluded_industry"], 1)

    def test_threads_always_rejects_rows_older_than_seven_days(self) -> None:
        row = {
            "permalink": "https://www.threads.com/@owner.home/post/SIX_DAYS_OLD",
            "text": "平鎮區3房2廳整層出租，35坪",
            "published_at": (DIGEST.NOW - DIGEST.timedelta(days=8)).isoformat(),
        }
        state: dict[str, object] = {}
        common_env = {
            DIGEST.THREADS_ACCESS_TOKEN_ENV: "",
            DIGEST.THREADS_IMPORT_ENV: json.dumps([row], ensure_ascii=False),
            DIGEST.THREADS_IMPORT_URL_ENV: "",
            DIGEST.GITHUB_REPOSITORY_ENV: "",
            DIGEST.GITHUB_TOKEN_ENV: "",
        }
        with (
            patch.object(DIGEST, "THREADS_IMPORT", ROOT / "data" / "__missing_threads__.json"),
            patch.dict(os.environ, common_env, clear=False),
        ):
            first_stats = DIGEST.empty_source_stats()
            first_items = DIGEST.load_threads_listings(first_stats, state)
            second_stats = DIGEST.empty_source_stats()
            second_items = DIGEST.load_threads_listings(second_stats, state)

        self.assertEqual(first_items, [])
        self.assertEqual(first_stats["collection_mode"], "initial")
        self.assertEqual(first_stats["window_days"], 7)
        self.assertEqual(first_stats["rejects"]["outside_collection_window"], 1)
        self.assertEqual(second_items, [])
        self.assertEqual(second_stats["collection_mode"], "ongoing")
        self.assertEqual(second_stats["window_days"], 7)
        self.assertEqual(second_stats["rejects"]["outside_collection_window"], 1)

    def test_threads_issue_form_creates_manual_public_candidate(self) -> None:
        issue = {
            "number": 21,
            "title": "[Threads房源] 中壢三房",
            "created_at": DIGEST.NOW.isoformat(),
            "body": """
### Threads 永久貼文網址

https://www.threads.com/@owner.home/post/ISSUE_HOME

### 完整貼文文字

中壢區3房2廳出租，室內35坪

### 貼文時間

2026-08-12T09:30:00+08:00
""",
        }
        row = DIGEST.parse_threads_issue_body(issue)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["_submission_source"], "GitHub issue #21")
        self.assertEqual(row["submitted_at"], DIGEST.NOW.isoformat())

    def test_search_uses_keyword_and_tag_plans_with_recent_and_top(self) -> None:
        stats = DIGEST.empty_source_stats()
        recorded_params: list[dict[str, object]] = []

        def search_response(*args: object, **kwargs: object) -> Mock:
            recorded_params.append(dict(kwargs["params"]))
            response = Mock(status_code=200)
            response.json.return_value = {"data": []}
            return response

        with (
            patch.object(
                DIGEST,
                "THREADS_SEARCH_PLANS",
                (("KEYWORD", "桃園"), ("TAG", "桃園租屋")),
            ),
            patch.object(DIGEST, "THREADS_SEARCH_TYPES", ("RECENT", "TOP")),
            patch.object(DIGEST.requests, "get", side_effect=search_response),
        ):
            rows = DIGEST.fetch_threads_search_rows("test-token", stats)

        self.assertEqual(rows, [])
        self.assertEqual(
            {
                (params["search_mode"], params["q"], params["search_type"])
                for params in recorded_params
            },
            {
                ("KEYWORD", "桃園", "RECENT"),
                ("TAG", "桃園租屋", "RECENT"),
                ("KEYWORD", "桃園", "TOP"),
                ("TAG", "桃園租屋", "TOP"),
            },
        )
        self.assertTrue(all(" " not in str(params["q"]) for params in recorded_params))
        self.assertTrue(all("has_replies" in str(params["fields"]) for params in recorded_params))
        self.assertTrue(all("is_reply" not in str(params["fields"]) for params in recorded_params))
        self.assertEqual(stats["api_pages"], 4)
        self.assertEqual(stats["raw_rows"], 0)
        self.assertEqual(stats["query_results"]["KEYWORD:RECENT:桃園"], 0)
        self.assertEqual(stats["query_results"]["TAG:TOP:桃園租屋"], 0)

    def test_search_uses_after_cursor_for_second_page(self) -> None:
        stats = DIGEST.empty_source_stats()
        first = Mock(status_code=200)
        first.json.return_value = {
            "data": [
                {
                    "id": "180123456789",
                    "permalink": "https://www.threads.com/@home/post/ABC123",
                }
            ],
            "paging": {"cursors": {"after": "cursor-2"}},
        }
        second = Mock(status_code=200)
        second.json.return_value = {"data": []}

        with (
            patch.object(
                DIGEST,
                "THREADS_SEARCH_PLANS",
                (("KEYWORD", "桃園"),),
            ),
            patch.object(DIGEST, "THREADS_SEARCH_TYPES", ("RECENT",)),
            patch.object(DIGEST, "THREADS_SEARCH_MAX_PAGES", 2),
            patch.object(
                DIGEST.requests,
                "get",
                side_effect=[first, second],
            ) as request,
        ):
            rows = DIGEST.fetch_threads_search_rows("test-token", stats)

        self.assertEqual(len(rows), 1)
        self.assertEqual(request.call_count, 2)
        self.assertNotIn("after", request.call_args_list[0].kwargs["params"])
        self.assertEqual(
            request.call_args_list[1].kwargs["params"]["after"],
            "cursor-2",
        )
        self.assertEqual(stats["api_pages"], 2)
        self.assertEqual(stats["raw_rows"], 1)
        self.assertEqual(stats["query_results"]["KEYWORD:RECENT:桃園"], 1)

    def test_official_search_keeps_taoyuan_four_room_and_all_photos(self) -> None:
        stats = DIGEST.empty_source_stats()
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [
                {
                    "id": "180123456789",
                    "media_type": "CAROUSEL_ALBUM",
                    "permalink": (
                        "https://www.threads.net/@real.home/post/ABC_123"
                    ),
                    "username": "real.home",
                    "text": (
                        "桃園區大有路四房出租\n"
                        "租金：25,000元\n"
                        "4房2廳2衛，約46坪，電梯大樓"
                    ),
                    "timestamp": DIGEST.NOW.isoformat(),
                    "children": {
                        "data": [
                            {
                                "media_type": "IMAGE",
                                "media_url": (
                                    "https://scontent.cdninstagram.com/one.jpg"
                                ),
                            },
                            {
                                "media_type": "IMAGE",
                                "media_url": (
                                    "https://scontent.cdninstagram.com/two.jpg"
                                ),
                            },
                        ]
                    },
                }
            ]
        }

        def archive_all(
            post_id: str,
            image_urls: list[str],
            source_stats: dict[str, object],
        ) -> list[str]:
            source_stats["images_archived"] = len(image_urls)
            return [
                f"https://example.test/assets/threads/{post_id}-{index:02d}.jpg"
                for index in range(1, len(image_urls) + 1)
            ]

        with (
            patch.dict(
                os.environ,
                {DIGEST.THREADS_ACCESS_TOKEN_ENV: "test-token"},
                clear=False,
            ),
            patch.object(DIGEST.requests, "get", return_value=response),
            patch.object(
                DIGEST,
                "archive_threads_images",
                side_effect=archive_all,
            ),
        ):
            items = DIGEST.load_threads_listings(stats)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.source, "Threads")
        self.assertEqual(item.district, "桃園區")
        self.assertEqual(item.layout, "4房2廳2衛")
        self.assertEqual(item.rent, 25_000)
        self.assertEqual(item.total_cost, 25_000)
        self.assertEqual(item.publisher, "@real.home")
        self.assertEqual(
            item.url,
            "https://www.threads.com/@real.home/post/ABC_123",
        )
        self.assertEqual(len(item.images), 2)
        self.assertEqual(item.image, item.images[0])
        self.assertEqual(stats["candidate_links"], 1)
        self.assertEqual(stats["validated"], 1)
        self.assertEqual(stats["images_found"], 2)
        self.assertEqual(stats["images_archived"], 2)

        rendered = DIGEST.render_card(item)
        self.assertIn('data-photo-count="2"', rendered)
        self.assertEqual(rendered.count('class="photo gallery-photo"'), 2)
        self.assertIn(">1/2</span>", rendered)
        self.assertIn(">2/2</span>", rendered)

    def test_author_reply_can_complete_listing_and_missing_rent_is_allowed(self) -> None:
        stats = DIGEST.empty_source_stats()
        main_response = Mock(status_code=200)
        main_response.json.return_value = {
            "data": [
                {
                    "id": "180123456700",
                    "media_type": "IMAGE",
                    "media_url": "https://scontent.cdninstagram.com/main.jpg",
                    "permalink": (
                        "https://www.threads.com/@real.home/post/REPLY_DETAILS"
                    ),
                    "username": "real.home",
                    "text": "房屋出租，完整資訊補在留言",
                    "timestamp": (DIGEST.NOW - DIGEST.timedelta(days=3)).isoformat(),
                    "has_replies": True,
                }
            ]
        }
        conversation_response = Mock(status_code=200)
        conversation_response.json.return_value = {
            "data": [
                {
                    "id": "180123456701",
                    "is_reply": True,
                    "username": "real.home",
                    "text": "地點：桃園區大有路\n4房2廳2衛，約46坪",
                    "timestamp": (DIGEST.NOW - DIGEST.timedelta(days=1)).isoformat(),
                },
                {
                    "id": "180123456702",
                    "is_reply": True,
                    "username": "someone.else",
                    "text": "中壢區三房，租金100元",
                    "timestamp": DIGEST.NOW.isoformat(),
                },
            ]
        }

        def threads_get(url: str, **kwargs: object) -> Mock:
            if url.endswith("/conversation"):
                return conversation_response
            return main_response

        with (
            patch.dict(
                os.environ,
                {DIGEST.THREADS_ACCESS_TOKEN_ENV: "test-token"},
                clear=False,
            ),
            patch.object(DIGEST, "THREADS_SEARCH_PLANS", (("KEYWORD", "桃園"),)),
            patch.object(DIGEST, "THREADS_SEARCH_TYPES", ("RECENT",)),
            patch.object(DIGEST.requests, "get", side_effect=threads_get),
            patch.object(
                DIGEST,
                "archive_threads_images",
                return_value=["https://example.test/assets/threads/main.jpg"],
            ),
        ):
            items = DIGEST.load_threads_listings(stats)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.district, "桃園區")
        self.assertEqual(item.layout, "4房2廳2衛")
        self.assertEqual(item.rent, 0)
        self.assertEqual(item.total_cost, 0)
        self.assertIn("原作者留言：地點：桃園區大有路", item.raw_text)
        self.assertNotIn("someone.else", item.raw_text)
        self.assertNotIn("租金100元", item.raw_text)
        self.assertEqual(stats["author_reply_rows"], 1)
        self.assertEqual(stats["missing_rent_accepted"], 1)
        self.assertEqual(stats["reply_api_attempts"], 1)
        self.assertEqual(stats["candidate_diagnostics"][0]["reasons"], [])
        self.assertIn("租金洽詢", DIGEST.render_card(item))

    def test_keyword_search_reply_is_grouped_under_its_root_post(self) -> None:
        stats = DIGEST.empty_source_stats()
        search_response = Mock(status_code=200)
        search_response.json.return_value = {
            "data": [
                {
                    "id": "reply-100",
                    "is_reply": True,
                    "root_post": {"id": "root-100"},
                    "username": "real.home",
                    "text": "地點：桃園區\n4房2廳2衛",
                    "timestamp": DIGEST.NOW.isoformat(),
                }
            ]
        }
        root_response = Mock(status_code=200)
        root_response.json.return_value = {
            "id": "root-100",
            "media_type": "IMAGE",
            "media_url": "https://scontent.cdninstagram.com/root.jpg",
            "permalink": "https://www.threads.com/@real.home/post/ROOT_100",
            "username": "real.home",
            "text": "房屋出租，詳細條件請看留言",
            "timestamp": (DIGEST.NOW - DIGEST.timedelta(days=2)).isoformat(),
        }

        def threads_get(url: str, **kwargs: object) -> Mock:
            if url.endswith("/keyword_search"):
                return search_response
            return root_response

        with (
            patch.dict(
                os.environ,
                {DIGEST.THREADS_ACCESS_TOKEN_ENV: "test-token"},
                clear=False,
            ),
            patch.object(DIGEST, "THREADS_SEARCH_PLANS", (("KEYWORD", "桃園"),)),
            patch.object(DIGEST, "THREADS_SEARCH_TYPES", ("RECENT",)),
            patch.object(DIGEST.requests, "get", side_effect=threads_get),
            patch.object(
                DIGEST,
                "archive_threads_images",
                return_value=["https://example.test/assets/threads/root.jpg"],
            ),
        ):
            items = DIGEST.load_threads_listings(stats)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_id, "real.home:ROOT_100")
        self.assertEqual(stats["search_reply_rows"], 1)
        self.assertEqual(stats["root_post_requests"], 1)
        self.assertEqual(stats["author_reply_rows"], 1)

    def test_threads_post_older_than_seven_days_without_activity_is_rejected(self) -> None:
        stats = DIGEST.empty_source_stats()
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [
                {
                    "id": "180123456799",
                    "media_type": "IMAGE",
                    "media_url": "https://scontent.cdninstagram.com/old.jpg",
                    "permalink": "https://www.threads.com/@real.home/post/OLD_POST",
                    "username": "real.home",
                    "text": "桃園區四房出租\n4房2廳2衛",
                    "timestamp": (DIGEST.NOW - DIGEST.timedelta(days=8)).isoformat(),
                }
            ]
        }
        with (
            patch.dict(
                os.environ,
                {DIGEST.THREADS_ACCESS_TOKEN_ENV: "test-token"},
                clear=False,
            ),
            patch.object(DIGEST, "THREADS_SEARCH_PLANS", (("KEYWORD", "桃園"),)),
            patch.object(DIGEST, "THREADS_SEARCH_TYPES", ("RECENT",)),
            patch.object(DIGEST.requests, "get", return_value=response),
            patch.object(DIGEST, "archive_threads_images") as archive,
        ):
            items = DIGEST.load_threads_listings(stats)

        self.assertEqual(items, [])
        self.assertEqual(stats["rejects"]["outside_collection_window"], 1)
        self.assertIn(
            "outside_collection_window",
            stats["candidate_diagnostics"][0]["reasons"],
        )
        archive.assert_not_called()

    def test_reply_permission_failure_does_not_hide_valid_main_post(self) -> None:
        stats = DIGEST.empty_source_stats()
        search_response = Mock(status_code=200)
        search_response.json.return_value = {
            "data": [
                {
                    "id": "180123456788",
                    "media_type": "IMAGE",
                    "media_url": "https://scontent.cdninstagram.com/current.jpg",
                    "permalink": "https://www.threads.com/@real.home/post/CURRENT",
                    "username": "real.home",
                    "text": "桃園區四房出租\n4房2廳2衛",
                    "timestamp": DIGEST.NOW.isoformat(),
                    "has_replies": True,
                }
            ]
        }
        denied_response = Mock(status_code=403)
        denied_response.json.return_value = {"error": {"message": "not permitted"}}

        def threads_get(url: str, **kwargs: object) -> Mock:
            if url.endswith("/conversation"):
                return denied_response
            return search_response

        with (
            patch.dict(
                os.environ,
                {DIGEST.THREADS_ACCESS_TOKEN_ENV: "test-token"},
                clear=False,
            ),
            patch.object(DIGEST, "THREADS_SEARCH_PLANS", (("KEYWORD", "桃園"),)),
            patch.object(DIGEST, "THREADS_SEARCH_TYPES", ("RECENT",)),
            patch.object(DIGEST.requests, "get", side_effect=threads_get),
            patch.object(
                DIGEST,
                "archive_threads_images",
                return_value=["https://example.test/assets/threads/current.jpg"],
            ),
        ):
            items = DIGEST.load_threads_listings(stats)

        self.assertEqual(len(items), 1)
        self.assertTrue(stats["reply_access_limited"])
        self.assertEqual(stats["reply_http_statuses"]["403"], 1)
        self.assertTrue(any("Meta官方" in notice for notice in stats["notices"]))

    def test_outside_district_and_under_three_rooms_is_rejected(self) -> None:
        stats = DIGEST.empty_source_stats()
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [
                {
                    "id": "180999999999",
                    "media_type": "IMAGE",
                    "media_url": "https://scontent.cdninstagram.com/three.jpg",
                    "permalink": (
                        "https://www.threads.com/@real.home/post/NOT_TAOYUAN"
                    ),
                    "username": "real.home",
                    "text": "龜山區兩房出租\n租金：20,000元\n2房2廳1衛",
                    "timestamp": DIGEST.NOW.isoformat(),
                }
            ]
        }
        with (
            patch.dict(
                os.environ,
                {DIGEST.THREADS_ACCESS_TOKEN_ENV: "test-token"},
                clear=False,
            ),
            patch.object(DIGEST.requests, "get", return_value=response),
            patch.object(DIGEST, "archive_threads_images") as archive,
        ):
            items = DIGEST.load_threads_listings(stats)

        self.assertEqual(items, [])
        self.assertEqual(stats["candidate_links"], 1)
        self.assertEqual(stats["validated"], 0)
        self.assertEqual(stats["rejects"]["invalid_district"], 1)
        self.assertEqual(stats["rejects"]["not_three_rooms"], 1)
        archive.assert_not_called()

    def test_all_threads_images_are_archived(self) -> None:
        stats = DIGEST.empty_source_stats()
        image_response = Mock(
            status_code=200,
            headers={"Content-Type": "image/jpeg"},
            content=b"x" * 900,
        )
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(DIGEST, "THREADS_ASSET_DIR", Path(temp_dir)),
            patch.object(
                DIGEST,
                "THREADS_ASSET_PUBLIC_BASE",
                "https://example.test/assets/threads",
            ),
            patch.object(
                DIGEST.requests,
                "get",
                side_effect=[image_response, image_response],
            ),
        ):
            archived = DIGEST.archive_threads_images(
                "180123456789",
                [
                    "https://scontent.cdninstagram.com/one.jpg",
                    "https://scontent.cdninstagram.com/two.jpg",
                ],
                stats,
            )

            self.assertEqual(len(archived), 2)
            self.assertTrue((Path(temp_dir) / "180123456789-01.jpg").exists())
            self.assertTrue((Path(temp_dir) / "180123456789-02.jpg").exists())
        self.assertEqual(stats["images_archived"], 2)


class SinyiAndYungchingTests(unittest.TestCase):
    def test_cloudflare_yungching_feed_accepts_only_verified_public_rows(self) -> None:
        payload = {
            "generated_at": "2026-08-12T10:00:00Z",
            "candidate_count": 2,
            "items": [
                {
                    "source_id": "2411508",
                    "url": "https://rent.yungching.com.tw/house/2411508",
                    "title": "冠倫大國",
                    "address": "桃園市桃園區大有路",
                    "layout": "4房(室)2廳2衛",
                    "size": "50.63坪",
                    "floor": "9/17樓",
                    "rent": 26000,
                    "updated": "2026年08月12日",
                    "publisher": "永義房屋",
                    "images": [
                        "https://yccdn.yungching.com.tw/a.jpg",
                        "https://example.test/not-allowed.jpg",
                    ],
                    "filter_tags": ["new"],
                },
                {
                    "source_id": "bad",
                    "url": "https://example.test/fake",
                    "title": "不合法",
                    "address": "桃園市桃園區",
                    "layout": "4房2廳2衛",
                    "rent": 26000,
                    "updated": "2026年08月12日",
                },
            ],
        }
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: payload,
        )
        stats: dict[str, object] = {"errors": [], "notices": []}

        with patch.object(DIGEST.session, "get", return_value=response):
            items = DIGEST.load_yungching_browser_feed(stats)

        self.assertEqual(list(items), ["2411508"])
        item = items["2411508"]
        self.assertEqual(item.image, "https://yccdn.yungching.com.tw/a.jpg")
        self.assertEqual(item.filter_tags, ["new"])
        self.assertEqual(stats["transport"], "cloudflare_browser_run")
        self.assertEqual(stats["category_counts"], {"all": 1, "new": 1})

    def test_cloudflare_yungching_feed_retries_temporary_failure(self) -> None:
        payload = {
            "generated_at": "2026-08-12T10:00:00Z",
            "candidate_count": 1,
            "items": [
                {
                    "source_id": "2411508",
                    "url": "https://rent.yungching.com.tw/house/2411508",
                    "title": "四房整層住家",
                    "address": "桃園市桃園區大有路",
                    "layout": "4房2廳2衛",
                    "size": "50.63坪",
                    "rent": 26000,
                    "updated": "2026年08月12日",
                    "images": ["https://yccdn.yungching.com.tw/a.jpg"],
                    "filter_tags": ["new"],
                }
            ],
        }
        temporary = Mock(status_code=503)
        temporary.raise_for_status.side_effect = DIGEST.requests.HTTPError(
            "503 Service Unavailable"
        )
        success = SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: payload,
        )
        stats: dict[str, object] = {"errors": [], "notices": []}

        with patch.object(
            DIGEST.session, "get", side_effect=[temporary, success]
        ) as request_get, patch.object(DIGEST.time, "sleep"):
            items = DIGEST.load_yungching_browser_feed(stats)

        self.assertEqual(list(items), ["2411508"])
        self.assertEqual(request_get.call_count, 2)
        self.assertEqual(stats["browser_feed_attempts"], 2)
        self.assertEqual(stats["notices"], [])

    def test_yungching_prefers_cloudflare_browser_feed_over_blocked_runner(self) -> None:
        item = DIGEST.Listing(
            source="永慶房屋",
            source_id="2411508",
            url="https://rent.yungching.com.tw/house/2411508",
            title="冠倫大國",
            district="桃園區",
            address="桃園市桃園區大有路",
            house_type="整層住家",
            layout="4房(室)2廳2衛",
            rent=26000,
            updated="2026年08月12日",
        )
        stats: dict[str, object] = {"errors": [], "notices": []}
        with patch.object(DIGEST, "load_source_snapshot", return_value=([], "", None, "")), patch.object(
            DIGEST, "load_yungching_browser_feed", return_value={item.source_id: item}
        ), patch.object(DIGEST, "crawl_yungching_candidates") as blocked_crawl, patch.object(
            DIGEST, "save_source_snapshot"
        ):
            result = DIGEST.collect_yungching_listings(stats)

        self.assertEqual(result, [item])
        blocked_crawl.assert_not_called()
        self.assertEqual(stats["validated"], 1)

    def test_sinyi_parser_keeps_40_ping_home_and_excludes_store(self) -> None:
        raw = """
        <a href="houseno/C357998">
          <div class="item_img"><img src="https://res.sinyi.com.tw/rent/C357998/smallimg/A.JPG"></div>
          <span class="item_title">採光佳家俱全配三房車位</span>
          <div class="item_detailbox">
            <span class="num num-1">成屋</span><span class="num">46.66</span>坪
            <span class="num">3/14</span>樓<span class="num">3房2廳2衛</span>
            <span class="num num-text">桃園市八德區豐德路</span>
            <span class="gray-date-1">2026/07/26 18:42</span>
            <div class="price_new"><span class="num">27,500</span>元/月</div>
          </div>
        </a>
        <a href="houseno/C354553">
          <div class="item_img"><img src="https://res.sinyi.com.tw/rent/C354553/smallimg/A.JPG"></div>
          <span class="item_title">近中壢火車站稀有店面</span>
          <span class="num">44.72</span>坪
          <span class="num num-text">桃園市中壢區中正路</span>
          <div class="price_new"><span class="num">45,000</span>元/月</div>
        </a>
        """

        items = DIGEST.parse_sinyi_list_cards(
            raw,
            "https://www.sinyi.com.tw/rent/list/example/1.html",
        )

        self.assertEqual(list(items), ["C357998"])
        item = items["C357998"]
        self.assertEqual(item.source, "信義房屋")
        self.assertEqual(item.district, "八德區")
        self.assertEqual(item.size, "46.66坪")
        self.assertEqual(item.rent, 27_500)
        self.assertEqual(item.updated, "2026/07/26 18:42")
        self.assertEqual(item.house_type, "整層住家")
        expected_url = "https://www.sinyi.com.tw/rent/houseno/C357998"
        self.assertEqual(item.url, expected_url)
        rendered = DIGEST.render_card(item)
        self.assertEqual(rendered.count(f'href="{expected_url}"'), 3)

    def test_sinyi_snapshot_rewrites_legacy_list_url_to_detail_page(self) -> None:
        item = DIGEST.Listing(
            source="信義房屋",
            source_id="C357998",
            url=(
                "https://www.sinyi.com.tw/rent/list/Taoyuan-city/"
                "320-324-330-334-zip/40-up-area/house-use/houseno/C357998"
            ),
            title="採光佳家俱全配四房車位",
            district="八德區",
            address="桃園市八德區豐德路",
            house_type="整層住家",
            size="46.66坪",
            rent=27_500,
            image="https://res.sinyi.com.tw/rent/C357998/smallimg/A.JPG",
            raw_text="桃園市八德區四房整層住家",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(temp_dir) / "last-success-sinyi.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "generated_at": DIGEST.NOW.isoformat(),
                        "items": [DIGEST.asdict(item)],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            loaded, _, _, error = DIGEST.load_source_snapshot(
                "信義房屋",
                snapshot,
            )

        self.assertEqual(error, "")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(
            loaded[0].url,
            "https://www.sinyi.com.tw/rent/houseno/C357998",
        )

    def test_yungching_list_parser_marks_official_new_tab(self) -> None:
        raw = """
        <a class="link" href="//rent.yungching.com.tw/house/2410499">
          <div class="yc-ng-rent-house-card list">
            <div class="caseName">青埔景觀4房雙車</div>
            <span class="address">桃園市中壢區領航北路二段</span>
            <span class="purpose">住宅</span>
            <span class="regArea">83.56坪</span>
            <span class="floor">19/19樓</span>
            <span class="room">4房(室)2廳2衛</span>
            <div class="price">58,500</div>
          </div>
        </a>
        """

        items = DIGEST.extract_yungching_list_cards(
            raw,
            DIGEST.yungching_result_url("new", 1),
            "new",
        )

        self.assertEqual(list(items), ["2410499"])
        item = items["2410499"]
        self.assertEqual(item.url, "https://rent.yungching.com.tw/house/2410499")
        self.assertEqual(item.filter_tags, ["new"])
        self.assertEqual(item.layout, "4房(室)2廳2衛")
        self.assertEqual(item.rent, 58_500)

    def test_yungching_first_page_waits_for_rendered_house_cards(self) -> None:
        card_html = """
        <html><body>
          <a class="link" href="//rent.yungching.com.tw/house/2410499">
            <div class="caseName">青埔景觀4房雙車</div>
            <span class="address">桃園市中壢區領航北路二段</span>
            <span class="purpose">住宅</span>
            <span class="regArea">83.56坪</span>
            <span class="room">4房(室)2廳2衛</span>
            <div class="price">58,500</div>
          </a>
        </body></html>
        """ + f"<!--{'x' * 900}-->"
        empty_html = f"<html><body>{'尚無其他結果' * 100}</body></html>"
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_fetch(url: str, **kwargs: object) -> tuple[None, str]:
            calls.append((url, kwargs))
            return None, card_html if "pg=1" in url else empty_html

        stats: dict[str, object] = {"errors": [], "notices": []}
        with patch.object(DIGEST, "fetch_html", side_effect=fake_fetch):
            items = DIGEST.crawl_yungching_candidates(stats)

        first_page_calls = [kwargs for url, kwargs in calls if "pg=1" in url]
        later_page_calls = [kwargs for url, kwargs in calls if "pg=1" not in url]
        self.assertEqual(list(items), ["2410499"])
        self.assertEqual(len(first_page_calls), 2)
        self.assertTrue(
            all(
                kwargs.get("browser_wait_selector") == 'a[href*="/house/"]'
                for kwargs in first_page_calls
            )
        )
        self.assertTrue(
            all(kwargs.get("browser_wait_selector") == "" for kwargs in later_page_calls)
        )
        self.assertEqual(stats["category_counts"], {"all": 1, "new": 1})
        self.assertEqual(stats["page_diagnostics"][0]["card_count"], 1)

    def test_yungching_detail_requires_detail_update_date_and_collects_photos(self) -> None:
        candidate = DIGEST.Listing(
            source="永慶房屋",
            source_id="2410994",
            url="https://rent.yungching.com.tw/house/2410994",
            title="列表標題",
            district="中壢區",
            address="桃園市中壢區領航南路一段",
            house_type="整層住家",
            layout="4房(室)2廳2衛",
            size="96.62坪",
            rent=60_000,
            filter_tags=["new"],
        )
        raw = """
        <html><head>
          <meta name="description" content="高樓層四房雙車位">
          <script type="application/ld+json">
          {"@type":"Product","name":"A19宜誠僑峰美妝4房雙車", "image":["https://yccdn.yungching.com.tw/cover.jpg"], "offers":{"price":"60000"}}
          </script>
        </head><body>
          <h1>A19宜誠僑峰美妝4房雙車</h1>
          <h3>桃園市中壢區領航南路一段</h3>
          <div>住宅 電梯大樓 坪數96.62坪 11/14樓 4房(室)2廳2衛</div>
          <div><h3>更新日期</h3><span>2026年08月03日</span></div>
          <a href="//shop.yungching.com.tw/033790555">永慶不動產 桃園中路加盟店</a>
          <figure><img src="https://yccdn.yungching.com.tw/photo-1.jpg"></figure>
          <figure><img src="https://yccdn.yungching.com.tw/photo-2.jpg"></figure>
          <p>有車位 近捷運 可開伙 有陽台 有電梯 冷氣 冰箱 洗衣機</p>
          <p>詳細房源說明 詳細房源說明 詳細房源說明 詳細房源說明 詳細房源說明</p>
          <p>測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容</p>
          <p>測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容</p>
          <p>測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容</p>
          <p>測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容</p>
          <p>測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容</p>
          <p>測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容</p>
          <p>測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容</p>
          <p>測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容測試內容</p>
        </body></html>
        """

        with patch.object(DIGEST, "fetch_html", return_value=(None, raw)):
            item = DIGEST.parse_yungching_detail(candidate)

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.updated, "2026年08月03日")
        self.assertEqual(item.layout, "4房(室)2廳2衛")
        self.assertEqual(item.image, "https://yccdn.yungching.com.tw/photo-1.jpg")
        self.assertIn("https://yccdn.yungching.com.tw/photo-2.jpg", item.images)
        self.assertIn("new", DIGEST.listing_filter_tokens(item))

    def test_absolute_source_dates_sort_by_real_age(self) -> None:
        old_now = DIGEST.NOW
        DIGEST.NOW = DIGEST.datetime(2026, 8, 4, 12, 0, tzinfo=DIGEST.TZ)
        try:
            sinyi = DIGEST.Listing(
                source="信義房屋",
                source_id="C1",
                url="https://example.test/C1",
                updated="2026/08/04 10:30",
            )
            yungching = DIGEST.Listing(
                source="永慶房屋",
                source_id="1",
                url="https://example.test/1",
                updated="2026年08月03日",
            )
            self.assertEqual(DIGEST.recency_minutes(sinyi), 90)
            self.assertEqual(DIGEST.recency_minutes(yungching), 36 * 60)
        finally:
            DIGEST.NOW = old_now

    def test_yungching_generic_og_uses_honest_no_photo_panel(self) -> None:
        self.assertFalse(
            DIGEST.is_yungching_photo_url(
                "https://rent.yungching.com.tw/list/assets/rent_og.jpg"
            )
        )
        self.assertTrue(
            DIGEST.is_yungching_photo_url(
                "https://yccdn.yungching.com.tw/v1/image/?key=real"
            )
        )
        item = DIGEST.Listing(
            source="永慶房屋",
            source_id="2410499",
            url="https://rent.yungching.com.tw/house/2410499",
            title="青埔四房",
            district="中壢區",
            address="桃園市中壢區領航北路二段",
            house_type="整層住家",
            layout="4房(室)2廳2衛",
            size="83.56坪",
            rent=58_500,
            updated="2026年07月31日",
        )
        self.assertTrue(DIGEST.source_snapshot_item_valid(item, "永慶房屋"))
        rendered = DIGEST.render_card(item)
        self.assertIn("來源未提供可讀取照片", rendered)
        self.assertIn('data-photo-count="0"', rendered)

    def test_yungching_primary_photo_is_archived_and_loaded_first(self) -> None:
        item = DIGEST.Listing(
            source="永慶房屋",
            source_id="2410994",
            url="https://rent.yungching.com.tw/house/2410994",
            title="青埔四房",
            image="https://yccdn.yungching.com.tw/photo-1.jpg",
            images=["https://yccdn.yungching.com.tw/photo-2.jpg"],
        )
        response = SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "image/jpeg"},
            content=b"real-yungching-photo" * 100,
        )
        stats: dict[str, object] = {}

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            DIGEST, "YUNGCHING_ASSET_DIR", Path(temp_dir)
        ), patch.object(DIGEST.requests, "get", return_value=response):
            DIGEST.prepare_yungching_images([item], stats)
            archived_path = Path(temp_dir) / Path(item.image).name
            self.assertTrue(archived_path.exists())

        self.assertTrue(item.image.startswith("assets/yungching/2410994-"))
        self.assertEqual(
            item.images,
            ["https://yccdn.yungching.com.tw/photo-2.jpg"],
        )
        self.assertEqual(stats["listings_with_source_images"], 1)
        self.assertEqual(stats["primary_images_local"], 1)
        rendered = DIGEST.render_card(item)
        self.assertIn('loading="eager"', rendered)
        self.assertIn('fetchpriority="high"', rendered)
        self.assertIn('loading="lazy"', rendered)
        self.assertIn('fetchpriority="low"', rendered)

        status = DIGEST.render_status(
            {
                "sources": {
                    "永慶房屋": {
                        "candidate_links": 1,
                        "validated": 1,
                        "published": 1,
                        "errors": [],
                        "notices": [],
                        **stats,
                    }
                }
            },
            "永慶房屋",
        )
        self.assertIn("有來源照片 1 筆", status)
        self.assertIn("無來源照片 0 筆", status)
        self.assertIn("首圖本站保存 1 筆", status)

    def test_yungching_archive_failure_keeps_true_remote_photo(self) -> None:
        item = DIGEST.Listing(
            source="永慶房屋",
            source_id="2410994",
            url="https://rent.yungching.com.tw/house/2410994",
            image="",
            images=["https://yccdn.yungching.com.tw/photo-1.jpg"],
        )
        stats: dict[str, object] = {}

        with patch.object(
            DIGEST.requests,
            "get",
            side_effect=DIGEST.requests.RequestException("blocked"),
        ):
            DIGEST.prepare_yungching_images([item], stats)

        self.assertEqual(
            item.image,
            "https://yccdn.yungching.com.tw/photo-1.jpg",
        )
        self.assertEqual(item.images, [])
        self.assertEqual(stats["primary_images_remote_only"], 1)
        self.assertEqual(stats["primary_image_download_failures"], 1)


class CurrentListingDisplayTests(unittest.TestCase):
    def test_previous_edition_marks_new_listings_across_all_sources(self) -> None:
        old_now = DIGEST.NOW
        DIGEST.NOW = DIGEST.datetime(2026, 8, 12, 18, 0, tzinfo=DIGEST.TZ)
        try:
            sources = ["591", "FB", "樂屋網", "Threads", "信義房屋", "永慶房屋"]
            items = [
                DIGEST.Listing(
                    source=source,
                    source_id=f"id-{index}",
                    url=f"https://example.test/{index}",
                    title=f"{source}四房",
                    image="https://example.test/photo.jpg",
                )
                for index, source in enumerate(sources)
            ]
            DIGEST.assign_previous_edition_new_flags(items, {"591:id-0"})

            self.assertFalse(DIGEST.is_new_listing(items[0]))
            self.assertTrue(all(DIGEST.is_new_listing(item) for item in items[1:]))
            for item in items[1:]:
                rendered = DIGEST.render_card(item)
                self.assertIn('class="new-listing-badge"', rendered)
                self.assertEqual(rendered.count("新房源"), 1)
            self.assertNotIn("新房源", DIGEST.render_card(items[0]))
        finally:
            DIGEST.NOW = old_now

    def test_latest_page_migration_prevents_mass_false_new_badges(self) -> None:
        old_now = DIGEST.NOW
        DIGEST.NOW = DIGEST.datetime(2026, 8, 12, 18, 0, tzinfo=DIGEST.TZ)
        try:
            item = DIGEST.Listing(
                source="FB",
                source_id="existing",
                url="https://example.test/existing",
                title="既有四房",
            )
            existing_payload = {
                "generated_at": "2026-08-11T12:00:00+08:00",
                "items": [
                    {
                        "source": "FB",
                        "source_id": "existing",
                        "validated_at": "2026-08-11T12:00:00+08:00",
                    }
                ],
            }
            with tempfile.TemporaryDirectory() as temp_dir:
                output = Path(temp_dir) / "latest.json"
                output.write_text(json.dumps(existing_payload), encoding="utf-8")
                missing = Path(temp_dir) / "missing.json"
                with patch.object(DIGEST, "OUTPUT_JSON", output), patch.object(
                    DIGEST, "LAST_DELIVERY_FILE", missing
                ):
                    previous, source = DIGEST.load_previous_edition_keys()
                    DIGEST.assign_previous_edition_new_flags([item], previous)
            self.assertEqual(source, "latest:migration")
            self.assertFalse(DIGEST.is_new_listing(item))
        finally:
            DIGEST.NOW = old_now

    def test_last_delivery_is_the_new_listing_comparison_source(self) -> None:
        old_now = DIGEST.NOW
        DIGEST.NOW = DIGEST.datetime(2026, 8, 12, 18, 0, tzinfo=DIGEST.TZ)
        try:
            recent = DIGEST.Listing(
                source="FB",
                source_id="recent",
                url="https://example.test/recent",
            )
            absent = DIGEST.Listing(
                source="Threads",
                source_id="absent",
                url="https://example.test/absent",
            )
            DIGEST.assign_previous_edition_new_flags(
                [recent, absent],
                {"fb:recent"},
            )

            self.assertFalse(recent.new_listing)
            self.assertTrue(absent.new_listing)
            self.assertIn("上一封快報未出現", DIGEST.render_card(absent))
        finally:
            DIGEST.NOW = old_now

    def test_new_listing_badge_expires_after_24_hours(self) -> None:
        old_now = DIGEST.NOW
        DIGEST.NOW = DIGEST.datetime(2026, 8, 21, 12, 0, tzinfo=DIGEST.TZ)
        try:
            recent = DIGEST.Listing(
                source="樂屋網",
                source_id="recent",
                url="https://example.test/recent",
                first_seen_at="2026-08-20T13:00:00+08:00",
            )
            expired = DIGEST.Listing(
                source="樂屋網",
                source_id="expired",
                url="https://example.test/expired",
                first_seen_at="2026-08-20T11:59:00+08:00",
            )

            DIGEST.assign_previous_edition_new_flags([recent, expired], set())

            self.assertTrue(recent.new_listing)
            self.assertFalse(expired.new_listing)
        finally:
            DIGEST.NOW = old_now

    def test_source_time_accepts_today_yesterday_month_day_and_iso(self) -> None:
        old_now = DIGEST.NOW
        DIGEST.NOW = DIGEST.datetime(2026, 8, 21, 12, 0, tzinfo=DIGEST.TZ)
        try:
            cases = {
                "今天 09:30 更新": DIGEST.datetime(2026, 8, 21, 9, 30, tzinfo=DIGEST.TZ),
                "昨日 23:20 刊登": DIGEST.datetime(2026, 8, 20, 23, 20, tzinfo=DIGEST.TZ),
                "08/20 18:42 更新": DIGEST.datetime(2026, 8, 20, 18, 42, tzinfo=DIGEST.TZ),
                "2026-08-21T09:15:00+08:00": DIGEST.datetime(2026, 8, 21, 9, 15, tzinfo=DIGEST.TZ),
            }
            for raw, expected in cases.items():
                with self.subTest(raw=raw):
                    item = DIGEST.Listing(source="樂屋網", source_id=raw, url="", updated=raw)
                    self.assertEqual(DIGEST.source_listing_time(item), expected)
        finally:
            DIGEST.NOW = old_now

    def test_591_uses_two_days_and_other_sources_use_seven_days(self) -> None:
        old_now = DIGEST.NOW
        DIGEST.NOW = DIGEST.datetime(2026, 8, 18, 16, 0, tzinfo=DIGEST.TZ)
        try:
            sources = ["591", "FB", "樂屋網", "Threads", "信義房屋", "永慶房屋"]
            items: list[object] = []
            stats = {"sources": {source: DIGEST.empty_source_stats() for source in sources}}
            for source in sources:
                stale_age = "3天前更新" if source == "591" else "8天前更新"
                items.extend(
                    [
                        DIGEST.Listing(
                            source=source,
                            source_id=f"{source}-fresh",
                            url="https://example.test/fresh",
                            updated="1小時前更新",
                        ),
                        DIGEST.Listing(
                            source=source,
                            source_id=f"{source}-stale",
                            url="https://example.test/stale",
                            updated=stale_age,
                        ),
                        DIGEST.Listing(
                            source=source,
                            source_id=f"{source}-missing",
                            url="https://example.test/missing",
                        ),
                    ]
                )

            kept = DIGEST.filter_source_freshness(items, stats)

            self.assertEqual(len(kept), len(sources))
            self.assertTrue(all(item.source_id.endswith("-fresh") for item in kept))
            self.assertTrue(all(item.source_timestamp for item in kept))
            for source in sources:
                row = stats["sources"][source]
                freshness_days = 2 if source == "591" else 7
                self.assertEqual(row["validated"], 1)
                self.assertEqual(row["freshness_rejected"], 2)
                self.assertEqual(row["freshness_window_days"], freshness_days)
                self.assertEqual(row[f"fresh_within_{freshness_days}_days"], 1)
                self.assertEqual(
                    row["rejects"][f"source_older_than_{freshness_days}_days"], 1
                )
                self.assertEqual(row["rejects"]["missing_source_time"], 1)
        finally:
            DIGEST.NOW = old_now

    def test_rental_permalink_contains_date_and_delivery_slot(self) -> None:
        self.assertEqual(
            DIGEST.archive_url_for_edition("2026-08-18-1600"),
            "https://flyspacesky.github.io/taoyuan-rental-digest/"
            "archive/2026-08-18-1600.html",
        )
        with self.assertRaisesRegex(ValueError, "無效"):
            DIGEST.archive_url_for_edition("latest")

    def test_existing_edition_is_reused_without_overwriting_archive(self) -> None:
        edition_id = "2026-08-18-1600"
        edition_url = DIGEST.archive_url_for_edition(edition_id)
        payload = {
            "edition_id": edition_id,
            "edition_url": edition_url,
            "generated_at": "2026-08-18T16:00:00+08:00",
            "stats": {},
            "items": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_dir = root / "archive"
            edition_dir = root / "editions"
            archive_dir.mkdir()
            edition_dir.mkdir()
            archive = archive_dir / f"{edition_id}.html"
            archive.write_text("fixed edition", encoding="utf-8")
            (edition_dir / f"{edition_id}.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            latest = root / "latest.json"
            index = root / "index.html"
            with (
                patch.object(DIGEST, "ARCHIVE_DIR", archive_dir),
                patch.object(DIGEST, "EDITION_DATA_DIR", edition_dir),
                patch.object(DIGEST, "OUTPUT_JSON", latest),
                patch.object(DIGEST, "OUTPUT_HTML", index),
            ):
                reused = DIGEST.reuse_existing_edition(edition_id, edition_url)

            self.assertTrue(reused)
            self.assertEqual(index.read_text(encoding="utf-8"), "fixed edition")
            self.assertEqual(archive.read_text(encoding="utf-8"), "fixed edition")
            self.assertEqual(json.loads(latest.read_text())["edition_id"], edition_id)

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

    def test_591_featured_requires_explicit_official_label(self) -> None:
        featured = self.listing("21700001")
        featured.raw_text = "優選好屋 可開伙"
        ordinary = self.listing("21700002")
        ordinary.title = "精選四房物件"

        self.assertTrue(DIGEST.is_591_featured(featured))
        self.assertFalse(DIGEST.is_591_featured(ordinary))
        self.assertEqual(
            DIGEST.section_items([featured, ordinary], "591", "featured"),
            [featured],
        )

    def test_render_uses_single_listing_column_tabs_sorts_and_date(self) -> None:
        owner = self.listing(
            "21700001",
            publisher="屋主: 林先生",
            category_hint="owner",
            category="owner",
        )
        owner.raw_text = "優選好屋"
        owner.total_cost = 35_300
        discount = self.listing(
            "21700002",
            category_hint="discount",
            category="discount",
            old_rent=35_000,
        )
        stats = {
            "sources": {
                "591": DIGEST.empty_source_stats(),
                "FB": DIGEST.empty_source_stats(),
                "樂屋網": DIGEST.empty_source_stats(),
                "Threads": DIGEST.empty_source_stats(),
                "信義房屋": DIGEST.empty_source_stats(),
                "永慶房屋": DIGEST.empty_source_stats(),
            },
            "candidates": 2,
            "validated": 2,
            "duplicates": 0,
            "published": 2,
        }
        stats["sources"]["591"].update(
            {"candidate_links": 2, "validated": 2, "published": 2}
        )

        rendered = DIGEST.render_html([owner, discount], stats)

        self.assertIn(
            f'<time datetime="{DIGEST.NOW.isoformat(timespec="minutes")}" '
            f'aria-label="本次執行時間">',
            rendered,
        )
        self.assertIn(f"{DIGEST.NOW:%Y/%m/%d %H:%M}", rendered)
        self.assertIn("公開社團入口 11 個", rendered)
        self.assertIn("Marketplace入口 1 個", rendered)
        self.assertIn("公開／已授權貼文 0 筆", rendered)
        self.assertIn("🔥 A級 0 筆", rendered)
        self.assertIn("🟡 B級 0 筆", rendered)
        self.assertIn("⚪ C級 0 筆", rendered)
        self.assertIn("公開投稿 0 筆", rendered)
        self.assertIn("私人收件匣 0 筆", rendered)
        self.assertIn("自動補齊 0 筆", rendered)
        self.assertIn("提交FB永久貼文", rendered)
        self.assertIn("FB 屋主房源雷達", rendered)
        self.assertIn(DIGEST.FB_ISSUE_TEMPLATE_URL, rendered)
        self.assertIn(DIGEST.FB_PRIVATE_SUBMISSION_URL, rendered)
        self.assertIn("私人社團授權投稿", rendered)
        self.assertIn(DIGEST.FB_MARKETPLACE_URL, rendered)
        self.assertIn("520租屋快訊網", rendered)
        self.assertIn("中壢租屋網", rendered)
        self.assertIn(DIGEST.THREADS_ISSUE_TEMPLATE_URL, rendered)
        self.assertIn("提交Threads永久貼文", rendered)
        self.assertIn("高符合", rendered)
        self.assertIn("資訊待補", rendered)
        self.assertIn("🔥 A級房源", rendered)
        self.assertIn("🟡 B級房源", rendered)
        self.assertIn("⚪ C級房源", rendered)
        self.assertIn("優選好屋", rendered)
        self.assertIn("租金總費用", rendered)
        self.assertIn("室內坪數", rendered)
        self.assertIn("人氣", rendered)
        self.assertIn("由新到舊", rendered)
        self.assertIn("由舊到新", rendered)
        self.assertIn("總費用低到高", rendered)
        self.assertIn("總費用高到低", rendered)
        self.assertIn('data-total="35300"', rendered)
        self.assertIn("租金低到高", rendered)
        self.assertIn("租金高到低", rendered)
        self.assertIn("坪數小到大", rendered)
        self.assertIn("坪數大到小", rendered)
        self.assertIn("人氣高到低", rendered)
        self.assertIn("人氣低到高", rendered)
        self.assertEqual(rendered.count('<select class="sort-select"'), 16)
        self.assertEqual(rendered.count('data-combined-sort="true"'), 2)
        self.assertEqual(rendered.count('value="recency:asc" selected'), 2)
        self.assertNotIn('value="order:asc" selected', rendered)
        self.assertEqual(rendered.count('data-sort="recency" aria-current="true"'), 2)
        self.assertNotIn('class="sort-button', rendered)
        self.assertIn("justify-content:flex-end", rendered)
        self.assertEqual(rendered.count('class="status-primary"'), 6)
        self.assertEqual(
            rendered.count('<div class="filter-group" data-filter-count="4"'),
            4,
        )
        self.assertEqual(
            rendered.count('<div class="filter-group" data-filter-count="3"'),
            0,
        )
        self.assertEqual(
            rendered.count('<div class="filter-group" data-filter-count="1"'),
            1,
        )
        self.assertEqual(
            rendered.count('<div class="filter-group" data-filter-count="2"'),
            1,
        )
        self.assertIn(
            ".status-primary{width:75%;align-self:flex-start;display:grid;"
            "grid-template-columns:repeat(4,minmax(0,1fr));",
            rendered,
        )
        self.assertIn(
            ".filter-group{width:75%;margin-right:auto;display:grid;",
            rendered,
        )
        self.assertIn("align-items:stretch;direction:ltr", rendered)
        self.assertIn(".status-primary,.filter-group{width:100%}", rendered)
        self.assertIn(
            ".sort-row{display:flex;align-items:center;"
            "flex-direction:row-reverse;justify-content:flex-start;"
            "gap:4px;min-height:52px;",
            rendered,
        )
        self.assertIn(
            ".sort-group{display:flex;align-items:center;"
            "flex-direction:row-reverse;justify-content:flex-start;gap:4px;",
            rendered,
        )
        self.assertIn(
            ".sort-control{display:flex;align-items:center;"
            "flex-direction:row-reverse;gap:2px;"
            "padding:0;border:0;background:transparent;",
            rendered,
        )
        self.assertIn(".sort-select{max-width:118px;", rendered)
        self.assertIn('id="back-to-top"', rendered)
        self.assertIn('aria-label="回到頁面頂端"', rendered)
        self.assertIn("window.scrollY < 480", rendered)
        self.assertIn("window.scrollTo({top: 0, behavior: 'smooth'})", rendered)
        self.assertIn("grid-template-columns:minmax(260px,32%)", rendered)
        self.assertNotIn("repeat(2,minmax(0,1fr))", rendered)
        self.assertEqual(rendered.count('<article class="card"'), 2)
        self.assertIn(
            'href="https://rent.591.com.tw/list?kind=1&layout=4&region=6"',
            rendered,
        )
        self.assertIn(
            'href="https://www.facebook.com/groups/feed/"',
            rendered,
        )
        self.assertIn(
            'href="https://rent.rakuya.com.tw/"',
            rendered,
        )
        self.assertIn('<a href="#source-threads">Threads</a>', rendered)
        self.assertIn('<a href="#source-sinyi">信義房屋</a>', rendered)
        self.assertIn('<a href="#source-yungching">永慶房屋</a>', rendered)
        self.assertIn('id="source-threads"', rendered)
        self.assertIn('id="source-sinyi"', rendered)
        self.assertIn('id="source-yungching"', rendered)
        self.assertIn("更新時間：新 → 舊", rendered)
        self.assertIn("上架時間：新 → 舊", rendered)
        self.assertIn("店面、透店、住辦、辦公", rendered)
        self.assertIn("更新日期與全部可讀取照片", rendered)
        self.assertIn(
            'href="https://www.threads.com/"',
            rendered,
        )
        self.assertIn("桃園區、", rendered)
        self.assertIn("官方搜尋＋人工授權入口＋自動篩選", rendered)
        self.assertIn("缺少租金或可讀照片仍可刊出", rendered)
        self.assertIn("留言權限 不可用", rendered)
        self.assertIn(">全部 <b>0</b></button>", rendered)
        self.assertNotIn(">優選好屋 <b>0</b></button>", rendered)
        self.assertIn("Threads 官方搜尋或公開投稿驗證通過", rendered)
        self.assertIn(
            "empty.textContent = cards.length",
            rendered,
        )

    def test_workflow_runs_three_daily_line_delivery_windows(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "rental-digest.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('cron: "30 9 * * *"', workflow)
        self.assertIn('cron: "0 16 * * *"', workflow)
        self.assertIn('cron: "0 22 * * *"', workflow)
        self.assertNotIn('cron: "30 9,16,22 * * *"', workflow)
        self.assertEqual(workflow.count('timezone: "Asia/Taipei"'), 3)
        self.assertIn("LINE_DELIVERY_SLOT:", workflow)
        self.assertIn("manual:${GITHUB_RUN_ID}", workflow)
        self.assertIn('EDITION_ID="${DATE}-0930"', workflow)
        self.assertIn('EDITION_ID="${DATE}-1600"', workflow)
        self.assertIn('EDITION_ID="${DATE}-2200"', workflow)
        self.assertIn("RENTAL_EDITION_ID:", workflow)
        self.assertIn("git add docs/archive", workflow)
        self.assertIn("docs/rental-data/last-delivery.json", workflow)
        self.assertIn("skip_line:", workflow)
        self.assertEqual(workflow.count("!inputs.skip_line"), 2)
        self.assertIn(
            'LINE_CHANNEL_ACCESS_TOKEN: ${{ secrets.LINE_CHANNEL_ACCESS_TOKEN }}',
            workflow,
        )

    def test_private_facebook_submission_page_never_requests_facebook_login(self) -> None:
        page = (ROOT / "docs" / "facebook-submit.html").read_text(encoding="utf-8")

        self.assertIn("/facebook-inbox", page)
        self.assertIn("republish_authorized", page)
        self.assertIn("no_facebook_credentials", page)
        self.assertIn("localStorage", page)
        self.assertNotIn('name="password"', page)
        self.assertNotIn('name="cookie"', page)
        self.assertNotIn('name="session"', page)


class Stable591EgressTests(unittest.TestCase):
    def test_proxy_is_scoped_to_591_and_encodes_credentials(self) -> None:
        config = DIGEST.rental_591_proxy_config(
            {
                DIGEST.RENTAL_591_PROXY_SERVER_ENV: "https://proxy.example:8443",
                DIGEST.RENTAL_591_PROXY_USERNAME_ENV: "user@example.com",
                DIGEST.RENTAL_591_PROXY_PASSWORD_ENV: "p@ss word",
            }
        )

        self.assertIsNotNone(config)
        self.assertEqual(
            config["requests"]["https"],
            "https://user%40example.com:p%40ss%20word@proxy.example:8443",
        )
        self.assertEqual(config["playwright"]["server"], "https://proxy.example:8443")
        self.assertTrue(DIGEST.is_591_url("https://bff-house.591.com.tw/v3/web/rent/list"))
        self.assertFalse(DIGEST.is_591_url("https://graph.threads.net/v1/posts"))

    def test_proxy_rejects_embedded_credentials(self) -> None:
        with self.assertRaises(ValueError):
            DIGEST.rental_591_proxy_config(
                {
                    DIGEST.RENTAL_591_PROXY_SERVER_ENV: (
                        "https://user:password@proxy.example:8443"
                    )
                }
            )


if __name__ == "__main__":
    unittest.main()
