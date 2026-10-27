#!/usr/bin/env python3
"""解析 longbridge 行情数据并更新数据库（修复 ticker 匹配问题）"""

import sys, re
sys.path.insert(0, '.')

from datetime import date
from app import get_db, init_db

# longbridge 获取的最新价格 (2026-07-01)
QUOTES = {
    "HOOD": 100.280,
    "JOBY": 8.920,
    "AVAV": 165.070,
    "COIN": 146.190,
    "TSLA": 420.600,
    "RKLB": 101.650,
    "ASTS": 88.860,
    "CEG": 248.370,
    "AMKR": 86.230,
    "PLAB": 32.530,
    "BE": 302.700,
    "COHR": 394.470,
    "VRT": 334.820,
    "LITE": 858.060,
    "SPCX": 170.860,
    "SATS": 103.915,
    "LUNR": 21.390,
    "IRDM": 54.850,
    "QQQM": 302.970,
    "MTSI": 380.370,
    "VECO": 75.800,
    "MU": 1154.290,
    "FN": 562.080,
    "MRVL": 297.890,
    "ENTG": 179.860,
    "FORM": 159.930,
    "ACLS": 189.450,
    "PWR": 720.040,
    "1810": 21.640,
    "688017": 423.98,
}

def normalize_ticker(t):
    """去掉 .US/.HK/.SZ 等后缀，统一匹配"""
    return re.sub(r'\.[A-Z]+$', '', t).upper()

def main():
    init_db()
    conn = get_db()
    today = str(date.today())

    # 建立数据库中 ticker 的索引（标准化后）
    rows = conn.execute(
        "SELECT id, ticker, entry_price, status, framework, conviction FROM recommendations"
    ).fetchall()
    db_map = {}
    for r in rows:
        key = normalize_ticker(r["ticker"])
        if key not in db_map:
            db_map[key] = []
        db_map[key].append(dict(r))

    updated = 0
    results = []
    unmatched = []

    for q_ticker, last_price in QUOTES.items():
        key = normalize_ticker(q_ticker)
        if key not in db_map:
            unmatched.append(q_ticker)
            continue

        for rec in db_map[key]:
            rec_id = rec["id"]
            entry = rec["entry_price"] or 0
            pct = round((last_price - entry) / entry * 100, 2) if entry else None

            existing = conn.execute(
                "SELECT id FROM price_snapshots WHERE recommendation_id=? AND snapshot_date=?",
                (rec_id, today)
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE price_snapshots SET price=?, pct_change=? WHERE id=?",
                    (last_price, pct, existing["id"])
                )
            else:
                conn.execute(
                    "INSERT INTO price_snapshots (recommendation_id, snapshot_date, price, pct_change) VALUES (?,?,?,?)",
                    (rec_id, today, last_price, pct)
                )

            results.append({
                "id": rec_id,
                "ticker": rec["ticker"],
                "entry": entry,
                "last": last_price,
                "pct": pct,
                "status": rec["status"],
                "framework": rec["framework"],
                "conviction": rec["conviction"],
            })
            updated += 1

    conn.commit()
    conn.close()

    if unmatched:
        print(f"未匹配的 ticker: {unmatched}")

    print(f"共更新 {updated} 条记录\n")
    print("=" * 70)

    # 按盈亏排序
    results.sort(key=lambda x: x["pct"] if x["pct"] is not None else -999)
    print(f"{'#':<5} {'股票':<10} {'框架':<14} {'入场':<10} {'最新':<10} {'盈亏':<12} {'状态':<8}")
    print("-" * 70)
    for r in results:
        pct_str = f"{r['pct']:+.2f}%" if r["pct"] else "N/A"
        print(f"{r['id']:<5} {r['ticker']:<10} {r['framework']:<14} {r['entry']:<10.2f} {r['last']:<10.2f} {pct_str:<12} {r['status']:<8}")

    # 统计
    valid = [r for r in results if r["pct"] is not None]
    if valid:
        avg_pct = sum(r["pct"] for r in valid) / len(valid)
        profit_cnt = sum(1 for r in valid if r["pct"] > 0)
        loss_cnt = sum(1 for r in valid if r["pct"] < 0)
        active_cnt = sum(1 for r in results if r["status"] == "active")

        print("-" * 70)
        print(f"平均盈亏: {avg_pct:+.2f}%")
        print(f"盈利: {profit_cnt} 只 | 亏损: {loss_cnt} 只 | 进行中: {active_cnt} 只")

        # 按框架分组
        print("\n按框架统计:")
        from collections import defaultdict
        fw_stats = defaultdict(list)
        for r in valid:
            fw_stats[r["framework"]].append(r["pct"])
        for fw, pcts in sorted(fw_stats.items()):
            avg = sum(pcts) / len(pcts)
            prof = sum(1 for p in pcts if p > 0)
            print(f"  {fw:<14}: 平均 {avg:+.2f}%, 盈利 {prof}/{len(pcts)}")

    return results

if __name__ == "__main__":
    main()
