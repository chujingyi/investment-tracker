import argparse
import json
import random
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
            goods_no = query["goods_no"][0]
            jobs.append(
                {
                    "event": event_name,
                    "site": site_conf["code"],
                    "domain": parsed.netloc,
                    "locale": site_conf["locale"],
                    "goods_no": goods_no,
                    "eve_no": query["eve_no"][0],
                    "grp_no": query["grp_no"][0],
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


def parse_stop_time(config: dict) -> datetime | None:
    raw = config.get("stop_monitor_at")
    if not raw:
        return None
    stop_at = datetime.fromisoformat(raw)
    if stop_at.tzinfo is None:
        stop_at = stop_at.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return stop_at


def should_stop(stop_at: datetime | None) -> bool:
    if stop_at is None:
        return False
    return datetime.now(stop_at.tzinfo) >= stop_at


def sleep_until_next_cycle(poll_interval_seconds: int, stop_at: datetime | None) -> bool:
    if stop_at is None:
        time.sleep(poll_interval_seconds)
        return True

    remaining = (stop_at - datetime.now(stop_at.tzinfo)).total_seconds()
    if remaining <= 0:
        return False

    time.sleep(min(poll_interval_seconds, remaining))
    return not should_stop(stop_at)


def safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        fallback = message.encode("ascii", errors="backslashreplace").decode("ascii")
        sys.stdout.write(fallback + "\n")
        sys.stdout.flush()


def monitor_loop(config: dict, run_once: bool) -> None:
    session = create_session()
    jobs = parse_events(config)
    campaign_dir = Path(config.get("campaign_dir", "."))
    logs_dir = campaign_dir / config.get("logs_dir", "logs")
    runtime_dir = campaign_dir / config.get("runtime_dir", "runtime")
    stop_at = parse_stop_time(config)
    logs_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    heartbeat_path = runtime_dir / f"{config['campaign_id']}_heartbeat.json"
    last_sales: dict[str, int] = {}

    safe_print(f"KTOWN4U monitor started for {config['campaign_id']}")
    if stop_at is not None:
        safe_print(f"Monitor stop time: {stop_at.isoformat()}")

    while True:
        if should_stop(stop_at):
            safe_print(f"Stop time reached for {config['campaign_id']}, exiting monitor loop.")
            write_heartbeat(heartbeat_path, config)
            return

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with ThreadPoolExecutor(max_workers=min(len(jobs), 8) or 1) as pool:
            futures = [pool.submit(fetch, session, job) for job in jobs]

            for job, future in zip(jobs, futures):
                name, sales = future.result()
                state_key = f"{job['event']}_{job['site']}_{job['goods_no']}"

                if isinstance(sales, int):
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
                    safe_print(f"[{ts}] [{job['event']}/{job['site']}/{job['goods_no']}] {sales}")

                time.sleep(random.uniform(1.5, 3))

        write_heartbeat(heartbeat_path, config)
        if run_once:
            return
        if not sleep_until_next_cycle(config["poll_interval_seconds"], stop_at):
            safe_print(f"Stop time reached for {config['campaign_id']}, exiting monitor loop.")
            write_heartbeat(heartbeat_path, config)
            return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to campaign JSON config")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    monitor_loop(config, run_once=args.once)


if __name__ == "__main__":
    main()
