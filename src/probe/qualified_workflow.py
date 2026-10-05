"""한정 절차를 기존 AgentRuntime·도구·검증·결론 그래프에 연결한다."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Literal

import httpx

from .climate_profile import (MEANING, CSV_MEANING, SOURCE_POLICY, CHECKER_VERSION, RESIDUAL_LIMIT,
                             SOURCE_URL, SECONDARY_URL, DOC_URL, parse_source, selected_rows,
                             transform_rows, normalized_csv, claim_text, dependency_hash, representation_check, read_capture)
from .control_plane import ControlError
from .qualified_profiles import registry, fingerprint, current_record, conclusion_card
from .schemas import (StrictModel, AgentResult, NumericProvenance, ScientificMutation,
                      StagedResult, new_id, ContextRef, RefType, utc_now)
from .scientific_verifier import numeric_fields
from .storage import sha256_bytes


def archive_report(state, rid):
    from .report_publication import report_root
    root = report_root(state, rid)
    manifest = root / "manifests/artifact_manifest.json"
    if not manifest.is_file():
        return
    document = json.loads(manifest.read_text(encoding="utf-8", errors="strict"))
    if not isinstance(document, dict) or not isinstance(document.get("files"), dict):
        raise ControlError("PROFILE_HISTORY_INVALID")
    version = document.get("state_version")
    entries = document["files"]
    if len(entries) > 300 or type(version) is not int:
        raise ControlError("PROFILE_HISTORY_INVALID")
    from .storage import sha256_file
    for relative, expected in entries.items():
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or source.stat().st_size > 10_000_000 or sha256_file(source) != expected:
            raise ControlError("PROFILE_HISTORY_HASH_MISMATCH")
        target = state.workspace.path(rid, f"report_history/v{version}-{sha256_bytes(manifest.read_bytes())[:12]}/" + relative)
        data = source.read_bytes()
        if target.exists() and target.read_bytes() != data:
            raise ControlError("PROFILE_HISTORY_CONFLICT")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


class ProfileDecision(StrictModel):
    decision: Literal["PROCEED", "CLARIFY", "HOLD"]
    reason: str


class CapturedText(str):
    """텍스트 인터페이스를 유지하면서 실제 관측한 HTTP 정보만 전달한다."""
    def __new__(cls, text, capture):
        result = super().__new__(cls, text)
        result.capture = capture
        return result


def _source(state, rid, text, *, semantics=None, secondary=False):
    data = text.encode("utf-8", errors="strict")
    from .release import _secret_free
    if not _secret_free("profile-source.txt", data):
        raise ControlError("PROFILE_SOURCE_SECRET_BLOCKED")
    if len(data) > 1_000_000:
        raise ControlError("PROFILE_SOURCE_LIMIT")
    metadata = deepcopy((CSV_MEANING if secondary else MEANING) if semantics is None else semantics)
    relative = "inputs/profile_sources/" + sha256_bytes(data) + (".csv" if secondary else ".txt")
    path = state.workspace.path(rid, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != data:
        raise ControlError("SOURCE_HASH_MISMATCH")
    if not path.exists():
        path.write_bytes(data)
    capture = deepcopy(getattr(text, "capture", {"kind": "LOCAL_SUPPLIED", "http_observed": False}))
    if not _secret_free("capture.json", json.dumps(capture, ensure_ascii=False).encode("utf-8", errors="strict")):
        raise ControlError("PROFILE_SOURCE_SECRET_BLOCKED")
    return {"source_relative": relative, "source_sha256": sha256_bytes(data),
            "semantics": metadata, "url": SECONDARY_URL if secondary else SOURCE_URL, "documentation": DOC_URL,
            "documentation_status": "PROFILE_RECORDED_URL_HTML_NOT_CAPTURED", "capture": capture,
            "hash_scope": "STORED_UTF8_TEXT_AFTER_TRANSPORT_DECODING", "stored_at": utc_now().isoformat(),
            "retrieved_at": capture.get("retrieved_at")}


async def _fetch_text(url):
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            capture = {"kind": "HTTP_OBSERVED", "http_observed": True, "url": url,
                       "status_code": response.status_code, "retrieved_at": utc_now().isoformat(),
                       "headers": {k: response.headers[k] for k in ("etag", "last-modified", "content-type", "content-encoding") if k in response.headers}}
            from .bounded_http import bounded_bytes, ResponseLimitError
            try:
                data = await bounded_bytes(response, 1_000_000)
            except ResponseLimitError as exc:
                raise ControlError("PROFILE_SOURCE_LIMIT" if exc.code == "RESPONSE_TOO_LARGE" else "PROFILE_SOURCE_ENCODING_INVALID") from None
    return CapturedText(data.decode("utf-8", errors="strict"), capture)


async def fetch_source(snapshot):
    if snapshot.get("egress") == "none":
        raise ControlError("DESTINATION_APPROVAL_REQUIRED")
    # 고정 공개 URL만 읽으며 질문·첨부·키는 전송하지 않는다.
    return await _fetch_text(SOURCE_URL)


async def fetch_secondary(state, rid, snapshot, *, before_dispatch=None, credentials=None):
    """승인된 검색 정책·공통 횟수·완료 예산 안에서 고정 공개 CSV만 읽는다."""
    from .control_plane import ControlStore, Credentials
    from .search_policy import decision, search_allocation
    from .product_policy import completion_budget, effective_cap
    from decimal import Decimal
    from pathlib import Path
    store = ControlStore(state._db)
    task = dict(snapshot, search_required=True)
    try:
        credentials = credentials or Credentials(Path(__file__).resolve().parents[2], state.workspace.root)
        protected = credentials.active_secrets(c.get("credential_env_name") for c in store.configs("connection"))
    except (ControlError, OSError):
        store.audit(rid, "QUALIFIED_SECONDARY_SKIPPED", {"status": "SOURCE_SECRET_BOUNDARY_UNAVAILABLE", "paid_calls": 0})
        return None
    status = decision(task, protected=protected)
    if status != "SEARCH_ALLOWED":
        store.audit(rid, "QUALIFIED_SECONDARY_SKIPPED", {"status": status, "paid_calls": 0})
        return None
    view = completion_budget(store, rid, snapshot)
    allocation = search_allocation(store, rid, snapshot, price=Decimal(0), price_source="PUBLIC_NASA_HTTP_DOWNLOAD", view=view)
    if not allocation["allowed_attempts"]:
        store.audit(rid, "QUALIFIED_SECONDARY_SKIPPED", {"status": allocation["status"], "paid_calls": 0})
        return None
    reserve = Decimal(view["completion_reserve_usd"] or "0") if snapshot.get("adaptive_budget") else Decimal(0)
    reservation = store.reserve(rid=rid, connection="nasa-public", model="fixed-public-csv", role="search", purpose="source_acquisition",
        bound=Decimal(0), run_limit=effective_cap(store, rid, snapshot),
        monthly_limit=min(store.defaults().monthly_limit_usd, Decimal(snapshot["monthly_limit_usd"])),
        request_limit=min(store.defaults().request_limit_usd, Decimal(snapshot["request_limit_usd"])),
        attempts=snapshot["depth_limits"]["attempts"], revision="PUBLIC_NASA_HTTP_DOWNLOAD", completion_reserve=reserve)
    dispatched = False
    try:
        if before_dispatch:
            before_dispatch()
        store.dispatch_search(reservation, rid, allocation["used"] + allocation["allowed_attempts"], "nasa-public", snapshot=snapshot, completion_reserve=reserve)
        dispatched = True
        return await _fetch_text(SECONDARY_URL)
    except (httpx.HTTPError, UnicodeError, ControlError) as error:
        store.audit(rid, "QUALIFIED_SECONDARY_UNAVAILABLE", {"error_type": type(error).__name__, "paid_calls": 0})
        return None
    finally:
        store.transition(reservation, "SETTLED" if dispatched else "RELEASED", settled=0 if dispatched else None)


def _dependency(rows, plan):
    return dependency_hash(rows, plan)


def latest_authority(state, rid):
    row = state._db.execute("SELECT step_key,output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'qualified_authority:%' AND status='COMPLETED' ORDER BY rowid DESC LIMIT 1", (rid,)).fetchone()
    return {"step_key": row[0], **json.loads(row[1])} if row else None


def amend_question(state, rid, question, *, expected_version):
    question.encode("utf-8", errors="strict")
    if not question.strip() or len(question) > 4000:
        raise ControlError("PROFILE_QUESTION_INVALID")
    if type(expected_version) is not int or state.state_version(rid) != expected_version:
        raise ControlError("PROFILE_CONTEXT_STALE")
    authority = latest_authority(state, rid)
    if not authority or not current_record(state, rid):
        raise ControlError("PROFILE_RESULT_REQUIRED")
    candidate = registry().choose(question)
    if candidate["status"] != "SUPPORTED" or candidate["profile_id"] != authority["plan"]["profile_id"]:
        raise ControlError("PROFILE_" + candidate["status"])
    old = authority["plan"]
    plan = dict(old, question=question, periods=candidate["periods"], transform=candidate["transform"],
                question_revision=old["question_revision"] + 1, checker_version=CHECKER_VERSION)
    document = {"plan": plan, "reviewed_by": "owner", "previous_question": old["question"],
                "amendment_kind": "QUESTION_SELECTION_AMENDMENT", "approved_at": utc_now().isoformat()}
    key = "qualified_authority:" + str(plan["question_revision"])
    version = state.record_qualified_revision(rid, "QUALIFIED_QUESTION_AMENDED", key, document,
        expected_version=expected_version, question=question, affected=True)
    return {"question": question, "original_question": plan["original_question"], "state_version": version,
            "question_revision": plan["question_revision"], "status": "NEEDS_REVALIDATION", "paid_calls": 0}


def _pair_source(state, rid, text, secondary_text=None, *, semantics=None, secondary_semantics=None):
    source = _source(state, rid, text, semantics=semantics)
    source["secondary"] = _source(state, rid, secondary_text, semantics=secondary_semantics, secondary=True) if secondary_text is not None else None
    group = fingerprint([source["source_sha256"], source["secondary"]["source_sha256"] if source["secondary"] else None])
    source["capture_group"] = group
    if source["secondary"]:
        source["secondary"]["capture_group"] = group
    return source


def latest_source(state, rid):
    row = state._db.execute("SELECT output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'qualified_source_current:%' AND status='COMPLETED' ORDER BY rowid DESC LIMIT 1", (rid,)).fetchone()
    return json.loads(row[0]) if row else None


def update_source(state, rid, text, *, semantics=None, secondary_text=None, secondary_semantics=None):
    record = current_record(state, rid)
    if not record:
        raise ControlError("PROFILE_RESULT_REQUIRED")
    payload = StagedResult.model_validate_json(state._one("SELECT payload_json FROM staged_mutations WHERE mutation_id=?", (record["mutation_id"],))[0])
    old = state.runtime_step(rid, payload.agent_result.provenance["qualified_profile"]["step_key"])["output"]
    version = state.state_version(rid)
    new = _pair_source(state, rid, text, secondary_text, semantics=semantics, secondary_semantics=secondary_semantics)
    plan = (latest_authority(state, rid) or old)["plan"]
    try:
        rows = parse_source(text, new["semantics"])
        new["used_rows_hash"] = _dependency(rows, plan)
        new["status"] = "SUPPORTED"
        audit = representation_check(state, rid, new, plan)
        new["representation"] = audit
        if not audit["eligible"]:
            new["status"] = audit["status"]
    except ValueError:
        new["used_rows_hash"] = None
        new["status"] = "CLARIFICATION_REQUIRED"
    new["semantic_hash"] = fingerprint(new["semantics"])
    index = state._db.execute("SELECT COUNT(*) FROM runtime_steps WHERE research_id=? AND step_key LIKE 'qualified_source_current:%'", (rid,)).fetchone()[0] + 1
    key = f"qualified_source_current:{index}:" + fingerprint(new)
    changed = new["used_rows_hash"] != old["used_rows_hash"] or new["semantic_hash"] != fingerprint(old["plan"]["semantics"]) or new["status"] != "SUPPORTED"
    state.record_qualified_revision(rid, "QUALIFIED_SOURCE_REVIEWED", key, new, expected_version=version, affected=changed)
    return {"affected": changed, "status": new["status"], "paid_calls": 0, "source": new}


async def execute_profile(runtime, rid, snapshot, *, source_text=None, secondary_text=None, replay=False, fault=None):
    state = runtime.state
    question_row = state._one("SELECT goal,research_question FROM research_runs WHERE research_id=?", (rid,))
    question = question_row["research_question"] or question_row["goal"]
    candidate = registry().choose(question)
    if candidate["status"] != "SUPPORTED":
        raise ControlError("PROFILE_" + candidate["status"])
    from .research_slice import CONFIG_KEY
    config = state.research_slice.config(rid)
    if state.runtime_step(rid, CONFIG_KEY):
        if not config.claim_evidence_provenance or not config.revision_invalidation:
            raise ControlError("PROFILE_POLICY_INCOMPATIBLE")
    else:
        config = config.model_copy(update={"claim_evidence_provenance":True,"revision_invalidation":True})
    state.research_slice.configure(rid, config)
    if not state._db.execute("SELECT 1 FROM research_budgets WHERE research_id=?", (rid,)).fetchone():
        cap = float(snapshot["run_limit_usd"])
        state.configure_budget(rid, cap * .25, cap * .75, cap)
    previous = current_record(state, rid)
    if previous and not replay:
        if conclusion_card(state, rid)["current"]:
            return previous
        raise ControlError("PROFILE_REVALIDATION_REQUIRED")
    if previous:
        archive_report(state, rid)
    old_binding = None
    if previous:
        old = StagedResult.model_validate_json(state._one("SELECT payload_json FROM staged_mutations WHERE mutation_id=?", (previous["mutation_id"],))[0])
        old_binding = state.runtime_step(rid, old.agent_result.provenance["qualified_profile"]["step_key"])["output"]
    current = (latest_source(state, rid) or old_binding) if replay else None
    if current and current.get("status", "SUPPORTED") != "SUPPORTED":
        raise ControlError("PROFILE_CLARIFICATION_REQUIRED")
    index = len(list(state._db.execute("SELECT 1 FROM runtime_steps WHERE research_id=? AND step_key LIKE 'qualified_result:%'", (rid,)))) + 1
    prefix = f"qualified_analysis:{index}"
    source_step = state.runtime_step(rid, prefix + ":source")
    if source_step:
        source = source_step["output"]
    else:
        if current:
            source = current
        else:
            text = source_text if source_text is not None else await fetch_source(snapshot)
            alternate = secondary_text
            if source_text is None and alternate is None and getattr(text, "capture", {}).get("http_observed") is True:
                alternate = await fetch_secondary(state, rid, snapshot, before_dispatch=getattr(runtime, "control_boundary", None),
                                                  credentials=getattr(runtime.provider, "credentials", None))
            source = _pair_source(state, rid, text, alternate)
        state.finish_runtime_step(rid, prefix + ":source", source)
    raw = read_capture(state, rid, source)
    rows = parse_source(raw, source["semantics"])
    authority = latest_authority(state, rid)
    if authority:
        plan = authority["plan"]
        if question != plan["question"]:
            raise ControlError("PROFILE_QUESTION_REVIEW_REQUIRED")
    else:
        plan = {"profile_id": candidate["profile_id"], "question": question, "original_question": question_row["goal"],
                "question_revision": 1, "periods": candidate["periods"], "method": "two_period_comparison",
                "semantics": source["semantics"], "transform": candidate["transform"],
                "source_policy": deepcopy(SOURCE_POLICY), "checker_version": CHECKER_VERSION}
        authority = {"step_key": "qualified_authority:1", "plan": plan, "reviewed_by": "profile"}
        state.finish_runtime_step(rid, authority["step_key"], {k: v for k, v in authority.items() if k != "step_key"})
    from .research_design import check_profile_design
    plan = check_profile_design(state, rid, plan)
    transformed = transform_rows(rows, plan["transform"])
    selected_rows(transformed, plan["periods"])
    representation = representation_check(state, rid, source, plan)
    state.finish_runtime_step(rid, prefix + ":representation", representation)
    if not representation["eligible"]:
        raise ControlError("PROFILE_" + representation["status"])
    if not replay:
        manager, _ = runtime._role_contract(rid, "manager", "원래 질문과 고정된 자료·절차를 검토하세요. 지원 범위를 벗어나면 HOLD, 필요한 의미가 불명확하면 CLARIFY를 반환하세요. 실행 가능한 경우에만 PROCEED를 반환하세요.\n" + json.dumps(plan, ensure_ascii=False),
            "QualifiedProfileDecision", runtime_key=prefix + ":manager")
        decision = await runtime._model_once(manager, ProfileDecision, prefix + ":decision")
        runtime._complete_task(manager.contract_id)
        if decision.decision != "PROCEED":
            state.runtime_event(rid, "QUALIFIED_AGENT_REVIEW_REQUIRED", decision.model_dump(mode="json"))
            raise ControlError("PROFILE_CLARIFICATION_REQUIRED")
    else:
        state.runtime_event(rid, "QUALIFIED_LOCAL_RECALCULATION", {"paid_calls": 0, "method_changed": False})
    coordinator, coordinator_task = runtime._role_contract(rid, "experiment_coordinator", "공식 자료를 고정하고 분석 입력을 확인합니다.",
        "QualifiedProfileImport", allowed_tools=["data.import", "data.profile"], max_tool_calls=2, runtime_key=prefix + ":import")
    normalized = state.workspace.path(rid, f"inputs/profile_sources/{fingerprint(plan)}-{source['source_sha256']}.csv")
    content = normalized_csv(transformed).encode("utf-8", errors="strict")
    normalized.write_bytes(content)
    import_tools = runtime._registry(coordinator.contract_id, import_source_paths=[normalized])
    _, imported = runtime._dispatch(import_tools, coordinator, coordinator_task, "data.import", {"source_path": str(normalized), "research_id": rid})
    did = imported.result["dataset_id"]
    _, profile = runtime._dispatch(import_tools, coordinator, coordinator_task, "data.profile", {"dataset_id": did})
    contract, task = runtime._role_contract(rid, "analysis_planner_worker", "저장된 두 기간 비교 절차를 그대로 계산합니다.\n" + json.dumps(plan, ensure_ascii=False),
        "QualifiedProfileWorker", parent_task_id=coordinator_task, allowed_tools=["stats.run", "visualization.render"],
        max_tool_calls=2, runtime_key=prefix + ":worker")
    tools = runtime._registry(contract.contract_id)
    request, result = runtime._dispatch(tools, contract, task, "stats.run", {"dataset_id": did, "method": "two_period_comparison",
        "variables": {"year": "year", "value": "value"}, "parameters": {"periods": plan["periods"]}})
    _, figure = runtime._dispatch(tools, contract, task, "visualization.render", {"dataset_id": did, "plot_type": "line", "x": "year", "y": "value",
        "title": "연간 기온 편차 · 관측값", "y_label": plan["transform"].get("unit", "degC")})
    cid = "QL-" + rid[2:]
    current_claim = next((c for c in state.research_slice.current(rid) if c.claim_id == cid), None)
    binding_key = prefix + ":binding"
    saved_binding = state.runtime_step(rid, binding_key)
    claim_revision = saved_binding["output"]["claim_revision"] if saved_binding else current_claim.revision + 1 if current_claim else 1
    binding = {**source, "plan": plan, "used_rows_hash": _dependency(rows, plan), "contract_id": contract.contract_id,
               "claim_id": cid, "claim_revision": claim_revision, "authority_key": authority["step_key"],
               "representation": representation}
    binding_key = prefix + ":binding"
    state.finish_runtime_step(rid, binding_key, binding)
    staged = state._db.execute("SELECT mutation_id,payload_json,status FROM staged_mutations WHERE contract_id=? ORDER BY rowid DESC LIMIT 1", (contract.contract_id,)).fetchone()
    if not staged:
        art = result.provenance["stats_artifact_id"]
        dataset = state.dataset_record(did, rid)
        provenance = {name: NumericProvenance(value=value, field=name, artifact_id=art, tool_call_id=request.request_id,
                    dataset_id=did, dataset_sha256=dataset["sha256"]) for name, value in numeric_fields(result.result).items()}
        science = ScientificMutation(dataset_id=did, profile_artifact_id=profile.result["artifact_id"], stats_artifact_id=art,
            figure_artifact_id=figure.result["artifact_id"], experiment_id=new_id("EXP"), evidence_id=new_id("EV"),
            method="two_period_comparison", claim=claim_text(plan, result.result), polarity="support", numeric_provenance=provenance)
        agent = AgentResult(result_id=new_id("ART"), research_id=rid, contract_id=contract.contract_id,
            actor_role=contract.assigned_role, status="completed", output=result.result, confidence=1,
            artifact_refs=[ContextRef(type=RefType.artifact, id=i) for i in (science.profile_artifact_id, art, science.figure_artifact_id)],
            provenance={"qualified_profile":{"step_key":binding_key,"fingerprint":fingerprint(binding),"profile_id":candidate["profile_id"]},
                        "qualified_claim_id":cid, "qualified_claim_revision":claim_revision,
                        "analysis_revision": index, "qualified_scope": {"profile_id":candidate["profile_id"],"periods":plan["periods"],
                            "semantics":plan["semantics"],"transform":plan["transform"],"used_rows_hash":binding["used_rows_hash"]}})
        payload = StagedResult(agent_result=agent, tool_request=request, tool_result=result, scientific=science)
        if fault:
            payload = fault(payload)
        mid = state.stage(payload)
    else:
        mid = staged["mutation_id"]
        payload = StagedResult.model_validate_json(staged["payload_json"])
    if not staged or staged["status"] != "COMMITTED":
        verification = state.verify(mid)
        if verification.verdict.value != "PASS":
            raise ControlError("PROFILE_VERIFICATION_FAILED")
        if getattr(runtime, "faults", None):
            runtime.faults.at("profile_after_verify")
        if getattr(runtime, "control_boundary", None):
            runtime.control_boundary()
        state.commit(mid)
        if getattr(runtime, "faults", None):
            runtime.faults.at("profile_after_commit")
    runtime._complete_task(contract.contract_id)
    runtime._complete_task(coordinator.contract_id)
    if previous:
        previous_payload = StagedResult.model_validate_json(state._one("SELECT payload_json FROM staged_mutations WHERE mutation_id=?", (previous["mutation_id"],))[0])
        old_experiment = previous_payload.scientific.experiment_id
        if state._one("SELECT status FROM experiments WHERE experiment_id=?", (old_experiment,))[0] == "VERIFIED":
            state.invalidate_experiment(rid, old_experiment, "새 계산으로 대체한 이전 결과입니다. 변경 이력은 보존합니다.")
    record = {"profile_id":candidate["profile_id"],"question":plan["question"],"mutation_id":mid,"claim_id":payload.agent_result.provenance["qualified_claim_id"],
        "source_description":"NASA GISTEMP v4 · 전 지구 연간 기온 편차 · 원래 기준 1951–1980년 · 저장값 × 0.01 °C · 표시 단위 " + plan["transform"]["unit"] + (" · 표시 기준 " + str(plan["transform"]["baseline_period"]) if plan["transform"].get("baseline_period") else "") + "\n" + SOURCE_URL,
        "calculation":payload.scientific.claim,"scope":"지정한 두 기간의 전 지구 관측 편차 평균 비교",
        "unconfirmed":["인과관계","미래 예측","지역별 기온","확증적 유의성", RESIDUAL_LIMIT] +
            (["다른 형식의 선택값 대조는 수행하지 않았습니다."] if not representation["performed"] else []),
        "analysis_revision":index,"execution":"LOCAL_TOOLS" if replay else runtime.provider.name,
        "question_revision": plan["question_revision"], "authority_key": authority["step_key"], "representation": representation,
        "reported_values": {"mean_a": payload.tool_result.result["periods"][0]["mean"],
                            "mean_b": payload.tool_result.result["periods"][1]["mean"], "difference": payload.tool_result.result["difference"]},
        "selection": {"A": list(range(plan["periods"][0][0], plan["periods"][0][1] + 1)),
                      "B": list(range(plan["periods"][1][0], plan["periods"][1][1] + 1))}}
    state.finish_runtime_step(rid, f"qualified_result:{index}", record)
    runtime._save_cursor(rid, "QUALIFIED_COMPLETED")
    return record
