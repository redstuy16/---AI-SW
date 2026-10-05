"""내보낸 고정 공개 분석을 외부 API 없이 다시 계산한다."""
import argparse
import csv
import json
from pathlib import Path

from .climate_profile import (parse_source, parse_csv_source, transform_rows, normalized_csv, claim_text,
                             selected_rows, ClimateComparisonProfile, RESIDUAL_LIMIT, CHECKER_VERSION)
from .period_comparison import compare_periods, compare_keyed
from .storage import sha256_file


def _capture(root, document, capture):
    relative = "qualified_sources/" + capture["source_relative"].rsplit("/", 1)[-1]
    original = root / relative
    path = original.resolve()
    if any(p.is_symlink() or p.is_junction() for p in [original, *original.parents]) or not path.is_relative_to(root) or not path.is_file():
        raise ValueError("REPLAY_CAPTURE_UNSAFE_OR_MISSING")
    if path.stat().st_size > 1_000_000 or sha256_file(path) != capture["source_sha256"] or document["files"].get(relative) != capture["source_sha256"]:
        raise ValueError("REPLAY_CAPTURE_HASH_MISMATCH")
    return path.read_text(encoding="utf-8", errors="strict")


def replay(path):
    original = Path(path).absolute()
    if any(p.is_symlink() or p.is_junction() for p in [original, *original.parents]):
        raise ValueError("REPLAY_SOURCE_UNSAFE")
    path = original.resolve()
    artifact_manifest = path.parent / "manifests/artifact_manifest.json"
    document = json.loads(artifact_manifest.read_text(encoding="utf-8", errors="strict"))
    if document["files"].get(path.name) != sha256_file(path):
        raise ValueError("REPLAY_MANIFEST_HASH_MISMATCH")
    manifest = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    original_source = path.parent / manifest["source"]
    source = original_source.resolve()
    if any(p.is_symlink() or p.is_junction() for p in [original_source, *original_source.parents]) or not source.is_relative_to(path.parent) or not source.is_file():
        raise ValueError("REPLAY_SOURCE_UNSAFE")
    binding = manifest["binding"]
    if sha256_file(source) != binding["source_sha256"] or document["files"].get(manifest["source"]) != binding["source_sha256"]:
        raise ValueError("REPLAY_SOURCE_HASH_MISMATCH")
    plan = binding["plan"]
    if manifest.get("research_design"):
        from .research_design import semantic_design, resolve_design
        from .qualified_profiles import fingerprint
        ref = manifest["research_design"]
        if ref["path"] != "research_design.json":
            raise ValueError("REPLAY_DESIGN_PATH_INVALID")
        design_file = path.parent / ref["path"]
        if design_file.is_symlink() or design_file.is_junction() or document["files"].get(ref["path"]) != sha256_file(design_file):
            raise ValueError("REPLAY_DESIGN_HASH_MISMATCH")
        design = json.loads(design_file.read_text(encoding="utf-8", errors="strict"))["current"]
        resolved = resolve_design(design["original_question"], design["design"])
        if ref["hash"] != design["hash"] or design["hash"] != fingerprint(semantic_design(design["design"])) or ref["revision"] != design["revision"] or resolved["issues"]:
            raise ValueError("REPLAY_DESIGN_SCOPE_MISMATCH")
        periods = resolved["profile"].get("periods")
        if periods and periods != plan["periods"]:
            raise ValueError("REPLAY_DESIGN_SCOPE_MISMATCH")
    if manifest.get("authority") and manifest["authority"]["plan"] != plan:
        raise ValueError("REPLAY_AUTHORITY_MISMATCH")
    candidate = ClimateComparisonProfile().candidate(plan["question"])
    if not candidate or candidate["status"] != "SUPPORTED" or candidate["periods"] != plan["periods"] or candidate["transform"] != plan["transform"]:
        raise ValueError("REPLAY_QUESTION_SCOPE_MISMATCH")
    if plan.get("checker_version", CHECKER_VERSION) != CHECKER_VERSION:
        raise ValueError("REPLAY_CHECKER_STALE")
    if source.stat().st_size > 1_000_000:
        raise ValueError("REPLAY_SOURCE_LIMIT")
    primary = parse_source(source.read_text(encoding="utf-8", errors="strict"), plan["semantics"])
    secondary = binding.get("secondary")
    required = plan.get("source_policy", {}).get("secondary_required", False)
    representation_status = "NOT_PRESENT_OPTIONAL"
    if secondary:
        relative = "qualified_sources/" + secondary["source_relative"].rsplit("/", 1)[-1]
        original_secondary = path.parent / relative
        alternate = original_secondary.resolve()
        if any(p.is_symlink() or p.is_junction() for p in [original_secondary, *original_secondary.parents]) or not alternate.is_relative_to(path.parent) or not alternate.is_file():
            raise ValueError("REPLAY_SECONDARY_UNSAFE_OR_MISSING")
        if alternate.stat().st_size > 1_000_000 or sha256_file(alternate) != secondary["source_sha256"] or document["files"].get(relative) != secondary["source_sha256"]:
            raise ValueError("REPLAY_SECONDARY_HASH_MISMATCH")
        if binding.get("capture_group") != secondary.get("capture_group"):
            raise ValueError("REPLAY_CAPTURE_INCOMPARABLE")
        keys = [r["year"] for r in selected_rows(primary, plan["periods"])]
        if plan["transform"].get("baseline_period"):
            keys += [r["year"] for r in selected_rows(primary, [plan["transform"]["baseline_period"]]) if r["year"] not in keys]
        alternate_rows = parse_csv_source(alternate.read_text(encoding="utf-8", errors="strict"), secondary["semantics"])
        if compare_keyed(primary, alternate_rows, keys):
            raise ValueError("REPLAY_SOURCE_CONFLICT")
        representation_status = "MATCHED_SELECTED_KEYS"
    elif required:
        raise ValueError("REPLAY_REQUIRED_REPRESENTATION_MISSING")
    current = manifest.get("current_source")
    if current:
        from .climate_profile import dependency_hash
        latest = parse_source(_capture(path.parent, document, current), current["semantics"])
        if current["semantics"] != plan["semantics"] or dependency_hash(latest, plan) != binding["used_rows_hash"]:
            raise ValueError("REPLAY_CURRENT_DEPENDENCY_MISMATCH")
        if current.get("secondary"):
            if current.get("capture_group") != current["secondary"].get("capture_group"):
                raise ValueError("REPLAY_CURRENT_CAPTURE_INCOMPARABLE")
            alternate_rows = parse_csv_source(_capture(path.parent, document, current["secondary"]), current["secondary"]["semantics"])
            keys = [r["year"] for r in selected_rows(latest, plan["periods"])]
            if plan["transform"].get("baseline_period"):
                keys += [r["year"] for r in selected_rows(latest, [plan["transform"]["baseline_period"]]) if r["year"] not in keys]
            if compare_keyed(latest, alternate_rows, keys):
                raise ValueError("REPLAY_CURRENT_REPRESENTATION_CONFLICT")
        elif required:
            raise ValueError("REPLAY_CURRENT_REQUIRED_REPRESENTATION_MISSING")
    rows = transform_rows(primary, plan["transform"])
    result = compare_periods(list(csv.DictReader(normalized_csv(rows).splitlines())), {"year":"year","value":"value"}, plan["periods"])
    if claim_text(plan, result) != manifest["expected_claim"]:
        raise ValueError("REPLAY_CLAIM_MISMATCH")
    return {"result":result,"status":"LOCAL_RECALCULATION_PASS","paid_calls":0,
            "manifest_sha256":sha256_file(path),
            "representation_status":representation_status, "limitation_shared_root":RESIDUAL_LIMIT,
            "limitation":"내보낸 해시에 대한 계산 재현입니다. 독립적인 진위 인증이나 과학적 참의 보장이 아닙니다."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.manifest), ensure_ascii=False))


if __name__ == "__main__":
    main()
