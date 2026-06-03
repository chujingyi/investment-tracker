#!/usr/bin/env python3
"""
Ktown4u event scanner.

The scanner reads each locale's /event page, fetches product groups through
Ktown4u's GraphQL eventProductsV2 API, then pairs the same campaign across
GL/CN/KR/JP by shared product numbers.
"""

import argparse
import json
import re
import time
from datetime import datetime
from html import unescape
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import CAMPAIGNS_DIR, EVENTS_DIR, SCAN_STATE_FILE


SITES = {
    "gl": {"base": "https://www.ktown4u.com", "shop_no": 164},
    "cn": {"base": "https://cn.ktown4u.com", "shop_no": 197},
    "kr": {"base": "https://kr.ktown4u.com", "shop_no": 174},
    "jp": {"base": "https://jp.ktown4u.com", "shop_no": 198},
}

EVENT_PRODUCTS_QUERY = """
query eventProductsV2($request: EventProductsRequest!) {
  eventProductsV2(request: $request) {
    groups {
      groupNo
      groupName
      comment
      displayOrder
      numberOfProducts
      products {
        canSell
        name
        sales
        shouldHideSales
        eventNo
        fanClubProductNo
        productNo
        groupNo
      }
    }
  }
}
"""

TARGET_KEYWORDS = (
    "sign",
    "offline",
    "off-line",
    "video call",
    "photo event",
    "签名",
    "签售",
    "线下",
    "视频",
    "拍照",
    "사인",
    "대면",
    "영통",
    "영상통화",
    "포토",
    "サイン",
    "対面",
    "テレビ電話",
    "写真",
)

EXCLUDE_KEYWORDS = (
    "sale festa",
    "special bonus",
    "special gift",
    "artist collection",
    "category zone",
    "official merch",
    "official",
    "pop up",
    " md",
    "merch",
    "mega benefits",
    "early summer",
    "grand open",
    "gwp",
    "倉庫",
    "warehouse",
)

# 产品名层面的排除词：周边商品，不是见面活动
EXCLUDE_PRODUCT_KEYWORDS = (
    "포토카드",      # 写真卡套组（非拍照活动）
    "photocard",
    "photo card",
    "トレカ",        # 日文写真卡
    "写真集",        # 写真集（影集）
    "写真本",
)


def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = create_session()


def load_known() -> set[str]:
    if not SCAN_STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(SCAN_STATE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_known(values: set[str]) -> None:
    SCAN_STATE_FILE.write_text(
        json.dumps(sorted(values), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def slugify(text: str, fallback: str = "campaign") -> str:
    text = re.sub(r"[^\w一-鿿가-힣ぁ-んァ-ン]+", "_", text).strip("_").lower()
    return text[:60] or fallback


def normalize_end_date(value: str) -> str:
    if not value:
        return ""
    value = value.replace(".0", "")
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def fetch_event_page(site: dict) -> list[dict]:
    response = SESSION.get(
        site["base"] + "/event",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        response.text,
        re.S,
    )
    if not match:
        raise ValueError("__NEXT_DATA__ not found")
    data = json.loads(unescape(match.group(1)))
    return data["props"]["pageProps"]["events"]


def fetch_products(event_no: int, shop_no: int) -> list[dict]:
    response = SESSION.post(
        "https://apis.ktown4u.com/vador/graphql",
        json={
            "query": EVENT_PRODUCTS_QUERY,
            "variables": {"request": {"eventNo": event_no, "shopNo": shop_no}},
        },
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    event_products = data.get("eventProductsV2") or {}
    groups = event_products.get("groups") or []

    products = []
    for group in groups:
        for product in group.get("products") or []:
            product_no = product.get("productNo")
            if not product_no:
                continue
            products.append(
                {
                    **product,
                    "groupName": group.get("groupName", ""),
                    "groupDisplayOrder": group.get("displayOrder", 0),
                }
            )
    return products


def is_target_text(*parts: str) -> bool:
    text = " ".join(part or "" for part in parts).lower()
    for keyword in TARGET_KEYWORDS:
        kw = keyword.lower()
        # "sign" 单独一个词时容易误匹配 signal/fansignal 等，
        # 改为词边界匹配（只对纯英文短词做此处理）
        if kw == "sign":
            if re.search(r"\bsign(ing|s|ed)?\b", text):
                return True
        else:
            if kw in text:
                return True
    return False


def is_excluded_event(event: dict) -> bool:
    text = f"{event.get('title', '')} {event.get('description', '')}".lower()
    return any(keyword in text for keyword in EXCLUDE_KEYWORDS)


def is_excluded_product(product: dict) -> bool:
    """排除周边商品类产品（포토카드 等）避免与拍照活动混淆"""
    text = f"{product.get('groupName', '')} {product.get('name', '')}".lower()
    return any(keyword in text for keyword in EXCLUDE_PRODUCT_KEYWORDS)


def classify_product(product: dict) -> str:
    text = f"{product.get('groupName', '')} {product.get('name', '')}".lower()
    if any(word in text for word in ("video call", "视频", "영통", "영상통화", "テレビ電話")):
        return "video_call"
    if any(word in text for word in ("photo event", "拍照", "포토", "写真")):
        return "photo_event"
    if (re.search(r"\bsign(ing|s|ed)?\b", text)
            or any(word in text for word in ("offline", "off-line", "签名", "签售", "线下", "사인", "대면", "サイン", "対面"))):
        return "signing"
    return slugify(product.get("groupName") or product.get("name") or "event")


def item_url(site: dict, product: dict) -> str:
    return (
        f"{site['base']}/iteminfo?"
        f"eve_no={product['eventNo']}&goods_no={product['productNo']}&grp_no={product['groupNo']}"
    )


def collect_site_candidates(code: str, site: dict) -> list[dict]:
    candidates = []
    for event in fetch_event_page(site):
        if is_excluded_event(event):
            continue
        try:
            products = fetch_products(event["eventNo"], site["shop_no"])
        except Exception as exc:
            print(f"[scan] fetch_products failed for event {event.get('eventNo')} ({code}): {exc}")
            continue
        target_products = [
            product
            for product in products
            if product.get("canSell")
            and is_target_text(
                event.get("title", ""),
                event.get("description", ""),
                product.get("groupName", ""),
                product.get("name", ""),
            )
            and not is_excluded_product(product)
        ]
        if not target_products:
            continue
        signature = ",".join(str(p["productNo"]) for p in sorted(target_products, key=lambda p: p["productNo"]))
        candidates.append(
            {
                "site": code,
                "event": event,
                "products": target_products,
                "signature": signature,
            }
        )
        time.sleep(0.05)
    return candidates


# 类别优先级：同一 productNo 跨站点分类冲突时，取最高优先级的类别
CLASSIFY_PRIORITY = {"signing": 3, "photo_event": 2, "video_call": 1}


def resolve_product_classifications(items: list[dict]) -> dict[int, str]:
    """同一 productNo 在多个站点可能因 groupName 不同而归入不同类别。
    例如：签售产品在 GL 的 groupName 是 'Off-Line Sign Event'（→signing），
    但在 CN/KR/JP 却混在视频通话 group 里（→video_call）。
    取所有站点中优先级最高的分类，确保签售不会被误归为视频通话。"""
    best: dict[int, str] = {}
    for item in items:
        for product in item["products"]:
            pno = product["productNo"]
            cat = classify_product(product)
            if CLASSIFY_PRIORITY.get(cat, 0) > CLASSIFY_PRIORITY.get(best.get(pno, ""), 0):
                best[pno] = cat
    return best


def choose_title(items: list[dict]) -> str:
    for preferred in ("gl", "cn", "jp", "kr"):
        for item in items:
            if item["site"] == preferred:
                event = item["event"]
                return event.get("title") or event.get("description") or str(event.get("eventNo"))
    event = items[0]["event"]
    return event.get("title") or event.get("description") or str(event.get("eventNo"))


def build_campaign(signature: str, items: list[dict]) -> dict:
    title = choose_title(items)
    end_dates = [item["event"].get("endDate", "") for item in items if item["event"].get("endDate")]
    end_date = sorted(end_dates)[0] if end_dates else ""
    first_date = (normalize_end_date(end_date) or datetime.now().strftime("%Y-%m-%d"))[:10].replace("-", "")
    campaign_id = f"{first_date}_{slugify(title)}_{signature.split(',')[0]}"

    # 跨站点统一分类：同一 productNo 取优先级最高的类别
    product_category = resolve_product_classifications(items)

    events: dict[str, list[str]] = {}
    exports: dict[str, str] = {}
    for item in sorted(items, key=lambda item: item["site"]):
        site = SITES[item["site"]]
        for product in sorted(item["products"], key=lambda product: (product["productNo"], product["groupNo"])):
            key = product_category.get(product["productNo"]) or classify_product(product)
            events.setdefault(key, []).append(item_url(site, product))
            exports.setdefault(key, f"{key}_rank.log")

    return {
        "campaign_id": campaign_id,
        "name": title,
        "poll_interval_seconds": 10,
        "stop_monitor_at": normalize_end_date(end_date),
        "events": events,
        "exports": exports,
        "source_signature": signature,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
    }


def scan(include_known: bool = False) -> list[dict]:
    known = load_known()
    grouped: dict[str, list[dict]] = {}
    for code, site in SITES.items():
        try:
            candidates = collect_site_candidates(code, site)
        except Exception as exc:
            print(f"[scan] collect_site_candidates failed for {code}: {exc}")
            continue
        for candidate in candidates:
            grouped.setdefault(candidate["signature"], []).append(candidate)

    campaigns = []
    for signature, items in sorted(grouped.items()):
        if not include_known and signature in known:
            continue
        campaigns.append(build_campaign(signature, items))
        known.add(signature)

    if not include_known:
        save_known(known)
    return campaigns


def init_mode() -> None:
    campaigns = scan(include_known=True)
    save_known({campaign["source_signature"] for campaign in campaigns})
    print(f"initialized {len(campaigns)} known K4 campaigns")


def write_campaigns(campaigns: list[dict]) -> None:
    CAMPAIGNS_DIR.mkdir(parents=True, exist_ok=True)
    for campaign in campaigns:
        folder = CAMPAIGNS_DIR / campaign["campaign_id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "campaign.json").write_text(
            json.dumps(campaign, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def save_scan_result(campaigns: list[dict]) -> Path:
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = EVENTS_DIR / f"k4_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(campaigns, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true", help="mark current campaigns as known")
    parser.add_argument("--all", action="store_true", help="include known campaigns in output")
    parser.add_argument("--write-campaigns", action="store_true", help="write campaigns/<campaign_id>/campaign.json")
    args = parser.parse_args()

    if args.init:
        init_mode()
    else:
        found = scan(include_known=args.all)
        output = save_scan_result(found)
        if args.write_campaigns:
            write_campaigns(found)
        print(json.dumps(found, ensure_ascii=False, indent=2))
        print(f"saved {len(found)} candidates to {output}")
