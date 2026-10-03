"""최종 소스에서 기존 파괴적 검사·반복 데모·출시 검증을 재실행한다."""
import json
from pathlib import Path

import qa_day1 as qa
from htrsa.preflight import _source_fingerprint, record_qa_validation


root = Path(__file__).resolve().parents[1]
qa.QA = root / "build/product-frozen-qa"
fingerprint = _source_fingerprint()
stress = qa.stress_suite()
qa.save("stress_results.json", stress)
repeat, exemplar = qa.demo_repeatability()
qa.save("demo_repeatability.json", repeat)
release = qa.validate_release(exemplar["A"])
qa.save("artifact_validation.json", release)
unchanged = fingerprint == _source_fingerprint()
assert unchanged and stress["all_executable_passed"] and all(repeat["passed"].values()) and release["passed"]
record_qa_validation("stress", True, {"source_fingerprint_at_start": fingerprint, "scenarios": len(stress["scenarios"]), "source_unchanged": unchanged})
record_qa_validation("artifact", True, {"file_count": release["file_count"], "source_unchanged": unchanged})
result = {"passed": True, "source_fingerprint": fingerprint, "source_unchanged": unchanged,
          "stress": stress["pytest"], "demos": repeat["passed"], "release": release}
qa.save("summary.json", result)
print(json.dumps({"passed": True, "stress_count": stress["pytest"]["passed_count"], "demos": repeat["passed"], "release_file_count": release["file_count"], "source_unchanged": unchanged}, ensure_ascii=True))
