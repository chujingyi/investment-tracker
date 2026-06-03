#!/usr/bin/env python3
"""
调度器
  正常扫描 (cron 每天 09:00-19:00 每小时):
    0 9-19 * * * cd /root/imini && python3 orchestrator.py

  每日晚报 (cron 每天 22:00):
    0 22 * * * cd /root/imini && python3 orchestrator.py --report

飞书通知时机：
  1. 扫描到新活动
  2. 监控进程启动
  3. 每日 22:00 — 活动列表 + 剩余天数 + 各活动排序报告
  4. 活动截止 — 停止通知 + 最终排序报告
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import feishu
from config import BASE_DIR, MONITORS_DIR, ORCH_LOG
from imini_scan import scan as run_scan
from imini_sort import sort_group_for_folder


# ── 工具 ──────────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(ORCH_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def make_folder_name(end_time: str, name: str, pro_id: str = "") -> str:
    date_part = end_time[:10] if end_time else "未知截止"
    clean = re.sub(r"[^\w一-鿿]", "", name)[:50]
    suffix = f"_{pro_id}" if pro_id else ""
    return f"{date_part}_{clean}{suffix}"


def get_info(folder: Path) -> dict:
    f = folder / "info.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def save_info(folder: Path, info: dict):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def is_running(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def remaining_str(end_time: str) -> str:
    """返回距截止的可读剩余时间，如 '3天2小时' 或 '已截止'"""
    if not end_time:
        return "截止时间未设置"
    try:
        delta = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S") - datetime.now()
        if delta.total_seconds() <= 0:
            return "已截止"
        days  = delta.days
        hours = delta.seconds // 3600
        if days > 0:
            return f"{days}天{hours}小时"
        mins = (delta.seconds % 3600) // 60
        return f"{hours}小时{mins}分钟" if hours > 0 else f"{mins}分钟"
    except ValueError:
        return "格式有误"


def _source_label(name: str) -> str:
    if "非大陆地址" in name:
        return "非大陆地址"
    if "大陆地址" in name:
        return "大陆地址"
    return "未知来源"


def _clean_group_title(name: str) -> str:
    title = re.sub(r"【\s*(?:非)?大陆地址\s*】", "", name)
    return re.sub(r"\s+", " ", title).strip()


# ── 进程管理 ──────────────────────────────────────────────────────────────────

def start_monitor(event: dict, folder: Path | None = None) -> Path:
    # 支持单 pro_id 和逗号分隔多 pro_id
    pro_ids_str = event.get("pro_ids") or event.get("pro_id", "")
    primary_id  = pro_ids_str.split(",")[0].strip()

    folder = folder or Path(MONITORS_DIR) / make_folder_name(
        event.get("end_time", ""),
        event.get("name", ""),
        primary_id,
    )
    folder.mkdir(parents=True, exist_ok=True)

    info = get_info(folder)
    if info.get("pid") and is_running(info["pid"]):
        log(f"监控已在运行: {folder.name}")
        return folder

    cmd = [
        sys.executable, str(BASE_DIR / "imini_monitor.py"),
        "--pro_id", pro_ids_str,
        "--folder", str(folder),
    ]
    if event.get("sources"):
        cmd += ["--sources", event["sources"]]
    if event.get("end_time"):
        cmd += ["--end_time", event["end_time"]]

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    save_info(folder, {
        **event,
        "pro_id":     primary_id,
        "pro_ids":    pro_ids_str,
        "pid":        proc.pid,
        "folder":     str(folder),
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    log(f"启动监控: {folder.name} (PID {proc.pid})")
    return folder


def restart_active_monitors():
    """重启未过截止时间但进程已退出的监控。"""
    for folder in sorted(Path(MONITORS_DIR).iterdir()):
        if not folder.is_dir():
            continue

        info = get_info(folder)
        if not info:
            continue

        end_time = info.get("end_time", "")
        if not end_time:
            continue

        try:
            if datetime.now() >= datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S"):
                continue
        except ValueError:
            log(f"跳过截止时间格式错误的活动: {folder.name} | {end_time}")
            continue

        pid = info.get("pid", 0)
        if pid and is_running(pid):
            continue
        if not pid:
            continue  # 手动停止（pid=0），不重启

        name = info.get("name") or folder.name
        log(f"监控异常退出，重启: {folder.name} (上次 PID {pid})")
        import feishu as _feishu
        _feishu.notify(f"⚠️ imini 监控异常退出，已自动重启\n活动: {name}")
        start_monitor(info, folder)


def stop_expired():
    """停止已到期的监控（进程卡死未自退出时）"""
    for folder in sorted(Path(MONITORS_DIR).iterdir()):
        if not folder.is_dir():
            continue
        info = get_info(folder)
        end_time = info.get("end_time", "")
        if not end_time:
            continue
        try:
            if datetime.now() < datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S"):
                continue
        except ValueError:
            continue

        pid = info.get("pid", 0)
        if not (pid and is_running(pid)):
            continue

        os.kill(pid, 15)
        log(f"活动到期，强制停止残留进程: {folder.name}")


# ── 事件分组 ──────────────────────────────────────────────────────────────────

def group_events(events: list) -> list:
    """将同一活动的大陆/非大陆版本合并为一条记录，减少进程数"""
    groups: dict = {}
    for event in events:
        key = (
            (event.get("end_time") or "")[:10],
            _clean_group_title(event.get("name", "")),
        )
        if key not in groups:
            groups[key] = {
                "pro_ids_list": [],
                "sources_list": [],
                "end_time":     event.get("end_time", ""),
                "name":         _clean_group_title(event.get("name", "")),
                "price":        event.get("price", ""),
            }
        g = groups[key]
        g["pro_ids_list"].append(event["pro_id"])
        g["sources_list"].append(_source_label(event["name"]))
        if event.get("end_time"):
            g["end_time"] = event["end_time"]

    result = []
    for g in groups.values():
        result.append({
            **g,
            "pro_id":  g["pro_ids_list"][0],
            "pro_ids": ",".join(g["pro_ids_list"]),
            "sources": ",".join(g["sources_list"]),
        })
    return result


# ── 每日晚报 ──────────────────────────────────────────────────────────────────

def daily_report():
    """每日 22:00 报告：活动列表 + 剩余天数 + 各活动排序"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    running_list = []
    pending_list = []
    stopped_list = []

    for folder in sorted(Path(MONITORS_DIR).iterdir()):
        if not folder.is_dir():
            continue
        info  = get_info(folder)
        pid   = info.get("pid", 0)
        alive = is_running(pid)
        end   = info.get("end_time", "")

        entry = {
            "folder":    folder,
            "name":      info.get("name", folder.name),
            "end_time":  end,
            "remaining": remaining_str(end),
            "alive":     alive,
        }

        if alive:
            running_list.append(entry)
        elif not end:
            pending_list.append(entry)
        else:
            stopped_list.append(entry)

    lines = [f"📅 每日监控报告  {now}\n"]

    if running_list:
        lines.append(f"🟢 监控中（{len(running_list)} 个）")
        for i, e in enumerate(running_list, 1):
            lines.append(
                f"\n{i}. {e['name']}\n"
                f"   截止: {e['end_time']}\n"
                f"   剩余: {e['remaining']}"
            )

    if pending_list:
        lines.append(f"\n\n⚠️ 待补截止时间（{len(pending_list)} 个）")
        for e in pending_list:
            info = get_info(e["folder"])
            lines.append(f"\n· {e['name']}\n  请回复: set {info.get('pro_id','?')} YYYY-MM-DD HH:MM")

    if stopped_list:
        lines.append(f"\n\n⏹ 已停止（{len(stopped_list)} 个）")
        for e in stopped_list:
            lines.append(f"\n· {e['name']}  截止: {e['end_time'] or '未知'}")

    if not running_list and not pending_list and not stopped_list:
        lines.append("暂无监控活动")

    feishu.notify("\n".join(lines))

    if running_list:
        seen_groups = set()
        for e in running_list:
            key = (e["end_time"][:10] if e["end_time"] else "", e["name"])
            if key in seen_groups:
                continue
            seen_groups.add(key)
            result = sort_group_for_folder(e["folder"])
            feishu.notify(result)

    log("每日晚报已发送")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    log("=== orchestrator 启动 ===")
    Path(MONITORS_DIR).mkdir(exist_ok=True)

    stop_expired()
    restart_active_monitors()

    new_events = run_scan() or []
    grouped    = group_events(new_events)

    for event in grouped:
        if event.get("end_time"):
            folder = start_monitor(event)
            feishu.notify(
                f"🆕 发现新活动\n\n"
                f"名称: {event['name']}\n"
                f"pro_id: {event['pro_ids']}\n"
                f"价格: {event['price']}元\n"
                f"截止: {event['end_time']}\n"
                f"剩余: {remaining_str(event['end_time'])}\n\n"
                f"✅ 监控已自动启动"
            )
        else:
            folder = Path(MONITORS_DIR) / make_folder_name("", event["name"], event.get("pro_id", ""))
            save_info(folder, event)
            feishu.notify(
                f"🆕 发现新活动（截止时间未知）\n\n"
                f"名称: {event['name']}\n"
                f"pro_id: {event['pro_ids']}\n"
                f"价格: {event['price']}元\n\n"
                f"⚠️ 请补充截止时间后自动启动监控：\n"
                f"set {event['pro_id']} YYYY-MM-DD HH:MM"
            )

    log(f"=== 完成，新活动 {len(grouped)} 个 ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="发送每日晚报")
    args = parser.parse_args()

    if args.report:
        daily_report()
    else:
        main()
