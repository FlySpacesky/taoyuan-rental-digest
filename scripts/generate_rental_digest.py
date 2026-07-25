#!/usr/bin/env python3
"""
桃園四房以上租屋快報抓取器

修正重點
1. 591、樂屋網逐頁抓到底，不限制固定頁數。
2. 每筆物件在輸出前重新開啟單筆頁驗證。
3. 頁面出現「物件不存在／已關閉／已刪除」等文字即剔除。
4. 樂屋網從搜尋結果直接解析單筆 rent.rakuya.com.tw/item/... 或
   community.rakuya.com.tw/.../rent/... 永久連結與原圖。
5. 屋主、友善、降價、一般物件互斥，不會跨區重複。
6. 近 48 小時跨來源去重。
7. Facebook 不接收帳號密碼或 Cookie；可讀取人工匯出的貼文 JSON。
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
from typing import Iterable

import requests
from bs4 import BeautifulSoup

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

EXCLUDE_MARKERS = (
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

OWNER_MARKERS = (
    "屋主直租",
    "屋主自租",
    "屋主本人",
    "免仲介費",
    "仲介勿擾",
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


def clean(value: str, limit: int = 500) -> str:
    value = html.unescape(value or "")
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


def fetch(url: str, *, attempts: int = 3) -> tuple[requests.Response | None, str]:
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=25, allow_redirects=True)
            text = response.text
            if response.status_code in {429, 500, 502, 503, 504}:
                raise requests.RequestException(f"temporary status {response.status_code}")
            return response, text
        except requests.RequestException as exc:
            if attempt + 1 >= attempts:
                print(f"[WARN] fetch failed: {url}: {exc}", file=sys.stderr)
                return None, ""
            time.sleep(1.2 * (2**attempt))
    return None, ""


def is_dead_page(response: requests.Response | None, text: str, expected_host: str) -> bool:
    if response is None or response.status_code in {404, 410}:
        return True
    final = urllib.parse.urlparse(response.url)
    if expected_host not in (final.hostname or ""):
        return True
    normalized = clean(text, 200000)
    return any(marker in normalized for marker in INVALID_MARKERS)


def meta(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        node = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if node and node.get("content"):
            return clean(node["content"], 1000)
    return ""


def first_json_ld(soup: BeautifulSoup) -> dict:
    for node in soup.select('script[type*="ld+json"]'):
        try:
            data = json.loads(node.get_text(strip=True))
        except (TypeError, json.JSONDecodeError):
            continue
        values = data if isinstance(data, list) else [data]
        for value in values:
            if isinstance(value, dict):
                if "@graph" in value and isinstance(value["@graph"], list):
                    values.extend(value["@graph"])
                if any(k in value for k in ("name", "headline", "offers", "image")):
                    return value
    return {}


def normalize_item_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def fingerprint(item: Listing) -> str:
    address = re.sub(r"[^\w\u4e00-\u9fff]", "", item.address.lower())
    title = re.sub(r"[^\w\u4e00-\u9fff]", "", item.title.lower())
    layout = re.sub(r"\s+", "", item.layout)
    base = f"{address}|{item.rent}|{layout}|{title[:36]}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def has_four_rooms(text: str) -> bool:
    match = re.search(r"(\d+)\s*房", text)
    return bool(match and int(match.group(1)) >= 4)


def allowed_district(item: Listing) -> bool:
    allowed = set(DISTRICTS_591)
    return item.district in allowed or any(name in item.address for name in allowed)


def excluded(text: str) -> bool:
    return any(marker in text for marker in EXCLUDE_MARKERS)


def extract_591_links(raw: str) -> list[str]:
    links: list[str] = []
    # HTML anchors
    soup = BeautifulSoup(raw, "html.parser")
    for anchor in soup.select("a[href]"):
        href = urllib.parse.urljoin("https://rent.591.com.tw", anchor.get("href", ""))
        if re.search(r"rent\.591\.com\.tw/(?:home/)?\d{7,}", href):
            links.append(normalize_item_url(href))
    # Embedded JSON / escaped URLs
    for match in re.finditer(r'(?:https?:\\/\\/rent\.591\.com\.tw\\/(?:home\\/)?|/(?:home/)?)(\d{7,})', raw):
        links.append(f"https://rent.591.com.tw/{match.group(1)}")
    return list(dict.fromkeys(links))


def crawl_591_listing_links() -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for district, section in DISTRICTS_591.items():
        empty_pages = 0
        for page in range(1, 101):
            url = (
                "https://rent.591.com.tw/list?"
                + urllib.parse.urlencode(
                    {"kind": 1, "layout": 4, "region": 6, "section": section, "page": page}
                )
            )
            _, raw = fetch(url)
            links = [link for link in extract_591_links(raw) if link not in seen]
            if not links:
                empty_pages += 1
                if empty_pages >= 2:
                    break
            else:
                empty_pages = 0
            for link in links:
                seen.add(link)
                found.append(link)
            print(f"[591] {district} page={page} new={len(links)}")
    return found


def parse_591_detail(url: str) -> Listing | None:
    response, raw = fetch(url)
    if is_dead_page(response, raw, "591.com.tw"):
        return None
    soup = BeautifulSoup(raw, "html.parser")
    text = clean(soup.get_text(" "), 200000)
    if not has_four_rooms(text) or excluded(text):
        return None

    item_id_match = re.search(r"(\d{7,})", urllib.parse.urlparse(response.url).path)
    item_id = item_id_match.group(1) if item_id_match else hashlib.md5(url.encode()).hexdigest()[:12]
    data = first_json_ld(soup)

    title = meta(soup, "og:title", "twitter:title") or clean(str(data.get("name") or data.get("headline") or ""))
    image = meta(soup, "og:image", "twitter:image")
    if not image:
        image_data = data.get("image", "")
        if isinstance(image_data, list):
            image = str(image_data[0]) if image_data else ""
        elif isinstance(image_data, dict):
            image = str(image_data.get("url", ""))
        else:
            image = str(image_data or "")

    district_match = re.search(r"(桃園區|中壢區|平鎮區|八德區)", text)
    address_match = re.search(r"地址[:：]?\s*((?:桃園區|中壢區|平鎮區|八德區)[^\n]{1,60})", text)
    layout_match = re.search(r"(\d+房\d*廳?\d*衛?)", text)
    size_match = re.search(r"(\d+(?:\.\d+)?坪)", text)
    floor_match = re.search(r"((?:B?\d+(?:~|～|-)\d+F|整棟|\d+F)\s*/\s*\d+F)", text, re.I)
    building_match = re.search(r"(電梯大樓|電梯華廈|華廈|公寓|透天厝|別墅|樓中樓)", text)
    rent_match = re.search(r"([\d,]+)\s*元/月", text)
    publisher_match = re.search(r"((?:屋主|仲介)[:：]?\s*[^0-9\n]{1,30})", text)
    updated_match = re.search(r"此房屋在[^()]{0,40}\(([^)]*更新)\)", text)
    lease_match = re.search(r"最短租期\s*([^，。]{1,20})", text)

    equipment = []
    for name in ("冰箱", "洗衣機", "電視", "冷氣", "熱水器", "床", "衣櫃", "第四台",
                 "網路", "天然瓦斯", "沙發", "桌椅", "陽台", "電梯", "車位"):
        if name in text:
            equipment.append(name)

    item = Listing(
        source="591",
        source_id=item_id,
        url=normalize_item_url(response.url),
        title=clean(title, 160),
        district=district_match.group(1) if district_match else "",
        address=clean(address_match.group(1), 90) if address_match else "",
        house_type="整層住家",
        building_type=building_match.group(1) if building_match else "",
        floor=floor_match.group(1).replace(" ", "") if floor_match else "",
        layout=layout_match.group(1) if layout_match else "",
        size=size_match.group(1) if size_match else "",
        equipment="、".join(equipment),
        rent=money(rent_match.group(1)) if rent_match else 0,
        min_lease=lease_match.group(1) if lease_match else "",
        updated=updated_match.group(1) if updated_match else "",
        publisher=publisher_match.group(1) if publisher_match else "",
        image=image,
        summary=meta(soup, "og:description", "description"),
        raw_text=text,
        validated_at=NOW.isoformat(),
    )
    if not item.title or not item.rent or not allowed_district(item):
        return None
    if any(marker in text for marker in OWNER_MARKERS):
        item.category_hint = "owner"
    item.fingerprint = fingerprint(item)
    return item


def rakuya_urls(base: dict[str, str]) -> Iterable[str]:
    for page in range(1, 101):
        params = dict(base)
        params["page"] = str(page)
        yield "https://rent.rakuya.com.tw/result?" + urllib.parse.urlencode(params, safe=",")


def extract_rakuya_links(raw: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(raw, "html.parser")
    result: list[str] = []
    for anchor in soup.select("a[href]"):
        href = urllib.parse.urljoin(base_url, anchor.get("href", ""))
        parsed = urllib.parse.urlparse(href)
        if (
            parsed.hostname in {"rent.rakuya.com.tw", "community.rakuya.com.tw"}
            and (
                re.search(r"/item/[0-9a-f]+", parsed.path)
                or re.search(r"/\d+/rent/[0-9a-f]+", parsed.path)
            )
        ):
            result.append(normalize_item_url(href))
    return list(dict.fromkeys(result))


def crawl_rakuya_links() -> dict[str, set[str]]:
    zipcodes = ",".join(DISTRICTS_RAKUYA.values())
    categories: dict[str, set[str]] = {
        "general": set(),
        "owner": set(),
        "friendly": set(),
    }

    query_sets: list[tuple[str, dict[str, str]]] = []
    for room in ("4", "5"):
        query_sets.append(("general", {"zipcode": zipcodes, "room": room}))
        # 樂屋網「房屋來源：屋主」目前使用 usecode=7
        query_sets.append(("owner", {"zipcode": zipcodes, "room": room, "usecode": "7"}))
        for keyword in ("可入籍", "租補", "可養寵物", "寵物友善", "高齡友善"):
            query_sets.append(
                ("friendly", {"zipcode": zipcodes, "room": room, "keyword": keyword})
            )

    for category, params in query_sets:
        no_new = 0
        for page_url in rakuya_urls(params):
            response, raw = fetch(page_url)
            if response is None:
                break
            links = extract_rakuya_links(raw, response.url)
            new_links = set(links) - categories[category]
            categories[category].update(new_links)
            print(f"[Rakuya] {category} {page_url} new={len(new_links)}")
            if not new_links:
                no_new += 1
                if no_new >= 2:
                    break
            else:
                no_new = 0
            if "符合條件的房屋已瀏覽完畢" in raw:
                break
    return categories


def parse_rakuya_detail(url: str, hints: set[str]) -> Listing | None:
    response, raw = fetch(url)
    if is_dead_page(response, raw, "rakuya.com.tw"):
        return None
    soup = BeautifulSoup(raw, "html.parser")
    text = clean(soup.get_text(" "), 200000)
    if not has_four_rooms(text) or excluded(text):
        return None

    path = urllib.parse.urlparse(response.url).path
    item_id_match = re.search(r"(?:/item/|/rent/)([0-9a-f]+)", path)
    item_id = item_id_match.group(1) if item_id_match else hashlib.md5(url.encode()).hexdigest()[:12]

    title = meta(soup, "og:title", "twitter:title")
    image = meta(soup, "og:image", "twitter:image")
    description = meta(soup, "og:description", "description")

    district_match = re.search(r"(桃園區|中壢區|平鎮區)", text)
    layout_match = re.search(r"(\d+房\d*廳?\d*衛?)", text)
    size_match = re.search(r"(?:主建|室內|建坪)?\s*(\d+(?:\.\d+)?坪)", text)
    floor_match = re.search(r"((?:B?\d+(?:~|～|-)\d+|整棟|\d+)\s*/\s*\d+樓)", text, re.I)
    type_match = re.search(r"(電梯大廈|電梯華廈|華廈|公寓|透天厝|別墅|樓中樓)", text)
    rent_values = [money(v) for v in re.findall(r"([\d,]+)\s*元", text)]
    rent_values = [v for v in rent_values if 3000 <= v <= 1_000_000]
    current_rent = rent_values[-1] if rent_values else 0
    old_rent = 0
    if len(rent_values) >= 2 and rent_values[-2] > current_rent:
        old_rent = rent_values[-2]

    address = ""
    if district_match:
        # 樂屋網詳情頁通常在區名後顯示路街或社區名
        address_match = re.search(
            rf"{district_match.group(1)}\s+([^。|]{2,50}(?:路|街|巷|社區|大廈|別墅))",
            text,
        )
        if address_match:
            address = f"{district_match.group(1)}{clean(address_match.group(1), 60)}"

    updated_match = re.search(r"((?:\d+分鐘|\d+小時|\d+天|\d+個月)前更新)", text)
    views_match = re.search(r"(?:瀏覽數[:：]?\s*)?(\d+次瀏覽|新上架)", text)

    features = []
    for name in ("附傢俱", "附設備", "可開伙", "可養寵物", "可入籍", "租補", "租金補助"):
        if name in text:
            features.append(name)

    item = Listing(
        source="樂屋網",
        source_id=item_id,
        url=normalize_item_url(response.url),
        title=clean(title or soup.title.get_text(" ") if soup.title else "", 160),
        district=district_match.group(1) if district_match else "",
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
    if not item.title or not item.rent or not allowed_district(item):
        return None
    if "owner" in hints or any(marker in text for marker in OWNER_MARKERS):
        item.category_hint = "owner"
    elif "friendly" in hints or any(marker in text for marker in FRIENDLY_MARKERS):
        item.category_hint = "friendly"
    if item.old_rent > item.rent:
        item.category_hint = "discount"
    item.fingerprint = fingerprint(item)
    return item


def load_facebook_import() -> list[Listing]:
    """
    安全替代方案：
    使用者在自己的瀏覽器登入 FB，人工將可見貼文的永久網址與必要欄位
    匯出到 data/facebook_posts.json。程式不接觸帳號、密碼或 Cookie。
    """
    if not FB_IMPORT.exists():
        return []
    try:
        rows = json.loads(FB_IMPORT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    result: list[Listing] = []
    for row in rows if isinstance(rows, list) else []:
        text = clean(" ".join(str(row.get(k, "")) for k in row), 5000)
        if not has_four_rooms(text) or excluded(text):
            continue
        if not any(marker in text for marker in ("屋主自租", "仲介勿擾", "社宅勿擾")):
            continue
        url = str(row.get("url", "")).strip()
        if not re.match(r"https://www\.facebook\.com/groups/[^/]+/(?:posts|permalink)/", url):
            continue
        item = Listing(
            source="FB",
            source_id=hashlib.md5(url.encode()).hexdigest()[:16],
            url=url,
            title=clean(str(row.get("title", "")), 160),
            district=clean(str(row.get("district", "")), 20),
            address=clean(str(row.get("address", "")), 90),
            house_type=clean(str(row.get("house_type", "")), 30),
            building_type=clean(str(row.get("building_type", "")), 30),
            floor=clean(str(row.get("floor", "")), 30),
            layout=clean(str(row.get("layout", "")), 30),
            equipment=clean(str(row.get("equipment", "")), 200),
            rent=money(str(row.get("rent", ""))),
            min_lease=clean(str(row.get("min_lease", "")), 30),
            image=str(row.get("image", "")).strip(),
            summary=clean(str(row.get("summary", "")), 300),
            category="priority",
            validated_at=NOW.isoformat(),
            raw_text=text,
        )
        item.fingerprint = fingerprint(item)
        if item.title and item.rent and item.image:
            result.append(item)
    return result


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sent": [], "prices": {}}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_categories(items: list[Listing], state: dict) -> list[Listing]:
    prices = state.setdefault("prices", {})
    # 類別互斥優先順序：屋主 > 降價 > 友善 > 一般
    for item in items:
        previous = int(prices.get(f"{item.source}:{item.source_id}", 0) or 0)
        if item.category_hint == "owner":
            item.category = "owner"
        elif item.old_rent > item.rent or (previous and item.rent < previous):
            item.category = "discount"
            if not item.old_rent:
                item.old_rent = previous
        elif item.category_hint == "friendly":
            item.category = "friendly"
        else:
            item.category = "general"
        prices[f"{item.source}:{item.source_id}"] = item.rent
    return items


def filter_recent_duplicates(items: list[Listing], state: dict) -> tuple[list[Listing], int]:
    cutoff = NOW - timedelta(hours=48)
    sent = []
    recent_keys: set[str] = set()
    for row in state.get("sent", []):
        try:
            at = datetime.fromisoformat(row["sent_at"])
        except (KeyError, ValueError, TypeError):
            continue
        if at >= cutoff:
            sent.append(row)
            recent_keys.add(row.get("source_key", ""))
            recent_keys.add(row.get("fingerprint", ""))

    output: list[Listing] = []
    removed = 0
    for item in items:
        source_key = f"{item.source}:{item.source_id}"
        # 降價屬重大更新，可再次顯示；其他物件近48小時不重複
        if item.category != "discount" and (
            source_key in recent_keys or item.fingerprint in recent_keys
        ):
            removed += 1
            continue
        output.append(item)
        sent.append(
            {
                "source_key": source_key,
                "fingerprint": item.fingerprint,
                "sent_at": NOW.isoformat(),
                "title": item.title,
                "url": item.url,
            }
        )
    state["sent"] = sent[-5000:]
    return output, removed


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def render_card(item: Listing) -> str:
    old = (
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
        f"<div><span>{esc(k)}</span><b>{esc(v or '—')}</b></div>" for k, v in details
    )
    activity = "・".join(v for v in (item.updated, item.views) if v)
    image = item.image or (
        "data:image/svg+xml,"
        + urllib.parse.quote(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="650">'
            '<rect width="100%" height="100%" fill="#4b5563"/>'
            '<text x="50%" y="50%" fill="white" text-anchor="middle" '
            'font-family="sans-serif" font-size="36">點擊查看來源照片</text></svg>'
        )
    )
    return f"""
    <article class="card">
      <a class="photo" href="{esc(item.url)}" target="_blank" rel="noopener noreferrer">
        <img src="{esc(image)}" alt="{esc(item.title)}" referrerpolicy="no-referrer">
        <span>{esc(item.source)}｜{esc(item.category)}</span>
      </a>
      <div class="body">
        <small>{esc(item.district)}</small>
        <h3><a href="{esc(item.url)}" target="_blank" rel="noopener noreferrer">{esc(item.title)}</a></h3>
        <p class="summary">{esc(item.layout)}・{esc(item.size)}・{esc(item.floor)}</p>
        <div class="details">{detail_html}</div>
        {old}
        <div class="rent">{item.rent:,} 元／月</div>
        <div class="activity">{esc(activity)}</div>
        <a class="button" href="{esc(item.url)}" target="_blank" rel="noopener noreferrer">物件直達連結 ↗</a>
      </div>
    </article>
    """


def render_html(items: list[Listing], stats: dict) -> str:
    sections = [
        ("591", "owner", "屋主直租"),
        ("591", "discount", "降價物件"),
        ("591", "general", "全部符合條件物件"),
        ("樂屋網", "general", "出租"),
        ("樂屋網", "owner", "屋主"),
        ("樂屋網", "friendly", "友善房源"),
        ("樂屋網", "discount", "最新降價"),
        ("FB", "priority", "屋主自租／仲介勿擾／社宅勿擾"),
    ]
    blocks = []
    for source, category, title in sections:
        values = [x for x in items if x.source == source and x.category == category]
        cards = "".join(render_card(x) for x in values)
        if not cards:
            cards = '<div class="empty">本次沒有通過單筆頁面驗證且符合條件的物件。</div>'
        blocks.append(
            f'<section><header><h2>{esc(title)}</h2><b>{len(values)} 筆</b></header>'
            f'<div class="cards">{cards}</div></section>'
        )
    fb_links = "".join(
        f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">FB社團 ↗</a>'
        for url in FB_GROUPS
    )
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>桃園四房以上租屋快報</title>
<style>
:root{{--orange:#f46b18;--bg:#f4f5f7;--line:#e1e4e8;--muted:#68717d}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",sans-serif;color:#202124}}
.wrap{{width:min(1220px,calc(100% - 28px));margin:auto}}body>header{{background:#fff7ee;border-bottom:4px solid var(--orange);padding:30px 0}}
h1{{font-size:clamp(32px,5vw,50px);margin:0 0 10px}}main{{padding:22px 0 48px}}
.notice{{background:#fff;border-left:5px solid var(--orange);padding:15px 18px;border-radius:10px;line-height:1.7}}
section{{margin-top:18px;background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px}}
section>header{{display:flex;justify-content:space-between;align-items:end;gap:12px}}section h2{{margin:0;font-size:26px}}
.cards{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px;margin-top:14px}}
.card{{border:1px solid var(--line);border-radius:11px;overflow:hidden}}.photo{{height:275px;display:block;position:relative;background:#596273}}
.photo img{{width:100%;height:100%;object-fit:cover}}.photo span{{position:absolute;left:12px;top:12px;background:#000b;color:#fff;padding:7px 9px;border-radius:6px;font-weight:800}}
.body{{padding:16px}}small{{color:var(--orange);font-weight:900}}h3{{font-size:21px;line-height:1.4;margin:7px 0}}h3 a{{color:inherit;text-decoration:none}}
.summary{{font-weight:800}}.details{{display:grid;gap:7px;border-top:1px solid #eee;padding-top:11px}}
.details div{{display:grid;grid-template-columns:100px 1fr;gap:8px;font-size:14px}}.details span{{color:var(--muted)}}
.old{{margin-top:10px;color:#8a9098}}.rent{{font-size:28px;color:#d95700;font-weight:950;margin-top:8px}}.activity{{color:var(--muted);font-size:14px}}
.button{{display:block;text-align:center;margin-top:12px;padding:11px;background:var(--orange);color:#fff;text-decoration:none;border-radius:7px;font-weight:900}}
.empty{{border:1px dashed #bbb;border-radius:8px;padding:25px;text-align:center;color:var(--muted);grid-column:1/-1}}
.fb-links{{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}}.fb-links a{{background:#1877f2;color:#fff;text-decoration:none;padding:8px 10px;border-radius:6px}}
@media(max-width:850px){{.cards{{grid-template-columns:1fr}}}}@media(max-width:560px){{.photo{{height:230px}}}}
</style></head><body>
<header><div class="wrap"><h1>桃園四房以上租屋快報</h1>
<p>591、樂屋網完整分頁抓取；每筆均已重新開啟單筆頁驗證。</p></div></header>
<main class="wrap"><div class="notice">
產生時間：{NOW.strftime("%Y/%m/%d %H:%M")}｜候選 {stats['candidates']} 筆｜
無效或不符條件排除 {stats['invalid']} 筆｜近48小時重複排除 {stats['duplicates']} 筆｜
本次顯示 {len(items)} 筆。
</div>
{''.join(blocks)}
<section><header><h2>FB社團</h2><b>安全模式</b></header>
<p>不使用帳號、密碼或Cookie。可將你在已登入瀏覽器中人工確認的貼文，
依 data/facebook_posts.example.json 格式匯入。</p><div class="fb-links">{fb_links}</div></section>
</main></body></html>"""


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    candidates: list[Listing] = []

    links_591 = crawl_591_listing_links()
    for index, url in enumerate(links_591, 1):
        print(f"[591 detail] {index}/{len(links_591)} {url}")
        item = parse_591_detail(url)
        if item:
            candidates.append(item)

    rakuya_map = crawl_rakuya_links()
    link_hints: dict[str, set[str]] = {}
    for hint, links in rakuya_map.items():
        for url in links:
            link_hints.setdefault(url, set()).add(hint)
    for index, (url, hints) in enumerate(link_hints.items(), 1):
        print(f"[Rakuya detail] {index}/{len(link_hints)} {url}")
        item = parse_rakuya_detail(url, hints)
        if item:
            candidates.append(item)

    candidates.extend(load_facebook_import())

    # 來源編號先去重
    unique: dict[str, Listing] = {}
    for item in candidates:
        unique[f"{item.source}:{item.source_id}"] = item
    candidates = list(unique.values())

    state = load_state()
    candidates = apply_categories(candidates, state)
    published, duplicate_count = filter_recent_duplicates(candidates, state)

    stats = {
        "candidates": len(links_591) + len(link_hints),
        "invalid": max(0, len(links_591) + len(link_hints) - len(candidates)),
        "duplicates": duplicate_count,
        "published": len(published),
    }
    OUTPUT_JSON.write_text(
        json.dumps(
            {"generated_at": NOW.isoformat(), "stats": stats, "items": [asdict(x) for x in published]},
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
