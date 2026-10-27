"""Regression coverage for task execution, approval atomicity and honest evaluation."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import app, get_db
from agent import config, events, runtime, tasks, tools, workflow
from agent import eval as historical
from agent.quality_eval import evaluate_outputs, CASES


def chat(conn):
    cur = conn.execute("INSERT INTO agent_chats(title,created_at) VALUES ('test',?)", (runtime._now(),))
    conn.commit()
    return cur.lastrowid


def fake_model(monkeypatch, calls=None):
    monkeypatch.setenv('LLM_API_KEY', 'test')
    index = 0
    def complete(messages, tool_specs=None, model=None):
        nonlocal index
        index += 1
        return ('', calls, {'prompt_tokens':10,'completion_tokens':5}) if index == 1 and calls else ('completed', [], {'prompt_tokens':10,'completion_tokens':5})
    monkeypatch.setattr(runtime, '_chat_completion', complete)


def notification_call(title='n'):
    return {'id':title, 'name':'send_notification', 'arguments':json.dumps({'title':title,'message':'body'})}


def run_proposal(conn, monkeypatch, count=1):
    fake_model(monkeypatch, [notification_call(str(i)) for i in range(count)])
    task_id = tasks.create_task(conn, 'review', 'review')
    workflow.run_task(conn, task_id)
    rows = conn.execute("SELECT * FROM pending_actions WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
    return task_id, rows


def test_approval_requires_all_actions(monkeypatch):
    conn = get_db()
    task_id, rows = run_proposal(conn, monkeypatch, 2)
    first = runtime.resolve_pending_action(conn, rows[0]['id'], True)
    result = tasks.on_pending_resolved(conn, first['chat_id'], True, task_id=task_id)
    assert result['status'] == 'waiting_approval'
    second = runtime.resolve_pending_action(conn, rows[1]['id'], True)
    result = tasks.on_pending_resolved(conn, second['chat_id'], True, task_id=task_id)
    assert result['status'] == 'succeeded'
    assert conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 2
    conn.close()


def test_write_failure_rolls_back_and_fails_task(monkeypatch):
    conn = get_db()
    task_id, rows = run_proposal(conn, monkeypatch)
    def broken(c, name, args):
        c.execute("INSERT INTO notifications(title,message,created_at) VALUES ('partial','x','2026-01-01')")
        raise RuntimeError('write interrupted')
    monkeypatch.setattr(tools, 'run_write', broken)
    row = runtime.resolve_pending_action(conn, rows[0]['id'], True)
    assert row['status'] == 'failed'
    assert conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0
    result = tasks.on_pending_resolved(conn, row['chat_id'], True, task_id=task_id)
    assert result['status'] == 'failed'
    conn.close()


def test_concurrent_approval_only_writes_once(monkeypatch):
    conn = get_db()
    _, rows = run_proposal(conn, monkeypatch)
    key = rows[0]['id']
    conn.close()
    def approve():
        c = get_db()
        try:
            return runtime.resolve_pending_action(c, key, True)
        finally:
            c.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: approve(), range(2)))
    assert sum(x['changed'] for x in results) == 1
    conn = get_db()
    assert conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 1
    conn.close()


def test_partial_approval_rejection_does_not_repeat_writes(monkeypatch):
    conn = get_db()
    task_id, rows = run_proposal(conn, monkeypatch, 2)
    runtime.resolve_pending_action(conn, rows[0]['id'], True)
    row = runtime.resolve_pending_action(conn, rows[1]['id'], False)
    task = tasks.on_pending_resolved(conn, row['chat_id'], False, task_id=task_id)
    assert task['status'] == 'cancelled'
    assert conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 1
    conn.close()


def test_repeat_tool_proposal_deduplicated_within_run(monkeypatch):
    conn = get_db()
    fake_model(monkeypatch, [notification_call(), notification_call()])
    trace = runtime.run_chat(conn, chat(conn), 'test')
    assert len(trace['pending']) == 2  # timeline shows both requests
    assert conn.execute('SELECT COUNT(*) FROM pending_actions').fetchone()[0] == 1
    conn.close()


def test_budget_checked_between_rounds(monkeypatch):
    conn = get_db()
    fake_model(monkeypatch, [{'id':'q','name':'query_portfolio','arguments':'{}'}])
    monkeypatch.setenv('AGENT_DAILY_TOKEN_BUDGET','10')
    with pytest.raises(config.BudgetExceeded):
        runtime.run_chat(conn, chat(conn), 'test')
    assert conn.execute('SELECT status FROM agent_runs').fetchone()[0] == 'error'
    conn.close()


def test_iteration_cap_counts_final_call_and_marks_incomplete(monkeypatch):
    conn = get_db()
    fake_model(monkeypatch, [{'id':'q','name':'query_portfolio','arguments':'{}'}])
    monkeypatch.setenv('AGENT_MAX_ITERATIONS','1')
    task_id = tasks.create_task(conn,'review','review')
    trace = workflow.run_task(conn,task_id)
    assert trace['usage'] == {'prompt_tokens':20,'completion_tokens':10}
    assert tasks.get_task(conn,task_id)['status'] == 'failed'
    conn.close()


def test_iteration_cap_leaked_tool_markup_replaced(monkeypatch):
    """The cap fallback must never persist leaked tool-call markup as the answer."""
    monkeypatch.setenv('LLM_API_KEY', 'test')
    monkeypatch.setenv('AGENT_MAX_ITERATIONS', '1')
    dsml = (
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="query_recommendations">\n'
        '<｜｜DSML｜｜ parameter name="limit" string="false">20</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    calls = {'n': 0}
    seen = []
    def complete(messages, tool_specs=None, model=None):
        calls['n'] += 1
        seen.append(messages)
        if calls['n'] == 1:
            return ('', [{'id':'q','name':'query_portfolio','arguments':'{}'}], {'prompt_tokens':10,'completion_tokens':5})
        return (dsml, [], {'prompt_tokens':8,'completion_tokens':40})
    monkeypatch.setattr(runtime, '_chat_completion', complete)
    conn = get_db()
    trace = runtime.run_chat(conn, chat(conn), 'test')
    assert trace['status'] == 'limited'
    assert '工具调用上限' in trace['answer']
    assert '｜' not in trace['answer']
    # the fallback call carried an explicit no-markup instruction
    assert seen[-1][-1]['role'] == 'user'
    assert '不要再输出任何工具调用' in seen[-1][-1]['content']
    saved = conn.execute("SELECT content FROM agent_messages WHERE role='assistant' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert '｜' not in saved
    conn.close()


def test_interrupted_task_fails_without_replay(monkeypatch):
    conn = get_db()
    task_id = tasks.create_task(conn,'review','review')
    conn.execute("UPDATE agent_tasks SET status='running',lease_until='2000-01-01' WHERE id=?",(task_id,))
    conn.commit()
    assert tasks.recover_expired_tasks(conn) == 1
    assert tasks.get_task(conn,task_id)['status'] == 'failed'
    assert workflow.run_pending_tasks(conn) == []
    conn.close()


def test_task_claim_cannot_be_reentered(monkeypatch):
    conn = get_db()
    fake_model(monkeypatch)
    task_id=tasks.create_task(conn,'review','review')
    conn.execute("UPDATE agent_tasks SET status='running' WHERE id=?",(task_id,))
    conn.commit()
    with pytest.raises(ValueError):
        workflow.run_task(conn,task_id)
    conn.close()


def test_retry_events_consumed_once(monkeypatch):
    conn=get_db()
    event_id=events.insert_event(conn,'manual_research',{'ticker':'TEST'})
    conn.execute("UPDATE agent_events SET status='retry' WHERE id=?",(event_id,))
    conn.commit()
    assert events.process_events(conn)[0][1]
    assert events.process_events(conn) == []
    assert conn.execute('SELECT COUNT(*) FROM agent_tasks').fetchone()[0] == 1
    conn.close()


def test_upcoming_calendar_events_are_scanned():
    conn=get_db()
    conn.execute("INSERT INTO catalysts(title,event_type,event_date,status,created_at,updated_at) VALUES ('earnings','earnings',?,'upcoming',?,?)",
                 (str(date.today()),runtime._now(),runtime._now()))
    conn.commit()
    assert events.scan_calendar_soon(conn,date.today()) == 1
    assert events.scan_calendar_soon(conn,date.today()) == 0
    conn.close()


def test_monitor_state_does_not_repeat_after_cooldown(monkeypatch):
    conn=get_db()
    conn.execute("INSERT INTO recommendations(created_at,ticker,framework,entry_price,monitoring,status) VALUES (?,'TEST','ad_hoc',100,1,'active')",(str(date.today()),))
    conn.commit()
    monkeypatch.setattr('app.enrich_rows',lambda rows:[dict(row) for row in rows])
    monkeypatch.setattr('app.evaluate_price_monitor',lambda row,today=None:{**row,'monitor_state':'triggered'})
    assert events.scan_price_approaching(conn) == 1
    conn.execute("UPDATE agent_events SET created_at='2000-01-01'")
    conn.commit()
    assert events.scan_price_approaching(conn) == 0
    conn.close()


def test_daily_morning_is_reserved_once(monkeypatch):
    from agent.morning import enqueue_daily_report
    monkeypatch.setenv('LLM_API_KEY','test')
    conn=get_db()
    a=enqueue_daily_report(conn)
    assert enqueue_daily_report(conn) == a
    assert conn.execute('SELECT COUNT(*) FROM agent_tasks').fetchone()[0] == 1
    conn.close()


def test_morning_budget_function_does_not_raise_attributeerror(monkeypatch):
    from agent.morning import maybe_generate_morning_report
    monkeypatch.setenv('LLM_API_KEY','test')
    monkeypatch.setattr(runtime,'check_budget',lambda c: (_ for _ in ()).throw(config.BudgetExceeded('budget')))
    conn=get_db()
    assert 'budget' in maybe_generate_morning_report(conn)
    conn.close()


def test_trigger_requires_api_key_or_csrf():
    with app.test_client() as client:
        assert client.post('/api/agent/events/scan').status_code == 400
        assert client.post('/api/agent/events/scan',headers={'X-API-Key':'wrong'}).status_code == 401
        assert client.post('/api/agent/events/scan',headers={'X-API-Key':os.environ['API_KEY']}).status_code == 200


def test_web_queues_without_calling_model(monkeypatch):
    monkeypatch.setenv('LLM_API_KEY','test')
    monkeypatch.setattr(runtime,'_chat_completion',lambda *a,**k: pytest.fail('web must not call model'))
    app.config['WTF_CSRF_ENABLED']=False
    with app.test_client() as client:
        response=client.post('/api/agent/chat',json={'message':'review'})
        assert response.status_code == 202
        task_id=response.json['task_id']
        assert client.get(f'/api/agent/tasks/{task_id}').json['task']['status'] == 'queued'
        assert client.post('/api/agent/chat',json={'message':'again','chat_id':response.json['chat_id']}).status_code == 409


def test_mature_windows_and_directional_trigger():
    start=date(2026,1,1)
    prices=[(start+timedelta(days=7),105)]
    assert historical._window_metrics(prices,100,str(start),30) is None
    assert historical._window_metrics(prices,100,str(start),7)['return_pct'] == 5
    record={'entry_price':100,'entry_rec':'watch','created_at':str(start),'wait_price':110,
            'monitor_direction':'breakout','_snapshots':[(str(start+timedelta(days=7)),105)]}
    conn=get_db()
    assert historical.evaluate(conn,[record])['trigger_hit']['n'] == 0
    conn.close()


def test_frozen_historical_dataset():
    conn=get_db()
    a=historical.run_eval(conn)
    conn.execute("INSERT INTO recommendations(created_at,ticker,framework,entry_price,entry_rec) VALUES ('2026-01-01','NEW','ad_hoc',100,'watch')")
    key=conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.execute("INSERT INTO price_snapshots(recommendation_id,snapshot_date,price) VALUES (?,'2026-03-01',120)",(key,))
    conn.commit()
    b=historical.run_eval(conn)
    assert a['current']['dataset_hash'] == b['current']['dataset_hash']
    assert b['current']['sample_count'] == 0
    assert historical.run_eval(conn,fresh=True)['current']['sample_count'] == 1
    conn.close()


def test_quality_contract_rejects_missing_and_fabricated_evidence():
    assert evaluate_outputs([])['passed'] == 0
    cases=json.loads(CASES.read_text())
    answers=[{'case_id':x['id'],'verdict':x['expected_verdict'],'evidence_ids':x['required_evidence'],'facts':x['expected_facts']} for x in cases]
    assert evaluate_outputs(answers)['passed'] == len(cases)
    answers[0]['evidence_ids']=['fabricated']
    assert evaluate_outputs(answers)['passed'] == len(cases)-1


def test_dst_schedule_uses_us_trading_weekday():
    from ai_monitor_worker import next_run_at
    ny=ZoneInfo('America/New_York')
    summer=next_run_at(datetime(2026,7,10,10,tzinfo=ny))
    winter=next_run_at(datetime(2026,12,11,10,tzinfo=ny))
    assert (summer.hour,summer.minute,summer.weekday()) == (4,20,5)
    assert (winter.hour,winter.minute,winter.weekday()) == (5,20,5)


def test_cancelled_task_cannot_publish_or_approve(monkeypatch):
    conn=get_db()
    task_id, rows=run_proposal(conn,monkeypatch)
    tasks.terminate_task(conn,task_id,'cancelled','test')
    row=runtime.resolve_pending_action(conn,rows[0]['id'],True)
    assert row['status'] == 'cancelled'
    assert not row['changed']
    assert conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0
    assert tasks.get_task(conn,task_id)['status'] == 'cancelled'
    conn.close()


def test_queued_task_without_budget_does_not_consume_attempt(monkeypatch):
    conn=get_db()
    task_id=tasks.create_task(conn,'review','review')
    monkeypatch.setenv('LLM_API_KEY','test')
    monkeypatch.setattr(runtime,'check_budget',lambda c: (_ for _ in ()).throw(config.BudgetExceeded('budget')))
    workflow.run_pending_tasks(conn)
    assert tasks.get_task(conn,task_id)['attempt'] == 0
    assert tasks.get_task(conn,task_id)['status'] == 'queued'
    conn.close()


def test_monitor_only_sends_valid_structured_decision(monkeypatch):
    conn=get_db()
    monkeypatch.setenv('LLM_API_KEY','test')
    monkeypatch.setattr(runtime,'_chat_completion',lambda *a,**k: (json.dumps({'notify':True,'reason':'event evidence'}),[],{}))
    task_id=tasks.create_task(conn,'monitor','monitor',{'event_id':1})
    workflow.run_task(conn,task_id)
    assert tasks.get_task(conn,task_id)['status'] == 'succeeded'
    assert conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 1
    monkeypatch.setattr(runtime,'_chat_completion',lambda *a,**k: ('not a decision',[],{}))
    task_id=tasks.create_task(conn,'monitor','monitor',{'event_id':2})
    workflow.run_task(conn,task_id)
    assert tasks.get_task(conn,task_id)['status'] == 'failed'
    assert conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 1
    conn.close()


def test_expired_approval_requires_new_evidence(monkeypatch):
    conn=get_db()
    _,rows=run_proposal(conn,monkeypatch)
    conn.execute("UPDATE pending_actions SET created_at='2000-01-01' WHERE id=?",(rows[0]['id'],))
    conn.commit()
    assert runtime.resolve_pending_action(conn,rows[0]['id'],True)['status'] == 'failed'
    assert conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0
    conn.close()


def test_additive_migration_preserves_legacy_messages():
    import sqlite3
    from agent.schema import SCHEMA_SQL, ensure_agent_tables
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO agent_messages(chat_id,role,content,created_at) VALUES (1,'user','keep','2026-01-01')")
    conn.commit()
    ensure_agent_tables(conn)
    ensure_agent_tables(conn)
    assert conn.execute('SELECT content FROM agent_messages').fetchone()[0] == 'keep'
    assert 'task_id' in {row[1] for row in conn.execute('PRAGMA table_info(pending_actions)')}
    conn.close()


def test_budget_failure_after_proposal_never_replays(monkeypatch):
    conn=get_db()
    fake_model(monkeypatch,[notification_call()])
    monkeypatch.setenv('AGENT_DAILY_TOKEN_BUDGET','10')
    task_id=tasks.create_task(conn,'review','review')
    with pytest.raises(config.BudgetExceeded):
        workflow.run_task(conn,task_id)
    assert tasks.get_task(conn,task_id)['status'] == 'failed'
    assert conn.execute('SELECT status FROM pending_actions').fetchone()[0] == 'cancelled'
    assert workflow.run_pending_tasks(conn) == []
    conn.close()


def test_renew_expired_monitor_and_validate_prices():
    conn=get_db()
    conn.execute("INSERT INTO recommendations(created_at,ticker,framework,entry_price,status,validity_date) VALUES ('2026-01-01','TEST','ad_hoc',100,'active','2026-01-01')")
    rec_id=conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.commit()
    key,_=runtime.create_pending_action(conn,None,None,'update_monitor',{'rec_id':rec_id,'action':'resume'})
    assert runtime.resolve_pending_action(conn,key,True)['status'] == 'approved'
    assert conn.execute('SELECT validity_date FROM recommendations').fetchone()[0] > str(date.today())
    assert tools.validate_write('propose_recommendation',{'ticker':'TEST','entry_rec':'watch','wait_price':float('nan')})
    conn.close()
