#!/usr/bin/env python3
"""
统一飞书 webhook 服务 — 同时管理 imini 和 K4 监控

指令格式（需加平台前缀）：
  imini status / k4 status / status   — 查看监控状态
  imini start <pro_id>                — 启动/恢复 imini 监控
  imini stop  <pro_id>                — 停止 imini 监控
  imini set   <pro_id> YYYY-MM-DD HH:MM
  imini sort  <pro_id>
  k4 start <campaign_id>
  k4 stop  <campaign_id>
  k4 set   <campaign_id> YYYY-MM-DD HH:MM
  k4 sort  <campaign_id>
  help
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

# ── 模块隔离：先加载 k4，再切回 imini，避免同名 config/feishu 冲突 ────────────
_K4_DIR = str(Path(os.environ.get("K4_DIR", str(Path(__file__).parent.parent / "k4"))))

sys.path.insert(0, _K4_DIR)
import orchestrator as _k4_orch_raw
from k4_sort import sort_campaign as k4_sort_campaign
for _m in ("config", "feishu", "orchestrator"):
    sys.modules.pop(_m, None)
sys.path.remove(_K4_DIR)

k4_orch = _k4_orch_raw
K4_CAMPAIGNS_DIR = Path(_K4_DIR) / "campaigns"
# ─────────────────────────────────────────────────────────────────────────────

# ── 模块隔离：加载 kms ────────────────────────────────────────────────────────
_KMS_DIR = str(Path(os.environ.get("KMS_DIR", str(Path(__file__).parent.parent / "kms"))))

sys.path.insert(0, _KMS_DIR)
import orchestrator as _kms_orch_raw
from kms_sort import sort_campaign as kms_sort_campaign
for _m in ("config", "feishu", "orchestrator"):
    sys.modules.pop(_m, None)
sys.path.remove(_KMS_DIR)

kms_orch = _kms_orch_raw
KMS_CAMPAIGNS_DIR = Path(_KMS_DIR) / "campaigns"
# ─────────────────────────────────────────────────────────────────────────────

from flask import Flask, jsonify, request

import feishu
from config import BASE_DIR, BOT_PORT, FEISHU_VERIFY_TOKEN, MONITORS_DIR
from imini_sort import sort_group_for_folder
from imini_scan import scan as imini_run_scan

app = Flask(__name__)


# ── 日志发送辅助 ──────────────────────────────────────────────────────────────

def _send_log(path: Path, label: str) -> None:
    """把日志文件作为附件发送给用户"""
    if not path.exists():
        feishu.notify(f"❌ 日志文件不存在: {label}")
        return
    feishu.notify_file(path)


# ── imini 工具函数 ─────────────────────────────────────────────────────────────

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


def find_imini_folder(pro_id: str) -> Path | None:
    for folder in Path(MONITORS_DIR).iterdir():
        if not folder.is_dir():
            continue
        info = get_info(folder)
        if info.get("pro_id") == pro_id:
            return folder
        pro_ids = [p.strip() for p in str(info.get("pro_ids", "")).split(",") if p.strip()]
        if pro_id in pro_ids:
            return folder
    return None


def imini_start_monitor(event: dict) -> int:
    pro_ids_str  = event.get("pro_ids") or event.get("pro_id", "")
    primary_id   = pro_ids_str.split(",")[0].strip()
    activity_end = event.get("activity_end", "")
    end_time     = event.get("end_time", "")
    # 文件夹以活动截止时间命名（更直观），无则用监控停止时间
    folder_time  = activity_end or end_time
    folder = Path(MONITORS_DIR) / make_folder_name(
        folder_time, event.get("name", ""), primary_id,
    )
    folder.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(BASE_DIR / "imini_monitor.py"),
           "--pro_id", pro_ids_str, "--folder", str(folder)]
    if event.get("sources"):
        cmd += ["--sources", event["sources"]]
    if end_time:
        cmd += ["--end_time", end_time]
    if activity_end:
        cmd += ["--activity_end", activity_end]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    save_info(folder, {
        **event,
        "pro_id":     primary_id,
        "pro_ids":    pro_ids_str,
        "pid":        proc.pid,
        "folder":     str(folder),
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    return proc.pid


# ── k4 工具函数 ────────────────────────────────────────────────────────────────

def find_k4_campaign(identifier: str) -> Path | None:
    identifier = identifier.strip().lower()
    # 第一轮：精确匹配 folder 名或 campaign_id
    for folder in k4_orch.campaign_folders():
        campaign = k4_orch.load_campaign(folder)
        if identifier in {folder.name.lower(), campaign.get("campaign_id", "").lower()}:
            return folder
    # 第二轮：模糊匹配（identifier 是 folder 名的子串即可）
    matches = []
    for folder in k4_orch.campaign_folders():
        if identifier in folder.name.lower():
            matches.append(folder)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # 多个命中时返回最近的一个（folder 名含日期前缀，倒序最大即最新）
        return sorted(matches, key=lambda f: f.name, reverse=True)[0]
    return None


# ── 指令处理 ──────────────────────────────────────────────────────────────────

def handle_imini(cmd: str, arg: str):
    """处理 imini 平台指令"""

    if cmd == "status":
        lines = ["📊 imini 监控状态："]
        shown = 0
        for folder in sorted(Path(MONITORS_DIR).iterdir()):
            if not folder.is_dir():
                continue
            info         = get_info(folder)
            pid          = info.get("pid", 0)
            alive        = is_running(pid)
            end_time     = info.get("end_time", "")
            activity_end = info.get("activity_end", "")
            # 已停止超过 12 小时不再显示（数据仍保留）
            ref_time = end_time or activity_end
            if not alive and ref_time:
                try:
                    stopped_at = datetime.strptime(ref_time, "%Y-%m-%d %H:%M:%S")
                    if (datetime.now() - stopped_at).total_seconds() > 12 * 3600:
                        continue
                except ValueError:
                    pass
            flag = "✅ 运行中" if alive else "⏹ 已停止"
            time_display = activity_end or end_time or "⚠️ 未设置"
            remaining = ""
            if activity_end or end_time:
                try:
                    deadline = datetime.strptime(activity_end or end_time, "%Y-%m-%d %H:%M:%S")
                    delta = deadline - datetime.now()
                    if delta.total_seconds() <= 0:
                        remaining = "已截止"
                    else:
                        d, s = delta.days, delta.seconds
                        h, m = s // 3600, (s % 3600) // 60
                        remaining = f"{d}天{h}小时" if d else (f"{h}小时{m}分钟" if h else f"{m}分钟")
                except ValueError:
                    pass
            lines.append(f"{flag}  {folder.name}\n  活动截止: {time_display}\n  剩余: {remaining or '未设截止时间'}")
            shown += 1
        if shown == 0:
            lines.append("暂无活跃监控进程")
        feishu.notify("\n".join(lines))

    elif cmd == "start":
        folder = find_imini_folder(arg)
        if folder is None:
            feishu.notify(f"❌ imini 未找到 pro_id={arg}")
            return
        info = get_info(folder)
        if is_running(info.get("pid", 0)):
            feishu.notify(f"⚠️ 监控已在运行\n活动: {info.get('name', folder.name)}")
            return
        if not info.get("activity_end") and not info.get("end_time"):
            feishu.notify(f"⚠️ 未设截止时间\n请先: imini set {arg} YYYY-MM-DD HH:MM")
            return
        pid = imini_start_monitor(info)
        feishu.notify(f"✅ 监控已启动\n活动: {info.get('name', folder.name)}\nPID: {pid}")

    elif cmd == "stop":
        folder = find_imini_folder(arg)
        if folder is None:
            feishu.notify(f"❌ imini 未找到 pro_id={arg}")
            return
        info = get_info(folder)
        pid  = info.get("pid", 0)
        if pid and is_running(pid):
            os.kill(pid, 15)
            info["pid"] = 0
            save_info(folder, info)
            feishu.notify(f"⏹ 已停止监控\n活动: {info.get('name', folder.name)}")
        else:
            feishu.notify(f"⚠️ 监控未在运行\n活动: {info.get('name', folder.name)}")

    elif cmd == "set":
        parts = arg.split(maxsplit=1)
        if len(parts) < 2:
            feishu.notify("格式：imini set <pro_id> YYYY-MM-DD HH:MM")
            return
        pro_id, activity_end = parts[0], parts[1]
        if len(activity_end) == 16:
            activity_end += ":00"
        try:
            activity_end_dt = datetime.strptime(activity_end, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            feishu.notify("截止时间格式不对，请使用：imini set <pro_id> YYYY-MM-DD HH:MM")
            return
        # 监控停止时间 = 活动截止 + 30 分钟（用于捞退单）
        end_time = (activity_end_dt + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        folder = find_imini_folder(pro_id)
        if folder is None:
            feishu.notify(f"❌ imini 未找到 pro_id={pro_id}")
            return
        info = get_info(folder)
        old_pid = info.get("pid", 0)
        if old_pid and is_running(old_pid):
            os.kill(old_pid, 15)
        new_name   = make_folder_name(activity_end, info["name"], info.get("pro_id", pro_id))
        new_folder = Path(MONITORS_DIR) / new_name
        if folder != new_folder:
            folder.rename(new_folder)
            new_log = new_folder / f"{folder.name}.log"
            if new_log.exists():
                new_log.rename(new_folder / f"{new_name}.log")
        info["activity_end"] = activity_end
        info["end_time"]     = end_time
        pid = imini_start_monitor(info)
        feishu.notify(
            f"✅ 截止时间已设置，监控已启动\n"
            f"活动: {info['name']}\n"
            f"活动截止: {activity_end}\n"
            f"监控停止: {end_time}（自动延长30分钟捞退单）\n"
            f"PID: {pid}"
        )

    elif cmd == "sort":
        folder = find_imini_folder(arg)
        if folder is None:
            feishu.notify(f"❌ imini 未找到 pro_id={arg}")
            return
        feishu.notify(sort_group_for_folder(folder))

    elif cmd == "scan":
        feishu.notify("🔍 开始扫描 imini...")
        found = imini_run_scan() or []
        if not found:
            feishu.notify("扫描完成，未发现新活动")
        else:
            for event in found:
                feishu.notify(
                    f"🆕 imini 发现新活动\n名称: {event.get('name', '')}\n"
                    f"pro_id: {event.get('pro_ids') or event.get('pro_id', '')}\n"
                    f"价格: {event.get('price', '')}元"
                )

    elif cmd in ("log", "log1", "log2"):
        if not arg or arg.lower() in ("orchestrator", "orch"):
            _send_log(Path(BASE_DIR) / "orchestrator.log", "imini orchestrator.log")
        elif cmd in ("log", "log1"):
            # 原始监控日志
            folder = find_imini_folder(arg)
            if folder is None:
                feishu.notify(f"❌ imini 未找到 pro_id={arg}")
                return
            log_files = sorted(folder.glob("*.log"))
            if not log_files:
                feishu.notify("❌ 该活动暂无日志文件")
                return
            _send_log(log_files[0], log_files[0].name)
        else:
            # 合并排序文件（保存在 monitors 根目录，以日期开头、_合并排序.log 结尾）
            folder = find_imini_folder(arg)
            if folder is None:
                feishu.notify(f"❌ imini 未找到 pro_id={arg}")
                return
            date_prefix = folder.name[:10]  # 取文件夹名前10位日期
            matches = sorted(Path(MONITORS_DIR).glob(f"{date_prefix}*_合并排序.log"))
            if not matches:
                feishu.notify("❌ 未找到合并排序文件，请先执行 imini sort")
                return
            _send_log(matches[0], matches[0].name)

    else:
        feishu.notify(f"未知 imini 指令: {cmd}，发送 help 查看可用命令")


def find_kms_campaign(identifier: str) -> Path | None:
    identifier = identifier.strip().lower()
    for folder in kms_orch.campaign_folders():
        campaign = kms_orch.load_campaign(folder)
        if identifier in {folder.name.lower(), campaign.get("campaign_id", "").lower()}:
            return folder
    matches = [f for f in kms_orch.campaign_folders() if identifier in f.name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return sorted(matches, key=lambda f: f.name, reverse=True)[0]
    return None


def handle_kms(cmd: str, arg: str):
    """处理 kms 平台指令"""

    if cmd == "status":
        feishu.notify("\n".join(kms_orch.status_lines()))

    elif cmd == "scan":
        feishu.notify("🔍 开始扫描 KMStation...")
        found = kms_orch.run_scan()
        if not found:
            feishu.notify("扫描完成，未发现新活动")
        else:
            for cfg in found:
                kms_orch.save_new_campaign(cfg)
                feishu.notify(
                    f"🆕 发现新活动\n名称: {cfg.get('name', '')}\n"
                    f"prod_ids: {cfg.get('prod_ids', [])}\n"
                    f"请用 kms set {cfg['campaign_id']} YYYY-MM-DD HH:MM 设置截止时间"
                )

    elif cmd == "start":
        folder = find_kms_campaign(arg)
        if folder is None:
            feishu.notify(f"❌ KMS 未找到 campaign: {arg}")
            return
        pid = kms_orch.start_monitor(folder)
        campaign = kms_orch.load_campaign(folder)
        feishu.notify(f"✅ KMS 监控已启动\n活动: {campaign.get('name', folder.name)}\nPID: {pid}")

    elif cmd == "stop":
        folder = find_kms_campaign(arg)
        if folder is None:
            feishu.notify(f"❌ KMS 未找到 campaign: {arg}")
            return
        stopped = kms_orch.stop_monitor(folder)
        campaign = kms_orch.load_campaign(folder)
        feishu.notify(f"{'⏹ 已停止' if stopped else '⚠️ 监控未在运行'}\n活动: {campaign.get('name', folder.name)}")

    elif cmd == "set":
        parts = arg.split(maxsplit=1)
        if len(parts) < 2:
            feishu.notify("格式：kms set <campaign_id> YYYY-MM-DD HH:MM")
            return
        campaign_id, stop_at = parts[0], parts[1].strip()
        if len(stop_at) == 16:
            stop_at += ":00"
        try:
            datetime.strptime(stop_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            feishu.notify("截止时间格式不对，请使用：kms set <campaign_id> YYYY-MM-DD HH:MM")
            return
        folder = find_kms_campaign(campaign_id)
        if folder is None:
            feishu.notify(f"❌ KMS 未找到 campaign: {campaign_id}")
            return
        campaign = kms_orch.load_campaign(folder)
        campaign["stop_monitor_at"] = stop_at
        kms_orch.save_campaign(folder, campaign)
        pid = kms_orch.start_monitor(folder)
        feishu.notify(
            f"✅ 截止时间已设置，KMS 监控已启动\n"
            f"活动: {campaign.get('name', folder.name)}\n"
            f"截止: {stop_at}\nPID: {pid}"
        )

    elif cmd == "sort":
        folder = find_kms_campaign(arg)
        if folder is None:
            feishu.notify(f"❌ KMS 未找到 campaign: {arg}")
            return
        text, xlsx_path = kms_sort_campaign(folder / "campaign.json")
        feishu.notify(text)

    elif cmd in ("log", "log1", "log2"):
        if not arg or arg.lower() in ("orchestrator", "orch"):
            _send_log(Path(_KMS_DIR) / "orchestrator.log", "kms orchestrator.log")
        elif cmd in ("log", "log1"):
            folder = find_kms_campaign(arg)
            if folder is None:
                feishu.notify(f"❌ KMS 未找到 campaign: {arg}")
                return
            stock_log = folder / "logs" / "stock.log"
            _send_log(stock_log, stock_log.name)
        else:
            folder = find_kms_campaign(arg)
            if folder is None:
                feishu.notify(f"❌ KMS 未找到 campaign: {arg}")
                return
            exports_dir = folder / "exports"
            # 优先找 xlsx，没有再找 log；只发最新的一个
            export_files = sorted(exports_dir.glob("*.xlsx")) if exports_dir.exists() else []
            if not export_files:
                export_files = sorted(exports_dir.glob("*.log")) if exports_dir.exists() else []
            if not export_files:
                feishu.notify("❌ 未找到排序导出文件，请先执行 kms sort")
                return
            feishu.notify_file(export_files[-1])

    else:
        feishu.notify(f"未知 kms 指令: {cmd}，发送 help 查看可用命令")


def handle_k4(cmd: str, arg: str):
    """处理 k4 平台指令"""

    if cmd == "status":
        feishu.notify("\n".join(k4_orch.status_lines()))

    elif cmd == "start":
        folder = find_k4_campaign(arg)
        if folder is None:
            feishu.notify(f"❌ K4 未找到 campaign: {arg}")
            return
        pid = k4_orch.start_monitor(folder)
        campaign = k4_orch.load_campaign(folder)
        feishu.notify(f"✅ K4 监控已启动\n活动: {campaign.get('name', folder.name)}\nPID: {pid}")

    elif cmd == "stop":
        folder = find_k4_campaign(arg)
        if folder is None:
            feishu.notify(f"❌ K4 未找到 campaign: {arg}")
            return
        stopped = k4_orch.stop_monitor(folder)
        campaign = k4_orch.load_campaign(folder)
        feishu.notify(f"{'⏹ 已停止' if stopped else '⚠️ 监控未在运行'}\n活动: {campaign.get('name', folder.name)}")

    elif cmd == "set":
        parts = arg.split(maxsplit=1)
        if len(parts) < 2:
            feishu.notify("格式：k4 set <campaign_id> YYYY-MM-DD HH:MM")
            return
        campaign_id, activity_end = parts[0], parts[1]
        if len(activity_end) == 16:
            activity_end += ":00"
        try:
            activity_end_dt = datetime.strptime(activity_end, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            feishu.notify("截止时间格式不对，请使用：k4 set <campaign_id> YYYY-MM-DD HH:MM")
            return
        # 监控停止时间 = 活动截止 + 30 分钟（用于捞退单）
        stop_monitor_at = (activity_end_dt + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        folder = find_k4_campaign(campaign_id)
        if folder is None:
            feishu.notify(f"❌ K4 未找到 campaign: {campaign_id}")
            return
        campaign = k4_orch.load_campaign(folder)
        campaign["activity_end_at"]  = activity_end
        campaign["stop_monitor_at"]  = stop_monitor_at
        k4_orch.save_campaign(folder, campaign)
        pid = k4_orch.start_monitor(folder)
        feishu.notify(
            f"✅ 截止时间已设置，K4 监控已启动\n"
            f"活动: {campaign.get('name', folder.name)}\n"
            f"活动截止: {activity_end}\n"
            f"监控停止: {stop_monitor_at}（自动延长30分钟捞退单）\n"
            f"PID: {pid}"
        )

    elif cmd == "scan":
        feishu.notify("🔍 开始扫描 K4...")
        found = k4_orch.run_scan() or []
        if not found:
            feishu.notify("扫描完成，未发现新活动")
        else:
            for cfg in found:
                feishu.notify(
                    f"🆕 K4 发现新活动\n名称: {cfg.get('name', '')}\n"
                    f"campaign: {cfg.get('campaign_id', '')}"
                )

    elif cmd == "sort":
        folder = find_k4_campaign(arg)
        if folder is None:
            feishu.notify(f"❌ K4 未找到 campaign: {arg}")
            return
        feishu.notify(k4_sort_campaign(folder / "campaign.json"))

    elif cmd in ("log", "log1", "log2"):
        if not arg or arg.lower() in ("orchestrator", "orch"):
            _send_log(Path(_K4_DIR) / "orchestrator.log", "k4 orchestrator.log")
        elif cmd in ("log", "log1"):
            # 所有站点监控日志合并按时间排序
            folder = find_k4_campaign(arg)
            if folder is None:
                feishu.notify(f"❌ K4 未找到 campaign: {arg}")
                return
            logs_dir = folder / "logs"
            log_files = sorted(logs_dir.glob("*.log")) if logs_dir.exists() else []
            # 排除 heartbeat.log，只合并站点日志
            site_logs = [lf for lf in log_files if lf.name != "heartbeat.log"]
            if not site_logs:
                feishu.notify("❌ 该 campaign 暂无监控日志文件")
                return
            all_lines = []
            for lf in site_logs:
                site = lf.stem
                for line in lf.read_text(encoding="utf-8", errors="replace").splitlines():
                    all_lines.append((line[:19], f"[{site}] {line}"))
            all_lines.sort(key=lambda x: x[0])
            merged = "\n".join(l for _, l in all_lines)
            tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".log", prefix=f"k4_{folder.name}_")
            tmp_path = Path(tmp_path_str)
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(merged)
                feishu.notify_file(tmp_path)
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            # 排序导出文件（exports 目录）
            folder = find_k4_campaign(arg)
            if folder is None:
                feishu.notify(f"❌ K4 未找到 campaign: {arg}")
                return
            exports_dir = folder / "exports"
            export_files = sorted(exports_dir.glob("*.log")) if exports_dir.exists() else []
            if not export_files:
                feishu.notify("❌ 未找到排序导出文件，请先执行 k4 sort")
                return
            for ef in export_files:
                feishu.notify_file(ef)

    else:
        feishu.notify(f"未知 k4 指令: {cmd}，发送 help 查看可用命令")


def handle_command(text: str):
    text = text.strip()
    lower = text.lower()

    # ── status（全平台）─────────────────────────────────────────────────────────
    if lower == "status":
        handle_imini("status", "")
        handle_k4("status", "")
        handle_kms("status", "")
        return

    # ── help ────────────────────────────────────────────────────────────────────
    if lower in ("help", "帮助"):
        feishu.notify(
            "📋 可用指令：\n\n"
            "【imini 平台】\n"
            "imini status\n"
            "imini scan\n"
            "imini start <pro_id>\n"
            "imini stop  <pro_id>\n"
            "imini set   <pro_id> YYYY-MM-DD HH:MM  （输入活动截止时间，监控自动延长30分钟）\n"
            "imini sort  <pro_id>\n"
            "imini log1 <pro_id>     — 原始监控日志\n"
            "imini log2 <pro_id>     — 合并排序文件\n"
            "imini log orchestrator  — 调度日志\n\n"
            "【K4 平台】\n"
            "k4 status\n"
            "k4 scan\n"
            "k4 start <campaign_id>\n"
            "k4 stop  <campaign_id>\n"
            "k4 set   <campaign_id> YYYY-MM-DD HH:MM  （输入活动截止时间，监控自动延长30分钟）\n"
            "k4 sort  <campaign_id>\n"
            "k4 log1 <campaign_id>   — 监控日志（所有站点合并）\n"
            "k4 log2 <campaign_id>   — 排序导出文件\n"
            "k4 log orchestrator     — 调度日志\n\n"
            "【KMS 平台】\n"
            "kms status\n"
            "kms scan\n"
            "kms start <campaign_id>\n"
            "kms stop  <campaign_id>\n"
            "kms set   <campaign_id> YYYY-MM-DD HH:MM\n"
            "kms sort  <campaign_id>\n"
            "kms log1 <campaign_id>  — 库存监控日志\n"
            "kms log2 <campaign_id>  — 排序导出文件\n"
            "kms log orchestrator    — 调度日志\n\n"
            "status  — 查看所有平台状态\n"
            "help    — 显示此帮助"
        )
        return

    # ── 平台路由 ────────────────────────────────────────────────────────────────
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        feishu.notify("未知指令，发送 help 查看可用命令")
        return

    platform = parts[0].lower()
    cmd      = parts[1].lower()
    arg      = parts[2].strip() if len(parts) > 2 else ""

    if platform == "imini":
        handle_imini(cmd, arg)
    elif platform == "k4":
        handle_k4(cmd, arg)
    elif platform == "kms":
        handle_kms(cmd, arg)
    else:
        feishu.notify("未知指令，发送 help 查看可用命令\n（imini / k4 / kms 开头）")


# ── Flask Webhook ─────────────────────────────────────────────────────────────

@app.route("/feishu/webhook", methods=["POST"])
def webhook():
    body = request.json or {}

    if body.get("type") == "url_verification":
        return jsonify({"challenge": body.get("challenge", "")})

    if FEISHU_VERIFY_TOKEN:
        if body.get("header", {}).get("token", "") != FEISHU_VERIFY_TOKEN:
            return jsonify({"code": 1, "msg": "invalid token"}), 403

    header = body.get("header", {})
    event  = body.get("event", {})

    if header.get("event_type") == "im.message.receive_v1":
        sender  = event.get("sender", {})
        msg     = event.get("message", {})
        open_id = sender.get("sender_id", {}).get("open_id", "")

        if open_id and not feishu.get_user_id():
            feishu.save_user_id(open_id)
            feishu.notify("✅ 已绑定，发送 help 查看可用指令")

        try:
            text = json.loads(msg.get("content", "{}")).get("text", "").strip()
        except Exception:
            text = ""

        if text:
            threading.Thread(target=handle_command, args=(text,), daemon=True).start()

    return jsonify({"code": 0})


# ── 启动 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    Path(MONITORS_DIR).mkdir(exist_ok=True)
    K4_CAMPAIGNS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[Bot Server] 启动，端口 {BOT_PORT}")
    print(f"[Bot Server] Webhook URL: http://112.124.10.177:{BOT_PORT}/feishu/webhook")
    app.run(host="0.0.0.0", port=BOT_PORT)
