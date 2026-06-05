from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import sqlite3
import json
from datetime import date, datetime, timedelta
import os

app = Flask(__name__)
app.secret_key = "tracker-2026"

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tracker.db")

FRAMEWORKS = {
    "momentum": "动量催化框架",
    "fundamental": "基本面研究框架",
    "radar": "每日扫描雷达",
    "ad_hoc": "临时问询",
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
        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            close_reason TEXT
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
    conn.commit()
    conn.close()


def calc_pct(entry, current):
    if entry and current:
        return round((current - entry) / entry * 100, 2)
    return None


def enrich_row(row):
    r = dict(row)
    r["signals"] = json.loads(r.get("signals") or "[]")
    r["framework_name"] = FRAMEWORKS.get(r["framework"], r["framework"])
    r["pct_change"] = None
    # get latest snapshot
    conn = get_db()
    snap = conn.execute(
        "SELECT price, snapshot_date FROM price_snapshots WHERE recommendation_id=? ORDER BY snapshot_date DESC LIMIT 1",
        (r["id"],),
    ).fetchone()
    conn.close()
    if snap:
        r["latest_price"] = snap["price"]
        r["latest_date"] = snap["snapshot_date"]
        r["pct_change"] = calc_pct(r["entry_price"], snap["price"])
    elif r["close_price"]:
        r["latest_price"] = r["close_price"]
        r["latest_date"] = r["close_date"]
        r["pct_change"] = calc_pct(r["entry_price"], r["close_price"])
    else:
        r["latest_price"] = None
        r["latest_date"] = None
    return r


def compute_stats():
    conn = get_db()
    rows = conn.execute("SELECT * FROM recommendations").fetchall()
    conn.close()
    rows = [enrich_row(r) for r in rows]

    total = len(rows)
    active = sum(1 for r in rows if r["status"] == "active")

    # Framework win rate: target1 hit before stop loss
    closed = [r for r in rows if r["status"] in ("stopped", "target1", "target2", "closed")]
    wins_fw = [r for r in closed if r["status"] in ("target1", "target2")]
    fw_win_rate = round(len(wins_fw) / len(closed) * 100, 1) if closed else None

    # T+21 win rate: latest price > entry (for recs older than 21 days)
    t21_eligible = [r for r in rows if r["created_at"] and
                    (date.today() - date.fromisoformat(r["created_at"])).days >= 21
                    and r["pct_change"] is not None]
    t21_wins = [r for r in t21_eligible if (r["pct_change"] or 0) > 0]
    t21_win_rate = round(len(t21_wins) / len(t21_eligible) * 100, 1) if t21_eligible else None

    # Per-framework breakdown
    fw_stats = {}
    for fw, fw_name in FRAMEWORKS.items():
        fw_rows = [r for r in rows if r["framework"] == fw]
        fw_closed = [r for r in fw_rows if r["status"] in ("stopped", "target1", "target2", "closed")]
        fw_wins = [r for r in fw_closed if r["status"] in ("target1", "target2")]
        fw_stats[fw] = {
            "name": fw_name,
            "total": len(fw_rows),
            "active": sum(1 for r in fw_rows if r["status"] == "active"),
            "win_rate": round(len(fw_wins) / len(fw_closed) * 100, 1) if fw_closed else None,
            "closed": len(fw_closed),
        }

    return {
        "total": total,
        "active": active,
        "fw_win_rate": fw_win_rate,
        "fw_win_count": len(wins_fw),
        "fw_closed_count": len(closed),
        "t21_win_rate": t21_win_rate,
        "t21_win_count": len(t21_wins),
        "t21_eligible_count": len(t21_eligible),
        "fw_stats": fw_stats,
    }


@app.route("/")
def dashboard():
    conn = get_db()
    recent = conn.execute(
        "SELECT * FROM recommendations ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    conn.close()
    recent = [enrich_row(r) for r in recent]
    stats = compute_stats()
    return render_template("dashboard.html", recent=recent, stats=stats)


@app.route("/recommendations")
def recommendations():
    framework = request.args.get("framework", "")
    status = request.args.get("status", "")
    source = request.args.get("source", "")

    query = "SELECT * FROM recommendations WHERE 1=1"
    params = []
    if framework:
        query += " AND framework=?"
        params.append(framework)
    if status:
        query += " AND status=?"
        params.append(status)
    if source:
        query += " AND source_type=?"
        params.append(source)
    query += " ORDER BY created_at DESC"

    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    rows = [enrich_row(r) for r in rows]
    return render_template(
        "recommendations.html",
        rows=rows,
        frameworks=FRAMEWORKS,
        selected_fw=framework,
        selected_status=status,
        selected_source=source,
    )


@app.route("/add", methods=["GET", "POST"])
def add():
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

        # Compute stop/targets from momentum framework defaults
        if framework == "momentum":
            stop = round(entry_price * 0.92, 4)
            t1 = round(entry_price * 1.15, 4)
            t2 = round(entry_price * 1.30, 4)
        else:
            stop = request.form.get("stop_loss_price") or None
            t1 = request.form.get("target1_price") or None
            t2 = request.form.get("target2_price") or None
            stop = float(stop) if stop else None
            t1 = float(t1) if t1 else None
            t2 = float(t2) if t2 else None

        conn = get_db()
        cur = conn.execute(
            """INSERT INTO recommendations
               (created_at, ticker, market, framework, entry_price,
                stop_loss_price, target1_price, target2_price,
                signals, conviction, source_type, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (created_at, ticker, market, framework, entry_price,
             stop, t1, t2, json.dumps(signals, ensure_ascii=False),
             conviction, source_type, notes),
        )
        rec_id = cur.lastrowid
        conn.commit()
        conn.close()
        flash(f"已添加 {ticker} 推荐记录 #{rec_id}", "success")
        return redirect(url_for("detail", rec_id=rec_id))

    return render_template("add.html", frameworks=FRAMEWORKS,
                           momentum_signals=MOMENTUM_SIGNALS,
                           fundamental_signals=FUNDAMENTAL_SIGNALS,
                           today=str(date.today()))


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
    snapshots = [dict(s) for s in snapshots]
    return render_template("detail.html", row=row, snapshots=snapshots,
                           frameworks=FRAMEWORKS, today=str(date.today()))


@app.route("/rec/<int:rec_id>/update_price", methods=["POST"])
def update_price(rec_id):
    price = float(request.form.get("price", 0))
    snap_date = request.form.get("snap_date") or str(date.today())

    conn = get_db()
    row = conn.execute("SELECT entry_price FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404

    pct = calc_pct(row["entry_price"], price)

    # Upsert snapshot for this date
    existing = conn.execute(
        "SELECT id FROM price_snapshots WHERE recommendation_id=? AND snapshot_date=?",
        (rec_id, snap_date),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE price_snapshots SET price=?, pct_change=? WHERE id=?",
            (price, pct, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO price_snapshots (recommendation_id, snapshot_date, price, pct_change) VALUES (?,?,?,?)",
            (rec_id, snap_date, price, pct),
        )

    # Auto-update status based on thresholds
    entry = row["entry_price"]
    rec = conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    stop = rec["stop_loss_price"]
    t1 = rec["target1_price"]
    status = rec["status"]
    if status == "active":
        if stop and price <= stop:
            conn.execute(
                "UPDATE recommendations SET status='stopped', close_price=?, close_date=?, close_reason='止损触发' WHERE id=?",
                (price, snap_date, rec_id),
            )
            flash(f"⚠️ 止损触发！已更新状态", "warning")
        elif t1 and price >= t1:
            conn.execute(
                "UPDATE recommendations SET status='target1' WHERE id=?", (rec_id,)
            )
            flash(f"🎯 已达到止盈1目标 (+15%)", "success")

    conn.commit()
    conn.close()
    flash(f"价格已更新：${price:.2f}（{pct:+.1f}%）", "info")
    return redirect(url_for("detail", rec_id=rec_id))


@app.route("/rec/<int:rec_id>/close", methods=["POST"])
def close_rec(rec_id):
    close_price = float(request.form.get("close_price", 0))
    close_date = request.form.get("close_date") or str(date.today())
    close_reason = request.form.get("close_reason", "手动关闭")
    new_status = request.form.get("status", "closed")

    conn = get_db()
    conn.execute(
        "UPDATE recommendations SET status=?, close_price=?, close_date=?, close_reason=? WHERE id=?",
        (new_status, close_price, close_date, close_reason, rec_id),
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
    data = {
        "labels": [row["created_at"]] + [s["snapshot_date"] for s in snaps],
        "prices": [row["entry_price"]] + [s["price"] for s in snaps],
        "pct": [0.0] + [s["pct_change"] for s in snaps],
        "entry": row["entry_price"],
    }
    return jsonify(data)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5050))
    print(f"投资追踪系统启动：http://127.0.0.1:{port}")
    app.run(debug=True, port=port)
