"""기존 StateService·SQLite·의존 그래프의 선택적 확장. 검증된 필드·인용 연결만 제한된 의미 지원을 제공한다."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN
from hashlib import sha256
import math
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .database import from_json, to_json
from .research_slice_schemas import (AnalysisPrecommit, Claim, EvidenceBinding,
                                     NumericSlot, ResearchSliceConfig, StructuredConclusion,
                                     VerifierObligation)
from .schemas import StagedResult, utc_now
from .storage import sha256_file

CONFIG_KEY = "research_slice_config"
DDL = """
CREATE TABLE IF NOT EXISTS research_slice_schema(version TEXT PRIMARY KEY);
INSERT OR IGNORE INTO research_slice_schema VALUES('1');
CREATE TABLE IF NOT EXISTS claim_revisions(
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 claim_id TEXT NOT NULL, revision INTEGER NOT NULL, current INTEGER NOT NULL,
 payload_json TEXT NOT NULL, PRIMARY KEY(research_id,claim_id,revision));
CREATE UNIQUE INDEX IF NOT EXISTS idx_current_claim ON claim_revisions(research_id,claim_id) WHERE current=1;
CREATE TABLE IF NOT EXISTS claim_bindings(
 research_id TEXT NOT NULL, binding_id TEXT NOT NULL, claim_id TEXT NOT NULL,
 claim_revision INTEGER NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(research_id,binding_id),
 FOREIGN KEY(research_id,claim_id,claim_revision) REFERENCES claim_revisions(research_id,claim_id,revision));
CREATE TABLE IF NOT EXISTS slice_staging(
 mutation_id TEXT PRIMARY KEY REFERENCES staged_mutations(mutation_id), payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verifier_obligations(
 research_id TEXT NOT NULL REFERENCES research_runs(research_id), obligation_id TEXT NOT NULL,
 subject_id TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(research_id,obligation_id));
CREATE TABLE IF NOT EXISTS verifier_obligation_history(
 research_id TEXT NOT NULL REFERENCES research_runs(research_id), event_id TEXT NOT NULL,
 payload_json TEXT NOT NULL, PRIMARY KEY(research_id,event_id));
"""


def identity(prefix: str, value: str) -> str:
    return prefix + "-" + uuid5(NAMESPACE_URL, value).hex


def digest(value) -> str:
    return sha256(to_json(value).encode("utf-8", errors="strict")).hexdigest()


def implementation_dependencies(check_id):
    dependencies = ["code:" + sha256_file(Path(__file__).with_name(name)) for name in ("service.py", "scientific_verifier.py", "research_slice.py")]
    if check_id in {"SOURCE_SEMANTIC_MATCH", "TRANSFORMATION_LINEAGE", "GOAL_SCOPE_MATCH", "CLAIM_SUPPORT"}:
        dependencies.append("code:" + sha256_file(Path(__file__).with_name("cycle5.py")))
    if check_id in {"F3P_INDEPENDENT_ASSOCIATION", "F3P_RIDGE_ARITHMETIC"}:
        import numpy
        dependencies += ["code:" + sha256_file(Path(__file__).with_name("verification_repair.py")),
                         "numpy:" + numpy.__version__, "function:" + ("numpy.corrcoef" if check_id == "F3P_INDEPENDENT_ASSOCIATION" else "numpy.linalg")]
    return dependencies


def pointer(document, path: str):
    current = document
    for part in path.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not part.isdecimal():
                raise ValueError("INVALID_NUMERIC_LOCATOR")
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise ValueError("INVALID_NUMERIC_LOCATOR")
    return current


def numeric_equal(slot: NumericSlot, actual) -> bool:
    if slot.value is None or actual is None:
        return slot.value is None and actual is None
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    if not math.isfinite(slot.value) or not math.isfinite(actual):
        return False
    if slot.tolerance_policy == "ROUND_HALF_EVEN":
        if slot.display_precision is None:
            return False
        quantum = Decimal(1).scaleb(-slot.display_precision)
        return Decimal(str(slot.value)) == Decimal(str(actual)).quantize(quantum, rounding=ROUND_HALF_EVEN)
    if slot.tolerance_policy == "ABSOLUTE_1E-10":
        return abs(slot.value - actual) <= 1e-10
    return slot.value == actual


def unit_for(field: str, scope: dict) -> str:
    name = field.rsplit("/", 1)[-1]
    if name in {"n", "sample_size"}:
        return "observations"
    if scope.get("method") == "two_period_comparison":
        return "year" if name in {"start", "end"} else scope.get("units", {}).get("value", "UNKNOWN")
    if scope.get("method") in {"pearson_correlation", "spearman_correlation"}:
        return "dimensionless"
    target = scope.get("variables", {}).get("target")
    return scope.get("units", {}).get(target, "UNKNOWN")


def support_state(bindings: list[EvidenceBinding]) -> str:
    active = [b for b in bindings if b.status == "ACTIVE"]
    supports = any(b.relation == "SUPPORTS" for b in active)
    contradicts = any(b.relation == "CONTRADICTS" for b in active)
    if supports and contradicts:
        return "CONFLICTED"
    if contradicts:
        return "NOT_SUPPORTED"
    if supports:
        return "SUPPORTED"
    return "INCONCLUSIVE"


class ResearchSlice:
    """모든 변경은 StateService를 통해 호출한다."""

    def __init__(self, state):
        self.state, self.db = state, state._db

    def config(self, rid: str) -> ResearchSliceConfig:
        saved = self.state.runtime_step(rid, CONFIG_KEY)
        return ResearchSliceConfig.model_validate(saved["output"]) if saved else ResearchSliceConfig()

    def configure(self, rid: str, config: ResearchSliceConfig) -> None:
        from .service import StateConflictError
        saved = self.state.runtime_step(rid, CONFIG_KEY)
        schema_exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='research_slice_schema'").fetchone()
        if schema_exists and [r[0] for r in self.db.execute("SELECT version FROM research_slice_schema")] != ["1"]:
            raise StateConflictError("RESEARCH_SLICE_SCHEMA_VERSION_MISMATCH")
        if saved:
            if saved["status"] != "COMPLETED" or saved["output"] != config.model_dump(mode="json"):
                raise StateConflictError("RECOVERY_RESEARCH_SLICE_POLICY_MISMATCH")
            return
        if config.claim_evidence_provenance or config.verifier_dependency_catalog or config.reliability_lab:
            # 기존 핵심 마이그레이션 6개는 유지한다.
            self.db.executescript("BEGIN IMMEDIATE;" + DDL + "COMMIT;")
        self.state.finish_runtime_step(rid, CONFIG_KEY, config.model_dump(mode="json"))

    def current(self, rid: str) -> list[Claim]:
        if not self.config(rid).claim_evidence_provenance:
            return []
        return [Claim.model_validate_json(r[0]) for r in self.db.execute(
            "SELECT payload_json FROM claim_revisions WHERE research_id=? AND current=1 ORDER BY claim_id", (rid,))]

    def bindings(self, claim: Claim) -> list[EvidenceBinding]:
        return [EvidenceBinding.model_validate_json(r[0]) for r in self.db.execute(
            "SELECT payload_json FROM claim_bindings WHERE research_id=? AND claim_id=? AND claim_revision=? ORDER BY binding_id",
            (claim.research_id, claim.claim_id, claim.revision))]

    def _target(self, rid: str, binding: EvidenceBinding):
        if binding.evidence_kind == "artifact":
            record = self.state.file_artifact(binding.target_id, rid)
            if record["status"] != "VERIFIED" or record["sha256"] != binding.target_hash:
                raise ValueError("UNVERIFIED_OR_STALE_ARTIFACT")
            revision = self._artifact_revision(rid, binding.target_id)
            if revision != binding.target_revision:
                raise ValueError("STALE_ARTIFACT_REVISION")
            path = self.state.workspace.path(rid, record["relative_path"])
            return from_json(path.read_text(encoding="utf-8", errors="strict"))
        from .scholarly import metadata_digest, source_from_row
        row = self.state._one("SELECT * FROM sources WHERE research_id=? AND source_id=?", (rid, binding.target_id))
        if (row["status"] != "VERIFIED" or binding.target_hash != row["metadata_hash"]
                or metadata_digest(source_from_row(row)) != row["metadata_hash"]
                or binding.target_revision != row["metadata_hash"]):
            raise ValueError("STALE_SOURCE_SNAPSHOT")
        return dict(row)

    def _artifact_revision(self, rid: str, artifact_id: str) -> str:
        row = self.db.execute("SELECT payload_json FROM staged_mutations WHERE research_id=? AND json_extract(payload_json,'$.scientific.stats_artifact_id')=? ORDER BY rowid DESC LIMIT 1", (rid, artifact_id)).fetchone()
        if row:
            return str(from_json(row[0])["agent_result"]["provenance"].get("analysis_revision", 0))
        return "0"

    def validate(self, claim: Claim, bindings: list[EvidenceBinding]) -> None:
        if not bindings or claim.claim_type == "causal":
            raise ValueError("UNSUPPORTED_MATERIAL_CLAIM")
        if len({b.binding_id for b in bindings}) != len(bindings):
            raise ValueError("DUPLICATE_BINDING")
        documents = {}
        for b in bindings:
            if b.claim_id != claim.claim_id or b.claim_revision != claim.revision or b.scope != claim.scope:
                raise ValueError("BINDING_SCOPE_OR_REVISION_MISMATCH")
            if b.status != "ACTIVE":
                continue
            target = self._target(claim.research_id, b)
            documents[b.target_id] = target
            if b.evidence_kind == "source":
                loc = b.locator
                if self.state.cycle5.enabled(claim.research_id):
                    declared = [r for r in self.state.cycle5.records(claim.research_id, "source") if r.target_kind == "source" and r.target_id == b.target_id]
                    if declared and (any(r.review_status != "APPROVED" or r.target_hash != b.target_hash for r in declared)
                                     or claim.scope.get("source_semantics") != {r.record_id: digest(r) for r in declared}):
                        raise ValueError("SOURCE_SEMANTIC_REVALIDATION_REQUIRED")
                fulltext = loc.get("access_level") == "FULLTEXT" and loc.get("field") == "fulltext"
                if (not fulltext and (loc.get("access_level") != "ABSTRACT" or loc.get("field") != "abstract")
                        or loc.get("url") != target["url"] or loc.get("doi") != target["doi"]
                        or loc.get("retrieved_at") != target["retrieved_at"]):
                    raise ValueError("SOURCE_ACCESS_OR_METADATA_MISMATCH")
                span = loc.get("span")
                evidence = self.db.execute("SELECT * FROM evidence WHERE research_id=? AND evidence_id=? AND source_id=? AND status='VERIFIED'", (claim.research_id, loc.get("evidence_id"), b.target_id)).fetchone()
                text = target["abstract"]
                if fulltext:
                    from .source_documents import document_span
                    text, proof = document_span(self.state, claim.research_id, b.target_id, loc.get("section", ""), proof=loc.get("document"))
                    if evidence is None or from_json(evidence["provenance_json"]).get("document") != proof:
                        raise ValueError("SOURCE_DOCUMENT_PROOF_MISMATCH")
                relation = {"SUPPORT": "SUPPORTS", "CONTRADICT": "CONTRADICTS", "NEUTRAL": "QUALIFIES"}
                origin = self.db.execute("SELECT claim,target_hypothesis_id FROM evidence WHERE research_id=? AND evidence_id=? AND status='VERIFIED'", (claim.research_id, claim.created_from)).fetchone()
                scoped_contradiction = (b.relation == "CONTRADICTS" and evidence is not None and origin is not None
                                        and origin["claim"] == claim.text and origin["target_hypothesis_id"] is not None
                                        and origin["target_hypothesis_id"] == evidence["target_hypothesis_id"] == claim.scope.get("hypothesis_id"))
                if (not span or not text or span not in text or evidence is None
                        or span != evidence["evidence_text"] or (claim.text != evidence["claim"] and not scoped_contradiction)
                        or evidence["source_metadata_hash"] != b.target_hash
                        or relation.get(evidence["polarity"]) != b.relation):
                    raise ValueError("SOURCE_DOES_NOT_SUPPORT_CLAIM")
                if ("fulltext_partial" if fulltext else "abstract_only") not in b.assumptions:
                    raise ValueError("MISSING_ABSTRACT_LIMITATION")
            else:
                if b.usage_type != "NUMERIC_RESULT" or not b.verification_record_ids:
                    raise ValueError("NUMERIC_VERIFICATION_MISSING")
                mutation = self.db.execute("SELECT mutation_id,payload_json FROM staged_mutations WHERE research_id=? AND status='COMMITTED' AND json_extract(payload_json,'$.scientific.stats_artifact_id')=?", (claim.research_id, b.target_id)).fetchone()
                if not mutation or mutation["mutation_id"] not in b.verification_record_ids:
                    raise ValueError("NUMERIC_VERIFICATION_MISSING")
                verified_payload = StagedResult.model_validate_json(mutation["payload_json"])
                from .qualified_profiles import verify_profile
                if any(not c["passed"] for c in verify_profile(self.state, verified_payload)):
                    raise ValueError("QUALIFIED_PROFILE_REVALIDATION_REQUIRED")
                expected_claim, _ = self.science_records(verified_payload, mutation["mutation_id"])
                obligations = self.obligation_records(claim.research_id, mutation["mutation_id"])
                if self.config(claim.research_id).verifier_dependency_catalog and (not obligations or any(r.outcome != "PASS" or r.qualification_status == "FAILED" or r.check_version != digest(implementation_dependencies(r.check_id)) for r in obligations)):
                    raise ValueError("OBLIGATION_SUPPORT_REVOKED")
                dataset = self.state.dataset_record(expected_claim.scope["dataset_id"], claim.research_id)
                if dataset["sha256"] != expected_claim.scope["dataset_revision"]:
                    raise ValueError("STALE_DATASET_REVISION")
                science = StagedResult.model_validate_json(mutation["payload_json"]).scientific
                if self.state.cycle5.enabled(claim.research_id):
                    payload = StagedResult.model_validate_json(mutation["payload_json"])
                    if any(not c["passed"] for c in self.state.cycle5.checks(claim.research_id, payload)):
                        raise ValueError("STALE_SEMANTIC_SUPPORT_REVALIDATION_REQUIRED")
                if science.plan_artifact_id:
                    self.state.file_artifact(science.plan_artifact_id, claim.research_id)
                if claim.text != expected_claim.text or claim.scope != expected_claim.scope or claim.claim_type != expected_claim.claim_type:
                    raise ValueError("CLAIM_EXCEEDS_VERIFIED_SCOPE")
                expected_fields = {s.name: s.locator for s in expected_claim.numeric_slots}
                if any(s.artifact_id == b.target_id and expected_fields.get(s.name) != s.locator for s in claim.numeric_slots):
                    raise ValueError("INVALID_NUMERIC_LOCATOR")
        active_slots = [s for s in claim.numeric_slots if s.artifact_id in documents]
        for slot in active_slots:
            b = next(b for b in bindings if b.target_id == slot.artifact_id and b.status == "ACTIVE")
            if slot.artifact_hash != b.target_hash or slot.artifact_revision != b.target_revision:
                raise ValueError("NUMERIC_REVISION_MISMATCH")
            try:
                actual = pointer(documents[slot.artifact_id], slot.locator)
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError("INVALID_NUMERIC_LOCATOR") from exc
            if not numeric_equal(slot, actual) or slot.unit != unit_for(slot.locator, claim.scope):
                raise ValueError("NUMERIC_VALUE_OR_UNIT_MISMATCH")
            if slot.method != claim.scope.get("method", "UNKNOWN") or slot.estimand_id != claim.estimand_id:
                raise ValueError("NUMERIC_METHOD_OR_ESTIMAND_MISMATCH")
        if any(b.evidence_kind == "artifact" and b.status == "ACTIVE" for b in bindings) and not active_slots:
            raise ValueError("MISSING_MATERIAL_NUMERIC_SLOTS")

    def _edge(self, rid, from_type, from_id, to_type, to_id):
        if (from_type, from_id) == (to_type, to_id):
            raise ValueError("DERIVATION_CYCLE")
        pending, seen = [(to_type, to_id)], set()
        while pending:
            node = pending.pop()
            if node == (from_type, from_id):
                raise ValueError("DERIVATION_CYCLE")
            if node in seen:
                continue
            seen.add(node)
            pending.extend((r[0], r[1]) for r in self.db.execute("SELECT to_type,to_id FROM entity_edges WHERE research_id=? AND from_type=? AND from_id=? AND edge_type='depends_on'", (rid, *node)))
        self.db.execute("INSERT OR IGNORE INTO entity_edges VALUES(?,?,?,?,?,?,?,?,?)", (identity("EDGE", to_json([rid, from_type, from_id, to_type, to_id])), rid, from_type, from_id, "depends_on", to_type, to_id, utc_now().isoformat(), "ACTIVE"))

    def _insert(self, claim: Claim, bindings: list[EvidenceBinding]) -> None:
        claim.evidence_binding_ids = [b.binding_id for b in bindings]
        self.db.execute("UPDATE claim_revisions SET current=0 WHERE research_id=? AND claim_id=?", (claim.research_id, claim.claim_id))
        self.db.execute("INSERT INTO claim_revisions VALUES(?,?,?,?,?)", (claim.research_id, claim.claim_id, claim.revision, 1, to_json(claim)))
        for b in bindings:
            self.db.execute("INSERT INTO claim_bindings VALUES(?,?,?,?,?)", (claim.research_id, b.binding_id, claim.claim_id, claim.revision, to_json(b)))
            self._edge(claim.research_id, "claim", f"{claim.claim_id}@{claim.revision}", b.evidence_kind, b.target_id)
            for subject in b.verification_record_ids:
                for obligation in self.obligation_records(claim.research_id, subject):
                    self._edge(claim.research_id, "claim", f"{claim.claim_id}@{claim.revision}", "verifier", obligation.obligation_id)

    def publish(self, claim: Claim, bindings: list[EvidenceBinding]) -> Claim:
        if not self.config(claim.research_id).claim_evidence_provenance:
            raise ValueError("CLAIM_PROVENANCE_DISABLED")
        existing = self.db.execute("SELECT payload_json FROM claim_revisions WHERE research_id=? AND claim_id=? AND revision=?", (claim.research_id, claim.claim_id, claim.revision)).fetchone()
        if existing:
            saved = Claim.model_validate_json(existing[0])
            if (saved.text != claim.text or saved.scope != claim.scope or saved.numeric_slots != claim.numeric_slots
                    or self.bindings(saved) != sorted(bindings, key=lambda b: b.binding_id)):
                raise ValueError("CLAIM_IDEMPOTENCY_CONFLICT")
            return saved
        current = next((c for c in self.current(claim.research_id) if c.claim_id == claim.claim_id), None)
        if claim.revision != (current.revision + 1 if current else 1):
            raise ValueError("CLAIM_REVISION_CONFLICT")
        self.validate(claim, bindings)
        claim.support_state = support_state(bindings)
        claim.supersedes = current.revision if current else None
        self.state._planning_commit(claim.research_id, "CLAIM_VERIFIED", "claim", claim.claim_id, {"revision": claim.revision}, lambda: self._insert(claim, bindings))
        return claim

    def science_records(self, payload: StagedResult, mutation_id: str):
        science, rid = payload.scientific, payload.agent_result.research_id
        artifact = self.state._one("SELECT sha256 FROM artifacts WHERE artifact_id=? AND research_id=?", (science.stats_artifact_id, rid))
        result = payload.tool_result.result
        plan = payload.tool_request.args.get("plan", {})
        scope = {"method": science.method, "dataset_id": science.dataset_id,
                 "dataset_revision": next(iter(science.numeric_provenance.values())).dataset_sha256,
                 "variables": plan.get("variables", payload.tool_request.args.get("variables", {})),
                 "units": plan.get("units", {}), "sampling": plan.get("sampling", "UNKNOWN"),
                 "split": result.get("split", {}), "skill_version": result.get("skill_version"),
                 "alpha": result.get("diagnostics", {}).get("fitted_training_only", {}).get("alpha"),
                 "time_trace": result.get("trace", []), "horizon": 1 if science.method == "ridge_rolling_origin" else None,
                 "assumptions": plan.get("assumption_evidence", "UNKNOWN"),
                 "limitations": result.get("limitations", ["observational association is not causal"])}
        scope["analysis_plan"] = plan
        if payload.agent_result.provenance.get("qualified_profile"):
            scope["qualified_profile"] = payload.agent_result.provenance.get("qualified_scope", {})
            scope["units"] = {"value": scope["qualified_profile"].get("transform", {}).get("unit", "UNKNOWN")}
        if self.state.cycle5.enabled(rid):
            scope["cycle5_binding"] = payload.agent_result.provenance.get("cycle5_binding", {})
        claim_id = payload.agent_result.provenance.get("qualified_claim_id") if payload.agent_result.provenance.get("qualified_profile") else identity("CL", science.evidence_id)
        estimand = digest({k: scope[k] for k in ("method", "dataset_id", "variables")})
        revision = str(payload.agent_result.provenance.get("analysis_revision", 0))
        from .scientific_verifier import numeric_fields
        material = {"n": result.get("n")}
        material.update({k: v for k, v in result.items() if k in {"estimate", "p_value", "metrics", "uncertainty"}})
        if science.method == "two_period_comparison":
            material.update({k: result[k] for k in ("periods", "difference")})
        slots = [NumericSlot(name=name, locator="/result/" + name.replace(".", "/"), value=value,
                             unit=unit_for(name.replace(".", "/"), scope), method=science.method,
                             estimand_id=estimand, artifact_id=science.stats_artifact_id,
                             artifact_revision=revision, artifact_hash=artifact["sha256"])
                 for name, value in numeric_fields(material).items()]
        claim = Claim(claim_id=claim_id, research_id=rid, text=science.claim,
                      claim_type="comparison" if science.method == "two_period_comparison" else "prediction" if science.method.startswith("ridge_") else "association",
                      scope=scope, estimand_id=estimand, numeric_slots=slots, created_from=mutation_id)
        if payload.agent_result.provenance.get("qualified_profile"):
            claim.revision = payload.agent_result.provenance["qualified_claim_revision"]
        binding = EvidenceBinding(binding_id=identity("EB", mutation_id), claim_id=claim_id,
            claim_revision=claim.revision, evidence_kind="artifact", target_id=science.stats_artifact_id,
            target_revision=revision, target_hash=artifact["sha256"], relation="SUPPORTS",
            usage_type="NUMERIC_RESULT", locator={"experiment_id": science.experiment_id, "dataset_id": science.dataset_id},
            scope=scope, assumptions=scope["limitations"], verification_record_ids=[mutation_id])
        return claim, [binding]

    def stage_science(self, mutation_id, payload):
        if payload.scientific and self.config(payload.agent_result.research_id).claim_evidence_provenance:
            claim, bindings = self.science_records(payload, mutation_id)
            self.db.execute("INSERT OR IGNORE INTO slice_staging VALUES(?,?)", (mutation_id, to_json({"claim": claim, "bindings": bindings})))

    def commit_science(self, mutation_id, payload):
        if not payload.scientific or not self.config(payload.agent_result.research_id).claim_evidence_provenance:
            return
        self.stage_science(mutation_id, payload)
        row = self.state._one("SELECT payload_json FROM slice_staging WHERE mutation_id=?", (mutation_id,))
        staged = from_json(row[0])
        claim, bindings = Claim.model_validate(staged["claim"]), [EvidenceBinding.model_validate(b) for b in staged["bindings"]]
        self.validate(claim, bindings)
        # 어려운 수치 항목을 생략해도 검증 비율이 좋아지지 않게 한다.
        expected, _ = self.science_records(payload, mutation_id)
        if {s.name for s in claim.numeric_slots} != {s.name for s in expected.numeric_slots}:
            raise ValueError("INCOMPLETE_MATERIAL_CLAIM")
        claim.support_state = support_state(bindings)
        self._insert(claim, bindings)
        self._edge(claim.research_id, "artifact", bindings[0].target_id, "dataset", payload.scientific.dataset_id)
        if self.state.cycle5.enabled(claim.research_id):
            self._edge(claim.research_id, "artifact", bindings[0].target_id, "research", claim.research_id)
        if payload.scientific.plan_artifact_id:
            self._edge(claim.research_id, "artifact", bindings[0].target_id, "artifact", payload.scientific.plan_artifact_id)
        for obligation in self.obligation_records(claim.research_id, mutation_id):
            self._edge(claim.research_id, "claim", f"{claim.claim_id}@{claim.revision}", "verifier", obligation.obligation_id)

    def literature_claim(self, rid: str, evidence_id: str):
        if not self.config(rid).claim_evidence_provenance:
            return
        e = self.state._one("SELECT * FROM evidence WHERE research_id=? AND evidence_id=?", (rid, evidence_id))
        s = self.state._one("SELECT * FROM sources WHERE research_id=? AND source_id=?", (rid, e["source_id"]))
        cid = identity("CL", evidence_id)
        fulltext = e["text_field"] == "fulltext"
        access = "FULLTEXT" if fulltext else "ABSTRACT"
        scope = {"access_level": access, "source_id": s["source_id"], "hypothesis_id": e["target_hypothesis_id"], "limitations": from_json(e["limitations_json"])}
        if self.state.cycle5.enabled(rid):
            declared = [r for r in self.state.cycle5.records(rid, "source") if r.target_kind == "source" and r.target_id == s["source_id"]]
            if declared:
                scope["source_semantics"] = {r.record_id: digest(r) for r in declared}
        claim = Claim(claim_id=cid, research_id=rid, text=e["claim"], claim_type="literature_summary", scope=scope, created_from=evidence_id)
        binding = EvidenceBinding(binding_id=identity("EB", evidence_id), claim_id=cid, claim_revision=1,
            evidence_kind="source", target_id=s["source_id"], target_revision=s["metadata_hash"], target_hash=s["metadata_hash"],
            relation={"SUPPORT": "SUPPORTS", "CONTRADICT": "CONTRADICTS", "NEUTRAL": "QUALIFIES"}[e["polarity"]],
            usage_type="SOURCE_SPAN", locator={"field": e["text_field"], "span": e["evidence_text"], "section": e["evidence_location"],
                "page": int(e["evidence_location"].split()[-1]) if fulltext else None, "access_level": access, "url": s["url"], "doi": s["doi"], "retrieved_at": s["retrieved_at"],
                "evidence_id": evidence_id, "source_snapshot": {k: s[k] for k in ("title", "abstract", "url", "doi", "retrieved_at", "metadata_hash")}},
            scope=scope, assumptions=["fulltext_partial" if fulltext else "abstract_only"], verification_record_ids=[evidence_id])
        if fulltext:
            binding.locator["document"] = from_json(e["provenance_json"])["document"]
        self.validate(claim, [binding])
        claim.support_state = support_state([binding])
        self._insert(claim, [binding])
        if fulltext:
            for item in binding.locator["document"].values():
                if isinstance(item, dict) and item.get("artifact_id"):
                    self._edge(rid, "claim", f"{cid}@1", "artifact", item["artifact_id"])

    def _next_revision(self, claim, bindings):
        revised = claim.model_copy(deep=True)
        revised.revision += 1
        revised.supersedes = claim.revision
        revised.updated_at = utc_now().isoformat()
        copied = []
        for binding in bindings:
            b = binding.model_copy(deep=True)
            b.binding_id = identity("EB", f"{claim.claim_id}:{revised.revision}:{binding.binding_id}")
            b.claim_revision = revised.revision
            copied.append(b)
        return revised, copied

    def invalidate(self, rid, target_type, target_id, reason, *, within_transaction=False):
        cfg = self.config(rid)
        if not cfg.claim_evidence_provenance or not cfg.revision_invalidation:
            return 0
        # 기존 의존 그래프를 순회한다. 의미 관계를 도출 근거로 취급하지 않는다.
        affected, pending = set(), [(target_type, target_id)]
        while pending:
            node = pending.pop()
            if node in affected:
                continue
            affected.add(node)
            pending.extend((r[0], r[1]) for r in self.db.execute("SELECT from_type,from_id FROM entity_edges WHERE research_id=? AND to_type=? AND to_id=? AND edge_type='depends_on'", (rid, *node)))
        changes = []
        changed_verifier = next((r for r in self.obligation_records(rid) if r.obligation_id == target_id), None) if target_type == "verifier" else None
        for claim in self.current(rid):
            bindings = self.bindings(claim)
            stale_ids = {b.binding_id for b in bindings if b.status == "ACTIVE" and ((b.evidence_kind, b.target_id) in affected or changed_verifier and changed_verifier.subject_id in b.verification_record_ids)}
            if not stale_ids:
                continue
            revised, copied = self._next_revision(claim, bindings)
            for old, new in zip(bindings, copied):
                if old.binding_id in stale_ids:
                    new.status = "STALE"
            revised.support_state = "NEEDS_REVALIDATION"
            revised.invalidated_by.append(f"{target_type}:{target_id}:{reason}")
            changes.append((revised, copied))
        def write():
            for claim, bindings in changes:
                self._insert(claim, bindings)
        if changes:
            if within_transaction:
                write()
            else:
                self.state._planning_commit(rid, "CLAIM_INVALIDATED", target_type, target_id, {"reason": reason}, write)
        return len(changes)

    def revalidate(self, rid, claim_id):
        claim = next(c for c in self.current(rid) if c.claim_id == claim_id)
        if claim.support_state != "NEEDS_REVALIDATION":
            try:
                self.validate(claim, self.bindings(claim))
                return claim
            except Exception:
                pass
        revised, bindings = self._next_revision(claim, self.bindings(claim))
        for binding in bindings:
            if binding.status == "ACTIVE":
                try:
                    self.validate(revised, [binding])
                except Exception:
                    # 무결성 오류는 현재 지원을 취소하며 오래된 연결을 되살리지 않는다.
                    binding.status = "STALE"
        revised.support_state = support_state(bindings)
        self.state._planning_commit(rid, "CLAIM_REVALIDATED", "claim", claim_id, {"revision": revised.revision}, lambda: self._insert(revised, bindings))
        return revised

    def obligation_records(self, rid, subject=None):
        if not self.config(rid).verifier_dependency_catalog:
            return []
        return [VerifierObligation.model_validate_json(r[0]) for r in self.db.execute("SELECT payload_json FROM verifier_obligations WHERE research_id=?" + (" AND subject_id=?" if subject else "") + " ORDER BY obligation_id", (rid, subject) if subject else (rid,))]

    def record_verification(self, row, result):
        rid, subject = row["research_id"], row["mutation_id"]
        cfg = self.config(rid)
        if not cfg.verifier_dependency_catalog:
            return
        payload = StagedResult.model_validate_json(row["payload_json"])
        science = payload.scientific
        for check in result.checks:
            cid = check.check_id
            family = ("NUMERIC_RECOMPUTE" if "INDEPENDENT" in cid or "ARITHMETIC" in cid else
                      "SEMANTIC_REVIEW" if cid in {"SOURCE_SEMANTIC_MATCH", "GOAL_SCOPE_MATCH", "CLAIM_SUPPORT"} else
                      "NUMERIC_RECOMPUTE" if cid == "TRANSFORMATION_LINEAGE" else
                      "TIME_SPLIT_RECONSTRUCTION" if "TIME" in cid or "SPLIT" in cid or "LEAK" in cid else
                      "DATASET_MEMBERSHIP" if "MEMBERSHIP" in cid else
                      "ORIGINAL_CONTRACT_CONFORMITY" if "CONTRACT" in cid or "PLAN" in cid else "HASH_SCHEMA")
            dependencies = implementation_dependencies(cid)
            obligation = VerifierObligation(obligation_id=identity("OB", subject + ":" + cid), research_id=rid,
                subject_id=subject, check_id=cid, check_version=digest(dependencies), method_family=family,
                tested_revision=digest(from_json(row["payload_json"])), outcome="PASS" if check.passed else "FAIL",
                data_dependency=["dataset:" + science.dataset_id + ":" + next(iter(science.numeric_provenance.values())).dataset_sha256] if cfg.dependency_metadata and science else ["UNKNOWN"],
                implementation_dependency=dependencies if cfg.dependency_metadata else ["UNKNOWN"],
                evidence_dependency=["artifact:" + science.stats_artifact_id] if cfg.dependency_metadata and science else ["UNKNOWN"],
                evidence_refs=[result.verification_id])
            self.db.execute("INSERT OR REPLACE INTO verifier_obligations VALUES(?,?,?,?)", (rid, obligation.obligation_id, subject, to_json(obligation)))
            event_id = identity("OH", result.verification_id + ":" + obligation.obligation_id)
            self.db.execute("INSERT OR IGNORE INTO verifier_obligation_history VALUES(?,?,?)", (rid, event_id, to_json(obligation)))

    def catalog_gate(self, row, fresh):
        if not self.config(row["research_id"]).verifier_dependency_catalog:
            return
        records = self.obligation_records(row["research_id"], row["mutation_id"])
        required = {c.check_id for c in fresh.checks}
        revision = digest(from_json(row["payload_json"]))
        if required != {r.check_id for r in records} or any(r.outcome != "PASS" or r.tested_revision != revision or r.qualification_status == "FAILED" or r.check_version != digest(implementation_dependencies(r.check_id)) for r in records):
            raise ValueError("CRITICAL_OBLIGATION_NOT_PASSED")

    def change_qualification(self, rid, obligation_id, status):
        record = next(r for r in self.obligation_records(rid) if r.obligation_id == obligation_id)
        if record.qualification_status == status:
            return
        old = record.model_dump(mode="json")
        record.qualification_status = status
        def write():
            self.invalidate(rid, "verifier", obligation_id, "qualification changed", within_transaction=True)
            self.db.execute("UPDATE verifier_obligations SET payload_json=? WHERE research_id=? AND obligation_id=?", (to_json(record), rid, obligation_id))
            self.db.execute("INSERT INTO verifier_obligation_history VALUES(?,?,?)", (rid, identity("OH", obligation_id + ":" + utc_now().isoformat()), to_json(record)))
        self.state._planning_commit(rid, "VERIFIER_QUALIFICATION_CHANGED", "verifier", obligation_id,
                                    {"before": old, "after": record.model_dump(mode="json")}, write)

    def precommit(self, rid, contract, plan, models):
        self.state.cycle5.bind_plan(rid, contract, plan)
        cfg = self.config(rid)
        if not (cfg.claim_evidence_provenance or cfg.verifier_dependency_catalog):
            return
        key = "analysis_precommit:" + contract.contract_id
        dataset = self.state.dataset_record(plan.dataset_id, rid)
        skill = plan.skill_plan
        metadata = AnalysisPrecommit(analysis_plan_hash=digest(plan), contract_hash=digest(contract),
            estimand_id=digest({"dataset_id": plan.dataset_id, "method": plan.method, "variables": skill.variables if skill else {"x": plan.selected_variables[0], "y": plan.selected_variables[1]}}),
            dataset_revision=dataset["sha256"], primary_method=plan.method,
            split_policy={"holdout_fraction": skill.holdout_fraction, "lags": skill.lags, "min_train": skill.min_train} if skill else {},
            stopping_rule="fixed approved plan; bounded original contract", seed=skill.seed if skill else None,
            budget=contract.constraints.model_dump(mode="json"),
            versions={"models": models, "prompt": sha256_file(Path(__file__).with_name("prompts") / "analysis_planner_worker.md"),
                      "tool": sha256_file(Path(__file__).with_name("real_tools.py")), "policy": cfg.version},
            result_access_state="UNKNOWN")
        saved = self.state.runtime_step(rid, key)
        if saved and saved["output"] != metadata.model_dump(mode="json"):
            raise ValueError("ANALYSIS_PRECOMMIT_CHANGED")
        if not saved:
            self.state.finish_runtime_step(rid, key, metadata.model_dump(mode="json"), contract.contract_id)

    def snapshot(self, rid):
        cfg = self.config(rid)
        if not (cfg.claim_evidence_provenance or cfg.verifier_dependency_catalog):
            return None
        claims, bindings = [], []
        if cfg.claim_evidence_provenance:
            for row in self.db.execute("SELECT payload_json,current FROM claim_revisions WHERE research_id=? ORDER BY claim_id,revision", (rid,)):
                claim = Claim.model_validate_json(row[0])
                records = self.bindings(claim)
                effective = claim.support_state
                # 외부 변조로 쓰기 경계를 우회해도 조회 시 무결성을 검사한다.
                if row[1] and effective in {"SUPPORTED", "CONFLICTED", "NOT_SUPPORTED"}:
                    try:
                        self.validate(claim, records)
                    except Exception:
                        effective = "NEEDS_REVALIDATION"
                claims.append({**claim.model_dump(mode="json"), "current": bool(row[1]), "effective_support_state": effective})
                bindings.extend(b.model_dump(mode="json") for b in records)
        precommits = [from_json(r[0]) for r in self.db.execute("SELECT output_json FROM runtime_steps WHERE research_id=? AND step_key LIKE 'analysis_precommit:%'", (rid,))]
        snapshot = {"config": cfg.model_dump(mode="json"), "extension_schema_version": "1", "claims": claims, "bindings": bindings,
                "obligations": [r.model_dump(mode="json") for r in self.obligation_records(rid)], "analysis_precommits": precommits,
                "obligation_history": [from_json(r[0]) for r in self.db.execute("SELECT payload_json FROM verifier_obligation_history WHERE research_id=? ORDER BY rowid", (rid,))] if cfg.verifier_dependency_catalog else [],
                "staged_material_claims": [{"mutation_id": r[0], "status": r[1], **from_json(r[2])} for r in self.db.execute("SELECT sm.mutation_id,sm.status,ss.payload_json FROM slice_staging ss JOIN staged_mutations sm USING(mutation_id) WHERE sm.research_id=? ORDER BY sm.rowid", (rid,))] if cfg.claim_evidence_provenance else [],
                "live_provenance_efficacy": "NOT_VALIDATED", "live_reliability_efficacy": "NOT_VALIDATED"}
        if self.state.cycle5.enabled(rid):
            snapshot["cycle5"] = self.state.cycle5.snapshot(rid)
        return snapshot


def shared_dependencies(records: list[VerifierObligation]) -> list[dict]:
    pairs = []
    for index, a in enumerate(records):
        for b in records[index + 1:]:
            shared = {field: sorted((set(getattr(a, field)) & set(getattr(b, field))) - {"UNKNOWN"})
                      for field in ("data_dependency", "implementation_dependency", "evidence_dependency")}
            pairs.append({"checks": [a.check_id, b.check_id], "shared": shared,
                          "independence": "NOT_ESTABLISHED"})
    return pairs
