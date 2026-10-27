#!/usr/bin/env python3
"""US weekday market-slot scanner plus five-second durable-task consumer.

Schedule: America/New_York 09:00 and 16:20 (DST aware). Exchange holidays
are not inferred. One worker instance is recommended for this personal app.
"""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import LongbridgeClient, _refresh_ai_infra_data, _sync_calendar_events, get_db, init_db


SHANGHAI = ZoneInfo("Asia/Shanghai")


def next_run_at(now=None):
    """US weekday close+20min and pre-open slots, with DST conversion.

    Exchange holidays are not inferred here; data freshness is checked by tools.
    """
    now = now or datetime.now(SHANGHAI)
    ny = ZoneInfo("America/New_York")
    local = now.astimezone(ny)
    candidates = []
    for offset in range(8):
        day = local + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for hour, minute in ((9, 0), (16, 20)):
            slot = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if slot > local:
                candidates.append(slot.astimezone(SHANGHAI))
    return min(candidates)


def _run_agent_events():
    """Scan data changes into events, dispatch and run queued tasks."""
    from agent import events as agent_events
    from agent import workflow as agent_workflow

    conn = get_db()
    try:
        scanned = agent_events.scan_events(conn)
        dispatched = agent_events.process_events(conn, limit=10)
        ran = []  # queue execution happens in the regular worker tick
    finally:
        conn.close()
    print(
        f"[{datetime.now(SHANGHAI).isoformat(timespec='seconds')}] "
        f"events: scanned={sum(scanned.values())} ({scanned}) "
        f"dispatched={len(dispatched)} ran={ran}",
        flush=True,
    )


def refresh():
    for name, callback in (("signals", _refresh_ai_infra_data), ("calendar", _sync_calendar_events)):
        try:
            callback(LongbridgeClient())
        except Exception as exc:
            print(f"{name} refresh failed: {exc}", flush=True)
    _run_agent_events()
    from agent.morning import enqueue_daily_report
    conn = get_db()
    try:
        enqueue_daily_report(conn)
    finally:
        conn.close()


def refresh_events():
    """Pre-open slot (America/New_York 09:00, DST-aware): event scan + dispatch only."""
    _run_agent_events()


def main():
    init_db()
    # Index local research methods once. Failure is visible; never call an LLM here.
    conn = get_db()
    try:
        from agent.knowledge import ingest_all
        ingest_all(conn)
    finally:
        conn.close()
    target = next_run_at()
    while True:
        try:
            now = datetime.now(SHANGHAI)
            if now >= target:
                if target.astimezone(ZoneInfo("America/New_York")).hour == 16:
                    refresh()
                else:
                    refresh_events()
                target = next_run_at()
            # Poll queued requests independently of market schedules.
            from agent.workflow import run_pending_tasks
            conn = get_db()
            try:
                run_pending_tasks(conn, limit=1)
            finally:
                conn.close()
        except Exception as exc:
            print(f"Worker iteration failed: {exc}", flush=True)
            if datetime.now(SHANGHAI) >= target:
                target = next_run_at()
        time.sleep(5)


if __name__ == "__main__":
    main()
