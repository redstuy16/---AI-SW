"""최종 소스에서 기존 파괴적·데모·새 프로세스 복구·내보내기 표식을 재검증한다."""
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "qa"))
import qa_day1 as qa
from probe.preflight import _source_fingerprint, record_qa_validation

qa.BUILD = ROOT / "build" / "live-record-final-gates" / uuid4().hex
qa.QA = qa.BUILD
qa.BUILD.mkdir(parents=True)
fingerprint = _source_fingerprint()
stress = qa.stress_suite()
qa.save("stress_results.json", stress)
repeat, exemplars = qa.demo_repeatability()
qa.save("demo_repeatability.json", repeat)
fresh = qa.BUILD / "fresh-process"
first = qa.run([sys.executable, str(ROOT / "qa/qa_day1.py"), "--probe", "crash", "--root", str(fresh)])
assert first.returncode == 0
second = qa.run([sys.executable, str(ROOT / "qa/qa_day1.py"), "--probe", "resume", "--root", str(fresh)])
assert second.returncode == 0
resume = json.loads((fresh / "resume.json").read_text(encoding="utf-8", errors="strict"))
release = qa.validate_release(exemplars["A"])
qa.save("artifact_validation.json", release)
assert stress["all_executable_passed"] and all(repeat["passed"].values()) and resume["passed"] and release["passed"]
assert fingerprint == _source_fingerprint()
record_qa_validation("stress", True, {"source_fingerprint": fingerprint, "fresh_process_resume": True,
                                     "scenarios": len(stress["scenarios"]), "results": str(qa.BUILD)})
record_qa_validation("artifact", True, {"source_fingerprint": fingerprint, "file_count": release["file_count"],
                                       "results": str(qa.BUILD)})
result = {"source_fingerprint": fingerprint, "source_unchanged": True, "stress": stress,
          "demo_repeatability": repeat, "fresh_process_resume": resume, "release": release,
          "artifacts": str(qa.BUILD), "live_paid_calls": 0}
text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
text.encode("utf-8", errors="strict")
(ROOT / "qa/results/live_api_recording_final_gates.json").write_text(text, encoding="utf-8", errors="strict")
print(json.dumps({"stress": stress["pytest"], "demo": repeat["passed"], "fresh_resume": resume["passed"],
                  "release": release["passed"], "file_count": release["file_count"]}, ensure_ascii=False))
