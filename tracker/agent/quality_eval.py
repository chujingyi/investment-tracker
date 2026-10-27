"""Offline contract evaluation of Agent outputs on fixed synthetic evidence.

Does not call providers or claim to evaluate investment returns/semantic truth.
Use real saved model outputs as responses; missing cases count as failures.
"""
import hashlib
import json
from pathlib import Path

CASES = Path(__file__).with_name("evaluation_cases.json")


def evaluate_outputs(responses, cases_path=CASES):
    raw = Path(cases_path).read_text()
    cases = json.loads(raw)
    if not isinstance(responses, list):
        raise ValueError("responses 必须为 JSON 数组")
    indexed = {}
    for response in responses:
        key = response.get("case_id")
        if key in indexed:
            raise ValueError(f"重复 case_id: {key}")
        indexed[key] = response
    details = []
    for case in cases:
        answer = indexed.get(case["id"])
        errors = []
        if answer is None:
            errors.append("missing_response")
        else:
            if answer.get("verdict") != case["expected_verdict"]:
                errors.append("verdict")
            cited = answer.get("evidence_ids")
            if not isinstance(cited, list) or not all(isinstance(x, str) for x in cited):
                errors.append("evidence_format")
            else:
                if set(cited) - set(case["allowed_evidence"]):
                    errors.append("unknown_evidence")
                if set(case["required_evidence"]) - set(cited):
                    errors.append("missing_evidence")
            if answer.get("facts", {}) != case["expected_facts"]:
                errors.append("facts")
        details.append({"case_id": case["id"], "passed": not errors, "errors": errors})
    return {
        "scope": "agent_output_contract", "dataset_hash": hashlib.sha256(raw.encode()).hexdigest(),
        "cases": len(cases), "passed": sum(x["passed"] for x in details), "details": details,
        "warning": "仅验证固定案例的结论/引用/事实契约；解释质量与证据推理仍需人工复核，不衡量投资收益",
    }
