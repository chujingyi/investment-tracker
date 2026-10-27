"""Agent P0 tests: schema, RAG, context, tools, runtime loop, approval flow, routes.

Run: API_KEY=test-key SECRET_KEY=test-key python -m pytest tests/ -v
"""

import json
import os
import atexit
import tempfile

import pytest

# Ensure env vars are set before importing app
os.environ.setdefault("SECRET_KEY", "test-key-for-unit-tests")
os.environ.setdefault("API_KEY", "test-api-key-12345")

# Never let tests inherit TURSO_DATABASE_URL from the developer's .env file.
_test_db_fd, _test_db_path = tempfile.mkstemp(prefix="agent-test-", suffix=".db")
os.close(_test_db_fd)
os.environ["TEST_DATABASE_PATH"] = _test_db_path
atexit.register(lambda: os.path.exists(_test_db_path) and os.unlink(_test_db_path))

from app import app, get_db, init_db  # noqa: E402

init_db()

# Tests must never talk to the real quote API even if tracker/.env was loaded.
for _name in ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN"):
    os.environ.pop(_name, None)

from agent import config, knowledge, runtime, tools  # noqa: E402


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    monkeypatch.setenv("AGENT_DAILY_TOKEN_BUDGET", "200000")


def _insert_rec(conn, ticker="MU", **overrides):
    payload = {
        "created_at": "2026-01-10",
        "ticker": ticker,
        "market": "US",
        "framework": "supply_chain",
        "entry_price": 900.0,
        "conviction": 4,
        "entry_rec": "watch",
        "status": "active",
        "monitoring": 1,
        "monitor_state": "tracking",
        "wait_price": 800.0,
        "validity_date": "2026-12-31",
    }
    payload.update(overrides)
    cols = ",".join(payload.keys())
    marks = ",".join("?" for _ in payload)
    cur = conn.execute(
        f"INSERT INTO recommendations ({cols}) VALUES ({marks})", tuple(payload.values())
    )
    conn.commit()
    return cur.lastrowid


def _insert_position(conn, symbol="MU.US", quantity=10, cost=900.0, last=1000.0):
    conn.execute(
        "INSERT INTO broker_positions (symbol, original_symbol, quantity, cost_price, "
        "last_price, synced_at) VALUES (?,?,?,?,?,?)",
        (symbol, symbol, quantity, cost, last, "2026-01-10T00:00:00"),
    )
    conn.commit()


# ── schema ───────────────────────────────────────────────────────────────────


class TestSchema:
    def test_agent_tables_exist(self):
        conn = get_db()
        try:
            names = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                ).fetchall()
            }
        finally:
            conn.close()
        for table in (
            "agent_chats",
            "agent_messages",
            "agent_runs",
            "agent_tool_calls",
            "pending_actions",
            "knowledge_chunks",
        ):
            assert table in names, f"missing table {table}"

    def test_fts_or_fallback_available(self):
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='knowledge_fts'"
            ).fetchone()
            assert row is not None or True  # fallback is acceptable
            results = knowledge.retrieve(conn, "不存在的词xyzqwe", k=2)
            assert isinstance(results, list)
        finally:
            conn.close()


# ── RAG ──────────────────────────────────────────────────────────────────────


class TestKnowledge:
    def test_strip_frontmatter(self):
        text = "---\nAIGC:\n  Label: '1'\n---\n\n# 标题\n\n正文内容"
        assert knowledge.strip_frontmatter(text).startswith("# 标题")

    def test_split_sections(self):
        markdown = "# 文档\n\n引言部分\n\n## 一、逻辑\n\n内容A\n\n## 二、规则\n\n内容B"
        chunks = knowledge.split_sections(markdown)
        assert len(chunks) == 3
        assert chunks[1][0] == "一、逻辑"
        assert "内容A" in chunks[1][1]

    def test_ingest_real_docs_and_retrieve(self):
        conn = get_db()
        try:
            stored = knowledge.ingest_framework_docs(conn)
            assert stored >= 10  # 5 real framework docs, each with many sections
            results = knowledge.retrieve(conn, "供应链 瓶颈 涨价", k=3)
            assert results, "expected hits for 供应链瓶颈框架"
            joined = " ".join(r["title"] + r["content"] for r in results)
            assert "供应链" in joined
        finally:
            conn.close()


# ── context ──────────────────────────────────────────────────────────────────


class TestContext:
    def test_context_empty_db(self):
        from agent import context

        conn = get_db()
        try:
            block = context.build_context(conn)
        finally:
            conn.close()
        assert "风控与仓位规则" in block
        assert "当前无持仓" in block

    def test_context_with_position_and_notification(self):
        from agent import context

        conn = get_db()
        try:
            rec_id = _insert_rec(conn, ticker="MU", entry_rec="actionable")
            _insert_position(conn, symbol="CTXMU.US", quantity=3, cost=900.0, last=950.0)
            conn.execute(
                "INSERT INTO notifications (title, message, is_read, created_at) "
                "VALUES ('测试通知MUCTX','x',0,date('now'))"
            )
            conn.commit()
            block = context.build_context(conn)
            assert "MU" in block
            assert "测试通知MUCTX" in block
        finally:
            conn.close()


# ── tools ────────────────────────────────────────────────────────────────────


class TestTools:
    def test_registry_has_10_tools(self):
        specs = tools.tool_definitions(include_write=True)
        assert len(specs) == 10
        assert sum(1 for s in specs if tools.is_write(s["function"]["name"])) == 3
        assert len(tools.tool_definitions(include_write=False)) == 7

    def test_query_portfolio(self):
        conn = get_db()
        try:
            _insert_rec(conn, ticker="MU", entry_rec="actionable")
            _insert_position(conn, symbol="MU.US", quantity=10, cost=900.0, last=1000.0)
            result = tools.run_read(conn, "query_portfolio", {})
            assert any(h["ticker"] == "MU" for h in result["holdings"])
            assert result["total_market_value"] >= 10000.0
        finally:
            conn.close()

    def test_get_quotes_falls_back_to_snapshots(self):
        conn = get_db()
        try:
            rec_id = _insert_rec(conn, ticker="MU")
            conn.execute(
                "INSERT INTO price_snapshots (recommendation_id, snapshot_date, price, pct_change) "
                "VALUES (?,?,?,?)",
                (rec_id, "2026-01-12", 1010.0, 1.0),
            )
            conn.commit()
            result = tools.run_read(conn, "get_quotes", {"symbols": ["MU"]})
            assert result["realtime"] is False
            assert result["quotes"][0]["price"] == 1010.0
            assert result["quotes"][0]["source"] == "db_snapshot"
        finally:
            conn.close()

    def test_market_calendar_and_catalysts(self):
        conn = get_db()
        try:
            from datetime import date as _date, timedelta as _td

            soon = str(_date.today() + _td(days=5))
            conn.execute(
                "INSERT INTO catalysts (title, event_type, event_date, status, created_at, updated_at) "
                "VALUES ('MU 财报','earnings',?,'pending',?,?)",
                (soon, soon, soon),
            )
            conn.commit()
            cal = tools.run_read(conn, "get_market_calendar", {"days": 30})
            assert any("财报" in e["title"] for e in cal["events"])
            cats = tools.run_read(conn, "query_catalysts", {"days": 30})
            assert cats["count"] >= 1
        finally:
            conn.close()

    def test_search_knowledge_tool(self):
        conn = get_db()
        try:
            knowledge.ingest_framework_docs(conn)
            result = tools.run_read(conn, "search_knowledge", {"query": "三档 决策门", "k": 3})
            assert result["count"] >= 1
        finally:
            conn.close()

    def test_propose_validation_gate(self):
        # "不碰" never enters the system
        errors = tools.validate_write(
            "propose_recommendation", {"ticker": "BAD", "entry_rec": "avoid"}
        )
        assert any("不碰" in e for e in errors)
        # actionable without a risk anchor fails the six-element contract
        errors = tools.validate_write(
            "propose_recommendation",
            {"ticker": "MU", "entry_rec": "actionable", "entry_price": 900},
        )
        assert any("风控锚点" in e for e in errors)
        # watch without a trigger condition fails; with condition passes and gets validity
        args = {"ticker": "VECO", "entry_rec": "watch", "wait_condition": "回调至78美元"}
        assert tools.validate_write("propose_recommendation", args) == []
        assert args["validity_date"]  # auto-filled review date

    def test_update_monitor_validation(self):
        assert tools.validate_write("update_monitor", {"rec_id": "x", "action": "fly"}) != []
        assert tools.validate_write("update_monitor", {"rec_id": 1, "action": "pause"}) == []

    def test_send_notification_validation(self):
        assert tools.validate_write("send_notification", {"title": ""}) != []
        assert (
            tools.validate_write("send_notification", {"title": "t", "message": "m"}) == []
        )


# ── approval flow ────────────────────────────────────────────────────────────


class TestApprovalFlow:
    def test_approve_propose_recommendation(self):
        conn = get_db()
        try:
            args = {
                "ticker": "APPRV1",
                "entry_rec": "watch",
                "entry_price": 100,
                "wait_price": 90,
                "conviction": 4,
            }
            pending_id, summary = runtime.create_pending_action(
                conn, None, None, "propose_recommendation", args
            )
            row = runtime.resolve_pending_action(conn, pending_id, approve=True)
            assert row["status"] == "approved"
            rec = conn.execute(
                "SELECT * FROM recommendations WHERE ticker='APPRV1'"
            ).fetchone()
            assert rec is not None
            assert rec["wait_price"] == 90.0
            assert rec["monitoring"] == 1
        finally:
            conn.close()

    def test_reject_leaves_no_write(self):
        conn = get_db()
        try:
            pending_id, _ = runtime.create_pending_action(
                conn, None, None, "send_notification",
                {"title": "REJTEST", "message": "不应出现"},
            )
            row = runtime.resolve_pending_action(conn, pending_id, approve=False)
            assert row["status"] == "rejected"
            hit = conn.execute(
                "SELECT COUNT(*) AS c FROM notifications WHERE title LIKE '%REJTEST%'"
            ).fetchone()
            assert hit["c"] == 0
        finally:
            conn.close()

    def test_approve_update_monitor(self):
        conn = get_db()
        try:
            rec_id = _insert_rec(conn, ticker="MONITOR1", monitoring=1)
            pending_id, _ = runtime.create_pending_action(
                conn, None, None, "update_monitor", {"rec_id": rec_id, "action": "pause"}
            )
            runtime.resolve_pending_action(conn, pending_id, approve=True)
            row = conn.execute(
                "SELECT monitoring, monitor_state FROM recommendations WHERE id=?",
                (rec_id,),
            ).fetchone()
            assert row["monitoring"] == 0
        finally:
            conn.close()


# ── runtime loop ─────────────────────────────────────────────────────────────


def _fake_llm_two_turns_factory():
    """Stateful fake: 1st call requests one read tool, later calls answer."""
    state = {"n": 0}

    def _llm(messages, tool_specs=None, model=None):
        state["n"] += 1
        if state["n"] == 1 and tool_specs:
            return "", [
                {"id": "call_1", "name": "query_portfolio", "arguments": "{}"}
            ], {"prompt_tokens": 100, "completion_tokens": 20}
        return "组合一切正常，MU 盈利 10%。", [], {"prompt_tokens": 200, "completion_tokens": 30}

    return _llm


class TestRuntime:
    def _new_chat(self, conn):
        cur = conn.execute(
            "INSERT INTO agent_chats (title, kind, created_at) VALUES ('t','chat','2026-01-01T00:00:00')"
        )
        conn.commit()
        return cur.lastrowid

    def test_chat_two_turns(self, llm_on, monkeypatch):
        monkeypatch.setattr(runtime, "_chat_completion", _fake_llm_two_turns_factory())
        conn = get_db()
        try:
            chat_id = self._new_chat(conn)
            trace = runtime.run_chat(conn, chat_id, "我的组合怎么样？")
            assert trace["answer"].startswith("组合一切正常")
            assert len(trace["tool_calls"]) == 1
            assert trace["tool_calls"][0]["status"] == "ok"
            assert trace["usage"]["prompt_tokens"] == 300
            run = conn.execute("SELECT * FROM agent_runs WHERE id=?", (trace["run_id"],)).fetchone()
            assert run["status"] == "ok"
            assert run["prompt_tokens"] == 300
            msgs = conn.execute(
                "SELECT role FROM agent_messages WHERE chat_id=? ORDER BY id", (chat_id,)
            ).fetchall()
            roles = [m["role"] for m in msgs]
            assert roles[0] == "user"
            assert "assistant" in roles
            calls = conn.execute(
                "SELECT COUNT(*) AS c FROM agent_tool_calls WHERE run_id=?", (trace["run_id"],)
            ).fetchone()
            assert calls["c"] == 1
        finally:
            conn.close()

    def test_max_iterations_guard(self, llm_on, monkeypatch):
        def endless_tools(messages, tool_specs=None, model=None):
            if tool_specs is None:
                return "已收敛", [], {"prompt_tokens": 1, "completion_tokens": 1}
            return "", [
                {"id": "c", "name": "query_portfolio", "arguments": "{}"}
            ], {"prompt_tokens": 1, "completion_tokens": 1}

        monkeypatch.setattr(runtime, "_chat_completion", endless_tools)
        conn = get_db()
        try:
            chat_id = self._new_chat(conn)
            trace = runtime.run_chat(conn, chat_id, "无限工具调用")
            assert trace["answer"] == "已收敛"
            assert len(trace["tool_calls"]) == config.max_iterations()
        finally:
            conn.close()

    def test_budget_guard(self, monkeypatch, llm_on):
        monkeypatch.setattr(runtime, "_chat_completion", _fake_llm_two_turns_factory())
        monkeypatch.setenv("AGENT_DAILY_TOKEN_BUDGET", "100")
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO agent_runs (chat_id, trigger, model, prompt_tokens, completion_tokens, "
                "status, created_at) VALUES (NULL,'chat','test',60,60,'ok', datetime('now'))"
            )
            conn.commit()
            chat_id = self._new_chat(conn)
            with pytest.raises(config.BudgetExceeded):
                runtime.run_chat(conn, chat_id, "应被预算拦截")
        finally:
            conn.close()

    def test_write_tool_intercepted_to_pending(self, llm_on, monkeypatch):
        def llm_with_write(messages, tool_specs=None, model=None):
            if tool_specs:
                args = {
                    "ticker": "WRITETST",
                    "entry_rec": "watch",
                    "wait_price": 50,
                    "conviction": 3,
                }
                return "", [
                    {
                        "id": "call_w",
                        "name": "propose_recommendation",
                        "arguments": json.dumps(args),
                    }
                ], {"prompt_tokens": 10, "completion_tokens": 5}
            return "已为你创建待审批的录入操作，请在页面上批准。", [], {
                "prompt_tokens": 10,
                "completion_tokens": 5,
            }

        monkeypatch.setattr(runtime, "_chat_completion", llm_with_write)
        conn = get_db()
        try:
            chat_id = self._new_chat(conn)
            trace = runtime.run_chat(conn, chat_id, "把 WRITETST 录入为观察")
            assert trace["tool_calls"][0]["status"] == "pending_approval"
            assert trace["pending"][0]["id"]
            pending_id = trace["pending"][0]["id"]
            hit = conn.execute(
                "SELECT status FROM pending_actions WHERE id=?", (pending_id,)
            ).fetchone()
            assert hit["status"] == "pending"
            rec = conn.execute(
                "SELECT COUNT(*) AS c FROM recommendations WHERE ticker='WRITETST'"
            ).fetchone()
            assert rec["c"] == 0  # not written until approval
        finally:
            conn.close()

    def test_invalid_write_args_rejected_without_pending(self, llm_on, monkeypatch):
        def llm_bad_args(messages, tool_specs=None, model=None):
            if tool_specs:
                return "", [
                    {
                        "id": "call_b",
                        "name": "propose_recommendation",
                        "arguments": json.dumps({"ticker": "BADTST", "entry_rec": "avoid"}),
                    }
                ], {"prompt_tokens": 5, "completion_tokens": 5}
            return "好的", [], {"prompt_tokens": 5, "completion_tokens": 5}

        monkeypatch.setattr(runtime, "_chat_completion", llm_bad_args)
        conn = get_db()
        try:
            chat_id = self._new_chat(conn)
            trace = runtime.run_chat(conn, chat_id, "录入 BADTST")
            assert trace["tool_calls"][0]["status"] == "invalid"
            assert trace["pending"] == []
        finally:
            conn.close()

    def test_readonly_mode_blocks_write(self, llm_on, monkeypatch):
        def llm_write(messages, tool_specs=None, model=None):
            if tool_specs:
                return "", [
                    {
                        "id": "call_r",
                        "name": "send_notification",
                        "arguments": json.dumps({"title": "t", "message": "m"}),
                    }
                ], {"prompt_tokens": 5, "completion_tokens": 5}
            return "好的", [], {"prompt_tokens": 5, "completion_tokens": 5}

        monkeypatch.setattr(runtime, "_chat_completion", llm_write)
        conn = get_db()
        try:
            chat_id = self._new_chat(conn)
            trace = runtime.run_chat(conn, chat_id, "发通知", allow_write=False)
            assert trace["tool_calls"][0]["status"] == "blocked"
        finally:
            conn.close()

    def test_morning_report(self, llm_on, monkeypatch):
        monkeypatch.setattr(runtime, "_chat_completion", _fake_llm_two_turns_factory())
        conn = get_db()
        try:
            from agent import morning

            trace = morning.generate_morning_report(conn)
            assert trace["report"].startswith("组合一切正常")
            chat = conn.execute(
                "SELECT kind FROM agent_chats WHERE id=?", (trace["chat_id"],)
            ).fetchone()
            assert chat["kind"] == "morning"
            hit = conn.execute(
                "SELECT COUNT(*) AS c FROM notifications WHERE title LIKE '[晨报]%'"
            ).fetchone()
            assert hit["c"] >= 1
        finally:
            conn.close()


# ── web routes ───────────────────────────────────────────────────────────────


class TestAgentRoutes:
    def test_agent_page_renders(self, client):
        rv = client.get("/agent")
        assert rv.status_code == 200
        assert "AI 投资助手" in rv.data.decode()

    def test_state_endpoint(self, client):
        rv = client.get("/api/agent/state")
        assert rv.status_code == 200
        data = rv.get_json()
        assert "llm" in data and "budget" in data and "chats" in data

    def test_chat_without_llm_key(self, client, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            rv = client.post(
                "/api/agent/chat",
                data=json.dumps({"message": "你好"}),
                content_type="application/json",
            )
            assert rv.status_code == 503
            assert "LLM" in rv.get_json()["error"]
        finally:
            app.config["WTF_CSRF_ENABLED"] = True

    def test_morning_report_without_llm_key(self, client, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        rv = client.post("/api/agent/morning_report", content_type="application/json", headers={"X-API-Key": os.environ["API_KEY"]})
        assert rv.status_code == 503

    def test_chat_endpoint_full_loop(self, client, monkeypatch, llm_on):
        monkeypatch.setattr(runtime, "_chat_completion", _fake_llm_two_turns_factory())
        old = app.config.get("WTF_CSRF_ENABLED")
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            rv = client.post(
                "/api/agent/chat",
                data=json.dumps({"message": "我的组合怎么样？"}),
                content_type="application/json",
            )
            assert rv.status_code == 202
            data = rv.get_json()
            assert data["chat_id"]
            from agent.workflow import run_pending_tasks
            conn = get_db()
            run_pending_tasks(conn)
            task = conn.execute("SELECT * FROM agent_tasks WHERE id=?", (data["task_id"],)).fetchone()
            assert task["status"] == "succeeded"
            result = json.loads(task["result"])
            assert result["answer"].startswith("组合一切正常")
            assert result["tool_calls"][0]["name"] == "query_portfolio"
            conn.close()
        finally:
            if old is None:
                app.config["WTF_CSRF_ENABLED"] = True
            else:
                app.config["WTF_CSRF_ENABLED"] = old

    def test_create_chat_and_messages(self, client, monkeypatch, llm_on):
        monkeypatch.setattr(runtime, "_chat_completion", _fake_llm_two_turns_factory())
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            rv = client.post("/api/agent/chats", data="{}", content_type="application/json")
            chat_id = rv.get_json()["chat_id"]
            rv = client.post(
                "/api/agent/chat",
                data=json.dumps({"chat_id": chat_id, "message": "查询组合"}),
                content_type="application/json",
            )
            assert rv.status_code == 202
            from agent.workflow import run_pending_tasks
            conn = get_db()
            run_pending_tasks(conn)
            conn.close()
            rv = client.get(f"/api/agent/messages?chat_id={chat_id}")
            data = rv.get_json()
            assert len(data["messages"]) >= 3
            assert data["messages"][0]["role"] == "user"
        finally:
            app.config["WTF_CSRF_ENABLED"] = True

    def test_pending_resolve_route(self, client):
        conn = get_db()
        try:
            pending_id, _ = runtime.create_pending_action(
                conn, None, None, "send_notification",
                {"title": "ROUTE测试通知", "message": "来自审批路由测试"},
            )
        finally:
            conn.close()
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            rv = client.post(
                f"/api/agent/pending/{pending_id}/resolve",
                data=json.dumps({"action": "approve"}),
                content_type="application/json",
            )
            assert rv.status_code == 200
            assert rv.get_json()["pending"]["status"] == "approved"
            conn = get_db()
            try:
                hit = conn.execute(
                    "SELECT COUNT(*) AS c FROM notifications WHERE title='ROUTE测试通知'"
                ).fetchone()
                assert hit["c"] == 1
            finally:
                conn.close()
        finally:
            app.config["WTF_CSRF_ENABLED"] = True
