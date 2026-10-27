"""Copy the local tracker database into the configured Turso database."""

import os
import sqlite3

from app import DB_PATH, get_db, init_db


TABLES = (
    "analysis_sessions",
    "recommendations",
    "knowledge_articles",
    "price_snapshots",
    "catalysts",
    "catalyst_tickers",
    "macro_events",
    "notifications",
    "broker_positions", "broker_executions", "ai_infra_monitor",
    "agent_chats", "agent_messages", "agent_runs", "agent_tool_calls",
    "agent_tasks", "pending_actions", "agent_events", "knowledge_chunks",
    "eval_runs", "agent_monitor_states", "agent_job_runs", "agent_task_steps",
)

BATCH_SIZE = 50


def migrate():
    if not os.environ.get("TURSO_DATABASE_URL"):
        raise RuntimeError("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN first")

    source = sqlite3.connect(DB_PATH)
    source.row_factory = sqlite3.Row
    init_db()
    target = get_db()

    copied = {}
    target.execute("PRAGMA foreign_keys = OFF")
    try:
        for table in TABLES:
            exists = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            rows = source.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                copied[table] = 0
                continue
            columns = rows[0].keys()
            names = ", ".join(columns)
            placeholders = ", ".join("?" for _ in columns)
            sql = f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({placeholders})"
            # Insert in batches to avoid timeout on remote DB
            for i in range(0, len(rows), BATCH_SIZE):
                batch = rows[i : i + BATCH_SIZE]
                target.executemany(
                    sql,
                    [tuple(row[name] for name in columns) for row in batch],
                )
                target.commit()
                print(f"  {table}: {min(i + BATCH_SIZE, len(rows))}/{len(rows)}")
            copied[table] = len(rows)
        from agent.knowledge import _rebuild_fts
        _rebuild_fts(target)
        target.commit()
    finally:
        target.execute("PRAGMA foreign_keys = ON")
        target.close()
        source.close()

    for table, count in copied.items():
        print(f"{table}: {count}")


if __name__ == "__main__":
    migrate()
