#!/usr/bin/env python3
"""
通用库存监控子进程
用法: python3 imini_monitor.py --pro_id <id1,id2,...> --folder <路径> \
        [--activity_end "YYYY-MM-DD HH:MM:SS"]  活动实际截止，用于间隔计算
        [--end_time "YYYY-MM-DD HH:MM:SS"]      监控停止时间（活动截止+30min）
        [--sources <label1,label2,...>]

动态检查间隔（基于 activity_end）：
  正常           → 60s
  最后 2 小时    → 10s
  最后 1 小时    → 5s
  最后 20 分钟   → 2s
  活动截止后     → 10s（退单监控期，直到 end_time 停止）
"""

import argparse
import json
import logging
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import feishu
from imini_sort import sort_group_for_folder

DETAIL_API = "https://www.imini.tv/mallorder/api/detail.php"
CTX = ssl._create_unverified_context()


def get_interval(activity_end_str: str, end_time_str: str) -> int:
    """计算当前查询间隔。
    以 activity_end 为基准；活动截止后进入退单期，固定 10s。
    若未设置 activity_end，则回退到 end_time 计算（兼容旧数据）。
    """
    ref = activity_end_str or end_time_str
    if not ref:
        return 60
    try:
        now = datetime.now()
        ref_dt = datetime.strptime(ref, "%Y-%m-%d %H:%M:%S")
        # 活动已截止 → 退单监控期，降速到 10s
        if activity_end_str and now >= datetime.strptime(activity_end_str, "%Y-%m-%d %H:%M:%S"):
            return 10
        remaining = (ref_dt - now).total_seconds()
        if remaining <= 20 * 60:  return 2
        if remaining <= 60 * 60:  return 5
        if remaining <= 2 * 3600: return 10
        return 60
    except ValueError:
        return 60


def is_past_deadline(end_time_str: str) -> bool:
    if not end_time_str:
        return False
    try:
        return datetime.now() >= datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False


def get_stock(pro_id: str) -> int:
    data = f"pro_id={pro_id}".encode()
    req = urllib.request.Request(
        DETAIL_API, data=data,
        headers={
            "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10, context=CTX) as resp:
        return int(json.loads(resp.read())["data"]["pro_stock"])


def _on_deadline_reached(args, folder: Path):
    """截止时间到：发飞书通知 + 最终排序报告"""
    try:
        sort_result = sort_group_for_folder(folder)
        feishu.notify(
            f"⏹ 活动已截止，监控结束\n\n"
            f"活动: {folder.name}\n"
            f"截止: {args.end_time}"
        )
        feishu.notify(sort_result)
    except Exception as e:
        logging.error("发送截止通知失败: %s", e)


def fetch_one(pro_id: str, source: str) -> tuple:
    """并行查询单个 pro_id，返回 (pro_id, source, stock, error)"""
    try:
        stock = get_stock(pro_id)
        return pro_id, source, stock, None
    except Exception as e:
        return pro_id, source, None, e


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pro_id",       required=True, help="逗号分隔的 pro_id，支持多个")
    parser.add_argument("--folder",       required=True)
    parser.add_argument("--end_time",     default="", help="监控停止时间（活动截止后30分钟）")
    parser.add_argument("--activity_end", default="", help="活动实际截止时间，用于计算查询间隔")
    parser.add_argument("--sources",      default="", help="逗号分隔的来源标签，与 pro_id 一一对应")
    args = parser.parse_args()

    folder = Path(args.folder)
    folder.mkdir(parents=True, exist_ok=True)

    pro_ids = [p.strip() for p in args.pro_id.split(",") if p.strip()]
    if args.sources:
        sources = [s.strip() for s in args.sources.split(",")]
        while len(sources) < len(pro_ids):
            sources.append(pro_ids[len(sources)])
    else:
        sources = pro_ids[:]

    log_file = folder / f"{folder.name}.log"
    logging.basicConfig(
        filename=str(log_file),
        level=logging.INFO,
        format="%(asctime)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logging.info(
        "监控启动 | pro_ids=%s | 活动截止=%s | 监控停止=%s",
        args.pro_id,
        args.activity_end or "未设置",
        args.end_time or "未设置",
    )
    last_stocks: dict = {p: None for p in pro_ids}
    last_heartbeat = datetime.now()
    HEARTBEAT_INTERVAL = 5 * 60  # 每5分钟写一次心跳

    while True:
        if is_past_deadline(args.end_time):
            logging.info("已到监控停止时间，监控正常结束")
            _on_deadline_reached(args, folder)
            break

        # 并行查询所有 pro_id
        with ThreadPoolExecutor(max_workers=len(pro_ids)) as pool:
            results = [f.result() for f in [
                pool.submit(fetch_one, p, s) for p, s in zip(pro_ids, sources)
            ]]

        success_count = 0
        for pro_id, source, stock, error in results:
            if error is not None:
                logging.error("[%s] 请求失败: %s", source, error)
                continue
            success_count += 1
            last = last_stocks[pro_id]
            if last is None:
                logging.info("[%s] 初始库存: %d", source, stock)
            elif stock != last:
                diff = stock - last
                logging.info("[%s] 库存变化: %d -> %d (差值: %d)", source, last, stock, diff)
            last_stocks[pro_id] = stock

        # 心跳日志：每5分钟写一次，确认程序存活且请求成功
        now = datetime.now()
        if (now - last_heartbeat).total_seconds() >= HEARTBEAT_INTERVAL:
            stocks_str = " | ".join(
                f"[{s}] {last_stocks[p]}" if last_stocks[p] is not None else f"[{s}] N/A"
                for p, s in zip(pro_ids, sources)
            )
            logging.info("💓 心跳 | 成功请求 %d/%d | %s", success_count, len(pro_ids), stocks_str)
            last_heartbeat = now

        time.sleep(get_interval(args.activity_end, args.end_time))


if __name__ == "__main__":
    main()
