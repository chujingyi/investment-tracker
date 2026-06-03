"""飞书 API 客户端：获取 token、发送消息、保存用户 ID"""

import json
import time
import urllib.request
from pathlib import Path

from config import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_STATE

_token_cache: dict = {"value": "", "expires_at": 0.0}


# ── Token ─────────────────────────────────────────────────────────────────────

def _get_token() -> str:
    if _token_cache["value"] and _token_cache["expires_at"] > time.time() + 60:
        return _token_cache["value"]
    data = json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        res = json.loads(r.read())
    _token_cache["value"] = res["tenant_access_token"]
    _token_cache["expires_at"] = time.time() + res.get("expire", 7200)
    return _token_cache["value"]


# ── 发消息 ────────────────────────────────────────────────────────────────────

def send(open_id: str, text: str) -> dict:
    token = _get_token()
    data = json.dumps({
        "receive_id": open_id,
        "msg_type":   "text",
        "content":    json.dumps({"text": text}),
    }).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
        data=data,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def notify(text: str):
    """发消息给已绑定的用户，未绑定时仅打印"""
    uid = get_user_id()
    if uid:
        try:
            send(uid, text)
        except Exception as e:
            print(f"[Feishu] 发送失败: {e}")
    else:
        print(f"[Feishu] 未绑定用户，消息未发出:\n{text}")


# ── 用户 ID 持久化 ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    p = Path(FEISHU_STATE)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save_state(state: dict):
    Path(FEISHU_STATE).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_user_id() -> str:
    return _load_state().get("open_id", "")


def save_user_id(open_id: str):
    state = _load_state()
    state["open_id"] = open_id
    _save_state(state)
