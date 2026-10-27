"""Single-agent runtime: a bounded ReAct-style loop over OpenAI-compatible chat completions.

Loop: LLM -> tool_calls -> execute/intercept -> results -> LLM -> ... -> final answer.
Guardrails: iteration cap, daily token budget, HTTP retries.
Every run and every tool call is recorded for evaluation (P2 telemetry).
"""

import json
import re
import time
import hashlib
from contextvars import ContextVar

_DEADLINE = ContextVar("agent_deadline", default=None)
_TASK_ID = ContextVar("agent_task_id", default=None)
from datetime import datetime

import requests

from agent import config, tools

HISTORY_WINDOW = 16
MAX_HISTORY_CHARS = 16000  # per-turn history text budget (prompt keeps newest)
MAX_TOOL_RESULT_CHARS = 3500


class LLMError(RuntimeError):
    pass


# Leaked tool-call markup (e.g. DeepSeek DSML: `<｜｜DSML｜｜ invoke ...>`) sometimes
# appears as plain text instead of real tool_calls; never show it as an answer.
_DSML_TAG_RE = re.compile(r"</?[｜|]{2}\s*DSML\s*[｜|]{2}[^>\n]*>")


def _clean_answer(content):
    """Return answer text, or '' when it is empty / leaked tool-call markup.

    Callers substitute a readable placeholder for '' so users never see raw
    markup; real answers never contain the DSML marker pattern.
    """
    content = (content or "").strip()
    if not content or _DSML_TAG_RE.search(content):
        return ""
    return content


# ── budget ───────────────────────────────────────────────────────────────────


def tokens_used_today(conn):
    prefix = datetime.now().date().isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS used "
        "FROM agent_runs WHERE created_at LIKE ?",
        (prefix + "%",),
    ).fetchone()
    return int(row["used"] or 0)


def check_budget(conn):
    budget = config.daily_token_budget()
    used = tokens_used_today(conn)
    if budget and used >= budget:
        raise config.BudgetExceeded(
            f"今日 LLM token 用量已达 {used}（预算 {budget}），Agent 暂停运行"
        )


# ── LLM transport (isolated so tests can monkeypatch) ────────────────────────


def _chat_completion(messages, tool_specs=None, model=None):
    """One call against the OpenAI-compatible endpoint. Returns (content, tool_calls, usage)."""
    config.require_configured()
    url = config.llm_base_url() + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.llm_api_key()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or config.llm_model(),
        "messages": messages,
        "temperature": config.llm_temperature(),
        "max_tokens": config.max_output_tokens(),
    }
    if tool_specs:
        payload["tools"] = tool_specs

    last_error = None
    for attempt in range(3):
        remaining = (_DEADLINE.get() or (time.monotonic() + config.run_timeout())) - time.monotonic()
        if remaining <= 0:
            raise LLMError("任务执行时间预算已耗尽")
        try:
            response = requests.post(
                url, headers=headers, json=payload, timeout=min(config.request_timeout(), remaining)
            )
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                last_error = f"HTTP {response.status_code}"
                time.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            data = response.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            raw_calls = message.get("tool_calls") or []
            tool_calls = [
                {
                    "id": call.get("id") or f"call_{index}",
                    "name": (call.get("function") or {}).get("name") or "",
                    "arguments": (call.get("function") or {}).get("arguments") or "{}",
                }
                for index, call in enumerate(raw_calls)
            ]
            return message.get("content") or "", tool_calls, data.get("usage") or {}
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = str(exc)
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        except ValueError as exc:
            raise LLMError(f"LLM 返回非 JSON 内容: {exc}") from exc
        except requests.HTTPError as exc:
            detail = ""
            try:
                detail = response.text[:200]
            except Exception:
                pass
            raise LLMError(f"LLM HTTP 错误: {exc} {detail}") from exc
    raise LLMError(f"LLM 调用失败（重试后仍不可用）: {last_error or 'unknown'}")


# ── persistence helpers ─────────────────────────────────────────────────────


def _now():
    return datetime.now().isoformat(timespec="seconds")


def insert_message(conn, chat_id, role, content, tool_calls_json=""):
    cur = conn.execute(
        "INSERT INTO agent_messages (chat_id, role, content, tool_calls_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (chat_id, role, content or "", tool_calls_json or "", _now()),
    )
    conn.commit()
    return cur.lastrowid


def _history_messages(conn, chat_id):
    rows = conn.execute(
        "SELECT role, content FROM agent_messages "
        "WHERE chat_id=? AND role IN ('user','assistant') AND content != '' "
        "ORDER BY id DESC LIMIT ?",
        (chat_id, HISTORY_WINDOW),
    ).fetchall()
    # A long-lived chat (e.g. many full review replays) can grow the prompt
    # past tens of thousands of tokens. Keep newest messages intact and
    # truncate the oldest oversized ones so a single turn stays bounded.
    budget = MAX_HISTORY_CHARS
    kept = []
    for row in rows:  # newest first
        text = row["content"] or ""
        if len(text) > budget:
            text = text[:max(budget, 0)] + ("\n…（历史记录过长已截断）" if budget > 0 else "")
        if text:
            kept.append({"role": row["role"], "content": text})
        budget -= len(text)
        if budget <= 0:
            break
    return list(reversed(kept))


def create_pending_action(conn, chat_id, run_id, name, args):
    if _TASK_ID.get():
        state = conn.execute("SELECT status FROM agent_tasks WHERE id=?", (_TASK_ID.get(),)).fetchone()
        if not state or state["status"] != "running":
            raise LLMError("任务已取消或过期")
    summary = tools.summarize_write(name, args)
    request_key = hashlib.sha256(
        f"{run_id}:{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}".encode()
    ).hexdigest()
    existing = conn.execute("SELECT id, summary FROM pending_actions WHERE request_key=?", (request_key,)).fetchone()
    if existing:
        return existing["id"], existing["summary"]
    cur = conn.execute(
        "INSERT INTO pending_actions (chat_id, run_id, task_id, request_key, tool_name, args_json, summary, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,'pending',?)",
        (chat_id, run_id, _TASK_ID.get(), request_key, name, json.dumps(args, ensure_ascii=False), summary, _now()),
    )
    conn.commit()
    return cur.lastrowid, summary


def resolve_pending_action(conn, pending_id, approve):
    """Execute an approved local write atomically with its decision record.

    BEGIN IMMEDIATE serializes concurrent approvals on SQLite/libSQL. All write
    tool handlers use this connection and must not commit independently.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM pending_actions WHERE id=?", (pending_id,)).fetchone()
        if not row:
            conn.rollback()
            return None
        if row["status"] != "pending":
            conn.rollback()
            return {**dict(row), "changed": False}
        status = "rejected"
        result = {"ok": False, "summary": "用户拒绝了操作"}
        if approve:
            age = datetime.now() - datetime.fromisoformat(row["created_at"])
            if age.total_seconds() > 24 * 3600:
                raise tools.ToolError("提案超过24小时，请重新获取数据并生成提案")
            if row["task_id"]:
                task = conn.execute("SELECT status FROM agent_tasks WHERE id=?", (row["task_id"],)).fetchone()
                if not task or task["status"] not in {"running", "waiting_approval"}:
                    raise tools.ToolError("任务已终止，不能执行旧提案")
            errors = tools.validate_write(row["tool_name"], json.loads(row["args_json"]), conn=conn)
            if errors:
                raise tools.ToolError("; ".join(errors))
            result = tools.run_write(conn, row["tool_name"], json.loads(row["args_json"]))
            if result.get("error"):
                raise tools.ToolError(str(result["error"]))
            status = "approved"
        conn.execute(
            "UPDATE pending_actions SET status=?, result_json=?, resolved_at=? WHERE id=?",
            (status, json.dumps(result, ensure_ascii=False), _now(), pending_id),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        # Failed writes leave no partial business changes and remain visible.
        conn.execute(
            "UPDATE pending_actions SET status='failed', result_json=?, resolved_at=? WHERE id=? AND status='pending'",
            (json.dumps({"error": str(exc)}, ensure_ascii=False), _now(), pending_id),
        )
        conn.commit()
    updated = conn.execute("SELECT * FROM pending_actions WHERE id=?", (pending_id,)).fetchone()
    if updated and updated["chat_id"]:
        insert_message(conn, updated["chat_id"], "user",
                       f"（审批结果：操作 #{pending_id}：{updated['status']}）")
    return {**dict(updated), "changed": True} if updated else None


# ── main loop ────────────────────────────────────────────────────────────────


def _run_llm_turn(conn, chat_id, run_id, message, tool_specs, timeline, model=None):
    """One LLM round-trip including tool execution. Returns (final_answer, done)."""
    check_budget(conn)
    if _TASK_ID.get():
        state = conn.execute("SELECT status FROM agent_tasks WHERE id=?",(_TASK_ID.get(),)).fetchone()
        if not state or state["status"] != "running":
            raise LLMError("任务已取消或过期")
    if _DEADLINE.get() and time.monotonic() >= _DEADLINE.get():
        raise LLMError("任务执行时间预算已耗尽")
    content, tool_calls, usage = _chat_completion(message, tool_specs, model=model)
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    conn.execute(
        "UPDATE agent_runs SET prompt_tokens=prompt_tokens+?, completion_tokens=completion_tokens+? WHERE id=?",
        (prompt_tokens, completion_tokens, run_id),
    )
    conn.commit()

    if not tool_calls:
        return _clean_answer(content), True

    message.append(
        {
            "role": "assistant",
            "content": content or "",
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
                for call in tool_calls
            ],
        }
    )
    persisted_calls = []
    for call in tool_calls:
        name = call["name"]
        try:
            args = json.loads(call["arguments"] or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except ValueError:
            args = {}
            timeline.append(
                {"name": name, "status": "error", "duration_ms": 0, "summary": "参数 JSON 解析失败"}
            )
            persisted_calls.append(timeline[-1])
            conn.execute("INSERT INTO agent_tool_calls (run_id,chat_id,tool_name,args_json,status,duration_ms,result_summary,created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (run_id,chat_id,name,call["arguments"],"invalid",0,"参数 JSON 解析失败",_now()))
            conn.commit()
            message.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": "工具参数不是合法 JSON，请修正后重试。",
                }
            )
            continue

        started = time.monotonic()
        pending_id = None
        try:
            if not tools.known_tool(name):
                result = {"error": f"未知工具: {name}"}
                status = "error"
            elif tools.is_write(name):
                if tool_specs and any(
                    spec["function"]["name"] == name for spec in tool_specs
                ):
                    errors = tools.validate_write(name, args, conn=conn)
                    if errors:
                        result = {
                            "error": "参数未通过三档决策门校验，请修正后重新调用",
                            "details": errors,
                        }
                        status = "invalid"
                    else:
                        pending_id, summary = create_pending_action(
                            conn, chat_id, run_id, name, args
                        )
                        result = {
                            "status": "pending_approval",
                            "pending_id": pending_id,
                            "summary": summary,
                            "hint": "已生成待审批卡片，用户批准后才会执行；请告知用户去审批。",
                        }
                        status = "pending_approval"
                else:
                    result = {"error": "当前场景为只读模式，不允许写操作"}
                    status = "blocked"
            elif name not in {spec["function"]["name"] for spec in (tool_specs or [])}:
                result = {"error": "当前任务未授权此工具"}
                status = "blocked"
            else:
                result = tools.run_read(conn, name, args)
                status = "ok"
        except tools.ToolError as exc:
            result = {"error": str(exc)}
            status = "error"
        except Exception as exc:
            result = {"error": f"工具执行异常: {exc}"}
            status = "error"
        duration_ms = int((time.monotonic() - started) * 1000)

        summary = result.pop("summary", None) or json.dumps(result, ensure_ascii=False)[:120]
        entry = {
            "name": name,
            "status": status,
            "pending_id": pending_id,
            "duration_ms": duration_ms,
            "summary": str(summary)[:200],
            "args": args,
        }
        timeline.append(entry)
        persisted_calls.append(entry)
        conn.execute(
            "INSERT INTO agent_tool_calls (run_id, chat_id, tool_name, args_json, status, "
            "duration_ms, result_summary, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, chat_id, name, json.dumps(args, ensure_ascii=False), status,
             duration_ms, entry["summary"], _now()),
        )
        conn.commit()

        serialized = json.dumps(result, ensure_ascii=False)
        if len(serialized) > MAX_TOOL_RESULT_CHARS:
            serialized = json.dumps({"truncated": True, "excerpt": serialized[:MAX_TOOL_RESULT_CHARS],
                                     "hint": "结果过长，请缩小查询范围"}, ensure_ascii=False)
        conn.execute("UPDATE agent_tool_calls SET result_json=? WHERE id=last_insert_rowid()",
                     (json.dumps(result, ensure_ascii=False),))
        conn.commit()
        message.append(
            {"role": "tool", "tool_call_id": call["id"], "content": serialized}
        )

    insert_message(
        conn,
        chat_id,
        "assistant",
        content or "",
        json.dumps(persisted_calls, ensure_ascii=False),
    )
    return content, False


def run_chat(conn, chat_id, user_message, allow_write=True, trigger="chat", system_prompt=None, model=None, task_id=None):
    """Run one agent turn. Returns a trace dict for the UI.

    Raises config.AgentNotConfigured / config.BudgetExceeded / LLMError.
    """
    config.require_configured()
    check_budget(conn)

    started = time.monotonic()
    if not system_prompt:
        from agent import context

        system_prompt = context.system_prompt(conn)

    effective_model = model or config.llm_model()
    incoming, outgoing = config.model_prices(effective_model)
    insert_message(conn, chat_id, "user", user_message)
    run_cur = conn.execute(
        "INSERT INTO agent_runs (chat_id, trigger, model, status, created_at, task_id, version) "
        "VALUES (?,?,?,?,?,?,?)",
        (chat_id, trigger, effective_model, "running", _now(), task_id, config.version()),
    )
    conn.commit()
    run_id = run_cur.lastrowid
    conn.execute("UPDATE agent_runs SET input_price=?,output_price=? WHERE id=?", (incoming, outgoing, run_id))
    conn.commit()
    if task_id:
        conn.execute("UPDATE agent_tasks SET run_id=? WHERE id=?", (run_id, task_id))
        conn.commit()

    tool_specs = tools.tool_definitions(include_write=allow_write)
    messages = [{"role": "system", "content": system_prompt}]
    messages += _history_messages(conn, chat_id)

    deadline_token = _DEADLINE.set(started + config.run_timeout())
    task_token = _TASK_ID.set(task_id)
    timeline = []
    final_answer = ""
    status = "ok"
    error = ""
    try:
        for _ in range(config.max_iterations()):
            content, done = _run_llm_turn(
                conn, chat_id, run_id, messages, tool_specs, timeline, model=effective_model
            )
            if done:
                final_answer = content or "（本轮未生成有效回复，请重试或调整问题。）"
                break
        else:
            # Iteration cap reached: one more call without tools, explicitly
            # asking for a plain-text summary (models may otherwise echo
            # tool-call markup as text, e.g. DeepSeek DSML).
            check_budget(conn)
            if time.monotonic() >= _DEADLINE.get():
                raise LLMError("任务执行时间预算已耗尽")
            nudge = {
                "role": "user",
                "content": "（系统提示：工具调用轮次已达上限。请不要再输出任何工具调用或标记文本，直接用中文文字总结：已完成的工作、关键发现、以及仍然缺失的信息。）",
            }
            content, tool_calls, usage = _chat_completion(messages + [nudge], model=effective_model)
            conn.execute("UPDATE agent_runs SET prompt_tokens=prompt_tokens+?, completion_tokens=completion_tokens+? WHERE id=?",
                         (int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0), run_id))
            conn.commit()
            final_answer = _clean_answer(content) or (
                f"（已达工具调用上限（{config.max_iterations()} 轮、{len(timeline)} 次工具调用），"
                "未能生成完整总结；建议缩小问题范围后重试。）"
            )
            status = "limited"
    except Exception as exc:
        status = "error"
        error = str(exc)
        raise
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        conn.execute(
            "UPDATE agent_runs SET latency_ms=?, status=?, error=? WHERE id=?",
            (latency_ms, status, error, run_id),
        )
        conn.commit()
        _DEADLINE.reset(deadline_token)
        _TASK_ID.reset(task_token)

    if final_answer:
        insert_message(conn, chat_id, "assistant", final_answer)

    pending = [
        {"id": item.get("pending_id"), "tool_name": item["name"], "summary": item["summary"]}
        for item in timeline
        if item["status"] == "pending_approval"
    ]
    usage_row = conn.execute(
        "SELECT prompt_tokens, completion_tokens FROM agent_runs WHERE id=?", (run_id,)
    ).fetchone()
    return {
        "chat_id": chat_id,
        "run_id": run_id,
        "status": status,
        "version": config.version(),
        "answer": final_answer,
        "tool_calls": timeline,
        "pending": pending,
        "usage": {
            "prompt_tokens": usage_row["prompt_tokens"],
            "completion_tokens": usage_row["completion_tokens"],
        },
        "latency_ms": latency_ms,
        "model": effective_model,
    }
