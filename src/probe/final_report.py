"""검증된 기록의 참조를 해석한 최종 연구 보고서."""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime
from typing import Literal

from pydantic import Field

from .database import from_json, to_json
from .schemas import StrictModel, utc_now
from .storage import sha256_bytes
from .scholarly import metadata_digest, source_from_row


class ReportValidationError(ValueError):
    """보고서의 주장을 검증된 기록에 연결할 수 없다."""


class FinalConclusion(StrictModel):
    research_question: str
    conclusion: str
    support_level: Literal["SUPPORTED", "PARTIALLY_SUPPORTED", "INCONCLUSIVE", "NOT_SUPPORTED"]
    evidence_refs: list[str] = Field(default_factory=list)
    experiment_refs: list[str] = Field(default_factory=list)
    contradiction_refs: list[str] = Field(default_factory=list)
    limitation_refs: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)


_NUMERIC = re.compile(r"\{\{NUM:(EXP-[0-9a-f]{32}):([A-Za-z_][A-Za-z_0-9.]*)\}\}")
_UNRESOLVED = re.compile(r"\{\{(?:NUM|SRC|EVIDENCE):[^}]*\}\}")
_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\])>,;]+", re.I)
_SOURCE_ID = re.compile(r"\bSRC-[0-9a-f]{32}\b")


def _safe(value: object) -> str:
    text = " ".join(str(value).split())
    return html.escape(text, quote=True).replace("[", "&#91;").replace("]", "&#93;")


def _source_rows(state, research_id: str) -> list[dict]:
    return [dict(row) for row in state._db.execute("SELECT * FROM sources WHERE research_id=? ORDER BY rowid",
                                                   (research_id,))]


def _evidence_rows(state, research_id: str) -> list[dict]:
    return [dict(row) for row in state._db.execute("SELECT * FROM evidence WHERE research_id=? ORDER BY rowid",
                                                   (research_id,))]


def _verified_experiments(state, research_id: str) -> list[dict]:
    return [dict(row) for row in state._db.execute("SELECT * FROM experiments WHERE research_id=? AND status='VERIFIED' ORDER BY rowid",
                                                   (research_id,))]


def _validate_literature_provenance(source: dict, evidence: dict, *, state=None) -> None:
    if source["status"] != "VERIFIED" or evidence["status"] != "VERIFIED":
        raise ReportValidationError("invalidated source or evidence")
    if evidence["source_metadata_hash"] != source["metadata_hash"]:
        raise ReportValidationError("source metadata hash mismatch")
    if metadata_digest(source_from_row(source)) != source["metadata_hash"]:
        raise ReportValidationError("stored source metadata was modified")
    field = evidence["text_field"]
    text = source.get("abstract") if field == "abstract" else None
    if field == "fulltext" and state is not None:
        from .source_documents import document_span
        from .control_plane import ControlError
        from .storage import ArtifactIntegrityError
        try:
            text, _ = document_span(state, source["research_id"], source["source_id"],
                                    evidence["evidence_location"], proof=from_json(evidence["provenance_json"]).get("document"))
        except (ControlError, ArtifactIntegrityError, ValueError, OSError) as exc:
            raise ReportValidationError("source document integrity lost") from exc
    if field == "webpage" and state is not None:
        from .web_sources import webpage_span
        from .control_plane import ControlError
        from .storage import ArtifactIntegrityError
        proof = from_json(evidence["provenance_json"]).get("web_document")
        if not proof: raise ReportValidationError("공개 웹 원문의 provenance 없음")
        try:
            text, _ = webpage_span(state, source["research_id"], source["source_id"], evidence["evidence_location"], proof=proof)
        except (ControlError, ArtifactIntegrityError, ValueError, OSError) as exc:
            raise ReportValidationError("공개 웹 원문의 무결성 검사 실패") from exc
    if not text or evidence["evidence_text"] not in text:
        raise ReportValidationError("source text alignment lost")
    digest = hashlib.sha256(evidence["evidence_text"].encode("utf-8", errors="strict")).hexdigest()
    if digest != evidence["text_hash"]:
        raise ReportValidationError("source text hash mismatch")
    verdict = from_json(evidence["verification_json"] or "{}")
    if verdict.get("passed") is not True:
        raise ReportValidationError("literature verifier did not pass")


def _trusted_stat(state, research_id: str, experiment_id: str, field: str) -> object:
    experiment = state._db.execute("SELECT * FROM experiments WHERE experiment_id=? AND research_id=? AND status='VERIFIED'",
                                   (experiment_id, research_id)).fetchone()
    if experiment is None:
        raise ReportValidationError("unverified numeric experiment reference")
    from .storage import DatasetIntegrityError
    try:
        dataset = state.dataset_record(experiment["dataset_id"], research_id)
    except (DatasetIntegrityError, ValueError):
        raise ReportValidationError("numeric dataset is invalid or changed") from None
    artifact = state.file_artifact(experiment["result_artifact_id"], research_id)
    if artifact["status"] != "VERIFIED":
        raise ReportValidationError("numeric artifact is not verified")
    document = from_json(state.workspace.path(research_id, artifact["relative_path"]).read_text(
        encoding="utf-8", errors="strict"))
    result = document.get("result")
    if document.get("dataset_id") != dataset["dataset_id"] or document.get("dataset_sha256") != dataset["sha256"]:
        raise ReportValidationError("numeric dataset provenance does not match")
    if not isinstance(result, dict):
        raise ReportValidationError("stats artifact has no result")
    value: object = result
    for part in field.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ReportValidationError("numeric field is absent from verified artifact")
    evidence = state._db.execute("SELECT provenance_json FROM evidence WHERE research_id=? AND experiment_id=? AND status='VERIFIED'",
                                 (research_id, experiment_id)).fetchone()
    if evidence is None:
        raise ReportValidationError("numeric experiment lacks verified evidence")
    provenance = from_json(evidence["provenance_json"])
    link = provenance.get(field)
    if not isinstance(link, dict) or link.get("artifact_id") != artifact["artifact_id"] or link.get("value") != value:
        raise ReportValidationError("numeric provenance does not match artifact")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportValidationError("numeric reference is not a number")
    import math
    if not math.isfinite(value):
        raise ReportValidationError("numeric reference is not finite")
    return value


def resolve_numeric_placeholders(state, research_id: str, text: str) -> str:
    def replace(match: re.Match) -> str:
        value = _trusted_stat(state, research_id, match.group(1), match.group(2))
        return format(value, ".6g") if isinstance(value, float) else str(value)
    resolved = _NUMERIC.sub(replace, text)
    if _UNRESOLVED.search(resolved):
        raise ReportValidationError("unresolved numeric or citation placeholder")
    return resolved


def build_final_conclusion(state, research_id: str) -> FinalConclusion:
    run = state._one("SELECT goal,research_question FROM research_runs WHERE research_id=?", (research_id,))
    from .qualified_profiles import conclusion_card
    qualified = conclusion_card(state, research_id)
    if qualified["available"] and qualified["current"]:
        mutation = state._one("SELECT payload_json FROM staged_mutations WHERE mutation_id=?", (qualified["record"]["mutation_id"],))
        material = from_json(mutation[0])["scientific"]
        exp_id = material["experiment_id"]
        statement = f"두 기간의 관측 편차 평균은 앞 기간 {{{{NUM:{exp_id}:periods.0.mean}}}}, 뒤 기간 {{{{NUM:{exp_id}:periods.1.mean}}}}이며, 뒤에서 앞을 뺀 차이는 {{{{NUM:{exp_id}:difference}}}}입니다. 인과관계·예측·확증적 유의성은 확인하지 않았습니다."
        return FinalConclusion(research_question=run["research_question"] or run["goal"], conclusion=statement,
            support_level="SUPPORTED", evidence_refs=[material["evidence_id"]], experiment_refs=[material["experiment_id"]],
            contradiction_refs=[], limitation_refs=[], unresolved_questions=qualified["unconfirmed"])
    sources = {row["source_id"]: row for row in _source_rows(state, research_id)}
    literature = []
    for row in _evidence_rows(state, research_id):
        if row["source_type"] == "LITERATURE" and row["status"] == "VERIFIED":
            source = sources.get(row["source_id"])
            if source is None:
                raise ReportValidationError("verified literature evidence has no source")
            _validate_literature_provenance(source, row, state=state)
            literature.append(row)
    support = [row["evidence_id"] for row in literature if row["polarity"] == "SUPPORT"]
    contradiction = [row["evidence_id"] for row in literature if row["polarity"] == "CONTRADICT"]
    experiments = _verified_experiments(state, research_id)
    invalidated_experiments = [row["experiment_id"] for row in state._db.execute(
        "SELECT experiment_id FROM experiments WHERE research_id=? AND status='INVALIDATED'",
        (research_id,))]
    experiment_rows = [row for row in _evidence_rows(state, research_id)
                       if row["experiment_id"] in {item["experiment_id"] for item in experiments}
                       and row["status"] == "VERIFIED"]
    experiment_support = [row["evidence_id"] for row in experiment_rows
                          if (row["polarity"] or "").upper() == "SUPPORT"]
    experiment_contradiction = [row["evidence_id"] for row in experiment_rows
                                if (row["polarity"] or "").upper() == "CONTRADICT"]
    if experiments and len(experiment_rows) < len(experiments):
        raise ReportValidationError("verified experiment lacks verified evidence")
    if experiments and experiment_support and support and contradiction:
        level = "PARTIALLY_SUPPORTED"
        conclusion = "Verified experiments indicate an association; retrieved abstracts contain both supporting and contradictory statements. Causality remains unresolved."
    elif experiments and experiment_support and support and experiment_contradiction:
        level = "PARTIALLY_SUPPORTED"
        conclusion = "Verified experimental results are mixed despite supporting abstract statements. Causality remains unresolved."
    elif experiments and experiment_support and support and invalidated_experiments:
        level = "PARTIALLY_SUPPORTED"
        conclusion = "Verified experiments and retrieved abstracts indicate an association, but invalidated results were excluded. Causality remains unresolved."
    elif experiments and experiment_support and support:
        level = "SUPPORTED"
        conclusion = "Verified experiments and retrieved abstracts indicate an association. Causality remains unresolved."
    elif experiment_contradiction and not experiment_support:
        level = "NOT_SUPPORTED"
        conclusion = "Verified experimental evidence contradicts the proposed association; a supporting conclusion is not warranted."
    elif experiments:
        level = "INCONCLUSIVE"
        conclusion = "Verified experiments are available, but verified literature support is insufficient for a combined conclusion."
    else:
        level = "INCONCLUSIVE"
        conclusion = "Available verified literature does not establish a combined experimental conclusion."
    synthesis = state._db.execute("SELECT synthesis_json FROM literature_syntheses WHERE research_id=?",
                                  (research_id,)).fetchone()
    unresolved = from_json(synthesis["synthesis_json"])["unresolved_issues"] if synthesis else ["LITERATURE_INCOMPLETE"]
    for row in state._db.execute("SELECT cr.output_json FROM critic_reviews cr JOIN experiments e ON e.experiment_id=cr.experiment_id WHERE cr.research_id=? AND e.status='VERIFIED'",
                                 (research_id,)):
        review = from_json(row["output_json"])
        unresolved += review.get("alternative_explanations", []) + review.get("confounders", [])
    if invalidated_experiments:
        unresolved.append("Invalidated experiment results were excluded from support")
    return FinalConclusion(research_question=run["research_question"] or run["goal"],
                           conclusion=conclusion, support_level=level,
                           evidence_refs=support + experiment_support,
                           experiment_refs=[row["experiment_id"] for row in experiments],
                           contradiction_refs=contradiction + experiment_contradiction,
                           limitation_refs=invalidated_experiments,
                           unresolved_questions=list(dict.fromkeys(unresolved)))


def validate_final_conclusion(state, research_id: str, candidate: FinalConclusion) -> None:
    run = state._one("SELECT goal,research_question FROM research_runs WHERE research_id=?", (research_id,))
    if candidate.research_question != (run["research_question"] or run["goal"]):
        raise ReportValidationError("research question differs from stored state")
    evidence = {row["evidence_id"]: row for row in _evidence_rows(state, research_id)}
    sources = {row["source_id"]: row for row in _source_rows(state, research_id)}
    experiments = {row["experiment_id"]: row for row in _verified_experiments(state, research_id)}
    for identity in candidate.evidence_refs + candidate.contradiction_refs:
        row = evidence.get(identity)
        if row is None or row["status"] != "VERIFIED":
            raise ReportValidationError("unresolved or unverified evidence reference")
        if row["source_type"] == "LITERATURE":
            source = sources.get(row["source_id"])
            if source is None:
                raise ReportValidationError("unresolved source reference")
            _validate_literature_provenance(source, row, state=state)
        elif row["experiment_id"] not in experiments:
            raise ReportValidationError("invalidated experiment evidence reference")
    for identity in candidate.experiment_refs:
        if identity not in experiments:
            raise ReportValidationError("unresolved experiment reference")
    if state.research_slice.config(research_id).claim_evidence_provenance:
        snapshot = state.research_slice.snapshot(research_id)
        current = {c["claim_id"]: c for c in snapshot["claims"] if c["current"]}
        covered_experiments, covered_evidence = set(), set()
        for binding in snapshot["bindings"]:
            claim = current.get(binding["claim_id"])
            if not claim or claim["revision"] != binding["claim_revision"]:
                continue
            experiment = binding["locator"].get("experiment_id")
            evidence_id = binding["locator"].get("evidence_id")
            used = experiment in candidate.experiment_refs or evidence_id in candidate.evidence_refs + candidate.contradiction_refs
            # 중립 관측값은 가설을 지지하지 않지만 검증된 수치 결과로 보고할 수 있다.
            neutral_observation = (experiment in candidate.experiment_refs
                and binding["evidence_kind"] == "artifact" and binding["usage_type"] == "NUMERIC_RESULT"
                and binding["relation"] == "QUALIFIES" and claim["effective_support_state"] == "INCONCLUSIVE"
                and claim["claim_type"] in {"association", "prediction", "comparison"})
            if used and (binding["status"] != "ACTIVE" or claim["effective_support_state"] in {"NEEDS_REVALIDATION", "INVALIDATED", "NEEDS_REVIEW"}
                         or (claim["effective_support_state"] == "INCONCLUSIVE" and not neutral_observation)):
                raise ReportValidationError("material claim needs revalidation")
            if used:
                covered_experiments.add(experiment)
                covered_evidence.add(evidence_id)
        required_literature = {eid for eid in candidate.evidence_refs + candidate.contradiction_refs if evidence[eid]["source_type"] == "LITERATURE"}
        if not set(candidate.experiment_refs) <= covered_experiments or not required_literature <= covered_evidence:
            raise ReportValidationError("required material claim is missing")
    if not set(candidate.contradiction_refs) >= {
            identity for identity, row in evidence.items()
            if row["source_type"] == "LITERATURE" and row["status"] == "VERIFIED"
            and row["polarity"] == "CONTRADICT" and sources.get(row["source_id"], {}).get("status") == "VERIFIED"}:
        raise ReportValidationError("verified contradiction omitted")
    if not set(candidate.contradiction_refs) >= {
            identity for identity, row in evidence.items()
            if row["experiment_id"] in experiments and row["status"] == "VERIFIED"
            and (row["polarity"] or "").upper() == "CONTRADICT"}:
        raise ReportValidationError("verified experiment contradiction omitted")
    listed_dois = {row["doi"] for row in sources.values() if row["doi"]}
    for doi in _DOI.findall(candidate.conclusion):
        if doi.lower().rstrip(".") not in listed_dois:
            raise ReportValidationError("unresolved DOI")
    if any(identity not in sources for identity in _SOURCE_ID.findall(candidate.conclusion)):
        raise ReportValidationError("unresolved source ID")
    if re.search(r"\d", _NUMERIC.sub("", candidate.conclusion)):
        raise ReportValidationError("unresolved raw numeric claim")
    resolve_numeric_placeholders(state, research_id, candidate.conclusion)
    historical_experiments = {row[0] for row in state._db.execute(
        "SELECT experiment_id FROM experiments WHERE research_id=?", (research_id,))}
    for identity in candidate.limitation_refs:
        if identity not in sources and identity not in evidence and identity not in historical_experiments:
            raise ReportValidationError("unresolved limitation reference")
    trusted = build_final_conclusion(state, research_id)
    if (candidate.conclusion != trusted.conclusion or candidate.support_level != trusted.support_level
            or set(candidate.evidence_refs) != set(trusted.evidence_refs)
            or set(candidate.experiment_refs) != set(trusted.experiment_refs)
            or not set(candidate.limitation_refs) >= set(trusted.limitation_refs)
            or not set(candidate.unresolved_questions) >= set(trusted.unresolved_questions)):
        raise ReportValidationError("conclusion is not supported by the verified synthesis")


def _render_report(state, research_id: str, conclusion: FinalConclusion) -> str:
    """검증·추적 기록은 별도 JSON에 보존하고 보고서에는 탐구 내용만 담는다."""
    from .report_ux import _friendly_report, markdown_report
    return markdown_report(_friendly_report(state, research_id), _source_rows(state, research_id), include_images=False) + "\n"


def export_final_report(state, research_id: str,
                        candidate: FinalConclusion | None = None) -> dict[str, str]:
    if state.workspace is None:
        raise ValueError("research workspace is required")
    run_status = state._one("SELECT run_status FROM research_runs WHERE research_id=?",
                            (research_id,))["run_status"]
    if run_status == "ACTIVE":
        raise ReportValidationError("active research cannot be exported as a final report")
    conclusion = candidate or build_final_conclusion(state, research_id)
    validate_final_conclusion(state, research_id, conclusion)
    report = _render_report(state, research_id, conclusion)
    if _UNRESOLVED.search(report):
        raise ReportValidationError("unresolved report reference")
    from .report_publication import new_revision
    root = new_revision(state, research_id)
    run = state._one("SELECT * FROM research_runs WHERE research_id=?", (research_id,))
    hypotheses = [dict(row) for row in state._db.execute("SELECT hypothesis_id,statement,status FROM hypotheses WHERE research_id=?",
                                                     (research_id,))]
    evidence = _evidence_rows(state, research_id)
    sources = _source_rows(state, research_id)
    experiments = [dict(row) for row in state._db.execute("SELECT experiment_id,method,status,result_artifact_id FROM experiments WHERE research_id=?",
                                                        (research_id,))]
    actions = [dict(row) for row in state._db.execute("SELECT action_id,action_index,action_type,details_json,created_at FROM research_actions WHERE research_id=? ORDER BY action_index",
                                                    (research_id,))]
    budget = state.budget(research_id)
    api_usage = [dict(row) for row in state._db.execute("SELECT actor_role,provider,model,status,input_tokens,output_tokens,estimated_cost_usd FROM agent_runs WHERE research_id=? AND provider IS NOT NULL",
                                                       (research_id,))]
    created = datetime.fromisoformat(run["created_at"])
    duration = max(0.0, (utc_now() - created).total_seconds())
    summary = {"research_id": research_id, "state_version": run["state_version"],
               "research_question": conclusion.research_question,
               "status": run["run_status"], "stop_reason": run["stop_reason"],
               "hypotheses": hypotheses,
               "verified_evidence_count": sum(row["status"] == "VERIFIED" for row in evidence),
               "verified_experiment_count": sum(row["status"] == "VERIFIED" for row in experiments),
               "invalidated_count": sum(row["status"] == "INVALIDATED" for row in evidence),
               "final_conclusion": conclusion.model_dump(mode="json"), "total_actions": len(actions),
               "api_usage": api_usage, "cost": {"spent_usd": budget["spent_usd"]},
               "duration_sec": duration}
    state_snapshot = {"research": {key: run[key] for key in ("research_id", "goal", "research_question", "run_status", "stop_reason", "state_version")},
                      "hypotheses": hypotheses, "sources": sources, "evidence": evidence,
                      "experiments": experiments}
    trace_records = [{"kind": "action", **action} for action in actions]
    trace_records += [{"kind": "planning_event", **dict(row)} for row in state._db.execute(
        "SELECT seq,event_type,entity_type,entity_id,payload_json,state_version,created_at "
        "FROM planning_events WHERE research_id=?", (research_id,))]
    trace_records += [{"kind": "state_event", **dict(row)} for row in state._db.execute(
        "SELECT seq,entity_type,entity_id,operation,state_version,mutation_id,created_at "
        "FROM state_events WHERE research_id=?", (research_id,))]
    trace_records += [{"kind": "runtime_event", **dict(row)} for row in state._db.execute(
        "SELECT seq,event_type,details_json,created_at FROM runtime_events WHERE research_id=?",
        (research_id,))]
    trace_records.sort(key=lambda item: (item["created_at"], item["kind"], item.get("seq", 0)))
    trace = "".join(to_json(item) + "\n" for item in trace_records)
    files = {
        "final_report.md": report,
        "research_summary.json": to_json(summary) + "\n",
        "state_snapshot.json": to_json(state_snapshot) + "\n",
        "research_trace.jsonl": trace,
        "cost_report.json": to_json({"budget": budget, "api_usage": api_usage}) + "\n",
        "hypotheses/hypotheses.json": to_json(hypotheses) + "\n",
        "evidence/evidence.json": to_json(evidence) + "\n",
        "evidence/sources.json": to_json(sources) + "\n",
        "experiments/experiments.json": to_json(experiments) + "\n",
        "verification/critic_reviews.json": to_json([dict(row) for row in state._db.execute(
            "SELECT review_id,experiment_id,output_json FROM critic_reviews WHERE research_id=?", (research_id,))]) + "\n",
    }
    slice_snapshot = state.research_slice.snapshot(research_id)
    if slice_snapshot:
        files["research_slice.json"] = to_json(slice_snapshot) + "\n"
    from .qualified_profiles import export_bundle
    from .research_design import current_design, summary
    design = current_design(state, research_id)
    if design:
        files["research_design.json"] = to_json({"current": design, "summary": summary(state, research_id)}) + "\n"
    qualified = export_bundle(state, research_id)
    if qualified:
        files["qualified_profile.json"] = to_json(qualified) + "\n"
        for binding in qualified["bindings"] + ([qualified["current_source"]] if qualified["current_source"] else []):
            for capture in [binding] + ([binding["secondary"]] if binding.get("secondary") else []):
                relative = capture["source_relative"]
                files["qualified_sources/" + relative.rsplit("/", 1)[-1]] = state.workspace.path(research_id, relative).read_text(encoding="utf-8", errors="strict")
        files["replay_manifest.json"] = to_json({"profile_id":qualified["record"]["profile_id"] if "record" in qualified else qualified["card"]["record"]["profile_id"],
            "binding":qualified["bindings"][-1], "source":"qualified_sources/" + qualified["bindings"][-1]["source_relative"].rsplit("/",1)[-1],
            "current_source": qualified["current_source"], "authority": qualified.get("authority"),
            **({"research_design": {"hash": design["hash"], "revision": design["revision"], "path": "research_design.json"}} if design else {}),
            "expected_claim":qualified["card"]["calculation"]}) + "\n"
    from .research_report import report_record
    ai_report = report_record(state, research_id)
    if ai_report:
        files["ai_report.json"] = to_json(ai_report) + "\n"
    manifest = {"research_id": research_id, "validation_version": 2, "state_version": run["state_version"],
                "files": {name: sha256_bytes(content.encode("utf-8", errors="strict"))
                          for name, content in files.items()},
                "scientific_artifacts": [dict(row) for row in state._db.execute(
                    "SELECT artifact_id,artifact_type,relative_path,sha256,status FROM artifacts "
                    "WHERE research_id=? AND relative_path IS NOT NULL", (research_id,))],
                "datasets": [dict(row) for row in state._db.execute(
                    "SELECT dataset_id,sha256,status FROM datasets WHERE research_id=?", (research_id,))],
                "source_metadata": [{"source_id": row["source_id"], "metadata_hash": row["metadata_hash"],
                                     "status": row["status"]} for row in sources]}
    files["manifests/artifact_manifest.json"] = to_json(manifest) + "\n"
    files["manifests/model_manifest.json"] = to_json({"research_id": research_id,
                                                       "models": sorted({(row["provider"], row["model"]) for row in api_usage})}) + "\n"
    files["manifests/tool_manifest.json"] = to_json({"research_id": research_id,
                                                      "tools": [dict(row) for row in state._db.execute(
                                                          "SELECT DISTINCT tc.tool_name FROM tool_calls tc "
                                                          "JOIN agent_runs ar ON ar.agent_run_id=tc.agent_run_id "
                                                          "JOIN contracts c ON c.contract_id=ar.contract_id "
                                                          "WHERE c.research_id=?",
                                                          (research_id,))]}) + "\n"
    from .report_ux import _friendly_report
    files["report_view.json"] = to_json(_friendly_report(state, research_id)) + "\n"
    manifest["files"]["report_view.json"] = sha256_bytes(files["report_view.json"].encode("utf-8", errors="strict"))
    # 모든 선언 파일도 동일 수정본의 검사 대상에 포함한다.
    for name in ("manifests/model_manifest.json", "manifests/tool_manifest.json"):
        manifest["files"][name] = sha256_bytes(files[name].encode("utf-8", errors="strict"))
    files["manifests/artifact_manifest.json"] = to_json(manifest) + "\n"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8", errors="strict"))
    for folder in ("figures",):
        (root / folder).mkdir(exist_ok=True)
    from .report_publication import selected_revision, publish_revision
    from .report_pdf import render_pdf
    with selected_revision(state, research_id, root):
        pdf = render_pdf(state, research_id)
    (root / "report.pdf").write_bytes(pdf["data"])
    manifest["files"]["report.pdf"] = pdf["sha256"]
    (root / "manifests/artifact_manifest.json").write_bytes((to_json(manifest) + "\n").encode("utf-8", errors="strict"))
    publish_revision(state, research_id, root)
    return {"report": str(root / "final_report.md"), "summary": str(root / "research_summary.json"),
            "manifest": str(root / "manifests/artifact_manifest.json")}
