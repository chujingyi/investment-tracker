"""Frozen-dataset historical recommendation performance, not model quality.

Compare mature calendar-day returns and close-only level touches. Agent output
contract evaluation is implemented separately in quality_eval.py.
"""

import json
import hashlib
from datetime import timedelta
from agent import config
from datetime import datetime

WINDOWS = (7, 30, 60)
EVAL_SCOPE = "historical_v2"


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _flt(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def load_candidates(conn):
    """Recommendations with entry price and at least one realized snapshot."""
    rows = conn.execute(
        "SELECT r.* FROM recommendations r "
        "WHERE r.entry_price IS NOT NULL AND r.entry_price > 0 "
        "AND r.entry_rec IN ('actionable','wait','watch') "
        "ORDER BY r.created_at"
    ).fetchall()
    candidates = []
    for row in rows:
        item = dict(row)
        snapshots = conn.execute(
            "SELECT snapshot_date, price FROM price_snapshots "
            "WHERE recommendation_id=? ORDER BY snapshot_date",
            (item["id"],),
        ).fetchall()
        item["_snapshots"] = [
            (_parse_date(s["snapshot_date"]), _flt(s["price"])) for s in snapshots
        ]
        if any(price is not None for _, price in item["_snapshots"]):
            candidates.append(item)
    return candidates


def _after_prices(snapshots, entry_date, days=None):
    """价格序列：entry 之后（可选窗口上限 N 天）内的收盘价列表。"""
    start = _parse_date(entry_date)
    if start is None:
        return []
    prices = []
    for snap_date, price in snapshots:
        if snap_date is None or price is None or snap_date < start:
            continue
        if days is not None and (snap_date - start).days > days:
            break
        prices.append(price)
    return prices


def _window_metrics(snapshots, entry_price, entry_date, days):
    """Calendar-day horizon, only mature samples; last close within 4 days.

    These are recommendation reference-price returns, not executed portfolio P&L.
    """
    start = _parse_date(entry_date)
    valid = sorted((d, p) for d, p in snapshots if d and p is not None and d >= start) if start else []
    end = start + timedelta(days=days) if start else None
    if not valid or valid[-1][0] < end:
        return None
    before = [(d, p) for d, p in valid if d <= end]
    if not before or (end - before[-1][0]).days > 4:
        return None
    last = before[-1][1]
    return {"direction_hit": int(last > entry_price),
            "return_pct": round((last-entry_price)/entry_price*100, 2), "obs": len(before)}


def _hit_level(snapshots, entry_date, level, mode):
    """是否在历史窗口内触及水平（low: 价格 <= level；high: 价格 >= level）。"""
    start = _parse_date(entry_date)
    if start is None:
        return False
    for snap_date, price in snapshots:
        if snap_date is None or price is None or snap_date < start:
            continue
        if mode == "low" and price <= level:
            return True
        if mode == "high" and price >= level:
            return True
    return False


def evaluate(conn, dataset=None):
    candidates = load_candidates(conn) if dataset is None else dataset
    candidates = [{**row, "_snapshots": [(_parse_date(d), p) for d, p in row["_snapshots"]]} for row in candidates]
    result = {
        "sample_count": len(candidates),
        "methodology": "historical_v2: calendar days, mature horizons, close-only touches; no execution/fees",
        "quality_warning": "历史推荐表现，不是模型准确率；收盘价触线不等于盘中触价或实际止损执行",
        "data_as_of": str(max((d for row in candidates for d, _ in row["_snapshots"] if d), default="")),
        "windows": {},
        "stop_hit": {"n": 0, "samples": 0},
        "target_hit": {"n": 0, "samples": 0},
        "trigger_hit": {"n": 0, "samples": 0},
        "by_tier": {},
    }
    for days in WINDOWS:
        hits, returns, obs = 0, [], 0
        for row in candidates:
            entry = _flt(row["entry_price"])
            if not entry:
                continue
            m = _window_metrics(row["_snapshots"], entry, row["created_at"], days)
            if m is None:
                continue
            hits += m["direction_hit"]
            returns.append(m["return_pct"])
            obs += m["obs"]
        result["windows"][str(days)] = {
            "direction_hit_rate": round(hits / len(returns) * 100, 1) if returns else None,
            "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
            "eval_samples": len(returns),
            "excluded_samples": len(candidates) - len(returns),
        }
    for row in candidates:
        entry = _flt(row["entry_price"])
        if not entry:
            continue
        stop = _flt(row.get("stop_loss_price"))
        target = _flt(row.get("target1_price"))
        wait = _flt(row.get("wait_price"))
        if stop:
            result["stop_hit"]["samples"] += 1
            if _hit_level(row["_snapshots"], row["created_at"], stop, "low"):
                result["stop_hit"]["n"] += 1
        if target:
            result["target_hit"]["samples"] += 1
            if _hit_level(row["_snapshots"], row["created_at"], target, "high"):
                result["target_hit"]["n"] += 1
        if wait:
            result["trigger_hit"]["samples"] += 1
            if _hit_level(row["_snapshots"], row["created_at"], wait,
                          "high" if row.get("monitor_direction") == "breakout" else "low"):
                result["trigger_hit"]["n"] += 1
    tiers = {}
    for row in candidates:
        entry = _flt(row["entry_price"])
        tier = row.get("entry_rec") or "watch"
        stats = tiers.setdefault(tier, {"n": 0, "returns": []})
        stats["n"] += 1
        if not entry:
            continue
        m = _window_metrics(row["_snapshots"], entry, row["created_at"], 30)
        if m:
            stats["returns"].append(m["return_pct"])
    result["by_tier"] = {
        tier: {
            "n": stats["n"],
            "avg_return_pct": round(sum(stats["returns"]) / len(stats["returns"]), 2)
            if stats["returns"] else None,
        }
        for tier, stats in tiers.items()
    }
    for key in ("stop_hit", "target_hit", "trigger_hit"):
        if result[key]["samples"]:
            result[key]["hit_rate"] = round(result[key]["n"] / result[key]["samples"] * 100, 1)
    return result


def get_baseline(conn):
    row = conn.execute(
        "SELECT * FROM eval_runs WHERE is_baseline=1 AND scope='historical_v2' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def get_latest_run(conn):
    row = conn.execute("SELECT * FROM eval_runs WHERE scope='historical_v2' ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def save_run(conn, metrics, is_baseline=False, note="", dataset_json=None, dataset_hash=None):
    cur = conn.execute(
        "INSERT INTO eval_runs (scope, sample_count, metrics_json, is_baseline, note, created_at, dataset_json, dataset_hash, version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            EVAL_SCOPE,
            metrics.get("sample_count", 0),
            json.dumps(metrics, ensure_ascii=False),
            1 if is_baseline else 0,
            note,
            _now(), dataset_json, dataset_hash, config.version(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _diff_windows(current_windows, base_windows):
    diff = {}
    for days in WINDOWS:
        key = str(days)
        c = (current_windows.get(key) or {}).get("direction_hit_rate")
        b = (base_windows.get(key) or {}).get("direction_hit_rate")
        diff[key] = round(c - b, 1) if c is not None and b is not None else None
    return diff


def run_eval(conn, fresh=False):
    """执行评测；首次或 --fresh 时写入基线。返回 {current, baseline, diff, run_id}。"""
    baseline = get_baseline(conn)
    if baseline and not fresh and baseline.get("dataset_json"):
        dataset_json = baseline["dataset_json"]
    else:
        dataset_json = json.dumps(load_candidates(conn), ensure_ascii=False, sort_keys=True, default=str)
    dataset_hash = hashlib.sha256(dataset_json.encode()).hexdigest()
    current = evaluate(conn, json.loads(dataset_json))
    current["dataset_hash"] = dataset_hash
    base_metrics = json.loads(baseline["metrics_json"] or "{}") if baseline else {}
    is_baseline = fresh or baseline is None
    note = "重建基线(--fresh)" if fresh else ("首次运行自动建基线" if is_baseline else "")
    run_id = save_run(conn, current, is_baseline=is_baseline, note=note, dataset_json=dataset_json, dataset_hash=dataset_hash)
    diff = {} if is_baseline else {
        "windows": _diff_windows(current["windows"], base_metrics.get("windows") or {})
    }
    return {
        "current": current,
        "baseline": None if is_baseline else base_metrics,
        "diff": diff,
        "run_id": run_id,
        "is_baseline": is_baseline,
    }


def render_report(report):
    current = report["current"]
    lines = [
        f"历史推荐表现（离线评测；{current['sample_count']} 条推荐样本）",
        "── 窗口上涨比例 / 平均收益 ──",
    ]
    for days in WINDOWS:
        w = current["windows"].get(str(days)) or {}
        delta = ""
        if report.get("diff"):
            d = report["diff"]["windows"].get(str(days))
            delta = f"  较基线 {d:+.1f}pp" if d is not None else ""
        lines.append(
            f"  {days:>2}天: 上涨比例 {w.get('direction_hit_rate')}% 平均收益 {w.get('avg_return_pct')}% "
            f"样本 {w.get('eval_samples')}{delta}"
        )
    lines.append("── 风控与触发达成率 ──")
    for label, key in (("收盘价触及止损线", "stop_hit"), ("收盘价触及目标线", "target_hit"), ("收盘价触及等待线", "trigger_hit")):
        item = current.get(key) or {}
        rate = f"（{item['hit_rate']}%）" if item.get("hit_rate") is not None else ""
        lines.append(f"  {label}: {item.get('n', 0)}/{item.get('samples', 0)}{rate}")
    lines.append("── 分档平均收益（30天口径）──")
    for tier, stats in (current.get("by_tier") or {}).items():
        lines.append(f"  {tier}: 样本 {stats['n']} 平均收益 {stats.get('avg_return_pct')}%")
    if report.get("is_baseline"):
        lines.append("（本次已存为基线）")
    else:
        lines.append("（已与基线对比）")
    lines.append(current.get("quality_warning", ""))
    lines.append("数据截至：" + current.get("data_as_of", "") + "；样本集 " + current.get("dataset_hash", "")[:12])
    return "\n".join(lines)