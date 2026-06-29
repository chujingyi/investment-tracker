"""Basic tests for the stock tracker Flask application.

Run: API_KEY=test-key SECRET_KEY=test-key python -m pytest tests/ -v
"""

import json
import os
import pytest

# Ensure env vars are set before importing app
os.environ.setdefault("SECRET_KEY", "test-key-for-unit-tests")
os.environ.setdefault("API_KEY", "test-api-key-12345")

from app import app


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
