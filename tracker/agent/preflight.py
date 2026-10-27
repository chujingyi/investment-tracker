"""Read-only readiness checks: never initialize schema or access holdings."""
import os
import sqlite3
import shutil
from urllib.parse import urlsplit
from agent import config
from service_support import configure_tls_trust, service_failure

REQUIRED = {
    "agent_chats": {"id", "kind"},
    "agent_messages": {"chat_id", "tool_calls_json"},
    "agent_tool_calls": {"run_id", "result_json"},
    "knowledge_chunks": {"title", "source", "content"},
    "agent_events": {"event_type", "dedup_key", "task_id"},
    "agent_tasks": {"run_id", "lease_until", "version"},
    "agent_runs": {"task_id", "version", "input_price", "output_price"},
    "pending_actions": {"task_id", "request_key"},
    "agent_task_steps": {"task_id", "name", "status"},
    "agent_monitor_states": {"rec_id", "state"},
    "agent_job_runs": {"job_key", "task_id"},
    "eval_runs": {"dataset_json", "dataset_hash", "version"},
}


def schema_readiness(connection):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    missing = sorted(set(REQUIRED) - tables)
    columns = {}
    for name, required in REQUIRED.items():
        if name in tables:
            available = {row[1] for row in connection.execute(f"PRAGMA table_info({name})").fetchall()}
            if required - available:
                columns[name] = sorted(required - available)
    return {"ready": not missing and not columns, "missing_tables": missing, "missing_columns": columns,
            "action": "none" if not missing and not columns else "back_up_then_initialize_schema"}


def check(network=False):
    from longbridge_api import is_configured
    report = {
        "mode": "network_read_only" if network else "configuration_only",
        "model": {"configured": config.is_configured(), "name": config.llm_model(),
                  "host": urlsplit(config.llm_base_url()).hostname, "called": False},
        "longbridge": {"configured": is_configured(), "private_account_accessed": False},
        "database": {"remote": bool(os.environ.get("TURSO_DATABASE_URL")), "checked": False},
        "deployment": {"docker_installed": bool(shutil.which("docker")),
                       "login_configured": bool(os.environ.get("APP_USERNAME") and os.environ.get("APP_PASSWORD"))},
    }
    if not network:
        return report
    if is_configured():
        try:
            from longbridge_api import LongbridgeClient
            client = LongbridgeClient()
            rows = client.quotes(["AAPL.US"])
            report["longbridge"].update(connected=bool(rows), calendar_available=client._calendar is not None)
        except Exception as exc:
            report["longbridge"].update(connected=False, diagnostic=service_failure(exc))
    connection = None
    try:
        if report["database"]["remote"]:
            configure_tls_trust()
            import libsql
            connection = libsql.connect(database=os.environ["TURSO_DATABASE_URL"], auth_token=os.environ.get("TURSO_AUTH_TOKEN", ""))
        else:
            from app import DB_PATH
            from pathlib import Path
            path = Path(os.environ.get("TEST_DATABASE_PATH") or DB_PATH)
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        connection.execute("SELECT 1").fetchone()
        report["database"].update(checked=True, connected=True, schema=schema_readiness(connection))
    except Exception as exc:
        report["database"].update(checked=True, connected=False, diagnostic=service_failure(exc))
    finally:
        if connection is not None:
            connection.close()
    return report
