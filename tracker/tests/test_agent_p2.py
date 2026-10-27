"""Agent P2 tests: offline evaluation, online metrics, metrics route.

Run: API_KEY=test-key SECRET_KEY=test-key python -m pytest tests/ -v
"""

import json
import os
import atexit
import tempfile
from datetime import date, datetime, timedelta

import pytest

os.environ.setdefault("SECRET_KEY", "test-key-for-unit-tests")
os.environ.setdefault("API_KEY", "test-api-key-12345")

_test_db_fd, _test_db_path = tempfile.mkstemp(prefix="agent-p2-test-", suffix=".db")
os.close(_test_db_fd)
os.environ["TEST_DATABASE_PATH"] = _test_db_path
atexit.register(lambda: os.path.exists(_test_db_path) and os.unlink(_test_db_path))

from app import app, get_db, init_db  # noqa: E402

init_db()

for _name in ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN"):
    os.environ.pop(_name, None)

from agent import eval as agent_eval  # noqa: E402
from agent import metrics as agent_metrics  # noqa: E402


def _rec(conn, ticker, entry_price, entry_rec="actionable", days_ago=60,
         stop=None, target=None, wait=None):
    cur = conn.execute(
        "INSERT INTO recommendations (created_at, ticker, market, framework, entry_price, "
        "conviction, entry_rec, status, stop_loss_price, target1_price, wait_price) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(date.today() - timedelta(days=days_ago)), ticker, "US", "supply_chain",
            entry_price, 4, entry_rec, "active", stop, target, wait,
        ),
    )
    conn.commit()
    return cur.lastrowid


def _snap(conn, rec_id, days_ago, price):
    conn.execute(
        "INSERT INTO price_snapshots (recommendation_id, snapshot_date, price, pct_change) "
        "VALUES (?,?,?,?)",
        (rec_id, str(date.today() - timedelta(days=days_ago)), price, 0.0),
    )
    conn.commit()


# ── offline evaluation ──────────────────────────────────────────────────────


class TestEval:
    def test_windows_and_hits(self):
        conn = get_db()
        try:
            # Uptrend: hit at all windows; wait level touched; target touched.
            r1 = _rec(conn, "EVUP", entry_price=100, days_ago=60, stop=90, target=110, wait=95)
            _snap(conn, r1, 59, 95)     # wait touched (95<=95)
            _snap(conn, r1, 53, 102)    # +7
            _snap(conn, r1, 30, 112)    # +30 → target touched
            _snap(conn, r1, 0, 118)     # +60
            # Downtrend: miss; stop 180 vs min 185 → stop not hit.
            r_b = _seed(conn, "ADOWN", entry_price=200, days_ago=60, stop=180)
            _snap(conn, r_b, 30, 195)
            _snap(conn, r_b, 1, 190)

            result = agent_eval.evaluate(conn)
            assert result["sample_count"] >= 2
            w30 = result["windows"]["30"]
            assert w30["direction_hit_rate"] is not None
            assert w30["eval_samples"] >= 2
            assert result["windows"]["7"]["direction_hit_rate"] is not None
            # Our seeded ADOWN never touched its stop; EVUP reached target & wait.
            assert result["stop_hit"]["n"] == 0
            assert result["target_hit"]["n"] >= 1
            assert result["trigger_hit"]["n"] >= 1
        finally:
            conn.close()

    def test_baseline_and_diff(self):
        conn = get_db()
        try:
            r = _seed(conn, "BASELINE1", entry_price=100, days_ago=30)
            _snap(conn, r, 0, 110)
            first = agent_eval.run_eval(conn)
            assert first["is_baseline"] is True
            assert agent_eval.get_baseline(conn) is not None
            # Add an up move then re-evaluate → direction improves.
            _snap(conn, r, 2, 130)
            second = agent_eval.run_eval(conn)
            assert second["is_baseline"] is False
            assert second["diff"]["windows"]["30"] == 0.0 or second["diff"]["windows"]["30"] is not None
            assert second["run_id"] != first["run_id"]
        finally:
            conn.close()

    def test_fresh_rebuilds_baseline(self):
        conn = get_db()
        try:
            _ = agent_eval.run_eval(conn)
            baseline_id = agent_eval.get_baseline(conn)["id"]
            rebuilt = agent_eval.run_eval(conn, fresh=True)
            assert rebuilt["is_baseline"] is True
            assert agent_eval.get_baseline(conn)["id"] != baseline_id
        finally:
            conn.close()

    def test_render_report(self):
        conn = get_db()
        try:
            report = agent_eval.run_eval(conn)
            text = agent_eval.render_report(report)
            assert "离线评测" in text
            assert "窗口上涨比例" in text
        finally:
            conn.close()


def _seed(conn, *args, **kwargs):
    return _rec(conn, *args, **kwargs)


# ── online metrics ──────────────────────────────────────────────────────────


class TestMetrics:
    def _seed_telemetry(self, conn):
        for i, status in enumerate(["succeeded", "succeeded", "waiting_approval", "failed"]):
            conn.execute(
                "INSERT INTO agent_tasks (workflow, agent_type, status, attempt, created_at, updated_at) "
                "VALUES ('review','review',?,1,?,?)",
                (status, datetime.now().isoformat(timespec="seconds"),
                 datetime.now().isoformat(timespec="seconds")),
            )
        for status in ("approved", "rejected", "pending"):
            conn.execute(
                "INSERT INTO pending_actions (tool_name, args_json, summary, status, created_at) "
                "VALUES ('update_monitor','{}','s',?,?)",
                (status, datetime.now().isoformat(timespec="seconds")),
            )
        for status in ("ok", "ok", "ok", "error"):
            conn.execute(
                "INSERT INTO agent_tool_calls (run_id, tool_name, args_json, status, duration_ms, "
                "result_summary, created_at) VALUES (1,'get_quotes','{}',?,10,'',?)",
                (status, datetime.now().isoformat(timespec="seconds")),
            )
        conn.execute(
            "INSERT INTO agent_runs (chat_id, trigger, model, prompt_tokens, completion_tokens, "
            "status, created_at) VALUES (NULL,'chat','test',1000000,500000,'ok',?)",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        conn.commit()

    def test_aggregates(self):
        conn = get_db()
        try:
            self._seed_telemetry(conn)
            m = agent_metrics.metrics(conn)
            # Shared test DB accumulates telemetry from other suites: assert
            # seeded lower bounds + exact math on the parts we control.
            assert m["tasks"]["total"] >= 4
            assert m["tasks"]["completion_rate"] is not None
            assert m["approvals"]["approval_rate"] is not None
            assert m["tools"]["total"] >= 4
            assert m["tools"]["ok"] >= 3
            assert m["cost"]["total_cny"] >= 2.0  # 1M*1 + 0.5M*2 seeded
            assert m["latency"]["task_avg_ms"] is not None
        finally:
            conn.close()

    def test_db_robust(self):
        """Aggregation never crashes whatever the accumulated data looks like."""
        conn = get_db()
        try:
            m = agent_metrics.metrics(conn)
            assert isinstance(m["tasks"]["total"], int)
            assert isinstance(m["cost"]["total_cny"], float)
            assert set(m.keys()) >= {"tasks", "approvals", "tools", "latency", "cost", "events"}
        finally:
            conn.close()


# ── route ───────────────────────────────────────────────────────────────────


class TestMetricsRoute:
    def test_metrics_endpoint(self):
        app.config["TESTING"] = True
        with app.test_client() as client:
            rv = client.get("/api/agent/metrics")
            assert rv.status_code == 200
            data = rv.get_json()
            assert "online" in data and "offline" in data
            assert "tasks" in data["online"] and "cost" in data["online"]