#!/usr/bin/env python3
"""
imini.tv 应募活动自动扫描
- 每次运行只处理新出现的 pro_id，已知的全部跳过
- 只保留：签名会 / 视频签售 / 手渡 / Photo Event
- 结果按类型写入 events/<类型>/<pro_id>.json
- 运行日志写入 scan.log

用法：
  python3 imini_scan.py          # 正常扫描
  python3 imini_scan.py --init   # 首次初始化（把当前所有 ID 标为已知，不处理）
"""

import argparse
import json
import re
import ssl
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
EVENTS_DIR = BASE_DIR / "events"
STATE_FILE = BASE_DIR / "known_ids.json"
LOG_FILE    = BASE_DIR / "scan.log"

CATEGORY_DIRS = {
    "线下签名会":     EVENTS_DIR / "线下签名会",
    "视频通话签名会": EVENTS_DIR / "视频通话签名会",
    "手渡咖啡":       EVENTS_DIR / "手渡咖啡",
    "photo_event":    EVENTS_DIR / "photo_event",
}

# 商品汇总页（含所有 pro_id）+ 备用：店铺首页
PRODUCT_LIST_URLS = [
    "https://www.imini.tv/xingjunwenhua/wap_pro/27864757_7964264_0_0.html",
    "https://www.imini.tv/wap_xingjunwenhua.html",
]
DETAIL_API = "https://www.imini.tv/mallorder/api/detail.php"

CTX = ssl._create_unverified_context()
UA  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
# ──────────────────────────────────────────────────────────────────────────────


def gbk(s):
    """API 返回的字符串是 latin1 编码的 GBK，需要二次解码"""
    if isinstance(s, str):
        try:
            return s.encode("latin1").decode("gbk")
        except Exception:
            return s
    return str(s)


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── 状态持久化 ────────────────────────────────────────────────────────────────

def load_known_ids() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return set()


def save_known_ids(ids: set):
    STATE_FILE.write_text(
        json.dumps(sorted(ids), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


# ── 网络请求 ──────────────────────────────────────────────────────────────────

def fetch_all_pro_ids() -> set:
    """从多个页面收集所有 pro_id，取并集"""
    ids = set()
    for url in PRODUCT_LIST_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15, context=CTX) as resp:
                html = resp.read().decode("gbk", errors="ignore")
            found = re.findall(r"wap_pro/(\d{8,})", html)
            ids.update(found)
        except Exception as e:
            log(f"警告：获取列表页失败 {url} — {e}")
    return ids


def fetch_detail(pro_id: str) -> dict:
    """调 detail API，返回活动信息字典"""
    data = f"pro_id={pro_id}".encode()
    req = urllib.request.Request(
        DETAIL_API, data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": UA,
            "X-Requested-With": "XMLHttpRequest",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10, context=CTX) as resp:
        d = json.loads(resp.read()).get("data", {})

    name  = gbk(d.get("pro_name", ""))
    price = gbk(str(d.get("pro_price", "")))
    stock = d.get("pro_stock", 0)

    # 截止时间来自 active_module（限时购模块）
    start_time = end_time = ""
    for mod in (d.get("active_module") or []):
        info = mod.get("popup_info") or mod.get("info") or {}
        if isinstance(info, dict) and info.get("end_time"):
            start_time = info.get("start_time", "")
            end_time   = info.get("end_time", "")

    is_ended = (price == "已结束")
    return {
        "pro_id":     pro_id,
        "name":       name,
        "status":     "已结束" if is_ended else "进行中",
        "price":      "" if is_ended else price,
        "stock":      stock,
        "start_time": start_time,
        "end_time":   end_time,          # 空字符串表示截止时间藏在图片里
        "url":        f"https://www.imini.tv/xingjunwenhua/wap_pro/{pro_id}.html",
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── 分类逻辑 ──────────────────────────────────────────────────────────────────

def classify(name: str) -> str | None:
    """
    返回类别名称，不属于目标类型则返回 None。
    顺序：视频通话签名会 > 手渡咖啡 > photo_event > 线下签名会
    （避免"视频通话签名会"被误归到线下签名会）
    """
    n = name.lower()
    if "视频通话" in name or "视频签售" in name:
        return "视频通话签名会"
    if "手渡" in name:
        return "手渡咖啡"
    if "photo event" in n:
        return "photo_event"
    if "签名会" in name:
        return "线下签名会"
    return None


# ── 保存结果 ──────────────────────────────────────────────────────────────────

def save_event(event: dict, category: str):
    folder = CATEGORY_DIRS[category]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{event['pro_id']}.json"
    path.write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ── 主流程 ────────────────────────────────────────────────────────────────────

def init_mode():
    """首次初始化：把当前所有 ID 标为已知，不写入 events 目录"""
    log("=== 初始化模式：标记当前所有 ID 为已知 ===")
    all_ids = fetch_all_pro_ids()
    if not all_ids:
        log("未获取到任何 ID，初始化失败")
        return
    save_known_ids(all_ids)
    log(f"已将 {len(all_ids)} 个 ID 标记为已知，后续扫描只处理新增活动")


def scan():
    """正常扫描：只处理新 ID"""
    log("=== 开始扫描 ===")

    # 确保目录存在
    for d in CATEGORY_DIRS.values():
        d.mkdir(parents=True, exist_ok=True)

    known_ids = load_known_ids()
    log(f"已知 ID 数: {len(known_ids)}")

    try:
        all_ids = fetch_all_pro_ids()
    except Exception as e:
        log(f"获取商品列表失败: {e}")
        return []

    log(f"页面共发现 {len(all_ids)} 个 pro_id")
    new_ids = all_ids - known_ids
    log(f"新 ID 数: {len(new_ids)}")

    if not new_ids:
        log("无新活动")
        log("=== 扫描完成 ===")
        return []

    found = []

    for pro_id in sorted(new_ids, reverse=True):  # 新 ID 通常更大，优先处理
        try:
            event = fetch_detail(pro_id)
        except Exception as e:
            log(f"[{pro_id}] 获取详情失败: {e}")
            time.sleep(0.3)
            continue

        category = classify(event["name"])

        if category:
            path = save_event(event, category)
            found.append(event)
            deadline = event["end_time"] or "待确认（见活动图片）"
            log(
                f"[新] [{category}] {event['name']}\n"
                f"      pro_id={pro_id} | 状态={event['status']} | "
                f"价格={event['price']}元 | 截止={deadline}"
            )
        else:
            log(f"[跳过] {event['name']} ({pro_id})")

        known_ids.add(pro_id)
        time.sleep(0.3)

    save_known_ids(known_ids)
    log(f"=== 扫描完成，本次发现 {len(found)} 个符合条件的新活动 ===\n")
    return found


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--init",
        action="store_true",
        help="首次初始化：把当前所有 ID 标为已知，不写入 events 目录",
    )
    args = parser.parse_args()

    if args.init:
        init_mode()
    else:
        scan()
