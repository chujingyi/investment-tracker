"""Database schema for the agent module.

All tables are created with IF NOT EXISTS and are safe to call repeatedly,
mirroring the migration style used in app.init_db().
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    kind TEXT DEFAULT 'chat',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT DEFAULT '',
    tool_calls_json TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_chat ON agent_messages(chat_id, id);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    trigger TEXT NOT NULL DEFAULT 'chat',
    model TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ok',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    chat_id INTEGER,
    tool_name TEXT NOT NULL,
    args_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'ok',
    duration_ms INTEGER DEFAULT 0,
    result_summary TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    run_id INTEGER,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    summary TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    result_json TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    section TEXT DEFAULT '',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    dedup_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    attempt INTEGER NOT NULL DEFAULT 0,
    task_id INTEGER,
    created_at TEXT NOT NULL,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_events_status ON agent_events(status);
CREATE INDEX IF NOT EXISTS idx_agent_events_dedup ON agent_events(dedup_key, created_at);

CREATE TABLE IF NOT EXISTS agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow TEXT NOT NULL,
    agent_type TEXT NOT NULL DEFAULT 'monitor',
    status TEXT NOT NULL DEFAULT 'queued',
    event_id INTEGER,
    chat_id INTEGER,
    payload TEXT DEFAULT '{}',
    result TEXT DEFAULT '{}',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    attempt INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status);

CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL DEFAULT 'offline',
    sample_count INTEGER DEFAULT 0,
    metrics_json TEXT DEFAULT '{}',
    is_baseline INTEGER DEFAULT 0,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def ensure_agent_tables(conn):
    """Create agent tables when missing. FTS availability is handled in knowledge.py."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    migrations = {
        "pending_actions": {"task_id": "INTEGER", "request_key": "TEXT"},
        "agent_tool_calls": {"result_json": "TEXT DEFAULT '{}'"},
        "agent_runs": {"input_price": "REAL", "output_price": "REAL", "task_id": "INTEGER", "version": "TEXT DEFAULT ''"},
        "agent_tasks": {"run_id": "INTEGER", "lease_until": "TEXT", "version": "TEXT DEFAULT ''"},
        "eval_runs": {"dataset_json": "TEXT", "dataset_hash": "TEXT", "version": "TEXT DEFAULT ''"},
    }
    for table, columns in migrations.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_request ON pending_actions(request_key)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_task_event ON agent_tasks(event_id) WHERE event_id IS NOT NULL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_monitor_states (
            rec_id INTEGER PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_task_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
            name TEXT NOT NULL, status TEXT NOT NULL, detail TEXT DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_job_runs (
            job_key TEXT PRIMARY KEY, task_id INTEGER, created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5("
            "title, content, content='knowledge_chunks', content_rowid='id')"
        )
        conn.commit()
    except Exception:
        # FTS5 unavailable in this engine: knowledge.py falls back to LIKE search.
        pass
