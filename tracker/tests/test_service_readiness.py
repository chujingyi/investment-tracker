"""Readiness checks must be explicit, read-only and redact service errors."""
import json
import os
import sqlite3
import pytest
from app import get_db
from agent import config, runtime, tools, tasks, workflow
from agent.preflight import check, schema_readiness
from service_support import configure_tls_trust, service_failure


def test_custom_ca_is_preserved(tmp_path, monkeypatch):
    path = tmp_path / 'custom-ca.pem'
    path.write_text('custom roots')
    monkeypatch.setenv('SSL_CERT_FILE', str(path))
    assert configure_tls_trust() == str(path)


def test_missing_ca_fails_without_disabling_verification(monkeypatch):
    monkeypatch.setenv('SSL_CERT_FILE', '/missing/roots.pem')
    with pytest.raises(ValueError):
        configure_tls_trust()


def test_default_ca_exists(monkeypatch):
    from pathlib import Path
    monkeypatch.delenv('SSL_CERT_FILE', raising=False)
    assert Path(configure_tls_trust()).is_file()


def test_expired_token_diagnostic_contains_no_raw_error():
    result = service_failure(Exception('code=401003 token expired SECRET_VALUE'))
    assert result['code'] == 'token_expired'
    assert 'SECRET_VALUE' not in json.dumps(result)


def test_expired_quotes_are_not_called_unconfigured(monkeypatch):
    class Expired:
        def __init__(self):
            raise RuntimeError('code=401003 token expired')
    monkeypatch.setattr('app.LongbridgeClient', Expired)
    conn = get_db()
    result = tools.run_read(conn, 'get_quotes', {'symbols':['AAPL']})
    assert result['realtime'] is False
    assert result['diagnostic']['code'] == 'token_expired'
    assert '已过期' in result['summary']
    conn.close()


def test_missing_calendar_is_not_empty_calendar():
    from longbridge_api import LongbridgeClient, LongbridgeCapabilityUnavailable
    client = object.__new__(LongbridgeClient)
    client._calendar = None
    with pytest.raises(LongbridgeCapabilityUnavailable):
        client.calendar('report', '2026-01-01', '2026-01-02')


def test_schema_check_does_not_create_tables():
    conn = sqlite3.connect(':memory:')
    result = schema_readiness(conn)
    assert result['ready'] is False
    assert len(result['missing_tables']) == 12
    assert conn.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0] == 0
    conn.close()


def test_schema_check_matches_initialized_database():
    conn = get_db()
    assert schema_readiness(conn)['ready'] is True
    conn.close()


def test_configuration_check_never_opens_network(monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'private-secret')
    result = check(network=False)
    assert result['model']['called'] is False
    assert result['database']['checked'] is False
    assert 'private-secret' not in json.dumps(result)


def test_model_payload_has_output_limit(monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'test')
    monkeypatch.setenv('LLM_MAX_OUTPUT_TOKENS', '128')
    def post(url, headers, json, timeout):
        assert json['max_tokens'] == 128
        class Response:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {'choices':[{'message':{'content':'ok'}}]}
        return Response()
    monkeypatch.setattr(runtime.requests, 'post', post)
    assert runtime._chat_completion([{'role':'user','content':'synthetic'}])[0] == 'ok'


def test_native_libsql_adapter_runs_task_and_approval(monkeypatch):
    libsql = pytest.importorskip('libsql')
    from app import _LibsqlConnection
    conn = _LibsqlConnection(libsql.connect(os.environ['TEST_DATABASE_PATH']))
    monkeypatch.setenv('LLM_API_KEY','test')
    calls = 0
    def complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return '', [{'id':'n','name':'send_notification','arguments':json.dumps({'title':'native-test','message':'synthetic'})}], {'prompt_tokens':10,'completion_tokens':5}
        return 'done', [], {'prompt_tokens':10,'completion_tokens':5}
    monkeypatch.setattr(runtime,'_chat_completion',complete)
    task_id = tasks.create_task(conn,'review','review')
    workflow.run_task(conn,task_id)
    pending = conn.execute('SELECT id FROM pending_actions WHERE task_id=?',(task_id,)).fetchone()
    row = runtime.resolve_pending_action(conn,pending['id'],True)
    task = tasks.on_pending_resolved(conn,row['chat_id'],True,task_id=task_id)
    assert task['status'] == 'succeeded'
    assert conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 1
    conn.close()
