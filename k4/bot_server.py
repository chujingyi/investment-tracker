#!/usr/bin/env python3
"""
Feishu webhook service for K4 monitors.

Commands:
  status
  start <campaign_id>
  stop <campaign_id>
  set <campaign_id> YYYY-MM-DD HH:MM
  sort <campaign_id>
  help
"""

import json
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

import feishu
import orchestrator
from config import BOT_PORT, CAMPAIGNS_DIR, FEISHU_VERIFY_TOKEN
from k4_sort import sort_campaign

app = Flask(__name__)


def find_campaign(identifier: str) -> Path | None:
    identifier = identifier.strip()
    for folder in orchestrator.campaign_folders():
        campaign = orchestrator.load_campaign(folder)
        if identifier in {folder.name, campaign.get("campaign_id", "")}:
            return folder
    return None


def normalize_time(value: str) -> str | None:
    value = value.strip()
    if len(value) == 16:
        value += ":00"
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return value
    except ValueError:
        try:
            datetime.fromisoformat(value)
            return value
        except ValueError:
            return None


def handle_command(text: str) -> None:
    text = text.strip()
    lower = text.lower()

    if lower == "status":
        feishu.notify("\n".join(orchestrator.status_lines()))
        return

    if lower.startswith("start "):
        campaign_id = text.split(maxsplit=1)[1]
        folder = find_campaign(campaign_id)
        if folder is None:
            feishu.notify(f"未找到 campaign: {campaign_id}")
            return
        pid = orchestrator.start_monitor(folder)
        feishu.notify(f"K4 监控已启动\ncampaign: {folder.name}\nPID: {pid}")
        return

    if lower.startswith("stop "):
        campaign_id = text.split(maxsplit=1)[1]
        folder = find_campaign(campaign_id)
        if folder is None:
            feishu.notify(f"未找到 campaign: {campaign_id}")
            return
        stopped = orchestrator.stop_monitor(folder)
        feishu.notify(f"{'已停止' if stopped else '监控未在运行'}\ncampaign: {folder.name}")
        return

    if lower.startswith("set "):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            feishu.notify("格式：set <campaign_id> YYYY-MM-DD HH:MM")
            return
        campaign_id, stop_at = parts[1], normalize_time(parts[2])
        if stop_at is None:
            feishu.notify("截止时间格式不对，请使用：set <campaign_id> YYYY-MM-DD HH:MM")
            return
        folder = find_campaign(campaign_id)
        if folder is None:
            feishu.notify(f"未找到 campaign: {campaign_id}")
            return
        campaign = orchestrator.load_campaign(folder)
        campaign["stop_monitor_at"] = stop_at
        orchestrator.save_campaign(folder, campaign)
        pid = orchestrator.start_monitor(folder)
        feishu.notify(f"截止时间已设置，监控已启动\ncampaign: {folder.name}\n截止: {stop_at}\nPID: {pid}")
        return

    if lower.startswith("sort "):
        campaign_id = text.split(maxsplit=1)[1]
        folder = find_campaign(campaign_id)
        if folder is None:
            feishu.notify(f"未找到 campaign: {campaign_id}")
            return
        feishu.notify(sort_campaign(folder / "campaign.json"))
        return

    if lower in {"help", "帮助"}:
        feishu.notify(
            "K4 可用指令：\n\n"
            "status\n  查看所有监控状态\n\n"
            "start <campaign_id>\n  启动或恢复监控\n\n"
            "stop <campaign_id>\n  停止监控\n\n"
            "set <campaign_id> YYYY-MM-DD HH:MM\n  设置截止时间并启动监控\n\n"
            "sort <campaign_id>\n  生成排序报告\n\n"
            "help\n  显示帮助"
        )
        return

    feishu.notify("未知指令，发送 help 查看可用命令")


@app.route("/feishu/webhook", methods=["POST"])
def webhook():
    body = request.json or {}

    if body.get("type") == "url_verification":
        return jsonify({"challenge": body.get("challenge", "")})

    if FEISHU_VERIFY_TOKEN and body.get("header", {}).get("token", "") != FEISHU_VERIFY_TOKEN:
        return jsonify({"code": 1, "msg": "invalid token"}), 403

    header = body.get("header", {})
    event = body.get("event", {})
    if header.get("event_type") == "im.message.receive_v1":
        sender = event.get("sender", {})
        message = event.get("message", {})
        open_id = sender.get("sender_id", {}).get("open_id", "")

        bound_id = feishu.get_user_id()
        if open_id and not bound_id:
            feishu.save_user_id(open_id)
            feishu.notify("K4 已绑定，发送 help 查看可用指令")
            bound_id = open_id

        if open_id != bound_id:
            return jsonify({"code": 0})

        try:
            text = json.loads(message.get("content", "{}")).get("text", "").strip()
        except Exception:
            text = ""

        if text:
            threading.Thread(target=handle_command, args=(text,), daemon=True).start()

    return jsonify({"code": 0})


if __name__ == "__main__":
    Path(CAMPAIGNS_DIR).mkdir(parents=True, exist_ok=True)
    print(f"[K4 Bot] listening on port {BOT_PORT}")
    app.run(host="0.0.0.0", port=BOT_PORT)
