"""연도 키가 있는 수치 자료의 두 구간을 기술적으로 비교한다."""
import math
import statistics
from decimal import Decimal, InvalidOperation


class SelectedDataError(ValueError):
    """선택한 자료의 결함을 계산 전에 식별한다."""
    def __init__(self, code, keys=()):
        self.code = code
        self.keys = list(keys)[:100]
        super().__init__(code + ": 필요한 자료 값이 없거나 읽을 수 없습니다.")


def numeric_value(value, key):
    if value is None or value == "":
        raise SelectedDataError("SELECTED_VALUE_MISSING", [key])
    if isinstance(value, bool):
        raise SelectedDataError("SELECTED_VALUE_INVALID", [key])
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise SelectedDataError("SELECTED_VALUE_INVALID", [key]) from None
    if not number.is_finite() or not math.isfinite(float(number)):
        raise SelectedDataError("SELECTED_VALUE_NONFINITE", [key])
    return number


def selected_values(rows, keys, *, key_column="year", value_column="value", year_keys=None):
    """선택 순서를 보존하며 키와 수치를 검사한다. 비선택 값은 계산하지 않는다."""
    keys = list(keys)
    if not keys:
        raise SelectedDataError("SELECTED_GROUP_EMPTY")
    if any(not isinstance(k, (str, int)) or isinstance(k, bool) or k == "" for k in keys):
        raise SelectedDataError("SELECTED_KEY_INVALID")
    if len(set(keys)) != len(keys):
        raise SelectedDataError("SELECTED_KEY_DUPLICATE", keys)
    wanted, found = set(keys), {}
    for row in rows:
        if key_column not in row:
            raise SelectedDataError("SOURCE_KEY_MISSING")
        key = row[key_column]
        if year_keys is True or year_keys is None and key_column == "year":
            if isinstance(key, bool) or not str(key).isdigit() or len(str(key)) != 4:
                raise SelectedDataError("SOURCE_KEY_INVALID", [str(key)])
            key = int(key)
        if key not in wanted:
            continue
        if key in found:
            raise SelectedDataError("SELECTED_KEY_DUPLICATE", [key])
        found[key] = numeric_value(row.get(value_column), key)
    missing = wanted - found.keys()
    if missing:
        raise SelectedDataError("SELECTED_KEY_MISSING", sorted(missing))
    return [(key, found[key]) for key in keys]


def selection_equivalent(expected, actual, *, method):
    """평균만 순열을 허용한다. 그룹·방향·가중치 등 다른 계약은 그대로 비교한다."""
    if set(expected) != set(actual) or not isinstance(expected.get("groups"), dict) or not isinstance(actual.get("groups"), dict):
        return False
    if {k: v for k, v in expected.items() if k != "groups"} != {k: v for k, v in actual.items() if k != "groups"}:
        return False
    if expected["groups"].keys() != actual["groups"].keys():
        return False
    for name, keys in expected["groups"].items():
        other = actual["groups"][name]
        if not isinstance(keys, list) or not isinstance(other, list) or not keys or not other:
            return False
        if any(not isinstance(k, (str, int)) or isinstance(k, bool) for k in keys + other):
            return False
        if len(set(keys)) != len(keys) or len(set(other)) != len(other):
            return False
        if method in {"unweighted_mean", "two_period_comparison"}:
            if set(keys) != set(other):
                return False
        elif keys != other:
            return False
    return True


def compare_keyed(primary, secondary, keys, *, key_column="year", value_column="value"):
    left = selected_values(primary, keys, key_column=key_column, value_column=value_column)
    right = dict(selected_values(secondary, keys, key_column=key_column, value_column=value_column))
    return [key for key, value in left if value != right[key]]


def require_meaning(expected, actual):
    """해당 절차가 지정한 의미 필드만 검사한다. 단위 일치로 수량 의미를 대신하지 않는다."""
    if not isinstance(actual, dict) or any(field not in actual for field in expected):
        raise ValueError("SOURCE_MEANING_REVIEW_REQUIRED")
    if actual != expected:
        raise ValueError("SOURCE_MEANING_CONFLICT")


def compare_periods(rows, variables, periods):
    if set(variables) != {"year", "value"} or len(periods) != 2:
        raise ValueError("두 기간과 연도·값 열이 필요합니다")
    groups, selected, used = [], set(), []
    for period in periods:
        if len(period) != 2 or any(type(y) is not int for y in period) or not 1000 <= period[0] <= period[1] <= 9999:
            raise ValueError("기간이 올바르지 않습니다")
        years = set(range(period[0], period[1] + 1))
        if selected & years:
            raise ValueError("비교 기간이 겹칩니다")
        selected |= years
        values = dict(selected_values(rows, sorted(years), key_column=variables["year"], value_column=variables["value"], year_keys=True))
        groups.append({"start": period[0], "end": period[1], "n": len(values), "mean": statistics.fmean(values.values())})
        used.extend(sorted(values))
    return {"method": "two_period_comparison", "n": len(used), "periods": groups,
            "difference": groups[1]["mean"] - groups[0]["mean"],
            "limitations": ["두 기간의 관측 평균 차이입니다. 인과관계·예측·확증적 유의성은 확인하지 않았습니다."]}
