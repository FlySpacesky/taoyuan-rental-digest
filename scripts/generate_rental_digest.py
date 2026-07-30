#!/usr/bin/env python3
"""
桃園四房以上租屋快報

來源：
- 591：桃園區、中壢區、平鎮區、八德區，整層住家、4房以上
- Facebook：安全 JSON 匯入
- 樂屋網：中壢區、桃園區、平鎮區，4房及5房以上

本版重點：
1. 完全不含 Threads。
2. 591 列表優先讀網站前端使用的官方 BFF；失效時才退回 SSR HTML / Chromium。
3. 591 屋主只接受 role_name / 詳情聯絡人角色明確以「屋主」開頭的物件。
4. 591 降價優先使用 BFF 官方 diff_price，詳情頁阻擋時使用同輪嚴格列表快照。
5. 404 / 410 / 已刪除 / 已關閉 / 已成交物件仍排除。
6. 樂屋網原租金只接受明確舊價標示或歷史快照，避免把押金誤判為原租金。
7. 48 小時來源內與跨來源去重；真正降價物件可重新顯示。
8. 頁首「桃園四房以上租屋快報＋591／FB社團／樂屋網」捲動時固定在頂端。
"""

from __future__ import annotations

import hashlib
import html
import json
import os
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
LAST_SUCCESS_591 = DATA_DIR / "last-success-591.json"
FB_IMPORT = ROOT / "data" / "facebook_posts.json"
FB_IMPORT_ENV = "FACEBOOK_POSTS_JSON"
FB_IMPORT_URL_ENV = "FACEBOOK_POSTS_JSON_URL"
FB_ASSET_DIR = DOCS / "assets" / "facebook"
FB_ASSET_PUBLIC_BASE = (
    "https://flyspacesky.github.io/taoyuan-rental-digest/assets/facebook"
)
FB_ISSUE_TITLE_PREFIX = "[FB房源]"
FB_ISSUE_TEMPLATE_URL = (
    "https://github.com/FlySpacesky/taoyuan-rental-digest/issues/new"
    "?template=facebook-listing.yml"
)
GITHUB_REPOSITORY_ENV = "GITHUB_REPOSITORY"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
FB_PUBLIC_CRAWLER_UA = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; "
    "+http://www.google.com/bot.html)"
)
_591_BFF_LIST_URL = "https://bff-house.591.com.tw/v3/web/rent/list"
_591_REFRESH_COOLDOWN = timedelta(hours=2)
_591_SNAPSHOT_MAX_AGE = timedelta(hours=72)

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
    "八德區": "334",
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
    "https://www.facebook.com/groups/4091621327828556",
]
FB_GROUP_IDS = frozenset(
    urllib.parse.urlparse(group_url).path.strip("/").split("/", 1)[1]
    for group_url in FB_GROUPS
)

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
)

# 「代理人」不能當成整頁關鍵字直接排除，因為 591 的介面/說明本身可能出現這個字。
# 改成只判斷「刊登身分真的為代理人」的語境。
PROXY_ROLE_PATTERNS = (
    r"(?:刊登者|刊登身分|刊登身份|出租人|聯絡人|身分|身份)\s*[:：]?\s*代理人",
    r"代理人\s*[:：]\s*[^\s|，。]{1,24}",
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
    total_cost: int = 0
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
    filter_tags: list[str] = field(default_factory=list, repr=False)


class BrowserFetcher:
    """單一 Chromium session，供 591 SSR / 反機器人頁面備援。"""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self._disabled = False

    def start(self) -> bool:
        if self._disabled:
            return False
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
            self._disabled = True
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
    browser_wait_ms: int = 2200,
) -> tuple[requests.Response | None, str]:
    """取得 HTML。

    關鍵修正：只要 Chromium 已拿到有效 HTML，就回傳 ``(None, rendered)``，
    不再把 requests 先前的 401 / 403 / 429 狀態帶到後續驗證。
    """

    if browser_first:
        rendered = browser.html(url, wait_ms=browser_wait_ms)
        if rendered and not looks_blocked(rendered):
            return None, rendered

    response, raw = get_requests(url)

    should_use_browser = browser_fallback and (
        response is None
        or response.status_code in {401, 403, 429}
        or looks_blocked(raw)
    )

    if should_use_browser:
        rendered = browser.html(url, wait_ms=browser_wait_ms)
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
    """硬性排除字詞；不包含泛用的「代理人」三個字。"""
    return any(marker in (text or "") for marker in EXCLUDE_MARKERS)


def proxy_listing(text: str = "", publisher: str = "") -> bool:
    """只有明確顯示刊登角色為代理人時才排除。

    不能單純用 ``"代理人" in text``，因為 591 網站導覽/說明文字可能包含
    「房東/代理人」等泛用字樣，會把正常屋主物件全部誤殺。
    """
    publisher_text = clean(publisher, 120)
    if publisher_text.startswith("代理人"):
        return True

    compact = clean(text, 24000)
    return any(re.search(pattern, compact) for pattern in PROXY_ROLE_PATTERNS)


def fingerprint(item: Listing) -> str:
    address = re.sub(r"[^\w\u4e00-\u9fff]", "", item.address.lower())
    title = re.sub(r"[^\w\u4e00-\u9fff]", "", item.title.lower())
    layout = re.sub(r"\s+", "", item.layout)
    base = f"{address}|{item.rent}|{layout}|{title[:42]}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# 591
# ---------------------------------------------------------------------------


_591_ITEM_URL_RE = re.compile(
    r"^https?://rent\.591\.com\.tw/(?:(?:home/house/detail|home)/)?"
    r"(\d{7,9})(?:$|[/?#])"
)


def _591_item_id_from_url(url: str) -> str:
    match = _591_ITEM_URL_RE.search(url or "")
    return match.group(1) if match else ""


def _591_publisher_from_node(node: Any) -> str:
    """只從物件自己的刊登者欄位讀取身分，不掃描導覽或推薦內容。"""
    if not hasattr(node, "select"):
        return ""

    selectors = (
        ".contact-info .base-info-pc .name",
        ".contact-info .base-info .name",
        ".contact-info .name",
        ".role-name span",
    )
    for selector in selectors:
        for candidate in node.select(selector):
            value = clean(candidate.get_text(" ", strip=True), 120)
            if re.match(r"^(?:屋主|仲介|代理人)\s*[:：]?", value):
                return value
    return ""


def _591_is_owner(publisher: str) -> bool:
    return bool(re.match(r"^屋主\s*[:：]?", clean(publisher, 120)))


def _591_is_proxy(publisher: str) -> bool:
    return bool(re.match(r"^代理人\s*[:：]?", clean(publisher, 120)))


def _591_layout_from_node(node: Any) -> str:
    """從單一卡片或詳情主資訊區取得格局，避免推薦物件與「591房」污染。"""
    if not hasattr(node, "select"):
        return ""

    candidates: list[str] = []
    for block in node.select(".item-info-txt"):
        if block.select_one(".house-home"):
            candidates.extend(
                clean(span.get_text(" ", strip=True), 80)
                for span in block.select("span.line")
            )
    candidates.extend(
        clean(span.get_text(" ", strip=True), 80)
        for span in node.select(".pattern span")
    )
    candidates.append(clean(node.get_text(" ", strip=True), 4000))

    pattern = re.compile(
        r"(?<!\d)(\d{1,2}\s*房(?:\s*\d{1,2}\s*廳)?(?:\s*\d{1,2}\s*衛)?)"
    )
    for value in candidates:
        match = pattern.search(value)
        if match:
            return re.sub(r"\s+", "", match.group(1))
    return ""


def _591_has_four_room_layout(layout: str) -> bool:
    match = re.match(r"(\d{1,2})房", layout or "")
    return bool(match and int(match.group(1)) >= 4)


def extract_591_ids(raw: str) -> list[str]:
    soup = BeautifulSoup(raw, "html.parser")
    ids: list[str] = []

    # 目前 591 SSR 列表的穩定邊界是 div.item[data-id]。只在物件卡片內取 ID，
    # 避免把 market.591.com.tw 社區連結或頁尾數字誤當租屋物件。
    cards = soup.select(
        ".item[data-id], .item[data-houseid], "
        ".item[data-house-id], .item[data-post-id]"
    )
    for node in cards:
        for key in ("data-id", "data-houseid", "data-house-id", "data-post-id"):
            value = str(node.get(key, ""))
            if re.fullmatch(r"\d{7,9}", value):
                ids.append(value)

    # 舊版或尚未補上 data-id 的卡片，仍只接受 rent.591.com.tw 的直達網址。
    if not ids:
        for anchor in soup.select(".list-wrapper a[href], main a[href]"):
            href = urllib.parse.urljoin(
                "https://rent.591.com.tw", anchor.get("href", "")
            )
            item_id = _591_item_id_from_url(href)
            if item_id:
                ids.append(item_id)

    # 最後備援只比對明確的 591 租屋網址；不再接受任意 /1234567 路徑。
    if not ids:
        ids.extend(
            re.findall(
                r'https?:\\?/\\?/rent\.591\.com\.tw\\?/'
                r'(?:(?:home/house/detail|home)\\?/)?(\d{7,9})',
                raw,
            )
        )

    unique: list[str] = []
    seen: set[str] = set()
    for item_id in ids:
        if item_id not in seen:
            seen.add(item_id)
            unique.append(item_id)
    return unique[:120]


_591_LIST_CACHE: dict[str, Listing] = {}
_591_BFF_CACHE_IDS: set[str] = set()
_591_REJECTS: dict[str, int] = {}


def reject_591(reason: str) -> None:
    _591_REJECTS[reason] = _591_REJECTS.get(reason, 0) + 1


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

    cards = soup.select(
        ".item[data-id], .item[data-houseid], "
        ".item[data-house-id], .item[data-post-id]"
    )
    for card in cards:
        item_id = ""
        for key in ("data-id", "data-houseid", "data-house-id", "data-post-id"):
            value = str(card.get(key, ""))
            if re.fullmatch(r"\d{7,9}", value):
                item_id = value
                break
        if not item_id:
            continue

        anchor = None
        for candidate in card.select("a[href]"):
            href = urllib.parse.urljoin(
                "https://rent.591.com.tw", candidate.get("href", "")
            )
            if _591_item_id_from_url(href) == item_id:
                anchor = candidate
                break
        if anchor is None:
            continue

        text = clean(card.get_text(" ", strip=True), 16000)
        layout = _591_layout_from_node(card)
        if (
            excluded(text)
            or "整層住家" not in text
            or not _591_has_four_room_layout(layout)
        ):
            continue

        district = district_from_text(text)
        if district not in ALLOWED_DISTRICTS:
            continue

        price_node = card.select_one(".item-info-price")
        price_text = price_node.get_text(" ", strip=True) if price_node else ""
        main_price_match = re.search(
            r"([\d,]{4,})\s*元\s*/\s*月",
            price_text,
        )
        rent = money(main_price_match.group(1)) if main_price_match else 0
        if not rent:
            rent_match = (
                re.search(r"([\d,]{4,})\s*元\s*/?\s*月", text)
                or re.search(
                    r"(?:租金|月租)\s*[:：]?\s*([\d,]{4,})\s*元", text
                )
            )
            rent = money(rent_match.group(1)) if rent_match else 0
        if not rent:
            continue

        size_match = re.search(r"(\d+(?:\.\d+)?\s*坪)", text)
        floor_match = re.search(r"((?:B?\d+(?:~|～|-)\d+F|整棟|\d+F)\s*/\s*\d+F)", text, re.I)
        building_match = re.search(r"(電梯大樓|電梯華廈|華廈|公寓|透天厝|別墅|樓中樓)", text)
        address_match = re.search(
            r"((?:桃園區|中壢區|平鎮區|八德區)\s*[-－]?\s*.*?)"
            r"(?=\s+(?:屋主|仲介|代理人)|"
            r"\s+(?:\d+分鐘|\d+小時|\d+天)(?:內|前)?更新|"
            r"\s+昨日\d+人瀏覽|\s+[\d,]{4,}\s*元\s*/\s*月|$)",
            text,
        )
        publisher = _591_publisher_from_node(card)
        role_node = card.select_one(".role-name")
        role_text = clean(
            role_node.get_text(" ", strip=True) if role_node else "", 300
        )
        updated_match = re.search(
            r"((?:\d+分鐘|\d+小時|\d+天)(?:內|前)?更新)", role_text
        )
        views_match = re.search(r"(昨日\d+人瀏覽|\d+人瀏覽)", role_text)

        title = clean(anchor.get("title") or anchor.get_text(" ", strip=True), 180)
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
            layout=layout,
            size=re.sub(r"\s+", "", size_match.group(1)) if size_match else "",
            equipment="、".join(equipment),
            rent=rent,
            updated=updated_match.group(1) if updated_match else "",
            views=views_match.group(1) if views_match else "",
            publisher=publisher,
            image=image,
            summary=text,
            raw_text=text,
            validated_at=NOW.isoformat(),
        )

        if _591_is_proxy(item.publisher):
            continue

        # Cache 可以先保留沒有圖片的資料；真正發布時仍要求有圖片。
        if not item.title or not item.rent:
            continue

        # 「屋主直租」只依 591 卡片自己的角色欄位；標題、導覽或免仲介費均不算。
        if _591_is_owner(item.publisher):
            item.category_hint = "owner"
        elif "降價" in text:
            item.category_hint = "discount"
        item.fingerprint = fingerprint(item)

        existing = result.get(item_id)
        if existing is None or len(item.summary) > len(existing.summary):
            result[item_id] = item

    return result


def parse_591_bff_cards(payload: Any) -> dict[str, Listing]:
    """將 591 官網清單頁使用的 BFF JSON 轉成已驗證的列表快照。"""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return {}

    result: dict[str, Listing] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue

        item_id = str(raw_item.get("id", ""))
        if not re.fullmatch(r"\d{7,9}", item_id):
            continue

        title = clean(raw_item.get("title", ""), 180)
        house_type = clean(
            raw_item.get("kind_name") or raw_item.get("ding_kind_name") or "",
            80,
        )
        layout = clean(raw_item.get("layoutStr", ""), 80)
        address = clean(raw_item.get("address", ""), 120)
        publisher = clean(raw_item.get("role_name", ""), 120)
        tags_value = raw_item.get("tags")
        tags = (
            [clean(value, 80) for value in tags_value if clean(value, 80)]
            if isinstance(tags_value, list)
            else []
        )
        text = clean(
            " ".join(
                [
                    title,
                    house_type,
                    layout,
                    address,
                    publisher,
                    clean(raw_item.get("community_name", ""), 120),
                    *tags,
                ]
            ),
            16000,
        )

        if (
            house_type != "整層住家"
            or not _591_has_four_room_layout(layout)
            or excluded(text)
            or _591_is_proxy(publisher)
        ):
            continue

        district = district_from_text(address)
        rent = money(str(raw_item.get("price", "")))
        image = clean(raw_item.get("cover", ""), 1000)
        photos = raw_item.get("photoList")
        if not image and isinstance(photos, list) and photos:
            image = clean(photos[0], 1000)
        if not title or not rent or not image or district not in ALLOWED_DISTRICTS:
            continue

        diff_price = money(str(raw_item.get("diff_price", "")))
        old_rent = rent + diff_price if diff_price > 0 else 0
        extra_fee = money(str(raw_item.get("extra_fee", "")))
        browse_count = money(str(raw_item.get("browse_count", "")))
        filter_tags = []
        if int(raw_item.get("preferred", 0) or 0) == 1:
            filter_tags.append("featured")
        item = Listing(
            source="591",
            source_id=item_id,
            url=f"https://rent.591.com.tw/{item_id}",
            title=title,
            district=district,
            address=address,
            house_type=house_type,
            floor=clean(raw_item.get("floor_name", ""), 80),
            layout=layout,
            size=clean(raw_item.get("area_name", ""), 80),
            rent=rent,
            old_rent=old_rent,
            total_cost=rent + extra_fee,
            updated=clean(raw_item.get("refresh_time", ""), 80),
            views=f"{browse_count}人瀏覽" if browse_count else "",
            publisher=publisher,
            image=image,
            summary=text,
            raw_text=text,
            validated_at=NOW.isoformat(),
            filter_tags=filter_tags,
        )
        if _591_is_owner(publisher):
            item.category_hint = "owner"
        elif old_rent > rent:
            item.category_hint = "discount"
        item.fingerprint = fingerprint(item)
        result[item_id] = item

    return result


def fetch_591_bff_cards(
    query: dict[str, Any],
) -> tuple[int | None, int, dict[str, Listing]]:
    """讀取 591 官網自己的清單 BFF；分頁使用 firstRow=0,30,60...。"""
    page_no = max(1, int(query.get("page", 1)))
    first_row = (page_no - 1) * 30
    params: dict[str, str] = {
        "kind": str(query.get("kind", 1)),
        "multiRoom": str(query.get("layout", 4)),
        "regionid": str(query.get("region", 6)),
        "sectionid": str(query.get("section", "")),
        "firstRow": str(first_row),
        "order": "posttime",
        "orderType": "desc",
    }
    if query.get("shType"):
        params["shType"] = str(query["shType"])

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://rent.591.com.tw",
        "Referer": "https://rent.591.com.tw/",
        "device": "pc",
    }
    for attempt in range(3):
        try:
            response = session.get(
                _591_BFF_LIST_URL,
                params=params,
                headers=headers,
                timeout=30,
            )
            if response.status_code in {429, 500, 502, 503, 504}:
                raise requests.RequestException(
                    f"temporary status {response.status_code}"
                )
            if response.status_code != 200:
                return response.status_code, first_row, {}
            payload = response.json()
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("data"), dict)
                or not isinstance(payload["data"].get("items"), list)
            ):
                raise ValueError("unexpected 591 BFF response")
            return response.status_code, first_row, parse_591_bff_cards(payload)
        except (requests.RequestException, ValueError) as exc:
            if attempt + 1 >= 3:
                print(
                    f"[WARN] 591 BFF failed: firstRow={first_row}: {exc}",
                    file=sys.stderr,
                )
                return None, first_row, {}
            time.sleep(1.4 * (2**attempt))
    return None, first_row, {}


def crawl_591_links(source_stats: dict[str, Any]) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    blocked_streak = 0
    stop_after_block = False

    def record_status(channel: str, status: int | None) -> None:
        statuses = source_stats.setdefault("http_statuses", {}).setdefault(channel, {})
        key = str(status) if status is not None else "network_error"
        statuses[key] = statuses.get(key, 0) + 1

    # 先跑 591 官方的屋主篩選，再跑一般列表。即使一般列表後段被限流，
    # 屋主物件仍會先進入驗證；是否為屋主最後仍由詳情頁聯絡人角色確認。
    search_modes: tuple[tuple[str, dict[str, str]], ...] = (
        ("owner", {"shType": "host"}),
        ("general", {}),
    )

    for mode, extra_query in search_modes:
        for district, section in DISTRICTS_591.items():
            empty_pages = 0
            for page_no in range(1, 101):
                query: dict[str, Any] = {
                    "kind": 1,
                    "layout": 4,
                    "region": 6,
                    "section": section,
                    **extra_query,
                    "page": page_no,
                    "sort": "posttime_desc",
                }
                url = "https://rent.591.com.tw/list?" + urllib.parse.urlencode(query)

                # 優先讀 591 清單頁前端本身使用的官方 BFF。它提供明確角色、
                # 主租金與官方降價差額，避免 HTML 額外費用覆蓋主租金。
                bff_status, first_row, bff_cards = fetch_591_bff_cards(query)
                record_status("bff", bff_status)
                response: requests.Response | None = None
                raw = ""
                ids: list[str] = []
                cards: dict[str, Listing] = {}
                browser_result = "not_used"
                if bff_cards:
                    cards = bff_cards
                    ids = list(cards)
                    _591_BFF_CACHE_IDS.update(cards)
                elif bff_status != 200:
                    # BFF 被擋或暫時失效時，才讀 SSR HTML。
                    response, raw = fetch_html(url)
                    record_status(
                        "html",
                        response.status_code if response is not None else None,
                    )
                    ids = extract_591_ids(raw)
                    cards = parse_591_list_cards(raw)

                if (not ids or not cards) and bff_status != 200:
                    source_stats["browser_attempts"] = (
                        int(source_stats.get("browser_attempts", 0)) + 1
                    )
                    rendered = browser.html(url)
                    if rendered:
                        rendered_cards = parse_591_list_cards(rendered)
                        if rendered_cards:
                            browser_result = "valid"
                            source_stats["browser_valid_pages"] = (
                                int(source_stats.get("browser_valid_pages", 0)) + 1
                            )
                            raw = rendered
                            cards = rendered_cards
                            ids = extract_591_ids(rendered)
                        else:
                            browser_result = (
                                "blocked" if looks_blocked(rendered) else "no_cards"
                            )
                    else:
                        browser_result = "empty"

                html_status = response.status_code if response is not None else None
                if (
                    not cards
                    and bff_status in {401, 403, 429}
                    and html_status in {401, 403, 429}
                    and browser_result in {"blocked", "empty", "no_cards"}
                ):
                    blocked_streak += 1
                else:
                    blocked_streak = 0

                for item_id, item in cards.items():
                    existing = _591_LIST_CACHE.get(item_id)
                    if existing is None or len(item.summary) > len(existing.summary):
                        _591_LIST_CACHE[item_id] = item

                # 只把已成功解析為目標卡片的 ID 放進詳情驗證，排除廣告、
                # 社區 market 連結與非 4 房卡片。
                new_ids = [item_id for item_id in cards if item_id not in seen]
                status = (
                    response.status_code
                    if response is not None
                    else ("bff" if bff_cards else "network")
                )
                print(
                    f"[591] mode={mode} {district} page={page_no} "
                    f"status={status} bff={bff_status} firstRow={first_row} "
                    f"browser={browser_result} "
                    f"ids={len(ids)} cards={len(cards)} "
                    f"cache={len(_591_LIST_CACHE)} new={len(new_ids)}"
                )

                # 官方 BFF、SSR 與 Chromium 連續兩次都明確被擋時，繼續打
                # 其餘行政區只會加重共享 Runner IP 的限流。
                if blocked_streak >= 2:
                    stop_after_block = True
                    source_stats["blocked_after_queries"] = 2
                    break

                if not new_ids:
                    empty_pages += 1
                    if empty_pages >= 2:
                        break
                else:
                    empty_pages = 0

                for item_id in new_ids:
                    seen.add(item_id)
                    links.append(f"https://rent.591.com.tw/{item_id}")

                time.sleep(0.75)
            if stop_after_block:
                break
        if stop_after_block:
            break

    source_stats["candidate_links"] = len(links)
    source_stats["list_cache"] = len(_591_LIST_CACHE)
    source_stats["rejects"] = dict(sorted(_591_REJECTS.items()))
    if not links:
        if stop_after_block:
            source_stats["errors"].append(
                "591官方BFF與列表HTML連續回應403/429；Chromium也只取得受阻頁。"
                "這是GitHub Runner出口IP被591限制，不是Chromium未安裝或物件ID解析失敗。"
            )
        else:
            source_stats["errors"].append(
                "591列表頁未取得物件編號；請查看http_statuses、browser_attempts"
                "與Actions記錄判斷取得層失敗原因。"
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


def _591_detail_image(soup: BeautifulSoup) -> str:
    """591 詳情頁圖片：OG → JSON-LD → 內容圖片。"""
    image = meta(soup, "og:image", "twitter:image")
    if image:
        return image

    _, json_image = json_ld_title_image(soup)
    if json_image:
        return json_image

    return _591_image_from_node(soup)


def _591_validated_cache(cached: Listing | None) -> Listing | None:
    """驗證同一輪官方列表快照；只在詳情端被擋時使用。"""
    if not cached:
        reject_591("blocked_no_cache")
        return None
    if excluded(cached.raw_text):
        reject_591("hard_excluded")
        return None
    if _591_is_proxy(cached.publisher):
        reject_591("proxy_or_excluded")
        return None
    if cached.house_type != "整層住家":
        reject_591("not_whole_home")
        return None
    if not _591_has_four_room_layout(cached.layout):
        reject_591("not_4_rooms")
        return None
    if not cached.title or not cached.rent or not cached.image or not allowed_district(cached):
        reject_591("missing_required_fields")
        return None
    return cached


def parse_591_detail(url: str) -> Listing | None:
    item_id_match = re.search(r"(\d{7,9})", urllib.parse.urlparse(url).path)
    item_id = item_id_match.group(1) if item_id_match else hashlib.md5(url.encode()).hexdigest()[:16]
    cached = _591_LIST_CACHE.get(item_id)

    # BFF 清單本身已提供物件ID、明確刊登角色、主租金、官方降價差額、
    # 格局、地址與圖片。直接使用嚴格驗證後的同輪快照，避免成功抓完清單後
    # 再對 261 筆詳情頁各送一次請求，把共享 Runner 出口 IP 推進限流。
    if cached and item_id in _591_BFF_CACHE_IDS:
        return _591_validated_cache(cached)
    else:
        # 非 BFF 列表來源仍保留 Chromium 備援。
        response, raw = fetch_html(url, browser_fallback=True)

    if response is not None and response.status_code in {404, 410}:
        reject_591("dead_404_410")
        return None

    if looks_blocked(raw):
        # 被擋時使用同一輪列表快照；但仍需符合必要欄位與排除條件。
        return _591_validated_cache(cached)

    if is_dead_page(response, raw, "591.com.tw"):
        reject_591("dead_marker")
        return None

    soup = BeautifulSoup(raw, "html.parser")
    info_board = soup.select_one(".info-board")
    description = meta(soup, "og:description", "description")
    scoped_nodes = soup.select(
        ".info-board, .house-condition-content, .house-condition, "
        ".contact-info, .service-list"
    )
    text = clean(
        " ".join(
            [node.get_text(" ", strip=True) for node in scoped_nodes]
            + [description]
        ),
        80000,
    )

    # 後續判斷只使用物件本身的主資訊、條件與聯絡人區塊；不掃描導覽、
    # 頁尾或推薦物件，避免「591房」及其他卡片的屋主/格局文字污染。
    if excluded(text):
        reject_591("hard_excluded")
        return None

    json_title, _ = json_ld_title_image(soup)
    heading = info_board.select_one("h1") if info_board else None
    title = (
        clean(heading.get_text(" ", strip=True), 180)
        if heading
        else meta(soup, "og:title", "twitter:title") or json_title
    )

    image = _591_detail_image(soup)
    layout = _591_layout_from_node(info_board) if info_board else ""
    size_match = re.search(r"(\d+(?:\.\d+)?\s*坪)", text)
    floor_match = re.search(r"((?:B?\d+(?:~|～|-)\d+F|整棟|\d+F)\s*/\s*\d+F)", text, re.I)
    building_match = re.search(r"(電梯大樓|電梯華廈|華廈|公寓|透天厝|別墅|樓中樓)", text)
    address_match = re.search(
        r"(?:地址\s*[:：]?\s*)?(桃園市?\s*)?((?:桃園區|中壢區|平鎮區|八德區)[^。|]{1,80})",
        text,
    )
    publisher = _591_publisher_from_node(soup)
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
        district=district_from_text(f"{title} {text}"),
        address=clean(address_match.group(2), 100) if address_match else "",
        house_type="整層住家" if "整層住家" in text else "",
        building_type=building_match.group(1) if building_match else "",
        floor=floor_match.group(1).replace(" ", "") if floor_match else "",
        layout=layout,
        size=re.sub(r"\s+", "", size_match.group(1)) if size_match else "",
        equipment="、".join(equipment),
        rent=_591_detail_rent(soup, text),
        min_lease=lease_match.group(1) if lease_match else "",
        updated=updated_match.group(1) if updated_match else "",
        views=views_match.group(1) if views_match else "",
        publisher=publisher,
        image=image,
        summary=description,
        raw_text=text,
        validated_at=NOW.isoformat(),
    )

    # 詳情頁欄位不足時，用當次列表快照補值，但不能覆蓋詳情頁已取得的值。
    if cached:
        for attr in (
            "title", "district", "address", "house_type", "building_type", "floor", "layout",
            "size", "equipment", "min_lease", "updated", "views", "publisher",
            "image", "summary",
        ):
            if not getattr(item, attr):
                setattr(item, attr, getattr(cached, attr))
        # 列表的主租金欄位比詳情頁自由文字可靠；詳情可能同時含管理費、
        # 車位費或其他額外費用，不能讓那些小額數字覆蓋主租金。
        if cached.rent:
            item.rent = cached.rent
        if cached.old_rent > cached.rent:
            item.old_rent = cached.old_rent

    if _591_is_proxy(item.publisher):
        reject_591("proxy_or_excluded")
        return None
    if item.house_type != "整層住家":
        reject_591("not_whole_home")
        return None
    if not _591_has_four_room_layout(item.layout):
        reject_591("not_4_rooms")
        return None

    missing = []
    if not item.title:
        missing.append("title")
    if not item.rent:
        missing.append("rent")
    if not item.image:
        missing.append("image")
    if not allowed_district(item):
        missing.append("district")
    if missing:
        reject_591("missing_required_fields")
        print(f"[591 reject] id={item_id} missing={','.join(missing)}")
        return None

    # 屋主分類只接受詳情頁（或被擋時同輪列表卡片）的明確角色欄位。
    if _591_is_owner(item.publisher):
        item.category_hint = "owner"
    elif cached and cached.category_hint == "discount":
        item.category_hint = cached.category_hint

    item.fingerprint = fingerprint(item)
    return item


def load_591_snapshot(
    path: Path | None = None,
) -> tuple[list[Listing], str, float | None, str]:
    """讀取最近一次成功的 591 真實快照，並拒絕過期或不完整資料。"""
    snapshot_path = path or LAST_SUCCESS_591
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], "", None, ""
    except (OSError, json.JSONDecodeError) as exc:
        return [], "", None, f"591上次成功快照無法讀取：{exc}"

    generated_at = str(payload.get("generated_at", ""))
    try:
        generated_time = datetime.fromisoformat(generated_at)
        if generated_time.tzinfo is None:
            raise ValueError("timezone is required")
        age = max(timedelta(0), NOW - generated_time.astimezone(TZ))
    except (TypeError, ValueError):
        return [], generated_at, None, "591上次成功快照缺少有效時區時間戳。"

    age_hours = age.total_seconds() / 3600
    if age > _591_SNAPSHOT_MAX_AGE:
        return (
            [],
            generated_at,
            age_hours,
            f"591上次成功快照已超過{int(_591_SNAPSHOT_MAX_AGE.total_seconds() / 3600)}小時，"
            "為避免顯示過期房源，本輪不沿用。",
        )

    rows = payload.get("items")
    if not isinstance(rows, list):
        return [], generated_at, age_hours, "591上次成功快照的items不是陣列。"

    field_names = set(Listing.__dataclass_fields__)
    items: list[Listing] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("source") != "591":
            continue
        item_id = str(row.get("source_id", ""))
        if not re.fullmatch(r"\d{7,9}", item_id) or item_id in seen:
            continue
        try:
            values = {key: row[key] for key in field_names if key in row}
            values["rent"] = int(values.get("rent", 0) or 0)
            values["old_rent"] = int(values.get("old_rent", 0) or 0)
            values["total_cost"] = int(values.get("total_cost", 0) or 0)
            item = Listing(**values)
        except (TypeError, ValueError):
            continue

        item.source_id = item_id
        item.url = f"https://rent.591.com.tw/{item_id}"
        if (
            excluded(item.raw_text)
            or _591_is_proxy(item.publisher)
            or item.house_type != "整層住家"
            or not _591_has_four_room_layout(item.layout)
            or not item.title
            or not item.rent
            or not item.image
            or not allowed_district(item)
        ):
            continue
        item.fingerprint = item.fingerprint or fingerprint(item)
        seen.add(item_id)
        items.append(item)

    if not items:
        return [], generated_at, age_hours, "591上次成功快照沒有可安全沿用的有效物件。"
    return items, generated_at, age_hours, ""


def save_591_snapshot(items: list[Listing], path: Path | None = None) -> None:
    """只在本輪成功取得 591 時更新備援快照。"""
    snapshot_path = path or LAST_SUCCESS_591
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "generated_at": NOW.isoformat(),
                "items": [asdict(item) for item in items if item.source == "591"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def use_591_snapshot(
    source_stats: dict[str, Any],
    items: list[Listing],
    generated_at: str,
    age_hours: float,
    reason: str,
) -> list[Listing]:
    """把上次成功資料列入本輪顯示，同時保留新鮮抓取的真實統計。"""
    fresh_candidates = int(source_stats.get("candidate_links", 0) or 0)
    source_stats["fresh_candidate_links"] = fresh_candidates
    source_stats["candidate_links"] = len(items)
    source_stats["validated"] = len(items)
    source_stats["snapshot_items"] = len(items)
    source_stats["snapshot_generated_at"] = generated_at
    source_stats["snapshot_age_hours"] = round(age_hours, 2)
    source_stats["fallback"] = reason

    if reason == "refresh_cooldown":
        source_stats["notices"].append(
            f"距上次成功抓取僅{age_hours:.1f}小時；為避免591限流，本輪沿用"
            f"{generated_at}的{len(items)}筆真實快照，未重新請求591。"
        )
    else:
        source_stats["errors"].append(
            f"本輪591新鮮候選為{fresh_candidates}筆，沿用{generated_at}的"
            f"{len(items)}筆上次成功真實快照；這些物件本輪未重新驗證，"
            "請點開591確認仍在刊登。"
        )
    return items


def collect_591_listings(source_stats: dict[str, Any]) -> list[Listing]:
    """取得 591；短時間重跑或來源受阻時，誠實標示並沿用近期成功快照。"""
    snapshot, snapshot_at, snapshot_age, snapshot_error = load_591_snapshot()

    if (
        snapshot
        and snapshot_age is not None
        and timedelta(hours=snapshot_age) < _591_REFRESH_COOLDOWN
    ):
        return use_591_snapshot(
            source_stats,
            snapshot,
            snapshot_at,
            snapshot_age,
            "refresh_cooldown",
        )

    links = crawl_591_links(source_stats)
    fresh: list[Listing] = []
    for index, url in enumerate(links, 1):
        print(f"[591 detail] {index}/{len(links)} {url}")
        item = parse_591_detail(url)
        if item:
            fresh.append(item)

    source_stats["validated"] = len(fresh)
    source_stats["list_cache"] = len(_591_LIST_CACHE)
    source_stats["rejects"] = dict(sorted(_591_REJECTS.items()))
    if links and not fresh:
        reject_summary = ", ".join(
            f"{key}={value}" for key, value in sorted(_591_REJECTS.items())
        ) or "無拒絕原因紀錄"
        source_stats["errors"].append(
            "591有取得候選物件，但0筆通過驗證。"
            f"列表快照={len(_591_LIST_CACHE)}；排除原因：{reject_summary}。"
            "請依rejects判斷是缺欄位、4房解析或排除條件造成。"
        )

    # 若前段已有少量成功資料、後續卻因 403/429 中止，這不是完整刷新。
    # 不得用部分結果覆蓋較完整的真實快照；新鮮資料優先，其餘沿用近期快照。
    if fresh and source_stats.get("blocked_after_queries"):
        source_stats["fresh_validated"] = len(fresh)
        source_stats["partial_refresh"] = True
        if snapshot and snapshot_age is not None:
            fresh_ids = {item.source_id for item in fresh}
            merged = fresh + [
                item for item in snapshot if item.source_id not in fresh_ids
            ]
            return use_591_snapshot(
                source_stats,
                merged,
                snapshot_at,
                snapshot_age,
                "partial_source_blocked",
            )
        source_stats["errors"].append(
            f"591本輪只取得{len(fresh)}筆後即遭阻擋，沒有可沿用的近期完整快照；"
            "本輪不會把部分結果保存成成功快照。"
        )
        return fresh

    if fresh:
        try:
            save_591_snapshot(fresh)
            source_stats["snapshot_updated_at"] = NOW.isoformat()
        except OSError as exc:
            source_stats["errors"].append(f"591成功資料無法保存為備援快照：{exc}")
        return fresh

    if snapshot and snapshot_age is not None:
        return use_591_snapshot(
            source_stats,
            snapshot,
            snapshot_at,
            snapshot_age,
            "source_blocked",
        )

    if snapshot_error:
        source_stats["errors"].append(snapshot_error)
    return []


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
    categories: dict[str, set[str]] = {
        "general": set(),
        "owner": set(),
        "friendly": set(),
        "discount": set(),
    }

    query_sets: list[tuple[str, dict[str, str]]] = []
    for room in ("4", "5"):
        query_sets.append(("general", {"zipcode": zipcodes, "room": room}))
        # 樂屋網現行四個頁籤使用 tab 參數；usecode=7 並不是「屋主」頁籤。
        query_sets.append(("owner", {"zipcode": zipcodes, "room": room, "tab": "rkp"}))
        query_sets.append(("friendly", {"zipcode": zipcodes, "room": room, "tab": "frd"}))
        query_sets.append(("discount", {"zipcode": zipcodes, "room": room, "tab": "low"}))

    for category, params in query_sets:
        no_new = 0
        for page_url in rakuya_result_urls(params):
            response, raw = fetch_html(
                page_url,
                browser_fallback=True,
                browser_wait_ms=6000,
            )
            if response is None and not raw:
                source_stats["errors"].append(
                    f"樂屋網搜尋頁無法讀取：{page_url}"
                )
                source_stats["candidate_links"] = len(
                    set().union(*categories.values())
                )
                return categories
            if looks_blocked(raw):
                source_stats["errors"].append(
                    "樂屋網搜尋頁遭存取驗證阻擋，requests與Chromium均未取得有效內容。"
                )
                source_stats["candidate_links"] = len(
                    set().union(*categories.values())
                )
                return categories

            base_url = response.url if response is not None else page_url
            links = extract_rakuya_links(raw, base_url)
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
    response, raw = fetch_html(
        url,
        browser_fallback=True,
        browser_wait_ms=4500,
    )
    if looks_blocked(raw):
        return None
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
        r"桃園市?(桃園區|中壢區|平鎮區|八德區)([^\-｜|]{1,45})[\-｜|]",
        title,
    )
    if title_address:
        address = f"{title_address.group(1)}{clean(title_address.group(2), 50)}"
    else:
        address_match = re.search(
            r"(桃園區|中壢區|平鎮區|八德區)\s*([^。|]{1,50}(?:路|街|巷|弄))",
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
        filter_tags=sorted(
            hint for hint in hints if hint in {"owner", "friendly", "discount"}
        ),
    )

    if not item.title or not item.rent or not item.image or not allowed_district(item):
        return None

    if "owner" in hints:
        item.category_hint = "owner"
    elif "friendly" in hints:
        item.category_hint = "friendly"

    if "discount" in hints or item.old_rent > item.rent:
        item.category_hint = "discount"
        item.filter_tags = sorted(set(item.filter_tags) | {"discount"})

    item.fingerprint = fingerprint(item)
    return item


# ---------------------------------------------------------------------------
# Facebook 安全匯入
# ---------------------------------------------------------------------------


def normalize_facebook_post_url(url: str) -> str:
    """只接受設定清單內社團的單篇貼文網址，並移除追蹤參數。"""
    try:
        parsed = urllib.parse.urlparse(str(url).strip())
    except ValueError:
        return ""

    if parsed.scheme not in {"http", "https"}:
        return ""
    if (parsed.hostname or "").lower() not in {
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
    }:
        return ""

    parts = [part for part in parsed.path.split("/") if part]
    if (
        len(parts) < 4
        or parts[0] != "groups"
        or parts[1] not in FB_GROUP_IDS
        or parts[2] not in {"posts", "permalink"}
        or not re.fullmatch(r"[A-Za-z0-9._-]+", parts[3])
    ):
        return ""

    return f"https://www.facebook.com/groups/{parts[1]}/{parts[2]}/{parts[3]}/"


def facebook_post_key(url: str) -> str:
    """回傳社團與貼文 ID 組成的穩定鍵，posts/permalink 視為同一筆。"""
    normalized = normalize_facebook_post_url(url)
    if not normalized:
        return ""
    parts = [part for part in urllib.parse.urlparse(normalized).path.split("/") if part]
    return f"{parts[1]}:{parts[3]}"


def is_public_http_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url).strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_direct_public_image_url(url: str) -> bool:
    """拒絕 Facebook 照片頁；image 欄位必須能直接作為 img src 使用。"""
    if not is_public_http_url(url):
        return False
    parsed = urllib.parse.urlparse(str(url).strip())
    hostname = (parsed.hostname or "").lower()
    return hostname not in {"facebook.com", "www.facebook.com", "m.facebook.com"}


def clean_multiline(value: Any, limit: int = 12000) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)[:limit]


def decode_json_fragment(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except (json.JSONDecodeError, TypeError):
        return html.unescape(value or "")


def facebook_labeled_money(text: str, *labels: str) -> int:
    labels_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{labels_pattern})\s*(?:費)?\s*[:：]?\s*"
        r"(?:NT\$?|新台幣)?\s*([\d,]{3,})",
        text or "",
        re.I,
    )
    value = money(match.group(1)) if match else 0
    return value if 1_000 <= value <= 1_000_000 else 0


def facebook_layout_from_text(text: str) -> str:
    match = re.search(
        r"(?<!\d)(\d{1,2}\s*房(?:\s*\d{1,2}\s*廳)?"
        r"(?:\s*\d{1,2}\s*衛)?)",
        text or "",
    )
    return re.sub(r"\s+", "", match.group(1)) if match else ""


def facebook_size_from_text(text: str) -> str:
    match = re.search(r"(?:約\s*)?(\d+(?:\.\d+)?\s*坪)", text or "")
    return re.sub(r"\s+", "", match.group(1)) if match else ""


def facebook_address_from_text(text: str) -> str:
    match = re.search(
        r"(?:地點|地址|位置)\s*[:：]\s*([^\n\r]{2,120})",
        text or "",
        re.I,
    )
    if match:
        return clean(match.group(1), 100)
    district = district_from_text(text)
    road = re.search(
        r"((?:桃園區|中壢區|平鎮區|八德區)[^\n\r，。]{0,50}"
        r"(?:路|街|巷|社區))",
        text or "",
    )
    return clean(road.group(1), 100) if road else district


def facebook_equipment_from_text(text: str) -> str:
    markers = (
        "家具家電全配",
        "附傢俱",
        "附設備",
        "電梯",
        "可開伙",
        "可租補",
        "租屋補助",
        "可入戶籍",
        "可養寵物",
        "寵物友善",
        "停車位",
        "車位",
    )
    values: list[str] = []
    for marker in markers:
        if marker in (text or "") and marker not in values:
            values.append(marker)
    return "、".join(values)


def facebook_title_from_text(text: str) -> str:
    for line in clean_multiline(text, 3000).splitlines():
        value = clean(line, 180).strip("｜|【】[]-—–・ ")
        if value and not re.fullmatch(r"[\W_]+", value):
            return value
    return ""


def fetch_public_facebook_metadata(url: str) -> dict[str, Any]:
    """以全新匿名請求讀取 Facebook 對搜尋爬蟲公開的單篇貼文中繼資料。"""
    normalized = normalize_facebook_post_url(url)
    key = facebook_post_key(normalized)
    if not normalized or not key:
        return {}

    try:
        response = requests.get(
            normalized,
            headers={
                "User-Agent": FB_PUBLIC_CRAWLER_UA,
                "Accept-Language": "zh-TW,zh;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=35,
            allow_redirects=True,
        )
    except requests.RequestException:
        return {}
    if response.status_code != 200 or not (1_000 < len(response.text) <= 3_000_000):
        return {}

    raw = response.text
    soup = BeautifulSoup(raw, "html.parser")
    canonical = meta(soup, "og:url")
    if facebook_post_key(canonical) != key:
        return {}

    description_node = soup.find("meta", attrs={"property": "og:description"})
    description = clean_multiline(
        description_node.get("content", "") if description_node else "",
        16000,
    )
    image = meta(soup, "og:image")

    post_id = key.split(":", 1)[1]
    post_marker = f'"post_id":"{post_id}"'
    post_index = raw.find(post_marker)
    window = raw[max(0, post_index - 7_000) : post_index + 800] if post_index >= 0 else ""

    actor = ""
    actor_matches = re.findall(
        r'"actors"\s*:\s*\[\{[\s\S]{0,1200}?"name"\s*:\s*"((?:\\.|[^"])*)"',
        window,
    )
    if actor_matches:
        actor = clean(decode_json_fragment(actor_matches[-1]), 80)

    creation_time = 0
    creation_matches = re.findall(r'"creation_time"\s*:\s*(\d{9,12})', window)
    if creation_matches:
        creation_time = int(creation_matches[-1])

    seo_title = ""
    title_matches = re.findall(
        r'"seo_title"\s*:\s*"((?:\\.|[^"])*)"',
        window,
    )
    if title_matches:
        seo_title = clean(decode_json_fragment(title_matches[-1]), 180)

    return {
        "url": normalized,
        "canonical_url": canonical,
        "post_text": description,
        "image_origin": image,
        "publisher": actor,
        "creation_time": creation_time,
        "title": seo_title,
        "publicly_readable": bool(description and image),
    }


def resolve_public_image_url(url: str) -> str:
    """直接圖片原樣保留；Facebook 照片頁則匿名解析 og:image。"""
    value = str(url or "").strip()
    if is_direct_public_image_url(value):
        return value
    if not is_public_http_url(value):
        return ""
    hostname = (urllib.parse.urlparse(value).hostname or "").lower()
    if hostname not in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
        return ""
    try:
        response = requests.get(
            value,
            headers={
                "User-Agent": FB_PUBLIC_CRAWLER_UA,
                "Accept-Language": "zh-TW,zh;q=0.9",
            },
            timeout=30,
            allow_redirects=True,
        )
    except requests.RequestException:
        return ""
    if response.status_code != 200 or len(response.text) > 3_000_000:
        return ""
    return meta(BeautifulSoup(response.text, "html.parser"), "og:image")


def is_safe_facebook_image_download(url: str) -> bool:
    """公開投稿只下載 Facebook CDN 或本站圖片，避免任意網址伺服器端請求。"""
    if not is_direct_public_image_url(url):
        return False
    parsed = urllib.parse.urlparse(str(url).strip())
    if parsed.scheme != "https":
        return False
    hostname = (parsed.hostname or "").lower()
    return (
        hostname == "flyspacesky.github.io"
        or hostname == "lookaside.fbsbx.com"
        or hostname.endswith(".fbsbx.com")
        or hostname.endswith(".fbcdn.net")
    )


def archive_facebook_image(
    post_url: str,
    image_url: str,
    source_stats: dict[str, Any],
) -> str:
    """把匿名可讀的真實照片保存到 Pages，避免 Facebook CDN 短效網址失效。"""
    key = facebook_post_key(post_url)
    if not key:
        return ""
    post_id = key.split(":", 1)[1]
    for extension in ("jpg", "png", "webp", "gif"):
        existing = FB_ASSET_DIR / f"{post_id}.{extension}"
        if existing.exists() and existing.stat().st_size > 500:
            return f"{FB_ASSET_PUBLIC_BASE}/{existing.name}"

    resolved = resolve_public_image_url(image_url)
    if not resolved or not is_safe_facebook_image_download(resolved):
        return ""
    try:
        response = requests.get(
            resolved,
            headers={
                "User-Agent": FB_PUBLIC_CRAWLER_UA,
                "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*",
            },
            timeout=35,
            allow_redirects=True,
        )
    except requests.RequestException:
        return ""
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
    extension_by_type = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }
    extension = extension_by_type.get(content_type, "")
    content = response.content
    if response.status_code != 200 or not extension or not (500 < len(content) <= 15_000_000):
        return ""

    FB_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    target = FB_ASSET_DIR / f"{post_id}.{extension}"
    target.write_bytes(content)
    source_stats["images_archived"] = source_stats.get("images_archived", 0) + 1
    return f"{FB_ASSET_PUBLIC_BASE}/{target.name}"


def enrich_facebook_row(
    row: dict[str, Any],
    source_stats: dict[str, Any],
) -> dict[str, Any]:
    """用公開貼文補齊投稿缺少欄位；無法匿名驗證時不臆測資料。"""
    enriched = dict(row)
    url = normalize_facebook_post_url(str(enriched.get("url", "")))
    if not url:
        return enriched
    enriched["url"] = url

    needs_metadata = bool(enriched.get("_submission_source")) or any(
        not clean(enriched.get(field, ""))
        for field in ("post_text", "image", "publisher", "updated")
    )
    metadata = fetch_public_facebook_metadata(url) if needs_metadata else {}
    if metadata:
        source_stats["public_metadata_enriched"] = (
            source_stats.get("public_metadata_enriched", 0) + 1
        )

    text = clean_multiline(
        enriched.get("post_text") or metadata.get("post_text") or "",
        16000,
    )
    enriched["post_text"] = text
    enriched["title"] = clean(
        enriched.get("title")
        or metadata.get("title")
        or facebook_title_from_text(text),
        180,
    )
    enriched["district"] = clean(
        enriched.get("district") or district_from_text(text),
        20,
    )
    enriched["address"] = clean(
        enriched.get("address") or facebook_address_from_text(text),
        100,
    )
    enriched["house_type"] = clean(
        enriched.get("house_type") or ("整層住家" if has_four_rooms(text) else ""),
        30,
    )
    enriched["building_type"] = clean(
        enriched.get("building_type") or ("電梯大樓" if "電梯" in text else ""),
        30,
    )
    enriched["layout"] = clean(
        enriched.get("layout") or facebook_layout_from_text(text),
        30,
    )
    enriched["size"] = clean(
        enriched.get("size") or facebook_size_from_text(text),
        30,
    )
    enriched["equipment"] = clean(
        enriched.get("equipment") or facebook_equipment_from_text(text),
        220,
    )

    rent = money(str(enriched.get("rent", ""))) or facebook_labeled_money(
        text,
        "租金",
        "月租",
        "房租",
    )
    management = facebook_labeled_money(text, "管理費")
    parking = facebook_labeled_money(text, "停車位", "車位")
    enriched["rent"] = rent
    enriched["total_cost"] = (
        money(str(enriched.get("total_cost", "")))
        or (rent + management + parking if rent else 0)
    )
    enriched["publisher"] = clean(
        enriched.get("publisher") or metadata.get("publisher"),
        80,
    )

    creation_time = int(metadata.get("creation_time") or 0)
    if creation_time and not clean(enriched.get("updated", "")):
        published = datetime.fromtimestamp(creation_time, TZ)
        enriched["published_at"] = published.isoformat()
        enriched["updated"] = published.strftime("%Y/%m/%d %H:%M刊登")

    image_origin = (
        str(enriched.get("image_origin", "")).strip()
        or str(enriched.get("image", "")).strip()
        or str(metadata.get("image_origin", "")).strip()
    )
    should_archive = bool(enriched.get("_submission_source")) or bool(metadata)
    archived = (
        archive_facebook_image(url, image_origin, source_stats)
        if should_archive
        else ""
    )
    if archived:
        enriched["image"] = archived
        enriched["image_origin"] = image_origin
    elif not is_direct_public_image_url(str(enriched.get("image", "")).strip()):
        enriched["image"] = ""

    if not clean(enriched.get("summary", "")):
        enriched["summary"] = clean(text, 500)
    return enriched


def parse_facebook_issue_body(issue: dict[str, Any]) -> dict[str, Any] | None:
    title = clean(issue.get("title", ""), 200)
    if not title.startswith(FB_ISSUE_TITLE_PREFIX):
        return None
    body = str(issue.get("body", ""))[:30_000]
    fields: dict[str, str] = {}
    for match in re.finditer(
        r"^###\s+(.+?)\s*$\r?\n([\s\S]*?)(?=^###\s+|\Z)",
        body,
        re.M,
    ):
        label = clean(match.group(1), 120)
        value = clean_multiline(match.group(2), 16000)
        if value not in {"", "_No response_", "No response"}:
            fields[label] = value

    def value_containing(*needles: str) -> str:
        for label, value in fields.items():
            if any(needle in label for needle in needles):
                return value
        return ""

    url = value_containing("永久貼文網址", "Facebook貼文網址")
    if not url:
        return None
    number = int(issue.get("number") or 0)
    return {
        "url": url,
        "post_text": value_containing("完整貼文文字", "貼文內容"),
        "image": value_containing("照片網址", "圖片網址"),
        "_submission_source": f"GitHub issue #{number}" if number else "GitHub issue",
    }


def load_github_facebook_issue_rows(source_stats: dict[str, Any]) -> list[dict[str, Any]]:
    repository = os.environ.get(GITHUB_REPOSITORY_ENV, "").strip()
    token = os.environ.get(GITHUB_TOKEN_ENV, "").strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not token
    ):
        source_stats["issue_source_enabled"] = False
        return []
    source_stats["issue_source_enabled"] = True

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "taoyuan-rental-digest",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    headers["Authorization"] = f"Bearer {token}"
    url = (
        f"https://api.github.com/repos/{repository}/issues"
        "?state=open&sort=updated&direction=desc&per_page=100"
    )
    try:
        response = requests.get(url, headers=headers, timeout=30)
        payload = response.json() if response.status_code == 200 else []
    except (requests.RequestException, ValueError):
        payload = []
    if not isinstance(payload, list):
        source_stats["notices"].append("GitHub FB公開投稿目前無法讀取；本輪仍使用其他FB來源。")
        return []

    rows: list[dict[str, Any]] = []
    for issue in payload:
        if not isinstance(issue, dict) or issue.get("pull_request"):
            continue
        row = parse_facebook_issue_body(issue)
        if row:
            rows.append(row)
    source_stats["issue_submissions_seen"] = len(rows)
    return rows


def facebook_row_reject_reasons(row: dict[str, Any]) -> list[str]:
    """回傳一筆 FB JSON 的所有可操作拒絕原因。"""
    text = clean(" ".join(str(row.get(k, "")) for k in row), 8000)
    reasons: list[str] = []

    if not normalize_facebook_post_url(str(row.get("url", ""))):
        reasons.append("invalid_or_unlisted_group_url")
    if not has_four_rooms(text):
        reasons.append("not_four_rooms")
    if excluded(text):
        reasons.append("excluded_management_or_broker")
    if "代理人" in text:
        reasons.append("excluded_proxy")

    district = clean(row.get("district") or district_from_text(text), 20)
    if district not in ALLOWED_DISTRICTS:
        reasons.append("invalid_district")
    if not money(str(row.get("rent", ""))):
        reasons.append("missing_rent")
    if not clean(row.get("title") or text.split("。")[0], 180):
        reasons.append("missing_title")
    if not is_direct_public_image_url(str(row.get("image", "")).strip()):
        reasons.append("image_not_direct_public")

    return list(dict.fromkeys(reasons))


def parse_social_row(row: dict[str, Any], source: str) -> Listing | None:
    if facebook_row_reject_reasons(row):
        return None

    text = clean(" ".join(str(row.get(k, "")) for k in row), 8000)
    district = clean(row.get("district") or district_from_text(text), 20)

    url = normalize_facebook_post_url(str(row.get("url", "")))
    image = str(row.get("image", "")).strip()
    title = clean(row.get("title") or text.split("。")[0], 180)
    rent = money(str(row.get("rent", "")))

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
        size=clean(row.get("size", ""), 30),
        equipment=clean(row.get("equipment", ""), 220),
        rent=rent,
        old_rent=money(str(row.get("old_rent", ""))),
        total_cost=money(str(row.get("total_cost", ""))) or rent,
        min_lease=clean(row.get("min_lease", ""), 30),
        updated=clean(row.get("updated", ""), 50),
        views=clean(row.get("views", ""), 50),
        publisher=clean(row.get("publisher", ""), 80),
        image=image,
        summary=clean(row.get("summary", ""), 500),
        category_hint="priority" if any(marker in text for marker in PRIORITY_MARKERS) else "general",
        raw_text=text,
        validated_at=NOW.isoformat(),
    )
    item.fingerprint = fingerprint(item)
    return item


def load_facebook_import(source_stats: dict[str, Any]) -> list[Listing]:
    source_stats["allowed_groups"] = len(FB_GROUPS)
    source_payloads: list[tuple[str, str]] = []

    if FB_IMPORT.exists():
        try:
            source_payloads.append(
                ("data/facebook_posts.json", FB_IMPORT.read_text(encoding="utf-8"))
            )
        except OSError as exc:
            source_stats["errors"].append(f"facebook_posts.json 無法讀取：{exc}")

    secret_json = os.environ.get(FB_IMPORT_ENV, "").strip()
    if secret_json:
        source_payloads.append((f"GitHub Actions secret {FB_IMPORT_ENV}", secret_json))

    feed_url = os.environ.get(FB_IMPORT_URL_ENV, "").strip()
    parsed_feed = urllib.parse.urlparse(feed_url)
    if feed_url and parsed_feed.scheme == "https" and parsed_feed.netloc:
        response, feed_text = get_requests(feed_url)
        if (
            response is not None
            and response.status_code == 200
            and 0 < len(feed_text) <= 2_000_000
        ):
            source_payloads.append((f"HTTPS feed {FB_IMPORT_URL_ENV}", feed_text))
        else:
            source_stats["errors"].append(
                f"{FB_IMPORT_URL_ENV} 無法取得有效JSON資料；"
                "請確認HTTPS網址可由GitHub Actions匿名讀取且小於2MB。"
            )
    elif feed_url:
        source_stats["errors"].append(
            f"{FB_IMPORT_URL_ENV} 必須是可匿名讀取的HTTPS網址。"
        )

    rows: list[dict[str, Any]] = []
    import_sources: list[str] = []
    for import_source, raw_json in source_payloads:
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            source_stats["errors"].append(f"{import_source} 不是有效JSON：{exc}")
            continue

        payload_rows = payload.get("posts") if isinstance(payload, dict) else payload
        if not isinstance(payload_rows, list):
            source_stats["errors"].append(
                f"{import_source} 的最外層必須是陣列，或包含 posts 陣列。"
            )
            continue
        import_sources.append(import_source)
        for row in payload_rows:
            if isinstance(row, dict):
                normalized = dict(row)
                normalized.setdefault("_import_source", import_source)
                rows.append(normalized)
            else:
                rows.append(row)

    issue_rows = load_github_facebook_issue_rows(source_stats)
    if issue_rows:
        import_sources.append("GitHub公開Issue投稿")
        rows.extend(issue_rows)

    if not rows and not import_sources:
        source_stats["errors"].append(
            "FB沒有資料來源：請建立 data/facebook_posts.json，或設定GitHub Actions "
            f"secret {FB_IMPORT_ENV}／{FB_IMPORT_URL_ENV}，或使用電子報的FB公開投稿入口。"
            "在不使用Facebook帳號、密碼、Cookie或Session的限制下，程式不會假裝能"
            "匿名抓取受登入保護的社團貼文。"
        )
        return []

    source_stats["import_sources"] = import_sources
    source_stats["import_source"] = " + ".join(import_sources)
    source_stats["input_rows"] = len(rows)
    rejects: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejects[reason] = rejects.get(reason, 0) + 1

    result: list[Listing] = []
    seen_keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            reject("invalid_row")
            continue

        enriched_row = enrich_facebook_row(row, source_stats)
        reasons = facebook_row_reject_reasons(enriched_row)
        url = normalize_facebook_post_url(str(enriched_row.get("url", "")))
        key = facebook_post_key(url)
        if not url or not key:
            for reason in reasons or ["invalid_or_unlisted_group_url"]:
                reject(reason)
            continue
        if key in seen_keys:
            reject("duplicate_url")
            continue
        seen_keys.add(key)

        if reasons:
            for reason in reasons:
                reject(reason)
            continue

        normalized_row = dict(enriched_row)
        normalized_row["url"] = url
        item = parse_social_row(normalized_row, "FB")
        if item:
            result.append(item)
        else:
            reject("listing_validation_failed")

    source_stats["candidate_links"] = len(seen_keys)
    source_stats["validated"] = len(result)
    source_stats["anonymous_verified_posts"] = len(result)
    source_stats["rejects"] = dict(sorted(rejects.items()))
    source_stats["notices"].append(
        f"FB目前合併檔案、Secret、HTTPS feed與GitHub公開投稿；"
        f"允許社團共{len(FB_GROUPS)}個。投稿只有在永久網址、公開貼文內容、"
        "照片、4房、地區與租金都能匿名驗證後才刊出。"
        "Facebook未向未登入訪客提供完整社團貼文清單，"
        "因此候選數代表已取得且可匿名驗證的永久貼文，不是社團全部貼文數。"
    )
    if rows and not result:
        source_stats["errors"].append(
            "FB匯入有資料列，但沒有資料通過社團永久網址、4房、地區、租金、"
            "直接圖片網址與排除條件驗證；請查看 rejects 的精確原因。"
        )
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
        price_dropped = bool(previous and item.rent < previous)
        if price_dropped and not item.old_rent:
            item.old_rent = previous

        if item.source == "FB":
            item.category = "priority" if item.category_hint == "priority" else "general"
        elif item.category_hint == "owner":
            item.category = "owner"
        elif item.category_hint == "discount" or item.old_rent > item.rent:
            item.category = "discount"
        elif item.source == "樂屋網" and item.category_hint == "friendly":
            item.category = "friendly"
        else:
            item.category = "general"

        prices[f"{item.source}:{item.source_id}"] = item.rent

    return items


def filter_recent_duplicates(items: list[Listing], state: dict[str, Any]) -> tuple[list[Listing], int]:
    """保留本輪所有有效物件；歷史只用來統計，不再把頁面清空。

    同一輪若兩個來源產生完全相同的房源指紋，仍只顯示第一筆；但曾在近48小時
    顯示過的有效物件會繼續留在網頁，讓「全部符合條件」反映目前真實供給。
    """
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
    duplicate_count = 0
    current_fingerprints: set[str] = set()

    for item in items:
        source_key = f"{item.source}:{item.source_id}"
        if item.fingerprint and item.fingerprint in current_fingerprints:
            duplicate_count += 1
            continue

        if item.fingerprint:
            current_fingerprints.add(item.fingerprint)
        output.append(item)
        if source_key in recent_keys or item.fingerprint in recent_keys:
            duplicate_count += 1
        else:
            retained_history.append(
                {
                    "source_key": source_key,
                    "fingerprint": item.fingerprint,
                    "sent_at": NOW.isoformat(),
                    "title": item.title,
                    "url": item.url,
                }
            )
            recent_keys.add(source_key)
            recent_keys.add(item.fingerprint)

    state["sent"] = retained_history[-5000:]
    return output, duplicate_count


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


def is_591_featured(item: Listing) -> bool:
    """591「優選好屋」只接受官方列表文字中的明確標籤。"""
    if item.source != "591":
        return False
    if "featured" in item.filter_tags:
        return True
    text = " ".join((item.title, item.summary, item.raw_text))
    return "優選好屋" in text


def listing_filter_tokens(item: Listing) -> list[str]:
    if item.source == "591":
        tokens = ["all"]
        if is_591_featured(item):
            tokens.append("featured")
        if _591_is_owner(item.publisher):
            tokens.append("owner")
        if (
            item.category_hint == "discount"
            or item.category == "discount"
            or item.old_rent > item.rent
        ):
            tokens.append("discount")
        return tokens

    if item.source == "樂屋網":
        tokens = ["rent"]
        tags = set(item.filter_tags)
        if (
            item.category == "owner"
            or item.category_hint == "owner"
            or "owner" in tags
        ):
            tokens.append("owner")
        if (
            item.category == "friendly"
            or item.category_hint == "friendly"
            or "friendly" in tags
        ):
            tokens.append("friendly")
        if (
            item.category == "discount"
            or item.category_hint == "discount"
            or "discount" in tags
            or item.old_rent > item.rent
        ):
            tokens.append("discount")
        return tokens

    tokens = ["all"]
    tokens.append("priority" if item.category == "priority" else "general")
    return tokens


def numeric_value(text: str) -> float:
    match = re.search(r"\d+(?:\.\d+)?", text or "")
    return float(match.group(0)) if match else 0.0


def recency_minutes(item: Listing) -> int:
    """供前端排序使用；數字越小代表越新。"""
    value = item.updated or ""
    if "剛剛" in value or "新上架" in value:
        return 0
    match = re.search(r"(\d+)\s*分鐘", value)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)\s*小時", value)
    if match:
        return int(match.group(1)) * 60
    match = re.search(r"(\d+)\s*天", value)
    if match:
        return int(match.group(1)) * 24 * 60
    match = re.search(r"(\d+)\s*個月", value)
    if match:
        return int(match.group(1)) * 30 * 24 * 60
    return 10**9


def card_badges(item: Listing) -> list[str]:
    badges: list[str] = []
    if is_591_featured(item):
        badges.append("優選好屋")
    if item.source == "591" and _591_is_owner(item.publisher):
        badges.append("屋主直租")
    elif item.source == "樂屋網":
        tokens = listing_filter_tokens(item)
        if "owner" in tokens:
            badges.append("屋主")
        if "friendly" in tokens:
            badges.append("友善房源")
    elif item.source == "FB" and item.category == "priority":
        badges.append("優先物件")
    if item.old_rent > item.rent:
        badges.append("降價")
    return badges


def render_card(item: Listing, order: int = 0) -> str:
    old_html = (
        f'<div class="old"><del>{item.old_rent:,} 元／月</del></div>'
        if item.old_rent > item.rent
        else ""
    )

    facts = [
        item.house_type,
        item.building_type,
        item.layout,
        item.size,
        item.floor,
    ]
    facts_html = "".join(
        f"<span>{esc(value)}</span>" for value in facts if value
    )
    tags = [value for value in (item.equipment, item.min_lease) if value]
    tags_html = "".join(f"<span>{esc(value)}</span>" for value in tags)
    badges_html = "".join(
        f'<span class="tag highlight">{esc(value)}</span>'
        for value in card_badges(item)
    )
    activity = "・".join(v for v in (item.updated, item.views) if v)
    categories = " ".join(listing_filter_tokens(item))
    area = numeric_value(item.size)
    popularity = int(numeric_value(item.views))
    total_cost = item.total_cost or item.rent

    return f"""
    <article class="card" data-categories="{esc(categories)}"
             data-order="{order}" data-recency="{recency_minutes(item)}"
             data-total="{total_cost}" data-rent="{item.rent}"
             data-area="{area}" data-popularity="{popularity}">
      <a class="photo" href="{esc(item.url)}" target="_blank" rel="noopener noreferrer">
        <img src="{esc(item.image)}" alt="{esc(item.title)}" referrerpolicy="no-referrer"
             onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">
        <div class="photo-fallback">照片暫時無法載入<br>點擊前往來源頁</div>
        <span class="source-badge">{esc(source_label(item.source))}</span>
      </a>
      <div class="body">
        <div class="body-main">
          <div class="tag-row">{badges_html}</div>
          <h3><a href="{esc(item.url)}" target="_blank" rel="noopener noreferrer">{esc(item.title)}</a></h3>
          <p class="facts">{facts_html or '<span>房屋資訊未完整提供</span>'}</p>
          <p class="address">⌖ {esc(item.address or item.district or '地址未提供')}</p>
          {f'<div class="feature-row">{tags_html}</div>' if tags_html else ''}
          <div class="activity">{esc(item.publisher)}{f'・{esc(activity)}' if activity else ''}</div>
        </div>
        <div class="price-column">
          {old_html}
          <div class="rent">{item.rent:,}<small> 元／月</small></div>
          <a class="button" href="{esc(item.url)}" target="_blank" rel="noopener noreferrer">查看物件 ↗</a>
        </div>
      </div>
    </article>
    """


def empty_message(stats: dict[str, Any], source: str, category: str) -> str:
    row = stats["sources"][source]
    candidate = int(row.get("candidate_links", 0) or 0)
    validated = int(row.get("validated", 0) or 0)

    if category == "discount":
        return (
            "本輪沒有經來源標示或租金歷史確認的降價物件；"
            "系統不會為了填滿分類而推測或製造降價。"
        )
    if candidate > 0 and validated == 0:
        return (
            f"本次有取得 {source_label(source)} 候選物件，但沒有物件通過來源驗證。"
            "請查看上方「本次紀錄」與來源訊息。"
        )
    if validated > 0:
        return "本次有驗證通過物件，但沒有物件符合此分類。"
    return "本次沒有取得符合條件的候選物件。"


def section_items(
    items: list[Listing],
    source: str,
    category: str,
) -> list[Listing]:
    source_items = [item for item in items if item.source == source]
    if category == "all":
        return source_items
    if source == "591" and category == "featured":
        return [item for item in source_items if is_591_featured(item)]
    if source == "591" and category == "owner":
        return [item for item in source_items if _591_is_owner(item.publisher)]
    if source == "591" and category == "discount":
        return [
            item
            for item in source_items
            if item.category_hint == "discount"
            or item.category == "discount"
            or item.old_rent > item.rent
        ]
    return [item for item in source_items if item.category == category]


def render_subsection(
    items: list[Listing],
    stats: dict[str, Any],
    source: str,
    category: str,
    title: str,
) -> str:
    values = section_items(items, source, category)
    cards = "".join(render_card(item) for item in values)
    if not cards:
        cards = (
            f'<div class="empty">{esc(empty_message(stats, source, category))}</div>'
        )
    return (
        f'<section class="subsection"><header><h2>{esc(title)}</h2><b>{len(values)} 筆</b></header>'
        f'<div class="cards">{cards}</div></section>'
    )


def render_listing_browser(
    items: list[Listing],
    stats: dict[str, Any],
    source: str,
    filters: tuple[tuple[str, str], ...],
    sorts: tuple[tuple[str, str], ...],
) -> str:
    source_items = [item for item in items if item.source == source]
    filter_buttons: list[str] = []
    for index, (key, label) in enumerate(filters):
        count = sum(key in listing_filter_tokens(item) for item in source_items)
        filter_buttons.append(
            f'<button type="button" class="filter-button{" active" if index == 0 else ""}" '
            f'data-filter="{esc(key)}">{esc(label)} <b>{count}</b></button>'
        )

    direction_labels = {
        "recency": ("由新到舊", "由舊到新", "asc"),
        "total": ("總費用低到高", "總費用高到低", "asc"),
        "rent": ("租金低到高", "租金高到低", "asc"),
        "area": ("坪數小到大", "坪數大到小", "asc"),
        "popularity": ("人氣高到低", "人氣低到高", "desc"),
    }
    sort_controls: list[str] = []
    for index, (key, label) in enumerate(sorts):
        first_label, second_label, default_direction = direction_labels[key]
        first_direction = "desc" if key == "popularity" else "asc"
        second_direction = "asc" if first_direction == "desc" else "desc"
        sort_controls.append(
            f'<label class="sort-control{" active" if index == 0 else ""}">'
            f'<span>{esc(label)}</span>'
            f'<select class="sort-select" data-sort="{esc(key)}" '
            f'data-default-direction="{default_direction}" '
            f'aria-current="{"true" if index == 0 else "false"}" '
            f'aria-label="{esc(source_label(source))} {esc(label)}排序">'
            f'<option value="{first_direction}"'
            f'{" selected" if default_direction == first_direction else ""}>'
            f'{esc(first_label)}</option>'
            f'<option value="{second_direction}"'
            f'{" selected" if default_direction == second_direction else ""}>'
            f'{esc(second_label)}</option>'
            "</select></label>"
        )
    cards = "".join(
        render_card(item, order)
        for order, item in enumerate(source_items)
    )
    empty = esc(empty_message(stats, source, filters[0][0]))

    return f"""
    <section class="listing-browser" data-listing-browser data-source="{esc(source)}">
      <div class="filter-bar">
        <div class="filter-group" data-filter-count="{len(filters)}"
             role="group" aria-label="{esc(source_label(source))} 分類">
          {''.join(filter_buttons)}
        </div>
        <div class="sort-row">
          <span>排序：</span>
          <div class="sort-group" role="group" aria-label="{esc(source_label(source))} 排序">
            {''.join(sort_controls)}
          </div>
          <strong class="visible-count">{len(source_items)} 筆</strong>
        </div>
      </div>
      <div class="listing-list">
        {cards}
        <div class="empty browser-empty"{" hidden" if cards else ""}>{empty}</div>
      </div>
    </section>
    """


def render_status(stats: dict[str, Any], source: str) -> str:
    row = stats["sources"][source]
    errors = row.get("errors", [])
    error_html = "".join(f"<li>{esc(error)}</li>" for error in errors[:8])
    notices = row.get("notices", [])
    notice_html = "".join(f"<li>{esc(notice)}</li>" for notice in notices[:8])

    diagnostics = ""
    if source == "591":
        rejects = row.get("rejects", {}) or {}
        reject_text = "、".join(f"{key}={value}" for key, value in sorted(rejects.items())) or "無"
        snapshot_html = ""
        if row.get("fallback"):
            snapshot_html = (
                '<strong class="fallback-warning">⚠ 本輪顯示上次成功快照：'
                f"{row.get('snapshot_items', 0)} 筆，"
                f"{row.get('snapshot_age_hours', 0)} 小時前；未重新驗證</strong>"
            )
        diagnostics = (
            f"{snapshot_html}"
            f"<span>列表快照 {row.get('list_cache', 0)} 筆</span>"
            f"<details><summary>591排除診斷</summary><div>{esc(reject_text)}</div></details>"
        )
    elif source == "FB":
        diagnostics = (
            f"<span>允許社團 {row.get('allowed_groups', len(FB_GROUPS))} 個</span>"
            f"<span>匿名驗證貼文 "
            f"{row.get('anonymous_verified_posts', row.get('validated', 0))} 筆</span>"
            f"<span>公開投稿 {row.get('issue_submissions_seen', 0)} 筆</span>"
            f"<span>自動補齊 {row.get('public_metadata_enriched', 0)} 筆</span>"
        )

    return f"""
    <div class="source-status">
      <div class="status-primary">
        <b>本次紀錄</b>
        <span>候選 {row.get('candidate_links', 0)} 筆</span>
        <span>驗證通過 {row.get('validated', 0)} 筆</span>
        <span>本頁顯示 {row.get('published', 0)} 筆</span>
      </div>
      <div class="status-secondary">
        {diagnostics}
        {f'<details><summary>來源說明</summary><ul>{notice_html}</ul></details>' if notices else ''}
        {f'<details><summary>查看來源訊息</summary><ul>{error_html}</ul></details>' if errors else ''}
      </div>
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
:root{{--orange:#f56a00;--orange-soft:#fff5eb;--bg:#f6f7f8;--line:#e5e7eb;--muted:#69717d;--fb:#1877f2;--raku:#d65431}}
*{{box-sizing:border-box}}
html{{scroll-behavior:smooth}}
body{{margin:0;background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",sans-serif;color:#202124}}
a{{color:inherit}}
.wrap{{width:min(1120px,calc(100% - 28px));margin:auto}}
body>header{{
  position:sticky;
  top:0;
  z-index:1000;
  background:rgba(255,255,255,.97);
  border-bottom:3px solid var(--orange);
  padding:14px 0 12px;
  box-shadow:0 5px 20px rgba(0,0,0,.09);
  backdrop-filter:blur(10px);
}}
h1{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;font-size:clamp(25px,4vw,36px);margin:0 0 6px}}
h1 time{{font-size:15px;color:#995018;background:var(--orange-soft);border:1px solid #ffd5b5;padding:6px 10px;border-radius:999px;white-space:nowrap}}
.subtitle{{font-size:15px;line-height:1.45;color:#4e5660;margin:0}}
.source-nav{{display:flex;gap:9px;flex-wrap:wrap;margin-top:9px}}
.source-nav a{{text-decoration:none;background:#fff;padding:9px 18px;border:1px solid var(--line);border-radius:7px;font-weight:900}}
.source-nav a:hover{{border-color:var(--orange);color:var(--orange)}}
.source-nav a:nth-child(2){{color:var(--fb)}}
.source-nav a:nth-child(3){{color:var(--raku)}}
.statusbar{{background:#23272d;color:#fff;padding:12px 0;font-size:14px}}
main{{padding:22px 0 48px}}
.notice{{background:#fff;border-left:5px solid var(--orange);padding:15px 18px;border-radius:10px;line-height:1.7}}
.source-block{{margin-top:22px;scroll-margin-top:175px}}
.source-heading{{display:flex;align-items:end;justify-content:space-between;gap:12px;margin-bottom:10px}}
.source-heading h2{{font-size:31px;margin:0}}
.source-heading a{{font-size:14px;color:#555;text-underline-offset:3px}}
.source-actions{{display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap}}
.source-actions .submission-link{{color:#fff;background:var(--fb);padding:7px 10px;border-radius:6px;text-decoration:none;font-weight:850}}
.source-status{{display:flex;flex-direction:column;align-items:flex-end;background:#fff;padding:12px 14px;border:1px solid var(--line);border-radius:10px}}
.status-primary{{width:75%;align-self:flex-start;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));align-items:center;direction:ltr}}
.status-primary>*{{min-width:0;padding:2px 10px;direction:ltr;text-align:center}}
.status-primary span{{color:#555}}
.status-secondary{{width:100%;display:flex;gap:8px 14px;align-items:center;justify-content:flex-end;text-align:right;flex-wrap:wrap}}
.status-secondary:empty{{display:none}}
.source-status details{{width:100%;color:#8a3f00;text-align:right}}
.source-status ul{{margin:8px 0 0;padding-left:20px;text-align:left}}
.source-status .fallback-warning{{width:100%;color:#8a3f00;background:#fff3cd;border:1px solid #f1ce72;padding:9px 11px;border-radius:7px}}
.listing-browser{{margin-top:14px}}
.filter-bar{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:0 16px;box-shadow:0 2px 8px #00000008}}
.filter-group{{width:75%;margin-right:auto;display:grid;grid-template-columns:repeat(var(--filter-count),minmax(0,1fr));align-items:stretch;direction:ltr;border-bottom:1px solid var(--line)}}
.filter-group[data-filter-count="4"]{{--filter-count:4}}
.filter-group[data-filter-count="3"]{{--filter-count:3}}
.filter-button{{width:100%;appearance:none;border:0;background:transparent;color:#4f5965;font:inherit;font-weight:800;cursor:pointer;padding:14px 8px;border-bottom:3px solid transparent;direction:ltr;text-align:center}}
.filter-button b{{font-size:12px;color:#8a929b;margin-left:3px}}
.filter-button:hover{{color:var(--orange)}}
.filter-button.active{{color:var(--orange);border-bottom-color:var(--orange)}}
.filter-button.active b{{color:var(--orange)}}
.sort-row{{display:flex;align-items:center;flex-direction:row-reverse;justify-content:flex-start;gap:4px;min-height:52px;padding:6px 0}}
.sort-row>span{{color:#8a929b;font-size:13px;font-weight:800}}
.sort-group{{display:flex;align-items:center;flex-direction:row-reverse;justify-content:flex-start;gap:4px;flex-wrap:wrap}}
.sort-control{{display:flex;align-items:center;flex-direction:row-reverse;gap:2px;padding:0;border:0;background:transparent;color:#555;font-size:13px;font-weight:850}}
.sort-control.active{{color:#b55a09}}
.sort-control.active .sort-select{{border-color:#ffb879;background:var(--orange-soft)}}
.sort-select{{max-width:118px;border:1px solid #d9dde3;border-radius:5px;background:#fff;color:#30343a;padding:5px 19px 5px 6px;font:inherit;font-size:12px;cursor:pointer}}
.sort-select:focus{{outline:2px solid #ffb879;outline-offset:1px}}
.visible-count{{color:#5a626d;white-space:nowrap;margin-right:2px;direction:ltr}}
.listing-list{{display:flex;flex-direction:column;gap:12px;margin-top:12px}}
.card{{display:grid;grid-template-columns:minmax(260px,32%) minmax(0,1fr);min-height:245px;border:1px solid var(--line);border-radius:9px;overflow:hidden;background:#fff;box-shadow:0 2px 8px #0000000a}}
.card:hover{{border-color:#ffc596;box-shadow:0 6px 22px #00000012}}
.photo{{height:100%;min-height:245px;display:block;position:relative;background:#596273;overflow:hidden}}
.photo img{{width:100%;height:100%;object-fit:cover}}
.photo .source-badge{{position:absolute;left:10px;top:10px;background:#111c;color:#fff;padding:6px 9px;border-radius:5px;font-size:13px;font-weight:900}}
.photo-fallback{{display:none;position:absolute;inset:0;align-items:center;justify-content:center;text-align:center;color:#fff;font-weight:900;background:#4b5563}}
.body{{display:grid;grid-template-columns:minmax(0,1fr) 175px;gap:18px;padding:18px 20px}}
.body-main{{min-width:0}}
h3{{font-size:21px;line-height:1.4;margin:7px 0 12px}}
h3 a{{text-decoration:none}}
.tag-row,.feature-row,.facts{{display:flex;gap:6px;align-items:center;flex-wrap:wrap}}
.tag-row:empty{{display:none}}
.tag{{font-size:13px;background:#edf5ff;color:#3472ad;padding:4px 7px;border-radius:4px}}
.tag.highlight{{background:#eaf7ed;color:#218145;font-weight:850}}
.facts{{margin:0 0 11px}}
.facts span{{font-size:14px;font-weight:800;padding-right:9px;border-right:1px solid #d9dce1}}
.facts span:last-child{{border-right:0}}
.address{{margin:0 0 11px;color:#59616c;line-height:1.55}}
.feature-row span{{font-size:13px;background:#fff4e8;color:#aa590f;padding:4px 7px;border-radius:4px}}
.activity{{color:var(--muted);font-size:13px;margin-top:14px}}
.price-column{{display:flex;flex-direction:column;align-items:flex-end;justify-content:center;text-align:right;border-left:1px solid #f0f1f3;padding-left:16px}}
.old{{color:#9a9fa7;font-size:13px;margin-bottom:2px}}
.rent{{font-size:29px;color:#df2a1b;font-weight:950;white-space:nowrap}}
.rent small{{font-size:13px;color:#df2a1b}}
.button{{display:inline-block;text-align:center;margin-top:16px;padding:9px 14px;background:#fff1df;color:#c35f00;text-decoration:none;border-radius:6px;font-weight:900}}
.button:hover{{background:var(--orange);color:#fff}}
.empty{{border:1px dashed #bbb;background:#fff;border-radius:8px;padding:28px;text-align:center;color:var(--muted)}}
.social-links{{display:flex;justify-content:flex-end;gap:7px;flex-wrap:wrap;margin-top:12px}}
.social-links a{{background:var(--fb);color:#fff;text-decoration:none;padding:8px 10px;border-radius:6px;font-weight:800}}
.social-note{{background:#fff8e9;border:1px solid #ffd7a6;padding:13px;border-radius:9px;margin-top:12px;line-height:1.7}}
.back-to-top{{position:fixed;right:18px;bottom:18px;z-index:1200;width:48px;height:48px;border:0;border-radius:50%;background:var(--orange);color:#fff;font-size:25px;font-weight:950;line-height:1;cursor:pointer;box-shadow:0 5px 18px #0004}}
.back-to-top:hover{{background:#d95d00;transform:translateY(-2px)}}
.back-to-top:focus-visible{{outline:3px solid #ffbd87;outline-offset:3px}}
[hidden]{{display:none!important}}
@media(max-width:820px){{
  .status-primary,.filter-group{{width:100%}}
  .card{{grid-template-columns:minmax(210px,34%) minmax(0,1fr)}}
  .body{{grid-template-columns:1fr}}
  .price-column{{align-items:flex-start;text-align:left;border-left:0;border-top:1px solid #f0f1f3;padding:10px 0 0}}
  .button{{margin-top:9px}}
}}
@media(max-width:620px){{
  body>header{{padding:10px 0 9px}}
  h1{{font-size:25px}}
  h1 time{{font-size:12px;padding:5px 8px}}
  .subtitle{{font-size:13px;line-height:1.4}}
  .source-nav{{gap:6px;margin-top:8px}}
  .source-nav a{{flex:1;text-align:center;padding:9px 6px}}
  .filter-bar{{padding:0 9px}}
  .filter-group{{display:flex;justify-content:flex-start;flex-wrap:nowrap;overflow-x:auto}}
  .filter-button{{flex:1 0 auto}}
  .filter-button{{padding-left:8px;padding-right:8px;font-size:13px}}
  .sort-row{{align-items:flex-start;flex-wrap:wrap;padding:8px 0}}
  .sort-row>span{{padding-top:9px}}
  .sort-group{{width:100%}}
  .sort-control{{flex:0 1 auto;justify-content:flex-start}}
  .sort-select{{max-width:118px;min-width:0}}
  .visible-count{{width:100%;padding:0 8px 4px;text-align:left}}
  .back-to-top{{right:12px;bottom:12px;width:44px;height:44px;font-size:23px}}
  .card{{grid-template-columns:minmax(130px,38%) minmax(0,1fr);min-height:220px}}
  .photo{{min-height:220px}}
  .body{{padding:12px;gap:10px}}
  h3{{font-size:17px;margin-top:4px}}
  .facts span{{font-size:12px}}
  .feature-row{{display:none}}
  .activity{{font-size:12px}}
  .rent{{font-size:23px}}
  .source-block{{scroll-margin-top:190px}}
}}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>桃園四房以上租屋快報
      <time datetime="{NOW.isoformat(timespec='minutes')}" aria-label="本次執行時間">
        {NOW.strftime('%Y/%m/%d %H:%M')}
      </time>
    </h1>
    <p class="subtitle">三個來源分區顯示；每筆物件均包含照片與來源直達連結，本輪有效物件不因近48小時曾顯示而隱藏。</p>
    <nav class="source-nav">
      <a href="#source-591">591</a>
      <a href="#source-fb">FB社團</a>
      <a href="#source-rakuya">樂屋網</a>
    </nav>
  </div>
</header>

<div class="statusbar"><div class="wrap">
產生時間：{NOW.strftime('%Y/%m/%d %H:%M')}｜候選 {stats['candidates']} 筆｜
驗證通過 {stats['validated']} 筆｜近48小時曾顯示／同輪重複 {stats['duplicates']} 筆｜
本次顯示 {len(items)} 筆
</div></div>

<main class="wrap">
<div class="notice">頁面顯示本輪所有驗證通過的有效物件；近48小時紀錄只提供重複診斷，不會再把仍有效的房源從頁面隱藏。來源被阻擋或FB資料來源未設定時會直接顯示原因。</div>

<div id="source-591" class="source-block">
  <div class="source-heading"><h2>591</h2><a href="https://rent.591.com.tw/list?kind=1&layout=4&region=6" target="_blank">開啟591搜尋 ↗</a></div>
  {render_status(stats, '591')}
  {render_listing_browser(
      items,
      stats,
      '591',
      (('all', '全部'), ('featured', '優選好屋'), ('owner', '屋主直租'), ('discount', '降價物件')),
      (('recency', '最新'), ('total', '租金總費用'), ('rent', '租金'), ('area', '坪數')),
  )}
</div>

<div id="source-fb" class="source-block">
  <div class="source-heading">
    <h2>FB社團</h2>
    <div class="source-actions">
      <a class="submission-link" href="{FB_ISSUE_TEMPLATE_URL}" target="_blank" rel="noopener noreferrer">提交FB永久貼文 ↗</a>
      <a href="https://www.facebook.com/groups/feed/" target="_blank" rel="noopener noreferrer">開啟Facebook社團 ↗</a>
    </div>
  </div>
  {render_status(stats, 'FB')}
  <div class="social-note">
    <strong>Facebook真實資料自動處理：</strong>
    點選「提交FB永久貼文」後，只要提供允許社團的單篇永久網址；完整貼文文字與照片頁可選填。
    GitHub Actions會以不含Cookie的匿名請求讀取公開中繼資料，自動補齊貼文內容、照片、刊登者、
    刊登時間、租金、格局、坪數與地區，並把真實照片保存到本站後再驗證刊出。
    檔案、Secret與匿名HTTPS JSON feed現在會合併處理，不會因既有JSON存在而忽略其他來源。
    不需要、也請勿提供Facebook帳號、密碼、Cookie或Session；社團首頁、分享短網址或無法匿名讀取的私密貼文仍不會刊出。
    在不登入的限制下，Facebook不提供完整社團貼文清單；本頁只顯示已取得永久網址且可匿名驗證的真實物件。
  </div>
  <div class="social-links">{fb_buttons}</div>
  {render_listing_browser(
      items,
      stats,
      'FB',
      (('all', '全部'), ('priority', '屋主自租／仲介勿擾／社宅勿擾'), ('general', '其他符合物件')),
      (('recency', '最新'), ('total', '租金總費用'), ('rent', '租金'), ('area', '坪數')),
  )}
</div>

<div id="source-rakuya" class="source-block">
  <div class="source-heading"><h2>樂屋網</h2><a href="https://rent.rakuya.com.tw/" target="_blank">開啟樂屋網 ↗</a></div>
  {render_status(stats, '樂屋網')}
  {render_listing_browser(
      items,
      stats,
      '樂屋網',
      (('rent', '出租'), ('owner', '屋主'), ('friendly', '友善房源'), ('discount', '最新降價')),
      (('recency', '最近更新'), ('rent', '租金'), ('area', '室內坪數'), ('popularity', '人氣')),
  )}
</div>
</main>
<button id="back-to-top" class="back-to-top" type="button"
        aria-label="回到頁面頂端" title="回到頁面頂端" hidden>↑</button>
<script>
const backToTop = document.querySelector('#back-to-top');
if (backToTop) {{
  const updateBackToTop = () => {{
    backToTop.hidden = window.scrollY < 480;
  }};
  backToTop.addEventListener('click', () => {{
    window.scrollTo({{top: 0, behavior: 'smooth'}});
  }});
  window.addEventListener('scroll', updateBackToTop, {{passive: true}});
  updateBackToTop();
}}

document.querySelectorAll('[data-listing-browser]').forEach((panel) => {{
  const list = panel.querySelector('.listing-list');
  const cards = Array.from(panel.querySelectorAll('.card'));
  const empty = panel.querySelector('.browser-empty');
  const count = panel.querySelector('.visible-count');
  const filterButtons = Array.from(panel.querySelectorAll('.filter-button'));
  const sortSelects = Array.from(panel.querySelectorAll('.sort-select'));
  let activeFilter = filterButtons[0]?.dataset.filter || 'all';
  let activeSort = sortSelects[0]?.dataset.sort || 'order';
  let direction = sortSelects[0]?.value || 'asc';

  const numeric = (card, key) => {{
    const value = Number(card.dataset[key]);
    return Number.isFinite(value) ? value : 0;
  }};

  const apply = () => {{
    const visible = cards.filter((card) =>
      (card.dataset.categories || '').split(' ').includes(activeFilter)
    );
    cards.forEach((card) => {{ card.hidden = !visible.includes(card); }});
    visible.sort((a, b) => {{
      const first = numeric(a, activeSort);
      const second = numeric(b, activeSort);
      const firstMissing = (
        (activeSort === 'recency' && first >= 1000000000) ||
        (['area', 'popularity'].includes(activeSort) && first <= 0)
      );
      const secondMissing = (
        (activeSort === 'recency' && second >= 1000000000) ||
        (['area', 'popularity'].includes(activeSort) && second <= 0)
      );
      if (firstMissing !== secondMissing) return firstMissing ? 1 : -1;
      const delta = direction === 'asc' ? first - second : second - first;
      return delta || numeric(a, 'order') - numeric(b, 'order');
    }});
    visible.forEach((card) => list.insertBefore(card, empty));
    empty.hidden = visible.length > 0;
    if (!visible.length) empty.textContent = '此分類目前沒有符合條件的物件。';
    count.textContent = `${{visible.length}} 筆`;
  }};

  filterButtons.forEach((button) => button.addEventListener('click', () => {{
    activeFilter = button.dataset.filter;
    filterButtons.forEach((value) => {{
      const selected = value === button;
      value.classList.toggle('active', selected);
      value.setAttribute('aria-pressed', String(selected));
    }});
    apply();
  }}));

  sortSelects.forEach((select) => select.addEventListener('change', () => {{
    activeSort = select.dataset.sort;
    direction = select.value;
    sortSelects.forEach((value) => {{
      const selected = value === select;
      value.closest('.sort-control')?.classList.toggle('active', selected);
      value.setAttribute('aria-current', selected ? 'true' : 'false');
    }});
    apply();
  }}));

  apply();
}});
</script>
</body>
</html>"""


def empty_source_stats() -> dict[str, Any]:
    return {
        "candidate_links": 0,
        "validated": 0,
        "published": 0,
        "errors": [],
        "notices": [],
        "list_cache": 0,
        "rejects": {},
    }


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
        candidates.extend(collect_591_listings(stats["sources"]["591"]))

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
