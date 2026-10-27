"""Online metrics (P2): aggregate the existing telemetry tables into the five
operations indicators — task completion rate, approval rate, tool success
rate, latency and cost. No new collection: P0/P1 already instrumented all of it.
"""

from datetime import datetime, timedelta

from agent import config


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _cost_cny(prompt_tokens, completion_tokens):
    return round(
        (prompt_tokens or 0) / 1_000_000 * config.input_price_per_m()
        + (completion_tokens or 0) / 1_000_000 * config.output_price_per_m(),
        4,
    )


def _task_buckets(days=7):
    return [(datetime.now() - timedelta(days=offset)).date().isoformat() for offset in range(days - 1, -1, -1)]


def metrics(conn):
    """Aggregate all telemetry tables into one report dict."""
    # ── tasks: completion by workflow ──────────────────────────────
    rows = conn.execute(
        "SELECT workflow, status, COUNT(*) AS cnt FROM agent_tasks GROUP BY workflow, status"
    ).fetchall()
    by_workflow = {}
    for item in rows:
        wf = by_workflow.setdefault(item["workflow"], {"total": 0, "succeeded": 0, "failed": 0})
        wf["total"] += item["cnt"]
        if item["status"] == "succeeded":
            wf["succeeded"] += item["cnt"]
        if item["status"] == "failed":
            wf["failed"] += item["cnt"]
    for wf in by_workflow.values():
        wf["completion_rate"] = round(wf["succeeded"] / wf["total"] * 100, 1) if wf["total"] else None
    total_tasks = sum(wf["total"] for wf in by_workflow.values())
    succeeded_tasks = sum(wf["succeeded"] for wf in by_workflow.values())
    tasks_report = {
        "total": total_tasks,
        "succeeded": succeeded_tasks,
        "completion_rate": round(succeeded_tasks / total_tasks * 100, 1) if total_tasks else None,
        "by_workflow": by_workflow,
    }

    # ── approvals ──────────────────────────────────────────────────
    rows = conn.execute("SELECT status, COUNT(*) AS cnt FROM pending_actions GROUP BY status").fetchall()
    approvals = {"approved": 0, "rejected": 0, "pending": 0, "failed": 0, "cancelled": 0}
    for item in rows:
        if item["status"] in approvals:
            approvals[item["status"]] += item["cnt"]
    decided = approvals["approved"] + approvals["rejected"]
    approvals["approval_rate"] = round(approvals["approved"] / decided * 100, 1) if decided else None

    # ── tool calls ─────────────────────────────────────────────────
    rows = conn.execute(
        "SELECT tool_name, status, COUNT(*) AS cnt FROM agent_tool_calls GROUP BY tool_name, status"
    ).fetchall()
    by_tool = {}
    tool_total = 0
    tool_ok = 0
    for item in rows:
        tool = by_tool.setdefault(item["tool_name"], {"total": 0, "ok": 0})
        tool["total"] += item["cnt"]
        tool_total += item["cnt"]
        if item["status"] == "ok":
            tool["ok"] += item["cnt"]
            tool_ok += item["cnt"]
    for tool in by_tool.values():
        tool["success_rate"] = round(tool["ok"] / tool["total"] * 100, 1) if tool["total"] else None

    # ── latency ────────────────────────────────────────────────────
    row = conn.execute("SELECT AVG(latency_ms) AS avg_ms FROM agent_tasks WHERE attempt>0").fetchone()
    task_latency = round(float(row["avg_ms"])) if row and row["avg_ms"] is not None else None
    row = conn.execute("SELECT AVG(duration_ms) AS avg_ms FROM agent_tool_calls").fetchone()
    tool_latency = round(float(row["avg_ms"])) if row and row["avg_ms"] is not None else None

    # ── cost ───────────────────────────────────────────────────────
    rows = conn.execute(
        "SELECT created_at, model, input_price, output_price, prompt_tokens, completion_tokens FROM agent_runs ORDER BY created_at"
    ).fetchall()
    days = {day: {"prompt": 0, "completion": 0, "runs": 0} for day in _task_buckets()}
    total_cost = 0.0
    by_model = {}
    total_prompt = 0
    total_completion = 0
    for item in rows:
        day = str(item["created_at"])[:10]
        bucket = days.setdefault(day, {"prompt": 0, "completion": 0, "runs": 0})
        incoming = item["input_price"] if item["input_price"] is not None else config.input_price_per_m()
        outgoing = item["output_price"] if item["output_price"] is not None else config.output_price_per_m()
        cost = ((item["prompt_tokens"] or 0)*incoming + (item["completion_tokens"] or 0)*outgoing)/1_000_000
        total_cost += cost
        bucket["cost"] = bucket.get("cost", 0) + cost
        by_model[item["model"]] = round(by_model.get(item["model"], 0) + cost, 6)
        bucket["prompt"] += item["prompt_tokens"] or 0
        bucket["completion"] += item["completion_tokens"] or 0
        bucket["runs"] += 1
        total_prompt += item["prompt_tokens"] or 0
        total_completion += item["completion_tokens"] or 0
    cost_report = {
        "total_cny": round(total_cost, 4),
        "estimated": True,
        "by_model_cny": by_model,
        "note": "按运行时配置估算；无价格快照的旧记录使用当前默认值，不等同账单",
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "by_day": [
            {"date": day, "cost_cny": round(data.get("cost", 0), 4), "runs": data["runs"]}
            for day, data in days.items()
        ],
        "prices": {
            "input_per_m": config.input_price_per_m(),
            "output_per_m": config.output_price_per_m(),
        },
    }

    # ── events ─────────────────────────────────────────────────────
    rows = conn.execute("SELECT status, COUNT(*) AS cnt FROM agent_events GROUP BY status").fetchall()
    events = {"processed": 0, "new": 0, "retry": 0, "dropped": 0}
    for item in rows:
        if item["status"] in events:
            events[item["status"]] = item["cnt"]

    durations = sorted(row[0] for row in conn.execute("SELECT latency_ms FROM agent_runs WHERE status IN ('ok','limited') AND latency_ms>0"))
    p95 = durations[max(0, (95*len(durations)+99)//100-1)] if durations else None
    status_counts = {row["status"]: row["n"] for row in conn.execute("SELECT status,COUNT(*) AS n FROM agent_tool_calls GROUP BY status")}
    return {
        "tasks": tasks_report,
        "approvals": approvals,
        "tools": {"total": tool_total, "ok": tool_ok, "by_tool": by_tool, "by_status": status_counts,
                  "note": "pending_approval 为待确认，不应算作工具执行失败"},
        "latency": {"task_avg_ms": task_latency, "tool_avg_ms": tool_latency, "run_p95_ms": p95},
        "cost": cost_report,
        "events": events,
        "generated_at": _now(),
    }