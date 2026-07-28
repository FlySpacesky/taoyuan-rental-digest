#!/usr/bin/env python3
"""
桃園四房以上租屋快報

來源：
- 591：桃園區、中壢區、平鎮區、八德區，整層住家、4房以上
- Facebook：安全 JSON 匯入
- 樂屋網：中壢區、桃園區、平鎮區，4房及5房以上

本版重點：
1. 完全不含 Threads。
2. 591 詳情頁優先使用 Playwright Chromium；若 requests 曾回 403，但 Chromium
   已取得有效 HTML，不再被舊的 403 狀態誤判。
3. 591 只有在 Chromium / requests 都拿不到有效頁面時，才退回當次列表頁快照。
4. 591 列表圖片不再限制必須是 591.com.tw 網域，允許 CDN 圖片。
5. 404 / 410 / 已刪除 / 已關閉 / 已成交物件仍排除。
6. 樂屋網原租金只接受明確舊價標示或歷史快照，避免把押金誤判為原租金。
7. 48 小時來源內與跨來源去重；真正降價物件可重新顯示。
8. 頁首「桃園四房以上租屋快報＋591／FB社團／樂屋網」捲動時固定在頂端。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)
ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DATA_DIR = DOCS / "rental-data"
STATE_FILE = DATA_DIR / "history.json"
OUTPUT_JSON = DATA_DIR / "latest.json"
OUTPUT_HTML = DOCS / "index.html"
FB_IMPORT = ROOT / "data" / "facebook_posts.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0 Safari/537.36"
)

DISTRICTS_591 = {
    "桃園區": "73",
    "中壢區": "67",
    "平鎮區": "68",
    "八德區": "75",
}
DISTRICTS_RAKUYA = {
    "中壢區": "320",
    "平鎮區": "324",
    "桃園區": "330",
}
ALLOWED_DISTRICTS = set(DISTRICTS_591)

FB_GROUPS = [
    "https://www.facebook.com/groups/117995715653134",
    "https://www.facebook.com/groups/0908728307emma",
    "https://www.facebook.com/groups/671061659720658",
    "https://www.facebook.com/groups/1925073351142915",
    "https://www.facebook.com/groups/1167119493432173",
    "https://www.facebook.com/groups/1590787834331314",
    "https://www.facebook.com/groups/468627751712411",
    "https://www.facebook.com/groups/261357414247414",
    "https://www.facebook.com/groups/178112912695401",
    "https://www.facebook.com/groups/768849317151214",
]

INVALID_MARKERS = (
    "很抱歉，您查詢的物件不存在，可能已關閉或者被刪除",
    "您查詢的物件不存在",
    "物件不存在",
    "物件已關閉",
    "已關閉或者被刪除",
    "物件已刪除",
    "此物件已刪除",
    "找不到此物件",
    "此房屋已成交",
    "抱歉！此物件目前無法瀏覽",
)

BLOCK_MARKERS = (
    "cf-chl-",
    "captcha",
    "access denied",
    "verify you are human",
    "機器人驗證",
)

EXCLUDE_MARKERS = (
    "社會住宅廠商",
    "代租代管",
    "包租代管",
    "租管通",
    "租賃住宅服務業",
    "租賃服務業",
    "代理人",
)

FRIENDLY_MARKERS = (
    "可入籍",
    "入戶籍",
    "租補",
    "租金補助",
    "租金補貼",
    "可養寵物",
    "寵物友善",
    "高齡友善",
)

PRIORITY_MARKERS = (
    "屋主直租",
    "屋主自租",
    "屋主本人",
    "免仲介費",
    "仲介勿擾",
    "社宅勿擾",
)

session = requests.Session()
session.headers.update(
    {
        "User-Agent": UA,
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.6",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }
)


@dataclass
class Listing:
    source: str
    source_id: str
    url: str
    title: str = ""
    district: str = ""
    address: str = ""
    house_type: str = ""
    building_type: str = ""
    floor: str = ""
    layout: str = ""
    size: str = ""
    equipment: str = ""
    rent: int = 0
    old_rent: int = 0
    min_lease: str = ""
    updated: str = ""
    views: str = ""
    publisher: str = ""
    image: str = ""
    summary: str = ""
    category_hint: str = ""
    category: str = ""
    fingerprint: str = ""
    validated_at: str = ""
    raw_text: str = field(default="", repr=False)


class BrowserFetcher:
    """單一 Chromium session，供 591 SSR / 反機器人頁面備援。"""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None

    def start(self) -> bool:
        if self._context is not None:
            return True
        if sync_playwright is None:
            return False
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            self._context = self._browser.new_context(
                locale="zh-TW",
                timezone_id="Asia/Taipei",
                user_agent=UA,
                viewport={"width": 1440, "height": 1800},
            )
            return True
        except Exception as exc:
            print(f"[WARN] Chromium 啟動失敗：{exc}", file=sys.stderr)
            self.close()
            return False

    def html(self, url: str, wait_ms: int = 2200) -> str:
        if not self.start():
            return ""
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=50000)
            page.wait_for_timeout(wait_ms)
            return page.content()
        except Exception as exc:
            print(f"[WARN] Browser fetch failed: {url}: {exc}", file=sys.stderr)
            return ""
        finally:
            page.close()

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._context = self._browser = self._pw = None


browser = BrowserFetcher()


def clean(value: Any, limit: int = 500) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def money(value: str | int) -> int:
    if isinstance(value, int):
        return value
    values = re.findall(r"\d[\d,]*", value or "")
    if not values:
        return 0
    return int(values[-1].replace(",", ""))


def get_requests(url: str, *, attempts: int = 3) -> tuple[requests.Response | None, str]:
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=30, allow_redirects=True)
            text = response.text
            if response.status_code in {429, 500, 502, 503, 504}:
                raise requests.RequestException(f"temporary status {response.status_code}")
            return response, text
        except requests.RequestException as exc:
            if attempt + 1 >= attempts:
                print(f"[WARN] GET failed: {url}: {exc}", file=sys.stderr)
                return None, ""
            time.sleep(1.4 * (2**attempt))
    return None, ""


def looks_blocked(raw: str) -> bool:
    lowered = (raw or "").lower()
    return len(raw or "") < 800 or any(marker in lowered for marker in BLOCK_MARKERS)


def fetch_html(
    url: str,
    *,
    browser_fallback: bool = False,
    browser_first: bool = False,
) -> tuple[requests.Response | None, str]:
    """取得 HTML。

    關鍵修正：只要 Chromium 已拿到有效 HTML，就回傳 ``(None, rendered)``，
    不再把 requests 先前的 401 / 403 / 429 狀態帶到後續驗證。
    """

    if browser_first:
        rendered = browser.html(url)
        if rendered and not looks_blocked(rendered):
            return None, rendered

    response, raw = get_requests(url)

    should_use_browser = browser_fallback and (
        response is None
        or response.status_code in {401, 403, 429}
        or looks_blocked(raw)
    )

    if should_use_browser:
        rendered = browser.html(url)
        if rendered:
            if not looks_blocked(rendered):
                return None, rendered
            raw = rendered

    return response, raw


def is_dead_page(response: requests.Response | None, raw: str, expected_host: str) -> bool:
    if response is not None:
        if response.status_code in {404, 410}:
            return True
        final_host = urllib.parse.urlparse(response.url).hostname or ""
        if expected_host not in final_host:
            return True
    text = clean(BeautifulSoup(raw, "html.parser").get_text(" "), 200000)
    return any(marker in text for marker in INVALID_MARKERS)


def meta(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        node = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if node and node.get("content"):
            return clean(node["content"], 1200)
    return ""


def iter_json_ld(soup: BeautifulSoup) -> Iterable[dict[str, Any]]:
    for node in soup.select('script[type*="ld+json"]'):
        try:
            data = json.loads(node.get_text(strip=True))
        except (TypeError, json.JSONDecodeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            value = stack.pop(0)
            if isinstance(value, dict):
                yield value
                graph = value.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
            elif isinstance(value, list):
                stack.extend(value)


def json_ld_title_image(soup: BeautifulSoup) -> tuple[str, str]:
    title = ""
    image = ""
    for value in iter_json_ld(soup):
        if not title:
            title = clean(value.get("name") or value.get("headline") or "", 180)
        if not image:
            raw_image = value.get("image")
            if isinstance(raw_image, list) and raw_image:
                image = str(raw_image[0])
            elif isinstance(raw_image, dict):
                image = str(raw_image.get("url") or "")
            elif raw_image:
                image = str(raw_image)
        if title and image:
            break
    return title, image


def json_ld_offer_price(soup: BeautifulSoup) -> int:
    for value in iter_json_ld(soup):
        offers = value.get("offers")
        if isinstance(offers, dict):
            price = money(str(offers.get("price", "")))
            if 3000 <= price <= 1_000_000:
                return price
    return 0


def normalize_item_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def has_four_rooms(text: str) -> bool:
    return any(int(v) >= 4 for v in re.findall(r"(\d+)\s*房", text or ""))


def district_from_text(text: str) -> str:
    match = re.search(r"(桃園區|中壢區|平鎮區|八德區)", text or "")
    return match.group(1) if match else ""


def allowed_district(item: Listing) -> bool:
    return item.district in ALLOWED_DISTRICTS or any(d in item.address for d in ALLOWED_DISTRICTS)


def excluded(text: str) -> bool:
    return any(marker in (text or "") for marker in EXCLUDE_MARKERS)


def fingerprint(item: Listing) -> str:
    address = re.sub(r"[^\w\u4e00-\u9fff]", "", item.address.lower())
    title = re.sub(r"[^\w\u4e00-\u9fff]", "", item.title.lower())
    layout = re.sub(r"\s+", "", item.layout)
    base = f"{address}|{item.rent}|{layout}|{title[:42]}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# 591
# ---------------------------------------------------------------------------


def extract_591_ids(raw: str) -> list[str]:
    soup = BeautifulSoup(raw, "html.parser")
    ids: list[str] = []

    for anchor in soup.select("a[href]"):
        href = urllib.parse.urljoin("https://rent.591.com.tw", anchor.get("href", ""))
        for pattern in (
            r"rent\.591\.com\.tw/(?:home/house/detail/)?(\d{7,9})(?:$|[/?#])",
            r"rent-detail-(\d{7,9})",
        ):
            match = re.search(pattern, href)
            if match:
                ids.append(match.group(1))

    for node in soup.select("[data-id], [data-houseid], [data-house-id], [data-post-id]"):
        for key in ("data-id", "data-houseid", "data-house-id", "data-post-id"):
            value = str(node.get(key, ""))
            if re.fullmatch(r"\d{7,9}", value):
                ids.append(value)

    patterns = (
        r'https?:\\?/\\?/rent\.591\.com\.tw\\?/(?:home/house/detail/)?(\d{7,9})',
        r'["\'](?:post_id|houseid|house_id|houseId)["\']\s*[:=]\s*["\']?(\d{7,9})',
        r'/(?:home/house/detail/)?(\d{7,9})(?:["\'?/#])',
    )
    for pattern in patterns:
        ids.extend(re.findall(pattern, raw))

    unique: list[str] = []
    seen: set[str] = set()
    for item_id in ids:
        if item_id not in seen:
            seen.add(item_id)
            unique.append(item_id)
    return unique[:120]


_591_LIST_CACHE: dict[str, Listing] = {}


def _591_image_from_node(node: Any) -> str:
    """從列表卡片找圖片；允許 591 CDN / 第三方 CDN，不再限制網域。"""
    if not hasattr(node, "select"):
        return ""

    bad_words = ("logo", "icon", "avatar", "loading", "placeholder", "sprite")

    for img in node.select("img"):
        for key in ("src", "data-src", "data-original", "data-lazy-src"):
            value = str(img.get(key, "") or "").strip()
            if value.startswith("//"):
                value = "https:" + value
            if value.startswith("http") and not any(word in value.lower() for word in bad_words):
                return value

        srcset = str(img.get("srcset", "") or "").strip()
        if srcset:
            value = srcset.split(",")[0].strip().split(" ")[0]
            if value.startswith("//"):
                value = "https:" + value
            if value.startswith("http") and not any(word in value.lower() for word in bad_words):
                return value

    return ""


def parse_591_list_cards(raw: str) -> dict[str, Listing]:
    """從 591 列表頁建立當次快照，詳情頁仍被擋時作最後備援。"""
    soup = BeautifulSoup(raw, "html.parser")
    result: dict[str, Listing] = {}

    for anchor in soup.select("a[href]"):
        href = urllib.parse.urljoin("https://rent.591.com.tw", anchor.get("href", ""))
        match = re.search(
            r"rent\.591\.com\.tw/(?:home/house/detail/)?(\d{7,9})(?:$|[/?#])",
            href,
        )
        if not match:
            continue
        item_id = match.group(1)

        card = None
        node = anchor
        for _ in range(10):
            node = getattr(node, "parent", None)
            if node is None:
                break
            text = clean(node.get_text(" ", strip=True), 16000) if hasattr(node, "get_text") else ""
            has_rent = bool(
                re.search(r"[\d,]{4,}\s*元\s*/?\s*月", text)
                or re.search(r"(?:租金|月租)\s*[:：]?\s*[\d,]{4,}\s*元", text)
            )
            if district_from_text(text) and has_four_rooms(text) and has_rent:
                card = node
                break

        if card is None:
            continue

        text = clean(card.get_text(" ", strip=True), 16000)
        if excluded(text) or not has_four_rooms(text):
            continue

        district = district_from_text(text)
        if district not in ALLOWED_DISTRICTS:
            continue

        rent_match = (
            re.search(r"([\d,]{4,})\s*元\s*/?\s*月", text)
            or re.search(r"(?:租金|月租)\s*[:：]?\s*([\d,]{4,})\s*元", text)
        )
        if not rent_match:
            continue
        rent = money(rent_match.group(1))

        layout_match = re.search(r"(\d+\s*房(?:\s*\d+\s*廳)?(?:\s*\d+\s*衛)?)", text)
        size_match = re.search(r"(\d+(?:\.\d+)?\s*坪)", text)
        floor_match = re.search(r"((?:B?\d+(?:~|～|-)\d+F|整棟|\d+F)\s*/\s*\d+F)", text, re.I)
        building_match = re.search(r"(電梯大樓|電梯華廈|華廈|公寓|透天厝|別墅|樓中樓)", text)
        address_match = re.search(
            r"((?:桃園區|中壢區|平鎮區|八德區)\s*[-－]?\s*[^距]{1,65})",
            text,
        )
        publisher_match = re.search(r"((?:屋主|仲介|代理人)\s*[^\d\s]{1,24})", text)
        updated_match = re.search(r"((?:\d+分鐘|\d+小時|\d+天)(?:內|前)?更新)", text)
        views_match = re.search(r"(昨日\d+人瀏覽|\d+人瀏覽)", text)

        title = clean(anchor.get_text(" ", strip=True), 180)
        image = _591_image_from_node(card)
        if (not title or title in {"優選好屋", "精選"}) and hasattr(card, "select_one"):
            img = card.select_one("img[alt]")
            if img:
                title = clean(img.get("alt", ""), 180)
        if not title:
            title = clean(text.split("整層住家", 1)[0], 180)

        equipment = [
            name
            for name in (
                "冰箱", "洗衣機", "電視", "冷氣", "熱水器", "床", "衣櫃",
                "第四台", "網路", "天然瓦斯", "沙發", "桌椅", "陽台", "電梯", "車位",
            )
            if name in text
        ]

        item = Listing(
            source="591",
            source_id=item_id,
            url=f"https://rent.591.com.tw/{item_id}",
            title=title,
            district=district,
            address=clean(address_match.group(1), 100) if address_match else district,
            house_type="整層住家",
            building_type=building_match.group(1) if building_match else "",
            floor=floor_match.group(1).replace(" ", "") if floor_match else "",
            layout=re.sub(r"\s+", "", layout_match.group(1)) if layout_match else "",
            size=re.sub(r"\s+", "", size_match.group(1)) if size_match else "",
            equipment="、".join(equipment),
            rent=rent,
            updated=updated_match.group(1) if updated_match else "",
            views=views_match.group(1) if views_match else "",
            publisher=publisher_match.group(1) if publisher_match else "",
            image=image,
            summary=text,
            raw_text=text,
            validated_at=NOW.isoformat(),
        )

        # Cache 可以先保留沒有圖片的資料；真正發布時仍要求有圖片。
        if not item.title or not item.rent:
            continue

        if any(marker in text for marker in PRIORITY_MARKERS) or item.publisher.startswith("屋主"):
            item.category_hint = "owner"
        if "降價" in text:
            item.category_hint = "discount"
        item.fingerprint = fingerprint(item)

        existing = result.get(item_id)
        if existing is None or len(item.summary) > len(existing.summary):
            result[item_id] = item

    return result


def crawl_591_links(source_stats: dict[str, Any]) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()

    for district, section in DISTRICTS_591.items():
        empty_pages = 0
        for page_no in range(1, 101):
            query = {
                "kind": 1,
                "layout": 4,
                "region": 6,
                "section": section,
                "page": page_no,
                "sort": "posttime_desc",
            }
            url = "https://rent.591.com.tw/list?" + urllib.parse.urlencode(query)

            _, raw = fetch_html(url, browser_fallback=True)
            ids = extract_591_ids(raw)
            cards = parse_591_list_cards(raw)

            if not ids or not cards:
                rendered = browser.html(url)
                if rendered:
                    ids = extract_591_ids(rendered) or ids
                    cards.update(parse_591_list_cards(rendered))

            for item_id, item in cards.items():
                existing = _591_LIST_CACHE.get(item_id)
                if existing is None or len(item.summary) > len(existing.summary):
                    _591_LIST_CACHE[item_id] = item

            new_ids = [item_id for item_id in ids if item_id not in seen]
            print(
                f"[591] {district} page={page_no} ids={len(ids)} "
                f"cards={len(cards)} cache={len(_591_LIST_CACHE)} new={len(new_ids)}"
            )

            if not new_ids:
                empty_pages += 1
                if empty_pages >= 2:
                    break
            else:
                empty_pages = 0

            for item_id in new_ids:
                seen.add(item_id)
                links.append(f"https://rent.591.com.tw/{item_id}")

            time.sleep(0.35)

    source_stats["candidate_links"] = len(links)
    if not links:
        source_stats["errors"].append(
            "591列表頁未取得物件編號；可能是GitHub Runner被591阻擋。"
            "請查看Actions記錄確認Chromium是否成功安裝與啟動。"
        )
    elif not _591_LIST_CACHE:
        source_stats["errors"].append(
            "591已取得候選物件編號，但列表快照為0筆；將以Chromium詳情頁驗證為主。"
        )
    return links


def _591_detail_rent(soup: BeautifulSoup, text: str) -> int:
    price = json_ld_offer_price(soup)
    if price:
        return price

    for pattern in (
        r"(?:租金|月租)\s*[:：]?\s*([\d,]{4,})\s*元",
        r"([\d,]{4,})\s*元\s*/\s*月",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = money(match.group(1))
            if 3000 <= parsed <= 1_000_000:
                return parsed
    return 0


def parse_591_detail(url: str) -> Listing | None:
    item_id_match = re.search(r"(\d{7,9})", urllib.parse.urlparse(url).path)
    item_id = item_id_match.group(1) if item_id_match else hashlib.md5(url.encode()).hexdigest()[:16]
    cached = _591_LIST_CACHE.get(item_id)

    # 關鍵修正：591 詳情頁直接 Chromium 優先。
    response, raw = fetch_html(url, browser_first=True, browser_fallback=True)

    if response is not None and response.status_code in {404, 410}:
        return None

    # 只有真正仍被擋住時，才退回列表快照。
    if looks_blocked(raw):
        if cached and cached.title and cached.rent and cached.image and allowed_district(cached):
            return cached
        return None

    if is_dead_page(response, raw, "591.com.tw"):
        return None

    soup = BeautifulSoup(raw, "html.parser")
    text = clean(soup.get_text(" "), 220000)

    if not has_four_rooms(text) or excluded(text):
        return None

    json_title, json_image = json_ld_title_image(soup)
    title = meta(soup, "og:title", "twitter:title") or json_title
    image = meta(soup, "og:image", "twitter:image") or json_image
    description = meta(soup, "og:description", "description")

    layout_match = re.search(r"(\d+\s*房\s*\d*\s*廳?\s*\d*\s*衛?)", text)
    size_match = re.search(r"(\d+(?:\.\d+)?\s*坪)", text)
    floor_match = re.search(r"((?:B?\d+(?:~|～|-)\d+F|整棟|\d+F)\s*/\s*\d+F)", text, re.I)
    building_match = re.search(r"(電梯大樓|電梯華廈|華廈|公寓|透天厝|別墅|樓中樓)", text)
    address_match = re.search(
        r"(?:地址\s*[:：]?\s*)?(桃園市?\s*)?((?:桃園區|中壢區|平鎮區|八德區)[^。|]{1,80})",
        text,
    )
    publisher_match = re.search(r"((?:屋主|仲介)[:：]?\s*[^0-9|]{1,35})", text)
    updated_match = re.search(r"((?:\d+分鐘|\d+小時|\d+天)內?更新|\d+天前更新)", text)
    views_match = re.search(r"(昨日\d+人瀏覽|\d+人瀏覽)", text)
    lease_match = re.search(r"最短租期\s*([^，。|]{1,20})", text)

    equipment = [
        name
        for name in (
            "冰箱", "洗衣機", "電視", "冷氣", "熱水器", "床", "衣櫃",
            "第四台", "網路", "天然瓦斯", "沙發", "桌椅", "陽台", "電梯", "車位",
        )
        if name in text
    ]

    item = Listing(
        source="591",
        source_id=item_id,
        url=f"https://rent.591.com.tw/{item_id}",
        title=clean(title, 180),
        district=district_from_text(text),
        address=clean(address_match.group(2), 100) if address_match else "",
        house_type="整層住家",
        building_type=building_match.group(1) if building_match else "",
        floor=floor_match.group(1).replace(" ", "") if floor_match else "",
        layout=re.sub(r"\s+", "", layout_match.group(1)) if layout_match else "",
        size=re.sub(r"\s+", "", size_match.group(1)) if size_match else "",
        equipment="、".join(equipment),
        rent=_591_detail_rent(soup, text),
        min_lease=lease_match.group(1) if lease_match else "",
        updated=updated_match.group(1) if updated_match else "",
        views=views_match.group(1) if views_match else "",
        publisher=publisher_match.group(1) if publisher_match else "",
        image=image,
        summary=description,
        raw_text=text,
        validated_at=NOW.isoformat(),
    )

    # 詳情頁欄位不足時，可用當次列表快照補值，但不能覆蓋詳情頁已取得的值。
    if cached:
        for attr in (
            "title", "district", "address", "building_type", "floor", "layout",
            "size", "equipment", "min_lease", "updated", "views", "publisher",
            "image", "summary",
        ):
            if not getattr(item, attr):
                setattr(item, attr, getattr(cached, attr))
        if not item.rent:
            item.rent = cached.rent

    if not item.title or not item.rent or not item.image or not allowed_district(item):
        return None

    if any(marker in text for marker in PRIORITY_MARKERS) or item.publisher.startswith("屋主"):
        item.category_hint = "owner"
    elif cached and cached.category_hint:
        item.category_hint = cached.category_hint

    item.fingerprint = fingerprint(item)
    return item


# ---------------------------------------------------------------------------
# 樂屋網
# ---------------------------------------------------------------------------


def rakuya_result_urls(params: dict[str, str]) -> Iterable[str]:
    for page_no in range(1, 101):
        query = dict(params)
        query["page"] = str(page_no)
        yield "https://rent.rakuya.com.tw/result?" + urllib.parse.urlencode(query, safe=",")


def extract_rakuya_links(raw: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(raw, "html.parser")
    links: list[str] = []
    for anchor in soup.select("a[href]"):
        href = urllib.parse.urljoin(base_url, anchor.get("href", ""))
        parsed = urllib.parse.urlparse(href)
        if parsed.hostname not in {"rent.rakuya.com.tw", "community.rakuya.com.tw"}:
            continue
        if re.search(r"/item/[0-9a-f]+", parsed.path) or re.search(r"/\d+/rent/[0-9a-f]+", parsed.path):
            links.append(normalize_item_url(href))
    return list(dict.fromkeys(links))


def crawl_rakuya_links(source_stats: dict[str, Any]) -> dict[str, set[str]]:
    zipcodes = ",".join(DISTRICTS_RAKUYA.values())
    categories: dict[str, set[str]] = {"general": set(), "owner": set(), "friendly": set()}

    query_sets: list[tuple[str, dict[str, str]]] = []
    for room in ("4", "5"):
        query_sets.append(("general", {"zipcode": zipcodes, "room": room}))
        query_sets.append(("owner", {"zipcode": zipcodes, "room": room, "usecode": "7"}))
        for keyword in ("可入籍", "租補", "可養寵物", "寵物友善", "高齡友善"):
            query_sets.append(("friendly", {"zipcode": zipcodes, "room": room, "keyword": keyword}))

    for category, params in query_sets:
        no_new = 0
        for page_url in rakuya_result_urls(params):
            response, raw = get_requests(page_url)
            if response is None:
                source_stats["errors"].append(f"樂屋網搜尋頁無法讀取：{page_url}")
                break

            links = extract_rakuya_links(raw, response.url)
            new_links = set(links) - categories[category]
            categories[category].update(new_links)
            print(f"[Rakuya] {category} page={page_url} new={len(new_links)}")

            if not new_links:
                no_new += 1
                if no_new >= 2:
                    break
            else:
                no_new = 0

            if "符合條件的房屋已瀏覽完畢" in raw:
                break

    source_stats["candidate_links"] = len(set().union(*categories.values()))
    return categories


def explicit_old_price(soup: BeautifulSoup, text: str, current_rent: int) -> int:
    """只接受明確原價，不把押金或保證金當成原租金。"""
    matches: list[int] = []

    for pattern in (
        r"(?:原租金|原價|降價前)\s*[:：]?\s*([\d,]+)\s*元",
        r"([\d,]+)\s*元\s*(?:降至|調降至|降為)",
    ):
        for value in re.findall(pattern, text):
            parsed = money(value)
            if parsed > current_rent:
                matches.append(parsed)

    for node in soup.select("del, s, [class*='old-price'], [class*='origin-price'], [class*='original-price']"):
        parsed = money(node.get_text(" ", strip=True))
        if parsed > current_rent:
            matches.append(parsed)

    return min(matches) if matches else 0


def parse_rakuya_detail(url: str, hints: set[str]) -> Listing | None:
    response, raw = get_requests(url)
    if is_dead_page(response, raw, "rakuya.com.tw"):
        return None

    soup = BeautifulSoup(raw, "html.parser")
    text = clean(soup.get_text(" "), 220000)

    if not has_four_rooms(text) or excluded(text):
        return None

    path = urllib.parse.urlparse(response.url).path if response else urllib.parse.urlparse(url).path
    item_id_match = re.search(r"(?:/item/|/rent/)([0-9a-f]+)", path)
    item_id = item_id_match.group(1) if item_id_match else hashlib.md5(url.encode()).hexdigest()[:16]

    title = meta(soup, "og:title", "twitter:title") or clean(soup.title.get_text(" ") if soup.title else "", 180)
    image = meta(soup, "og:image", "twitter:image")
    description = meta(soup, "og:description", "description")

    layout_match = re.search(r"(\d+房\d*廳?\d*衛?)", text)
    size_match = re.search(r"(?:主建|室內|建坪|坪數)?\s*(\d+(?:\.\d+)?坪)", text)
    floor_match = re.search(r"((?:B?\d+(?:~|～|-)\d+|整棟|\d+)\s*/\s*\d+樓)", text, re.I)
    type_match = re.search(r"(電梯大廈|電梯華廈|華廈|公寓|透天厝|別墅|樓中樓)", text)

    current_rent = json_ld_offer_price(soup)
    if not current_rent:
        monthly = [money(v) for v in re.findall(r"([\d,]+)\s*元(?:\s*/\s*月)?", text)]
        monthly = [v for v in monthly if 3000 <= v <= 1_000_000]
        current_rent = monthly[-1] if monthly else 0

    old_rent = explicit_old_price(soup, text, current_rent)

    district = district_from_text(title + " " + text)
    address = ""
    title_address = re.search(
        r"桃園市?(桃園區|中壢區|平鎮區)([^\-｜|]{1,45})[\-｜|]",
        title,
    )
    if title_address:
        address = f"{title_address.group(1)}{clean(title_address.group(2), 50)}"
    else:
        address_match = re.search(
            r"(桃園區|中壢區|平鎮區)\s*([^。|]{1,50}(?:路|街|巷|弄))",
            text,
        )
        if address_match:
            address = f"{address_match.group(1)}{clean(address_match.group(2), 60)}"

    updated_match = re.search(r"((?:\d+分鐘|\d+小時|\d+天|\d+個月)前更新)", text)
    views_match = re.search(r"(\d+次瀏覽|新上架)", text)

    features = [
        name
        for name in ("附傢俱", "附設備", "可開伙", "可養寵物", "可入籍", "租補", "租金補助")
        if name in text
    ]

    item = Listing(
        source="樂屋網",
        source_id=item_id,
        url=normalize_item_url(response.url if response else url),
        title=clean(title, 180),
        district=district,
        address=address,
        house_type="整層住家" if "整層住家" in text else "",
        building_type=type_match.group(1) if type_match else "",
        floor=floor_match.group(1).replace(" ", "") if floor_match else "",
        layout=layout_match.group(1) if layout_match else "",
        size=size_match.group(1) if size_match else "",
        equipment="、".join(features),
        rent=current_rent,
        old_rent=old_rent,
        updated=updated_match.group(1) if updated_match else "",
        views=views_match.group(1) if views_match else "",
        image=image,
        summary=description,
        raw_text=text,
        validated_at=NOW.isoformat(),
    )

    if not item.title or not item.rent or not item.image or not allowed_district(item):
        return None

    if "owner" in hints or any(marker in text for marker in PRIORITY_MARKERS):
        item.category_hint = "owner"
    elif "friendly" in hints or any(marker in text for marker in FRIENDLY_MARKERS):
        item.category_hint = "friendly"

    if item.old_rent > item.rent:
        item.category_hint = "discount"

    item.fingerprint = fingerprint(item)
    return item


# ---------------------------------------------------------------------------
# Facebook 安全匯入
# ---------------------------------------------------------------------------


def parse_social_row(row: dict[str, Any], source: str) -> Listing | None:
    text = clean(" ".join(str(row.get(k, "")) for k in row), 8000)
    if not has_four_rooms(text) or excluded(text):
        return None

    district = clean(row.get("district") or district_from_text(text), 20)
    if district not in ALLOWED_DISTRICTS:
        return None

    url = str(row.get("url", "")).strip()
    image = str(row.get("image", "")).strip()
    title = clean(row.get("title") or text.split("。")[0], 180)
    rent = money(str(row.get("rent", "")))

    if not url or not image or not title or not rent:
        return None

    item = Listing(
        source=source,
        source_id=hashlib.md5(url.encode()).hexdigest()[:18],
        url=url,
        title=title,
        district=district,
        address=clean(row.get("address", ""), 100),
        house_type=clean(row.get("house_type", ""), 30),
        building_type=clean(row.get("building_type", ""), 30),
        floor=clean(row.get("floor", ""), 30),
        layout=clean(row.get("layout", ""), 30),
        equipment=clean(row.get("equipment", ""), 220),
        rent=rent,
        min_lease=clean(row.get("min_lease", ""), 30),
        image=image,
        summary=clean(row.get("summary", ""), 500),
        category_hint="priority" if any(marker in text for marker in PRIORITY_MARKERS) else "general",
        raw_text=text,
        validated_at=NOW.isoformat(),
    )
    item.fingerprint = fingerprint(item)
    return item


def load_facebook_import(source_stats: dict[str, Any]) -> list[Listing]:
    if not FB_IMPORT.exists():
        source_stats["errors"].append(
            "尚未建立 data/facebook_posts.json。Meta 已移除可讀取社團新貼文的 Groups API，"
            "因此GitHub Actions無法僅憑社團網址自動取得新貼文。"
        )
        return []

    try:
        rows = json.loads(FB_IMPORT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        source_stats["errors"].append(f"facebook_posts.json 無法讀取：{exc}")
        return []

    result: list[Listing] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url", ""))
        if not re.match(r"https://www\.facebook\.com/groups/[^/]+/(?:posts|permalink)/", url):
            continue
        item = parse_social_row(row, "FB")
        if item:
            result.append(item)

    source_stats["candidate_links"] = len(rows) if isinstance(rows, list) else 0
    source_stats["validated"] = len(result)
    return result


# ---------------------------------------------------------------------------
# 分類、去重與版面
# ---------------------------------------------------------------------------


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sent": [], "prices": {}}


def save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_categories(items: list[Listing], state: dict[str, Any]) -> list[Listing]:
    prices = state.setdefault("prices", {})

    for item in items:
        previous = int(prices.get(f"{item.source}:{item.source_id}", 0) or 0)

        if item.source == "FB":
            item.category = "priority" if item.category_hint == "priority" else "general"
        elif item.category_hint == "owner":
            item.category = "owner"
        elif item.category_hint == "discount" or item.old_rent > item.rent or (previous and item.rent < previous):
            item.category = "discount"
            if not item.old_rent:
                item.old_rent = previous
        elif item.source == "樂屋網" and item.category_hint == "friendly":
            item.category = "friendly"
        else:
            item.category = "general"

        prices[f"{item.source}:{item.source_id}"] = item.rent

    return items


def filter_recent_duplicates(items: list[Listing], state: dict[str, Any]) -> tuple[list[Listing], int]:
    cutoff = NOW - timedelta(hours=48)
    retained_history: list[dict[str, Any]] = []
    recent_keys: set[str] = set()

    for row in state.get("sent", []):
        try:
            sent_at = datetime.fromisoformat(row["sent_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if sent_at >= cutoff:
            retained_history.append(row)
            recent_keys.add(row.get("source_key", ""))
            recent_keys.add(row.get("fingerprint", ""))

    output: list[Listing] = []
    removed = 0

    for item in items:
        source_key = f"{item.source}:{item.source_id}"
        if item.category != "discount" and (
            source_key in recent_keys or item.fingerprint in recent_keys
        ):
            removed += 1
            continue

        output.append(item)
        retained_history.append(
            {
                "source_key": source_key,
                "fingerprint": item.fingerprint,
                "sent_at": NOW.isoformat(),
                "title": item.title,
                "url": item.url,
            }
        )

    state["sent"] = retained_history[-5000:]
    return output, removed


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def source_label(source: str) -> str:
    return {"591": "591", "FB": "FB社團", "樂屋網": "樂屋網"}.get(source, source)


def category_label(item: Listing) -> str:
    labels = {
        ("591", "owner"): "屋主直租",
        ("591", "discount"): "降價物件",
        ("591", "general"): "一般物件",
        ("樂屋網", "owner"): "屋主",
        ("樂屋網", "discount"): "最新降價",
        ("樂屋網", "friendly"): "友善房源",
        ("樂屋網", "general"): "出租",
        ("FB", "priority"): "優先置頂",
        ("FB", "general"): "其他符合物件",
    }
    return labels.get((item.source, item.category), item.category)


def render_card(item: Listing) -> str:
    old_html = (
        f'<div class="old">原租金：<del>{item.old_rent:,} 元／月</del></div>'
        if item.old_rent > item.rent
        else ""
    )

    details = [
        ("房屋類型", item.house_type),
        ("房屋型態", item.building_type),
        ("出租地址", item.address),
        ("總樓層／層別", item.floor),
        ("格局", item.layout),
        ("提供設備", item.equipment),
    ]
    if item.source in {"591", "FB"}:
        details.append(("最短租期", item.min_lease))

    detail_html = "".join(
        f"<div><span>{esc(label)}</span><b>{esc(value or '未提供')}</b></div>"
        for label, value in details
    )
    activity = "・".join(v for v in (item.updated, item.views) if v)

    return f"""
    <article class="card">
      <a class="photo" href="{esc(item.url)}" target="_blank" rel="noopener noreferrer">
        <img src="{esc(item.image)}" alt="{esc(item.title)}" referrerpolicy="no-referrer"
             onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">
        <div class="photo-fallback">照片暫時無法載入<br>點擊前往來源頁</div>
        <span>{esc(source_label(item.source))}｜{esc(category_label(item))}</span>
      </a>
      <div class="body">
        <small>{esc(item.district)}</small>
        <h3><a href="{esc(item.url)}" target="_blank" rel="noopener noreferrer">{esc(item.title)}</a></h3>
        <p class="summary">{esc(item.layout or '格局未提供')}・{esc(item.size or '坪數未提供')}・{esc(item.floor or '樓層未提供')}</p>
        <div class="details">{detail_html}</div>
        {old_html}
        <div class="rent">{item.rent:,} 元／月</div>
        <div class="activity">{esc(activity)}</div>
        <a class="button" href="{esc(item.url)}" target="_blank" rel="noopener noreferrer">物件直達連結 ↗</a>
      </div>
    </article>
    """


def empty_message(stats: dict[str, Any], source: str) -> str:
    row = stats["sources"][source]
    candidate = int(row.get("candidate_links", 0) or 0)
    validated = int(row.get("validated", 0) or 0)

    if candidate > 0 and validated == 0:
        return (
            f"本次有取得 {source_label(source)} 候選物件，但沒有物件通過來源驗證。"
            "請查看上方「本次紀錄」與來源訊息。"
        )
    if validated > 0:
        return "本區目前沒有新的物件；可能屬於其他分類，或已於近48小時顯示過。"
    return "本次沒有取得符合條件的候選物件。"


def render_subsection(
    items: list[Listing],
    stats: dict[str, Any],
    source: str,
    category: str,
    title: str,
) -> str:
    values = [item for item in items if item.source == source and item.category == category]
    cards = "".join(render_card(item) for item in values)
    if not cards:
        cards = f'<div class="empty">{esc(empty_message(stats, source))}</div>'
    return (
        f'<section class="subsection"><header><h2>{esc(title)}</h2><b>{len(values)} 筆</b></header>'
        f'<div class="cards">{cards}</div></section>'
    )


def render_status(stats: dict[str, Any], source: str) -> str:
    row = stats["sources"][source]
    errors = row.get("errors", [])
    error_html = "".join(f"<li>{esc(error)}</li>" for error in errors[:8])
    return f"""
    <div class="source-status">
      <b>本次紀錄</b>
      <span>候選 {row.get('candidate_links', 0)} 筆</span>
      <span>驗證通過 {row.get('validated', 0)} 筆</span>
      <span>本頁顯示 {row.get('published', 0)} 筆</span>
      {f'<details><summary>查看來源訊息</summary><ul>{error_html}</ul></details>' if errors else ''}
    </div>
    """


def render_html(items: list[Listing], stats: dict[str, Any]) -> str:
    fb_buttons = "".join(
        f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">FB社團 {idx:02d} ↗</a>'
        for idx, url in enumerate(FB_GROUPS, 1)
    )

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>桃園四房以上租屋快報</title>
<style>
:root{{--orange:#f46b18;--bg:#f3f4f6;--line:#e1e4e8;--muted:#68717d;--fb:#1877f2;--raku:#d65431}}
*{{box-sizing:border-box}}
html{{scroll-behavior:smooth}}
body{{margin:0;background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",sans-serif;color:#202124}}
a{{color:inherit}}
.wrap{{width:min(1220px,calc(100% - 28px));margin:auto}}
body>header{{
  position:sticky;
  top:0;
  z-index:1000;
  background:linear-gradient(135deg,rgba(255,255,255,.98),rgba(255,244,231,.98));
  border-bottom:4px solid var(--orange);
  padding:14px 0 12px;
  box-shadow:0 5px 20px rgba(0,0,0,.13);
  backdrop-filter:blur(10px);
}}
h1{{font-size:clamp(25px,4vw,36px);margin:0 0 6px}}
.subtitle{{font-size:15px;line-height:1.45;color:#4e5660;margin:0}}
.source-nav{{display:flex;gap:9px;flex-wrap:wrap;margin-top:9px}}
.source-nav a{{text-decoration:none;background:#fff;padding:10px 18px;border-radius:9px;font-weight:900;box-shadow:0 2px 9px #0001}}
.source-nav a:nth-child(2){{color:var(--fb)}}
.source-nav a:nth-child(3){{color:var(--raku)}}
.statusbar{{background:#23272d;color:#fff;padding:12px 0;font-size:14px}}
main{{padding:22px 0 48px}}
.notice{{background:#fff;border-left:5px solid var(--orange);padding:15px 18px;border-radius:10px;line-height:1.7}}
.source-block{{margin-top:22px;scroll-margin-top:175px}}
.source-heading{{display:flex;align-items:end;justify-content:space-between;gap:12px;margin-bottom:10px}}
.source-heading h2{{font-size:31px;margin:0}}
.source-heading a{{font-size:14px;color:#555}}
.source-status{{display:flex;gap:8px 14px;align-items:center;flex-wrap:wrap;background:#fff;padding:12px 14px;border:1px solid var(--line);border-radius:10px}}
.source-status span{{color:#555}}
.source-status details{{width:100%;color:#8a3f00}}
.source-status ul{{margin:8px 0 0;padding-left:20px}}
.subsection{{margin-top:14px;background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px}}
.subsection>header{{display:flex;justify-content:space-between;align-items:end;gap:12px}}
.subsection h2{{margin:0;font-size:25px}}
.cards{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px;margin-top:14px}}
.card{{border:1px solid var(--line);border-radius:11px;overflow:hidden;background:#fff}}
.photo{{height:275px;display:block;position:relative;background:#596273}}
.photo img{{width:100%;height:100%;object-fit:cover}}
.photo>span{{position:absolute;left:12px;top:12px;background:#000b;color:#fff;padding:7px 9px;border-radius:6px;font-weight:800}}
.photo-fallback{{display:none;position:absolute;inset:0;align-items:center;justify-content:center;text-align:center;color:#fff;font-weight:900;background:#4b5563}}
.body{{padding:16px}}
small{{color:var(--orange);font-weight:900}}
h3{{font-size:21px;line-height:1.4;margin:7px 0}}
h3 a{{text-decoration:none}}
.summary{{font-weight:800}}
.details{{display:grid;gap:7px;border-top:1px solid #eee;padding-top:11px}}
.details div{{display:grid;grid-template-columns:100px 1fr;gap:8px;font-size:14px;line-height:1.5}}
.details span{{color:var(--muted)}}
.old{{margin-top:10px;color:#8a9098}}
.rent{{font-size:28px;color:#d95700;font-weight:950;margin-top:8px}}
.activity{{color:var(--muted);font-size:14px}}
.button{{display:block;text-align:center;margin-top:12px;padding:11px;background:var(--orange);color:#fff;text-decoration:none;border-radius:7px;font-weight:900}}
.empty{{border:1px dashed #bbb;border-radius:8px;padding:25px;text-align:center;color:var(--muted);grid-column:1/-1}}
.social-links{{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}}
.social-links a{{background:var(--fb);color:#fff;text-decoration:none;padding:8px 10px;border-radius:6px;font-weight:800}}
.social-note{{background:#fff8e9;border:1px solid #ffd7a6;padding:13px;border-radius:9px;margin-top:12px;line-height:1.7}}
@media(max-width:850px){{
  .cards{{grid-template-columns:1fr}}
}}
@media(max-width:560px){{
  body>header{{padding:10px 0 9px}}
  h1{{font-size:25px}}
  .subtitle{{font-size:13px;line-height:1.4}}
  .source-nav{{gap:6px;margin-top:8px}}
  .source-nav a{{flex:1;text-align:center;padding:9px 6px}}
  .photo{{height:230px}}
  .source-block{{scroll-margin-top:190px}}
}}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>桃園四房以上租屋快報</h1>
    <p class="subtitle">三個來源分區顯示；每筆物件均包含照片與來源直達連結，並排除近48小時重複物件。</p>
    <nav class="source-nav">
      <a href="#source-591">591</a>
      <a href="#source-fb">FB社團</a>
      <a href="#source-rakuya">樂屋網</a>
    </nav>
  </div>
</header>

<div class="statusbar"><div class="wrap">
產生時間：{NOW.strftime('%Y/%m/%d %H:%M')}｜候選 {stats['candidates']} 筆｜
驗證通過 {stats['validated']} 筆｜近48小時重複排除 {stats['duplicates']} 筆｜
本次顯示 {len(items)} 筆
</div></div>

<main class="wrap">
<div class="notice">每個來源都會顯示本次候選與驗證結果。來源被網站阻擋或匯入檔不存在時，會直接顯示原因；空白分類也會區分「驗證失敗」與「近48小時已顯示」。</div>

<div id="source-591" class="source-block">
  <div class="source-heading"><h2>591</h2><a href="https://rent.591.com.tw/list?kind=1&layout=4&region=6" target="_blank">開啟591搜尋 ↗</a></div>
  {render_status(stats, '591')}
  {render_subsection(items, stats, '591', 'owner', '屋主直租')}
  {render_subsection(items, stats, '591', 'discount', '降價物件')}
  {render_subsection(items, stats, '591', 'general', '全部符合條件物件')}
</div>

<div id="source-fb" class="source-block">
  <div class="source-heading"><h2>FB社團</h2><a href="https://www.facebook.com/groups/feed/" target="_blank">開啟Facebook社團 ↗</a></div>
  {render_status(stats, 'FB')}
  <div class="social-note">Facebook社團採安全JSON匯入，不需要提供Facebook帳號、密碼或Cookie。</div>
  <div class="social-links">{fb_buttons}</div>
  {render_subsection(items, stats, 'FB', 'priority', '屋主自租／仲介勿擾／社宅勿擾')}
  {render_subsection(items, stats, 'FB', 'general', '其他符合條件FB物件')}
</div>

<div id="source-rakuya" class="source-block">
  <div class="source-heading"><h2>樂屋網</h2><a href="https://rent.rakuya.com.tw/" target="_blank">開啟樂屋網 ↗</a></div>
  {render_status(stats, '樂屋網')}
  {render_subsection(items, stats, '樂屋網', 'general', '出租')}
  {render_subsection(items, stats, '樂屋網', 'owner', '屋主')}
  {render_subsection(items, stats, '樂屋網', 'friendly', '友善房源')}
  {render_subsection(items, stats, '樂屋網', 'discount', '最新降價')}
</div>
</main>
</body>
</html>"""


def empty_source_stats() -> dict[str, Any]:
    return {"candidate_links": 0, "validated": 0, "published": 0, "errors": []}


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stats: dict[str, Any] = {
        "sources": {
            "591": empty_source_stats(),
            "FB": empty_source_stats(),
            "樂屋網": empty_source_stats(),
        }
    }
    candidates: list[Listing] = []

    try:
        links_591 = crawl_591_links(stats["sources"]["591"])
        valid_591 = 0
        for index, url in enumerate(links_591, 1):
            print(f"[591 detail] {index}/{len(links_591)} {url}")
            item = parse_591_detail(url)
            if item:
                candidates.append(item)
                valid_591 += 1
        stats["sources"]["591"]["validated"] = valid_591
        if links_591 and valid_591 == 0:
            stats["sources"]["591"]["errors"].append(
                "591有取得候選物件，但0筆通過驗證。請在Actions搜尋「[591]」與「[591 detail]」；"
                "本版已改為Chromium詳情頁優先，若仍為0通常代表GitHub Runner仍被591阻擋或頁面結構再次變更。"
            )

        rakuya_map = crawl_rakuya_links(stats["sources"]["樂屋網"])
        hints_by_url: dict[str, set[str]] = {}
        for hint, links in rakuya_map.items():
            for url in links:
                hints_by_url.setdefault(url, set()).add(hint)

        valid_rakuya = 0
        for index, (url, hints) in enumerate(hints_by_url.items(), 1):
            print(f"[Rakuya detail] {index}/{len(hints_by_url)} {url}")
            item = parse_rakuya_detail(url, hints)
            if item:
                candidates.append(item)
                valid_rakuya += 1
        stats["sources"]["樂屋網"]["validated"] = valid_rakuya

        candidates.extend(load_facebook_import(stats["sources"]["FB"]))

    finally:
        browser.close()

    unique: dict[str, Listing] = {}
    for item in candidates:
        unique[f"{item.source}:{item.source_id}"] = item
    candidates = list(unique.values())

    state = load_state()
    candidates = apply_categories(candidates, state)
    published, duplicate_count = filter_recent_duplicates(candidates, state)

    for source in stats["sources"]:
        stats["sources"][source]["published"] = sum(1 for item in published if item.source == source)

    stats.update(
        {
            "candidates": sum(v["candidate_links"] for v in stats["sources"].values()),
            "validated": len(candidates),
            "duplicates": duplicate_count,
            "published": len(published),
        }
    )

    OUTPUT_JSON.write_text(
        json.dumps(
            {
                "generated_at": NOW.isoformat(),
                "stats": stats,
                "items": [asdict(item) for item in published],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    OUTPUT_HTML.write_text(render_html(published, stats), encoding="utf-8")
    save_state(state)

    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
