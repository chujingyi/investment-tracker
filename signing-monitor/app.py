#!/usr/bin/env python3
"""
Signing Monitor Dashboard — 统一监控面板（远程服务器版）
通过 SSH 连接阿里云服务器读取监控数据

启动: python3 app.py
访问: http://localhost:9090

环境变量:
  DASHBOARD_PORT   监听端口 (默认 9090)
  DASHBOARD_TOKEN  访问认证 token (为空则不启用认证)
  FLASK_DEBUG      设为 1 开启调试模式 (默认关闭)
  SSH_HOST         服务器地址 (默认 112.124.10.177)
  SSH_USER         SSH 用户名 (默认 root)
  SSH_PASSWORD     SSH 密码
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_file, render_template, abort
from dotenv import load_dotenv

from pathlib import Path
BASE_DIR = Path(__file__).parent

# 自动加载 .env 文件
load_dotenv(BASE_DIR / ".env")

TZ = ZoneInfo("Asia/Shanghai")

# ── SSH 配置 ─────────────────────────────────────────────────────────────────

SSH_HOST = os.getenv("SSH_HOST", "")
SSH_USER = os.getenv("SSH_USER", "")
SSH_PASSWORD = os.getenv("SSH_PASSWORD", "")
SSH_PORT = int(os.getenv("SSH_PORT", "22"))

# 服务器上各平台路径
REMOTE_PATHS = {
    "kms": {
        "base": "/root/kms",
        "campaigns_dir": "/root/kms/campaigns",
        "monitor_script": "kms_monitor.py",
        "scan_script": "kms_scan.py",
        "orchestrator": "orchestrator.py",
        "sort_script": "kms_sort.py",
    },
    "k4": {
        "base": "/root/k4",
        "campaigns_dir": "/root/k4/campaigns",
        "events_dir": "/root/k4/events",
        "monitor_script": "k4_monitor.py",
        "scan_script": "k4_scan.py",
        "orchestrator": "orchestrator.py",
        "sort_script": "k4_sort.py",
    },
    "imini": {
        "base": "/root/imini",
        "monitors_dir": "/root/imini/monitors",
        "events_dir": "/root/imini/events",
        "scan_log": "/root/imini/scan.log",
        "monitor_script": "imini_monitor.py",
        "scan_script": "imini_scan.py",
        "orchestrator": "orchestrator.py",
        "sort_script": "imini_sort.py",
    },
}

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

# ── 认证 ──────────────────────────────────────────────────────────────────────

AUTH_TOKEN = os.getenv("DASHBOARD_TOKEN", "")


@app.before_request
def check_auth():
    if not AUTH_TOKEN:
        return
    if request.path in ("/", "/login") and request.method == "GET":
        return
    if request.path.startswith("/static/"):
        return
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {AUTH_TOKEN}":
        abort(401)


# ── SSH 工具函数 ─────────────────────────────────────────────────────────────

_ssh_lock = threading.Lock()


def _ssh_base_args() -> list[str]:
    return ["sshpass", "-p", SSH_PASSWORD, "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{SSH_USER}@{SSH_HOST}"]


def ssh_exec(cmd: str, timeout: int = 30) -> tuple[str, str, int]:
    """执行远程命令，返回 (stdout, stderr, exit_code)"""
    with _ssh_lock:
        try:
            result = subprocess.run(
                _ssh_base_args() + [cmd],
                capture_output=True, text=True, timeout=timeout,
            )
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            return "", "timeout", -1
        except Exception as e:
            return "", str(e), -1


def ssh_read_file(path: str) -> str | None:
    """读取远程文件内容，失败返回 None"""
    out, err, rc = ssh_exec(f"cat '{path}' 2>/dev/null", timeout=15)
    if rc == 0:
        return out
    return None


def ssh_read_json(path: str) -> dict:
    """读取远程 JSON 文件"""
    content = ssh_read_file(path)
    if content is None:
        return {}
    try:
        return json.loads(content)
    except Exception:
        return {}


def ssh_list_dir(path: str) -> list[str]:
    """列出远程目录下的子项名称"""
    out, _, rc = ssh_exec(f"ls -1 '{path}' 2>/dev/null", timeout=10)
    if rc != 0:
        return []
    return [x for x in out.strip().splitlines() if x]


def ssh_stat_size(path: str) -> int:
    """获取远程文件大小，失败返回 0"""
    out, _, rc = ssh_exec(f"stat -c %s '{path}' 2>/dev/null || echo 0", timeout=5)
    try:
        return int(out.strip())
    except ValueError:
        return 0


def ssh_scp_download(remote_path: str, local_path: str) -> bool:
    """用 scp 下载远程文件到本地"""
    try:
        result = subprocess.run(
            ["sshpass", "-p", SSH_PASSWORD, "scp",
             "-o", "StrictHostKeyChecking=no",
             f"{SSH_USER}@{SSH_HOST}:{remote_path}", local_path],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


# ── 文件 token 映射 ────────────────────────────────────────────────────────

_file_tokens: dict[str, dict] = {}  # token → {"remote_path": str, "is_jsonl": bool}
_token_counter = 0
MAX_TOKENS = 500


def _get_file_token(remote_path: str, is_jsonl: bool = False) -> str:
    """为远程文件路径生成唯一 token"""
    global _token_counter
    for tok, info in _file_tokens.items():
        if info["remote_path"] == remote_path:
            info["is_jsonl"] = is_jsonl
            return tok
    while len(_file_tokens) >= MAX_TOKENS:
        oldest = next(iter(_file_tokens))
        del _file_tokens[oldest]
    _token_counter += 1
    tok = hashlib.md5(f"{remote_path}_{_token_counter}".encode()).hexdigest()[:12]
    _file_tokens[tok] = {"remote_path": remote_path, "is_jsonl": is_jsonl}
    return tok


def _resolve_file_token(token: str) -> dict | None:
    return _file_tokens.get(token)


# ── 状态缓存 ──────────────────────────────────────────────────────────────────

_status_cache: dict = {"data": None, "ts": 0.0}
_STATUS_TTL = 10  # 秒


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _parse_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            if fmt is None:
                dt = datetime.fromisoformat(raw)
            else:
                dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            return dt
        except (ValueError, TypeError):
            continue
    return None


def _remaining_text(dt: datetime | None) -> str:
    if dt is None:
        return "未设截止时间"
    now = datetime.now(TZ)
    delta = dt - now
    if delta.total_seconds() <= 0:
        return "已截止"
    d, s = delta.days, delta.seconds
    h, m = s // 3600, (s % 3600) // 60
    if d:
        return f"{d}天{h}小时"
    if h:
        return f"{h}小时{m}分钟"
    return f"{m}分钟"


# ── 批量数据获取（远程执行 Python 一次性收集所有平台数据） ───────────────────

_REMOTE_COLLECTOR_SCRIPT = r'''
import json, os, subprocess, re
from datetime import datetime, timezone, timedelta
from pathlib import Path

HEARTBEAT_STALE_MINUTES = 10

def get_active_pids():
    """获取所有监控进程的 PID 集合"""
    try:
        out = subprocess.check_output(["ps", "aux"], text=True)
        pids = set()
        for line in out.splitlines():
            if "_monitor" in line and "python" in line and "grep" not in line:
                parts = line.split()
                if parts:
                    try: pids.add(int(parts[1]))
                    except ValueError: pass
        return pids
    except:
        return set()

def _is_heartbeat_alive(hb_updated_at: str) -> bool:
    """判断心跳是否在 HEARTBEAT_STALE_MINUTES 分钟内"""
    if not hb_updated_at:
        return False
    try:
        dt = datetime.fromisoformat(hb_updated_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        now = datetime.now(timezone.utc)
        return (now - dt).total_seconds() < HEARTBEAT_STALE_MINUTES * 60
    except:
        return False

def collect():
    active_pids = get_active_pids()
    result = {"kms": [], "k4": [], "imini": []}

    # KMS
    kms_dir = Path("/root/kms/campaigns")
    if kms_dir.exists():
        for folder in sorted(kms_dir.iterdir()):
            if not folder.is_dir(): continue
            cfg_path = folder / "campaign.json"
            if not cfg_path.exists(): continue
            try: cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            except: continue

            pid = 0
            pid_path = folder / "runtime" / "monitor.pid"
            if pid_path.exists():
                try: pid = int(pid_path.read_text().strip())
                except: pass

            hb = {}
            hb_path = folder / "runtime" / "heartbeat.json"
            if hb_path.exists():
                try: hb = json.loads(hb_path.read_text())
                except: pass

            log_files = []
            # stock.log
            p = folder / "logs" / "stock.log"
            if p.exists() and p.stat().st_size > 0:
                log_files.append({"name": "logs/stock.log", "path": str(p), "size": p.stat().st_size})
            # heartbeat.log
            p = folder / "logs" / "heartbeat.log"
            if p.exists() and p.stat().st_size > 0:
                log_files.append({"name": "logs/heartbeat.log", "path": str(p), "size": p.stat().st_size})
            # exports
            exports_dir = folder / "exports"
            if exports_dir.exists():
                for f in sorted(exports_dir.iterdir()):
                    if f.is_file() and f.stat().st_size > 0:
                        log_files.append({"name": f"exports/{f.name}", "path": str(f), "size": f.stat().st_size})

            result["kms"].append({
                "campaign_id": cfg.get("campaign_id", folder.name),
                "name": cfg.get("name", folder.name),
                "prod_ids": cfg.get("prod_ids", []),
                "started_at": cfg.get("scanned_at", ""),
                "stop_monitor_at": cfg.get("stop_monitor_at", ""),
                "publish_time": cfg.get("publish_time", ""),
                "pid": pid,
                "running": pid in active_pids or _is_heartbeat_alive(hb.get("updated_at", "")),
                "heartbeat": hb.get("updated_at", ""),
                "log_files": log_files,
                "site_url": f"https://kms.kmstation.net/prod/prodInfo?prodId={cfg.get('prod_ids', [''])[0]}" if cfg.get("prod_ids") else "",
            })

    # K4
    k4_dir = Path("/root/k4/campaigns")
    loaded_k4_ids = set()
    if k4_dir.exists():
        for folder in sorted(k4_dir.iterdir()):
            if not folder.is_dir(): continue
            cfg_path = folder / "campaign.json"
            if not cfg_path.exists(): continue
            try: cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            except: continue

            cid = cfg.get("campaign_id", folder.name)
            loaded_k4_ids.add(cid)

            pid = 0
            pid_path = folder / "runtime" / "monitor.pid"
            if pid_path.exists():
                try: pid = int(pid_path.read_text().strip())
                except: pass

            hb = {}
            hb_name = f"{folder.name}_heartbeat.json"
            hb_path = folder / "runtime" / hb_name
            if hb_path.exists():
                try: hb = json.loads(hb_path.read_text())
                except: pass

            log_files = []
            logs_dir = folder / "logs"
            if logs_dir.exists():
                for f in sorted(logs_dir.iterdir()):
                    if f.is_file() and f.stat().st_size > 0:
                        log_files.append({"name": f"logs/{f.name}", "path": str(f), "size": f.stat().st_size})
            exports_dir = folder / "exports"
            if exports_dir.exists():
                for f in sorted(exports_dir.iterdir()):
                    if f.is_file() and f.stat().st_size > 0:
                        log_files.append({"name": f"exports/{f.name}", "path": str(f), "size": f.stat().st_size})

            result["k4"].append({
                "campaign_id": cid,
                "name": cfg.get("name", folder.name),
                "events": list(cfg.get("events", {}).keys()),
                "event_urls": {k: v if isinstance(v, list) else [v] for k, v in cfg.get("events", {}).items()},
                "started_at": cfg.get("scanned_at", ""),
                "stop_monitor_at": cfg.get("stop_monitor_at", ""),
                "pid": pid,
                "running": pid in active_pids or _is_heartbeat_alive(hb.get("updated_at", "")),
                "heartbeat": hb.get("updated_at", ""),
                "log_files": log_files,
                "site_url": "",
            })

    # K4 events/ 目录扫描记录
    k4_events_dir = Path("/root/k4/events")
    if k4_events_dir.exists():
        for ef in sorted(k4_events_dir.glob("*.json")):
            try: scan_data = json.loads(ef.read_text(encoding="utf-8-sig"))
            except: continue
            items = scan_data if isinstance(scan_data, list) else [scan_data]
            for item in items:
                if not isinstance(item, dict): continue
                cid = item.get("campaign_id", "")
                if cid in loaded_k4_ids: continue
                loaded_k4_ids.add(cid)
                result["k4"].append({
                    "campaign_id": cid,
                    "name": item.get("name", ""),
                    "events": list(item.get("events", {}).keys()),
                    "event_urls": {k: v[0] if isinstance(v, list) and v else "" for k, v in item.get("events", {}).items()},
                    "started_at": item.get("scanned_at", ""),
                    "stop_monitor_at": item.get("stop_monitor_at", ""),
                    "pid": 0,
                    "running": False,
                    "heartbeat": "",
                    "log_files": [],
                    "is_scan_only": True,
                    "site_url": "",
                })

    # imini
    imini_dir = Path("/root/imini/monitors")
    loaded_imini_ids = set()
    if imini_dir.exists():
        for folder in sorted(imini_dir.iterdir()):
            if not folder.is_dir(): continue
            info_path = folder / "info.json"
            if not info_path.exists(): continue
            try: info = json.loads(info_path.read_text(encoding="utf-8-sig"))
            except: continue

            pid = info.get("pid", 0)
            log_files = []
            for f in sorted(folder.glob("*.log")):
                if f.is_file() and f.stat().st_size > 0:
                    log_files.append({"name": f.name, "path": str(f), "size": f.stat().st_size})
            # 同级合并排序文件
            sort_file = imini_dir / f"{folder.name.rsplit('_', 1)[0]}_合并排序.log"
            if sort_file.exists() and sort_file.is_file() and sort_file.stat().st_size > 0:
                log_files.append({"name": f"sort/{sort_file.name}", "path": str(sort_file), "size": sort_file.stat().st_size})

            pro_id = info.get("pro_id", "")
            loaded_imini_ids.add(pro_id)

            result["imini"].append({
                "campaign_id": folder.name,
                "name": info.get("name", folder.name),
                "pro_id": pro_id,
                "pro_ids": info.get("pro_ids", ""),
                "started_at": info.get("started_at", ""),
                "stop_monitor_at": info.get("end_time", ""),
                "pid": pid,
                "running": pid in active_pids or _is_heartbeat_alive(info.get("heartbeat_at", "")),
                "log_files": log_files,
                "price": info.get("price", ""),
                "site_url": f"https://www.imini.tv/wap_xingjunwenhua.html?pro_id={pro_id}" if pro_id else "",
            })

    # imini scan.log
    scan_log = Path("/root/imini/scan.log")
    if scan_log.exists():
        import re
        text = scan_log.read_text(encoding="utf-8", errors="replace")
        pattern = re.compile(
            r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+\[新\]\s+\[(\S+)\]\s+(.+?)\n'
            r'\s+pro_id=(\d+)\s*\|\s*状态=(\S+)\s*\|\s*价格=([^\|]*)\|\s*截止=(.+?)(?:\n|$)'
        )
        for m in pattern.finditer(text):
            scanned_at, event_type, name, pro_id, status, price, deadline = m.groups()
            if pro_id in loaded_imini_ids: continue
            loaded_imini_ids.add(pro_id)
            result["imini"].append({
                "campaign_id": pro_id,
                "name": name.strip(),
                "pro_id": pro_id,
                "event_type": event_type,
                "started_at": scanned_at,
                "stop_monitor_at": deadline.strip(),
                "pid": 0,
                "running": False,
                "log_files": [],
                "status_text": status,
                "price": price.strip(),
                "is_scan_only": True,
                "site_url": f"https://www.imini.tv/wap_xingjunwenhua.html?pro_id={pro_id}",
            })

    return result

print(json.dumps(collect(), ensure_ascii=False))
'''

_batch_cache: dict = {"data": None, "ts": 0.0}
_BATCH_TTL = 8  # 秒


def _get_remote_batch() -> dict:
    """执行远程收集脚本并返回所有平台数据（带缓存）"""
    now = time.time()
    cached = _batch_cache.get("data")
    if cached and (now - _batch_cache["ts"]) < _BATCH_TTL:
        return cached

    # 将收集脚本写入临时文件并上传执行
    import base64
    b64 = base64.b64encode(_REMOTE_COLLECTOR_SCRIPT.encode("utf-8")).decode()
    cmd = f"echo '{b64}' | base64 -d | python3 - 2>/dev/null"
    out, err, rc = ssh_exec(cmd, timeout=60)

    if rc == 0 and out.strip():
        try:
            data = json.loads(out.strip())
            _batch_cache["data"] = data
            _batch_cache["ts"] = now
            return data
        except json.JSONDecodeError:
            pass

    # 降级：返回缓存或空
    return cached or {"kms": [], "k4": [], "imini": []}


# ── KMS 活动（远程） ─────────────────────────────────────────────────────────

def _kms_campaigns() -> list[dict]:
    results = []
    pconf = REMOTE_PATHS["kms"]

    # 尝试从批量数据中获取
    batch = _get_remote_batch()
    if "kms" in batch:
        for item in batch["kms"]:
            stop_at = _parse_dt(item.get("stop_monitor_at", ""))
            log_files = []
            for lf in item.get("log_files", []):
                log_files.append({
                    "name": lf["name"],
                    "token": _get_file_token(lf["path"]),
                    "size": lf["size"],
                    "is_jsonl": False,
                })
            results.append({
                "platform": "kms",
                "campaign_id": item["campaign_id"],
                "name": item["name"],
                "prod_ids": item.get("prod_ids", []),
                "started_at": item.get("started_at", ""),
                "stop_monitor_at": item.get("stop_monitor_at", ""),
                "publish_time": item.get("publish_time", ""),
                "remaining": _remaining_text(stop_at),
                "pid": item["pid"],
                "running": item["running"],
                "heartbeat": item.get("heartbeat", ""),
                "log_files": log_files,
                "site_url": item.get("site_url", ""),
            })

    return results


# ── K4 活动（远程） ──────────────────────────────────────────────────────────

def _k4_campaigns() -> list[dict]:
    results = []
    batch = _get_remote_batch()
    if "k4" in batch:
        for item in batch["k4"]:
            stop_at = _parse_dt(item.get("stop_monitor_at", ""))
            log_files = []
            for lf in item.get("log_files", []):
                log_files.append({
                    "name": lf["name"],
                    "token": _get_file_token(lf["path"]),
                    "size": lf["size"],
                    "is_jsonl": False,
                })
            results.append({
                "platform": "k4",
                "campaign_id": item["campaign_id"],
                "name": item["name"],
                "events": item.get("events", []),
                "started_at": item.get("started_at", ""),
                "stop_monitor_at": item.get("stop_monitor_at", ""),
                "remaining": _remaining_text(stop_at),
                "pid": item["pid"],
                "running": item["running"],
                "heartbeat": item.get("heartbeat", ""),
                "log_files": log_files,
                "is_scan_only": item.get("is_scan_only", False),
                "site_url": item.get("site_url", ""),
                "event_urls": item.get("event_urls", {}),
            })

    return results


# ── imini 活动（远程） ──────────────────────────────────────────────────────

def _imini_campaigns() -> list[dict]:
    results = []
    batch = _get_remote_batch()
    if "imini" in batch:
        for item in batch["imini"]:
            stop_at = _parse_dt(item.get("stop_monitor_at", ""))
            log_files = []
            for lf in item.get("log_files", []):
                log_files.append({
                    "name": lf["name"],
                    "token": _get_file_token(lf["path"]),
                    "size": lf["size"],
                    "is_jsonl": False,
                })
            results.append({
                "platform": "imini",
                "campaign_id": item["campaign_id"],
                "name": item["name"],
                "pro_id": item.get("pro_id", ""),
                "pro_ids": item.get("pro_ids", ""),
                "started_at": item.get("started_at", ""),
                "stop_monitor_at": item.get("stop_monitor_at", ""),
                "remaining": _remaining_text(stop_at),
                "pid": item["pid"],
                "running": item["running"],
                "log_files": log_files,
                "price": item.get("price", ""),
                "event_type": item.get("event_type", ""),
                "status_text": item.get("status_text", ""),
                "is_scan_only": item.get("is_scan_only", False),
                "site_url": item.get("site_url", ""),
            })

    return results


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", auth_token=AUTH_TOKEN)


@app.route("/api/status")
def api_status():
    now = time.time()
    cached = _status_cache.get("data")
    if cached and (now - _status_cache["ts"]) < _STATUS_TTL:
        return jsonify(cached)

    all_campaigns = _kms_campaigns() + _k4_campaigns() + _imini_campaigns()

    def _is_expired(c):
        """根据截止时间判断是否过期"""
        stop = c.get("stop_monitor_at", "")
        if not stop:
            return True  # 无截止时间视为过期
        try:
            stop_dt = datetime.fromisoformat(stop)
            if stop_dt.tzinfo is None:
                stop_dt = stop_dt.replace(tzinfo=timezone(timedelta(hours=8)))
            return datetime.now(timezone.utc) >= stop_dt
        except:
            return True

    active = [c for c in all_campaigns if not _is_expired(c)]
    expired = [c for c in all_campaigns if _is_expired(c)]

    by_platform: dict[str, dict] = {}
    for c in all_campaigns:
        p = c["platform"]
        by_platform.setdefault(p, {"active": 0, "total": 0})
        by_platform[p]["total"] += 1
        if not _is_expired(c):
            by_platform[p]["active"] += 1

    result = {
        "active": active,
        "expired": expired,
        "stats": {
            "active_count": len(active),
            "expired_count": len(expired),
            "by_platform": by_platform,
        },
    }

    _status_cache["data"] = result
    _status_cache["ts"] = now

    return jsonify(result)


@app.route("/api/log/<token>")
def api_log(token):
    """下载远程日志文件"""
    info = _resolve_file_token(token)
    if not info:
        return jsonify({"error": "无效的文件 token"}), 403

    remote_path = info["remote_path"]
    filename = os.path.basename(remote_path)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}")
    tmp_path = tmp.name
    tmp.close()

    ok = ssh_scp_download(remote_path, tmp_path)
    if not ok:
        return jsonify({"error": "文件下载失败"}), 404

    return send_file(tmp_path, as_attachment=True, download_name=filename)


def _preview_xlsx(remote_path: str):
    """通过远程 openpyxl 解析 xlsx，返回按成员分 sheet 的结构化数据"""
    script = f'''
import json, sys
import openpyxl
wb = openpyxl.load_workbook("{remote_path}", read_only=True)
result = []
for sn in wb.sheetnames:
    ws = wb[sn]
    rows = []
    for row in ws.iter_rows(values_only=True):
        rows.append([str(c) if c is not None else "" for c in row])
    result.append({{"name": sn, "rows": rows}})
wb.close()
print(json.dumps(result, ensure_ascii=False))
'''
    import base64
    b64 = base64.b64encode(script.encode()).decode()
    out, _, rc = ssh_exec(f"echo '{b64}' | base64 -d | python3", timeout=30)
    if rc != 0 or not out.strip():
        return jsonify({"error": "Excel 解析失败", "format": "binary", "filename": remote_path.rsplit("/", 1)[-1]})
    try:
        sheets = json.loads(out.strip())
    except json.JSONDecodeError:
        return jsonify({"error": "Excel 解析失败", "format": "binary", "filename": remote_path.rsplit("/", 1)[-1]})

    return jsonify({"format": "xlsx", "sheets": sheets, "filename": remote_path.rsplit("/", 1)[-1]})


@app.route("/api/log_preview/<token>")
def api_log_preview(token):
    """在线预览日志"""
    info = _resolve_file_token(token)
    if not info:
        return jsonify({"error": "无效的文件 token"}), 403

    remote_path = info["remote_path"]

    # xlsx 文件：远程 openpyxl 解析后返回结构化数据
    if remote_path.endswith((".xlsx", ".xls")):
        return _preview_xlsx(remote_path)

    # 其他二进制格式不支持预览
    if remote_path.endswith((".zip", ".gz", ".rar")):
        return jsonify({"format": "binary", "filename": remote_path.rsplit("/", 1)[-1]})

    # 读取前 500 行
    out, _, rc = ssh_exec(f"head -500 '{remote_path}' 2>/dev/null", timeout=15)
    if rc != 0:
        return jsonify({"error": "文件读取失败"}), 404

    # 获取总行数
    wc_out, _, _ = ssh_exec(f"wc -l '{remote_path}' 2>/dev/null", timeout=5)
    try:
        total_lines = int(wc_out.strip().split()[0])
    except Exception:
        total_lines = 500

    is_jsonl = info.get("is_jsonl", False)

    if is_jsonl:
        records = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                records.append({"_raw": line})
        return jsonify({
            "format": "jsonl",
            "records": records,
            "total": total_lines,
        })

    return jsonify({
        "format": "text",
        "lines": out.splitlines()[:500],
        "total": total_lines,
    })


@app.route("/api/report/<platform>/<path:campaign_id>")
def api_report(platform, campaign_id):
    """在远程服务器上执行排序脚本"""
    REPORT_TIMEOUT = 120

    try:
        def _generate_report():
            if platform == "kms":
                pconf = REMOTE_PATHS["kms"]
                config_path = f"{pconf['campaigns_dir']}/{campaign_id}/campaign.json"
                cmd = f"cd {pconf['base']} && python3 {pconf['sort_script']} --config {config_path} 2>&1"
                out, _, rc = ssh_exec(cmd, timeout=REPORT_TIMEOUT)
                return {"text": out, "xlsx_token": None}, 200 if rc == 0 else 500

            elif platform == "k4":
                pconf = REMOTE_PATHS["k4"]
                config_path = f"{pconf['campaigns_dir']}/{campaign_id}/campaign.json"
                cmd = f"cd {pconf['base']} && python3 {pconf['sort_script']} --config {config_path} 2>&1"
                out, _, rc = ssh_exec(cmd, timeout=REPORT_TIMEOUT)
                return {"text": out, "xlsx_token": None}, 200 if rc == 0 else 500

            elif platform == "imini":
                pconf = REMOTE_PATHS["imini"]
                folder_path = f"{pconf['monitors_dir']}/{campaign_id}"
                cmd = f"cd {pconf['base']} && python3 {pconf['sort_script']} --folder {folder_path} 2>&1"
                out, _, rc = ssh_exec(cmd, timeout=REPORT_TIMEOUT)
                return {"text": out, "xlsx_token": None}, 200 if rc == 0 else 500

            return {"error": "未知平台"}, 400

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_generate_report)
            try:
                data, status_code = future.result(timeout=REPORT_TIMEOUT)
                return jsonify(data), status_code
            except FuturesTimeoutError:
                return jsonify({"error": f"排序报告生成超时（{REPORT_TIMEOUT}s）"}), 504

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scan/<platform>", methods=["POST"])
def api_scan(platform):
    """触发远程扫描"""
    pconf = REMOTE_PATHS.get(platform)
    if not pconf:
        return jsonify({"error": "未知平台"}), 400
    try:
        cmd = f"cd {pconf['base']} && python3 {pconf.get('orchestrator', 'orchestrator.py')} 2>&1"
        out, err, rc = ssh_exec(cmd, timeout=120)
        return jsonify({
            "success": rc == 0,
            "stdout": out[-2000:] if out else "",
            "stderr": err[-2000:] if err else "",
            "returncode": rc,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/action/<platform>/<path:campaign_id>/<action>", methods=["POST"])
def api_action(platform, campaign_id, action):
    """启动/停止远程监控"""
    try:
        if platform == "kms":
            pconf = REMOTE_PATHS["kms"]
            config_path = f"{pconf['campaigns_dir']}/{campaign_id}/campaign.json"

            if action == "start":
                # 后台启动监控
                cmd = (f"cd {pconf['base']} && nohup python3 -u {pconf['monitor_script']} "
                       f"--config {config_path} > /dev/null 2>&1 & echo $!")
                out, _, rc = ssh_exec(cmd, timeout=10)
                if rc == 0 and out.strip():
                    return jsonify({"success": True, "pid": int(out.strip())})
                return jsonify({"success": False, "error": out[-500:]})

            elif action == "stop":
                pid_str = ssh_read_file(f"{pconf['campaigns_dir']}/{campaign_id}/runtime/monitor.pid")
                pid = 0
                try:
                    pid = int(pid_str.strip()) if pid_str else 0
                except ValueError:
                    pass
                if pid:
                    ssh_exec(f"kill {pid} 2>/dev/null", timeout=5)
                    ssh_exec(f"echo '' > {pconf['campaigns_dir']}/{campaign_id}/runtime/monitor.pid", timeout=5)
                    return jsonify({"success": True})
                return jsonify({"success": False, "error": "无 PID"})

        elif platform == "k4":
            pconf = REMOTE_PATHS["k4"]
            config_path = f"{pconf['campaigns_dir']}/{campaign_id}/campaign.json"

            if action == "start":
                cmd = (f"cd {pconf['base']} && nohup python3 -u {pconf['monitor_script']} "
                       f"--config {config_path} > /dev/null 2>&1 & echo $!")
                out, _, rc = ssh_exec(cmd, timeout=10)
                if rc == 0 and out.strip():
                    return jsonify({"success": True, "pid": int(out.strip())})
                return jsonify({"success": False, "error": out[-500:]})

            elif action == "stop":
                pid_str = ssh_read_file(f"{pconf['campaigns_dir']}/{campaign_id}/runtime/monitor.pid")
                pid = 0
                try:
                    pid = int(pid_str.strip()) if pid_str else 0
                except ValueError:
                    pass
                if pid:
                    ssh_exec(f"kill {pid} 2>/dev/null", timeout=5)
                    ssh_exec(f"echo '' > {pconf['campaigns_dir']}/{campaign_id}/runtime/monitor.pid", timeout=5)
                    return jsonify({"success": True})
                return jsonify({"success": False, "error": "无 PID"})

        elif platform == "imini":
            pconf = REMOTE_PATHS["imini"]
            folder_path = f"{pconf['monitors_dir']}/{campaign_id}"
            info = ssh_read_json(f"{folder_path}/info.json")

            if action == "start":
                pro_ids = info.get("pro_ids", info.get("pro_id", ""))
                sources = info.get("sources", "")
                end_time = info.get("end_time", "")
                cmd = (f"cd {pconf['base']} && nohup python3 -u {pconf['monitor_script']} "
                       f"--pro_id {pro_ids} --folder {folder_path} "
                       f"--sources '{sources}' --end_time '{end_time}' "
                       f"> /dev/null 2>&1 & echo $!")
                out, _, rc = ssh_exec(cmd, timeout=10)
                if rc == 0 and out.strip():
                    return jsonify({"success": True, "pid": int(out.strip())})
                return jsonify({"success": False, "error": out[-500:]})

            elif action == "stop":
                pid = info.get("pid", 0)
                if pid:
                    ssh_exec(f"kill {pid} 2>/dev/null", timeout=5)
                    info["pid"] = 0
                    import base64
                    b64 = base64.b64encode(json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")).decode()
                    ssh_exec(f"echo '{b64}' | base64 -d > '{folder_path}/info.json'", timeout=10)
                    return jsonify({"success": True})
                return jsonify({"success": False, "error": "进程未运行"})

        return jsonify({"error": "不支持的操作"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/deadline/<platform>/<path:campaign_id>", methods=["POST"])
def api_set_deadline(platform, campaign_id):
    """设置截止时间（远程修改配置文件）"""
    data = request.get_json(force=True)
    deadline = data.get("deadline", "").strip()
    if not deadline:
        return jsonify({"error": "截止时间不能为空"}), 400

    try:
        if platform == "kms":
            cfg_path = f"{REMOTE_PATHS['kms']['campaigns_dir']}/{campaign_id}/campaign.json"
            cfg = ssh_read_json(cfg_path)
            if not cfg:
                return jsonify({"success": False, "error": f"读取远程配置失败: {cfg_path}"})
            cfg["stop_monitor_at"] = deadline
            # 用 base64 编码避免特殊字符问题（与 k4/imini 统一）
            import base64
            b64 = base64.b64encode(json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")).decode()
            out, err, rc = ssh_exec(
                f"echo '{b64}' | base64 -d > '{cfg_path}'",
                timeout=10
            )
            if rc != 0:
                return jsonify({"success": False, "error": f"SSH 写入失败 (rc={rc}): {err[:200]}"})
            return jsonify({"success": True})

        elif platform == "k4":
            cfg_path = f"{REMOTE_PATHS['k4']['campaigns_dir']}/{campaign_id}/campaign.json"
            cfg = ssh_read_json(cfg_path)
            if not cfg:
                return jsonify({"success": False, "error": f"读取远程配置失败: {cfg_path}"})
            cfg["stop_monitor_at"] = deadline
            tmp_json = json.dumps(cfg, ensure_ascii=False, indent=2)
            import base64
            b64 = base64.b64encode(tmp_json.encode("utf-8")).decode()
            out, err, rc = ssh_exec(
                f"echo '{b64}' | base64 -d > '{cfg_path}'",
                timeout=10
            )
            if rc != 0:
                return jsonify({"success": False, "error": f"SSH 写入失败 (rc={rc}): {err[:200]}"})
            return jsonify({"success": True})

        elif platform == "imini":
            info_path = f"{REMOTE_PATHS['imini']['monitors_dir']}/{campaign_id}/info.json"
            info = ssh_read_json(info_path)
            if not info:
                return jsonify({"success": False, "error": f"读取远程配置失败: {info_path}"})
            info["end_time"] = deadline
            tmp_json = json.dumps(info, ensure_ascii=False, indent=2)
            import base64
            b64 = base64.b64encode(tmp_json.encode("utf-8")).decode()
            out, err, rc = ssh_exec(
                f"echo '{b64}' | base64 -d > '{info_path}'",
                timeout=10
            )
            if rc != 0:
                return jsonify({"success": False, "error": f"SSH 写入失败 (rc={rc}): {err[:200]}"})
            return jsonify({"success": True})

        elif platform == "k4":
            cfg_path = f"{REMOTE_PATHS['k4']['campaigns_dir']}/{campaign_id}/campaign.json"
            cfg = ssh_read_json(cfg_path)
            cfg["stop_monitor_at"] = deadline
            # 用临时文件方式写入
            tmp_json = json.dumps(cfg, ensure_ascii=False, indent=2)
            # base64 编码避免特殊字符问题
            import base64
            b64 = base64.b64encode(tmp_json.encode("utf-8")).decode()
            out, _, rc = ssh_exec(
                f"echo '{b64}' | base64 -d > '{cfg_path}'",
                timeout=10
            )
            return jsonify({"success": rc == 0})

        elif platform == "imini":
            info_path = f"{REMOTE_PATHS['imini']['monitors_dir']}/{campaign_id}/info.json"
            info = ssh_read_json(info_path)
            info["end_time"] = deadline
            tmp_json = json.dumps(info, ensure_ascii=False, indent=2)
            import base64
            b64 = base64.b64encode(tmp_json.encode("utf-8")).decode()
            out, _, rc = ssh_exec(
                f"echo '{b64}' | base64 -d > '{info_path}'",
                timeout=10
            )
            return jsonify({"success": rc == 0})

        return jsonify({"error": "未知平台"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/delete/<platform>/<path:campaign_id>", methods=["POST"])
def api_delete(platform, campaign_id):
    """删除远程服务器上的活动数据"""
    try:
        if platform == "kms":
            folder = f"{REMOTE_PATHS['kms']['campaigns_dir']}/{campaign_id}"
            # 先检查是否存在
            out, _, rc = ssh_exec(f"test -d '{folder}' && echo exists", timeout=5)
            if "exists" not in out:
                return jsonify({"success": False, "error": "活动目录不存在"}), 404
            # 如果有监控进程先停止
            pid_str = ssh_read_file(f"{folder}/runtime/monitor.pid")
            if pid_str:
                try:
                    pid = int(pid_str.strip())
                    if pid:
                        ssh_exec(f"kill {pid} 2>/dev/null", timeout=5)
                except ValueError:
                    pass
            ssh_exec(f"rm -rf '{folder}'", timeout=15)

        elif platform == "k4":
            folder = f"{REMOTE_PATHS['k4']['campaigns_dir']}/{campaign_id}"
            out, _, rc = ssh_exec(f"test -d '{folder}' && echo exists", timeout=5)
            if "exists" in out:
                # 有文件夹的正式监控：先停进程再删目录
                pid_str = ssh_read_file(f"{folder}/runtime/monitor.pid")
                if pid_str:
                    try:
                        pid = int(pid_str.strip())
                        if pid:
                            ssh_exec(f"kill {pid} 2>/dev/null", timeout=5)
                    except ValueError:
                        pass
                ssh_exec(f"rm -rf '{folder}'", timeout=15)
            else:
                # scan_only 记录：从 events/*.json 中删除对应 campaign_id 的条目
                events_dir = REMOTE_PATHS['k4'].get("events_dir", "/root/k4/events")
                safe_id = campaign_id.replace("'", "'\\''")
                script = (
                    "import json,os,glob\n"
                    f"for f in glob.glob('{events_dir}/k4_scan_*.json'):\n"
                    "    data=json.load(open(f))\n"
                    f"    data=[x for x in data if x.get('campaign_id','')!='{safe_id}']\n"
                    "    json.dump(data,open(f,'w'),ensure_ascii=False,indent=2)\n"
                )
                # 用 stdin pipe 传脚本，避免 shell 截断长命令
                result = subprocess.run(
                    _ssh_base_args() + ["python3 -"],
                    input=script, capture_output=True, text=True, timeout=15,
                )
                if result.returncode != 0:
                    return jsonify({"success": False, "error": f"远程删除失败: {result.stderr[:200]}"})

        elif platform == "imini":
            folder = f"{REMOTE_PATHS['imini']['monitors_dir']}/{campaign_id}"
            out, _, rc = ssh_exec(f"test -d '{folder}' && echo exists", timeout=5)
            if "exists" in out:
                # 有文件夹的正式监控：先停进程再删目录
                info = ssh_read_json(f"{folder}/info.json")
                pid = info.get("pid", 0)
                if pid:
                    ssh_exec(f"kill {pid} 2>/dev/null", timeout=5)
                ssh_exec(f"rm -rf '{folder}'", timeout=15)
            else:
                # scan_only 记录：没有文件夹，需要从 scan.log 中删除对应行
                import re
                scan_log_path = REMOTE_PATHS["imini"].get("scan_log", "/root/imini/scan.log")
                # 用 sed 删除包含该 pro_id 的扫描记录行（匹配 pro_id=XXX 的行及其上下文）
                safe_id = campaign_id.replace("'", "'\\''")
                ssh_exec(
                    f"sed -i '/pro_id={safe_id}/d' '{scan_log_path}'",
                    timeout=10
                )
        else:
            return jsonify({"success": False, "error": "未知平台"}), 400

        # 清除缓存，强制下次刷新
        _status_cache["data"] = None
        _batch_cache["data"] = None

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "9090"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    print(f"Signing Monitor Dashboard (Remote: {SSH_HOST}): http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
