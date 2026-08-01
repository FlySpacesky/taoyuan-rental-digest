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

    def test_partial_blocked_refresh_merges_and_preserves_complete_snapshot(self) -> None:
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

        self.assertEqual([item.source_id for item in result], ["21700001", "21700002"])
        self.assertEqual(stats["fresh_candidate_links"], 1)
        self.assertEqual(stats["fresh_validated"], 1)
        self.assertEqual(stats["candidate_links"], 2)
        self.assertEqual(stats["fallback"], "partial_source_blocked")
        save.assert_not_called()


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
    def test_resolved_group_permalink_is_allowlisted(self) -> None:
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

    def test_repo_facebook_import_contains_supplied_real_post(self) -> None:
        stats = DIGEST.empty_source_stats()

        items = DIGEST.load_facebook_import(stats)

        target = next(
            (
                item
                for item in items
                if item.url.endswith("/permalink/4623380861319264/")
            ),
            None,
        )
        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.layout, "4房2廳2衛")
        self.assertEqual(target.size, "46坪")
        self.assertEqual(target.rent, 23_000)
        self.assertEqual(target.publisher, "林思妤")
        self.assertEqual(target.updated, "2026/07/13 21:43刊登")
        self.assertEqual(stats["import_source"], "data/facebook_posts.json")
        self.assertEqual(stats["allowed_groups"], len(DIGEST.FB_GROUPS))
        self.assertEqual(stats["anonymous_verified_posts"], 1)
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
                "updated": "2026/07/30 08:00刊登",
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

        self.assertIn("invalid_or_unlisted_group_url", reasons)
        self.assertIn("not_four_rooms", reasons)
        self.assertIn("excluded_management_or_broker", reasons)
        self.assertIn("image_not_direct_public", reasons)
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
        self.assertEqual(stats["rejects"]["not_four_rooms"], 1)
        self.assertEqual(stats["rejects"]["image_not_direct_public"], 1)
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
    def test_missing_access_token_reports_actionable_error(self) -> None:
        stats = DIGEST.empty_source_stats()
        with patch.dict(
            os.environ,
            {DIGEST.THREADS_ACCESS_TOKEN_ENV: ""},
            clear=False,
        ):
            items = DIGEST.load_threads_listings(stats)

        self.assertEqual(items, [])
        self.assertEqual(stats["candidate_links"], 0)
        self.assertEqual(stats["validated"], 0)
        self.assertIn(DIGEST.THREADS_ACCESS_TOKEN_ENV, stats["errors"][0])
        self.assertIn("threads_keyword_search", stats["errors"][0])

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
        self.assertTrue(
            all(
                int(params["until"]) - int(params["since"])
                == DIGEST.THREADS_SEARCH_LOOKBACK_DAYS * 24 * 60 * 60
                for params in recorded_params
            )
        )
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
                    "timestamp": "2026-07-30T01:02:03+0000",
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

    def test_non_taoyuan_or_under_four_rooms_is_rejected(self) -> None:
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
                    "text": "中壢區三房出租\n租金：20,000元\n3房2廳1衛",
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
        self.assertEqual(stats["rejects"]["not_taoyuan_district"], 1)
        self.assertEqual(stats["rejects"]["not_four_rooms"], 1)
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
        self.assertIn("允許社團 11 個", rendered)
        self.assertIn("匿名驗證貼文 0 筆", rendered)
        self.assertIn("公開投稿 0 筆", rendered)
        self.assertIn("自動補齊 0 筆", rendered)
        self.assertIn("提交FB永久貼文", rendered)
        self.assertIn("GitHub Actions會以不含Cookie的匿名請求", rendered)
        self.assertIn(DIGEST.FB_ISSUE_TEMPLATE_URL, rendered)
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
        self.assertNotIn('class="sort-button', rendered)
        self.assertIn("justify-content:flex-end", rendered)
        self.assertEqual(rendered.count('class="status-primary"'), 4)
        self.assertEqual(
            rendered.count('<div class="filter-group" data-filter-count="4"'),
            2,
        )
        self.assertEqual(
            rendered.count('<div class="filter-group" data-filter-count="3"'),
            1,
        )
        self.assertEqual(
            rendered.count('<div class="filter-group" data-filter-count="1"'),
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
        self.assertIn('id="source-threads"', rendered)
        self.assertIn(
            'href="https://www.threads.com/"',
            rendered,
        )
        self.assertIn("桃園區、", rendered)
        self.assertIn("輪播全部照片都已完整保存", rendered)
        self.assertIn(">Threads <b>0</b></button>", rendered)
        self.assertNotIn(">優選好屋 <b>0</b></button>", rendered)
        self.assertIn("Threads 官方 API 驗證通過", rendered)
        self.assertIn(
            "empty.textContent = cards.length",
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
