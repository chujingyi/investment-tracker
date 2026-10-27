"""Daily morning report (P1): a read-only workflow task enqueued at the post-close slot.

The worker's close slot (America/New_York 16:20, DST-aware ≈ Beijing next-day
early morning) refreshes data, scans events, then reserves one report task.

The generation itself lives in agent.workflow (morning workflow); this module
keeps the P0-facing API (generate_morning_report / maybe_generate_morning_report)
so the worker and the /api/agent/morning_report route stay unchanged.
"""

from datetime import date

from agent import config, runtime, tasks, workflow

# Compatibility export (P0 code may reference the constant).
MORNING_PROMPT = workflow.MORNING_PROMPT


def generate_morning_report(conn):
    """Run the morning task. Returns a dict with report/chat/task ids and usage.

    Raises config.AgentNotConfigured / config.BudgetExceeded / LLMError.
    """
    config.require_configured()

    today = date.today().isoformat()
    title = f"晨报 {today}"
    task_id = tasks.create_task(
        conn,
        workflow="morning",
        agent_type=workflow.route_agent_type("morning"),
        payload={"title": title},
    )

    trace = workflow.run_task(conn, task_id)
    report = trace["answer"] or "（晨报生成为空，请检查 LLM 配置与日志）"

    task = tasks.get_task(conn, task_id)
    return {
        "report": report,
        "chat_id": task["chat_id"],
        "task_id": task_id,
        "chat_title": title,
        "usage": trace["usage"],
    }


def maybe_generate_morning_report(conn):
    """Worker hook: generate the report when enabled, configured and within budget.

    Never raises — a failed morning report must not break the data refresh loop.
    Returns a status string for logging.
    """
    if not config.morning_enabled():
        return "morning disabled"
    if not config.is_configured():
        return "morning skipped: LLM not configured"
    try:
        runtime.check_budget(conn)
    except config.BudgetExceeded as exc:
        return f"morning skipped: {exc}"
    try:
        result = generate_morning_report(conn)
    except Exception as exc:
        return f"morning failed: {exc}"
    usage = result.get("usage") or {}
    return (
        f"morning report ok (task #{result['task_id']}, "
        f"tokens {usage.get('prompt_tokens', 0)}+{usage.get('completion_tokens', 0)})"
    )

def enqueue_daily_report(conn):
    """Reserve one daily report task atomically (safe across worker restarts)."""
    if not config.morning_enabled() or not config.is_configured():
        return None
    key = "morning:" + date.today().isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute("SELECT task_id FROM agent_job_runs WHERE job_key=?", (key,)).fetchone()
        if existing:
            conn.rollback()
            return existing["task_id"]
        task_id = tasks.create_task(conn, "morning", "monitor", {}, commit=False)
        conn.execute("INSERT INTO agent_job_runs(job_key,task_id,created_at) VALUES (?,?,?)",
                     (key, task_id, runtime._now()))
        conn.commit()
        return task_id
    except Exception:
        conn.rollback()
        raise
