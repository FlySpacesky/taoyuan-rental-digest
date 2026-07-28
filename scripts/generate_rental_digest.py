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
FB_IMPORT = ROOT / "data" / "facebook_posts.json"
FB_IMPORT_ENV = "FACEBOOK_POSTS_JSON"
FB_IMPORT_URL_ENV = "FACEBOOK_POSTS_JSON_URL"
_591_BFF_LIST_URL = "https://bff-house.591.com.tw/v3/web/rent/list"

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
        browse_count = money(str(raw_item.get("browse_count", "")))
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
            updated=clean(raw_item.get("refresh_time", ""), 80),
            views=f"{browse_count}人瀏覽" if browse_count else "",
            publisher=publisher,
            image=image,
            summary=text,
            raw_text=text,
            validated_at=NOW.isoformat(),
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
                response: requests.Response | None = None
                raw = ""
                ids: list[str] = []
                cards: dict[str, Listing] = {}
                if bff_cards:
                    cards = bff_cards
                    ids = list(cards)
                    _591_BFF_CACHE_IDS.update(cards)
                elif bff_status != 200:
                    # BFF 被擋或暫時失效時，才讀 SSR HTML。
                    response, raw = fetch_html(url)
                    ids = extract_591_ids(raw)
                    cards = parse_591_list_cards(raw)

                if (not ids or not cards) and bff_status != 200:
                    rendered = browser.html(url)
                    if rendered:
                        rendered_cards = parse_591_list_cards(rendered)
                        if rendered_cards:
                            raw = rendered
                            cards = rendered_cards
                            ids = extract_591_ids(rendered)

                for item_id, item in cards.items():
                    existing = _591_LIST_CACHE.get(item_id)
                    if existing is None or len(item.summary) > len(existing.summary):
                        _591_LIST_CACHE[item_id] = item

                # 只把已成功解析為目標卡片的 ID 放進詳情驗證，排除廣告、
                # 社區 market 連結與非 4 房卡片。
                new_ids = [item_id for item_id in cards if item_id not in seen]
                status = response.status_code if response is not None else "browser"
                print(
                    f"[591] mode={mode} {district} page={page_no} "
                    f"status={status} bff={bff_status} firstRow={first_row} "
                    f"ids={len(ids)} cards={len(cards)} "
                    f"cache={len(_591_LIST_CACHE)} new={len(new_ids)}"
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

                time.sleep(0.75)

    source_stats["candidate_links"] = len(links)
    source_stats["list_cache"] = len(_591_LIST_CACHE)
    source_stats["rejects"] = dict(sorted(_591_REJECTS.items()))
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

    # BFF 已提供當輪官方清單快照時，先用 requests 嘗試詳情頁；若 Runner
    # 直接被 403，立即使用經嚴格驗證的快照，避免每筆再啟動無效 Chromium。
    if cached and item_id in _591_BFF_CACHE_IDS:
        response, raw = get_requests(url)
        if response is not None and response.status_code in {404, 410}:
            reject_591("dead_404_410")
            return None
        if (
            response is None
            or response.status_code in {401, 403, 429}
            or looks_blocked(raw)
        ):
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


def normalize_facebook_post_url(url: str) -> str:
    """只接受設定清單內社團的單篇貼文網址，並移除追蹤參數。"""
    try:
        parsed = urllib.parse.urlparse(str(url).strip())
    except ValueError:
        return ""

    if parsed.scheme not in {"http", "https"}:
        return ""
    if (parsed.hostname or "").lower() not in {"facebook.com", "www.facebook.com"}:
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


def is_public_http_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url).strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_social_row(row: dict[str, Any], source: str) -> Listing | None:
    text = clean(" ".join(str(row.get(k, "")) for k in row), 8000)
    if not has_four_rooms(text) or excluded(text) or "代理人" in text:
        return None

    district = clean(row.get("district") or district_from_text(text), 20)
    if district not in ALLOWED_DISTRICTS:
        return None

    url = normalize_facebook_post_url(str(row.get("url", "")))
    image = str(row.get("image", "")).strip()
    title = clean(row.get("title") or text.split("。")[0], 180)
    rent = money(str(row.get("rent", "")))

    if not url or not is_public_http_url(image) or not title or not rent:
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
    raw_json = ""
    import_source = ""

    if FB_IMPORT.exists():
        try:
            raw_json = FB_IMPORT.read_text(encoding="utf-8")
            import_source = "data/facebook_posts.json"
        except OSError as exc:
            source_stats["errors"].append(f"facebook_posts.json 無法讀取：{exc}")
            return []
    else:
        raw_json = os.environ.get(FB_IMPORT_ENV, "").strip()
        if raw_json:
            import_source = f"GitHub Actions secret {FB_IMPORT_ENV}"
        else:
            feed_url = os.environ.get(FB_IMPORT_URL_ENV, "").strip()
            parsed_feed = urllib.parse.urlparse(feed_url)
            if feed_url and parsed_feed.scheme == "https" and parsed_feed.netloc:
                response, feed_text = get_requests(feed_url)
                if (
                    response is not None
                    and response.status_code == 200
                    and 0 < len(feed_text) <= 2_000_000
                ):
                    raw_json = feed_text
                    import_source = f"HTTPS feed {FB_IMPORT_URL_ENV}"
                else:
                    source_stats["errors"].append(
                        f"{FB_IMPORT_URL_ENV} 無法取得有效JSON資料；"
                        "請確認HTTPS網址可由GitHub Actions匿名讀取且小於2MB。"
                    )
                    return []
            elif feed_url:
                source_stats["errors"].append(
                    f"{FB_IMPORT_URL_ENV} 必須是可匿名讀取的HTTPS網址。"
                )
                return []

    if not raw_json:
        source_stats["errors"].append(
            "FB沒有資料來源：請建立 data/facebook_posts.json，或設定GitHub Actions "
            f"secret {FB_IMPORT_ENV}／{FB_IMPORT_URL_ENV}。在不使用Facebook帳號、密碼、"
            "Cookie或Session的限制下，程式不會假裝能匿名抓取受登入保護的社團貼文。"
        )
        return []

    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        source_stats["errors"].append(f"{import_source} 不是有效JSON：{exc}")
        return []

    rows = payload.get("posts") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        source_stats["errors"].append(
            f"{import_source} 的最外層必須是陣列，或包含 posts 陣列。"
        )
        return []

    source_stats["import_source"] = import_source
    source_stats["input_rows"] = len(rows)
    rejects: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejects[reason] = rejects.get(reason, 0) + 1

    result: list[Listing] = []
    seen_urls: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            reject("invalid_row")
            continue

        url = normalize_facebook_post_url(str(row.get("url", "")))
        if not url:
            reject("invalid_or_unlisted_group_url")
            continue
        if url in seen_urls:
            reject("duplicate_url")
            continue
        seen_urls.add(url)

        normalized_row = dict(row)
        normalized_row["url"] = url
        item = parse_social_row(normalized_row, "FB")
        if item:
            result.append(item)
        else:
            reject("listing_validation_failed")

    source_stats["candidate_links"] = len(seen_urls)
    source_stats["validated"] = len(result)
    source_stats["rejects"] = dict(sorted(rejects.items()))
    if seen_urls and not result:
        source_stats["errors"].append(
            "FB匯入有貼文網址，但沒有資料通過4房、地區、租金、圖片與排除條件驗證。"
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


def render_status(stats: dict[str, Any], source: str) -> str:
    row = stats["sources"][source]
    errors = row.get("errors", [])
    error_html = "".join(f"<li>{esc(error)}</li>" for error in errors[:8])

    diagnostics = ""
    if source == "591":
        rejects = row.get("rejects", {}) or {}
        reject_text = "、".join(f"{key}={value}" for key, value in sorted(rejects.items())) or "無"
        diagnostics = (
            f"<span>列表快照 {row.get('list_cache', 0)} 筆</span>"
            f"<details><summary>591排除診斷</summary><div>{esc(reject_text)}</div></details>"
        )

    return f"""
    <div class="source-status">
      <b>本次紀錄</b>
      <span>候選 {row.get('candidate_links', 0)} 筆</span>
      <span>驗證通過 {row.get('validated', 0)} 筆</span>
      <span>本頁顯示 {row.get('published', 0)} 筆</span>
      {diagnostics}
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
  {render_subsection(items, stats, '591', 'owner', '屋主直租')}
  {render_subsection(items, stats, '591', 'discount', '降價物件')}
  {render_subsection(items, stats, '591', 'all', '全部符合條件物件')}
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
    return {
        "candidate_links": 0,
        "validated": 0,
        "published": 0,
        "errors": [],
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
        links_591 = crawl_591_links(stats["sources"]["591"])
        valid_591 = 0
        for index, url in enumerate(links_591, 1):
            print(f"[591 detail] {index}/{len(links_591)} {url}")
            item = parse_591_detail(url)
            if item:
                candidates.append(item)
                valid_591 += 1
        stats["sources"]["591"]["validated"] = valid_591
        stats["sources"]["591"]["list_cache"] = len(_591_LIST_CACHE)
        stats["sources"]["591"]["rejects"] = dict(sorted(_591_REJECTS.items()))
        if links_591 and valid_591 == 0:
            reject_summary = ", ".join(
                f"{key}={value}" for key, value in sorted(_591_REJECTS.items())
            ) or "無拒絕原因紀錄"
            stats["sources"]["591"]["errors"].append(
                "591有取得候選物件，但0筆通過驗證。"
                f"列表快照={len(_591_LIST_CACHE)}；排除原因：{reject_summary}。"
                "請依 rejects 判斷是網站阻擋、缺欄位、4房解析或排除條件造成。"
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
