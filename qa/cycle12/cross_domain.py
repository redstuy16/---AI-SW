"""고정 천문 자료로 공통 선택·의미·형식 검사를 실행한다. 운영 프로필은 등록하지 않는다."""
import csv
from copy import deepcopy
from decimal import Decimal
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from htrsa.period_comparison import selected_values, compare_keyed, selection_equivalent, require_meaning
from htrsa.qualified_profiles import registry
from htrsa.storage import sha256_file


def main():
    directory = Path(__file__).parent / "public"
    primary_path, secondary_path = directory / "trappist1_default.csv", directory / "trappist1_default.json"
    assert sha256_file(primary_path) == "147b89cda0970e00435d8b316542ca32afbe11d3f2bd638ad71275f9a5fa928b"
    assert sha256_file(secondary_path) == "4dbb44370638af750a6bc41543f6a0244ab5b4de3fbcfa3f785f8f59f8a91277"
    primary = list(csv.DictReader(primary_path.read_text(encoding="utf-8").splitlines()))
    secondary = json.loads(secondary_path.read_text(encoding="utf-8"))
    meaning = {"product": "NASA Exoplanet Archive PS", "quantity_kind": "orbital_period_duration", "unit": "day",
               "selection": "hostname='TRAPPIST-1' and default_flag=1", "value_column": "pl_orbper"}
    require_meaning(meaning, dict(meaning))
    assert all(row["hostname"] == "TRAPPIST-1" and int(row["default_flag"]) == 1 for row in primary)
    names = [row["pl_name"] for row in primary]
    assert not compare_keyed(primary, secondary, names, key_column="pl_name", value_column="pl_orbper")
    original = {"groups": {"A": names[:3], "B": names[3:]}, "direction": "B-A", "weights": None}
    amended = {"groups": {"A": names[:2], "B": names[2:]}, "direction": "B-A", "weights": None}
    snapshots = []
    for revision, contract in enumerate((original, amended), 1):
        means = []
        for group in ("A", "B"):
            selected = selected_values(primary, contract["groups"][group], key_column="pl_name", value_column="pl_orbper")
            amounts = [value for _, value in selected]
            result = statistics.fmean(amounts)
            independent = sum(amounts, Decimal(0)) / Decimal(len(amounts))
            assert math.isclose(result, float(independent), rel_tol=1e-12, abs_tol=1e-12)
            means.append(independent)
        difference = means[1] - means[0]
        snapshots.append({"plan_revision": revision, "selected_keys": contract["groups"],
            "means_day": [str(value) for value in means], "difference_day": str(difference),
            "display_operation": "MULTIPLY_BY_24", "difference_hour": str(difference * 24),
            "meaning": meaning, "authorization": "REFERENCE_AUTHOR_DEFINED_DIAGNOSTIC", "teacher_review": "NOT_VALIDATED"})
    permuted = deepcopy(original)
    permuted["groups"]["A"].reverse()
    assert selection_equivalent(original, permuted, method="unweighted_mean")
    assert not selection_equivalent(original, permuted, method="ordered_timeseries")
    assert not selection_equivalent(original, amended, method="unweighted_mean")
    corrupted = deepcopy(secondary)
    corrupted[0]["pl_orbper"], corrupted[1]["pl_orbper"] = corrupted[1]["pl_orbper"], corrupted[0]["pl_orbper"]
    differing = compare_keyed(primary, corrupted, names, key_column="pl_name", value_column="pl_orbper")
    assert differing == names[:2]
    try:
        require_meaning(meaning, dict(meaning, quantity_kind="transit_epoch"))
    except ValueError as error:
        quantity_status = str(error)
    else:
        raise AssertionError("같은 day 단위의 다른 물리량이 승인되었습니다")
    result = {"execution": "FROZEN_CROSS_DOMAIN_DIAGNOSTIC", "passed": True, "source_families": 1,
              "captured_rows": len(primary), "snapshots": snapshots, "swapped_keys_detected": differing,
              "same_unit_wrong_quantity": quantity_status, "retained_fields": ["pl_refname", "rowupdate", "pl_orbpererr1", "pl_orbpererr2"],
              "runtime_routing": registry().choose("TRAPPIST-1의 공전 주기 평균 비교")["status"],
              "production_qualification": "NOT_QUALIFIED", "paid_calls": 0, "live_efficacy": "NOT_VALIDATED",
              "limitations": ["동일 카탈로그의 두 형식이며 독립 관측이 아닙니다.", "카탈로그 점 추정값입니다. 공분산을 반영한 불확도 전파는 구현하지 않았습니다.",
                              "HTML 참조는 실행하지 않는 원문입니다. 취득 manifest는 참조 작성자의 기록이며 이번 원래 HTTP 취득을 관측하지 않았습니다."]}
    data = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict")
    output = ROOT / "build/cycle12/cross_domain_results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
