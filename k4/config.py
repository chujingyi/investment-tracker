import os
from pathlib import Path


BASE_DIR = Path(__file__).parent
CAMPAIGNS_DIR = BASE_DIR / "campaigns"
EVENTS_DIR = BASE_DIR / "events"
SCAN_STATE_FILE = BASE_DIR / "known_campaigns.json"
ORCH_LOG = BASE_DIR / "orchestrator.log"
FEISHU_STATE = BASE_DIR / "feishu_state.json"

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_VERIFY_TOKEN = os.getenv("FEISHU_VERIFY_TOKEN", "")

BOT_PORT = int(os.getenv("BOT_PORT", "8080"))
