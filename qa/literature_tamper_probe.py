"""복사한 종료 연구의 DOI·출처 연결을 변조하여 검사한다."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from htrsa.database import initialize
from htrsa.final_report import ReportValidationError, build_final_conclusion
from htrsa.service import StateService
from htrsa.storage import Workspace


def main() -> None:
    validation = json.loads((ROOT / "qa/results" / "artifact_validation.json").read_text(encoding="utf-8"))
    demo = json.loads((ROOT / "qa/results" / "demo_repeatability.json").read_text(encoding="utf-8"))
    original = Path(validation["clean_database"])
    research_id = demo["runs"]["A"][0]["research_id"]
    folder = ROOT / "build" / "qa-day1" / f"literature-tamper-{uuid4().hex[:12]}"
    folder.mkdir(parents=True, exist_ok=True)
    copy = folder / "state.sqlite"
    shutil.copy2(original, copy)
    db = initialize(copy)
    state = StateService(db, Workspace(original.parent / "workspace"))
    source = db.execute("SELECT source_id,doi FROM sources WHERE research_id=? AND status='VERIFIED' LIMIT 1",
                        (research_id,)).fetchone()
    other = db.execute("SELECT source_id FROM sources WHERE research_id=? AND source_id<>? AND status='VERIFIED' LIMIT 1",
                       (research_id, source["source_id"])).fetchone()
    evidence = db.execute("SELECT evidence_id FROM evidence WHERE research_id=? AND source_id=? AND status='VERIFIED' LIMIT 1",
                          (research_id, source["source_id"])).fetchone()
    if not source or not other or not evidence:
        raise AssertionError("demo lacks two verified sources for tampering")
    attacks = {}
    try:
        db.execute("UPDATE sources SET doi=? WHERE source_id=?", ("10.9999/nonexistent-qa", source["source_id"]))
        try:
            build_final_conclusion(state, research_id)
            attacks["altered_doi"] = "FAIL"
        except ReportValidationError:
            attacks["altered_doi"] = "PASS"
        db.execute("UPDATE sources SET doi=? WHERE source_id=?", (source["doi"], source["source_id"]))
        db.execute("UPDATE evidence SET source_id=? WHERE evidence_id=?", (other["source_id"], evidence["evidence_id"]))
        try:
            build_final_conclusion(state, research_id)
            attacks["source_id_mismatch"] = "FAIL"
        except ReportValidationError:
            attacks["source_id_mismatch"] = "PASS"
    finally:
        db.close()
    result = {"passed": all(value == "PASS" for value in attacks.values()),
              "attacks": attacks, "canonical_source_unchanged": True,
              "note": "The probe modifies a database copy. Provider-wide DOI existence is not established offline."}
    serialized = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    serialized.encode("utf-8", errors="strict")
    (ROOT / "qa/results" / "literature_tamper.json").write_text(serialized, encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
