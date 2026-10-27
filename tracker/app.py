from flask import Flask, Response, render_template, request, redirect, url_for, flash, jsonify
import sqlite3
import json
from datetime import date, datetime, timedelta
from hmac import compare_digest
import os
import threading
import time
from collections.abc import Mapping
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFProtect

from longbridge_api import LongbridgeClient, LongbridgeNotConfigured

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tracker-2026")
csrf = CSRFProtect(app)
API_KEY = os.environ.get("API_KEY")
API_WRITE_ENDPOINTS = {"api_add_session", "api_add_rec"}


@app.before_request
def require_login():
    """Protect personal investment data when the app is publicly deployed."""
    if request.endpoint in {"health", "static"}:
        return None
    if request.endpoint in {"api_agent_events_scan", "api_agent_morning_report"}:
        supplied_key = request.headers.get("X-API-Key", "")
        if supplied_key:
            if not API_KEY or not compare_digest(supplied_key, API_KEY):
                return jsonify({"error": "Unauthorized"}), 401
            return None
        csrf.protect()
    if request.endpoint in API_WRITE_ENDPOINTS:
        if not API_KEY:
            return jsonify({"error": "API is not configured"}), 503
        supplied_key = request.headers.get("X-API-Key", "")
        if not compare_digest(supplied_key, API_KEY):
            return jsonify({"error": "Unauthorized"}), 401

    username = os.environ.get("APP_USERNAME")
    password = os.environ.get("APP_PASSWORD")
    if not username and not password:
        if os.environ.get("VERCEL"):
            return jsonify({"error": "APP_USERNAME and APP_PASSWORD are required"}), 503
        return None
    if not username or not password:
        return jsonify({"error": "Incomplete login configuration"}), 503

    auth = request.authorization
    valid = (
        auth
        and compare_digest(auth.username or "", username)
        and compare_digest(auth.password or "", password)
    )
    if valid:
        return None
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="Investment Tracker"'},
    )


@app.route("/health")
def health():
    return jsonify({"ok": True})


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


class _MappingRow(Mapping):
    """Provide sqlite3.Row-style name and index access for libSQL rows."""

    def __init__(self, columns, values):
        self._columns = tuple(columns)
        self._values = tuple(values)
        self._index = {name: i for i, name in enumerate(self._columns)}

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._index[key]]

    def __iter__(self):
        return iter(self._columns)

    def __len__(self):
        return len(self._columns)

    def keys(self):
        return self._columns


class _LibsqlCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def _columns(self):
        return [column[0] for column in (self._cursor.description or [])]

    def _row(self, value):
        return None if value is None else _MappingRow(self._columns, value)

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _LibsqlConnection:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, parameters=()):
        return _LibsqlCursor(self._connection.execute(sql, parameters))

    def executemany(self, sql, parameters):
        return _LibsqlCursor(self._connection.executemany(sql, parameters))

    def executescript(self, script):
        return _LibsqlCursor(self._connection.executescript(script))

    def __getattr__(self, name):
        return getattr(self._connection, name)

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
    "actionable": ("可入",     "entry-actionable"),
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

LIFECYCLE_LABEL = {
    "holding": ("持有中", "lifecycle-holding"),
    "sold": ("历史持仓", "lifecycle-sold"),
    "watching": ("自选", "lifecycle-watching"),
    "archived": ("已归档", "lifecycle-archived"),
}

# Longbridge used the temporary SKHYV code on the first trading day.  Treat
# both codes as one security throughout the product lifecycle.
SYMBOL_ALIASES = {"SKHYV.US": "SKHY.US"}

PRIMARY_THEMES = (
    "宽基指数",
    "半导体",
    "光通信与网络",
    "AI数据中心",
    "电力与公用事业",
    "航空航天与国防",
    "机器人与自动化",
    "加密资产",
    "金融",
    "互联网与消费",
    "资源与材料",
)

TICKER_THEME_OVERRIDES = {
    "QQQM.US": "宽基指数", "VOO.US": "宽基指数", "SQQQ.US": "宽基指数",
    "NVDA.US": "半导体", "SKHY.US": "半导体", "MU.US": "半导体",
    "AMKR.US": "半导体", "PLAB.US": "半导体", "VECO.US": "半导体",
    "AMAT.US": "半导体", "ASML.US": "半导体", "ACLS.US": "半导体",
    "FORM.US": "半导体", "ENTG.US": "半导体",
    "COHR.US": "光通信与网络", "LITE.US": "光通信与网络", "FN.US": "光通信与网络",
    "MTSI.US": "光通信与网络", "MRVL.US": "光通信与网络",
    "CEG.US": "电力与公用事业", "BE.US": "电力与公用事业", "PWR.US": "电力与公用事业",
    "ETN.US": "电力与公用事业", "GEV.US": "电力与公用事业",
    "IREN.US": "AI数据中心", "GOOG.US": "AI数据中心", "VRT.US": "AI数据中心",
    "RKLB.US": "航空航天与国防", "ASTS.US": "航空航天与国防", "SATS.US": "航空航天与国防",
    "IRDM.US": "航空航天与国防", "LUNR.US": "航空航天与国防", "SPCX.US": "航空航天与国防",
    "AVAV.US": "航空航天与国防", "ONDS.US": "航空航天与国防", "JOBY.US": "航空航天与国防",
    "TSLA.US": "机器人与自动化",
    "COIN.US": "加密资产", "HOOD.US": "加密资产",
    "NVA.US": "资源与材料", "BRK.B.US": "金融", "BABA.US": "互联网与消费",
}

THEME_ROTATION_CONFIG = {
    "宽基指数": {"proxy": "QQQ.US", "proxy_name": "QQQ"},
    "半导体": {"proxy": "SMH.US", "proxy_name": "SMH"},
    "光通信与网络": {"proxy": "XLC.US", "proxy_name": "XLC"},
    "AI数据中心": {"proxy": "XLK.US", "proxy_name": "XLK"},
    "电力与公用事业": {"proxy": "XLU.US", "proxy_name": "XLU"},
    "航空航天与国防": {"proxy": "XLI.US", "proxy_name": "XLI"},
    "机器人与自动化": {"proxy": "XLK.US", "proxy_name": "XLK"},
    "加密资产": {"proxy": "IBIT.US", "proxy_name": "IBIT"},
    "金融": {"proxy": "XLF.US", "proxy_name": "XLF"},
    "互联网与消费": {"proxy": "XLY.US", "proxy_name": "XLY"},
    "资源与材料": {"proxy": "XLB.US", "proxy_name": "XLB"},
}


def primary_theme(rec):
    """Collapse free-form tags into one small, mutually-exclusive theme."""
    text = " ".join(
        str(rec.get(key) or "")
        for key in ("theme", "session_theme", "catalyst", "notes")
    ).lower()
    ticker = normalize_symbol(rec.get("ticker"), rec.get("market"))
    if ticker in TICKER_THEME_OVERRIDES:
        return TICKER_THEME_OVERRIDES[ticker]
    for theme in PRIMARY_THEMES:
        if theme.lower() in text:
            return theme
    if "指数" in text or ticker == "QQQM.US":
        return "宽基指数"
    if any(key in text for key in ("商业航天", "spacex", "starlink", "卫星")):
        return "航空航天与国防"
    if any(key in text for key in ("加密", "crypto")):
        return "加密资产"
    if any(key in text for key in ("机器人", "无人机", "evtol", "低空")):
        return "机器人与自动化"
    if any(key in text for key in ("光通信", "光互联", "光模块", "cpo", "光子", "inp", "外延片")):
        return "光通信与网络"
    if any(key in text for key in ("电力", "核电", "电网", "能源", "sofc", "hvdc")):
        return "电力与公用事业"
    if any(key in text for key in ("半导体", "存储", "hbm", "芯片", "晶圆", "探针卡", "离子注入", "先进制程")):
        return "半导体"
    if any(key in text for key in ("ai基础设施", "ai基建", "数据中心", "卖铲人")):
        return "AI数据中心"
    if any(key in text for key in ("资源", "材料", "矿业", "金属", "煤炭")):
        return "资源与材料"
    if any(key in text for key in ("金融", "银行", "保险", "券商")):
        return "金融"
    if any(key in text for key in ("互联网", "消费", "电商", "零售")):
        return "互联网与消费"
    return "互联网与消费"


def normalize_symbol(ticker, market=""):
    """Convert tracker and broker symbols to one canonical representation."""
    value = str(ticker or "").strip().upper()
    if not value:
        return ""
    if "." in value:
        symbol = value
    else:
        market = str(market or "US").strip().upper()
        if market == "CN":
            market = "SH" if value.startswith(("5", "6", "9")) else "SZ"
        symbol = f"{value}.{market}"
    return SYMBOL_ALIASES.get(symbol, symbol)


def display_symbol(symbol):
    return str(symbol or "").rsplit(".", 1)[0]

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
    "dividend":  "分红除息",
    "split":     "拆股/合股",
    "ipo":       "IPO",
    "closed":    "休市",
    "policy":    "政策/法规",
    "product":   "产品发布",
    "contract":  "合同/订单",
    "macro":     "宏观事件",
    "fed":       "美联储",
    "other":     "其他",
}

MONITOR_ACTION_REASONS = {
    "logic_changed":  "逻辑已改变",
    "too_expensive":  "价格太贵",
    "better_opp":     "有更好机会",
    "risk_too_high":  "风险过高",
    "price_adjusted": "调整目标价",
    "manual":         "其他原因",
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
    test_db_path = os.environ.get("TEST_DATABASE_PATH")
    turso_url = None if test_db_path else os.environ.get("TURSO_DATABASE_URL")
    if turso_url:
        from service_support import configure_tls_trust
        configure_tls_trust()
        import libsql

        token = os.environ.get("TURSO_AUTH_TOKEN")
        if not token:
            raise RuntimeError("TURSO_AUTH_TOKEN is required with TURSO_DATABASE_URL")
        conn = _LibsqlConnection(
            libsql.connect(database=turso_url, auth_token=token)
        )
    else:
        conn = sqlite3.connect(test_db_path or DB_PATH)
        conn.row_factory = sqlite3.Row
        # Web + Worker write the same local DB; WAL + busy_timeout avoid
        # "database is locked" under concurrent read/write.
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
    conn.execute("PRAGMA foreign_keys = ON")
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

        CREATE TABLE IF NOT EXISTS catalysts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_date DATE NOT NULL,
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            impact_score REAL,
            impact_notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS catalyst_tickers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            catalyst_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            price_before REAL,
            price_after REAL,
            pct_change REAL,
            FOREIGN KEY (catalyst_id) REFERENCES catalysts(id)
        );

        CREATE TABLE IF NOT EXISTS macro_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            thesis TEXT DEFAULT '',
            related_tickers TEXT DEFAULT '',
            review_dates TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ai_infra_monitor (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            checked_at TEXT NOT NULL,
            signal TEXT NOT NULL DEFAULT 'wait',
            score INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            data_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS broker_positions (
            symbol TEXT PRIMARY KEY,
            original_symbol TEXT NOT NULL,
            name TEXT DEFAULT '',
            quantity REAL NOT NULL DEFAULT 0,
            available_quantity REAL NOT NULL DEFAULT 0,
            cost_price REAL,
            last_price REAL,
            currency TEXT DEFAULT '',
            market TEXT DEFAULT '',
            synced_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS broker_executions (
            trade_key TEXT PRIMARY KEY,
            trade_id TEXT DEFAULT '',
            order_id TEXT DEFAULT '',
            symbol TEXT NOT NULL,
            original_symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            trade_done_at TEXT NOT NULL,
            synced_at TEXT NOT NULL
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
        "monitor_direction": "TEXT DEFAULT ''",
        "monitor_state": "TEXT DEFAULT 'tracking'",
    }
    for col, coldef in new_cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE recommendations ADD COLUMN {col} {coldef}")
    conn.commit()

    position_existing = {row[1] for row in conn.execute("PRAGMA table_info(broker_positions)")}
    if "last_price" not in position_existing:
        conn.execute("ALTER TABLE broker_positions ADD COLUMN last_price REAL")
        conn.commit()

    catalyst_existing = {row[1] for row in conn.execute("PRAGMA table_info(catalysts)")}
    catalyst_cols = {
        "event_scope": "TEXT DEFAULT ''",
        "source_name": "TEXT DEFAULT '手动维护'",
        "source_url": "TEXT DEFAULT ''",
        "external_id": "TEXT DEFAULT ''",
        "last_checked_at": "TEXT DEFAULT ''",
    }
    for col, coldef in catalyst_cols.items():
        if col not in catalyst_existing:
            conn.execute(f"ALTER TABLE catalysts ADD COLUMN {col} {coldef}")
    conn.commit()

    # Notifications table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    # Create indexes if not exist
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recommendations_session_id ON recommendations(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recommendations_status ON recommendations(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recommendations_ticker ON recommendations(ticker)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_price_snapshots_rec_date ON price_snapshots(recommendation_id, snapshot_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_catalyst_tickers_catalyst_id ON catalyst_tickers(catalyst_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_catalyst_tickers_ticker ON catalyst_tickers(ticker)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_is_read ON notifications(is_read)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recs_session ON recommendations(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recs_status ON recommendations(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recs_monitoring ON recommendations(monitoring)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recs_created ON recommendations(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snaps_rec ON price_snapshots(recommendation_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snaps_rec_date ON price_snapshots(recommendation_id, snapshot_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_created ON analysis_sessions(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_broker_exec_symbol ON broker_executions(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_broker_exec_time ON broker_executions(trade_done_at)")

    # Agent tables (P0: single agent + tool calling)
    try:
        from agent.schema import ensure_agent_tables

        ensure_agent_tables(conn)
    except ImportError:
        pass

    conn.commit()
    conn.close()


AI_INFRA_SYMBOLS = ("SOXX.US", "SMH.US", "NVDA.US", "TSM.US")


def _ensure_ai_monitor_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_infra_monitor (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            checked_at TEXT NOT NULL,
            signal TEXT NOT NULL DEFAULT 'wait',
            score INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            data_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.commit()


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _ai_symbol_metrics(candles):
    """Calculate the small, explainable indicator set used by the AI monitor."""
    rows = []
    for candle in candles:
        try:
            rows.append({
                "close": float(candle["close"]),
                "low": float(candle["low"]),
                "volume": int(candle.get("volume") or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    if len(rows) < 60:
        raise ValueError("至少需要 60 根日K")

    closes = [row["close"] for row in rows]
    lows = [row["low"] for row in rows]
    volumes = [row["volume"] for row in rows]
    last = closes[-1]
    prior_three_low = min(lows[-6:-3])
    recent_three_low = min(lows[-3:])
    return {
        "last": round(last, 3),
        "ma5": round(_mean(closes[-5:]), 3),
        "ma20": round(_mean(closes[-20:]), 3),
        "ma60": round(_mean(closes[-60:]), 3),
        "return_3d": round((last / closes[-4] - 1) * 100, 2),
        "no_new_low": recent_three_low >= prior_three_low,
        "recent_low": round(recent_three_low, 3),
        "prior_low": round(prior_three_low, 3),
        "volume_contracting": volumes[-1] < _mean(volumes[-6:-1]),
        "volume_ratio_5d": round(volumes[-1] / _mean(volumes[-6:-1]), 2)
            if _mean(volumes[-6:-1]) else None,
    }


def calculate_ai_infra_signal(candles_by_symbol):
    """Return a five-condition, conservative re-entry signal."""
    metrics = {
        symbol: _ai_symbol_metrics(candles_by_symbol[symbol])
        for symbol in AI_INFRA_SYMBOLS
    }
    soxx, smh = metrics["SOXX.US"], metrics["SMH.US"]
    nvda, tsm = metrics["NVDA.US"], metrics["TSM.US"]

    conditions = [
        {
            "key": "hold_ma60",
            "label": "SOXX 与 SMH 守住动态 60 日均线",
            "met": soxx["last"] >= soxx["ma60"] and smh["last"] >= smh["ma60"],
        },
        {
            "key": "no_new_low",
            "label": "两只行业 ETF 连续三日未创更低低点",
            "met": soxx["no_new_low"] and smh["no_new_low"],
        },
        {
            "key": "volume_contracting",
            "label": "SOXX 与 SMH 下跌成交量收缩",
            "met": soxx["volume_contracting"] and smh["volume_contracting"],
        },
        {
            "key": "leaders_relative_strength",
            "label": "NVDA 与 TSM 三日表现强于 SOXX",
            "met": (
                nvda["return_3d"] >= soxx["return_3d"]
                and tsm["return_3d"] >= soxx["return_3d"]
            ),
        },
        {
            "key": "reclaim_average",
            "label": "SOXX 与 SMH 重新站上 5 日或 20 日均线",
            "met": (
                (soxx["last"] >= soxx["ma5"] or soxx["last"] >= soxx["ma20"])
                and (smh["last"] >= smh["ma5"] or smh["last"] >= smh["ma20"])
            ),
        },
    ]
    score = sum(1 for condition in conditions if condition["met"])
    signal = "actionable" if score >= 3 else "wait"
    summary = (
        "满足至少三项止跌条件，可考虑第一批 20%–25% 试仓。"
        if signal == "actionable"
        else f"仅满足 {score}/5 项，尚未触发买回。"
    )
    return {
        "signal": signal,
        "score": score,
        "summary": summary,
        "conditions": conditions,
        "symbols": metrics,
    }


def _to_float(v):
    """Safely convert DB value to float, returns None for non-numeric."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def safe_float(value, default=0.0):
    converted = _to_float(value)
    return default if converted is None else converted


def _business_days_since(date_value, today=None):
    """Count weekdays after date_value through today; sufficient for freshness checks."""
    if not date_value:
        return None
    try:
        start = date.fromisoformat(str(date_value)[:10])
    except (TypeError, ValueError):
        return None
    end = today or date.today()
    if start >= end:
        return 0
    cursor = start + timedelta(days=1)
    count = 0
    while cursor <= end:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count


def evaluate_price_monitor(row, today=None):
    """Evaluate one monitor without treating a price touch as an automatic buy."""
    today = today or date.today()
    r = dict(row)
    lp = _to_float(r.get("latest_price"))
    wp = _to_float(r.get("wait_price"))
    ep = _to_float(r.get("entry_price"))
    direction = (r.get("monitor_direction") or "").strip().lower()
    if direction not in {"pullback", "breakout"}:
        direction = "breakout" if wp and ep and wp > ep else "pullback"
    r["monitor_direction"] = direction
    r["monitor_direction_label"] = "突破" if direction == "breakout" else "回落"
    r["monitor_state"] = "tracking"
    r["monitor_reason"] = "条件尚未接近"

    validity = r.get("validity_date")
    if validity:
        try:
            days_left = (date.fromisoformat(str(validity)[:10]) - today).days
            r["days_until_expiry"] = days_left
            r["is_expiring_soon"] = 0 <= days_left <= 7
            if days_left < 0:
                r["monitor_state"] = "invalidated"
                r["monitor_reason"] = "监控有效期已结束"
                return r
        except ValueError:
            pass

    stale_days = _business_days_since(r.get("latest_date"), today)
    r["stale_trading_days"] = stale_days
    if lp is None or wp is None:
        r["monitor_state"] = "revalidate"
        r["monitor_reason"] = "缺少最新价格或目标条件"
        return r
    if stale_days is None or stale_days > 1:
        r["monitor_state"] = "revalidate"
        r["monitor_reason"] = f"行情已过期（{stale_days if stale_days is not None else '未知'} 个交易日）"
        return r

    if direction == "pullback":
        gap_pct = (lp - wp) / wp * 100
        r["monitor_gap_pct"] = round(gap_pct, 2)
        if lp <= wp * 0.92:
            r["monitor_state"] = "revalidate"
            r["monitor_reason"] = "价格深度跌穿等待区，需重新检查逻辑"
        elif lp <= wp:
            r["monitor_state"] = "triggered"
            r["monitor_reason"] = "价格进入等待区，尚需确认基本面与量价"
        elif gap_pct <= 10:
            r["monitor_state"] = "approaching"
            r["monitor_reason"] = "距离回落目标不足 10%"
    else:
        gap_pct = (lp - wp) / wp * 100
        r["monitor_gap_pct"] = round(gap_pct, 2)
        if lp >= wp:
            r["monitor_state"] = "triggered"
            r["monitor_reason"] = "价格达到突破位，尚需确认收盘与成交量"
        elif lp >= wp * 0.90:
            r["monitor_state"] = "approaching"
            r["monitor_reason"] = "距离突破位不足 10%"
    return r


def _validate_ticker(value):
    if not isinstance(value, str) or not value or len(value) > 20:
        return False
    return all(char.isdigit() or char.isupper() or char == "." for char in value)


def infer_event_scope(event):
    """Classify legacy event rows while new rows store an explicit scope."""
    scope = (event.get("event_scope") or "").strip().lower()
    if scope in {"company", "sector", "macro"}:
        return scope
    title = (event.get("title") or "").lower()
    if any(token in title for token in ("板块", "行业", "产业链", "财报季跟踪")):
        return "sector"
    event_type = (event.get("event_type") or "").lower()
    if event_type in {"macro", "fed", "policy"}:
        return "macro"
    return "company"


def _candle_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).date().isoformat()
    return str(value)[:10]


def calc_pct(entry, current):
    e, c = _to_float(entry), _to_float(current)
    if e and c:
        return round((c - e) / e * 100, 2)
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
        r["primary_theme"] = primary_theme(r)

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
        if r.get("monitoring"):
            wp = _to_float(r.get("wait_price"))
            lp = _to_float(r.get("latest_price"))
            if wp and lp:
                direction = (r.get("monitor_direction") or "").lower()
                if direction not in {"pullback", "breakout"}:
                    ep = _to_float(r.get("entry_price"))
                    direction = "breakout" if ep and wp > ep else "pullback"
                r["price_alert"] = lp >= wp if direction == "breakout" else lp <= wp

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
    # Batch-query rec_count for all sessions
    rec_counts = {}
    if sessions:
        session_ids = [s["id"] for s in sessions]
        conn = get_db()
        ph = ",".join("?" * len(session_ids))
        for row in conn.execute(
            f"SELECT session_id, COUNT(*) as cnt FROM recommendations "
            f"WHERE session_id IN ({ph}) GROUP BY session_id",
            session_ids
        ).fetchall():
            rec_counts[row["session_id"]] = row["cnt"]
        conn.close()

    sessions_list = []
    for s in sessions:
        s = dict(s)
        s["rec_count"] = rec_counts.get(s["id"], 0)
        sessions_list.append(s)
    return render_template("dashboard.html", recent=recent_recs, stats=stats,
                           sessions=sessions_list, monitoring=monitoring_recs)


@app.route("/sessions")
def sessions():
    conn = get_db()
    rows = conn.execute("SELECT * FROM analysis_sessions ORDER BY created_at DESC").fetchall()
    conn.close()
    # Batch-query all recommendations for all sessions
    all_recs = {}
    if rows:
        session_ids = [s["id"] for s in rows]
        conn = get_db()
        ph = ",".join("?" * len(session_ids))
        for row in conn.execute(
            f"SELECT session_id, entry_rec, status FROM recommendations "
            f"WHERE session_id IN ({ph})",
            session_ids
        ).fetchall():
            sid = row["session_id"]
            if sid not in all_recs:
                all_recs[sid] = []
            all_recs[sid].append(dict(row))
        conn.close()

    result = []
    for s in rows:
        s = dict(s)
        recs = all_recs.get(s["id"], [])
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
    entry_counts = {}
    grouped_recs = {}
    for r in recs:
        er = (r.get("entry_rec") or "watch").strip()
        entry_counts[er] = entry_counts.get(er, 0) + 1
        grouped_recs.setdefault(er, []).append(r)
    return render_template("session_detail.html", session=dict(session), recs=recs,
                           entry_rec=ENTRY_REC, today=str(date.today()),
                           entry_counts=entry_counts, grouped_recs=grouped_recs)


PER_PAGE = 50


def _lifecycle_for(position, executions, recommendations):
    if position and _to_float(position.get("quantity")) > 0:
        return "holding"
    if executions:
        return "sold"
    if any(
        r.get("status") == "active" and r.get("entry_rec") != "avoid"
        for r in recommendations
    ):
        return "watching"
    return "archived"


def _load_broker_data(conn):
    positions = {
        row["symbol"]: dict(row)
        for row in conn.execute("SELECT * FROM broker_positions").fetchall()
    }
    executions = {}
    for row in conn.execute(
        "SELECT * FROM broker_executions ORDER BY trade_done_at DESC"
    ).fetchall():
        item = dict(row)
        executions.setdefault(item["symbol"], []).append(item)
    return positions, executions


def _build_stock_rows(recommendations, positions, executions):
    recs_by_symbol = {}
    for rec in recommendations:
        symbol = normalize_symbol(rec.get("ticker"), rec.get("market"))
        recs_by_symbol.setdefault(symbol, []).append(rec)

    rows = []
    for symbol in sorted(set(recs_by_symbol) | set(positions) | set(executions)):
        recs = sorted(
            recs_by_symbol.get(symbol, []),
            key=lambda item: (item.get("status") == "active", item.get("created_at") or ""),
            reverse=True,
        )
        representative = recs[0] if recs else {}
        position = positions.get(symbol)
        trades = executions.get(symbol, [])
        lifecycle = _lifecycle_for(position, trades, recs)
        row = dict(representative)
        row.update(
            {
                "symbol": symbol,
                "ticker_display": display_symbol(symbol),
                "market_display": symbol.rsplit(".", 1)[-1] if "." in symbol else "",
                "position": position,
                "quantity": _to_float(position.get("quantity")) if position else 0,
                "cost_price": _to_float(position.get("cost_price")) if position else None,
                "current_price": (
                    _to_float(position.get("last_price")) if position and position.get("last_price")
                    else representative.get("latest_price")
                ),
                "trade_count": len(trades),
                "last_trade_at": trades[0]["trade_done_at"] if trades else "",
                "rec_count": len(recs),
                "lifecycle": lifecycle,
                "lifecycle_info": LIFECYCLE_LABEL[lifecycle],
                "has_recommendation": bool(recs),
                "primary_theme": representative.get("primary_theme")
                or TICKER_THEME_OVERRIDES.get(symbol, "互联网与消费"),
            }
        )
        if lifecycle == "holding":
            row["judgement"] = "持有"
            row["judgement_class"] = "entry-holding"
        elif lifecycle == "sold":
            row["judgement"] = "已退出"
            row["judgement_class"] = "entry-exited"
        elif representative.get("is_expired_validity"):
            row["judgement"] = "待复核"
            row["judgement_class"] = "entry-review"
        else:
            judgement_map = {
                "actionable": ("可入", "entry-actionable"),
                "immediate": ("可入", "entry-immediate"),
                "wait": ("等待", "entry-wait"),
                "watch": ("观察", "entry-watch"),
            }
            row["judgement"], row["judgement_class"] = judgement_map.get(
                representative.get("entry_rec"), ("观察", "entry-watch")
            )
        if lifecycle == "holding" and row.get("current_price") and row.get("cost_price"):
            row["display_pnl"] = round(
                (row["current_price"] - row["cost_price"]) * row["quantity"], 2
            )
            row["display_pnl_pct"] = round(
                (row["current_price"] - row["cost_price"]) / row["cost_price"] * 100, 2
            )
        elif lifecycle == "sold" and trades:
            buy_cost = sum(
                _to_float(t["quantity"]) * _to_float(t["price"])
                for t in trades if str(t.get("side", "")).lower() == "buy"
            )
            sell_value = sum(
                _to_float(t["quantity"]) * _to_float(t["price"])
                for t in trades if str(t.get("side", "")).lower() == "sell"
            )
            row["display_pnl"] = round(sell_value - buy_cost, 2)
            row["display_pnl_pct"] = round((sell_value - buy_cost) / buy_cost * 100, 2) if buy_cost else None
        else:
            row["display_pnl"] = None
            row["display_pnl_pct"] = None
        rows.append(row)
    order = {"holding": 0, "watching": 1, "sold": 2, "archived": 3}
    rows.sort(
        key=lambda item: (
            order[item["lifecycle"]],
            item.get("created_at") or item.get("last_trade_at") or "",
        ),
        reverse=False,
    )
    return rows


@app.route("/recommendations")
def recommendations():
    framework = request.args.get("framework", "")
    status = request.args.get("status", "")
    entry_rec_filter = request.args.get("entry_rec", "")
    lifecycle_filter = request.args.get("lifecycle", "")
    selected_theme = request.args.get("theme", "")
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1

    conn = get_db()
    rec_rows = conn.execute(
        "SELECT * FROM recommendations ORDER BY created_at DESC"
    ).fetchall()
    themes = list(PRIMARY_THEMES)
    positions, executions = _load_broker_data(conn)
    conn.close()

    enriched = enrich_rows(rec_rows)
    rows = _build_stock_rows(enriched, positions, executions)
    visible_rows = [r for r in rows if r["lifecycle"] != "archived"]
    tab_counts = {
        "all": len(visible_rows),
        "watching": sum(r["lifecycle"] == "watching" for r in visible_rows),
        "holding": sum(r["lifecycle"] == "holding" for r in visible_rows),
        "history": sum(r["trade_count"] > 0 for r in rows),
    }
    if framework:
        rows = [r for r in rows if r.get("framework") == framework]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if entry_rec_filter:
        rows = [r for r in rows if r.get("entry_rec") == entry_rec_filter]
    if lifecycle_filter == "history":
        # 历史持仓包含当前仍持有和已卖出的所有交易过的股票。
        rows = [r for r in rows if r["trade_count"] > 0]
    elif lifecycle_filter:
        rows = [r for r in rows if r["lifecycle"] == lifecycle_filter]
    else:
        rows = visible_rows
    if selected_theme:
        rows = [r for r in rows if r.get("primary_theme") == selected_theme]

    total = len(rows)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    rows = rows[(page - 1) * PER_PAGE:page * PER_PAGE]
    filter_qs = "&".join(
        f"{k}={v}" for k, v in [
            ("framework", framework), ("status", status),
            ("entry_rec", entry_rec_filter), ("lifecycle", lifecycle_filter),
            ("theme", selected_theme),
        ] if v
    )
    return render_template(
        "recommendations.html", rows=rows, frameworks=FRAMEWORKS,
        entry_rec_map=ENTRY_REC,
        selected_fw=framework, selected_status=status,
        selected_entry=entry_rec_filter,
        selected_lifecycle=lifecycle_filter,
        selected_theme=selected_theme, themes=themes,
        tab_counts=tab_counts,
        page=page, pages=pages, total=total, filter_qs=filter_qs,
    )


@app.route("/stocks/sync", methods=["POST"])
def sync_stocks():
    """Synchronise read-only Longbridge positions and executions."""
    try:
        snapshot = LongbridgeClient().account_snapshot(datetime(2025, 9, 12))
    except (LongbridgeNotConfigured, Exception) as exc:
        flash(f"长桥同步失败：{exc}", "danger")
        return redirect(url_for("recommendations"))

    now = datetime.now().isoformat(timespec="seconds")
    conn = get_db()
    conn.execute("DELETE FROM broker_positions")
    for item in snapshot["positions"]:
        original = item["symbol"]
        symbol = normalize_symbol(original)
        conn.execute(
            "INSERT OR REPLACE INTO broker_positions "
            "(symbol,original_symbol,name,quantity,available_quantity,cost_price,last_price,currency,market,synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                symbol, original, item.get("name", ""), item["quantity"],
                item["available_quantity"], item.get("cost_price"), item.get("last_price"),
                item.get("currency", ""), item.get("market", ""), now,
            ),
        )
    for item in snapshot["executions"]:
        original = item["symbol"]
        symbol = normalize_symbol(original)
        conn.execute(
            "INSERT OR REPLACE INTO broker_executions "
            "(trade_key,trade_id,order_id,symbol,original_symbol,side,quantity,price,trade_done_at,synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                item["trade_key"], item.get("trade_id", ""), item.get("order_id", ""),
                symbol, original, item.get("side", ""), item["quantity"], item["price"],
                item["trade_done_at"], now,
            ),
        )
    conn.commit()
    conn.close()
    flash(
        f"长桥同步完成：{len(snapshot['positions'])} 个持仓，"
        f"{len(snapshot['executions'])} 笔成交",
        "success",
    )
    return redirect(url_for("recommendations"))


@app.route("/stock/<path:symbol>")
def stock_detail(symbol):
    symbol = normalize_symbol(symbol)
    conn = get_db()
    all_recs = [dict(row) for row in conn.execute(
        "SELECT * FROM recommendations ORDER BY created_at DESC"
    ).fetchall()]
    matching = [
        row for row in all_recs
        if normalize_symbol(row.get("ticker"), row.get("market")) == symbol
    ]
    position_row = conn.execute(
        "SELECT * FROM broker_positions WHERE symbol=?", (symbol,)
    ).fetchone()
    trade_rows = conn.execute(
        "SELECT * FROM broker_executions WHERE symbol=? ORDER BY trade_done_at DESC",
        (symbol,),
    ).fetchall()
    conn.close()

    recommendations_for_stock = enrich_rows(matching)
    position = dict(position_row) if position_row else None
    trades = [dict(row) for row in trade_rows]
    lifecycle = _lifecycle_for(position, trades, recommendations_for_stock)
    timeline = []
    for rec in recommendations_for_stock:
        timeline.append({
            "at": rec.get("created_at") or "",
            "kind": "recommendation",
            "title": rec.get("entry_rec_label", ("推荐", ""))[0],
            "detail": rec.get("framework_name", ""),
            "rec": rec,
        })
    for trade in trades:
        is_buy = str(trade.get("side", "")).lower() == "buy"
        timeline.append({
            "at": trade.get("trade_done_at") or "",
            "kind": "buy" if is_buy else "sell",
            "title": "买入" if is_buy else "卖出",
            "detail": f"{trade['quantity']:g} 股 @ {trade['price']:g}",
            "trade": trade,
        })
    timeline.sort(key=lambda item: item["at"], reverse=True)
    if not recommendations_for_stock and not position and not trades:
        flash("股票记录不存在", "warning")
        return redirect(url_for("recommendations"))
    return render_template(
        "stock_detail.html",
        symbol=symbol,
        ticker=display_symbol(symbol),
        market=symbol.rsplit(".", 1)[-1] if "." in symbol else "",
        position=position,
        trades=trades,
        recs=recommendations_for_stock,
        lifecycle=lifecycle,
        lifecycle_info=LIFECYCLE_LABEL[lifecycle],
        timeline=timeline,
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
               catalyst=?, wait_price=?, wait_condition=?, validity_date=?, monitor_direction=?,
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
             request.form.get("monitor_direction", "") or "",
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
        fw_defaults_json=json.dumps(FRAMEWORK_DEFAULTS),
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
                catalyst, wait_price, wait_condition, validity_date, monitor_direction)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (0, 0, sid, created_at, ticker, market, framework, entry_price,
             stop, t1, json.dumps(signals, ensure_ascii=False),
             conviction, source_type, notes,
             entry_rec, risk_level, hold_period, strategy_type,
             catalyst, wait_price, wait_condition, validity_date,
             request.form.get("monitor_direction", "") or ""),
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
                           all_sessions=all_sessions, default_session=session_id,
                           fw_defaults_json=json.dumps(FRAMEWORK_DEFAULTS))


@app.route("/rec/<int:rec_id>")
def detail(rec_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    snapshots = conn.execute(
        "SELECT * FROM price_snapshots WHERE recommendation_id=? ORDER BY snapshot_date",
        (rec_id,),
    ).fetchall()
    related_catalysts = []
    position = None
    broker_trades = []
    lifecycle = "watching"
    if row:
        ticker = row["ticker"]
        symbol = normalize_symbol(row["ticker"], row["market"])
        related = conn.execute(
            "SELECT c.* FROM catalysts c "
            "JOIN catalyst_tickers ct ON c.id = ct.catalyst_id "
            "WHERE ct.ticker=? AND c.status IN ('pending','active') "
            "ORDER BY c.event_date ASC",
            (ticker,)
        ).fetchall()
        related_catalysts = [dict(c) for c in related]
        position_row = conn.execute(
            "SELECT * FROM broker_positions WHERE symbol=?", (symbol,)
        ).fetchone()
        position = dict(position_row) if position_row else None
        broker_trades = [dict(item) for item in conn.execute(
            "SELECT * FROM broker_executions WHERE symbol=? ORDER BY trade_done_at DESC",
            (symbol,),
        ).fetchall()]
        lifecycle = _lifecycle_for(position, broker_trades, [dict(row)])
    conn.close()
    if not row:
        flash("记录不存在", "danger")
        return redirect(url_for("recommendations"))
    row = enrich_row(row)
    return render_template("detail.html", row=row, snapshots=[dict(s) for s in snapshots],
                           frameworks=FRAMEWORKS, today=str(date.today()),
                           entry_rec=ENTRY_REC, risk_level=RISK_LEVEL,
                           hold_period=HOLD_PERIOD, strategy_type=STRATEGY_TYPE,
                           related_catalysts=related_catalysts,
                           position=position, broker_trades=broker_trades,
                           lifecycle=lifecycle, lifecycle_info=LIFECYCLE_LABEL[lifecycle])


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
        sl = _to_float(row["stop_loss_price"])
        t1 = _to_float(row["target1_price"])
        if sl and price <= sl:
            conn.execute(
                "UPDATE recommendations SET status='loss', close_price=?, close_date=?, close_reason='止损触发' WHERE id=?",
                (price, snap_date, rec_id))
            msg += " ⚠️ 止损触发"
        elif t1 and price >= t1:
            conn.execute("UPDATE recommendations SET status='profit' WHERE id=?", (rec_id,))
            msg += " 🎯 到达止盈目标"
    if row["monitoring"]:
        wp = _to_float(row["wait_price"])
        direction = (row["monitor_direction"] or "").lower() if "monitor_direction" in row.keys() else ""
        if direction not in {"pullback", "breakout"}:
            ep = _to_float(row["entry_price"])
            direction = "breakout" if wp and ep and wp > ep else "pullback"
        touched = wp and (price >= wp if direction == "breakout" else price <= wp)
        if touched:
            flash(f"🔔 {row['ticker']} 已触发价格条件，请先复核逻辑和量价。", "warning")
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
    row = conn.execute(
        "SELECT monitoring, ticker, hold_period, validity_date FROM recommendations WHERE id=?",
        (rec_id,),
    ).fetchone()
    if not row:
        conn.close()
        flash("推荐不存在", "warning")
        return redirect(url_for("monitor"))
    new_val = 0 if row["monitoring"] else 1
    validity_date = row["validity_date"]
    if new_val and not validity_date:
        default_days = {"short": 30, "medium": 90, "long": 180, "vlong": 90}
        validity_date = str(date.today() + timedelta(days=default_days.get(row["hold_period"], 90)))
    conn.execute(
        "UPDATE recommendations SET monitoring=?, validity_date=?, monitor_state=? WHERE id=?",
        (new_val, validity_date, "tracking" if new_val else "disabled", rec_id),
    )
    conn.commit()
    conn.close()
    flash(("🔔 已开启价格监控" if new_val else "已关闭监控") + f" — {row['ticker']}", "success")
    return redirect(url_for("detail", rec_id=rec_id))


@app.route("/rec/<int:rec_id>/monitor_action", methods=["POST"])
def monitor_action(rec_id):
    action   = request.form.get("action", "")
    conn = get_db()
    row  = conn.execute("SELECT ticker FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    if not row:
        conn.close()
        flash("推荐不存在", "warning")
        return redirect(url_for("monitor"))

    if action == "bought":
        buy_type  = request.form.get("buy_type", "real")
        buy_price = request.form.get("buy_price")
        try:
            buy_price = float(buy_price)
        except (TypeError, ValueError):
            buy_price = None
        if buy_type == "real":
            conn.execute(
                "UPDATE recommendations SET is_bought=1, entry_price=COALESCE(?,entry_price), wait_price=NULL, monitoring=0, monitor_state='completed' WHERE id=?",
                (buy_price, rec_id))
            flash(f"已标记 {row['ticker']} 为实盘买入", "success")
        else:
            conn.execute(
                "UPDATE recommendations SET is_paper=1, wait_price=NULL, monitoring=0, monitor_state='completed' WHERE id=?", (rec_id,))
            flash(f"已标记 {row['ticker']} 为模拟买入", "success")

    elif action == "skipped":
        reason = request.form.get("reason", "manual")
        conn.execute("UPDATE recommendations SET wait_price=NULL, monitoring=0, monitor_state='dismissed' WHERE id=?", (rec_id,))
        flash(f"已跳过 {row['ticker']} 的监控目标", "info")

    elif action == "continue":
        flash(f"{row['ticker']} 继续等待", "info")

    conn.commit()
    conn.close()
    return redirect(url_for("monitor"))


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

    # Deduplicate: skip first snapshot if same date as created_at
    snap_labels = [s["snapshot_date"] for s in snaps]
    snap_prices = [s["price"] for s in snaps]
    snap_pcts = [s["pct_change"] for s in snaps]

    if snap_labels and snap_labels[0] == row["created_at"][:10]:
        snap_labels = snap_labels[1:]
        snap_prices = snap_prices[1:]
        snap_pcts = snap_pcts[1:]

    return jsonify({
        "labels": [row["created_at"][:10]] + snap_labels,
        "prices": [row["entry_price"]] + snap_prices,
        "pct": [0.0] + snap_pcts,
        "entry": row["entry_price"],
    })


@app.route("/api/refresh_prices", methods=["POST"])
@csrf.exempt
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

    try:
        lb = LongbridgeClient()
    except LongbridgeNotConfigured as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 503

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
        entry_price = _to_float(r["entry_price"])
        if not entry_price:
            continue

        # Days from recommendation date to today, +10 buffer for weekends/holidays
        count = (today_date - start).days + 10
        count = max(count, 5)

        try:
            candles = lb.candlesticks(sym, count)
        except Exception:
            errors.append(sym)
            continue

        existing_dates = {row[0] for row in conn.execute(
            "SELECT snapshot_date FROM price_snapshots WHERE recommendation_id=?", (rec_id,)
        ).fetchall()}

        for candle in candles:
            try:
                snap_date = _candle_date(candle["timestamp"])
                close_price = float(candle["close"])
            except (KeyError, TypeError, ValueError):
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


def _refresh_ai_infra_data(lb):
    candles_by_symbol = {}
    for symbol in AI_INFRA_SYMBOLS:
        candles_by_symbol[symbol] = lb.candlesticks(symbol, 70)
    result = calculate_ai_infra_signal(candles_by_symbol)

    checked_at = datetime.now().isoformat(timespec="seconds")
    conn = get_db()
    _ensure_ai_monitor_table(conn)
    conn.execute(
        "INSERT OR REPLACE INTO ai_infra_monitor "
        "(id, checked_at, signal, score, summary, data_json) VALUES (1,?,?,?,?,?)",
        (
            checked_at,
            result["signal"],
            result["score"],
            result["summary"],
            json.dumps(result, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()
    result["checked_at"] = checked_at
    return result


@app.route("/api/refresh_ai_infra", methods=["POST"])
def refresh_ai_infra():
    """Refresh the AI-infrastructure re-entry monitor from Longbridge daily bars."""
    try:
        result = _refresh_ai_infra_data(LongbridgeClient())
    except LongbridgeNotConfigured as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": f"AI 基础设施监控更新失败: {exc}"}), 502
    return jsonify(result)


# API endpoints for Claude to write directly
@app.route("/api/add_session", methods=["POST"])
@csrf.exempt
def api_add_session():
    data = request.get_json(silent=True) or {}
    if not data.get("question"):
        return jsonify({"error": "question is required"}), 400
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
@csrf.exempt
def api_add_rec():
    data = request.get_json(silent=True) or {}
    ticker = data.get("ticker", "").upper()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    if not _validate_ticker(ticker):
        return jsonify({"error": "invalid ticker"}), 400
    entry_price = float(data.get("entry_price", 0))
    framework = data.get("framework", "momentum")
    fw_def = FRAMEWORK_DEFAULTS.get(framework)
    if fw_def and entry_price:
        stop = round(entry_price * fw_def["stop_pct"], 4)
        t1   = round(entry_price * fw_def["target_pct"], 4)
    else:
        stop = _to_float(data.get("stop_loss_price"))
        t1   = _to_float(data.get("target1_price"))
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO recommendations
           (is_bought, is_paper, session_id, created_at, ticker, market, framework, entry_price,
            stop_loss_price, target1_price, signals,
            conviction, source_type, notes,
            entry_rec, risk_level, hold_period, strategy_type,
            catalyst, wait_price, wait_condition, validity_date, monitor_direction)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (data.get("is_bought", 0), data.get("is_paper", 0),
         data.get("session_id"), data.get("created_at", str(date.today())),
         ticker, data.get("market", "US"),
         framework, entry_price, stop, t1,
         json.dumps(data.get("signals", []), ensure_ascii=False),
         int(data.get("conviction", 3) or 3), data.get("source_type", "paper"),
         data.get("notes", ""),
         data.get("entry_rec", "watch"), data.get("risk_level", "medium"),
         data.get("hold_period", "short"), data.get("strategy_type", "momentum"),
         data.get("catalyst", ""), _to_float(data.get("wait_price")),
         data.get("wait_condition", ""), data.get("validity_date"),
         data.get("monitor_direction", "") or ""),
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
        r["event_scope"] = infer_event_scope(r)
        r["scope_label"] = {"company": "个股", "sector": "板块", "macro": "宏观"}.get(r["event_scope"], "事件")
        try:
            r["days_until"] = (date.fromisoformat(r["event_date"]) - today).days
        except Exception:
            r["days_until"] = None
        tickers_raw = conn.execute(
            "SELECT * FROM catalyst_tickers WHERE catalyst_id=?", (r["id"],)
        ).fetchall()
        r["tickers"] = []
        for t in tickers_raw:
            td = dict(t)
            td["latest_price"] = prices.get(td["ticker"])
            td["entry_price"]  = entries.get(td["ticker"])
            lp = _to_float(td["latest_price"])
            ep = _to_float(td["entry_price"])
            if lp and ep:
                td["pct_from_entry"] = round(
                    (lp - ep) / ep * 100, 1)
            else:
                td["pct_from_entry"] = None
            r["tickers"].append(td)

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

    # Calendar events: catalyst dates (blue)
    cal_catalyst = [{"date": r["event_date"], "title": r["title"], "type": r["event_type"],
                     "cat_id": r["id"]} for r in all_cats if r["status"] == "upcoming"]

    return render_template("catalysts.html",
                           needs_review=needs_review, this_week=this_week,
                           this_month=this_month, later=later,
                           overdue=overdue, past=past,
                           cal_catalyst=cal_catalyst,
                           catalyst_types=CATALYST_TYPES,
                           today=str(today))


@app.route("/catalysts/add", methods=["GET", "POST"])
def catalyst_add():
    if request.method == "POST":
        title      = request.form.get("title", "").strip()
        event_type = request.form.get("event_type", "other")
        event_scope= request.form.get("event_scope", "macro")
        event_date = request.form.get("event_date", str(date.today()))
        description= request.form.get("description", "").strip()
        tickers_raw= request.form.get("tickers", "").strip()
        today_str  = str(date.today())
        if not title:
            flash("标题不能为空", "warning")
            return redirect(url_for("catalyst_add"))
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO catalysts (title, event_type, event_scope, event_date, description, status, "
            "source_name, last_checked_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,'upcoming','手动维护',?,?,?)",
            (title, event_type, event_scope, event_date, description,
             today_str, today_str, today_str)
        )
        cat_id = cur.lastrowid
        for t in [x.strip().upper() for x in tickers_raw.split(",") if x.strip()]:
            conn.execute(
                "INSERT INTO catalyst_tickers (catalyst_id, ticker) VALUES (?,?)",
                (cat_id, t)
            )
        conn.commit()
        conn.close()
        flash("事件已添加", "success")
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
        flash("事件不存在", "danger")
        return redirect(url_for("catalysts"))
    row = dict(row)
    row["type_label"] = CATALYST_TYPES.get(row["event_type"], row["event_type"])
    row["event_scope"] = infer_event_scope(row)
    row["scope_label"] = {"company": "个股", "sector": "板块", "macro": "宏观"}.get(row["event_scope"], "事件")
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


CALENDAR_EVENT_META = {
    "report": {"event_type": "earnings", "scope": "company", "label": "财报"},
    "dividend": {"event_type": "dividend", "scope": "company", "label": "分红除息"},
    "split": {"event_type": "split", "scope": "company", "label": "拆股/合股"},
    "ipo": {"event_type": "ipo", "scope": "company", "label": "IPO"},
    "macrodata": {"event_type": "macro", "scope": "macro", "label": "宏观数据"},
}


CALENDAR_FIELD_ZH = {
    "previous": "前值", "estimate": "预测", "actual": "公布",
    "estimate_eps": "预期 EPS", "estimate_revenue": "预期营收",
    "actual_eps": "实际 EPS", "actual_revenue": "实际营收",
}


def _calendar_text_zh(value):
    text = str(value or "")
    replacements = {
        "US Eastern": "美东时间", "U.S. Eastern": "美东时间",
        "Pre-Market": "盘前", "Before Market": "盘前",
        "After-Hours": "盘后", "After Market": "盘后",
        "Earnings Release": "业绩披露", "Dividend": "分红",
        "Stock Split": "拆股", "Initial Public Offering": "首次公开募股",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def _calendar_description(info):
    parts = []
    if info.get("date"):
        parts.append(_calendar_text_zh(info["date"]))
    if info.get("content"):
        parts.append(_calendar_text_zh(info["content"]))
    data = info.get("data") or {}
    if data:
        values = [f"{CALENDAR_FIELD_ZH.get(key, _calendar_text_zh(key))} {_calendar_text_zh(value)}"
                  for key, value in data.items() if key and value not in (None, "", "--", "待公布")]
        if values:
            parts.append("；".join(values))
    return " · ".join(dict.fromkeys(parts))


def _sync_calendar_events(lb, categories=None):
    """Persist Longbridge company and macro calendars for the next 180 days."""
    conn = get_db()
    recs = conn.execute(
        "SELECT DISTINCT ticker, market FROM recommendations WHERE status='active'"
    ).fetchall()

    today_str = str(date.today())
    end_str   = str(date.today() + timedelta(days=180))
    counts = {"added": 0, "updated": 0, "removed": 0, "skipped": 0, "errors": {}}

    # Market closures are not actionable for this monitor; remove prior imports.
    closed_rows = conn.execute(
        "SELECT id FROM catalysts WHERE event_type='closed' AND source_name='长桥财经日历'"
    ).fetchall()
    for row in closed_rows:
        conn.execute("DELETE FROM catalyst_tickers WHERE catalyst_id=?", (row["id"],))
        conn.execute("DELETE FROM catalysts WHERE id=?", (row["id"],))
    counts["removed"] = len(closed_rows)

    symbols = []
    raw_by_symbol = {}
    for rec in recs:
        raw_ticker = rec["ticker"]
        market = (rec["market"] or "US").upper()
        lb_sym = raw_ticker if "." in raw_ticker else f"{raw_ticker}.{market}"
        if lb_sym.endswith(".US"):
            if lb_sym not in symbols:
                symbols.append(lb_sym)
            raw_by_symbol[lb_sym] = raw_ticker

    # Real positions are authoritative; local active recommendations extend the scope.
    if hasattr(lb, "position_symbols"):
        try:
            for lb_sym in lb.position_symbols():
                if lb_sym.endswith(".US") and lb_sym not in symbols:
                    symbols.append(lb_sym)
                raw_by_symbol.setdefault(lb_sym, lb_sym.split(".")[0])
        except Exception as exc:
            counts["errors"]["positions"] = str(exc)

    wanted_categories = categories or tuple(CALENDAR_EVENT_META)
    for category_index, category in enumerate(wanted_categories):
        meta = CALENDAR_EVENT_META[category]
        company_filtered = category in ("report", "dividend", "split")
        if company_filtered and not symbols:
            continue
        if category_index and getattr(lb, "calendar_sync_delay", 0):
            time.sleep(lb.calendar_sync_delay)
        try:
            data = lb.calendar(
                category, today_str, end_str, market="US",
                symbols=symbols if company_filtered else None,
            )
        except Exception as exc:
            counts["errors"][category] = str(exc)
            continue

        for item in data.get("list", []):
            for info in item.get("infos", []):
                if category == "macrodata" and int(info.get("star") or 0) < 3:
                    counts["skipped"] += 1
                    continue
                ev_date = str(item.get("date", ""))[:10]
                if not ev_date:
                    counts["skipped"] += 1
                    continue
                symbol = info.get("symbol") or ""
                raw_ticker = raw_by_symbol.get(symbol) or (symbol.split(".")[0] if symbol else "")
                identity = info.get("id") or f"{symbol}:{info.get('content', '')}"
                external_id = f"longbridge:{category}:{identity}"
                subject = raw_ticker or info.get("counter_name") or "美国"
                title = f"{subject} {meta['label']}" if category != "macrodata" else _calendar_text_zh(info.get("content") or meta["label"])
                description = _calendar_description(info)
                existing = conn.execute(
                    "SELECT id FROM catalysts WHERE external_id=?", (external_id,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE catalysts SET title=?, event_type=?, event_scope=?, event_date=?, description=?, "
                        "source_name='长桥财经日历', last_checked_at=?, updated_at=? WHERE id=?",
                        (title, meta["event_type"], meta["scope"], ev_date, description,
                         today_str, today_str, existing["id"]),
                    )
                    counts["updated"] += 1
                    continue
                cur = conn.execute(
                    "INSERT INTO catalysts (title, event_type, event_scope, event_date, description, status, "
                    "source_name, external_id, last_checked_at, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,'upcoming','长桥财经日历',?,?,?,?)",
                    (title, meta["event_type"], meta["scope"], ev_date, description,
                     external_id, today_str, today_str, today_str),
                )
                if raw_ticker and meta["scope"] == "company":
                    conn.execute(
                        "INSERT INTO catalyst_tickers (catalyst_id, ticker) VALUES (?,?)",
                        (cur.lastrowid, raw_ticker),
                    )
                counts["added"] += 1

    conn.commit()
    conn.close()
    return counts


def _sync_earnings_data(lb):
    """Backward-compatible earnings-only sync used by older callers."""
    return _sync_calendar_events(lb, categories=("report",))


@app.route("/api/sync_earnings", methods=["POST"])
def sync_earnings():
    try:
        result = _sync_calendar_events(LongbridgeClient())
    except LongbridgeNotConfigured as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": f"Longbridge calendar request failed: {exc}"}), 502
    return jsonify(result)


# ── Rotation Radar ────────────────────────────────────────────────────────────

_market_data_lock = threading.RLock()
_market_data_client = None
_retired_market_data_clients = []
_sector_data_cache = {"payload": None, "cached_at": 0.0}
SECTOR_DATA_CACHE_SECONDS = 300


def _is_closed_context_error(exc):
    message = str(exc).lower()
    return "context closed" in message or "client is closed" in message


def _get_market_data_client(reconnect=False):
    """Keep the SDK context alive for the Flask process and replace it on failure."""
    global _market_data_client
    if reconnect and _market_data_client is not None:
        # Some SDK versions close shared runtime state when a context is garbage
        # collected. Keep failed contexts referenced until process shutdown.
        _retired_market_data_clients.append(_market_data_client)
        _market_data_client = None
    if _market_data_client is None:
        _market_data_client = LongbridgeClient()
    return _market_data_client


def _stock_rows_for_themes():
    conn = get_db()
    rec_rows = conn.execute(
        "SELECT * FROM recommendations ORDER BY created_at DESC"
    ).fetchall()
    positions, executions = _load_broker_data(conn)
    conn.close()
    return _build_stock_rows(enrich_rows(rec_rows), positions, executions)


@app.route("/themes")
def themes():
    view = request.args.get("view", "rotation")
    selected_theme = request.args.get("theme", "")
    if selected_theme not in PRIMARY_THEMES:
        selected_theme = ""

    if view == "supply":
        stock_states = {}
        for row in _stock_rows_for_themes():
            if row["lifecycle"] == "archived":
                continue
            stock_states[row["ticker_display"]] = {
                "symbol": row["symbol"],
                "label": row["lifecycle_info"][0],
                "state": row["lifecycle"],
                "theme": row["primary_theme"],
            }
        return render_template(
            "supply_chain.html",
            themes=PRIMARY_THEMES,
            selected_theme=selected_theme,
            stock_states=stock_states,
        )

    return render_template(
        "rotation.html", themes=PRIMARY_THEMES, selected_theme=selected_theme
    )


@app.route("/rotation")
def rotation():
    return redirect(url_for("themes", view="rotation"))


def _calculate_sector_data(lb):
    """Build rotation signals from the same themes used by the stock list."""
    stock_rows = _stock_rows_for_themes()
    eligible = [r for r in stock_rows if r["lifecycle"] in {"holding", "watching"}]

    members_by_theme = {theme: [] for theme in PRIMARY_THEMES}
    holding_counts = {theme: 0 for theme in PRIMARY_THEMES}
    watching_counts = {theme: 0 for theme in PRIMARY_THEMES}
    for row in eligible:
        theme = row["primary_theme"]
        if theme not in members_by_theme:
            continue
        members_by_theme[theme].append(row["symbol"])
        if row["lifecycle"] == "holding":
            holding_counts[theme] += 1
        else:
            watching_counts[theme] += 1

    proxies = [config["proxy"] for config in THEME_ROTATION_CONFIG.values()]
    member_symbols = [symbol for members in members_by_theme.values() for symbol in members]
    all_symbols = sorted(set(proxies + member_symbols + ["SPY.US"]))

    # ── 1. Quotes (1D) ────────────────────────────────────────────
    try:
        quote_map = {q["symbol"]: q for q in lb.quotes(all_symbols)}
    except Exception as exc:
        if _is_closed_context_error(exc):
            raise
        quote_map = {}
        for symbol in all_symbols:
            try:
                for quote in lb.quotes([symbol]):
                    quote_map[quote["symbol"]] = quote
            except Exception as retry_exc:
                if _is_closed_context_error(retry_exc):
                    raise
                pass
        if not quote_map:
            raise RuntimeError(f"quote fetch failed: {exc}")

    spy_1d = float(quote_map.get("SPY.US", {}).get("change_percentage", 0) or 0)

    # ── 2. Klines (23 trading days = ~1 month) ────────────────────
    kline_map = {}
    for sym in all_symbols:
        try:
            candles = lb.candlesticks(sym, 23)
            if candles:
                kline_map[sym] = [float(c["close"]) for c in candles]
        except Exception as exc:
            if _is_closed_context_error(exc):
                raise
            pass

    def pct_change(closes, n):
        """n-day % change from the closes list (most recent last)."""
        if len(closes) < n + 1:
            return None
        return (closes[-1] - closes[-(n+1)]) / closes[-(n+1)] * 100

    spy_closes = kline_map.get("SPY.US", [])
    spy_1w = pct_change(spy_closes, 5)
    spy_1m = pct_change(spy_closes, 22)

    def mean(values):
        values = [value for value in values if value is not None]
        return sum(values) / len(values) if values else None

    def symbol_metrics(symbol):
        quote = quote_map.get(symbol, {})
        closes = kline_map.get(symbol, [])
        return {
            "chg_1d": float(quote.get("change_percentage", 0) or 0) if quote else None,
            "chg_1w": pct_change(closes, 5),
            "chg_1m": pct_change(closes, 22),
            "chg_2w": pct_change(closes, 10),
        }

    # ── 3. Build result ───────────────────────────────────────────
    result = []
    for theme in PRIMARY_THEMES:
        config = THEME_ROTATION_CONFIG[theme]
        members = members_by_theme[theme]
        valid_members = [symbol for symbol in members if symbol in kline_map]
        calculation_symbols = valid_members or [config["proxy"]]
        metrics = [symbol_metrics(symbol) for symbol in calculation_symbols]
        chg_1d = mean([item["chg_1d"] for item in metrics])
        chg_1w = mean([item["chg_1w"] for item in metrics])
        chg_1m = mean([item["chg_1m"] for item in metrics])
        chg_2w = mean([item["chg_2w"] for item in metrics])

        rs_1d = round(chg_1d - spy_1d, 2) if chg_1d is not None else None
        rs_1w = round(chg_1w - spy_1w, 2) if (chg_1w is not None and spy_1w is not None) else None
        rs_1m = round(chg_1m - spy_1m, 2) if (chg_1m is not None and spy_1m is not None) else None
        rs_2w = round(chg_2w - pct_change(spy_closes, 10), 2) if (chg_2w is not None and pct_change(spy_closes, 10) is not None) else None

        # Rotation quadrant: x=RS_1M, y=RS_trend (1W RS vs 2W RS momentum)
        rot_x = rs_1m
        rot_y = round(rs_1w - rs_2w, 2) if (rs_1w is not None and rs_2w is not None) else None

        result.append({
            "symbol": theme,
            "name": theme,
            "proxy": config["proxy_name"],
            "price": float(quote_map.get(config["proxy"], {}).get("last", 0) or 0),
            "chg_1d": round(chg_1d, 2) if chg_1d is not None else None,
            "chg_1w":  round(chg_1w, 2) if chg_1w is not None else None,
            "chg_1m":  round(chg_1m, 2) if chg_1m is not None else None,
            "rs_1d":   rs_1d,
            "rs_1w":   rs_1w,
            "rs_1m":   rs_1m,
            "rot_x":   rot_x,
            "rot_y":   rot_y,
            "holding_count": holding_counts[theme],
            "watching_count": watching_counts[theme],
            "member_count": len(members),
            "sample_count": len(valid_members),
            "source": "portfolio" if valid_members else "proxy",
        })

    return {
        "sectors": result,
        "spy": {
            "chg_1d": spy_1d,
            "chg_1w": round(spy_1w, 2) if spy_1w is not None else None,
            "chg_1m": round(spy_1m, 2) if spy_1m is not None else None,
        },
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "stale": False,
    }


@app.route("/api/sector_data")
def sector_data():
    """Return cached theme rotation data and self-heal a closed SDK context."""
    force_refresh = request.args.get("refresh") == "1"
    now = time.time()
    cached = _sector_data_cache["payload"]
    if (
        cached
        and not force_refresh
        and now - _sector_data_cache["cached_at"] < SECTOR_DATA_CACHE_SECONDS
    ):
        return jsonify(cached)

    with _market_data_lock:
        cached = _sector_data_cache["payload"]
        now = time.time()
        if (
            cached
            and not force_refresh
            and now - _sector_data_cache["cached_at"] < SECTOR_DATA_CACHE_SECONDS
        ):
            return jsonify(cached)

        last_error = None
        for attempt in range(2):
            try:
                lb = _get_market_data_client(reconnect=attempt > 0)
                payload = _calculate_sector_data(lb)
                _sector_data_cache.update(payload=payload, cached_at=time.time())
                return jsonify(payload)
            except LongbridgeNotConfigured as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                if not _is_closed_context_error(exc):
                    break

        if cached:
            stale_payload = dict(cached)
            stale_payload.update(
                stale=True,
                warning=f"实时行情暂不可用，显示最近成功数据：{last_error}",
            )
            return jsonify(stale_payload)
        status = 503 if isinstance(last_error, LongbridgeNotConfigured) else 502
        return jsonify({"error": f"行情获取失败：{last_error}"}), status


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

    # Normalize tickers to Longbridge format
    us_tickers = []
    for t in tickers_raw:
        sym = t if "." in t else t + ".US"
        if sym.endswith(".US"):
            us_tickers.append(sym)

    try:
        lb = LongbridgeClient()
    except LongbridgeNotConfigured as exc:
        return jsonify({"error": str(exc)}), 503

    def _calendar(category, market=None, symbols=None):
        try:
            return lb.calendar(
                category, start_str, end_str, market=market, symbols=symbols
            )
        except Exception:
            return {"list": []}

    # Closed dates
    closed = {}
    data = _calendar("closed", market="US")
    for item in data.get("list", []):
        for info in item.get("infos", []):
            closed[item["date"]] = info.get("content", "休市")

    # Dividends for active US tickers (batch up to 10)
    dividends = []
    batch_size = 10
    for i in range(0, len(us_tickers), batch_size):
        batch = us_tickers[i:i + batch_size]
        data = _calendar("dividend", market="US", symbols=batch)
        for item in data.get("list", []):
            for info in item.get("infos", []):
                ticker = info.get("symbol", "").split(".")[0]
                dividends.append({
                    "date":    item["date"],
                    "ticker":  ticker,
                    "amount":  info.get("content", ""),
                    "market":  info.get("market", "US"),
                })

    # IPOs — US market
    ipos = []
    data = _calendar("ipo", market="US")
    for item in data.get("list", []):
        for info in item.get("infos", []):
            ipos.append({
                "date":    item["date"],
                "ticker":  info.get("symbol", "").split(".")[0],
                "name":    info.get("counter_name", ""),
                "content": info.get("content", ""),
            })

    # Splits — active US tickers
    splits = []
    for i in range(0, len(us_tickers), batch_size):
        batch = us_tickers[i:i + batch_size]
        data = _calendar("split", market="US", symbols=batch)
        for item in data.get("list", []):
            for info in item.get("infos", []):
                ticker = info.get("symbol", "").split(".")[0]
                splits.append({
                    "date":    item["date"],
                    "ticker":  ticker,
                    "content": info.get("content", ""),
                })

    return jsonify({
        "closed":    closed,
        "dividends": dividends,
        "ipos":      ipos,
        "splits":    splits,
    })


# ── Supply Chain ──────────────────────────────────────────────────────────────

@app.route("/supply-chain")
def supply_chain():
    return redirect(url_for("themes", view="supply"))


# ── Monitor ───────────────────────────────────────────────────────────────────

@app.route("/monitor")
def monitor():
    conn = get_db()
    today = date.today()
    _ensure_ai_monitor_table(conn)

    ai_row = conn.execute(
        "SELECT checked_at, signal, score, summary, data_json "
        "FROM ai_infra_monitor WHERE id=1"
    ).fetchone()
    ai_monitor = None
    if ai_row:
        try:
            ai_monitor = json.loads(ai_row["data_json"])
        except (json.JSONDecodeError, TypeError):
            ai_monitor = {}
        ai_monitor.update({
            "key": "ai_infrastructure",
            "name": "AI 基础设施",
            "horizon": "近期（数日到数周）",
            "checked_at": ai_row["checked_at"],
            "signal": ai_row["signal"],
            "score": ai_row["score"],
            "summary": ai_row["summary"],
        })
    sector_monitors = [ai_monitor] if ai_monitor else []

    # Only explicit, active monitors belong in the current-monitoring inbox.
    # Include any rec with at least one price condition (wait/stop_loss/target).
    recs = conn.execute(
        "SELECT * FROM recommendations "
        "WHERE monitoring=1 AND status='active' "
        "AND (CAST(wait_price AS REAL) > 0 OR CAST(stop_loss_price AS REAL) > 0 OR CAST(target1_price AS REAL) > 0) "
        "ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    recs = enrich_rows(recs)

    triggered, approaching, watching, revalidate, expired = [], [], [], [], []
    for r in recs:
        r = evaluate_price_monitor(r, today)
        if r["monitor_state"] == "invalidated":
            expired.append(r)
        elif r["monitor_state"] == "revalidate":
            revalidate.append(r)
        elif r["monitor_state"] == "triggered":
            triggered.append(r)
        elif r["monitor_state"] == "approaching":
            approaching.append(r)
        else:
            watching.append(r)

    all_count = len(triggered) + len(approaching) + len(watching) + len(revalidate) + len(expired)

    # ── Event data split into company / sector / macro scopes ─────
    conn2 = get_db()
    prices2 = {}
    entries2 = {}
    for row in conn2.execute(
        "SELECT r.ticker, r.entry_price, ps.price "
        "FROM recommendations r "
        "JOIN price_snapshots ps ON ps.recommendation_id=r.id "
        "WHERE ps.snapshot_date=(SELECT MAX(s2.snapshot_date) FROM price_snapshots s2 WHERE s2.recommendation_id=r.id)"
    ).fetchall():
        prices2[row["ticker"]]  = row["price"]
        entries2[row["ticker"]] = row["entry_price"]

    all_cats = [dict(r) for r in conn2.execute(
        "SELECT * FROM catalysts ORDER BY event_date ASC, id DESC"
    ).fetchall()]
    for r in all_cats:
        r["event_scope"] = infer_event_scope(r)
        r["type_label"] = CATALYST_TYPES.get(r["event_type"], r["event_type"])
        try:
            r["days_until"] = (date.fromisoformat(r["event_date"]) - today).days
        except Exception:
            r["days_until"] = None
        tickers_raw = conn2.execute(
            "SELECT * FROM catalyst_tickers WHERE catalyst_id=?", (r["id"],)
        ).fetchall()
        r["tickers"] = []
        for t in tickers_raw:
            td = dict(t)
            td["latest_price"] = prices2.get(td["ticker"])
            td["entry_price"]  = entries2.get(td["ticker"])
            lp = _to_float(td["latest_price"])
            ep = _to_float(td["entry_price"])
            if lp and ep:
                td["pct_from_entry"] = round(
                    (lp - ep) / ep * 100, 1)
            else:
                td["pct_from_entry"] = None
            r["tickers"].append(td)

    conn2.close()

    macro_events = [r for r in all_cats if r["event_scope"] == "macro"]
    sector_events = [r for r in all_cats if r["event_scope"] == "sector"]
    company_events = [r for r in all_cats if r["event_scope"] == "company"]
    review_ids   = {r["id"] for r in macro_events if r["status"] == "happened" and not r.get("impact_score")}
    needs_review = [r for r in macro_events if r["id"] in review_ids]
    cat_this_week  = [r for r in macro_events if r["status"] == "upcoming" and r["days_until"] is not None and 0  <= r["days_until"] <= 7]
    cat_this_month = [r for r in macro_events if r["status"] == "upcoming" and r["days_until"] is not None and 8  <= r["days_until"] <= 30]
    cat_later      = [r for r in macro_events if r["status"] == "upcoming" and r["days_until"] is not None and r["days_until"] > 30]
    cat_overdue    = [r for r in macro_events if r["status"] == "upcoming" and r["days_until"] is not None and r["days_until"] < 0]
    cat_past       = [r for r in macro_events if r["status"] in ("happened", "skipped") and r["id"] not in review_ids]
    macro_count = len([r for r in macro_events if r["status"] == "upcoming"])
    cal_catalyst   = [{"date": r["event_date"], "title": r["title"], "type": r["event_type"], "cat_id": r["id"]}
                      for r in all_cats if r["status"] == "upcoming"]

    return render_template("monitor.html",
                           triggered=triggered, approaching=approaching,
                           watching=watching, revalidate=revalidate, expired=expired,
                           all_count=all_count,
                           monitor_action_reasons=MONITOR_ACTION_REASONS,
                           catalyst_types=CATALYST_TYPES,
                           needs_review=needs_review,
                           cat_this_week=cat_this_week, cat_this_month=cat_this_month,
                           cat_later=cat_later, cat_overdue=cat_overdue, cat_past=cat_past,
                           cal_catalyst=cal_catalyst,
                           ai_monitor=ai_monitor,
                           sector_monitors=sector_monitors,
                           sector_events=sector_events,
                           company_events=company_events,
                           macro_count=macro_count,
                           today=str(today))


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


# ── Notifications ─────────────────────────────────────────────────────

def add_notification(title, message):
    """Helper: insert a notification row."""
    conn = get_db()
    now = str(date.today())
    conn.execute(
        "INSERT INTO notifications (title, message, is_read, created_at) VALUES (?,?,0,?)",
        (title, message, now)
    )
    conn.commit()
    conn.close()


@app.route("/api/notifications")
def api_notifications():
    conn = get_db()
    notifs = conn.execute(
        "SELECT * FROM notifications ORDER BY created_at DESC"
    ).fetchall()
    result = [dict(n) for n in notifs]
    conn.close()
    return jsonify(result)


@app.route("/api/notifications/count")
def api_notifications_count():
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM notifications WHERE is_read=0"
    ).fetchone()
    conn.close()
    return jsonify({"count": row["cnt"]})


@app.route("/api/notifications/mark_read", methods=["POST"])
def api_notifications_mark_read():
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get("ids", [])
    conn = get_db()
    if ids:
        ph = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE notifications SET is_read=1 WHERE id IN ({ph})", ids
        )
    else:
        conn.execute("UPDATE notifications SET is_read=1")
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── AI Agent (P0: single agent + tool calling) ─────────────────────

@app.route("/agent")
def agent_page():
    from agent import config as agent_config

    return render_template(
        "agent.html",
        llm_configured=agent_config.is_configured(),
        llm_model=agent_config.llm_model(),
    )


@app.route("/api/agent/state")
def api_agent_state():
    from agent import config as agent_config, runtime as agent_runtime

    conn = get_db()
    try:
        chats = [dict(r) for r in conn.execute(
            "SELECT id, title, kind, created_at FROM agent_chats ORDER BY id DESC LIMIT 50"
        ).fetchall()]
        pending = [dict(r) for r in conn.execute(
            "SELECT id, chat_id, tool_name, summary, status, created_at FROM pending_actions "
            "WHERE status='pending' ORDER BY id DESC LIMIT 20"
        ).fetchall()]
        tokens = agent_runtime.tokens_used_today(conn)
    finally:
        conn.close()
    return jsonify({
        "llm": {"configured": agent_config.is_configured(), "model": agent_config.llm_model()},
        "budget": {"used_today": tokens, "limit": agent_config.daily_token_budget()},
        "chats": chats,
        "pending": pending,
    })


@app.route("/api/agent/chats", methods=["POST"])
def api_agent_chats_create():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()[:60] or (
        f"对话 {datetime.now().strftime('%m-%d %H:%M')}"
    )
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO agent_chats (title, kind, created_at) VALUES (?,?,?)",
            (title, "chat", datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return jsonify({"chat_id": cur.lastrowid, "title": title})
    finally:
        conn.close()


@app.route("/api/agent/messages")
def api_agent_messages():
    chat_id = request.args.get("chat_id", type=int)
    if not chat_id:
        return jsonify({"messages": [], "pendings": []})
    conn = get_db()
    try:
        messages = [dict(r) for r in conn.execute(
            "SELECT id, role, content, tool_calls_json, created_at FROM agent_messages "
            "WHERE chat_id=? ORDER BY id",
            (chat_id,),
        ).fetchall()]
        pendings = [dict(r) for r in conn.execute(
            "SELECT * FROM pending_actions WHERE chat_id=? ORDER BY id", (chat_id,)
        ).fetchall()]
    finally:
        conn.close()
    for item in messages:
        try:
            item["tool_calls"] = json.loads(item.pop("tool_calls_json") or "[]")
        except ValueError:
            item["tool_calls"] = []
    return jsonify({"messages": messages, "pendings": pendings})


@app.route("/api/agent/chat", methods=["POST"])
def api_agent_chat():
    from agent import runtime as agent_runtime
    from agent.config import AgentNotConfigured, BudgetExceeded

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message or len(message) > 12000:
        return jsonify({"error": "message is required and must be <= 12000 characters"}), 400
    conn = get_db()
    try:
        chat_id = data.get("chat_id")
        if not chat_id:
            title = message[:24] + ("…" if len(message) > 24 else "")
            cur = conn.execute(
                "INSERT INTO agent_chats (title, kind, created_at) VALUES (?,?,?)",
                (title, "chat", datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
            chat_id = cur.lastrowid
        from agent import config as agent_config, tasks as agent_tasks
        agent_config.require_configured()
        agent_runtime.check_budget(conn)
        if not conn.execute("SELECT 1 FROM agent_chats WHERE id=?", (int(chat_id),)).fetchone():
            return jsonify({"error": "chat not found"}), 404
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute("SELECT id FROM agent_tasks WHERE chat_id=? AND status IN ('queued','running','retrying','waiting_approval') LIMIT 1", (int(chat_id),)).fetchone()
        if active:
            conn.rollback()
            return jsonify({"error": "该会话尚有任务或审批未处理", "task_id": active["id"]}), 409
        task_id = agent_tasks.create_task(conn, "chat", "research", {"message": message, "chat_id": int(chat_id)})
        conn.execute("UPDATE agent_tasks SET chat_id=? WHERE id=?", (int(chat_id), task_id))
        conn.commit()
        return jsonify({"task_id": task_id, "chat_id": chat_id, "status": "queued"}), 202
    except AgentNotConfigured as exc:
        return jsonify({"error": str(exc)}), 503
    except BudgetExceeded as exc:
        return jsonify({"error": str(exc)}), 429
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    finally:
        conn.close()


@app.route("/api/agent/pending/<int:pending_id>/resolve", methods=["POST"])
def api_agent_pending_resolve(pending_id):
    from agent import runtime as agent_runtime
    from agent import tasks as agent_tasks

    data = request.get_json(silent=True) or {}
    if data.get("action") not in {"approve", "reject"}:
        return jsonify({"error": "action must be approve or reject"}), 400
    approve = data["action"] == "approve"
    conn = get_db()
    try:
        row = agent_runtime.resolve_pending_action(conn, pending_id, approve)
        if row and row.get("changed") and row.get("task_id"):
            task = agent_tasks.on_pending_resolved(
                conn, row["chat_id"], approve, reject_summary=(data.get("reason") or "")[:1000], task_id=row["task_id"]
            )
            if task:
                row["task"] = task
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "pending action not found"}), 404
    return jsonify({"pending": row})


@app.route("/api/agent/events")
def api_agent_events():
    from agent import events as agent_events

    conn = get_db()
    try:
        events = agent_events.list_events(conn, limit=30)
    finally:
        conn.close()
    return jsonify({"events": events})


@app.route("/api/agent/tasks")
def api_agent_tasks():
    from agent import tasks as agent_tasks

    status = request.args.get("status", "") or None
    conn = get_db()
    try:
        tasks = agent_tasks.list_tasks(conn, limit=50, status=status)
    finally:
        conn.close()
    return jsonify({"tasks": tasks})


@app.route("/api/agent/metrics")
def api_agent_metrics():
    from agent import eval as agent_eval, metrics as agent_metrics

    conn = get_db()
    try:
        online = agent_metrics.metrics(conn)
        latest = agent_eval.get_latest_run(conn)
        baseline = agent_eval.get_baseline(conn)
        offline = None
        if latest:
            offline = json.loads(latest["metrics_json"] or "{}")
            offline["run_id"] = latest["id"]
            offline["created_at"] = latest["created_at"]
            offline["is_baseline"] = bool(latest["is_baseline"])
            if baseline and baseline["id"] != latest["id"]:
                base_metrics = json.loads(baseline["metrics_json"] or "{}")
                offline["baseline_diff"] = agent_eval._diff_windows(
                    offline.get("windows") or {}, base_metrics.get("windows") or {}
                )
    finally:
        conn.close()
    return jsonify({"online": online, "offline": offline})


@app.route("/api/agent/events/scan", methods=["POST"])
@csrf.exempt
def api_agent_events_scan():
    """Manual event scan: scan → dispatch → run queued tasks."""
    from agent import events as agent_events
    from agent import workflow as agent_workflow

    conn = get_db()
    try:
        scanned = agent_events.scan_events(conn)
        dispatched = agent_events.process_events(conn, limit=10)
        ran = []  # execution belongs to the worker, never a web request
        return jsonify({
            "scanned": scanned,
            "dispatched": len(dispatched),
            "ran": [{"task_id": task_id, "status": status} for task_id, status in ran],
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    finally:
        conn.close()


@app.route("/api/agent/workflows/<wf>/run", methods=["POST"])
def api_agent_workflow_run(wf):
    """Manually trigger a workflow (review / research). Synchronous."""
    from agent import workflow as agent_workflow

    data = request.get_json(silent=True) or {}
    if wf not in agent_workflow.WORKFLOWS:
        return jsonify({"error": f"unknown workflow: {wf}"}), 400
    payload = {}
    if wf == "research":
        ticker = (data.get("ticker") or "").strip().upper()
        if not ticker:
            return jsonify({"error": "ticker is required for research"}), 400
        payload = {"ticker": ticker}
    elif wf == "review":
        payload = {"ticker": (data.get("ticker") or "").strip().upper()}
    conn = get_db()
    try:
        from agent import config as agent_config, tasks as agent_tasks
        agent_config.require_configured()
        task_id = agent_tasks.create_task(conn, wf, agent_workflow.route_agent_type(wf), payload)
        return jsonify({"task": agent_tasks.get_task(conn, task_id), "task_id": task_id}), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    finally:
        conn.close()


@app.route("/api/agent/morning_report", methods=["POST"])
@csrf.exempt
def api_agent_morning_report():
    from agent import config as agent_config, tasks as agent_tasks
    from agent.config import AgentNotConfigured
    conn = get_db()
    try:
        agent_config.require_configured()
        task_id = agent_tasks.create_task(conn, "morning", "monitor")
        return jsonify({"task_id": task_id, "status": "queued"}), 202
    except AgentNotConfigured as exc:
        return jsonify({"error": str(exc)}), 503
    finally:
        conn.close()


@app.route("/api/agent/tasks/<int:task_id>/cancel", methods=["POST"])
def api_agent_task_cancel(task_id):
    from agent import tasks as agent_tasks
    conn = get_db()
    try:
        task = agent_tasks.terminate_task(conn, task_id, "cancelled", "用户取消任务")
        return (jsonify({"task": task}), 200) if task else (jsonify({"error": "task not found"}), 404)
    finally:
        conn.close()


@app.route("/api/agent/tasks/<int:task_id>")
def api_agent_task_detail(task_id):
    from agent import tasks as agent_tasks
    conn = get_db()
    try:
        task = agent_tasks.get_task(conn, task_id)
        if not task:
            return jsonify({"error": "task not found"}), 404
        task["result"] = json.loads(task.get("result") or "{}")
        task["steps"] = [dict(row) for row in conn.execute("SELECT name,status,detail,created_at FROM agent_task_steps WHERE task_id=? ORDER BY id", (task_id,))]
        if task.get("run_id"):
            task["tool_calls"] = [dict(row) for row in conn.execute(
                "SELECT tool_name AS name,status,duration_ms,result_summary AS summary FROM agent_tool_calls WHERE run_id=? ORDER BY id", (task["run_id"],))]
        return jsonify({"task": task})
    finally:
        conn.close()



if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5050))
    print(f"投资追踪系统启动：http://127.0.0.1:{port}")
    app.run(debug=True, port=port)
