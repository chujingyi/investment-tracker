import os
from pathlib import Path

BASE_DIR     = Path(__file__).parent
MONITORS_DIR = BASE_DIR / "monitors"
EVENTS_DIR   = BASE_DIR / "events"
STATE_FILE   = BASE_DIR / "known_ids.json"
FEISHU_STATE = BASE_DIR / "feishu_state.json"
ORCH_LOG     = BASE_DIR / "orchestrator.log"

# 飞书应用凭证 — 从环境变量读取，不要硬编码在代码里
FEISHU_APP_ID     = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

# 飞书开发者后台 → 事件订阅 → Verification Token
FEISHU_VERIFY_TOKEN = os.getenv("FEISHU_VERIFY_TOKEN", "")

# bot_server 监听端口
BOT_PORT = int(os.getenv("BOT_PORT", "8080"))
