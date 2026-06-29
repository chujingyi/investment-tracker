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
    "momentum":    "动量催化框架",
    "fundamental": "基本面研究框架",
    "supply_chain":"供应链瓶颈框架",
    "radar":       "每日扫描雷达",
    "ad_hoc":      "临时问询",
}

# stop_pct / target_pct as multipliers; None = user must fill manually
FRAMEWORK_DEFAULTS = {
    "momentum":    {"stop_pct": 0.88, "target_pct": 1.25, "stop_label": "-12%", "target_label": "+25%"},
    "fundamental": {"stop_pct": 0.85, "target_pct": 1.40, "stop_label": "-15%", "target_label": "+40%"},
    "supply_chain":{"stop_pct": 0.88, "target_pct": 1.25, "stop_label": "-12%", "target_label": "+25%"},
    "radar":       {"stop_pct": 0.90, "target_pct": 1.20, "stop_label": "-10%", "target_label": "+20%"},
    "ad_hoc":      None,
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

CATALYST_TYPES = {
    "earnings":  "财报",
    "policy":    "政策/法规",
    "product":   "产品发布",
    "contract":  "合同/订单",
    "macro":     "宏观事件",
    "fed":       "美联储",
    "dividend":  "除权除息",
    "ipo":       "IPO/新股",
    "split":     "拆股/合股",
    "closed":    "休市",
    "other":     "其他",
}

# ── Monitor lifecycle states ──────────────────────────────────────
MONITOR_STATUS = {
    "idle":      "待监控",
    "active":    "监控中",
    "triggered": "到价待操作",
    "bought":    "已买入",
    "skipped":   "已跳过",
}

MONITOR_ACTION_REASONS = {
    "logic_changed":  "逻辑变了",
    "not_interested": "不感兴趣",
    "priority_change":"资金优先给别的",
    "price_adjusted": "调价继续观察",
    "manual":         "手动停止",
}

KB_CATEGORIES = {
    "industry":  "行业研究",
    "framework": "投资框架",
    "macro":     "宏观主题",
    "event":     "事件分析",
    "company":   "个股深析",
    "other":     "其他",
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

        CREATE TABLE IF NOT EXISTS knowledge_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other',
            tags TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            content TEXT DEFAULT '',
            created_at DATE NOT NULL,
            updated_at DATE NOT NULL
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
        "is_paper": "INTEGER DEFAULT 0",
    }
    for col, coldef in new_cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE recommendations ADD COLUMN {col} {coldef}")
    # ── Migrate catalysts: add meta column ──
    cat_existing = {row[1] for row in conn.execute("PRAGMA table_info(catalysts)")}
    if "meta" not in cat_existing:
        conn.execute("ALTER TABLE catalysts ADD COLUMN meta TEXT DEFAULT '{}'")
    # ── Wave config table for rotation radar ──
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS wave_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wave_num INTEGER NOT NULL UNIQUE,
            name TEXT NOT NULL,
            icon TEXT NOT NULL DEFAULT 'bi-arrow-repeat',
            category TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'idle',
            progress_pct INTEGER DEFAULT 0,
            tickers TEXT DEFAULT '[]',
            signal_text TEXT DEFAULT '',
            action_type TEXT DEFAULT 'watch',
            color TEXT NOT NULL DEFAULT '#6366f1',
            sort_order INTEGER DEFAULT 0,
            sc_node_ids TEXT DEFAULT '[]',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Seed initial wave data if empty
    if conn.execute("SELECT COUNT(*) FROM wave_config").fetchone()[0] == 0:
        _seed_waves(conn)
    conn.commit()
    conn.close()


def _seed_waves(conn):
    """Seed the 9 AI super-cycle waves with initial config from hardcoded values."""
    waves = [
        (1, '半导体算力层',       'bi-cpu',      '',                'done',     95,  ['NVDA','ARM','AMD','AVGO','INTC','TSM'],                          'NVDA 今年仅 +12%，半导体 ETF +85%，明显跑输指数——资金已在轮出',                                                                                     'exit',   '#6366f1', 0,  ['gpu','foundry']),
        (2, '存储芯片',           'bi-hdd-stack','',                'active',   70,  ['MU','WDC','STX','SNDK'],                                          'MU Q3 营收 +346% YoY，EPS $25.11，Q4 指引 ~$31。分析师目标 $1,300–$1,550',                                                                      'hold',   '#7c3aed', 1,  ['hbm','dram','nand']),
        (3, '光模块 / 光通信',     'bi-optical-audio','optical',    'early',    30,  ['LITE','COHR','MRVL','AAOI','CIEN','GLW','NOK','MTSI','VECO'],          '800G 产能缺口 40–60% 持续到 2027，InP 激光芯片是真正的供给瓶颈。光模块市场 $260 亿规模',                                                  'enter',  '#0891b2', 2,  ['optchip','optmod']),
        (4, 'AI 算力 & 数据中心',  'bi-building',  '',                'brewing',  20,  ['SMCI','DELL','CRWV','NBIS','IREN','CIFR','WULF','CORZ'],              'AI 专用数据中心建设加速，算力租赁估值重构。部分 Bitcoin 矿机转型 AI 算力',                                                                  'watch',  '#ea580c', 3,  ['aiserver','dcbuild']),
        (5, '网络设备',           'bi-router',    '',                'brewing',  30,  ['ANET','AVGO','MRVL','CSCO'],                                        'ANET 以太网交换机受益 AI 集群互联，高带宽低延迟需求持续增长',                                                                                  'watch',  '#16a34a', 4,  ['switch','intercon']),
        (6, '电源 / 电网 / 温控',  'bi-lightning-charge','power',   'brewing',  35,  ['CEG','VRT','ETN','GEV','SMR','OKLO','VST'],                           'CEG（核电）逻辑硬已持有。VRT 已炒热变高β不追。GB200 机架功耗 120kW 是传统服务器 10 倍，电力是真瓶颈',                               'split',  '#ca8a04', 5,  ['nuclear','fuelcell','grid','cooling']),
        (7, '原材料 / 稀土',      'bi-gem',       'material',        'idle',     5,   ['MP','USAR','UUUU','FCX','AA'],                                      '铜、稀土、铝随 AI 建设量级扩大受益。需等前几波充分兑现后才会轮到',                                                                              'wait',   '#9f7a3a', 6,  ['wafer','gas','resist','target']),
        (8, '太空 / 航天',        'bi-rocket',    'dc_infra',        'brewing',  40,  ['ASTS','RKLB','LUNR','PL','SPCX'],                                   'ASTS/RKLB 已涨 400%+。SPCX（SpaceX IPO）是独立事件驱动。注意：此波有独立逻辑，不完全依附 AI 周期',                                           'wait',   '#7e22ce', 7,  ['satellite','launch']),
        (9, '终局应用层',         'bi-stars',     'ai_app',          'idle',     5,   ['TSLA','PLTR','SYM','SERV'],                                       'AI 基础设施建成后，真正把 AI 变成收入的应用公司。PLTR 已有部分定价',                                                                          'future', '#dc2626', 8,  ['ent_ai','auto','robot']),
    ]
    conn.executemany("""
        INSERT INTO wave_config (wave_num, name, icon, category, status, progress_pct, tickers, signal_text, action_type, color, sort_order, sc_node_ids)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(w[0], w[1], w[2], w[3], w[4], w[5], json.dumps(w[6]), w[7], w[8], w[9], w[10], json.dumps(w[11])) for w in waves])


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
        fw_def = FRAMEWORK_DEFAULTS.get(r["framework"])
        r["fw_stop_label"]   = fw_def["stop_label"]   if fw_def else None
        r["fw_target_label"] = fw_def["target_label"] if fw_def else None

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

        # ── Monitor status lifecycle ──
        db_status = (r.get("monitor_status") or "idle").strip()
        try:
            r["action_log"] = json.loads(r.get("monitor_action_log") or "[]")
        except Exception:
            r["action_log"] = []
        # Terminal states: never auto-override
        if db_status in ("bought", "skipped"):
            r["monitor_status"] = db_status
        elif r.get("monitoring") and r.get("price_alert"):
            r["monitor_status"] = "triggered"
        elif r.get("monitoring"):
            r["monitor_status"] = "active"
        else:
            r["monitor_status"] = "idle"
        r["monitorStatusLabel"] = MONITOR_STATUS.get(r["monitor_status"], r["monitor_status"])

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
        if r["entry_rec"] == "immediate" or r.get("is_bought") or r.get("is_paper"):
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
    # Compute entry_counts for the session
    entry_counts = {}
    grouped_recs = {}
    for r in recs:
        key = (r.get("entry_rec") or "watch")
        entry_counts[key] = entry_counts.get(key, 0) + 1
        grouped_recs.setdefault(key, []).append(r)
    return render_template("session_detail.html", session=dict(session), recs=recs,
                           entry_rec=ENTRY_REC, entry_counts=entry_counts,
                           grouped_recs=grouped_recs, today=str(date.today()))


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
    if bought_filter == "real":
        where += " AND is_bought=1"
    elif bought_filter == "paper":
        where += " AND is_paper=1"
    elif bought_filter == "none":
        where += " AND is_bought=0 AND (is_paper IS NULL OR is_paper=0)"

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
        fw_def = FRAMEWORK_DEFAULTS.get(framework)
        if fw_def and entry_price:
            stop = round(entry_price * fw_def["stop_pct"], 4)
            t1   = round(entry_price * fw_def["target_pct"], 4)
        else:
            stop = float(request.form.get("stop_loss_price") or 0) or None
            t1   = float(request.form.get("target1_price") or 0) or None
        wait_price_raw = request.form.get("wait_price", "")
        conn.execute(
            """UPDATE recommendations SET
               entry_price=?, stop_loss_price=?, target1_price=?,
               entry_rec=?, risk_level=?, hold_period=?, strategy_type=?,
               catalyst=?, wait_price=?, wait_condition=?, validity_date=?,
               conviction=?, notes=?, signals=?
               WHERE id=?""",
            (entry_price, stop, t1,
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

        fw_def = FRAMEWORK_DEFAULTS.get(framework)
        if fw_def and entry_price:
            stop = round(entry_price * fw_def["stop_pct"], 4)
            t1   = round(entry_price * fw_def["target_pct"], 4)
        else:
            stop = float(request.form.get("stop_loss_price") or 0) or None
            t1   = float(request.form.get("target1_price") or 0) or None

        conn = get_db()
        cur = conn.execute(
            """INSERT INTO recommendations
               (is_bought, is_paper, session_id, created_at, ticker, market, framework, entry_price,
                stop_loss_price, target1_price, signals,
                conviction, source_type, notes,
                entry_rec, risk_level, hold_period, strategy_type,
                catalyst, wait_price, wait_condition, validity_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (0, 0, sid, created_at, ticker, market, framework, entry_price,
             stop, t1, json.dumps(signals, ensure_ascii=False),
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
    is_position = row["entry_rec"] == "immediate" or row["is_bought"] or (row["is_paper"] if "is_paper" in row.keys() else 0)
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
    type_ = request.form.get("type", "real")
    conn = get_db()
    row = conn.execute("SELECT is_bought, is_paper, ticker FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    if type_ == "paper":
        new_val = 0 if (row["is_paper"] or 0) else 1
        conn.execute("UPDATE recommendations SET is_paper=? WHERE id=?", (new_val, rec_id))
        flash(("已标记为模拟盘买入" if new_val else "已取消模拟盘") + f" — {row['ticker']}", "success")
    else:
        new_val = 0 if row["is_bought"] else 1
        conn.execute("UPDATE recommendations SET is_bought=? WHERE id=?", (new_val, rec_id))
        flash(("💰 已标记为实盘买入" if new_val else "已取消实盘") + f" — {row['ticker']}", "success")
    conn.commit()
    conn.close()
    return redirect(url_for("detail", rec_id=rec_id))


@app.route("/rec/<int:rec_id>/toggle_monitor", methods=["POST"])
def toggle_monitor(rec_id):
    conn = get_db()
    row = conn.execute("SELECT monitoring, ticker, monitor_status FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    new_val = 0 if row["monitoring"] else 1
    new_status = "active" if new_val else "idle"
    # Don't override terminal states
    if row["monitor_status"] in ("bought", "skipped"):
        new_status = row["monitor_status"]
    conn.execute("UPDATE recommendations SET monitoring=?, monitor_status=? WHERE id=?", (new_val, new_status, rec_id))
    conn.commit()
    conn.close()
    flash(("🔔 已开启价格监控" if new_val else "已关闭监控") + f" — {row['ticker']}", "success")
    return redirect(url_for("detail", rec_id=rec_id))


@app.route("/rec/<int:rec_id>/monitor_action", methods=["POST"])
def monitor_action(rec_id):
    """Handle closed-loop actions on triggered recommendations."""
    action = request.form.get("action", "").strip()
    conn = get_db()
    row = conn.execute(
        "SELECT ticker, monitor_status, monitor_action_log, monitoring, wait_price FROM recommendations WHERE id=?",
        (rec_id,),
    ).fetchone()
    if not row:
        flash("记录不存在", "error")
        return redirect(url_for("recommendations"))

    try:
        action_log = json.loads(row["monitor_action_log"] or "[]")
    except Exception:
        action_log = []

    today_str = str(date.today())

    if action == "bought":
        buy_type = request.form.get("buy_type", "paper")
        buy_price = float(request.form.get("buy_price", 0))
        if buy_type == "real":
            conn.execute("UPDATE recommendations SET is_bought=1, monitoring=0, monitor_status='bought', "
                        "close_price=?, close_date=? WHERE id=?", (buy_price, today_str, rec_id))
        else:
            conn.execute("UPDATE recommendations SET is_paper=1, monitoring=0, monitor_status='bought', "
                        "close_price=?, close_date=? WHERE id=?", (buy_price, today_str, rec_id))
        action_log.append({
            "action": "bought", "date": today_str,
            "buy_type": buy_type, "buy_price": buy_price,
        })
        label = "实盘" if buy_type == "real" else "模拟"
        flash(f"✅ 已记录{label}买入 {row['ticker']} @ ${buy_price:.2f}", "success")

    elif action == "skipped":
        reason_key = request.form.get("reason", "manual")
        reason_label = MONITOR_ACTION_REASONS.get(reason_key, reason_key)
        conn.execute("UPDATE recommendations SET monitoring=0, monitor_status='skipped' WHERE id=?", (rec_id,))
        action_log.append({
            "action": "skipped", "date": today_str,
            "reason": reason_key, "reason_label": reason_label,
        })
        flash(f"⏭️ 已跳过 {row['ticker']} — {reason_label}", "info")

    elif action == "adjust":
        new_wait = float(request.form.get("new_wait_price", row["wait_price"] or 0))
        old_wait = row["wait_price"]
        conn.execute("UPDATE recommendations SET wait_price=?, monitor_status='active' WHERE id=?", (new_wait, rec_id))
        action_log.append({
            "action": "adjust", "date": today_str,
            "old_wait_price": old_wait, "new_wait_price": new_wait,
        })
        flash(f"📝 {row['ticker']} 等待价调整: ${old_wait:.2f} → ${new_wait:.2f}", "warning")

    elif action == "continue":
        conn.execute("UPDATE recommendations SET monitor_status='active' WHERE id=?", (rec_id,))
        action_log.append({"action": "continue", "date": today_str})
        flash(f"▶️ {row['ticker']} 继续观察中", "info")

    else:
        flash("未知操作", "error")
        conn.close()
        return redirect(url_for("detail", rec_id=rec_id))

    conn.execute("UPDATE recommendations SET monitor_action_log=? WHERE id=?",
                 (json.dumps(action_log, ensure_ascii=False), rec_id))
    conn.commit()
    conn.close()
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
                snap_date = parts[1][:10]
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
    fw_def = FRAMEWORK_DEFAULTS.get(framework)
    if fw_def and entry_price:
        stop = round(entry_price * fw_def["stop_pct"], 4)
        t1   = round(entry_price * fw_def["target_pct"], 4)
    else:
        stop = data.get("stop_loss_price")
        t1   = data.get("target1_price")
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO recommendations
           (is_bought, is_paper, session_id, created_at, ticker, market, framework, entry_price,
            stop_loss_price, target1_price, signals,
            conviction, source_type, notes,
            entry_rec, risk_level, hold_period, strategy_type,
            catalyst, wait_price, wait_condition, validity_date)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (data.get("is_bought", 0), data.get("is_paper", 0),
         data.get("session_id"), data.get("created_at", str(date.today())),
         data["ticker"].upper(), data.get("market", "US"),
         framework, entry_price, stop, t1,
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


# ── Catalysts ─────────────────────────────────────────────────────────────────

@app.route("/catalysts")
def catalysts():
    conn  = get_db()
    today = date.today()

    # Latest price per ticker
    prices  = {}
    entries = {}
    for row in conn.execute(
        "SELECT r.ticker, r.entry_price, ps.price "
        "FROM recommendations r "
        "JOIN price_snapshots ps ON ps.recommendation_id=r.id "
        "WHERE ps.snapshot_date=(SELECT MAX(s2.snapshot_date) FROM price_snapshots s2 WHERE s2.recommendation_id=r.id)"
    ).fetchall():
        prices[row["ticker"]]  = row["price"]
        entries[row["ticker"]] = row["entry_price"]

    all_cats = [dict(r) for r in conn.execute(
        "SELECT * FROM catalysts ORDER BY event_date ASC, id DESC"
    ).fetchall()]

    for r in all_cats:
        r["type_label"] = CATALYST_TYPES.get(r["event_type"], r["event_type"])
        try:
            r["days_until"] = (date.fromisoformat(r["event_date"]) - today).days
        except Exception:
            r["days_until"] = None
        try:
            r["meta"] = json.loads(r.get("meta") or "{}")
        except Exception:
            r["meta"] = {}
        tickers_raw = conn.execute(
            "SELECT * FROM catalyst_tickers WHERE catalyst_id=?", (r["id"],)
        ).fetchall()
        r["tickers"] = []
        for t in tickers_raw:
            td = dict(t)
            td["latest_price"] = prices.get(td["ticker"])
            td["entry_price"]  = entries.get(td["ticker"])
            if td["latest_price"] and td["entry_price"]:
                td["pct_from_entry"] = round(
                    (td["latest_price"] - td["entry_price"]) / td["entry_price"] * 100, 1)
            else:
                td["pct_from_entry"] = None
            r["tickers"].append(td)

    # Macro events with parsed review_dates
    macro_events_raw = [dict(r) for r in conn.execute(
        "SELECT * FROM macro_events WHERE status='active' ORDER BY created_at DESC"
    ).fetchall()]
    macro_events = []
    macro_cal_events = []  # flat list for calendar: {date, label, macro_id, color:'macro'}
    for ev in macro_events_raw:
        try:
            tickers_list = json.loads(ev["related_tickers"]) if ev["related_tickers"] else []
        except Exception:
            tickers_list = [t.strip() for t in ev.get("related_tickers","").split(",") if t.strip()]
        ev["tickers_list"] = tickers_list
        dates_parsed = []
        rd_raw = ev.get("review_dates") or ""
        # Try JSON format first (from monitor form)
        try:
            rd_json = json.loads(rd_raw)
            for entry in rd_json:
                d_str = entry.get("date","")[:10]
                label = entry.get("label","")
                try:
                    d = date.fromisoformat(d_str)
                    days_left = (d - today).days
                    dates_parsed.append({"date": d_str, "label": label, "days_left": days_left,
                                         "is_due": 0 <= days_left <= 7, "is_past": days_left < 0})
                    if days_left >= 0:
                        macro_cal_events.append({"date": d_str, "label": label, "macro_id": ev["id"]})
                except Exception:
                    pass
        except Exception:
            # Fall back to line-by-line format: "YYYY-MM-DD 说明"
            for line in rd_raw.splitlines():
                parts = line.strip().split(" ", 1)
                if len(parts) >= 1 and parts[0]:
                    d_str = parts[0][:10]
                    label = parts[1] if len(parts) > 1 else ""
                    try:
                        d = date.fromisoformat(d_str)
                        days_left = (d - today).days
                        dates_parsed.append({"date": d_str, "label": label, "days_left": days_left,
                                             "is_due": 0 <= days_left <= 7, "is_past": days_left < 0})
                        if days_left >= 0:
                            macro_cal_events.append({"date": d_str, "label": label, "macro_id": ev["id"]})
                    except Exception:
                        pass
        ev["dates_parsed"] = sorted(dates_parsed, key=lambda x: x["date"])
        macro_events.append(ev)
    conn.close()

    review_ids   = set()
    needs_review = []
    for r in all_cats:
        if r["status"] == "happened" and not r.get("impact_score"):
            needs_review.append(r)
            review_ids.add(r["id"])

    this_week  = [r for r in all_cats if r["status"] == "upcoming" and r["days_until"] is not None and 0  <= r["days_until"] <= 7]
    this_month = [r for r in all_cats if r["status"] == "upcoming" and r["days_until"] is not None and 8  <= r["days_until"] <= 30]
    later      = [r for r in all_cats if r["status"] == "upcoming" and r["days_until"] is not None and r["days_until"] > 30]
    overdue    = [r for r in all_cats if r["status"] == "upcoming" and r["days_until"] is not None and r["days_until"] < 0]
    past       = [r for r in all_cats
                  if r["status"] in ("happened", "skipped") and r["id"] not in review_ids]

    # Calendar events: catalyst dates (blue) + macro dates (orange)
    cal_catalyst = [{"date": r["event_date"], "title": r["title"], "type": r["event_type"],
                     "cat_id": r["id"]} for r in all_cats if r["status"] == "upcoming"]

    return render_template("catalysts.html",
                           needs_review=needs_review, this_week=this_week,
                           this_month=this_month, later=later,
                           overdue=overdue, past=past,
                           macro_events=macro_events,
                           cal_catalyst=cal_catalyst,
                           macro_cal_events=macro_cal_events,
                           catalyst_types=CATALYST_TYPES,
                           today=str(today))


@app.route("/catalysts/add", methods=["GET", "POST"])
def catalyst_add():
    if request.method == "POST":
        title      = request.form.get("title", "").strip()
        event_type = request.form.get("event_type", "other")
        event_date = request.form.get("event_date", str(date.today()))
        description= request.form.get("description", "").strip()
        tickers_raw= request.form.get("tickers", "").strip()
        today_str  = str(date.today())
        if not title:
            flash("标题不能为空", "warning")
            return redirect(url_for("catalyst_add"))
        # ── Build type-specific meta ──
        meta = {}
        if event_type == "dividend":
            meta["ex_div_date"]  = request.form.get("ex_div_date", "").strip()
            meta["pay_date"]     = request.form.get("pay_date", "").strip()
            meta["amount"]       = request.form.get("div_amount", "").strip()
        elif event_type == "ipo":
            meta["book_start"]   = request.form.get("book_start", "").strip()
            meta["list_date"]    = request.form.get("list_date", "").strip()
            meta["price_range"]  = request.form.get("price_range", "").strip()
        elif event_type == "split":
            meta["ratio"]        = request.form.get("split_ratio", "").strip()
            meta["ex_date"]      = request.form.get("split_ex_date", "").strip()
        elif event_type == "closed":
            meta["reason"]       = request.form.get("close_reason", "").strip()
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO catalysts (title, event_type, event_date, description, status, created_at, updated_at, meta) "
            "VALUES (?,?,?,?,'upcoming',?,?,?)",
            (title, event_type, event_date, description, today_str, today_str, json.dumps(meta, ensure_ascii=False))
        )
        cat_id = cur.lastrowid
        for t in [x.strip().upper() for x in tickers_raw.split(",") if x.strip()]:
            conn.execute(
                "INSERT INTO catalyst_tickers (catalyst_id, ticker) VALUES (?,?)",
                (cat_id, t)
            )
        conn.commit()
        conn.close()
        flash("催化剂已添加", "success")
        return redirect(url_for("catalyst_detail", cat_id=cat_id))
    return render_template("catalyst_add.html",
                           catalyst_types=CATALYST_TYPES,
                           today=str(date.today()))


@app.route("/catalysts/<int:cat_id>")
def catalyst_detail(cat_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM catalysts WHERE id=?", (cat_id,)).fetchone()
    if not row:
        conn.close()
        flash("催化剂不存在", "danger")
        return redirect(url_for("catalysts"))
    row = dict(row)
    row["type_label"] = CATALYST_TYPES.get(row["event_type"], row["event_type"])
    try:
        row["meta"] = json.loads(row.get("meta") or "{}")
    except Exception:
        row["meta"] = {}
    tickers = [dict(t) for t in conn.execute(
        "SELECT * FROM catalyst_tickers WHERE catalyst_id=?", (cat_id,)
    ).fetchall()]
    # Attach latest price from price_snapshots via recommendations
    for t in tickers:
        snap = conn.execute(
            "SELECT ps.price FROM price_snapshots ps "
            "JOIN recommendations r ON r.id=ps.recommendation_id "
            "WHERE r.ticker=? ORDER BY ps.snapshot_date DESC LIMIT 1",
            (t["ticker"],)
        ).fetchone()
        t["latest_price"] = snap["price"] if snap else None
    conn.close()
    return render_template("catalyst_detail.html", row=row, tickers=tickers)


@app.route("/catalysts/<int:cat_id>/status", methods=["POST"])
def catalyst_update_status(cat_id):
    status       = request.form.get("status", "upcoming")
    impact_score = request.form.get("impact_score") or None
    impact_notes = request.form.get("impact_notes", "").strip()
    conn = get_db()
    conn.execute(
        "UPDATE catalysts SET status=?, impact_score=?, impact_notes=?, updated_at=? WHERE id=?",
        (status, impact_score, impact_notes, str(date.today()), cat_id)
    )
    # If marking as happened, try to fill in price_before / price_after
    if status == "happened":
        row = conn.execute("SELECT event_date FROM catalysts WHERE id=?", (cat_id,)).fetchone()
        if row:
            ev_date = row["event_date"]
            tickers = conn.execute(
                "SELECT ct.id, ct.ticker FROM catalyst_tickers ct WHERE ct.catalyst_id=?", (cat_id,)
            ).fetchall()
            for t in tickers:
                before = conn.execute(
                    "SELECT ps.price FROM price_snapshots ps "
                    "JOIN recommendations r ON r.id=ps.recommendation_id "
                    "WHERE r.ticker=? AND ps.snapshot_date < ? ORDER BY ps.snapshot_date DESC LIMIT 1",
                    (t["ticker"], ev_date)
                ).fetchone()
                after = conn.execute(
                    "SELECT ps.price FROM price_snapshots ps "
                    "JOIN recommendations r ON r.id=ps.recommendation_id "
                    "WHERE r.ticker=? AND ps.snapshot_date >= ? ORDER BY ps.snapshot_date ASC LIMIT 1",
                    (t["ticker"], ev_date)
                ).fetchone()
                pb = before["price"] if before else None
                pa = after["price"]  if after  else None
                pct = round((pa - pb) / pb * 100, 2) if pb and pa else None
                conn.execute(
                    "UPDATE catalyst_tickers SET price_before=?, price_after=?, pct_change=? WHERE id=?",
                    (pb, pa, pct, t["id"])
                )
    conn.commit()
    conn.close()
    flash("状态已更新", "success")
    return redirect(url_for("catalyst_detail", cat_id=cat_id))


@app.route("/catalysts/<int:cat_id>/delete", methods=["POST"])
def catalyst_delete(cat_id):
    conn = get_db()
    conn.execute("DELETE FROM catalyst_tickers WHERE catalyst_id=?", (cat_id,))
    conn.execute("DELETE FROM catalysts WHERE id=?", (cat_id,))
    conn.commit()
    conn.close()
    flash("已删除", "success")
    return redirect(url_for("catalysts"))


@app.route("/catalyst_ticker/delete", methods=["POST"])
def catalyst_ticker_delete():
    ct_id  = request.form.get("ct_id")
    cat_id = request.form.get("cat_id")
    conn = get_db()
    conn.execute("DELETE FROM catalyst_tickers WHERE id=?", (ct_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("catalyst_detail", cat_id=cat_id))


@app.route("/catalysts/<int:cat_id>/quick_happened", methods=["POST"])
def catalyst_quick_happened(cat_id):
    conn = get_db()
    conn.execute(
        "UPDATE catalysts SET status='happened', updated_at=? WHERE id=?",
        (str(date.today()), cat_id)
    )
    # Auto-fill price_before / price_after
    row = conn.execute("SELECT event_date FROM catalysts WHERE id=?", (cat_id,)).fetchone()
    if row:
        ev_date = row["event_date"]
        for t in conn.execute("SELECT id, ticker FROM catalyst_tickers WHERE catalyst_id=?", (cat_id,)).fetchall():
            before = conn.execute(
                "SELECT ps.price FROM price_snapshots ps JOIN recommendations r ON r.id=ps.recommendation_id "
                "WHERE r.ticker=? AND ps.snapshot_date < ? ORDER BY ps.snapshot_date DESC LIMIT 1",
                (t["ticker"], ev_date)
            ).fetchone()
            after = conn.execute(
                "SELECT ps.price FROM price_snapshots ps JOIN recommendations r ON r.id=ps.recommendation_id "
                "WHERE r.ticker=? AND ps.snapshot_date >= ? ORDER BY ps.snapshot_date ASC LIMIT 1",
                (t["ticker"], ev_date)
            ).fetchone()
            pb = before["price"] if before else None
            pa = after["price"]  if after  else None
            pct = round((pa - pb) / pb * 100, 2) if pb and pa else None
            conn.execute("UPDATE catalyst_tickers SET price_before=?, price_after=?, pct_change=? WHERE id=?",
                         (pb, pa, pct, t["id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/sync_earnings", methods=["POST"])
def sync_earnings():
    conn = get_db()
    # Active tickers only
    recs = conn.execute(
        "SELECT DISTINCT ticker FROM recommendations WHERE status='active'"
    ).fetchall()

    today_str = str(date.today())
    end_str   = str(date.today() + timedelta(days=180))
    added = 0

    for rec in recs:
        raw_ticker = rec["ticker"]
        # Normalize to Longbridge symbol format (TICKER.US)
        if "." in raw_ticker:
            lb_sym = raw_ticker
        else:
            lb_sym = raw_ticker + ".US"
        # Skip non-US and ETFs that won't have earnings
        if lb_sym.endswith(".HK") or lb_sym.endswith(".CN"):
            continue

        try:
            result = subprocess.run(
                ["longbridge", "finance-calendar", "report",
                 "--symbol", lb_sym, "--start", today_str, "--end", end_str, "--format", "json"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0 or not result.stdout.strip():
                continue
            data = json.loads(result.stdout)
            for item in data.get("list", []):
                for info in item.get("infos", []):
                    ev_date = item.get("date", "")[:10]
                    if not ev_date:
                        continue
                    ext = info.get("ext", {}).get("financial_report", {})
                    period = ext.get("period", "")
                    fy     = ext.get("fiscal_year", "")
                    title  = f"{raw_ticker} Q{period} FY{fy} 财报"
                    # Check duplicate
                    existing = conn.execute(
                        "SELECT id FROM catalysts WHERE event_date=? AND event_type='earnings' "
                        "AND id IN (SELECT catalyst_id FROM catalyst_tickers WHERE ticker=?)",
                        (ev_date, raw_ticker)
                    ).fetchone()
                    if existing:
                        continue
                    cur = conn.execute(
                        "INSERT INTO catalysts (title, event_type, event_date, description, status, created_at, updated_at) "
                        "VALUES (?,?,?,?,'upcoming',?,?)",
                        (title, "earnings", ev_date,
                         info.get("content", ""), today_str, today_str)
                    )
                    conn.execute(
                        "INSERT INTO catalyst_tickers (catalyst_id, ticker) VALUES (?,?)",
                        (cur.lastrowid, raw_ticker)
                    )
                    added += 1
        except Exception:
            continue

    conn.commit()
    conn.close()
    return jsonify({"added": added})


# ── Rotation Radar ────────────────────────────────────────────────────────────

@app.route("/rotation")
def rotation():
    conn = get_db()
    # 1) Fetch all waves
    rows = conn.execute("SELECT * FROM wave_config ORDER BY sort_order").fetchall()
    waves = []
    for r in rows:
        w = dict(r)
        w['tickers'] = json.loads(w['tickers']) if w['tickers'] else []
        w['sc_node_ids'] = json.loads(w['sc_node_ids']) if w['sc_node_ids'] else []

        # 2) Enrich each ticker with live recommendation data
        enriched_tickers = []
        for t in w['tickers']:
            rec = conn.execute("""
                SELECT r.id, r.ticker, r.entry_price, r.status, r.monitoring,
                       r.monitor_status, r.is_bought, r.close_price, r.session_id,
                       s.price as latest_price, s.snapshot_date
                FROM recommendations r
                LEFT JOIN price_snapshots s ON s.recommendation_id = r.id
                WHERE r.ticker LIKE ? AND r.status != 'closed'
                ORDER BY s.snapshot_date DESC LIMIT 1
            """, (f"{t}.US",)).fetchone()
            info = {'symbol': t, 'tracked': False}
            if rec:
                price = rec['latest_price'] or rec['close_price']
                info.update({
                    'tracked': True,
                    'rec_id': rec['id'],
                    'entry_price': rec['entry_price'],
                    'current_price': price,
                    'pnl_pct': calc_pct(rec['entry_price'], price),
                    'monitoring': bool(rec['monitoring']),
                    'monitor_status': rec['monitor_status'] or 'idle',
                    'is_bought': bool(rec['is_bought']),
                    'session_id': rec['session_id'],
                })
            enriched_tickers.append(info)
        w['enriched_tickers'] = enriched_tickers

        # 3) Fetch catalyst events for this wave's tickers
        if w['tickers']:
            ph = ','.join(['?'] * len(w['tickers']))
            cat_rows = conn.execute(f"""
                SELECT c.id, c.event_type, c.event_date, c.title, c.description,
                       GROUP_CONCAT(ct.ticker) as tickers
                FROM catalysts c
                LEFT JOIN catalyst_tickers ct ON c.id = ct.catalyst_id
                WHERE c.event_date >= date('now', '-30 days')
                  AND EXISTS (
                      SELECT 1 FROM catalyst_tickers ct2
                      WHERE ct2.catalyst_id = c.id
                        AND ct2.ticker IN ({ph})
                  )
                GROUP BY c.id ORDER BY c.event_date ASC
            """, w['tickers']).fetchall()
        else:
            cat_rows = []
        w['catalysts'] = [dict(c) for c in cat_rows]

        waves.append(w)
    conn.close()

    # 4) Overall progress summary
    total_progress = sum(w['progress_pct'] for w in waves) // len(waves) if waves else 0
    active_wave = next((w for w in waves if w['status'] in ('active','early')), None)

    return render_template("rotation.html",
                           waves=waves,
                           total_progress=total_progress,
                           active_wave=active_wave)


# ── Supply Chain ──────────────────────────────────────────────────────────────

@app.route("/supply-chain")
def supply_chain():
    return render_template("supply_chain.html")


# ── Monitor ───────────────────────────────────────────────────────────────────

@app.route("/monitor")
def monitor():
    conn = get_db()
    today = date.today()

    # Macro events
    macro_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM macro_events WHERE status='active' ORDER BY created_at DESC"
    ).fetchall()]
    for ev in macro_rows:
        try:
            ev["related_tickers_parsed"] = [t.strip() for t in json.loads(ev["related_tickers"]) if t.strip()] if ev["related_tickers"] else []
        except Exception:
            ev["related_tickers_parsed"] = [t.strip() for t in ev["related_tickers"].split(",") if t.strip()]
        dates_parsed = []
        for line in (ev["review_dates"] or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) < 1:
                continue
            d_str = parts[0]
            label = parts[1] if len(parts) > 1 else ""
            try:
                d = date.fromisoformat(d_str)
                days_left = (d - today).days
                dates_parsed.append({
                    "date": d_str, "label": label,
                    "is_due": 0 <= days_left <= 7,
                    "is_past": days_left < 0,
                    "days_left": days_left,
                })
            except Exception:
                continue
        ev["review_dates_parsed"] = sorted(dates_parsed, key=lambda x: x["date"])

    # Recommendations with wait_price
    recs = conn.execute(
        "SELECT * FROM recommendations WHERE wait_price IS NOT NULL AND wait_price > 0 AND status='active' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    recs = enrich_rows(recs)

    triggered, approaching, watching, expired = [], [], [], []
    for r in recs:
        lp = r.get("latest_price")
        wp = r.get("wait_price")
        if not lp or not wp:
            watching.append(r)
            continue
        gap_pct = abs((lp - wp) / wp * 100)
        # Check validity expiry
        vd = r.get("validity_date")
        if vd:
            try:
                days_left = (date.fromisoformat(vd) - today).days
                r["days_until_expiry"] = days_left
                r["is_expired_validity"] = days_left < 0
                r["is_expiring_soon"] = 0 <= days_left <= 7
            except Exception:
                r["is_expired_validity"] = False
                r["is_expiring_soon"] = False
        else:
            r["is_expired_validity"] = False
            r["is_expiring_soon"] = False

        if r["is_expired_validity"]:
            expired.append(r)
        elif gap_pct <= 3:
            triggered.append(r)
        elif gap_pct <= 10:
            approaching.append(r)
        else:
            watching.append(r)

    all_count = len(triggered) + len(approaching) + len(watching)

    # Catalyst events for Tab 3
    cat_conn = get_db()
    selected_status = request.args.get("cat_status", "")
    selected_type = request.args.get("type", "")
    cat_query = "SELECT c.*, GROUP_CONCAT(ct.ticker) as tickers FROM catalysts c LEFT JOIN catalyst_tickers ct ON c.id = ct.catalyst_id"
    cat_wheres = []
    cat_params = []
    if selected_status:
        cat_wheres.append("c.status = ?")
        cat_params.append(selected_status)
    if selected_type:
        cat_wheres.append("c.event_type = ?")
        cat_params.append(selected_type)
    if cat_wheres:
        cat_query += " WHERE " + " AND ".join(cat_wheres)
    cat_query += " GROUP BY c.id ORDER BY c.event_date ASC"
    catalyst_rows = [dict(r) for r in cat_conn.execute(cat_query, cat_params).fetchall()]
    for cr in catalyst_rows:
        cr["tickers"] = cr["tickers"].split(",") if cr["tickers"] else []
    catalyst_count = cat_conn.execute("SELECT COUNT(*) FROM catalysts").fetchone()[0]
    cat_conn.close()

    return render_template("monitor.html",
                           triggered=triggered, approaching=approaching,
                           watching=watching, expired=expired,
                           macro_events=macro_rows, all_count=all_count,
                           monitor_action_reasons=MONITOR_ACTION_REASONS,
                           catalyst_types=CATALYST_TYPES,
                           catalyst_rows=catalyst_rows,
                           catalyst_count=catalyst_count,
                           selected_status=selected_status,
                           selected_type=selected_type)


@app.route("/macro/add", methods=["POST"])
def macro_add():
    title   = request.form.get("title", "").strip()
    thesis  = request.form.get("thesis", "").strip()
    tickers = request.form.get("related_tickers", "").strip()
    dates   = request.form.get("review_dates", "").strip()
    if not title:
        flash("标题不能为空", "warning")
        return redirect(url_for("monitor"))
    today_str = str(date.today())
    conn = get_db()
    conn.execute(
        "INSERT INTO macro_events (title, thesis, related_tickers, review_dates, status, created_at, updated_at) "
        "VALUES (?,?,?,?,'active',?,?)",
        (title, thesis, tickers, dates, today_str, today_str)
    )
    conn.commit()
    conn.close()
    flash("宏观事件已添加", "success")
    return redirect(url_for("monitor"))


@app.route("/macro/<int:ev_id>/close", methods=["POST"])
def macro_close(ev_id):
    conn = get_db()
    conn.execute("UPDATE macro_events SET status='closed', updated_at=? WHERE id=?",
                 (str(date.today()), ev_id))
    conn.commit()
    conn.close()
    flash("宏观事件已关闭", "success")
    return redirect(url_for("monitor"))


# ── Knowledge Base ────────────────────────────────────────────────────────────

@app.route("/knowledge")
def knowledge():
    conn = get_db()
    cat = request.args.get("category", "")
    q   = request.args.get("q", "").strip()
    sql  = "SELECT * FROM knowledge_articles"
    args = []
    where = []
    if cat:
        where.append("category=?"); args.append(cat)
    if q:
        where.append("(title LIKE ? OR tags LIKE ? OR summary LIKE ? OR content LIKE ?)")
        args += [f"%{q}%"] * 4
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC"
    rows = conn.execute(sql, args).fetchall()
    counts = {r["category"]: r["n"] for r in conn.execute(
        "SELECT category, COUNT(*) n FROM knowledge_articles GROUP BY category"
    )}
    conn.close()
    return render_template("knowledge.html",
                           rows=rows, kb_categories=KB_CATEGORIES,
                           selected_cat=cat, q=q, counts=counts)


@app.route("/knowledge/<int:art_id>")
def knowledge_detail(art_id):
    conn = get_db()
    art = conn.execute("SELECT * FROM knowledge_articles WHERE id=?", (art_id,)).fetchone()
    conn.close()
    if not art:
        flash("文章不存在", "danger"); return redirect(url_for("knowledge"))
    return render_template("knowledge_detail.html", art=art, kb_categories=KB_CATEGORIES)


@app.route("/knowledge/add", methods=["GET", "POST"])
def knowledge_add():
    if request.method == "POST":
        title    = request.form["title"].strip()
        category = request.form.get("category", "other")
        tags     = request.form.get("tags", "").strip()
        summary  = request.form.get("summary", "").strip()
        content  = request.form.get("content", "").strip()
        today    = str(date.today())
        if not title:
            flash("标题不能为空", "danger")
        else:
            conn = get_db()
            cur = conn.execute(
                "INSERT INTO knowledge_articles (title,category,tags,summary,content,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (title, category, tags, summary, content, today, today)
            )
            art_id = cur.lastrowid
            conn.commit(); conn.close()
            flash(f"已保存《{title}》", "success")
            return redirect(url_for("knowledge_detail", art_id=art_id))
    return render_template("knowledge_add.html", kb_categories=KB_CATEGORIES, art=None)


@app.route("/knowledge/<int:art_id>/edit", methods=["GET", "POST"])
def knowledge_edit(art_id):
    conn = get_db()
    art = conn.execute("SELECT * FROM knowledge_articles WHERE id=?", (art_id,)).fetchone()
    if not art:
        conn.close(); flash("文章不存在", "danger"); return redirect(url_for("knowledge"))
    if request.method == "POST":
        title    = request.form["title"].strip()
        category = request.form.get("category", "other")
        tags     = request.form.get("tags", "").strip()
        summary  = request.form.get("summary", "").strip()
        content  = request.form.get("content", "").strip()
        if not title:
            flash("标题不能为空", "danger")
        else:
            conn.execute(
                "UPDATE knowledge_articles SET title=?,category=?,tags=?,summary=?,content=?,updated_at=? WHERE id=?",
                (title, category, tags, summary, content, str(date.today()), art_id)
            )
            conn.commit(); conn.close()
            flash("已更新", "success")
            return redirect(url_for("knowledge_detail", art_id=art_id))
    conn.close()
    return render_template("knowledge_add.html", kb_categories=KB_CATEGORIES, art=art)


@app.route("/knowledge/<int:art_id>/delete", methods=["POST"])
def knowledge_delete(art_id):
    conn = get_db()
    art = conn.execute("SELECT title FROM knowledge_articles WHERE id=?", (art_id,)).fetchone()
    if art:
        conn.execute("DELETE FROM knowledge_articles WHERE id=?", (art_id,))
        conn.commit()
        flash(f"已删除《{art['title']}》", "success")
    conn.close()
    return redirect(url_for("knowledge"))


# ── Notifications ─────────────────────────────────────────────────────────────

@app.route("/api/notifications")
def api_notifications():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM notifications ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/notifications/count")
def api_notifications_count():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
    conn.close()
    return jsonify({"count": count})


@app.route("/api/notifications/mark_read", methods=["POST"])
def api_notifications_mark_read():
    data = request.get_json(silent=True) or {}
    ids  = data.get("ids", [])
    conn = get_db()
    if ids:
        conn.execute(
            "UPDATE notifications SET is_read=1 WHERE id IN ({})".format(",".join("?" * len(ids))),
            ids
        )
    else:
        conn.execute("UPDATE notifications SET is_read=1")
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Market Calendar ────────────────────────────────────────────────────────────

@app.route("/api/market_calendar")
def market_calendar():
    """Return closed dates, dividends, IPOs and splits for the calendar widget."""
    start_str = str(date.today())
    end_str   = str(date.today() + timedelta(days=180))

    conn = get_db()
    tickers_raw = [r["ticker"] for r in conn.execute(
        "SELECT DISTINCT ticker FROM recommendations WHERE status='active'"
    ).fetchall()]
    conn.close()

    us_tickers = []
    for t in tickers_raw:
        sym = t if "." in t else t + ".US"
        if sym.endswith(".US"):
            us_tickers.append(sym)

    def _lb_json(cmd_args, timeout=12):
        try:
            r = subprocess.run(
                ["longbridge", "finance-calendar"] + cmd_args + ["--format", "json"],
                capture_output=True, text=True, timeout=timeout
            )
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
        return {"list": []}

    # Closed dates
    closed = {}
    data = _lb_json(["closed", "--market", "US", "--start", start_str, "--end", end_str])
    for item in data.get("list", []):
        for info in item.get("infos", []):
            closed[item["date"]] = info.get("content", "休市")

    # Dividends for active US tickers (batched)
    dividends = []
    for i in range(0, len(us_tickers), 10):
        batch = us_tickers[i:i + 10]
        args  = ["dividend", "--start", start_str, "--end", end_str]
        for sym in batch:
            args += ["--symbol", sym]
        data = _lb_json(args)
        for item in data.get("list", []):
            for info in item.get("infos", []):
                raw_id = info.get("counter_id", "")
                dividends.append({
                    "date":   item["date"],
                    "ticker": raw_id.split("/")[-1] if raw_id else "",
                    "amount": info.get("content", ""),
                })

    # IPOs
    ipos = []
    data = _lb_json(["ipo", "--market", "US", "--start", start_str, "--end", end_str])
    for item in data.get("list", []):
        for info in item.get("infos", []):
            ipos.append({
                "date":    item["date"],
                "ticker":  info.get("counter_id", "").split("/")[-1],
                "name":    info.get("counter_name", ""),
                "content": info.get("content", ""),
            })

    # Splits for active US tickers
    splits = []
    for i in range(0, len(us_tickers), 10):
        batch = us_tickers[i:i + 10]
        args  = ["split", "--start", start_str, "--end", end_str]
        for sym in batch:
            args += ["--symbol", sym]
        data = _lb_json(args)
        for item in data.get("list", []):
            for info in item.get("infos", []):
                splits.append({
                    "date":    item["date"],
                    "ticker":  info.get("counter_id", "").split("/")[-1],
                    "content": info.get("content", ""),
                })

    return jsonify({"closed": closed, "dividends": dividends, "ipos": ipos, "splits": splits})


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5050))
    print(f"投资追踪系统启动：http://127.0.0.1:{port}")
    app.run(debug=True, port=port)
