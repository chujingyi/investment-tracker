"""Workflow engine (P1): three reusable workflows + task runner.

Workflows are declared in code (versioned, testable) — visual configuration
becomes a platform concern (P2+). Each workflow defines its own prompt, tool
permissions and model route; runtime.run_chat executes the agent loop.
"""

import json
from datetime import datetime, timedelta

from agent import config, runtime, tasks

# ── workflow prompts ─────────────────────────────────────────────────────────

MORNING_PROMPT = """生成今日投资晨报。请按以下步骤调用工具收集真实数据后撰写，禁止编造数字：

1. query_portfolio — 持仓与组合概况
2. query_recommendations — 活跃推荐/观察清单
3. get_sector_signals — 板块信号（AI 基建轮动信号）
4. get_market_calendar — 今日与未来 7 天重要事件（财报/宏观）
5. 如有需要，get_quotes 补充关键持仓的实时报价，search_knowledge 检索相关框架逻辑

输出格式（中文，简洁）：
## 一、隔夜与持仓变化
（每只持仓的最新价与涨跌，只报工具返回的数据）
## 二、需要关注的事项
（触发/接近触发的监控、即将到期的观察项、临近事件）
## 三、今日 3 个行动点
（每条带具体数据依据与建议动作；没有值得行动的就明说"今日无必须行动项"，不要硬凑）
## 四、风险提示
（数据缺口、即将失效的信号等）

全文不超过 600 字，数据引用必须来自工具结果。"""

MONITOR_PROMPT = """你是一个价格监控研判 Agent。有监控项状态发生变化，请判断是否值得提醒用户，并给出最精简的说明。

监控信息：
{payload}

请按以下步骤：
1. 用 query_recommendations 确认该推荐当前状态，get_quotes 拉取实时报价；
2. 如果状态是 triggered（价格到达等待区），额外用 search_knowledge 检索对应框架逻辑判断是否值得入场提醒；
3. 输出（严格三段，不要多余内容）：
   仅返回 JSON：{{"notify": true或false, "reason": "变化依据与建议动作"}}。
   日历事件没有 rec_id/ticker 时，只检查日历与事件，不编造标的。

重要：本场景为只读，不允许修改任何数据。"""

REVIEW_PROMPT = """你是投资复盘 Agent。以下观察项已到复查日期，请逐条给出结论：转为"可入"、继续观察、或关闭。

待复盘项：
{items}

执行步骤：
1. query_recommendations 按 ticker 查当前记录（含 wait_price/validity_date/监控状态）；
2. get_quotes 拉取当前价格；search_knowledge 检索对应框架，判断原逻辑是否仍然成立；
3. 结论建议：
   - 逻辑成立且价格合理 → 转"可入"：调用 propose_recommendation（entry_rec=actionable，给出入场/止损/目标）【会走审批】；
   - 逻辑成立但需继续等 → 保持观察：调用 update_monitor（resume，刷新有效期）【会走审批】；
   - 逻辑不成立或超期过久 → 关闭：调用 update_monitor（close）【会走审批】；
   - 信息不足 → 明确说明缺什么数据，不调用任何写工具。
4. 每条结论给出理由（数据支撑），最后汇总一张清单。

写操作都会生成审批卡片，用户批准后才生效；被拒绝后请按反馈修正。"""

RESEARCH_PROMPT = """你是投资研究 Agent。请对指定股票完成一次候选研究，按三档决策门输出结论。

研究目标：{TICKER}

执行步骤：
1. query_recommendations（看是否已有记录）、get_quotes（现价/涨跌）、query_catalysts（催化剂）、get_market_calendar（临近事件）；
2. search_knowledge 检索相关投资框架（供应链瓶颈/轮动/动量催化/NAV 折价），判断是否满足框架条件；
3. 按三档决策门输出：
   - "可入"（逻辑成立、价格合理）：给出完整操作建议，调用 propose_recommendation（含入场价/止损或目标/notes 操作指令）【会走审批】；
   - "观察"（有潜力但没到买点）：给出触发条件，调用 propose_recommendation（entry_rec=watch + wait_condition）【会走审批】；
   - "不碰"：不调用任何写工具，直接说明原因；
4. 最后用 3 行以内总结结论与依据（引用工具数据）。"""

# ── workflow registry ──────────────────────────────────────────────────────

WORKFLOWS = {
    "chat": {"title": "研究对话", "kind": "chat", "agent_type": "research",
             "allow_write": True, "model_route": "strong"},
    "morning": {
        "title": "每日晨报",
        "kind": "morning",
        "agent_type": "monitor",
        "allow_write": False,
        "model_route": "fast",
    },
    "monitor": {
        "title": "事件研判",
        "kind": "task",
        "agent_type": "monitor",
        "allow_write": False,
        "model_route": "fast",
    },
    "review": {
        "title": "到期复盘",
        "kind": "task",
        "agent_type": "review",
        "allow_write": True,
        "model_route": "review",
    },
    "research": {
        "title": "候选研究",
        "kind": "task",
        "agent_type": "research",
        "allow_write": True,
        "model_route": "strong",
    },
}


def route_agent_type(wf_name):
    meta = WORKFLOWS.get(wf_name)
    return meta["agent_type"] if meta else "monitor"


def model_for(wf_name):
    meta = WORKFLOWS.get(wf_name) or {}
    route = meta.get("model_route", "fast")
    if route == "strong":
        return config.llm_strong_model()
    if route == "review":
        return config.llm_review_model()
    return config.llm_fast_model()


def build_prompt(wf_name, payload=None, retry_hint=None):
    payload = payload or {}
    if wf_name == "chat":
        prompt = str(payload.get("message") or "")
    elif wf_name == "morning":
        prompt = MORNING_PROMPT
    elif wf_name == "monitor":
        prompt = MONITOR_PROMPT.format(payload=json.dumps(payload, ensure_ascii=False)[:800])
    elif wf_name == "review":
        rows = payload.get("items")
        if rows is None:
            rows = [{"rec_id": payload.get("rec_id"), "ticker": payload.get("ticker"),
                     "validity_date": payload.get("validity_date")}]
        items = "\n".join(
            f"- #{r.get('rec_id')} {r.get('ticker')} 复查日期 {r.get('validity_date')}"
            for r in rows if r.get("rec_id")
        )
        scope = f"仅复盘 {payload['ticker']} 的相关记录" if payload.get("ticker") else "仅复盘 validity_date 已到期的活跃观察记录"
        prompt = REVIEW_PROMPT.format(items=items or scope)
    elif wf_name == "research":
        ticker = payload.get("ticker") or payload.get("symbol") or "（未指定，请先问用户）"
        prompt = RESEARCH_PROMPT.format(TICKER=str(ticker).upper())
    else:
        raise ValueError(f"unknown workflow: {wf_name}")
    if retry_hint:
        prompt = f"{prompt}\n\n[上轮审批反馈] {retry_hint}"
    return prompt


def create_task_and_run(conn, workflow, payload=None, event_id=None, retry_hint=None):
    """Create a task then execute it synchronously. Returns task dict + trace."""
    task_id = tasks.create_task(
        conn, workflow, route_agent_type(workflow), payload=payload, event_id=event_id
    )
    trace = run_task(conn, task_id, retry_hint=retry_hint)
    task = tasks.get_task(conn, task_id)
    task["trace"] = trace
    return task


def run_task(conn, task_id, retry_hint=None):
    """Execute a queued/retrying task. Returns the run trace.

    Raises config.AgentNotConfigured / config.BudgetExceeded / runtime.LLMError.
    """
    task = tasks.get_task(conn, task_id)
    if not task:
        raise ValueError(f"task {task_id} not found")
    if task["status"] not in ("queued", "retrying"):
        raise ValueError(f"task {task_id} is {task['status']}, cannot run")

    meta = WORKFLOWS.get(task["workflow"])
    if not meta:
        tasks.set_task_status(conn, task_id, "failed", {"error": "unknown workflow"})
        raise ValueError(f"unknown workflow: {task['workflow']}")

    # Missing configuration/budget must not consume an execution attempt.
    config.require_configured()
    runtime.check_budget(conn)
    lease = (datetime.now() + timedelta(seconds=config.run_timeout() + 60)).isoformat(timespec="seconds")
    claimed = conn.execute(
        "UPDATE agent_tasks SET status='running', run_id=NULL, attempt=attempt+1, lease_until=?, version=?, updated_at=? "
        "WHERE id=? AND status IN ('queued','retrying')",
        (lease, config.version(), runtime._now(), task_id),
    )
    conn.commit()
    if claimed.rowcount != 1:
        raise ValueError("任务已被其他执行器领取")
    payload = {}
    try:
        payload = json.loads(task["payload"] or "{}")
    except ValueError:
        pass

    chat_id = task["chat_id"] or payload.get("chat_id")
    if not chat_id:
        cur = conn.execute(
            "INSERT INTO agent_chats (title, kind, created_at) VALUES (?,?,?)",
            (f"{meta['title']} #{task_id}", meta["kind"], runtime._now()),
        )
        conn.commit()
        chat_id = cur.lastrowid

    conn.execute("UPDATE agent_tasks SET chat_id=? WHERE id=?", (chat_id, task_id))
    conn.commit()
    tasks.record_step(conn, task_id, "prepare", "succeeded", "会话、任务范围、模型与权限已确定")
    feedback = json.loads(task["result"] or "{}")
    prompt = build_prompt(task["workflow"], payload, retry_hint or feedback.get("retry_hint"))
    tasks.record_step(conn, task_id, "agent_loop", "running", config.version())
    try:
        trace = runtime.run_chat(
            conn,
            chat_id,
            prompt,
            allow_write=meta["allow_write"],
            trigger=task["workflow"],
            model=model_for(task["workflow"]),
            task_id=task_id,
        )
    except config.AgentNotConfigured:
        # LLM key missing: keep the task queued so it runs once configured.
        tasks.set_task_status(conn, task_id, "queued")
        raise
    except config.BudgetExceeded as exc:
        # A mid-loop failure must not silently replay proposals the next day.
        tasks.terminate_task(conn, task_id, "failed", str(exc))
        raise
    except Exception as exc:
        tasks.record_step(conn, task_id, "agent_loop", "failed", str(exc))
        tasks.terminate_task(conn, task_id, "failed", str(exc))
        raise
    tasks.record_step(conn, task_id, "agent_loop", "succeeded", f"run_id={trace['run_id']}")
    tasks.update_after_run(conn, task_id, chat_id, trace)
    if tasks.get_task(conn, task_id)["status"] in {"failed", "cancelled"}:
        return trace
    if task["workflow"] == "morning" and trace.get("status") != "limited":
        conn.execute(
            "INSERT INTO notifications (title,message,is_read,created_at) VALUES (?,?,0,?)",
            (f"[晨报] {datetime.now().date()} 投资晨报已生成", trace["answer"][:900], runtime._now()),
        )
        conn.commit()
    if task["workflow"] == "monitor" and trace.get("status") != "limited":
        try:
            text = trace["answer"].strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            decision = json.loads(text)
            if not isinstance(decision.get("notify"), bool) or not decision.get("reason"):
                raise ValueError("监控结论缺少 notify/reason")
            if decision["notify"]:
                conn.execute("INSERT INTO notifications (title,message,is_read,created_at) VALUES (?,?,0,?)",
                             ("[事件复核] 需要关注", str(decision["reason"])[:900], runtime._now()))
                conn.commit()
        except (ValueError, TypeError, AttributeError):
            tasks.set_task_status(conn, task_id, "failed", {"error": "监控结果不符合结构化约定，未发送提醒"})
    tasks.record_step(conn, task_id, "result", tasks.get_task(conn, task_id)["status"], "结果校验与审批状态已保存")
    return trace


def run_pending_tasks(conn, limit=5):
    """Worker hook: run queued tasks (config unset → leave them queued)."""
    tasks.recover_expired_tasks(conn)
    rows = conn.execute(
        "SELECT id FROM agent_tasks WHERE status IN ('queued','retrying') ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    ran = []
    for row in rows:
        try:
            run_task(conn, row["id"])
            ran.append((row["id"], "ok"))
        except config.AgentNotConfigured:
            ran.append((row["id"], "skipped:llm_not_configured"))
        except Exception as exc:
            ran.append((row["id"], f"failed:{exc}"))
    return ran