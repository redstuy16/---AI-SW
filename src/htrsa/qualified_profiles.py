"""한정된 연구 절차의 선택·검증 경계. 일반 연구에 분야 필드를 요구하지 않는다."""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Protocol

from .database import to_json


def fingerprint(value):
    return sha256(to_json(value).encode("utf-8", errors="strict")).hexdigest()


class QualifiedResearchProfile(Protocol):
    profile_id: str
    display_name: str
    qualified: bool

    def candidate(self, question: str) -> dict | None: ...
    def checks(self, state, payload, binding: dict) -> list[dict]: ...


class ProfileRegistry:
    def __init__(self):
        self.profiles = {}

    def register(self, profile):
        if profile.profile_id in self.profiles:
            raise ValueError("PROFILE_ALREADY_REGISTERED")
        self.profiles[profile.profile_id] = profile

    def choose(self, question):
        candidates = [(p, p.candidate(question)) for p in self.profiles.values() if p.qualified]
        matches = [(p, c) for p, c in candidates if c is not None]
        if len(matches) != 1:
            return {"status": "GENERAL" if not matches else "CLARIFICATION_REQUIRED", "original_question": question}
        profile, candidate = matches[0]
        return dict(candidate, profile_id=profile.profile_id, original_question=question)

    def public(self):
        return [{"profile_id": p.profile_id, "name": p.display_name} for p in self.profiles.values() if p.qualified]


def registry():
    from .climate_profile import ClimateComparisonProfile
    result = ProfileRegistry()
    result.register(ClimateComparisonProfile())
    return result


def verify_profile(state, payload):
    binding = payload.agent_result.provenance.get("qualified_profile")
    if not binding:
        contract, _ = state.contract(payload.agent_result.contract_id)
        if contract.task_type == "QualifiedProfileWorker":
            return [{"check_id": "QUALIFIED_PROFILE_BINDING", "passed": False, "message": "연구 프로필 연결이 누락되었습니다."}]
        return []
    checks = []
    try:
        reserved = {"reviewed_by", "owner_approved", "approved_by", "qualification", "profile_qualified", "secondary_required", "source_policy"}
        checks.append({"check_id": "PROFILE_WORKER_AUTHORITY", "passed": not (reserved & payload.agent_result.provenance.keys()) and
                       set(binding) == {"step_key", "fingerprint", "profile_id"},
                       "message": "Worker 출력에서 소유자 승인·검증 자격·필수 대조 정책을 지정할 수 없습니다."})
        saved = state.runtime_step(payload.agent_result.research_id, binding["step_key"])
        trusted = saved["output"] if saved and saved["status"] == "COMPLETED" else None
        valid = trusted is not None and fingerprint(trusted) == binding["fingerprint"] and trusted["plan"]["profile_id"] == binding["profile_id"]
        checks.append({"check_id": "QUALIFIED_PROFILE_BINDING", "passed": valid, "message": "저장된 질문·절차·출처에 연결됩니다."})
        profile = registry().profiles[binding["profile_id"]]
        if valid:
            checks.extend(profile.checks(state, payload, trusted))
    except (KeyError, TypeError, ValueError, OSError, UnicodeError):
        checks.append({"check_id": "QUALIFIED_PROFILE_BINDING", "passed": False, "message": "연구 프로필 근거가 없거나 손상되었습니다."})
    return checks


def current_record(state, rid):
    rows = state._db.execute("SELECT output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'qualified_result:%' AND status='COMPLETED' ORDER BY rowid DESC", (rid,)).fetchall()
    return json.loads(rows[0][0]) if rows else None


def conclusion_card(state, rid):
    record = current_record(state, rid)
    if not record:
        return {"available": False}
    row = state._one("SELECT payload_json FROM staged_mutations WHERE research_id=? AND mutation_id=? AND status='COMMITTED'", (rid, record["mutation_id"]))
    from .schemas import StagedResult
    payload = StagedResult.model_validate_json(row[0])
    checks = verify_profile(state, payload)
    from .research_design import verify_design
    checks += verify_design(state, payload)
    claim = next((c for c in state.research_slice.current(rid) if c.claim_id == record["claim_id"]), None)
    valid = bool(checks) and all(c["passed"] for c in checks) and claim is not None and claim.support_state != "NEEDS_REVALIDATION"
    if valid:
        try:
            state.research_slice.validate(claim, state.research_slice.bindings(claim))
        except (ValueError, OSError):
            valid = False
    from .qualified_workflow import latest_authority, latest_source
    authority = latest_authority(state, rid)
    history_records = [json.loads(r[0]) for r in state._db.execute("SELECT output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'qualified_result:%' AND status='COMPLETED' ORDER BY rowid DESC LIMIT 20", (rid,))]
    before = history_records[1] if len(history_records) > 1 else None
    changes = None
    if before:
        changes = {"kind": "QUESTION_SELECTION_AMENDMENT" if before["question"] != record["question"] else "SOURCE_RECALCULATION",
                   "old_question": before["question"], "new_question": record["question"], "groups": {}, "values": []}
        for group in ("A", "B"):
            old_keys = before.get("selection", {}).get(group, [])
            new_keys = record.get("selection", {}).get(group, [])
            changes["groups"][group] = {"before": old_keys, "after": new_keys,
                "added": [k for k in new_keys if k not in old_keys], "removed": [k for k in old_keys if k not in new_keys]}
        for name, value in record.get("reported_values", {}).items():
            old_value = before.get("reported_values", {}).get(name)
            changes["values"].append({"name": name, "before": old_value, "after": value, "changed": old_value != value})
    representation = next((c["details"] for c in reversed(checks) if c["check_id"] in {"PROFILE_REPRESENTATION_POLICY", "PROFILE_CURRENT_REPRESENTATION"}), record.get("representation"))
    pending_question = authority["plan"]["question"] if authority and authority["plan"]["question"] != record["question"] else None
    return {"available": True, "question": record["question"], "source": record["source_description"],
            "calculation": record["calculation"], "scope": record["scope"], "unconfirmed": record["unconfirmed"],
            "current": valid, "currentness": "현재 결론" if valid else "다시 확인 필요",
            "message": "" if valid else ("사용한 자료가 바뀌어 이 결론을 다시 확인해야 합니다. 이전 결론은 변경 이력에 남습니다." if latest_source(state, rid) and not pending_question else "질문·자료·검증 조건을 다시 확인해야 합니다. 이전 결론은 변경 이력에 남습니다."),
            "claim_id": record["claim_id"], "revision": claim.revision if claim else None,
            "history": [dict(r) for r in state._db.execute("SELECT revision,current,payload_json FROM claim_revisions WHERE research_id=? AND claim_id=? ORDER BY revision", (rid, record["claim_id"]))],
            "checks": checks, "record": record, "changes": changes, "representation": representation,
            "pending_question": pending_question, "original_question": authority["plan"]["original_question"] if authority else record["question"],
            "state_version": state.state_version(rid),
            "analysis_history": [dict(r, historical=i > 0 or not valid) for i, r in enumerate(history_records)],
            "source_changed": bool(latest_source(state, rid)), "live_efficacy": "NOT_VALIDATED"}


def source_inspection(state, rid, *, include_rows=False):
    """고정된 근거만 조회한다. 행은 명시적으로 펼칠 때 읽으며 네트워크·모델 호출은 없다."""
    from .qualified_workflow import latest_authority, latest_source
    from .climate_profile import parse_source, selected_rows, representation_check, RESIDUAL_LIMIT, read_capture
    record = current_record(state, rid)
    if not record:
        return {"available": False, "paid_calls": 0}
    payload = json.loads(state._one("SELECT payload_json FROM staged_mutations WHERE research_id=? AND mutation_id=?", (rid, record["mutation_id"]))[0])
    binding = state.runtime_step(rid, payload["agent_result"]["provenance"]["qualified_profile"]["step_key"])["output"]
    authority = latest_authority(state, rid)
    plan = authority["plan"] if authority else binding["plan"]
    source = latest_source(state, rid) or binding
    def capture(item):
        return {k: item.get(k) for k in ("source_sha256", "url", "semantics", "capture", "hash_scope", "retrieved_at", "stored_at", "documentation", "documentation_status")}
    result = {"available": True, "original_question": plan.get("original_question", plan["question"]),
              "current_question": plan["question"], "plan": plan, "primary": capture(source),
              "secondary": capture(source["secondary"]) if source.get("secondary") else None,
              "representation": representation_check(state, rid, source, plan), "limitation": RESIDUAL_LIMIT,
              "paid_calls": 0, "network_calls": 0, "rows_loaded": False}
    if include_rows:
        try:
            rows = parse_source(read_capture(state, rid, source), source["semantics"])
            selected = selected_rows(rows, plan["periods"])
            result.update(rows=selected[:200], selected_count=len(selected), truncated=len(selected) > 200, rows_loaded=True)
        except (ValueError, KeyError, OSError, UnicodeError) as error:
            result["row_error"] = getattr(error, "code", "SOURCE_REVIEW_REQUIRED")
    return result


def export_bundle(state, rid):
    card = conclusion_card(state, rid)
    if not card["available"]:
        return None
    if not card["current"]:
        raise ValueError("QUALIFIED_PROFILE_REVALIDATION_REQUIRED")
    records = [json.loads(r[0]) for r in state._db.execute("SELECT output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'qualified_analysis:%:binding' ORDER BY rowid", (rid,))]
    return {"schema_version": "1", "card": card, "bindings": records,
            "authority": __import__("htrsa.qualified_workflow", fromlist=["latest_authority"]).latest_authority(state, rid),
            "replay": {"method": "two_period_comparison", "paid_llm_required": False,
                       "command": "python -m htrsa.qualified_replay replay_manifest.json"},
            "current_source": __import__("htrsa.qualified_workflow", fromlist=["latest_source"]).latest_source(state, rid)}
