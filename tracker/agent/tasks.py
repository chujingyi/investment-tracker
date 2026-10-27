"""Task state machine (P1): agent_tasks lifecycle and pending-action linkage.

The monitor state machine from P0 (tracking/approaching/triggered/revalidate/
snoozed/archived) is generalized here into a task state machine shared by all
workflows: queued → running → waiting_approval → succeeded | rejected |
retrying → failed/cancelled.
"""

import json
from datetime import datetime

TASK_STATUSES = (
    "queued",
    "running",
    "waiting_approval",
    "succeeded",
    "rejected",
    "retrying",
    "failed",
    "cancelled",
)

TASK_LABELS = {
    "queued": "排队中",
    "running": "执行中",
    "waiting_approval": "待审批",
    "succeeded": "已完成",
    "rejected": "被拒绝",
    "retrying": "修正重试",
    "failed": "失败",
    "cancelled": "已归档",
}

MAX_RETRY_ATTEMPTS = 2  # initial run + one rework after rejection


def _now():
    return datetime.now().isoformat(timespec="seconds")


def create_task(conn, workflow, agent_type, payload=None, event_id=None, commit=True):
    cur = conn.execute(
        "INSERT INTO agent_tasks (workflow, agent_type, status, event_id, payload, "
        "attempt, created_at, updated_at, chat_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            workflow,
            agent_type,
            "queued",
            event_id,
            json.dumps(payload or {}, ensure_ascii=False),
            0,
            _now(),
            _now(),
            (payload or {}).get("chat_id"),
        ),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def get_task(conn, task_id):
    row = conn.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["status_label"] = TASK_LABELS.get(item["status"], item["status"])
    return item


def set_task_status(conn, task_id, status, extra=None):
    import json as _json

    if status not in TASK_STATUSES:
        raise ValueError(f"invalid task status: {status}")
    if extra is None:
        conn.execute(
            "UPDATE agent_tasks SET status=?, updated_at=? WHERE id=?",
            (status, _now(), task_id),
        )
    else:
        merged = {}
        row = get_task(conn, task_id)
        if row:
            try:
                merged = _json.loads(row.get("result") or "{}")
            except ValueError:
                pass
        merged.update(extra)
        conn.execute(
            "UPDATE agent_tasks SET status=?, result=?, updated_at=? WHERE id=?",
            (status, _json.dumps(merged, ensure_ascii=False), _now(), task_id),
        )
    conn.commit()


def update_after_run(conn, task_id, chat_id, trace):
    """Persist run telemetry + outcome after a workflow run."""
    if get_task(conn, task_id)["status"] != "running":
        return  # a cancelled/expired task cannot publish a late result
    conn.execute(
        "UPDATE agent_tasks SET chat_id=?, prompt_tokens=prompt_tokens+?, completion_tokens=completion_tokens+?, "
        "latency_ms=latency_ms+?, lease_until=NULL, updated_at=? WHERE id=?",
        (
            chat_id,
            trace["usage"]["prompt_tokens"],
            trace["usage"]["completion_tokens"],
            trace["latency_ms"],
            _now(),
            task_id,
        ),
    )
    conn.commit()
    if trace.get("pending"):
        set_task_status(conn, task_id, "waiting_approval", {"answer": trace.get("answer")})
    else:
        set_task_status(
            conn, task_id, "failed" if trace.get("status") == "limited" else "succeeded",
            {"answer": trace.get("answer"), "tool_calls": trace.get("tool_calls"),
             "warning": "工具轮次上限，结果未验证完整" if trace.get("status") == "limited" else ""},
        )


def list_tasks(conn, limit=50, status=None):
    if status:
        rows = conn.execute(
            "SELECT * FROM agent_tasks WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM agent_tasks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["status_label"] = TASK_LABELS.get(item["status"], item["status"])
        result.append(item)
    return result


def find_task_by_chat(conn, chat_id):
    row = conn.execute(
        "SELECT * FROM agent_tasks WHERE chat_id=? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    return dict(row) if row else None


def on_pending_resolved(conn, chat_id, approved, reject_summary=None, task_id=None):
    """Aggregate decisions for the exact task/run; never complete early."""
    task = get_task(conn, task_id) if task_id else find_task_by_chat(conn, chat_id)
    if not task or task["status"] != "waiting_approval":
        return task
    rows = conn.execute(
        "SELECT status FROM pending_actions WHERE run_id=?", (task["run_id"],)
    ).fetchall()
    states = [row["status"] for row in rows]
    if "failed" in states:
        conn.execute("UPDATE pending_actions SET status='cancelled', resolved_at=? WHERE run_id=? AND status='pending'",
                     (_now(), task["run_id"]))
        conn.commit()
        set_task_status(conn, task["id"], "failed", {"error": "审批动作执行失败；请核对业务数据后另建任务"})
    elif "rejected" in states:
        conn.execute("UPDATE pending_actions SET status='cancelled', resolved_at=? WHERE run_id=? AND status='pending'",
                     (_now(), task["run_id"]))
        conn.commit()
        # Rework cannot repeat already approved writes from a partially accepted run.
        if task["attempt"] >= MAX_RETRY_ATTEMPTS or "approved" in states:
            set_task_status(conn, task["id"], "cancelled", {"reason": reject_summary or "用户拒绝；保留已批准动作"})
        else:
            set_task_status(conn, task["id"], "retrying", {"retry_hint": reject_summary or "用户拒绝了上轮建议，请重新核实，不要重复相同提案"})
    elif "pending" not in states and states and all(state == "approved" for state in states):
        set_task_status(conn, task["id"], "succeeded", {"approval": "approved"})
    return get_task(conn, task["id"])


def recover_expired_tasks(conn):
    """Fail interrupted runs visibly; do not replay possibly committed writes."""
    rows = conn.execute(
        "SELECT id, run_id FROM agent_tasks WHERE status='running' AND (lease_until IS NULL OR lease_until < ?)",
        (_now(),),
    ).fetchall()
    for row in rows:
        set_task_status(conn, row["id"], "failed", {"error": "执行进程中断或租约超时；请检查步骤记录后重新创建任务"})
        conn.execute("UPDATE pending_actions SET status='cancelled', resolved_at=? WHERE task_id=? AND status='pending'",
                     (_now(), row["id"]))
        if row["run_id"]:
            conn.execute("UPDATE agent_runs SET status='error', error='worker interrupted' WHERE id=? AND status='running'",
                         (row["run_id"],))
    conn.commit()
    return len(rows)


def record_step(conn, task_id, name, status, detail=""):
    conn.execute("INSERT INTO agent_task_steps(task_id,name,status,detail,created_at) VALUES (?,?,?,?,?)",
                 (task_id,name,status,str(detail)[:2000],_now()))
    conn.commit()


def terminate_task(conn, task_id, status, reason):
    """Terminal states cannot be resurrected by an interrupted or late run."""
    row = get_task(conn, task_id)
    if not row:
        return None
    if row["status"] in {"succeeded", "failed", "cancelled"}:
        return row
    set_task_status(conn, task_id, status, {"error": reason})
    conn.execute("UPDATE pending_actions SET status='cancelled',resolved_at=? WHERE task_id=? AND status='pending'",
                 (_now(),task_id))
    conn.execute("UPDATE agent_tasks SET lease_until=NULL WHERE id=?",(task_id,))
    conn.commit()
    return get_task(conn,task_id)
