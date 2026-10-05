"""기존 파괴적·데모·F3-P·복구·내보내기 검사를 별도 결과로 실행한다."""
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "qa")]
import qa_day1 as qa
import f3p_eval
from f3p_recovery_probe import BOUNDARIES
from probe.preflight import record_qa_validation, environment_status, _source_fingerprint


def save(path, value):
    def portable(item):
        if isinstance(item, str) and item.startswith((str(ROOT) + "\\", str(ROOT) + "/")):
            return Path(item).relative_to(ROOT).as_posix()
        if isinstance(item, dict):
            return {k: portable(v) for k, v in item.items()}
        if isinstance(item, list):
            return [portable(v) for v in item]
        return item
    data = (json.dumps(portable(value), ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def command(arguments, output, expected=0):
    process = subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=240)
    data = (process.stdout + process.stderr).encode("utf-8", errors="strict")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    if process.returncode != expected:
        raise RuntimeError(f"검사 실패 {process.returncode}: " + process.stderr[-2000:] + process.stdout[-1000:])
    return process


def main():
    base = ROOT / "build/cycle12/offline" / uuid4().hex
    base.mkdir(parents=True)
    qa.BUILD = base / "qa-day1"
    qa.QA = base / "qa"
    qa.QA.mkdir(parents=True)
    stress = qa.stress_suite()
    save(base / "stress.json", stress)
    print(json.dumps({"phase": "stress", "passed": stress["all_executable_passed"], "tests": stress["pytest"]}), flush=True)
    repeat, exemplars = qa.demo_repeatability()
    save(base / "demo.json", repeat)
    fresh = base / "fresh-process"
    for phase in ("crash", "resume"):
        command([sys.executable, "-B", "-X", "utf8", "qa/qa_day1.py", "--probe", phase, "--root", str(fresh)], base / f"general-{phase}.log")
    resume = json.loads((fresh / "resume.json").read_text(encoding="utf-8"))
    release = qa.validate_release(exemplars["A"])
    save(base / "release.json", release)
    print(json.dumps({"phase": "demo-release", "repeat": repeat["passed"], "resume": resume["passed"], "release": release["passed"]}), flush=True)
    config = json.loads(f3p_eval.CONFIG.read_text(encoding="utf-8"))
    evaluation = base / "f3p-evaluation"
    cases = [f3p_eval.run_case(case, evaluation / case["id"], config["dataset_seed"]) for case in config["cases"]]
    save(base / "f3p.json", {"cases": cases, "live_efficacy": "NOT_VALIDATED"})
    boundaries = []
    for boundary in BOUNDARIES:
        folder = base / "f3p-recovery" / boundary
        for phase in ("crash", "resume"):
            command([sys.executable, "-B", "-X", "utf8", "qa/f3p_recovery_probe.py", "--phase", phase, "--folder", str(folder), "--boundary", boundary], base / f"{boundary}-{phase}.log")
        boundaries.append(json.loads((folder / "resume.json").read_text(encoding="utf-8")))
    profiles = []
    for boundary in ("profile_after_verify", "profile_after_commit"):
        folder = base / "qualified-recovery" / boundary
        for phase in ("crash", "resume"):
            command([sys.executable, "-B", "-X", "utf8", "qa/cycle12/recovery_probe.py", "--phase", phase, "--folder", str(folder), "--boundary", boundary], base / f"{boundary}-{phase}.log", 79 if phase == "crash" else 0)
        profiles.append(json.loads((folder / "resume.json").read_text(encoding="utf-8")))
    exports = []
    (base / "qa/results").mkdir(parents=True, exist_ok=True)
    for clean in (False, True):
        # 기존 검사 로직은 그대로 사용하고 결과 저장 위치만 이번 실행으로 바꾼다.
        code = "import sys;from pathlib import Path;sys.path.insert(0,'qa');import f3p_export_probe as probe;probe.ROOT=Path(" + repr(str(base)) + ");sys.argv=['f3p_export_probe','--evaluation-dir'," + repr(str(evaluation)) + "]" + ("+['--clean']" if clean else "") + ";probe.main()"
        result = command([sys.executable, "-B", "-X", "utf8", "-c", code], base / f"export-{clean}.log")
        exports.append({"clean": clean, "result": json.loads(result.stdout)})
    passed = (stress["all_executable_passed"] and all(repeat["passed"].values()) and resume["passed"] and release["passed"] and
              all(c["fixture_correct"] for c in cases) and all(b["passed"] for b in boundaries) and all(p["passed"] for p in profiles) and
              all(e["result"]["all_passed"] for e in exports))
    record_qa_validation("stress", passed, {"scenarios": len(stress["scenarios"]), "fresh_process_resume": resume["passed"], "cycle12_profiles": len(profiles)})
    record_qa_validation("artifact", release["passed"] and all(e["result"]["all_passed"] for e in exports), {"file_count": release["file_count"]})
    result = {"source_fingerprint": _source_fingerprint(), "stress": stress, "demo_repeatability": repeat, "fresh_process_resume": resume,
              "release": release, "f3p_faults": cases, "f3p_recovery": boundaries, "qualified_recovery": profiles,
              "f3p_exports": exports, "environment": environment_status(), "passed": passed, "output_dir": str(base),
              "paid_calls": 0, "live_efficacy": "NOT_VALIDATED", "execution": "OFFLINE_FAKE_AGENT_REAL_TOOLS"}
    save(ROOT / "build/cycle12/offline_results.json", result)
    print(json.dumps({"passed": passed, "f3p_cases": len(cases), "f3p_recovery_boundaries": len(boundaries), "qualified_boundaries": len(profiles), "output_dir": str(base)}))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
