"""Event layer (P1): scan data changes into events, deduplicate, dispatch tasks.

The SQLite table agent_events acts as a zero-dependency event queue. The ECS
worker calls scan_events() on a schedule; process_events() turns new events
into workflow tasks. Anti-noise principle from the monitor state machine is
carried over: the same state without new evidence is never re-emitted.
"""

import json
from datetime import datetime, timedelta

EVENT_TYPES = {
    "review_due",
    "calendar_soon",
    "price_approaching",
    "manual_research",
    "manual_review",
}

# Cooling windows per event type.
COOLDOWN_HOURS = {
    "review_due": 24 * 30,       # a due review fires at most once a month
    "calendar_soon": 24 * 60,    # an upcoming event is emitted once
    "price_approaching": 24,     # state changes only, at most once a day
    "manual_research": 0,
    "manual_review": 0,
}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def dedup_key(event_type, payload):
    """Stable key for idempotency; state is included for state-change events."""
    rec_id = payload.get("rec_id")
    ticker = payload.get("ticker")
    if event_type == "review_due":
        return f"review_due:rec{rec_id}"
    if event_type == "calendar_soon":
        return f"calendar_soon:event{payload.get('event_id')}"
    if event_type == "price_approaching":
        return f"price_approaching:rec{rec_id}:{payload.get('state')}"
    if event_type == "manual_research":
        return f"manual_research:{ticker}:{payload.get('nonce', 0)}"
    if event_type == "manual_review":
        return f"manual_review:{payload.get('nonce', 0)}"
    return f"{event_type}:{ticker or ''}:{json.dumps(payload, ensure_ascii=False)[:40]}"


def _has_recent_event(conn, key, hours):
    if hours <= 0:
        return False
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT 1 FROM agent_events WHERE dedup_key=? AND status IN ('new','processed','retry') "
        "AND created_at >= ? LIMIT 1",
        (key, cutoff),
    ).fetchone()
    return bool(row)


def insert_event(conn, event_type, payload, nonce=None):
    """Insert an event unless a recent duplicate exists. Returns id or None."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {event_type}")
    if nonce is not None:
        payload = dict(payload or {})
        payload["nonce"] = nonce
    key = dedup_key(event_type, payload or {})
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _has_recent_event(conn, key, COOLDOWN_HOURS.get(event_type, 24)):
            conn.rollback()
            return None
        cur = conn.execute(
            "INSERT INTO agent_events (event_type, payload_json, dedup_key, status, created_at) "
            "VALUES (?,?,?,'new',?)",
            (event_type, json.dumps(payload or {}, ensure_ascii=False), key, _now()),
        )
        event_id = cur.lastrowid
        conn.commit()
        return event_id
    except Exception:
        conn.rollback()
        raise


# ── scanning ─────────────────────────────────────────────────────────────────


def scan_review_due(conn, today=None):
    """Observations whose review date has arrived while still active."""
    today = today or datetime.now().date()
    rows = conn.execute(
        "SELECT id, ticker, validity_date, wait_price FROM recommendations "
        "WHERE status='active' AND entry_rec IN ('watch','wait') "
        "AND validity_date IS NOT NULL AND validity_date <= ?",
        (str(today),),
    ).fetchall()
    created = 0
    for row in rows:
        event_id = insert_event(
            conn,
            "review_due",
            {
                "rec_id": row["id"],
                "ticker": row["ticker"],
                "validity_date": row["validity_date"],
            },
        )
        created += 1 if event_id else 0
    return created


def scan_calendar_soon(conn, today, horizon_days=7):
    """Pending catalysts inside the horizon → calendar event."""
    today = today or datetime.now().date()
    end = str(today + timedelta(days=horizon_days))
    rows = conn.execute(
        "SELECT id, title, event_date, status FROM catalysts "
        "WHERE event_date BETWEEN ? AND ? AND status IN ('pending','upcoming') ORDER BY event_date",
        (str(today), end),
    ).fetchall()
    created = 0
    for row in rows:
        event_id = insert_event(
            conn,
            "calendar_soon",
            {
                "event_id": row["id"],
                "title": row["title"],
                "event_date": row["event_date"],
            },
        )
        created += 1 if event_id else 0
    return created


def scan_price_approaching(conn, today=None):
    """Monitor state changes worth an agent look (approaching/triggered/revalidate)."""
    from app import enrich_rows, evaluate_price_monitor

    today = today or datetime.now().date()
    rows = conn.execute(
        "SELECT * FROM recommendations WHERE status='active' AND monitoring=1"
    ).fetchall()
    if not rows:
        return 0
    enriched = enrich_rows(rows)
    created = 0
    for row in enriched:
        evaluated = evaluate_price_monitor(row, today=today)
        state = evaluated.get("monitor_state")
        previous = conn.execute("SELECT state FROM agent_monitor_states WHERE rec_id=?", (row["id"],)).fetchone()
        if previous and previous["state"] == state:
            continue
        if state not in {"approaching", "triggered", "revalidate"}:
            conn.execute("INSERT OR REPLACE INTO agent_monitor_states (rec_id,state,updated_at) VALUES (?,?,?)",
                         (row["id"], state or "tracking", _now()))
            conn.commit()
            continue
        event_id = insert_event(
            conn,
            "price_approaching",
            {
                "rec_id": row["id"],
                "ticker": row["ticker"],
                "state": state,
                "price": evaluated.get("latest_price"),
                "wait_price": evaluated.get("wait_price"),
                "reason": evaluated.get("monitor_reason") or "",
            },
        )
        if event_id:
            conn.execute("INSERT OR REPLACE INTO agent_monitor_states (rec_id,state,updated_at) VALUES (?,?,?)",
                         (row["id"], state, _now()))
            conn.commit()
        created += 1 if event_id else 0
    return created


def scan_events(conn, today=None):
    """Run all scanners. Returns {type: count} for the worker log."""
    today = today or datetime.now().date()
    return {
        "review_due": scan_review_due(conn, today),
        "calendar_soon": scan_calendar_soon(conn, today),
        "price_approaching": scan_price_approaching(conn, today),
    }


# ── processing ───────────────────────────────────────────────────────────────


EVENT_TO_WORKFLOW = {
    "review_due": "review",
    "calendar_soon": "monitor",
    "price_approaching": "monitor",
    "manual_research": "research",
    "manual_review": "review",
}


def _mark_event(conn, event_id, status, task_id=None, attempt=None):
    if attempt is None:
        conn.execute(
            "UPDATE agent_events SET status=?, task_id=?, processed_at=? WHERE id=?",
            (status, task_id, _now(), event_id),
        )
    else:
        conn.execute(
            "UPDATE agent_events SET status=?, task_id=?, attempt=?, processed_at=? WHERE id=?",
            (status, task_id, attempt, _now(), event_id),
        )
    conn.commit()


def process_events(conn, limit=10):
    """Turn 'new' events into tasks. Returns list of (event_id, task_id)."""
    from agent import tasks, workflow

    rows = conn.execute(
        "SELECT id FROM agent_events WHERE status IN ('new','retry') ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    results = []
    for item in rows:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM agent_events WHERE id=? AND status IN ('new','retry')",
                               (item["id"],)).fetchone()
            if not row:
                conn.rollback()
                continue
            payload = json.loads(row["payload_json"] or "{}")
            wf_name = EVENT_TO_WORKFLOW[row["event_type"]]
            existing = conn.execute("SELECT id FROM agent_tasks WHERE event_id=?", (row["id"],)).fetchone()
            task_id = existing["id"] if existing else tasks.create_task(
                conn, workflow=wf_name, agent_type=workflow.route_agent_type(wf_name),
                payload=payload, event_id=row["id"], commit=False,
            )
            conn.execute("UPDATE agent_events SET status='processed',task_id=?,processed_at=? WHERE id=?",
                         (task_id, _now(), row["id"]))
            conn.commit()
            results.append((row["id"], task_id))
        except Exception:
            conn.rollback()
            conn.execute("UPDATE agent_events SET attempt=attempt+1, "
                         "status=CASE WHEN attempt+1>=3 THEN 'dropped' ELSE 'retry' END WHERE id=?",
                         (item["id"],))
            conn.commit()
            results.append((item["id"], None))
    return results


def list_events(conn, limit=30):
    rows = conn.execute(
        "SELECT * FROM agent_events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except ValueError:
            item["payload"] = {}
        result.append(item)
    return result