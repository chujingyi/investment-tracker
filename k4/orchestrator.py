#!/usr/bin/env python3
"""
K4 monitor orchestrator.

Manual-first workflow:
  1. Create campaigns/<campaign_id>/campaign.json from campaign.sample.json.
  2. Run python3 orchestrator.py to start or recover active monitors.
  3. Run python3 orchestrator.py --report to send a Feishu daily report.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import feishu
from config import BASE_DIR, CAMPAIGNS_DIR, ORCH_LOG
from k4_scan import scan as run_scan
from k4_sort import sort_campaign


def log(message: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}"
    ORCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ORCH_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def campaign_folders() -> list[Path]:
    CAMPAIGNS_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(folder for folder in CAMPAIGNS_DIR.iterdir() if (folder / "campaign.json").exists())


def load_campaign(folder: Path) -> dict:
    with (folder / "campaign.json").open(encoding="utf-8-sig") as f:
        return json.load(f)


def save_campaign(folder: Path, campaign: dict) -> None:
    (folder / "campaign.json").write_text(
        json.dumps(campaign, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def runtime_dir(folder: Path) -> Path:
    path = folder / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pid_path(folder: Path) -> Path:
    return runtime_dir(folder) / "monitor.pid"


def read_pid(folder: Path) -> int:
    try:
        return int(pid_path(folder).read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def write_pid(folder: Path, pid: int) -> None:
    pid_path(folder).write_text(str(pid), encoding="utf-8")


def is_running(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def parse_stop_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def is_expired(campaign: dict) -> bool:
    stop_at = parse_stop_time(campaign.get("stop_monitor_at", ""))
    if stop_at is None:
        return False
    now = datetime.now(stop_at.tzinfo) if stop_at.tzinfo else datetime.now()
    return now >= stop_at


def remaining_text(campaign: dict) -> str:
    # 优先用活动截止时间计算剩余（对用户更有意义），无则用监控停止时间
    ref_raw = campaign.get("activity_end_at") or campaign.get("stop_monitor_at", "")
    ref_dt = parse_stop_time(ref_raw)
    if ref_dt is None:
        return "未设置"
    now = datetime.now(ref_dt.tzinfo) if ref_dt.tzinfo else datetime.now()
    delta = ref_dt - now
    if delta.total_seconds() <= 0:
        return "已截止"
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"


def ensure_campaign_paths(folder: Path, campaign: dict) -> dict:
    changed = False
    if campaign.get("campaign_dir") != str(folder):
        campaign["campaign_dir"] = str(folder)
        changed = True
    campaign.setdefault("logs_dir", "logs")
    campaign.setdefault("runtime_dir", "runtime")
    campaign.setdefault("exports_dir", "exports")
    if changed:
        save_campaign(folder, campaign)
    (folder / campaign["logs_dir"]).mkdir(parents=True, exist_ok=True)
    (folder / campaign["runtime_dir"]).mkdir(parents=True, exist_ok=True)
    (folder / campaign["exports_dir"]).mkdir(parents=True, exist_ok=True)
    return campaign


def start_monitor(folder: Path) -> int:
    campaign = ensure_campaign_paths(folder, load_campaign(folder))
    pid = read_pid(folder)
    if pid and is_running(pid):
        return pid

    runtime = runtime_dir(folder)
    with (runtime / "monitor.stdout.log").open("ab") as stdout, \
         (runtime / "monitor.stderr.log").open("ab") as stderr:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(BASE_DIR / "k4_monitor.py"), "--config", str(folder / "campaign.json")],
            stdout=stdout,
            stderr=stderr,
            cwd=str(BASE_DIR),
        )
    write_pid(folder, proc.pid)
    log(f"启动监控: {folder.name} (PID {proc.pid})")
    return proc.pid


def stop_monitor(folder: Path) -> bool:
    pid = read_pid(folder)
    if not (pid and is_running(pid)):
        return False
    os.kill(pid, 15)
    write_pid(folder, 0)  # 清零，区别于崩溃
    log(f"停止监控: {folder.name} (PID {pid})")
    return True


def stop_expired() -> None:
    for folder in campaign_folders():
        campaign = load_campaign(folder)
        if is_expired(campaign):
            stop_monitor(folder)


def check_resources() -> None:
    """检查磁盘和内存，超过 90% 时通过飞书告警。"""
    import shutil
    alerts = []
    try:
        du = shutil.disk_usage("/")
        pct = du.used / du.total * 100
        if pct >= 90:
            alerts.append(
                f"磁盘使用率 {pct:.1f}%"
                f"（已用 {du.used // 1024**3} G / 共 {du.total // 1024**3} G）"
            )
    except Exception as exc:
        alerts.append(f"磁盘检查失败: {exc}")
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            mem[k.strip()] = int(v.strip().split()[0])
        total = mem["MemTotal"]
        avail = mem.get("MemAvailable", mem.get("MemFree", total))
        pct = (total - avail) / total * 100
        if pct >= 90:
            alerts.append(
                f"内存使用率 {pct:.1f}%"
                f"（可用 {avail // 1024} MB / 共 {total // 1024} MB）"
            )
    except Exception:
        pass
    if alerts:
        feishu.notify("🚨 K4 服务器资源告警\n\n" + "\n".join(alerts))


def restart_active_monitors() -> None:
    for folder in campaign_folders():
        campaign = ensure_campaign_paths(folder, load_campaign(folder))
        if is_expired(campaign):
            continue
        if not campaign.get("stop_monitor_at"):
            continue
        pid = read_pid(folder)
        if pid and is_running(pid):
            continue
        if not pid:
            continue  # 手动停止（pid=0），不重启
        # pid 有值但进程不在 → 异常崩溃，重启
            name = campaign.get("name") or folder.name
            log(f"监控异常退出，正在重启: {folder.name} (上次 PID {pid})")
            feishu.notify(
                f"⚠️ K4 监控异常退出，已自动重启\n"
                f"活动: {name}\n"
                f"上次 PID: {pid}"
            )
        start_monitor(folder)


def stopped_over_12h(campaign: dict) -> bool:
    """已停止且截止时间超过 12 小时前，不再显示在 status 里。"""
    stop_at = parse_stop_time(campaign.get("stop_monitor_at", ""))
    if stop_at is None:
        return False
    now = datetime.now(stop_at.tzinfo) if stop_at.tzinfo else datetime.now()
    return (now - stop_at).total_seconds() > 12 * 3600


def status_lines() -> list[str]:
    lines = ["K4 当前监控状态："]
    folders = campaign_folders()
    if not folders:
        return lines + ["暂无 campaign"]

    shown = 0
    for folder in folders:
        campaign = load_campaign(folder)
        pid = read_pid(folder)
        running = is_running(pid)
        if not running and stopped_over_12h(campaign):
            continue
        shown += 1
        state = "🟢 运行中" if running else "🔴 已停止"
        activity_end = campaign.get("activity_end_at", "")
        stop_at_str  = campaign.get("stop_monitor_at", "")
        time_line = f"  活动截止: {activity_end or stop_at_str or '未设置'}"
        if activity_end and stop_at_str and activity_end != stop_at_str:
            time_line += f"\n  监控停止: {stop_at_str}"
        lines.append(
            f"{state}  {campaign.get('campaign_id', folder.name)}\n"
            f"  名称: {campaign.get('name', folder.name)}\n"
            f"{time_line}\n"
            f"  剩余: {remaining_text(campaign)}"
        )
    if shown == 0:
        lines.append("暂无活跃 campaign")
    return lines


def daily_report() -> None:
    feishu.notify("\n".join(status_lines()))
    for folder in campaign_folders():
        campaign = load_campaign(folder)
        pid = read_pid(folder)
        if pid and is_running(pid):
            feishu.notify(sort_campaign(folder / "campaign.json"))
    log("每日晚报已发送")


def save_new_campaign(campaign: dict) -> Path:
    campaign_id = campaign["campaign_id"]
    folder = CAMPAIGNS_DIR / campaign_id
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / "campaign.json").exists():
        save_campaign(folder, campaign)
    ensure_campaign_paths(folder, load_campaign(folder))
    return folder


def main() -> None:
    log("=== orchestrator 启动 ===")
    CAMPAIGNS_DIR.mkdir(parents=True, exist_ok=True)
    check_resources()
    stop_expired()
    restart_active_monitors()

    new_events = run_scan() or []
    for campaign in new_events:
        folder = save_new_campaign(campaign)
        if campaign.get("stop_monitor_at"):
            pid = start_monitor(folder)
            feishu.notify(
                "K4 发现新活动，监控已启动\n"
                f"campaign: {campaign['campaign_id']}\n"
                f"名称: {campaign.get('name', '')}\n"
                f"截止: {campaign.get('stop_monitor_at', '')}\n"
                f"PID: {pid}"
            )
        else:
            feishu.notify(
                "K4 发现新活动，但截止时间未知\n"
                f"campaign: {campaign['campaign_id']}\n"
                f"名称: {campaign.get('name', '')}\n"
                f"请用 set {campaign['campaign_id']} YYYY-MM-DD HH:MM 设置截止时间"
            )

    log(f"=== 完成，新活动 {len(new_events)} 个 ===")


def evening_report() -> None:
    """19:05 扫描播报：当天无新活动时汇报今日扫描记录。"""
    today = datetime.now().strftime("%Y-%m-%d")
    scan_times: list[str] = []
    found_new = False
    seen: set[str] = set()

    if ORCH_LOG.exists():
        for line in ORCH_LOG.read_text(encoding="utf-8").splitlines():
            if not line.startswith(f"[{today}") or line in seen:
                continue
            seen.add(line)
            if "=== orchestrator 启动 ===" in line:
                try:
                    scan_times.append(line[12:17])  # HH:MM
                except Exception:
                    pass
            if "新活动" in line:
                try:
                    n = int(line.split("新活动")[1].split("个")[0].strip())
                    if n > 0:
                        found_new = True
                except Exception:
                    pass

    if found_new:
        return  # 有新活动已推送，不重复播报

    if not scan_times:
        feishu.notify(f"📋 K4 今日（{today}）无扫描记录")
        return

    feishu.notify(
        f"📋 K4 今日扫描播报（{today}）\n"
        f"共扫描 {len(scan_times)} 次\n"
        f"时间：{' / '.join(scan_times)}\n\n"
        "未发现新活动"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="发送每日报告")
    parser.add_argument("--status", action="store_true", help="打印监控状态")
    parser.add_argument("--evening-report", action="store_true", help="发送19点扫描播报")
    args = parser.parse_args()

    try:
        if args.report:
            daily_report()
        elif args.status:
            print("\n".join(status_lines()))
        elif args.evening_report:
            evening_report()
        else:
            main()
    except Exception as exc:
        import traceback
        msg = traceback.format_exc()
        log(f"orchestrator 异常崩溃: {exc}")
        try:
            feishu.notify(f"🚨 K4 orchestrator 异常崩溃\n\n{msg[-800:]}")
        except Exception:
            pass
        raise
