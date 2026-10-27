"""Each test has its own SQLite database and cannot call external providers."""
import os
import pytest

os.environ["APP_USERNAME"] = ""
os.environ["APP_PASSWORD"] = ""
os.environ.pop("VERCEL", None)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    from app import app, init_db
    monkeypatch.setenv("TEST_DATABASE_PATH", str(tmp_path / "test.db"))
    for key in ("LLM_API_KEY", "LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    def no_network(*args, **kwargs):
        raise AssertionError("External calls are forbidden in tests")
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request", no_network)
    app.config["WTF_CSRF_ENABLED"] = True
    init_db()
    yield
    app.config["WTF_CSRF_ENABLED"] = True
