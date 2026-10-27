"""Agent P1 tests: events, task state machine, workflows, routes.

Run: API_KEY=test-key SECRET_KEY=test-key python -m pytest tests/ -v
"""

import json
import os
import atexit
import tempfile
from datetime import date, timedelta

import pytest

os.environ.setdefault("SECRET_KEY", "test-key-for-unit-tests")
os.environ.setdefault("API_KEY", "test-api-key-12345")

_test_db_fd, _test_db_path = tempfile.mkstemp(prefix="agent-p1-test-", suffix=".db")
os.close(_test_db_fd)
os.environ["TEST_DATABASE_PATH"] = _test_db_path
atexit.register(lambda: os.path.exists(_test_db_path) and os.unlink(_test_db_path))

from app import app, get_db, init_db  # noqa: E402

init_db()

for _name in ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN"):
    os.environ.pop(_name, None)

from agent import events, runtime, tasks, workflow  # noqa: E402


@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    monkeypatch.setenv("AGENT_DAILY_TOKEN_BUDGET", "200000")


def _llm_factory(tool_call=None):
    """Stateful fake: first call issues an optional tool call, later calls answer."""
    state = {"n": 0}

    def _llm(messages, tool_specs=None, model=None):
        state["n"] += 1
        if tool_call and tool_specs and state["n"] == 1:
            return "", [tool_call], {"prompt_tokens": 10, "completion_tokens": 5}
        return "处理完成，结论已给出。", [], {"prompt_tokens": 20, "completion_tokens": 10}

    return _llm


def _watch_rec(conn, ticker="WATCH1", validity_days=-1):
    cur = conn.execute(
        "INSERT INTO recommendations (created_at, ticker, market, framework, entry_price, "
        "conviction, entry_rec, status, monitoring, monitor_state, wait_price, validity_date) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(date.today() - timedelta(days=10)), ticker, "US", "supply_chain", 100.0,
            3, "watch", "active", 1, "tracking", 90.0,
            str(date.today() + timedelta(days=validity_days)),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _price_snapshot(conn, rec_id, price, days_ago=1):
    conn.execute(
        "INSERT INTO price_snapshots (recommendation_id, snapshot_date, price, pct_change) "
        "VALUES (?,?,?,?)",
        (rec_id, str(date.today() - timedelta(days=days_ago)), price, 0.0),
    )
    conn.commit()


# ── events ──────────────────────────────────────────────────────────────────


class TestEvents:
    def test_review_due_scanned_and_deduped(self):
        conn = get_db()
        try:
            _watch_rec(conn, "DUE1", validity_days=-1)
            counts = events.scan_events(conn)
            assert counts["review_due"] >= 1
            first_total = len(events.list_events(conn))
            events.scan_events(conn)  # same due state → dedup
            assert len(events.list_events(conn)) == first_total
        finally:
            conn.close()

    def test_calendar_soon_event(self):
        conn = get_db()
        try:
            soon = str(date.today() + timedelta(days=3))
            conn.execute(
                "INSERT INTO catalysts (title, event_type, event_date, status, created_at, updated_at) "
                "VALUES ('MU 财报','earnings',?,'pending',?,?)",
                (soon, soon, soon),
            )
            conn.commit()
            counts = events.scan_events(conn)
            assert counts["calendar_soon"] >= 1
        finally:
            conn.close()

    def test_price_approaching_event(self):
        conn = get_db()
        try:
            rec_id = _watch_rec(conn, "PRICE1", validity_days=90)
            # wait 110, breakout above it at 115 → triggered
            conn.execute(
                "UPDATE recommendations SET entry_price=100, wait_price=110, monitor_direction='breakout' WHERE id=?",
                (rec_id,),
            )
            conn.commit()
            _price_snapshot(conn, rec_id, 115.0)
            counts = events.scan_events(conn)
            assert counts["price_approaching"] >= 1
            hit = [
                e for e in events.list_events(conn)
                if e["event_type"] == "price_approaching"
                and e["payload"].get("ticker") == "PRICE1"
            ]
            assert hit and hit[0]["payload"]["state"] == "triggered"
        finally:
            conn.close()

    def test_manual_event_with_nonce(self):
        conn = get_db()
        try:
            events.insert_event(conn, "manual_research", {"ticker": "NVDA"}, nonce=1)
            events.insert_event(conn, "manual_research", {"ticker": "NVDA"}, nonce=2)
            hit = [e for e in events.list_events(conn) if e["event_type"] == "manual_research"]
            assert len(hit) == 2
        finally:
            conn.close()

    def test_process_events_creates_queued_task(self):
        conn = get_db()
        try:
            rec_id = _watch_rec(conn, "DISP")
            events.scan_events(conn)
            dispatched = events.process_events(conn)
            assert dispatched
            task = tasks.get_task(conn, dispatched[0][1])
            assert task["status"] == "queued"
        finally:
            conn.close()


# ── task state machine ──────────────────────────────────────────────────────


class TestTasks:
    def test_create_and_list(self):
        conn = get_db()
        try:
            task_id = tasks.create_task(conn, "review", "review", payload={"ticker": "X"})
            task = tasks.get_task(conn, task_id)
            assert task["status"] == "queued"
            assert task["status_label"] in tasks.TASK_LABELS.values()
            assert tasks.list_tasks(conn, status="queued")
        finally:
            conn.close()

    def test_run_task_success(self, llm_on, monkeypatch):
        monkeypatch.setattr(runtime, "_chat_completion", _llm_factory())
        conn = get_db()
        try:
            task_id = tasks.create_task(conn, "review", "review", {"items": []})
            trace = workflow.run_task(conn, task_id)
            task = tasks.get_task(conn, task_id)
            assert task["status"] == "succeeded"
            assert task["chat_id"]
            assert task["prompt_tokens"] >= 20
            assert trace["answer"]
        finally:
            conn.close()

    def test_pending_marks_waiting_approval(self, llm_on, monkeypatch):
        tool_call = {
            "id": "c1", "name": "send_notification",
            "arguments": json.dumps({"title": "提醒", "message": "测试"}),
        }
        monkeypatch.setattr(runtime, "_chat_completion", _llm_factory(tool_call=tool_call))
        monkeypatch.setenv("AGENT_MAX_ITERATIONS", "1")
        conn = get_db()
        try:
            task_id = tasks.create_task(conn, "review", "review", {"items": []})
            workflow.run_task(conn, task_id)
            task = tasks.get_task(conn, task_id)
            assert task["status"] == "waiting_approval"
        finally:
            conn.close()

    def test_approval_approve_marks_succeeded(self, llm_on, monkeypatch):
        tool_call = {
            "id": "c1", "name": "send_notification",
            "arguments": json.dumps({"title": "t", "message": "m"}),
        }
        monkeypatch.setattr(runtime, "_chat_completion", _llm_factory(tool_call=tool_call))
        monkeypatch.setenv("AGENT_MAX_ITERATIONS", "1")
        conn = get_db()
        try:
            task_id = tasks.create_task(conn, "review", "review", {"items": []})
            workflow.run_task(conn, task_id)
            task = tasks.get_task(conn, task_id)
            pending = conn.execute("SELECT id FROM pending_actions WHERE task_id=?", (task_id,)).fetchone()
            runtime.resolve_pending_action(conn, pending["id"], True)
            updated = tasks.on_pending_resolved(conn, task["chat_id"], True, task_id=task_id)
            assert updated["status"] == "succeeded"
        finally:
            conn.close()

    def test_rejection_reruns_once_then_archives(self, llm_on, monkeypatch):
        # Always produce a pending write → each run ends waiting_approval.
        tool_call = {
            "id": "c", "name": "send_notification",
            "arguments": json.dumps({"title": "t", "message": "m"}),
        }

        def _always_pending(messages, tool_specs=None, model=None):
            if tool_specs:
                return "", [tool_call], {"prompt_tokens": 5, "completion_tokens": 5}
            return "好的", [], {"prompt_tokens": 5, "completion_tokens": 5}

        monkeypatch.setattr(runtime, "_chat_completion", _always_pending)
        monkeypatch.setenv("AGENT_MAX_ITERATIONS", "1")
        conn = get_db()
        try:
            task_id = tasks.create_task(conn, "review", "review", {"items": []})
            workflow.run_task(conn, task_id)  # attempt 1 → waiting_approval
            task = tasks.get_task(conn, task_id)
            assert task["attempt"] == 1
            # First rejection → rework (attempt 2), still pending
            pending = conn.execute("SELECT id FROM pending_actions WHERE task_id=? AND status='pending'", (task_id,)).fetchone()
            runtime.resolve_pending_action(conn, pending["id"], False)
            task = tasks.on_pending_resolved(conn, task["chat_id"], False, "建议关闭", task_id=task_id)
            assert task["status"] == "retrying"
            workflow.run_pending_tasks(conn)
            task = tasks.get_task(conn, task_id)
            assert task["attempt"] == 2
            # Second rejection → archived
            pending = conn.execute("SELECT id FROM pending_actions WHERE task_id=? AND status='pending'", (task_id,)).fetchone()
            runtime.resolve_pending_action(conn, pending["id"], False)
            task = tasks.on_pending_resolved(conn, task["chat_id"], False, "仍建议关闭", task_id=task_id)
            assert task["status"] == "cancelled"
        finally:
            conn.close()


# ── workflows ───────────────────────────────────────────────────────────────


class TestWorkflow:
    def test_registry_and_routing(self, monkeypatch):
        assert set(workflow.WORKFLOWS) == {"morning", "monitor", "review", "research", "chat"}
        assert workflow.route_agent_type("review") == "review"
        assert workflow.route_agent_type("research") == "research"
        monkeypatch.setenv("LLM_FAST_MODEL", "fast-model")
        monkeypatch.setenv("LLM_STRONG_MODEL", "strong-model")
        assert workflow.model_for("monitor") == "fast-model"
        assert workflow.model_for("research") == "strong-model"

    def test_build_prompt_shapes(self):
        assert "晨报" in workflow.build_prompt("morning")
        prompt = workflow.build_prompt("review", {"items": [{"rec_id": 1, "ticker": "MU", "validity_date": "2026-10-20"}]})
        assert "MU" in prompt
        assert "关闭" in prompt  # review workflow includes the close path
        research = workflow.build_prompt("research", {"ticker": "mu"})
        assert "MU" in research
        hint = workflow.build_prompt("review", retry_hint="用户拒绝了")
        assert "审批反馈" in hint

    def test_manual_research_flow(self, llm_on, monkeypatch):
        monkeypatch.setattr(runtime, "_chat_completion", _llm_factory())
        conn = get_db()
        try:
            task = workflow.create_task_and_run(conn, "research", payload={"ticker": "NVDA"})
            assert task["status"] == "succeeded"
            assert task["chat_id"]
        finally:
            conn.close()

    def test_run_pending_tasks_skips_without_llm(self, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        conn = get_db()
        try:
            task_id = tasks.create_task(conn, "monitor", "monitor", {"state": "approaching"})
            ran = workflow.run_pending_tasks(conn)
            assert any(status == "skipped:llm_not_configured" for _, status in ran)
            assert tasks.get_task(conn, task_id)["status"] == "queued"
        finally:
            conn.close()


# ── routes ──────────────────────────────────────────────────────────────────


class TestP1Routes:
    def test_events_and_tasks_endpoints(self):
        conn = get_db()
        try:
            _watch_rec(conn, "ROUTE1", validity_days=-1)
            events.scan_events(conn)
            events.process_events(conn)
        finally:
            conn.close()
        app.config["TESTING"] = True
        with app.test_client() as client:
            rv = client.get("/api/agent/events")
            assert rv.status_code == 200
            assert "events" in rv.get_json()
            rv = client.get("/api/agent/tasks")
            assert rv.status_code == 200
            assert isinstance(rv.get_json()["tasks"], list)

    def test_scan_endpoint_without_llm(self, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        with app.test_client() as client:
            rv = client.post("/api/agent/events/scan", headers={"X-API-Key": os.environ["API_KEY"]})
            assert rv.status_code == 200
            data = rv.get_json()
            assert "scanned" in data and data["ran"] == []  # HTTP never executes queued tasks

    def test_workflow_run_endpoint_missing_ticker(self):
        with app.test_client() as client:
            rv = client.post(
                "/api/agent/workflows/research/run",
                data=json.dumps({}), content_type="application/json",
            )
            assert rv.status_code == 400

    def test_workflow_run_endpoint_success(self, llm_on, monkeypatch):
        monkeypatch.setattr(runtime, "_chat_completion", _llm_factory())
        app.config["WTF_CSRF_ENABLED"] = False
        try:
            with app.test_client() as client:
                rv = client.post(
                    "/api/agent/workflows/review/run",
                    data=json.dumps({}), content_type="application/json",
                )
                assert rv.status_code == 202
                task = rv.get_json()["task"]
                assert task["status"] == "queued"
                conn = get_db()
                workflow.run_pending_tasks(conn)
                assert tasks.get_task(conn, task["id"])["status"] == "succeeded"
                conn.close()
        finally:
            app.config["WTF_CSRF_ENABLED"] = True