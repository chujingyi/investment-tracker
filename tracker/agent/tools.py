"""Standardized tool layer: 10 tools (7 read + 3 write) exposed to the agent.

Governance model (P0):
- read tools execute immediately;
- write tools are intercepted by the runtime: arguments are validated first
  (decision-gate schema), then a pending_actions row is created and the real
  write happens only after the user approves it in the UI;
- tool availability is scoped per scenario: the morning report job mounts
  read tools only.
"""

import json
import math
from datetime import date, datetime, timedelta

WRITE_TOOLS = {"propose_recommendation", "update_monitor", "send_notification"}

# Allowed entry tiers for new recommendations. "avoid" is deliberately absent:
# the decision-gate rules say "不碰" stocks are never recorded in the system.
ENTRY_TIERS = {"actionable", "watch"}
FRAMEWORKS = {"momentum", "fundamental", "supply_chain", "radar", "ad_hoc"}


class ToolError(ValueError):
    """Validation failure that is returned to the LLM so it can self-correct."""


# ── helpers ──────────────────────────────────────────────────────────────────


def _lb_symbol(ticker, market):
    text = (ticker or "").strip().upper()
    if "." in text:
        return text
    market = (market or "US").upper()
    if market == "HK":
        return f"{text}.HK"
    if market in ("CN", "SH", "SZ"):
        return f"{text}.SH" if (text.startswith("6") or text.startswith("688")) else f"{text}.SZ"
    return f"{text}.US"


def _num(value):
    try:
        result = float(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def _f(value, digits=2):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _compact_rec(row):
    return {
        "id": row["id"],
        "ticker": row["ticker"],
        "tier": row.get("entry_rec") or "watch",
        "framework": row.get("framework"),
        "entry_price": row.get("entry_price"),
        "latest_price": row.get("latest_price"),
        "pct_from_entry": row.get("pct_change"),
        "conviction": row.get("conviction"),
        "wait_price": row.get("wait_price"),
        "wait_condition": row.get("wait_condition"),
        "validity_date": row.get("validity_date"),
        "monitoring": bool(row.get("monitoring")),
        "monitor_state": row.get("monitor_state"),
        "status": row.get("status"),
        "catalyst": row.get("catalyst") or "",
        "notes": (row.get("notes") or "")[:200],
    }


# ── read tools ───────────────────────────────────────────────────────────────


def _tool_query_portfolio(conn, args):
    from app import _load_broker_data, _build_stock_rows, enrich_rows

    rows = conn.execute(
        "SELECT * FROM recommendations WHERE status='active' ORDER BY created_at DESC"
    ).fetchall()
    enriched = enrich_rows(rows)
    positions, executions = _load_broker_data(conn)
    stock_rows = _build_stock_rows(enriched, positions, executions)

    holdings = []
    total_value = 0.0
    total_pnl = 0.0
    for row in stock_rows:
        if row["lifecycle"] != "holding":
            continue
        value = (row.get("current_price") or 0) * (row.get("quantity") or 0)
        total_value += value
        if row.get("display_pnl") is not None:
            total_pnl += row["display_pnl"]
        holdings.append(
            {
                "ticker": row["ticker_display"],
                "quantity": row.get("quantity"),
                "cost_price": _f(row.get("cost_price")),
                "current_price": _f(row.get("current_price")),
                "pnl": _f(row.get("display_pnl")),
                "pnl_pct": _f(row.get("display_pnl_pct")),
                "conviction": row.get("conviction"),
            }
        )
    watching = sum(1 for r in enriched if (r.get("entry_rec") or "watch") in ("watch", "wait"))
    actionable = sum(1 for r in enriched if r.get("entry_rec") in ("actionable", "immediate"))
    synced = conn.execute("SELECT MAX(synced_at) AS as_of FROM broker_positions").fetchone()
    result = {
        "as_of": synced["as_of"] if synced else None,
        "source": "local_broker_snapshot",
        "warning": "持仓来自本地同步快照，不代表实时账户；不含账户现金",
        "holdings": holdings,
        "total_market_value": round(total_value, 2),
        "total_pnl": round(total_pnl, 2),
        "active_recommendations": {"watch_or_wait": watching, "actionable": actionable, "total": len(enriched)},
        "summary": f"{len(holdings)} 只持仓，市值 ${total_value:,.0f}，浮动盈亏 {total_pnl:+,.0f}；观察 {watching} 条 / 可入 {actionable} 条",
    }
    return result


def _tool_query_recommendations(conn, args):
    from app import enrich_rows

    clauses = ["status=?"]
    params = [args.get("status") or "active"]
    if args.get("entry_rec"):
        clauses.append("entry_rec=?")
        params.append(args["entry_rec"])
    if args.get("framework"):
        clauses.append("framework=?")
        params.append(args["framework"])
    if args.get("ticker"):
        clauses.append("ticker=?")
        params.append(str(args["ticker"]).upper())
    limit = max(1, min(int(args.get("limit") or 20), 50))
    rows = conn.execute(
        f"SELECT * FROM recommendations WHERE {' AND '.join(clauses)} "
        f"ORDER BY created_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    enriched = enrich_rows(rows)
    recs = [_compact_rec(r) for r in enriched]
    return {
        "recommendations": recs,
        "count": len(recs),
        "summary": f"返回 {len(recs)} 条推荐记录",
    }


def _tool_query_catalysts(conn, args):
    today = date.today()
    days = max(1, min(int(args.get("days") or 30), 180))
    end = str(today + timedelta(days=days))
    clauses = ["event_date BETWEEN ? AND ?"]
    params = [str(today), end]
    if args.get("ticker"):
        clauses.insert(0, "ct.ticker=?")
        params.insert(0, str(args["ticker"]).upper())
    limit = max(1, min(int(args.get("limit") or 20), 50))
    rows = conn.execute(
        "SELECT DISTINCT c.* FROM catalysts c "
        "LEFT JOIN catalyst_tickers ct ON ct.catalyst_id=c.id "
        f"WHERE {' AND '.join(clauses)} ORDER BY c.event_date ASC LIMIT ?",
        (*params, limit),
    ).fetchall()
    events = []
    for row in rows:
        tickers = [
            t["ticker"]
            for t in conn.execute(
                "SELECT ticker FROM catalyst_tickers WHERE catalyst_id=?", (row["id"],)
            ).fetchall()
        ]
        events.append(
            {
                "date": row["event_date"],
                "title": row["title"],
                "type": row["event_type"],
                "scope": row["event_scope"] or "",
                "tickers": tickers,
                "status": row["status"],
                "description": (row["description"] or "")[:200],
            }
        )
    return {
        "events": events,
        "count": len(events),
        "summary": f"未来 {days} 天 {len(events)} 个催化剂/事件",
    }


def _tool_get_quotes(conn, args):
    symbols = args.get("symbols") or []
    if isinstance(symbols, str):
        symbols = [symbols]
    symbols = [str(s).strip().upper() for s in symbols if str(s).strip()][:10]
    if not symbols:
        raise ToolError("symbols 不能为空（最多 10 个）")

    quotes = []
    realtime = True
    failure = None
    try:
        from app import LongbridgeClient

        client = LongbridgeClient()
        lookup = [_lb_symbol(s, "US") for s in symbols]
        for item in client.quotes(lookup):
            quotes.append(
                {
                    "symbol": item["symbol"].split(".")[0],
                    "price": _f(item["last"], 4),
                    "change_pct": _f(item["change_percentage"]),
                    "prev_close": _f(item["prev_close"]),
                    "source": "longbridge",
                    "as_of": datetime.now().isoformat(timespec="seconds"),
                }
            )
        missing = [s for s in symbols if s not in {q["symbol"] for q in quotes}]
    except Exception as exc:
        from service_support import service_failure
        failure = service_failure(exc)
        realtime = False
        missing = symbols

    # A mixed response must not claim all prices are realtime.
    if missing:
        realtime = False
    # Fallback: latest stored snapshot price (marked as non-realtime).
    for symbol in missing:
        row = conn.execute(
            "SELECT ps.price, ps.snapshot_date FROM price_snapshots ps "
            "JOIN recommendations r ON r.id=ps.recommendation_id "
            "WHERE UPPER(r.ticker)=? "
            "AND ps.snapshot_date=(SELECT MAX(s2.snapshot_date) FROM price_snapshots s2 "
            "WHERE s2.recommendation_id=ps.recommendation_id) LIMIT 1",
            (symbol,),
        ).fetchone()
        quotes.append(
            {
                "symbol": symbol,
                "price": _f(row["price"], 4) if row else None,
                "change_pct": None,
                "as_of": row["snapshot_date"] if row else None,
                "source": "db_snapshot",
            }
        )
    found = sum(1 for q in quotes if q.get("price"))
    source_note = "实时行情" if realtime else "含数据库快照，非实时；请检查数据日期"
    if failure:
        source_note += "；" + failure["message"]
    return {
        "quotes": quotes,
        "realtime": realtime,
        "diagnostic": failure,
        "summary": f"{found}/{len(symbols)} 个报价获取成功（{source_note}）",
    }


def _tool_get_sector_signals(conn, args):
    result = {"summary": ""}
    row = conn.execute("SELECT * FROM ai_infra_monitor WHERE id=1").fetchone()
    if row:
        result["ai_infra_signal"] = {
            "signal": row["signal"],
            "score": row["score"],
            "summary": row["summary"],
            "checked_at": row["checked_at"],
        }
    framework_rows = conn.execute(
        "SELECT framework, COUNT(*) AS cnt FROM recommendations "
        "WHERE status='active' GROUP BY framework ORDER BY cnt DESC"
    ).fetchall()
    result["active_by_framework"] = {r["framework"]: r["cnt"] for r in framework_rows}
    parts = []
    if row:
        parts.append(f"AI 基建信号 {row['signal']}（{row['score']}/5，{row['checked_at'][:10]}）")
    parts.append("、".join(f"{r['framework']}×{r['cnt']}" for r in framework_rows) or "无活跃推荐")
    result["summary"] = "；".join(parts)
    return result


def _tool_search_knowledge(conn, args):
    from agent import knowledge

    query = (args.get("query") or "").strip()
    if not query:
        raise ToolError("query 不能为空")
    k = int(args.get("k") or 5)
    results = knowledge.retrieve(conn, query, k=k)
    for item in results:
        item["source"] = item["source"].replace("framework:", "").replace("kb:", "知识库#")
    return {
        "results": results,
        "count": len(results),
        "summary": f"检索到 {len(results)} 段相关内容"
        + (f"：{results[0]['title']}《{results[0]['section'] or results[0]['title']}》" if results else ""),
    }


def _tool_get_market_calendar(conn, args):
    today = date.today()
    days = max(1, min(int(args.get("days") or 14), 60))
    end = str(today + timedelta(days=days))
    rows = conn.execute(
        "SELECT * FROM catalysts WHERE event_date BETWEEN ? AND ? ORDER BY event_date ASC",
        (str(today), end),
    ).fetchall()
    events = []
    for row in rows:
        tickers = [
            t["ticker"]
            for t in conn.execute(
                "SELECT ticker FROM catalyst_tickers WHERE catalyst_id=?", (row["id"],)
            ).fetchall()
        ]
        events.append(
            {
                "date": row["event_date"],
                "title": row["title"],
                "type": row["event_type"],
                "scope": row["event_scope"] or "",
                "tickers": tickers,
                "status": row["status"],
            }
        )
    macro_rows = conn.execute(
        "SELECT title, status, related_tickers FROM macro_events WHERE status='active' "
        "ORDER BY updated_at DESC LIMIT 5"
    ).fetchall()
    return {
        "days": days,
        "events": events,
        "active_macro_theses": [dict(r) for r in macro_rows],
        "count": len(events),
        "summary": f"未来 {days} 天 {len(events)} 个日历事件，活跃宏观命题 {len(macro_rows)} 个",
    }


# ── write tools (executed only after user approval) ─────────────────────────


def _validate_propose_recommendation(args, conn=None):
    from app import _validate_ticker

    errors = []
    ticker = str(args.get("ticker") or "").strip().upper()
    tier = str(args.get("entry_rec") or "").strip().lower()
    if not ticker or not _validate_ticker(ticker):
        errors.append("ticker 必填且格式合法（大写字母/数字，可含 .）")
    if conn is not None and ticker:
        dup = conn.execute(
            "SELECT id, entry_rec, ticker FROM recommendations "
            "WHERE status='active' AND UPPER(ticker)=? LIMIT 1", (ticker,)
        ).fetchone()
        if dup:
            errors.append(
                f"该 ticker 已有活跃记录 #{dup['id']}（档位 {dup['entry_rec']}）；"
                "如需调整请用 update_monitor，避免重复录入"
            )
    if tier not in ENTRY_TIERS:
        errors.append("entry_rec 必须是 actionable（可入）或 watch（观察）；'不碰'的股票不录入系统")
    entry_price = _num(args.get("entry_price"))
    stop = _num(args.get("stop_loss_price"))
    target1 = _num(args.get("target1_price"))
    wait_price = _num(args.get("wait_price"))
    wait_condition = (args.get("wait_condition") or "").strip()
    validity_date = (args.get("validity_date") or "").strip()
    conviction = args.get("conviction", 3)
    try:
        conviction = int(conviction)
        assert 1 <= conviction <= 5
    except Exception:
        errors.append("conviction 必须是 1-5 的整数")

    for key in ("entry_price", "stop_loss_price", "target1_price", "wait_price"):
        if args.get(key) is not None:
            value = _num(args[key])
            if value is None or not math.isfinite(value) or value <= 0:
                errors.append(f"{key} 必须是大于0的有限数值")
    if validity_date:
        try:
            if date.fromisoformat(validity_date) < date.today():
                errors.append("validity_date 不能早于今天")
        except ValueError:
            errors.append("validity_date 必须为 YYYY-MM-DD")
    if tier == "actionable":
        quantity = _num(args.get("quantity"))
        if quantity is None or not math.isfinite(quantity) or quantity <= 0:
            errors.append("可入档 quantity 必须为正数")
        if args.get("order_type") not in {"market", "limit"}:
            errors.append("可入档 order_type 必须为 market 或 limit")
        if args.get("account") not in {"paper", "manual_real"}:
            errors.append("可入档 account 必须为 paper 或 manual_real；本工具不下单")
        if not entry_price:
            errors.append("可入档必须有参考入场价 entry_price")
        if not (stop or target1):
            errors.append("可入档必须有风控锚点：stop_loss_price 或 target1_price 至少一个")
        if not ((wait_price or entry_price) and (stop or target1)):
            errors.append("可入档操作指令不完整：需等待价或入场价 + 风控锚点")
    if tier == "watch":
        if not (wait_price or wait_condition):
            errors.append("观察档必须写清触发条件：wait_price（价位）或 wait_condition（事件/条件描述）")
        if not validity_date:
            args["validity_date"] = str(date.today() + timedelta(days=30))
    if tier == "actionable" and not (wait_price or args.get("notes")):
        errors.append("可入档请在 notes 中写明操作指令（怎么买/买多少/哪个账户）或提供 wait_price")
    return errors


def _run_propose_recommendation(conn, args):
    from app import FRAMEWORK_DEFAULTS

    ticker = str(args["ticker"]).strip().upper()
    tier = str(args["entry_rec"]).strip().lower()
    entry_price = _num(args.get("entry_price")) or 0.0
    framework = str(args.get("framework") or "ad_hoc")
    if framework not in FRAMEWORKS:
        framework = "ad_hoc"
    stop = _num(args.get("stop_loss_price"))
    target1 = _num(args.get("target1_price"))
    if not stop and not target1 and entry_price:
        fw_def = FRAMEWORK_DEFAULTS.get(framework)
        if fw_def:
            stop = round(entry_price * fw_def["stop_pct"], 4)
            target1 = round(entry_price * fw_def["target_pct"], 4)
    wait_price = _num(args.get("wait_price"))
    validity_date = (args.get("validity_date") or "").strip() or str(date.today() + timedelta(days=30))
    conviction = int(args.get("conviction") or 3)
    monitoring = 1 if args.get("monitoring", 1) in (1, True, "1", "true") else 0
    direction = ""
    if wait_price:
        direction = "breakout" if (entry_price and wait_price > entry_price) else "pullback"

    notes = args.get("notes") or ""
    if tier == "actionable":
        notes += "\n操作计划（仅记录，不下单）：" + json.dumps(
            {key: args.get(key) for key in ("quantity", "order_type", "account")}, ensure_ascii=False)
    cur = conn.execute(
        """INSERT INTO recommendations
           (is_bought, is_paper, session_id, created_at, ticker, market, framework, entry_price,
            stop_loss_price, target1_price, signals, conviction, source_type, notes,
            entry_rec, risk_level, hold_period, strategy_type, catalyst, wait_price,
            wait_condition, validity_date, monitor_direction, monitoring, monitor_state)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            0, 0, None, str(date.today()), ticker, str(args.get("market") or "US"),
            framework, entry_price, stop, target1,
            json.dumps(args.get("signals") or [], ensure_ascii=False),
            conviction, str(args.get("source_type") or "agent"), notes,
            tier, str(args.get("risk_level") or "medium"), str(args.get("hold_period") or "short"),
            str(args.get("strategy_type") or "momentum"), args.get("catalyst") or "",
            wait_price, args.get("wait_condition") or "", validity_date, direction,
            monitoring, "tracking" if monitoring else "disabled",
        ),
    )
    rec_id = cur.lastrowid
    return {
        "rec_id": rec_id,
        "detail_url": f"/rec/{rec_id}",
        "summary": f"已录入 {ticker}（{'可入' if tier == 'actionable' else '观察'}）推荐 #{rec_id}",
    }


def _validate_update_monitor(args, conn=None):
    errors = []
    rec_id = args.get("rec_id")
    try:
        int(rec_id)
    except (TypeError, ValueError):
        errors.append("rec_id 必须是推荐记录 ID（整数）")
    action = str(args.get("action") or "").strip().lower()
    if action not in {"pause", "resume", "close"}:
        errors.append("action 必须是 pause（暂停监控）/ resume（恢复监控）/ close（关闭推荐）")
    return errors


def _run_update_monitor(conn, args):
    rec_id = int(args["rec_id"])
    action = str(args["action"]).strip().lower()
    row = conn.execute(
        "SELECT ticker, validity_date, hold_period, status FROM recommendations WHERE id=?", (rec_id,)
    ).fetchone()
    if not row:
        return {"error": f"推荐 #{rec_id} 不存在", "summary": f"推荐 #{rec_id} 不存在"}
    if row["status"] != "active" and action != "close":
        return {"error": "记录已结束，不能恢复或暂停监控"}
    if action == "pause":
        conn.execute(
            "UPDATE recommendations SET monitoring=0, monitor_state='disabled' WHERE id=?",
            (rec_id,),
        )
        text = f"已暂停 {row['ticker']} 的监控"
    elif action == "resume":
        validity = row["validity_date"]
        if not validity or validity < date.today().isoformat():
            validity = str(date.today() + timedelta(
                days={"short": 30, "medium": 90, "long": 180, "vlong": 90}.get(row["hold_period"], 90)))
        conn.execute(
            "UPDATE recommendations SET monitoring=1, monitor_state='tracking', validity_date=? WHERE id=?",
            (validity, rec_id),
        )
        text = f"已恢复 {row['ticker']} 的监控（有效期至 {validity}）"
    else:
        conn.execute(
            "UPDATE recommendations SET status='stopped', close_date=?, close_reason=?, "
            "monitoring=0, monitor_state='disabled' WHERE id=?",
            (str(date.today()), "Agent 建议关闭（用户已批准）", rec_id),
        )
        text = f"已关闭 {row['ticker']} 推荐记录"
    return {"ok": True, "summary": text}


def _validate_send_notification(args, conn=None):
    errors = []
    title = (args.get("title") or "").strip()
    message = (args.get("message") or "").strip()
    if not title or len(title) > 80:
        errors.append("title 必填且不超过 80 字")
    if not message or len(message) > 1000:
        errors.append("message 必填且不超过 1000 字")
    return errors


def _run_send_notification(conn, args):
    conn.execute(
        "INSERT INTO notifications (title, message, is_read, created_at) VALUES (?,?,0,?)",
        (args["title"], args["message"], datetime.now().isoformat(timespec="seconds")),
    )
    return {"ok": True, "summary": f"通知已发送：{args['title']}"}


# ── registry ─────────────────────────────────────────────────────────────────

_HANDLERS = {
    "query_portfolio": _tool_query_portfolio,
    "query_recommendations": _tool_query_recommendations,
    "query_catalysts": _tool_query_catalysts,
    "get_quotes": _tool_get_quotes,
    "get_sector_signals": _tool_get_sector_signals,
    "search_knowledge": _tool_search_knowledge,
    "get_market_calendar": _tool_get_market_calendar,
}

_WRITE_VALIDATORS = {
    "propose_recommendation": _validate_propose_recommendation,
    "update_monitor": _validate_update_monitor,
    "send_notification": _validate_send_notification,
}

_WRITE_RUNNERS = {
    "propose_recommendation": _run_propose_recommendation,
    "update_monitor": _run_update_monitor,
    "send_notification": _run_send_notification,
}


def summarize_write(tool_name, args):
    """Human-readable one-liner shown on the approval card."""
    if tool_name == "propose_recommendation":
        tier = "可入" if args.get("entry_rec") == "actionable" else "观察"
        return (
            f"录入推荐 {str(args.get('ticker') or '?').upper()}（{tier}）："
            f"入场 {args.get('entry_price') or '-'}，止损 {args.get('stop_loss_price') or '-'}，"
            f"目标 {args.get('target1_price') or '-'}，等待价 {args.get('wait_price') or '-'}"
        )
    if tool_name == "update_monitor":
        return f"监控操作：推荐 #{args.get('rec_id')} → {args.get('action')}"
    if tool_name == "send_notification":
        return f"发送通知：{args.get('title')}"
    return json.dumps(args, ensure_ascii=False)[:120]


def tool_definitions(include_write=True):
    """OpenAI-format tool list mounted for a run."""
    specs = [
        {
            "type": "function",
            "function": {
                "name": "query_portfolio",
                "description": "查询当前持仓（每票数量/成本/现价/盈亏）、组合市值与推荐统计",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_recommendations",
                "description": "查询推荐/观察清单，可按状态、档位（actionable/watch/wait）、框架、ticker 过滤",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "active/stopped，默认 active"},
                        "entry_rec": {"type": "string", "description": "actionable/watch/wait/immediate"},
                        "framework": {"type": "string"},
                        "ticker": {"type": "string"},
                        "limit": {"type": "integer", "description": "默认 20，最大 50"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_catalysts",
                "description": "查询未来 N 天的催化剂/事件（财报、政策、板块事件），可按 ticker 过滤",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "days": {"type": "integer", "description": "默认 30 天"},
                        "limit": {"type": "integer"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_quotes",
                "description": "获取实时报价（最多 10 个 ticker）；行情 API 未配置时降级返回数据库最新快照价",
                "parameters": {
                    "type": "object",
                    "properties": {"symbols": {"type": "array", "items": {"type": "string"}}},
                    "required": ["symbols"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_sector_signals",
                "description": "查询板块信号：AI 基建轮动信号（score/5）与各框架活跃推荐分布",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "检索投资框架与规则文档（供应链瓶颈、轮动、动量催化、NAV 折价、三档决策门等），回答方法论问题前必须先检索",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "k": {"type": "integer", "description": "返回条数，默认 5"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_market_calendar",
                "description": "查询未来 N 天财经日历事件与活跃宏观命题",
                "parameters": {
                    "type": "object",
                    "properties": {"days": {"type": "integer", "description": "默认 14 天"}},
                },
            },
        },
    ]
    if include_write:
        specs += [
            {
                "type": "function",
                "function": {
                    "name": "propose_recommendation",
                    "description": (
                        "录入一条新推荐（写入操作，需用户审批）。"
                        "entry_rec=actionable（可入）时必须提供 entry_price 和风控锚点（stop_loss_price 或 target1_price），"
                        "并在 notes 写操作指令；entry_rec=watch（观察）时必须提供 wait_price 或 wait_condition，"
                        "并可设 validity_date。'不碰'的股票不要调用此工具。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string"},
                            "quantity": {"type": "number", "description": "可入档计划数量，不自动下单"},
                            "order_type": {"type": "string", "enum": ["market", "limit"]},
                            "account": {"type": "string", "enum": ["paper", "manual_real"]},
                            "entry_rec": {"type": "string", "enum": ["actionable", "watch"]},
                            "entry_price": {"type": "number", "description": "参考入场价（可入档必填）"},
                            "stop_loss_price": {"type": "number"},
                            "target1_price": {"type": "number"},
                            "wait_price": {"type": "number", "description": "等待/触发价"},
                            "wait_condition": {"type": "string", "description": "观察档触发条件描述"},
                            "validity_date": {"type": "string", "description": "复查日期 YYYY-MM-DD"},
                            "conviction": {"type": "integer", "description": "1-5"},
                            "framework": {"type": "string", "enum": sorted(FRAMEWORKS)},
                            "catalyst": {"type": "string"},
                            "notes": {"type": "string", "description": "操作指令：怎么买/买多少/哪个账户"},
                            "monitoring": {"type": "boolean", "description": "是否挂价格监控，默认 true"},
                        },
                        "required": ["ticker", "entry_rec"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_monitor",
                    "description": "更新已有推荐的监控状态（写入操作，需用户审批）：pause 暂停、resume 恢复、close 关闭推荐",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "rec_id": {"type": "integer"},
                            "action": {"type": "string", "enum": ["pause", "resume", "close"]},
                        },
                        "required": ["rec_id", "action"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_notification",
                    "description": "向通知中心发送一条提醒（写入操作，需用户审批）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "message": {"type": "string"},
                        },
                        "required": ["title", "message"],
                    },
                },
            },
        ]
    return specs


def is_write(name):
    return name in WRITE_TOOLS


def known_tool(name):
    return name in _HANDLERS or name in _WRITE_VALIDATORS


def run_read(conn, name, args):
    """Execute a read tool and return its result dict (raises ToolError on bad args)."""
    handler = _HANDLERS.get(name)
    if not handler:
        raise ToolError(f"未知工具: {name}")
    errors = validate_arguments(name, args)
    if errors:
        raise ToolError("; ".join(errors))
    return handler(conn, args or {})


def validate_write(name, args, conn=None):
    """Validate a write tool call BEFORE creating the pending action.

    Returns a list of error strings; empty list means the args are acceptable.
    """
    validator = _WRITE_VALIDATORS.get(name)
    if not validator:
        return [f"未知写工具: {name}"]
    try:
        errors = validate_arguments(name, args)
        return errors + validator(args or {}, conn=conn)
    except Exception as exc:  # defensive: validators should not throw
        return [f"参数校验异常: {exc}"]


def run_write(conn, name, args):
    """Execute an approved write tool. Only called from the approval resolver."""
    runner = _WRITE_RUNNERS.get(name)
    if not runner:
        return {"error": f"未知写工具: {name}", "summary": f"未知写工具 {name}"}
    errors = validate_write(name, args, conn=conn)
    if errors:
        return {"error": "; ".join(errors)}
    return runner(conn, args or {})


def validate_arguments(name, args):
    """Validate the public tool schema at the execution boundary, not just in prompts."""
    if not isinstance(args, dict):
        return ["工具参数必须为对象"]
    spec = next((x["function"]["parameters"] for x in tool_definitions() if x["function"]["name"] == name), None)
    if spec is None:
        return ["未知工具"]
    errors = [f"缺少参数 {key}" for key in spec.get("required", []) if key not in args]
    for key, value in args.items():
        prop = spec.get("properties", {}).get(key)
        if prop is None:
            # Existing business fields remain backward compatible; never execute code/SQL.
            continue
        kind = prop.get("type")
        valid = {"string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
                 "number": isinstance(value, (int, float)) and not isinstance(value, bool),
                 "array": isinstance(value, list), "object": isinstance(value, dict)}.get(kind, True)
        if not valid or ("enum" in prop and value not in prop["enum"]):
            errors.append(f"参数 {key} 类型或取值不合法")
        if kind == "array" and isinstance(value, list) and prop.get("items", {}).get("type") == "string":
            if not all(isinstance(item, str) for item in value):
                errors.append(f"参数 {key} 必须为字符串列表")
    return errors
