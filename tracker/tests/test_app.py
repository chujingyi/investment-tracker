"""Basic tests for the stock tracker Flask application.

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
_test_db_fd, _test_db_path = tempfile.mkstemp(prefix="tracker-test-", suffix=".db")
os.close(_test_db_fd)
os.environ["TEST_DATABASE_PATH"] = _test_db_path
atexit.register(lambda: os.path.exists(_test_db_path) and os.unlink(_test_db_path))

from app import app, init_db

init_db()


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def api_headers():
    return {"X-API-Key": os.environ["API_KEY"], "Content-Type": "application/json"}


# ── Page Rendering Tests ───────────────────────────────────────────


class TestPages:
    def test_dashboard(self, client):
        rv = client.get("/")
        assert rv.status_code == 200
        assert "投资复盘" in rv.data.decode()

    def test_sessions_page(self, client):
        rv = client.get("/sessions")
        assert rv.status_code == 200

    def test_add_page(self, client):
        rv = client.get("/add")
        assert rv.status_code == 200

    def test_recommendations_page(self, client):
        rv = client.get("/recommendations")
        assert rv.status_code == 200
        body = rv.data.decode()
        assert "股票全流程" in body
        assert "stock-tabs" in body
        assert 'select name="lifecycle"' not in body

    def test_theme_map_uses_unified_views(self, client):
        rv = client.get("/themes")
        assert rv.status_code == 200
        body = rv.data.decode()
        assert "主题地图" in body
        assert "轮动概览" in body
        assert "供应链定位" in body
        assert 'href="/monitor"' in body

        supply = client.get("/themes?view=supply&theme=半导体")
        assert supply.status_code == 200
        supply_body = supply.data.decode()
        assert "AI 基础设施产业链" in supply_body
        assert "theme-dim" in supply_body

    def test_legacy_theme_routes_redirect(self, client):
        assert client.get("/rotation").status_code == 302
        assert client.get("/supply-chain").status_code == 302

    def test_monitor_page_has_sector_and_macro_tabs(self, client):
        rv = client.get("/monitor")
        assert rv.status_code == 200
        body = rv.data.decode()
        assert "板块信号" in body
        assert "宏观事件" in body


# ── CSRF Protection Tests ──────────────────────────────────────────


class TestCSRF:
    def test_post_without_csrf_rejected(self, client):
        rv = client.post("/add", data={"ticker": "TEST"})
        assert rv.status_code == 400


# ── API Authentication Tests ───────────────────────────────────────


class TestAPIAuth:
    def test_api_no_key_returns_401(self, client):
        rv = client.post("/api/add_session", json={"question": "test"})
        assert rv.status_code == 401

    def test_api_wrong_key_returns_401(self, client):
        rv = client.post(
            "/api/add_session",
            json={"question": "test"},
            headers={"X-API-Key": "wrong-key"},
        )
        assert rv.status_code == 401

    def test_api_no_key_blocked_when_not_configured(self, client, monkeypatch):
        """API endpoints should return 503 if API_KEY is not set."""
        import app as app_module

        orig_key = app_module.API_KEY
        monkeypatch.setattr(app_module, "API_KEY", None)

        rv = client.post(
            "/api/add_session", json={"question": "test"}, headers={"X-API-Key": "any"}
        )
        assert rv.status_code == 503

# ── API Functional Tests ───────────────────────────────────────────


class TestAPIEndpoints:
    def test_sector_data_reconnects_after_closed_context(self, client, monkeypatch):
        import app as app_module

        reconnect_calls = []
        monkeypatch.setattr(
            app_module, "_sector_data_cache", {"payload": None, "cached_at": 0.0}
        )
        monkeypatch.setattr(
            app_module,
            "_get_market_data_client",
            lambda reconnect=False: reconnect_calls.append(reconnect) or object(),
        )
        attempts = iter((RuntimeError("context closed"), {"sectors": [], "spy": {}, "updated_at": "now", "stale": False}))

        def calculate(_client):
            result = next(attempts)
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(app_module, "_calculate_sector_data", calculate)
        rv = client.get("/api/sector_data?refresh=1")
        assert rv.status_code == 200
        assert reconnect_calls == [False, True]

    def test_sector_data_uses_last_success_after_live_failure(self, client, monkeypatch):
        import app as app_module

        cached = {"sectors": [], "spy": {}, "updated_at": "earlier", "stale": False}
        monkeypatch.setattr(
            app_module, "_sector_data_cache", {"payload": cached, "cached_at": 0.0}
        )
        monkeypatch.setattr(app_module, "_get_market_data_client", lambda reconnect=False: object())
        monkeypatch.setattr(
            app_module,
            "_calculate_sector_data",
            lambda _client: (_ for _ in ()).throw(RuntimeError("temporary failure")),
        )
        rv = client.get("/api/sector_data?refresh=1")
        assert rv.status_code == 200
        assert rv.get_json()["stale"] is True

    def test_add_session(self, client, api_headers):
        rv = client.post(
            "/api/add_session",
            json={"question": "pytest session", "theme": "momentum"},
            headers=api_headers,
        )
        assert rv.status_code == 200
        data = json.loads(rv.data)
        assert "session_id" in data

    def test_add_rec(self, client, api_headers):
        # First create a session
        rv = client.post(
            "/api/add_session",
            json={"question": "pytest session for rec"},
            headers=api_headers,
        )
        sid = json.loads(rv.data)["session_id"]

        rv = client.post(
            "/api/add_rec",
            json={
                "ticker": "TSLA",
                "session_id": sid,
                "framework": "momentum",
                "entry_price": 250,
            },
            headers=api_headers,
        )
        assert rv.status_code == 200

    def test_add_session_missing_question(self, client, api_headers):
        rv = client.post(
            "/api/add_session", json={"theme": "test"}, headers=api_headers
        )
        assert rv.status_code == 400

    def test_add_rec_missing_ticker(self, client, api_headers):
        rv = client.post("/api/add_rec", json={"framework": "momentum"}, headers=api_headers)
        assert rv.status_code == 400


# ── Database Tests ─────────────────────────────────────────────────


class TestDatabase:
    def test_db_indexes_exist(self):
        with app.app_context():
            from app import get_db

            db = get_db()
            indexes = db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            idx_names = {i["name"] for i in indexes}
            expected = {
                "idx_recs_session",
                "idx_recs_status",
                "idx_recs_monitoring",
                "idx_recs_created",
                "idx_snaps_rec",
                "idx_snaps_rec_date",
                "idx_sessions_created",
            }
            assert expected.issubset(idx_names)

    def test_foreign_keys_enabled(self):
        with app.app_context():
            from app import get_db

            db = get_db()
            fk = db.execute("PRAGMA foreign_keys").fetchone()
            assert fk[0] == 1

    def test_broker_tables_exist(self):
        with app.app_context():
            from app import get_db

            db = get_db()
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            names = {row["name"] for row in tables}
            assert {"broker_positions", "broker_executions"}.issubset(names)


# ── Security Tests ──────────────────────────────────────────────────


class TestSecurity:
    def test_ticker_validation(self):
        from app import _validate_ticker

        assert _validate_ticker("AAPL") is True
        assert _validate_ticker("BRK.B") is True
        assert _validate_ticker("123") is True
        assert _validate_ticker("aapl") is False  # lowercase
        assert _validate_ticker("AAPL; DROP TABLE") is False
        assert _validate_ticker("AAPL && echo pwned") is False

    def test_safe_float(self):
        from app import safe_float

        assert safe_float("123.45") == 123.45
        assert safe_float("N/A") == 0.0  # non-numeric returns default
        assert safe_float("N/A", None) is None  # custom default works
        assert safe_float(None) == 0.0  # None returns default 0.0
        assert safe_float(None, -1) == -1  # custom default
        assert safe_float(100) == 100.0

    def test_symbol_normalization_and_alias(self):
        from app import normalize_symbol, primary_theme

        assert normalize_symbol("NVDA", "US") == "NVDA.US"
        assert normalize_symbol("SKHYV.US", "US") == "SKHY.US"
        assert normalize_symbol("688017", "CN") == "688017.SH"
        assert normalize_symbol("300308", "CN") == "300308.SZ"
        assert primary_theme({"ticker": "QQQM", "market": "US"}) == "宽基指数"
        assert primary_theme({"ticker": "NVDA", "market": "US"}) == "半导体"


class TestStockLifecycle:
    def test_stock_detail_combines_research_position_and_trade(self, client):
        from app import get_db

        db = get_db()
        rec_cursor = db.execute(
            "INSERT INTO recommendations "
            "(created_at,ticker,market,framework,entry_price,entry_rec,status) "
            "VALUES (?,?,?,?,?,?,?)",
            ("2026-07-15", "FLOW", "US", "momentum", 10, "watch", "active"),
        )
        db.execute(
            "INSERT OR REPLACE INTO broker_positions "
            "(symbol,original_symbol,name,quantity,available_quantity,cost_price,currency,market,synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("FLOW.US", "FLOW.US", "Flow Test", 2, 2, 9.5, "USD", "US", "2026-07-15T12:00:00"),
        )
        db.execute(
            "INSERT OR REPLACE INTO broker_executions "
            "(trade_key,trade_id,order_id,symbol,original_symbol,side,quantity,price,trade_done_at,synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("flow-trade", "flow-trade", "flow-order", "FLOW.US", "FLOW.US", "Buy", 2, 9.5,
             "2026-07-15T10:00:00", "2026-07-15T12:00:00"),
        )
        db.commit()
        db.close()

        listing = client.get("/recommendations")
        assert listing.status_code == 200
        assert "FLOW" in listing.data.decode()
        assert "持有中" in listing.data.decode()

        history = client.get("/recommendations?lifecycle=history")
        assert history.status_code == 200
        assert "FLOW" in history.data.decode()

        detail = client.get("/stock/FLOW.US")
        body = detail.data.decode()
        assert detail.status_code == 200
        assert "Flow Test" in body
        assert "长桥成交记录" in body
        assert "9.5000" in body

        original_detail = client.get(f"/rec/{rec_cursor.lastrowid}")
        original_body = original_detail.data.decode()
        assert original_detail.status_code == 200
        assert "价格走势" in original_body
        assert "推荐详情" in original_body
        assert "长桥实盘" in original_body


class TestAIInfraMonitor:
    @staticmethod
    def _candles(start=100.0, step=1.0, volume=1000):
        return [
            {
                "close": start + i * step,
                "low": start + i * step - 1,
                "volume": volume - i,
            }
            for i in range(70)
        ]

    def test_signal_triggers_for_stable_uptrend(self):
        from app import calculate_ai_infra_signal

        candles = {
            "SOXX.US": self._candles(100, 1.0),
            "SMH.US": self._candles(120, 1.1),
            "NVDA.US": self._candles(80, 1.2),
            "TSM.US": self._candles(90, 1.15),
        }
        result = calculate_ai_infra_signal(candles)
        assert result["signal"] == "actionable"
        assert result["score"] >= 3

    def test_signal_rejects_insufficient_history(self):
        from app import calculate_ai_infra_signal

        candles = {symbol: self._candles()[:20] for symbol in (
            "SOXX.US", "SMH.US", "NVDA.US", "TSM.US"
        )}
        with pytest.raises(ValueError, match="60"):
            calculate_ai_infra_signal(candles)

    def test_ecs_worker_schedules_weekday_after_close(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from ai_monitor_worker import next_run_at

        tz = ZoneInfo("Asia/Shanghai")
        # Friday 06:00 → next slot is the same-day 20:30 event scan.
        friday = datetime(2026, 7, 10, 6, 0, tzinfo=tz)
        target = next_run_at(friday)
        assert target.weekday() == 4
        assert (target.hour, target.minute) == (21, 0)
        # After Friday's 20:30 slot → Monday 05:20 (weekend skipped).
        after_evening = datetime(2026, 7, 10, 21, 0, tzinfo=tz)
        target = next_run_at(after_evening)
        assert target.weekday() == 5
        assert (target.hour, target.minute) == (4, 20)


class TestPriceMonitorLifecycle:
    def test_pullback_touch_requires_confirmation(self):
        from app import evaluate_price_monitor

        row = {
            "entry_price": 120,
            "wait_price": 100,
            "latest_price": 99,
            "latest_date": "2026-07-07",
            "validity_date": "2026-08-01",
            "monitor_direction": "pullback",
        }
        result = evaluate_price_monitor(row, today=__import__("datetime").date(2026, 7, 7))
        assert result["monitor_state"] == "triggered"
        assert "确认" in result["monitor_reason"]

    def test_deep_pullback_breach_requires_revalidation(self):
        from app import evaluate_price_monitor

        row = {
            "entry_price": 120,
            "wait_price": 100,
            "latest_price": 90,
            "latest_date": "2026-07-07",
            "validity_date": "2026-08-01",
            "monitor_direction": "pullback",
        }
        result = evaluate_price_monitor(row, today=__import__("datetime").date(2026, 7, 7))
        assert result["monitor_state"] == "revalidate"

    def test_stale_price_requires_revalidation(self):
        from datetime import date
        from app import evaluate_price_monitor

        row = {
            "entry_price": 120,
            "wait_price": 100,
            "latest_price": 110,
            "latest_date": "2026-07-01",
            "validity_date": "2026-08-01",
            "monitor_direction": "pullback",
        }
        result = evaluate_price_monitor(row, today=date(2026, 7, 7))
        assert result["monitor_state"] == "revalidate"


class TestCalendarSync:
    def test_syncs_company_and_three_star_macro_events(self):
        from app import _sync_calendar_events, get_db

        class FakeCalendar:
            calls = []

            def position_symbols(self):
                return ["NVDA.US"]

            def calendar(self, category, start, end, market=None, symbols=None):
                self.calls.append(category)
                if category == "report":
                    assert "NVDA.US" in symbols
                    return {"list": [{"date": "2026-08-01", "infos": [
                        {"id": "report-1", "symbol": "TSLA.US", "counter_name": "Tesla",
                         "content": "Earnings Release", "date": "After Market", "data": {"estimate_eps": "1.20"}, "star": 0},
                        {"id": "report-nvda", "symbol": "NVDA.US", "counter_name": "NVIDIA",
                         "content": "季度业绩", "date": "盘后", "data": {}, "star": 0},
                    ]}]}
                if category == "macrodata":
                    return {"list": [{"date": "2026-08-02", "infos": [
                        {"id": "macro-3", "symbol": "", "counter_name": "", "content": "美国 CPI",
                         "date": "20:30", "data": {"预测": "2.8"}, "star": 3},
                        {"id": "macro-2", "symbol": "", "counter_name": "", "content": "低重要性数据",
                         "date": "21:00", "data": {}, "star": 2},
                    ]}]}
                return {"list": []}

        with app.app_context():
            result = _sync_calendar_events(FakeCalendar())
            assert result["added"] == 3
            assert result["skipped"] == 1
            assert "closed" not in FakeCalendar.calls
            db = get_db()
            company = db.execute("SELECT event_scope FROM catalysts WHERE external_id='longbridge:report:report-1'").fetchone()
            macro = db.execute("SELECT event_scope FROM catalysts WHERE external_id='longbridge:macrodata:macro-3'").fetchone()
            held = db.execute("SELECT event_scope FROM catalysts WHERE external_id='longbridge:report:report-nvda'").fetchone()
            assert company["event_scope"] == "company"
            assert macro["event_scope"] == "macro"
            assert held["event_scope"] == "company"
            translated = db.execute("SELECT description FROM catalysts WHERE external_id='longbridge:report:report-1'").fetchone()["description"]
            assert "盘后" in translated
            assert "业绩披露" in translated
            assert "预期 EPS 1.20" in translated

            second = _sync_calendar_events(FakeCalendar())
            assert second["added"] == 0
            assert second["updated"] == 3
