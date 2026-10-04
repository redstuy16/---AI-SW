"""재사용한 v4 평가 함수의 16개 기대 상태를 이번 결과 폴더에 보관한다."""
import csv
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "qa/qualified_profiles")]
import evaluate as existing
from htrsa.climate_profile import MEANING, parse_source
from htrsa.preflight import _source_fingerprint


def main():
    folder = ROOT / "build/cycle12/v4-evaluation" / uuid4().hex
    cases = [("normal", key) for key in ("comparison", "fahrenheit", "baseline", "partial")]
    cases += [("fault", key) for key in ("absolute_offset", "storage", "local_claim", "wrong_period", "missing_duplicate", "causal_claim")]
    cases += [("change", key) for key in ("used", "semantic", "unused")]
    cases += [("ambiguous", key) for key in ("ambiguous", "forecast", "causal")]
    values = []
    for category, key in cases:
        value = existing.run_case(folder / uuid4().hex, category, key)
        values.append(value)
        print(json.dumps({"case": key, "expected": value["expected"], "observed": value["observed"], "correct": value["correct"]}), flush=True)
    source_rows = parse_source(existing.SOURCE.read_text(encoding="utf-8"), MEANING)
    csv_rows = csv.DictReader((existing.SOURCE.parent / "gistemp.csv").read_text(encoding="utf-8").splitlines()[1:])
    official = {int(row["Year"]): row["J-D"] for row in csv_rows}
    matched = all(abs(float(row["value"]) - float(official[row["year"]])) < 1e-10 for row in source_rows if row["value"] is not None)
    result = {"execution": "EXISTING_V4_HARNESS_FAKE_AGENT_REAL_TOOLS", "cases": values,
              "source_scale_pair_matched": matched, "source_fingerprint": _source_fingerprint(),
              "categories": {category: sum(value["category"] == category for value in values) for category in ("normal", "fault", "change", "ambiguous")},
              "all_passed": all(value["correct"] for value in values) and matched,
              "source_families": 1, "paid_calls": 0, "live_efficacy": "NOT_VALIDATED", "output_dir": folder.relative_to(ROOT).as_posix(),
              "limitations": ["16개는 같은 기후 자료의 설계 변형이며 Live 효능·경쟁 비교·독립 연구 16건이 아닙니다."]}
    data = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict")
    (ROOT / "build/cycle12/v4_evaluation.json").write_bytes(data)
    if not result["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
