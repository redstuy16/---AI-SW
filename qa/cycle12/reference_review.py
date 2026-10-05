"""동결한 참조 패키지의 해시·정책 재실행·추가 진단을 앱 검사와 분리한다."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from probe.sandbox import clean_environment

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = Path.home() / "Downloads/PROBE_Cycle12_Reference_2026-10-03.zip"
EXPECTED = "06479024d75ce9eff01399fe61b84acd631ca32a0dd253fc28086c79a9b8341f"
FROZEN = ROOT / "build/cycle12/reference_frozen"


def digest(data):
    return sha256(data).hexdigest()


def inspect_inputs():
    if digest(PACKAGE.read_bytes()) != EXPECTED:
        raise ValueError("참조 ZIP 해시가 명세와 다릅니다")
    with zipfile.ZipFile(PACKAGE) as archive:
        for item in archive.infolist():
            path = (FROZEN / item.filename).resolve()
            if not path.is_relative_to(FROZEN.resolve()) or item.file_size > 2_000_000 or item.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError("참조 패키지 경로·크기가 허용 범위를 벗어납니다")
            if item.is_dir():
                continue
            data = archive.read(item)
            if item.filename.endswith((".py", ".md", ".json", ".csv", ".txt")):
                data.decode("utf-8", errors="strict").encode("utf-8", errors="strict")
            if path.exists():
                if path.read_bytes() != data:
                    raise ValueError("기존 참조 파일이 바뀌었습니다: " + item.filename)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
    manifest = json.loads((FROZEN / "PACKAGE_MANIFEST.json").read_text(encoding="utf-8"))
    for name, record in manifest["files"].items():
        data = (FROZEN / name).read_bytes()
        assert len(data) == record["bytes"] and digest(data) == record["sha256"], name
    freeze = json.loads((FROZEN / "freeze_manifest.json").read_text(encoding="utf-8"))
    for name, expected in freeze["files"].items():
        assert digest((FROZEN / name).read_bytes()) == expected, name
    return len(manifest["files"]), len(freeze["files"])


def main():
    package_count, frozen_count = inspect_inputs()
    # 고정 해시로 확인하고 읽어 검토한 참조 코드만 평가자 프로세스에서 실행한다.
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(FROZEN / "run_reference.py"), "observed_cycle12_reference"],
                            cwd=FROZEN, env=clean_environment(), capture_output=True, text=True, encoding="utf-8", timeout=60)
    if result.returncode:
        raise RuntimeError("참조 재실행 실패: " + result.stderr[-1000:])
    observed = FROZEN / "observed_cycle12_reference"
    comparisons = {}
    for name in ("summary.json", "results.json", "results.csv"):
        previous, current = (FROZEN / "replay" / name).read_bytes(), (observed / name).read_bytes()
        comparisons[name] = {"byte_identical": previous == current,
            "line_endings_normalized_identical": previous.replace(b"\r\n", b"\n") == current.replace(b"\r\n", b"\n"),
            "semantic_identical": json.loads(previous) == json.loads(current) if name.endswith(".json") else None,
            "original_sha256": digest(previous), "observed_sha256": digest(current)}
        assert comparisons[name]["line_endings_normalized_identical"], name
    spec = importlib.util.spec_from_file_location("cycle12_reference_policies", FROZEN / "policies.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = next(case for case in json.loads((FROZEN / "cases.json").read_text(encoding="utf-8")) if case["case_id"] == "case_001")
    probes = []
    for name in ("baseline_only", "aggregation_only", "mean_order_only", "missing_selected_value", "required_representation_absent"):
        case = deepcopy(base)
        if name == "baseline_only": case["source_witness"]["baseline"] = "1991-2020"
        elif name == "aggregation_only": case["source_witness"]["aggregation"] = "monthly_Jan"
        elif name == "mean_order_only": case["approved_goal"]["groups"]["A"].reverse()
        elif name == "missing_selected_value":
            case["approved_goal"]["groups"]["A"][-1] = "2026"
            case["goal_witness"] = deepcopy(case["approved_goal"])
        else:
            case["secondary_rows"] = None
            case["secondary_required"] = True
        try:
            status, reason = module.s2(case)
            probes.append({"probe": name, "status": status, "reason": reason})
        except TypeError as error:
            probes.append({"probe": name, "status": "UNHANDLED_EXCEPTION", "exception_type": type(error).__name__})
    assert [item["status"] for item in probes] == ["READY", "READY", "GOAL_CONFLICT", "UNHANDLED_EXCEPTION", "CHECK_PENDING"]
    inspect_inputs()
    summary = json.loads((observed / "summary.json").read_text(encoding="utf-8"))
    record = {"execution": "REFERENCE_POLICY_REPLAY_ONLY", "package_sha256": EXPECTED,
              "package_files_checked": package_count, "freeze_files_checked": frozen_count,
              "case_count": summary["case_count"], "source_families": summary["real_source_families"],
              "policy_evaluations": summary["policy_evaluations"], "comparisons": comparisons,
              "additional_probes": probes, "summary": summary, "app_or_live_agent_run": False,
              "teacher_use": "NOT_VALIDATED", "python": sys.version, "platform": platform.platform(), "passed": True}
    text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    (ROOT / "build/cycle12/reference_review.json").write_bytes(text.encode("utf-8", errors="strict"))
    print(json.dumps({"passed": True, "case_count": record["case_count"], "policy_evaluations": record["policy_evaluations"],
                      "package_files_checked": package_count, "freeze_files_checked": frozen_count, "comparisons": comparisons}, ensure_ascii=False))


if __name__ == "__main__":
    main()
