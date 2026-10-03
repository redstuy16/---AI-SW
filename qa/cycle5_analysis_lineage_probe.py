"""선언한 변환 dataset을 실제 분석·반영·상위 의미 무효화에 연결한다."""
import asyncio
import csv
from decimal import Decimal
import json
from pathlib import Path
from uuid import uuid4
from cycle5_fixtures import csv_content, dataset, semantic, science_fixture
from htrsa.cycle5 import TransformationLineage
from htrsa.preflight import _source_fingerprint
from htrsa.schemas import ResearchContract
from htrsa.service import StateConflictError

ROOT = Path(__file__).resolve().parents[1]


def main():
    fp = _source_fingerprint()
    folder = ROOT / "build" / ("cycle5-analysis-lineage-" + uuid4().hex)
    cases = []
    for healthy in (True, False):
        db, state, runtime, prepared, oracle = science_fixture(folder / str(healthy))
        rid, child_id = prepared["research_id"], prepared["dataset_id"]
        try:
            child = state.dataset_record(child_id, rid)
            with state.workspace.path(rid, child["stored_path"]).open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            for row in rows:
                for col in ("a", "b"): row[col] = str(Decimal(row[col]) / Decimal("1.8"))
            parent = dataset(state, rid, "D-parent", csv_content(rows, headers=("unit", "a", "b")))
            refs = {}
            for col in ("a", "b"):
                record = semantic(state, rid, "SM-parent-" + col, parent["dataset_id"], col, unit=col + "-units")
                refs[record.record_id] = record.revision
                refs["SM-" + col] = 2
            contract = ResearchContract(contract_id="C-lineage", research_id=rid, task_type="fixed_transformation",
                issued_by="owner", assigned_role="analysis_planner_worker", objective="소유자가 고정한 선형 변환",
                inputs=[{"type": "dataset", "id": parent["dataset_id"]}], allowed_tools=["analysis.skill"], output_schema_id="AnalysisPlan")
            state.issue_contract(contract)
            state.cycle5.declare_lineage(TransformationLineage(record_id="TL-analysis", research_id=rid, contract_id=contract.contract_id,
                parent_kind="dataset", parent_id=parent["dataset_id"], parent_hash=parent["sha256"], child_kind="dataset", child_id=child_id,
                child_hash=child["sha256"], key_columns=["unit"], columns={"a": "a", "b": "b"}, operation="affine",
                scale=1.8 if healthy else 2, offset=0, semantic_refs=refs), actor_role="owner")
            error, result = None, None
            try: result = asyncio.run(runtime.resume(rid))
            except Exception as exc: error = type(exc).__name__
            commits = db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (rid,)).fetchone()[0]
            correct, stale = False, False
            if healthy and result is not None:
                correct = abs(result["result"]["metrics"]["estimate"] - oracle) < 1e-10
                parent_semantic = next(s for s in state.cycle5.records(rid, "source") if s.record_id == "SM-parent-a")
                state.cycle5.propose_source(parent_semantic.model_copy(update={"revision": 3, "review_status": "NEEDS_REVIEW", "reviewed_by": None}), actor_role="analysis_planner_worker")
                try: state.cycle5.validate_completed(rid)
                except StateConflictError: stale = True
            passed = (healthy and commits == 1 and correct and stale and error is None) or (not healthy and commits == 0 and error is not None)
            cases.append({"healthy": healthy, "commits": commits, "correct_against_independent_numeric_reference": correct,
                          "parent_semantic_change_blocks_old_pass": stale, "halted": error, "passed": passed})
        finally: db.close()
    result = {"all_passed": all(c["passed"] for c in cases), "cases": cases, "source_fingerprint": fp,
              "source_unchanged": fp == _source_fingerprint(), "live_api_calls": 0, "live_efficacy": "NOT_VALIDATED"}
    (ROOT / "qa/results/cycle5_analysis_lineage_results.json").write_bytes((json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict"))
    print(json.dumps(result, ensure_ascii=False))
    if not result["all_passed"] or not result["source_unchanged"]: raise SystemExit(1)


if __name__ == "__main__": main()
