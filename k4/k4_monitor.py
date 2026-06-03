import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import feishu
from k4_sort import sort_campaign

FINAL_REPORT_DELAY = 30 * 60  # 活动结束后等 30 分钟再发结果


SITES = {
    "cn.ktown4u.com": {"code": "cn", "locale": "zh-CN"},
    "www.ktown4u.com": {"code": "gl", "locale": "en-US"},
    "kr.ktown4u.com": {"code": "kr", "locale": "ko-KR"},
    "jp.ktown4u.com": {"code": "jp", "locale": "ja-JP"},
}

USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X)"
BASE_COOKIE = (
    "_ga=GA1.1.xxx; "
    "org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE={locale}; "
    "accessToken=your_token;"
)


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8-sig") as f:
        config = json.load(f)

    required = ["campaign_id", "poll_interval_seconds", "events"]
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"config missing keys: {', '.join(missing)}")
    return config


def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def build_cookie(locale: str) -> str:
    return BASE_COOKIE.format(locale=locale[:2])


def parse_events(config: dict) -> list[dict]:
    jobs = []
    for event_name, urls in config["events"].items():
        for url in urls:
            parsed = urlparse(url)
            site_conf = SITES.get(parsed.netloc)
            if not site_conf:
                raise ValueError(f"unsupported domain in url: {url}")

            query = parse_qs(parsed.query)
            try:
                goods_no = query["goods_no"][0]
                eve_no = query["eve_no"][0]
                grp_no = query["grp_no"][0]
            except (KeyError, IndexError) as exc:
                raise ValueError(f"URL missing required parameter ({exc}): {url}") from exc
            jobs.append(
                {
                    "event": event_name,
                    "site": site_conf["code"],
                    "domain": parsed.netloc,
                    "locale": site_conf["locale"],
                    "goods_no": goods_no,
                    "eve_no": eve_no,
                    "grp_no": grp_no,
                }
            )
    return jobs


def fetch(session: requests.Session, job: dict) -> tuple[str | None, int | str]:
    params = {
        "goods_no": job["goods_no"],
        "eve_no": job["eve_no"],
        "grp_no": job["grp_no"],
        "t": int(time.time() * 1000),
    }
    url = f"https://{job['domain']}/iteminfo"

    try:
        response = session.get(
            url,
            params=params,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": job["locale"],
                "Cookie": build_cookie(job["locale"]),
            },
            timeout=(10, 30),
        )
        response.raise_for_status()

        match = re.search(r'__NEXT_DATA__" type="application/json">(.*?)</script>', response.text, re.S)
        if not match:
            raise ValueError("NEXT_DATA payload not found")

        data = json.loads(match.group(1))
        details = data["props"]["pageProps"]["productDetails"]
        name = details.get("productName")
        qty = details.get("quantity")
        sales = -qty if isinstance(qty, int) and qty < 0 else qty
        if not isinstance(sales, int):
            raise ValueError(f"invalid quantity: {qty!r}")
        return name, sales
    except Exception as exc:
        return None, f"ERR: {exc}"


def write_heartbeat(path: Path, config: dict) -> None:
    payload = {
        "campaign_id": config["campaign_id"],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_datetime(raw: str) -> datetime | None:
    """解析时间字符串，支持 ISO 格式（含时区）和纯字符串两种形式。无时区时补 Asia/Shanghai。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return dt


def parse_stop_time(config: dict) -> datetime | None:
    return _parse_datetime(config.get("stop_monitor_at", ""))


def parse_activity_end_time(config: dict) -> datetime | None:
    """活动实际截止时间（用于间隔计算），无则回退到 stop_monitor_at。"""
    return _parse_datetime(config.get("activity_end_at", "")) or parse_stop_time(config)


def get_dynamic_interval(activity_end_at: datetime | None, default_interval: int) -> int:
    """根据距活动截止的剩余时间动态计算查询间隔。
    活动截止后进入退单监控期，固定 10s。
    """
    if activity_end_at is None:
        return default_interval
    now = datetime.now(activity_end_at.tzinfo)
    if now >= activity_end_at:
        return 10  # 退单监控期
    remaining = (activity_end_at - now).total_seconds()
    if remaining <= 20 * 60:  return 2
    if remaining <= 60 * 60:  return 5
    if remaining <= 2 * 3600: return 10
    return default_interval


def should_stop(stop_at: datetime | None) -> bool:
    if stop_at is None:
        return False
    return datetime.now(stop_at.tzinfo) >= stop_at


def sleep_until_next_cycle(interval: int, stop_at: datetime | None) -> bool:
    if stop_at is None:
        time.sleep(interval)
        return True

    remaining = (stop_at - datetime.now(stop_at.tzinfo)).total_seconds()
    if remaining <= 0:
        return False

    time.sleep(min(interval, remaining))
    return not should_stop(stop_at)


def safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        fallback = message.encode("ascii", errors="backslashreplace").decode("ascii")
        sys.stdout.write(fallback + "\n")
        sys.stdout.flush()


HEARTBEAT_LOG_INTERVAL = 5 * 60  # 每5分钟写一条心跳日志


def monitor_loop(config: dict, run_once: bool) -> bool:
    """返回 True 表示自然到期退出，False 表示 --once 单次退出。"""
    session = create_session()
    jobs = parse_events(config)
    campaign_dir = Path(config.get("campaign_dir", "."))
    logs_dir = campaign_dir / config.get("logs_dir", "logs")
    runtime_dir = campaign_dir / config.get("runtime_dir", "runtime")
    stop_at = parse_stop_time(config)
    activity_end_at = parse_activity_end_time(config)
    logs_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    heartbeat_path = runtime_dir / f"{config['campaign_id']}_heartbeat.json"
    heartbeat_log_path = logs_dir / "heartbeat.log"
    last_sales: dict[str, int] = {}
    last_heartbeat_log = datetime.now()

    safe_print(f"KTOWN4U monitor started for {config['campaign_id']}")
    if activity_end_at is not None:
        safe_print(f"Activity end time: {activity_end_at.isoformat()}")
    if stop_at is not None:
        safe_print(f"Monitor stop time: {stop_at.isoformat()}")

    while True:
        if should_stop(stop_at):
            safe_print(f"Stop time reached for {config['campaign_id']}, exiting monitor loop.")
            write_heartbeat(heartbeat_path, config)
            return True

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        success_count = 0
        fail_count = 0

        with ThreadPoolExecutor(max_workers=min(len(jobs), 8) or 1) as pool:
            futures = [pool.submit(fetch, session, job) for job in jobs]

            for job, future in zip(jobs, futures):
                name, sales = future.result()
                state_key = f"{job['event']}_{job['site']}_{job['goods_no']}"

                if isinstance(sales, int):
                    success_count += 1
                    old = last_sales.get(state_key)
                    if old is None or sales != old:
                        diff = "" if old is None else f" Δ{sales - old:+}"
                        msg = (
                            f"[{ts}] [{job['event']}/{job['site']}/{job['goods_no']}] "
                            f"{name} → {sales}{diff}"
                        )
                        safe_print(msg)
                        log_path = logs_dir / f"{job['site']}_{job['event']}.log"
                        with log_path.open("a", encoding="utf-8") as f:
                            f.write(msg + "\n")
                    last_sales[state_key] = sales
                else:
                    fail_count += 1
                    safe_print(f"[{ts}] [{job['event']}/{job['site']}/{job['goods_no']}] {sales}")

        write_heartbeat(heartbeat_path, config)

        # 心跳日志：每5分钟写一条，确认程序存活且记录成功/失败数
        now = datetime.now()
        if (now - last_heartbeat_log).total_seconds() >= HEARTBEAT_LOG_INTERVAL:
            hb_msg = f"[{ts}] 💓 心跳 | 成功 {success_count}/{len(jobs)} | 失败 {fail_count}/{len(jobs)}"
            with heartbeat_log_path.open("a", encoding="utf-8") as f:
                f.write(hb_msg + "\n")
            safe_print(hb_msg)
            last_heartbeat_log = now

        if run_once:
            return False

        interval = get_dynamic_interval(activity_end_at, config["poll_interval_seconds"])
        if not sleep_until_next_cycle(interval, stop_at):
            safe_print(f"Stop time reached for {config['campaign_id']}, exiting monitor loop.")
            write_heartbeat(heartbeat_path, config)
            return True


def send_final_report(config: dict, config_path: Path) -> None:
    """活动到期后等 30 分钟，发送最终排序报告。"""
    campaign_id = config["campaign_id"]
    name = config.get("name") or campaign_id
    safe_print(f"Campaign {campaign_id} ended. Waiting {FINAL_REPORT_DELAY // 60} min before final report...")
    time.sleep(FINAL_REPORT_DELAY)
    safe_print(f"Sending final report for {campaign_id}...")
    try:
        report = sort_campaign(config_path)
        feishu.notify(f"📊 K4 活动结束 — {name}\n\n{report}")
    except Exception as exc:
        safe_print(f"Final report failed: {exc}")
        feishu.notify(f"⚠️ K4 活动 {name} 已结束，但生成报告时出错：{exc}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to campaign JSON config")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    try:
        expired = monitor_loop(config, run_once=args.once)
    except Exception as exc:
        import traceback
        name = config.get("name") or config.get("campaign_id", "unknown")
        feishu.notify(
            f"🚨 K4 监控进程异常崩溃\n"
            f"活动: {name}\n\n"
            f"{traceback.format_exc()[-600:]}"
        )
        raise
    if expired:
        send_final_report(config, config_path)


if __name__ == "__main__":
    main()
