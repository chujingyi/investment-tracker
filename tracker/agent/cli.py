"""Command-line helpers for the agent package.

Usage (from the tracker/ directory):
    python -m agent.cli ingest   # (re)build the knowledge chunk index
    python -m agent.cli smoke     # config/context/tool/FTS checks without an LLM call
"""

import json
import sys

from agent import config, knowledge


def _get_conn():
    from app import get_db, init_db

    init_db()
    return get_db()


def cmd_ingest():
    conn = _get_conn()
    try:
        result = knowledge.ingest_all(conn)
        total = knowledge.chunk_count(conn)
        print(
            f"ingest ok: {result['framework_chunks']} framework chunks, "
            f"{result['kb_chunks']} kb chunks, total {total} in knowledge_chunks"
        )
        sample = knowledge.retrieve(conn, "供应链 瓶颈", k=2)
        for item in sample:
            print(f"  sample hit: {item['title']} <{item['section'] or '开头'}>")
        return 0
    finally:
        conn.close()


def cmd_smoke():
    conn = _get_conn()
    failures = []
    try:
        knowledge.ingest_all(conn)
        total = knowledge.chunk_count(conn)
        print(f"[1] knowledge chunks: {total}")
        if total <= 0:
            failures.append("no knowledge chunks")

        from agent import context

        block = context.build_context(conn)
        print(f"[2] context block: {len(block)} chars")
        if "风控与仓位规则" not in block:
            failures.append("context missing risk rules")

        from agent import tools

        specs = tools.tool_definitions(include_write=True)
        print(f"[3] tool definitions: {len(specs)} (write tools: "
              f"{sum(1 for s in specs if tools.is_write(s['function']['name']))})")
        if len(specs) != 10:
            failures.append(f"expected 10 tools, got {len(specs)}")

        portfolio = tools.run_read(conn, "query_portfolio", {})
        print(f"[4] query_portfolio: {portfolio['summary']}")
        recs = tools.run_read(conn, "query_recommendations", {"limit": 3})
        print(f"[5] query_recommendations: {recs['summary']}")
        cal = tools.run_read(conn, "get_market_calendar", {"days": 7})
        print(f"[6] get_market_calendar: {cal['summary']}")

        errors = tools.validate_write(
            "propose_recommendation",
            {"ticker": "TEST", "entry_rec": "actionable"},
        )
        print(f"[7] decision-gate validation (bad args) -> {len(errors)} errors: {errors[:2]}")
        if not errors:
            failures.append("validation should have failed on incomplete actionable rec")

        try:
            tools.run_read(conn, "get_quotes", {"symbols": ["MU"]})
            print("[7b] get_quotes: executed (source depends on Longbridge env)")
        except Exception as exc:
            failures.append(f"get_quotes raised: {exc}")

        print(f"[8] LLM configured: {config.is_configured()} "
              f"(base_url={config.llm_base_url()}, model={config.llm_model()})")
    finally:
        conn.close()

    if failures:
        print("\nSMOKE FAILED:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nSMOKE OK")
    return 0


def cmd_eval():
    conn = _get_conn()
    try:
        from agent import eval as agent_eval

        fresh = "--fresh" in sys.argv[1:]
        report = agent_eval.run_eval(conn, fresh=fresh)
        print(agent_eval.render_report(report))
        return 0
    finally:
        conn.close()


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    command = argv[0] if argv else "smoke"
    if command == "doctor":
        from pathlib import Path
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
        from agent.preflight import check
        report = check(network="--network" in argv)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if "--network" in argv:
            ready = report["database"].get("schema", {}).get("ready", False)
            return 0 if ready and report["longbridge"].get("connected", False) else 1
        return 0
    if command == "ingest":
        return cmd_ingest()
    if command == "smoke":
        return cmd_smoke()
    if command == "eval":
        return cmd_eval()
    if command == "quality-eval":
        from agent.quality_eval import evaluate_outputs
        if len(argv) != 2:
            print("usage: python -m agent.cli quality-eval responses.json")
            return 2
        from pathlib import Path
        report = evaluate_outputs(json.loads(Path(argv[1]).read_text()))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] == report["cases"] else 1
    print(f"unknown command: {command} (use: ingest | smoke | eval)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
