from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import sqlite3
import json
import subprocess
from datetime import date, datetime, timedelta
import os

app = Flask(__name__)
app.secret_key = "tracker-2026"


def _resolve_db_path():
    """Always use the main worktree's database so all worktrees share one source of truth."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        import subprocess
        out = subprocess.check_output(
            ["git", "worktree", "list", "--porcelain"],
            cwd=here, stderr=subprocess.DEVNULL,
        ).decode()
        main_root = out.strip().split("\n")[0].replace("worktree ", "").strip()
        candidate = os.path.join(main_root, "tracker", "data", "tracker.db")
        if os.path.isdir(os.path.dirname(candidate)):
            return candidate
    except Exception:
        pass
    return os.path.join(here, "data", "tracker.db")


DB_PATH = _resolve_db_path()

FRAMEWORKS = {
    "momentum": "动量催化框架",
    "fundamental": "基本面研究框架",
    "supply_chain": "供应链瓶颈框架",
    "radar": "每日扫描雷达",
    "ad_hoc": "临时问询",
}

ENTRY_REC = {
    "immediate": ("✅", "立即可入", "entry-immediate"),
    "wait":      ("⏳", "等回落",   "entry-wait"),
    "watch":     ("👀", "仅观察",   "entry-watch"),
    "avoid":     ("❌", "不碰",     "entry-avoid"),
}

# Badge text without emoji (for table/badge display)
ENTRY_REC_LABEL = {
    "immediate": ("立即可入", "entry-immediate"),
    "wait":      ("等回落",   "entry-wait"),
    "watch":     ("仅观察",   "entry-watch"),
    "avoid":     ("不碰",     "entry-avoid"),
}

STATUS_LABEL = {
    "active":  "追踪中",
    "stopped": "已停止",
    "profit":  "已止盈",
    "loss":    "已止损",
}

RISK_LEVEL = {
    "high":   ("高风险", "tag-risk-high"),
    "medium": ("中风险", "tag-risk-medium"),
    "low":    ("低风险", "tag-risk-low"),
}

HOLD_PERIOD = {
    "short":  "短线 (<1月)",
    "medium": "中线 (1-3月)",
    "long":   "中长线 (3-6月)",
    "vlong":  "长线 (6月+)",
    "-":      "—",
}

STRATEGY_TYPE = {
    "momentum":    "情绪催化",
    "fundamental": "基本面",
    "theme":       "主题埋伏",
    "technical":   "技术反弹",
}

MOMENTUM_SIGNALS = [
    "强催化剂（符合当前市场主线）",
    "大单净流入为正",
    "今日涨幅 < 8% 或均线偏离 < 15%",
    "新闻在 24 小时内（非旧闻）",
    "成交量放大（量比 > 1.5x）",
]

FUNDAMENTAL_SIGNALS = [
    "估值低于行业均值",
    "营收/利润持续增长",
    "护城河明显（品牌/技术/渠道）",
    "机构持仓增加",
    "催化剂：产品/政策/并购",
]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS analysis_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATE NOT NULL,
            question TEXT NOT NULL,
            theme TEXT DEFAULT '',
            market_context TEXT DEFAULT '',
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            is_bought INTEGER DEFAULT 0,
            session_id INTEGER,
            created_at DATE NOT NULL,
            ticker TEXT NOT NULL,
            market TEXT DEFAULT 'US',
            framework TEXT NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss_price REAL,
            target1_price REAL,
            target2_price REAL,
            signals TEXT DEFAULT '[]',
            conviction INTEGER DEFAULT 3,
            source_type TEXT DEFAULT 'paper',
            notes TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            close_price REAL,
            close_date DATE,
            close_reason TEXT,
            entry_rec TEXT DEFAULT 'watch',
            risk_level TEXT DEFAULT 'medium',
            hold_period TEXT DEFAULT 'short',
            strategy_type TEXT DEFAULT 'momentum',
            catalyst TEXT DEFAULT '',
            wait_price REAL,
            wait_condition TEXT DEFAULT '',
            validity_date DATE,
            monitoring INTEGER DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES analysis_sessions(id)
        );

        CREATE TABLE IF NOT EXISTS price_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id INTEGER NOT NULL,
            snapshot_date DATE NOT NULL,
            price REAL NOT NULL,
            pct_change REAL,
            FOREIGN KEY (recommendation_id) REFERENCES recommendations(id)
        );
    """)
    # Migrate existing table if missing new columns
    existing = {row[1] for row in conn.execute("PRAGMA table_info(recommendations)")}
    new_cols = {
        "is_bought": "INTEGER DEFAULT 0",
        "session_id": "INTEGER",
        "entry_rec": "TEXT DEFAULT 'watch'",
        "risk_level": "TEXT DEFAULT 'medium'",
        "hold_period": "TEXT DEFAULT 'short'",
        "strategy_type": "TEXT DEFAULT 'momentum'",
        "catalyst": "TEXT DEFAULT ''",
        "wait_price": "REAL",
        "wait_condition": "TEXT DEFAULT ''",
        "validity_date": "DATE",
        "monitoring": "INTEGER DEFAULT 0",
    }
    for col, coldef in new_cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE recommendations ADD COLUMN {col} {coldef}")
    conn.commit()
    conn.close()


def calc_pct(entry, current):
    if entry and current:
        return round((current - entry) / entry * 100, 2)
    return None


def _parse_signals(raw):
    if not raw:
        return []
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, ValueError):
        return [s.strip() for s in raw.split(";") if s.strip()]


def enrich_rows(rows):
    if not rows:
        return []
    rows = [dict(r) for r in rows]
    ids = [r["id"] for r in rows]
    session_ids = list({r["session_id"] for r in rows if r.get("session_id")})
    conn = get_db()
    snaps = {}
    if ids:
        ph = ",".join("?" * len(ids))
        for s in conn.execute(
            f"SELECT recommendation_id, price, snapshot_date FROM price_snapshots "
            f"WHERE recommendation_id IN ({ph}) ORDER BY snapshot_date DESC", ids
        ).fetchall():
            rid = s["recommendation_id"]
            if rid not in snaps:
                snaps[rid] = dict(s)
    sessions_map = {}
    if session_ids:
        ph = ",".join("?" * len(session_ids))
        for s in conn.execute(
            f"SELECT id, question, theme FROM analysis_sessions WHERE id IN ({ph})", session_ids
        ).fetchall():
            sessions_map[s["id"]] = dict(s)
    conn.close()

    today = date.today()
    for r in rows:
        r["signals"] = _parse_signals(r.get("signals"))
        r["framework_name"] = FRAMEWORKS.get(r["framework"], r["framework"])
        r["entry_rec_info"] = ENTRY_REC.get(r.get("entry_rec") or "watch", ENTRY_REC["watch"])
        r["entry_rec_label"] = ENTRY_REC_LABEL.get(r.get("entry_rec") or "watch", ENTRY_REC_LABEL["watch"])
        r["status_label"] = STATUS_LABEL.get(r.get("status") or "active", r.get("status", ""))
        r["risk_info"] = RISK_LEVEL.get(r.get("risk_level") or "medium", RISK_LEVEL["medium"])
        r["hold_label"] = HOLD_PERIOD.get(r.get("hold_period") or "short", "")
        r["strategy_label"] = STRATEGY_TYPE.get(r.get("strategy_type") or "momentum", "")

        sess = sessions_map.get(r.get("session_id"))
        r["session_question"] = sess["question"] if sess else ""
        r["session_theme"] = sess["theme"] if sess else ""

        snap = snaps.get(r["id"])
        r["pct_change"] = None
        if snap:
            r["latest_price"] = snap["price"]
            r["latest_date"] = snap["snapshot_date"]
            r["pct_change"] = calc_pct(r["entry_price"], snap["price"])
            try:
                r["days_since_update"] = (today - date.fromisoformat(snap["snapshot_date"])).days
            except Exception:
                r["days_since_update"] = None
        elif r.get("close_price"):
            r["latest_price"] = r["close_price"]
            r["latest_date"] = r["close_date"]
            r["pct_change"] = calc_pct(r["entry_price"], r["close_price"])
            r["days_since_update"] = None
        else:
            r["latest_price"] = None
            r["latest_date"] = None
            r["days_since_update"] = None

        r["days_until_expiry"] = None
        r["is_expired_validity"] = False
        r["is_expiring_soon"] = False
        if r.get("validity_date") and r.get("status") == "active":
            try:
                vd = date.fromisoformat(r["validity_date"])
                diff = (vd - today).days
                r["days_until_expiry"] = diff
                r["is_expired_validity"] = diff < 0
                r["is_expiring_soon"] = 0 <= diff <= 3
            except Exception:
                pass

        r["price_alert"] = False
        if r.get("monitoring") and r.get("wait_price") and r.get("latest_price"):
            gap_pct = abs(r["latest_price"] - r["wait_price"]) / r["wait_price"] * 100
            r["price_alert"] = gap_pct <= 3

    return rows


def enrich_row(row):
    return enrich_rows([row])[0]


def compute_stats():
    conn = get_db()
    rows = conn.execute("SELECT * FROM recommendations").fetchall()
    conn.close()
    rows = enrich_rows(rows)
    total = len(rows)
    active = sum(1 for r in rows if r["status"] == "active")
    immediate = sum(1 for r in rows if r.get("entry_rec") == "immediate" and r["status"] == "active")
    monitoring = sum(1 for r in rows if r.get("monitoring") and r["status"] == "active")

    def judge_call(r):
        """判断一条推荐的对错。返回 'win' / 'loss' / None（未定）。

        - 立即可入/已买（持仓类）：到达止盈 = 对，触发止损 = 错
        - 不碰（反向预测）：跌穿 -8% = 对，涨过 +15% = 错
        - 仅观察/等回落：不计入（没有方向判断）
        """
        if r["entry_rec"] == "immediate" or r.get("is_bought"):
            if r["status"] == "profit":
                return "win"
            if r["status"] == "loss":
                return "loss"
            return None
        if r["entry_rec"] == "avoid":
            pct = r.get("pct_change")
            if pct is None:
                return None
            if pct <= -8:
                return "win"
            if pct >= 15:
                return "loss"
            return None
        return None

    judged = [(r, judge_call(r)) for r in rows]
    decided = [(r, v) for r, v in judged if v is not None]
    wins = [r for r, v in decided if v == "win"]
    fw_win_rate = round(len(wins) / len(decided) * 100, 1) if decided else None

    t21_eligible = [r for r in rows if r["created_at"] and
                    (date.today() - date.fromisoformat(r["created_at"])).days >= 21
                    and r["pct_change"] is not None]
    t21_wins = [r for r in t21_eligible if (r["pct_change"] or 0) > 0]
    t21_win_rate = round(len(t21_wins) / len(t21_eligible) * 100, 1) if t21_eligible else None

    fw_stats = {}
    for fw, fw_name in FRAMEWORKS.items():
        fw_rows = [r for r in rows if r["framework"] == fw]
        fw_decided = [(r, v) for r, v in judged if v is not None and r["framework"] == fw]
        fw_wins = [r for r, v in fw_decided if v == "win"]
        fw_stats[fw] = {
            "name": fw_name, "total": len(fw_rows),
            "active": sum(1 for r in fw_rows if r["status"] == "active"),
            "win_rate": round(len(fw_wins) / len(fw_decided) * 100, 1) if fw_decided else None,
            "closed": len(fw_decided),
        }
    return {
        "total": total, "active": active, "immediate": immediate, "monitoring": monitoring,
        "fw_win_rate": fw_win_rate, "fw_win_count": len(wins), "fw_closed_count": len(decided),
        "t21_win_rate": t21_win_rate, "t21_win_count": len(t21_wins), "t21_eligible_count": len(t21_eligible),
        "fw_stats": fw_stats,
    }


@app.route("/")
def dashboard():
    conn = get_db()
    recent_recs = conn.execute(
        "SELECT * FROM recommendations ORDER BY created_at DESC LIMIT 8"
    ).fetchall()
    sessions = conn.execute(
        "SELECT * FROM analysis_sessions ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    monitoring_recs = conn.execute(
        "SELECT * FROM recommendations WHERE monitoring=1 AND status='active' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    recent_recs = enrich_rows(recent_recs)
    monitoring_recs = enrich_rows(monitoring_recs)
    stats = compute_stats()
    sessions_list = []
    for s in sessions:
        s = dict(s)
        conn = get_db()
        s["rec_count"] = conn.execute(
            "SELECT COUNT(*) FROM recommendations WHERE session_id=?", (s["id"],)
        ).fetchone()[0]
        conn.close()
        sessions_list.append(s)
    return render_template("dashboard.html", recent=recent_recs, stats=stats,
                           sessions=sessions_list, monitoring=monitoring_recs)


@app.route("/sessions")
def sessions():
    conn = get_db()
    rows = conn.execute("SELECT * FROM analysis_sessions ORDER BY created_at DESC").fetchall()
    conn.close()
    result = []
    for s in rows:
        s = dict(s)
        conn = get_db()
        recs = conn.execute(
            "SELECT entry_rec, status FROM recommendations WHERE session_id=?", (s["id"],)
        ).fetchall()
        conn.close()
        s["rec_count"] = len(recs)
        s["immediate_count"] = sum(1 for r in recs if r["entry_rec"] == "immediate")
        s["wait_count"] = sum(1 for r in recs if r["entry_rec"] == "wait")
        s["watch_count"] = sum(1 for r in recs if r["entry_rec"] == "watch")
        result.append(s)
    return render_template("sessions.html", sessions=result)


@app.route("/session/add", methods=["GET", "POST"])
def add_session():
    if request.method == "POST":
        question = request.form.get("question", "").strip()
        theme = request.form.get("theme", "").strip()
        market_context = request.form.get("market_context", "").strip()
        created_at = request.form.get("created_at") or str(date.today())
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO analysis_sessions (created_at, question, theme, market_context) VALUES (?,?,?,?)",
            (created_at, question, theme, market_context),
        )
        sid = cur.lastrowid
        conn.commit()
        conn.close()
        flash(f"已创建分析会话 #{sid}", "success")
        return redirect(url_for("session_detail", session_id=sid))
    return render_template("add_session.html", today=str(date.today()))


@app.route("/session/<int:session_id>/edit", methods=["GET", "POST"])
def edit_session(session_id):
    conn = get_db()
    session = conn.execute("SELECT * FROM analysis_sessions WHERE id=?", (session_id,)).fetchone()
    if not session:
        conn.close()
        flash("会话不存在", "danger")
        return redirect(url_for("sessions"))
    if request.method == "POST":
        conn.execute(
            "UPDATE analysis_sessions SET created_at=?, question=?, theme=?, market_context=? WHERE id=?",
            (request.form.get("created_at") or str(date.today()),
             request.form.get("question", "").strip(),
             request.form.get("theme", "").strip(),
             request.form.get("market_context", "").strip(),
             session_id),
        )
        conn.commit()
        conn.close()
        flash("会话已更新", "success")
        return redirect(url_for("session_detail", session_id=session_id))
    conn.close()
    return render_template("edit_session.html", session=dict(session))


@app.route("/session/<int:session_id>/delete", methods=["POST"])
def delete_session(session_id):
    conn = get_db()
    # Unlink recommendations (don't delete them, just detach from session)
    conn.execute("UPDATE recommendations SET session_id=NULL WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM analysis_sessions WHERE id=?", (session_id,))
    conn.commit()
    conn.close()
    flash("会话已删除（推荐记录已保留，可在「所有推荐」中查看）", "warning")
    return redirect(url_for("sessions"))


@app.route("/session/<int:session_id>")
def session_detail(session_id):
    conn = get_db()
    session = conn.execute("SELECT * FROM analysis_sessions WHERE id=?", (session_id,)).fetchone()
    recs = conn.execute(
        "SELECT * FROM recommendations WHERE session_id=? ORDER BY entry_rec, ticker",
        (session_id,)
    ).fetchall()
    conn.close()
    if not session:
        flash("会话不存在", "danger")
        return redirect(url_for("sessions"))
    recs = enrich_rows(recs)
    return render_template("session_detail.html", session=dict(session), recs=recs,
                           entry_rec=ENTRY_REC, today=str(date.today()))


PER_PAGE = 50


@app.route("/recommendations")
def recommendations():
    framework = request.args.get("framework", "")
    status = request.args.get("status", "")
    source = request.args.get("source", "")
    entry_rec_filter = request.args.get("entry_rec", "")
    bought_filter = request.args.get("bought", "")
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1

    where = " WHERE 1=1"
    params = []
    if framework:
        where += " AND framework=?"; params.append(framework)
    if status:
        where += " AND status=?"; params.append(status)
    if source:
        where += " AND source_type=?"; params.append(source)
    if entry_rec_filter:
        where += " AND entry_rec=?"; params.append(entry_rec_filter)
    if bought_filter != "":
        where += " AND is_bought=?"; params.append(int(bought_filter))

    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM recommendations" + where, params).fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM recommendations" + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [PER_PAGE, (page - 1) * PER_PAGE],
    ).fetchall()
    conn.close()
    rows = enrich_rows(rows)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    filter_qs = "&".join(
        f"{k}={v}" for k, v in [
            ("framework", framework), ("status", status), ("source", source),
            ("entry_rec", entry_rec_filter), ("bought", bought_filter),
        ] if v
    )
    return render_template(
        "recommendations.html", rows=rows, frameworks=FRAMEWORKS,
        entry_rec_map=ENTRY_REC,
        selected_fw=framework, selected_status=status,
        selected_source=source, selected_entry=entry_rec_filter,
        selected_bought=bought_filter,
        page=page, pages=pages, total=total, filter_qs=filter_qs,
    )


@app.route("/rec/<int:rec_id>/edit", methods=["GET", "POST"])
def edit_rec(rec_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    if not row:
        conn.close()
        flash("记录不存在", "danger")
        return redirect(url_for("recommendations"))
    if request.method == "POST":
        entry_price = float(request.form.get("entry_price") or row["entry_price"])
        framework = row["framework"]
        if framework == "momentum" and entry_price:
            stop = round(entry_price * 0.92, 4)
            t1 = round(entry_price * 1.15, 4)
            t2 = round(entry_price * 1.30, 4)
        else:
            stop = float(request.form.get("stop_loss_price") or 0) or None
            t1 = float(request.form.get("target1_price") or 0) or None
            t2 = float(request.form.get("target2_price") or 0) or None
        wait_price_raw = request.form.get("wait_price", "")
        conn.execute(
            """UPDATE recommendations SET
               entry_price=?, stop_loss_price=?, target1_price=?, target2_price=?,
               entry_rec=?, risk_level=?, hold_period=?, strategy_type=?,
               catalyst=?, wait_price=?, wait_condition=?, validity_date=?,
               conviction=?, notes=?, signals=?
               WHERE id=?""",
            (entry_price, stop, t1, t2,
             request.form.get("entry_rec", row["entry_rec"]),
             request.form.get("risk_level", row["risk_level"]),
             request.form.get("hold_period", row["hold_period"]),
             request.form.get("strategy_type", row["strategy_type"]),
             request.form.get("catalyst", ""),
             float(wait_price_raw) if wait_price_raw else None,
             request.form.get("wait_condition", ""),
             request.form.get("validity_date") or None,
             int(request.form.get("conviction") or row["conviction"] or 3),
             request.form.get("notes", ""),
             json.dumps(request.form.getlist("signals"), ensure_ascii=False),
             rec_id),
        )
        conn.commit()
        conn.close()
        flash(f"已更新 {row['ticker']}", "success")
        return redirect(url_for("detail", rec_id=rec_id))
    conn.close()
    row = dict(row)
    row["signals"] = _parse_signals(row.get("signals"))
    return render_template(
        "edit_rec.html", row=row, frameworks=FRAMEWORKS,
        momentum_signals=MOMENTUM_SIGNALS,
        fundamental_signals=FUNDAMENTAL_SIGNALS,
        entry_rec=ENTRY_REC, risk_level=RISK_LEVEL,
        hold_period=HOLD_PERIOD, strategy_type=STRATEGY_TYPE,
        today=str(date.today()),
    )


@app.route("/add", methods=["GET", "POST"])
def add():
    session_id = request.args.get("session_id", "")
    if request.method == "POST":
        ticker = request.form.get("ticker", "").upper().strip()
        market = request.form.get("market", "US")
        framework = request.form.get("framework", "momentum")
        entry_price = float(request.form.get("entry_price", 0))
        source_type = request.form.get("source_type", "paper")
        conviction = int(request.form.get("conviction", 3))
        notes = request.form.get("notes", "")
        created_at = request.form.get("created_at") or str(date.today())
        signals = request.form.getlist("signals")
        sid = request.form.get("session_id") or None

        entry_rec = request.form.get("entry_rec", "watch")
        risk_level = request.form.get("risk_level", "medium")
        hold_period = request.form.get("hold_period", "short")
        strategy_type = request.form.get("strategy_type", "momentum")
        catalyst = request.form.get("catalyst", "")
        wait_price_raw = request.form.get("wait_price", "")
        wait_price = float(wait_price_raw) if wait_price_raw else None
        wait_condition = request.form.get("wait_condition", "")
        validity_date = request.form.get("validity_date") or None

        if framework == "momentum":
            stop = round(entry_price * 0.92, 4)
            t1 = round(entry_price * 1.15, 4)
            t2 = round(entry_price * 1.30, 4)
        else:
            stop = float(request.form.get("stop_loss_price") or 0) or None
            t1 = float(request.form.get("target1_price") or 0) or None
            t2 = float(request.form.get("target2_price") or 0) or None

        conn = get_db()
        cur = conn.execute(
            """INSERT INTO recommendations
               (is_bought, session_id, created_at, ticker, market, framework, entry_price,
                stop_loss_price, target1_price, target2_price, signals,
                conviction, source_type, notes,
                entry_rec, risk_level, hold_period, strategy_type,
                catalyst, wait_price, wait_condition, validity_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (0, sid, created_at, ticker, market, framework, entry_price,
             stop, t1, t2, json.dumps(signals, ensure_ascii=False),
             conviction, source_type, notes,
             entry_rec, risk_level, hold_period, strategy_type,
             catalyst, wait_price, wait_condition, validity_date),
        )
        rec_id = cur.lastrowid
        conn.commit()
        conn.close()
        flash(f"已添加 {ticker} 推荐记录 #{rec_id}", "success")
        if sid:
            return redirect(url_for("session_detail", session_id=int(sid)))
        return redirect(url_for("detail", rec_id=rec_id))

    conn = get_db()
    all_sessions = conn.execute(
        "SELECT id, question, created_at FROM analysis_sessions ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return render_template("add.html", frameworks=FRAMEWORKS,
                           momentum_signals=MOMENTUM_SIGNALS,
                           fundamental_signals=FUNDAMENTAL_SIGNALS,
                           today=str(date.today()),
                           entry_rec=ENTRY_REC, risk_level=RISK_LEVEL,
                           hold_period=HOLD_PERIOD, strategy_type=STRATEGY_TYPE,
                           all_sessions=all_sessions, default_session=session_id)


@app.route("/rec/<int:rec_id>")
def detail(rec_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    snapshots = conn.execute(
        "SELECT * FROM price_snapshots WHERE recommendation_id=? ORDER BY snapshot_date",
        (rec_id,),
    ).fetchall()
    conn.close()
    if not row:
        flash("记录不存在", "danger")
        return redirect(url_for("recommendations"))
    row = enrich_row(row)
    return render_template("detail.html", row=row, snapshots=[dict(s) for s in snapshots],
                           frameworks=FRAMEWORKS, today=str(date.today()),
                           entry_rec=ENTRY_REC, risk_level=RISK_LEVEL,
                           hold_period=HOLD_PERIOD, strategy_type=STRATEGY_TYPE)


@app.route("/rec/<int:rec_id>/update_price", methods=["POST"])
def update_price(rec_id):
    price = float(request.form.get("price", 0))
    snap_date = request.form.get("snap_date") or str(date.today())
    conn = get_db()
    row = conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    pct = calc_pct(row["entry_price"], price)
    existing = conn.execute(
        "SELECT id FROM price_snapshots WHERE recommendation_id=? AND snapshot_date=?",
        (rec_id, snap_date),
    ).fetchone()
    if existing:
        conn.execute("UPDATE price_snapshots SET price=?, pct_change=? WHERE id=?",
                     (price, pct, existing["id"]))
    else:
        conn.execute(
            "INSERT INTO price_snapshots (recommendation_id, snapshot_date, price, pct_change) VALUES (?,?,?,?)",
            (rec_id, snap_date, price, pct),
        )
    msg = f"价格已更新：${price:.2f}（{pct:+.1f}%）"
    # 止损/止盈只对真实或模拟持仓生效（立即可入 / 已买入）；
    # 观察、等回落、不碰类是预测记录，不存在持仓，不触发
    is_position = row["entry_rec"] == "immediate" or row["is_bought"]
    if row["status"] == "active" and is_position:
        if row["stop_loss_price"] and price <= row["stop_loss_price"]:
            conn.execute(
                "UPDATE recommendations SET status='loss', close_price=?, close_date=?, close_reason='止损触发' WHERE id=?",
                (price, snap_date, rec_id))
            msg += " ⚠️ 止损触发"
        elif row["target1_price"] and price >= row["target1_price"]:
            conn.execute("UPDATE recommendations SET status='profit' WHERE id=?", (rec_id,))
            msg += " 🎯 到达止盈目标"
    if row["monitoring"] and row["wait_price"] and price <= row["wait_price"]:
        flash(f"🔔 {row['ticker']} 已到达等待价 ${row['wait_price']:.2f}，可以考虑入场！", "warning")
    conn.commit()
    conn.close()
    flash(msg, "info")
    return redirect(url_for("detail", rec_id=rec_id))


@app.route("/rec/<int:rec_id>/toggle_bought", methods=["POST"])
def toggle_bought(rec_id):
    conn = get_db()
    row = conn.execute("SELECT is_bought, ticker FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    new_val = 0 if row["is_bought"] else 1
    conn.execute("UPDATE recommendations SET is_bought=? WHERE id=?", (new_val, rec_id))
    conn.commit()
    conn.close()
    flash(("💰 已标记为真实买入" if new_val else "已改回未买入") + f" — {row['ticker']}", "success")
    return redirect(url_for("detail", rec_id=rec_id))


@app.route("/rec/<int:rec_id>/toggle_monitor", methods=["POST"])
def toggle_monitor(rec_id):
    conn = get_db()
    row = conn.execute("SELECT monitoring, ticker FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    new_val = 0 if row["monitoring"] else 1
    conn.execute("UPDATE recommendations SET monitoring=? WHERE id=?", (new_val, rec_id))
    conn.commit()
    conn.close()
    flash(("🔔 已开启价格监控" if new_val else "已关闭监控") + f" — {row['ticker']}", "success")
    return redirect(url_for("detail", rec_id=rec_id))


@app.route("/rec/<int:rec_id>/close", methods=["POST"])
def close_rec(rec_id):
    conn = get_db()
    conn.execute(
        "UPDATE recommendations SET status=?, close_price=?, close_date=?, close_reason=? WHERE id=?",
        (request.form.get("status", "stopped"),
         float(request.form.get("close_price", 0)),
         request.form.get("close_date") or str(date.today()),
         request.form.get("close_reason", "手动关闭"), rec_id),
    )
    conn.commit()
    conn.close()
    flash("已关闭该推荐记录", "success")
    return redirect(url_for("detail", rec_id=rec_id))


@app.route("/rec/<int:rec_id>/delete", methods=["POST"])
def delete_rec(rec_id):
    conn = get_db()
    conn.execute("DELETE FROM price_snapshots WHERE recommendation_id=?", (rec_id,))
    conn.execute("DELETE FROM recommendations WHERE id=?", (rec_id,))
    conn.commit()
    conn.close()
    flash("记录已删除", "warning")
    return redirect(url_for("recommendations"))


@app.route("/api/chart/<int:rec_id>")
def chart_data(rec_id):
    conn = get_db()
    row = conn.execute("SELECT entry_price, created_at FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    snaps = conn.execute(
        "SELECT snapshot_date, price, pct_change FROM price_snapshots WHERE recommendation_id=? ORDER BY snapshot_date",
        (rec_id,),
    ).fetchall()
    conn.close()
    if not row:
        return jsonify({})
    return jsonify({
        "labels": [row["created_at"]] + [s["snapshot_date"] for s in snaps],
        "prices": [row["entry_price"]] + [s["price"] for s in snaps],
        "pct": [0.0] + [s["pct_change"] for s in snaps],
        "entry": row["entry_price"],
    })


@app.route("/api/refresh_prices", methods=["POST"])
def refresh_prices():
    conn = get_db()
    recs = conn.execute(
        "SELECT id, ticker, market, entry_price, created_at FROM recommendations"
    ).fetchall()

    def lb_symbol(ticker, market):
        t = ticker.strip().upper()
        if '.' in t:
            return t
        m = (market or 'US').upper()
        if m == 'HK':
            return f"{t}.HK"
        if m in ('CN', 'SH', 'SZ'):
            return f"{t}.SH" if (t.startswith('6') or t.startswith('688')) else f"{t}.SZ"
        return f"{t}.US"

    today_date = date.today()
    updated = 0
    errors = []

    for r in recs:
        if not r["entry_price"] or not r["created_at"]:
            continue
        try:
            start = date.fromisoformat(r["created_at"])
        except ValueError:
            continue

        sym = lb_symbol(r["ticker"], r["market"])
        rec_id = r["id"]
        entry_price = r["entry_price"]

        # Days from recommendation date to today, +10 buffer for weekends/holidays
        count = (today_date - start).days + 10
        count = max(count, 5)

        try:
            result = subprocess.run(
                ["longbridge", "kline", sym, "--period=1d", f"--count={count}"],
                capture_output=True, text=True, timeout=20
            )
        except Exception as e:
            errors.append(sym)
            continue

        existing_dates = {row[0] for row in conn.execute(
            "SELECT snapshot_date FROM price_snapshots WHERE recommendation_id=?", (rec_id,)
        ).fetchall()}

        for line in result.stdout.splitlines():
            line = line.strip()
            if not line.startswith('|'):
                continue
            parts = [p.strip() for p in line.split('|')]
            # kline columns: | Time | Open | High | Low | Close | Volume | Turnover |
            if len(parts) < 6 or not parts[1] or parts[1].startswith('-') or parts[1] == 'Time':
                continue
            try:
                snap_date = parts[1].split()[0]
                close_price = float(parts[5])
            except (ValueError, IndexError):
                continue

            if snap_date < r["created_at"]:
                continue

            pct = round((close_price - entry_price) / entry_price * 100, 2)

            if snap_date in existing_dates:
                conn.execute(
                    "UPDATE price_snapshots SET price=?, pct_change=? WHERE recommendation_id=? AND snapshot_date=?",
                    (close_price, pct, rec_id, snap_date)
                )
            else:
                conn.execute(
                    "INSERT INTO price_snapshots (recommendation_id, snapshot_date, price, pct_change) VALUES (?,?,?,?)",
                    (rec_id, snap_date, close_price, pct)
                )
                existing_dates.add(snap_date)
            updated += 1

    conn.commit()
    conn.close()
    return jsonify({"updated": updated, "errors": errors})


# API endpoints for Claude to write directly
@app.route("/api/add_session", methods=["POST"])
def api_add_session():
    data = request.get_json()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO analysis_sessions (created_at, question, theme, market_context) VALUES (?,?,?,?)",
        (data.get("created_at", str(date.today())),
         data["question"], data.get("theme", ""), data.get("market_context", "")),
    )
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"session_id": sid})


@app.route("/api/add_rec", methods=["POST"])
def api_add_rec():
    data = request.get_json()
    entry_price = float(data.get("entry_price", 0))
    framework = data.get("framework", "momentum")
    if framework == "momentum" and entry_price:
        stop = round(entry_price * 0.92, 4)
        t1 = round(entry_price * 1.15, 4)
        t2 = round(entry_price * 1.30, 4)
    else:
        stop = data.get("stop_loss_price")
        t1 = data.get("target1_price")
        t2 = data.get("target2_price")
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO recommendations
           (is_bought, session_id, created_at, ticker, market, framework, entry_price,
            stop_loss_price, target1_price, target2_price, signals,
            conviction, source_type, notes,
            entry_rec, risk_level, hold_period, strategy_type,
            catalyst, wait_price, wait_condition, validity_date)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (data.get("is_bought", 0),
         data.get("session_id"), data.get("created_at", str(date.today())),
         data["ticker"].upper(), data.get("market", "US"),
         framework, entry_price, stop, t1, t2,
         json.dumps(data.get("signals", []), ensure_ascii=False),
         data.get("conviction", 3), data.get("source_type", "paper"),
         data.get("notes", ""),
         data.get("entry_rec", "watch"), data.get("risk_level", "medium"),
         data.get("hold_period", "short"), data.get("strategy_type", "momentum"),
         data.get("catalyst", ""), data.get("wait_price"),
         data.get("wait_condition", ""), data.get("validity_date")),
    )
    rec_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"rec_id": rec_id})


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5050))
    print(f"投资追踪系统启动：http://127.0.0.1:{port}")
    app.run(debug=True, port=port)
