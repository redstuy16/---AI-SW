"""종료된 연구의 출시 후보 내보내기."""
from __future__ import annotations

import argparse
from contextvars import ContextVar
import csv
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
from typing import Any

from .database import from_json, initialize, to_json
from .final_report import _render_report, build_final_conclusion, validate_final_conclusion
from .preflight import environment_status
from .service import StateService
from .storage import UnsafeWorkspacePathError, Workspace, sha256_bytes, sha256_file
from .local_auth import contains_auth_material


class ReleaseExportError(RuntimeError):
    """안전하지 않거나 참조가 해석되지 않은 파일 때문에 내보내기를 중단한다."""


_SECRET_NAME = re.compile(r"(?:^|\.)(?:env|pem|key)$|credentials|secret", re.I)
_SECRET_CONTENT = re.compile(r"(?:[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET)|CROSSREF_MAILTO|HTRSA_\w+_MODEL)\s*=|sk-[A-Za-z0-9_-]{12,}")
_PROTECTED_VALUES = ContextVar("release_protected_values", default=())


def _secret_free(name: str, data: bytes) -> bool:
    if contains_auth_material(data):
        return False
    if _SECRET_NAME.search(Path(name).name):
        return False
    protected = [value for key,value in os.environ.items() if value and (key.endswith("_KEY") or "TOKEN" in key or "SECRET" in key)] + list(_PROTECTED_VALUES.get())
    if any(value.encode("utf-8", errors="strict") in data or json.dumps(value)[1:-1].encode("utf-8") in data for value in protected):
        return False
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return True
    if any(value in text for value in protected): return False
    try:
        decoded = to_json(json.loads(text))
        if contains_auth_material(decoded): return False
        if any(value in decoded for value in protected): return False
    except ValueError: pass
    return _SECRET_CONTENT.search(text) is None


def _spreadsheet_safe_csv(data: bytes) -> bytes:
    """표시용 CSV만 이스케이프하고 원본 바이트는 보존한다."""
    rows = list(csv.reader(io.StringIO(data.decode("utf-8", errors="strict"), newline="")))
    changed = False
    for row in rows:
        for index, value in enumerate(row):
            text = value.lstrip(" \t\r\n")
            suspicious = bool(text and text[0] in "=+-@") or value.startswith(("\t", "\r", "\n"))
            if suspicious and text.startswith(("+", "-")):
                try:
                    suspicious = not Decimal(text).is_finite()
                except InvalidOperation:
                    pass
            if suspicious:
                row[index] = "\x27" + value
                changed = True
    if not changed:
        return data
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    return stream.getvalue().encode("utf-8", errors="strict")


def _git_commit() -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                                timeout=5, check=False)
        value = result.stdout.strip()
        return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _migration_version(state: StateService) -> str | None:
    row = state._db.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    return row["version"] if row and row["version"] else None


def validate_report_snapshot(state: StateService, research_id: str) -> dict[str, Any]:
    if state.workspace is None:
        raise ReleaseExportError("research workspace is required")
    source = state.workspace.path(research_id, "research_output")
    if not source.is_dir():
        raise ReleaseExportError("research_output is missing; run final report export first")
    manifest_path = state.workspace.path(research_id, "research_output/manifests/artifact_manifest.json")
    try:
        manifest = from_json(manifest_path.read_text(encoding="utf-8", errors="strict"))
        if manifest["state_version"] != state.state_version(research_id):
            raise ReleaseExportError("report state version is stale")
        for relative, digest in manifest["files"].items():
            path = state.workspace.path(research_id, f"research_output/{relative}")
            if not path.is_file() or sha256_file(path) != digest:
                raise ReleaseExportError(f"report file hash mismatch: {relative}")
        for record in state._db.execute(
                "SELECT stored_path,sha256 FROM datasets WHERE research_id=?", (research_id,)):
            path = state.workspace.path(research_id, record["stored_path"])
            if not path.is_file() or sha256_file(path) != record["sha256"]:
                raise ReleaseExportError("dataset hash mismatch")
        for record in state._db.execute(
                "SELECT relative_path,sha256 FROM artifacts WHERE research_id=? AND relative_path IS NOT NULL",
                (research_id,)):
            path = state.workspace.path(research_id, record["relative_path"])
            if not path.is_file() or sha256_file(path) != record["sha256"]:
                raise ReleaseExportError("scientific artifact hash mismatch")
        conclusion = build_final_conclusion(state, research_id)
        validate_final_conclusion(state, research_id, conclusion)
        report = state.workspace.path(research_id, "research_output/final_report.md")
        if report.read_text(encoding="utf-8", errors="strict") != _render_report(state, research_id, conclusion):
            raise ReleaseExportError("final report differs from canonical state")
        slice_snapshot = state.research_slice.snapshot(research_id)
        from .qualified_profiles import export_bundle
        from .research_design import current_design, summary
        design = current_design(state, research_id)
        if design:
            saved_design = state.workspace.path(research_id, "research_output/research_design.json")
            if from_json(saved_design.read_text(encoding="utf-8", errors="strict")) != {"current": design, "summary": summary(state, research_id)}:
                raise ReleaseExportError("연구 설계가 현재 검증된 수정본과 다릅니다.")
        qualified = export_bundle(state, research_id)
        if qualified:
            saved_profile = state.workspace.path(research_id, "research_output/qualified_profile.json")
            if from_json(saved_profile.read_text(encoding="utf-8", errors="strict")) != qualified:
                raise ReleaseExportError("qualified profile differs from current verified state")
        if slice_snapshot:
            if any(not item["verification"]["passed"] for item in slice_snapshot.get("cycle5", {}).get("lineages", [])):
                raise ReleaseExportError("declared transformation lineage is not validated")
            exported_slice = state.workspace.path(research_id, "research_output/research_slice.json")
            if from_json(exported_slice.read_text(encoding="utf-8", errors="strict")) != slice_snapshot:
                raise ReleaseExportError("research slice differs from canonical state")
    except (KeyError, TypeError, ValueError, OSError, UnicodeError) as exc:
        if isinstance(exc, ReleaseExportError):
            raise
        raise ReleaseExportError("release source integrity validation failed") from exc
    return manifest


def _copy_research_output(state: StateService, research_id: str, output: Path) -> list[dict[str, Any]]:
    manifest = validate_report_snapshot(state, research_id)
    source = state.workspace.path(research_id, "research_output")
    target = (output / "research_output").resolve()
    output = output.resolve()
    if not target.is_relative_to(output):
        raise ReleaseExportError("release output escaped its root")
    files: list[dict[str, Any]] = []
    allowed = set(manifest["files"]) | {"manifests/artifact_manifest.json", "manifests/model_manifest.json", "manifests/tool_manifest.json", "demo_manifest.json"}
    conversions = {}
    for path in sorted(source.rglob("*.csv")):
        if path.relative_to(source).as_posix() not in allowed:
            continue
        if not path.is_file() or not path.resolve().is_relative_to(source.resolve()):
            raise ReleaseExportError("CSV escaped research workspace")
        raw = path.read_bytes()
        safe = _spreadsheet_safe_csv(raw)
        if safe != raw:
            relative = path.relative_to(source).as_posix()
            archive = "canonical_text/" + relative + ".raw.txt"
            if not _secret_free(archive, raw):
                raise ReleaseExportError("secret-bearing CSV excluded")
            original = output / archive
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(raw)
            files.append({"path": archive, "sha256": sha256_bytes(raw), "size_bytes": len(raw), "purpose": "inert canonical CSV archive"})
            conversions[relative] = {"canonical_sha256": sha256_bytes(raw), "archive_path": archive,
                                     "display_sha256": sha256_bytes(safe), "display_bytes": safe}
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(source.resolve()):
            raise ReleaseExportError("release source escaped research workspace")
        relative = path.relative_to(source).as_posix()
        data = path.read_bytes()
        if relative in conversions:
            data = conversions[relative]["display_bytes"]
        if relative == "manifests/artifact_manifest.json" and conversions:
            portable = from_json(data.decode("utf-8", errors="strict"))
            portable["export_transformations"] = {k: {f: v for f, v in value.items() if f != "display_bytes"} for k, value in conversions.items()}
            for name, conversion in conversions.items():
                portable["files"][name] = conversion["display_sha256"]
            data = (to_json(portable) + "\n").encode("utf-8", errors="strict")
        if relative == "demo_manifest.json":
            try:
                demo = from_json(data.decode("utf-8", errors="strict"))
                demo["fixture"] = "demo_scenarios/" + Path(demo["fixture"]).name
                demo["final_report"] = "research_output/final_report.md"
                demo["manifest_path"] = "research_output/demo_manifest.json"
                data = (to_json(demo) + "\n").encode("utf-8", errors="strict")
            except (KeyError, TypeError, ValueError, UnicodeError) as exc:
                raise ReleaseExportError("invalid demo manifest") from exc
        if not _secret_free(relative, data):
            raise ReleaseExportError(f"secret-bearing artifact excluded: {relative}")
        if relative not in allowed:
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        files.append({"path": (Path("research_output") / relative).as_posix(),
                      "sha256": sha256_bytes(data), "size_bytes": len(data)})
    if not any(item["path"] == "research_output/final_report.md" for item in files):
        raise ReleaseExportError("final_report.md is missing")
    return files


def _write_text(root: Path, relative: str, content: str) -> dict[str, Any]:
    data = content.encode("utf-8", errors="strict")
    if not _secret_free(relative, data):
        raise ReleaseExportError(f"generated release file contains a secret: {relative}")
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": relative.replace("\\", "/"), "sha256": sha256_bytes(data), "size_bytes": len(data)}


def _copy_skill_artifacts(state: StateService, research_id: str, output: Path) -> list[dict[str, Any]]:
    """명시적으로 활성화하고 검증한 Skill의 재현 산출물만 포함한다."""
    selected = state._db.execute(
        "SELECT a.artifact_id,a.relative_path,a.sha256,a.size_bytes FROM artifacts a "
        "WHERE a.research_id=? AND a.status='VERIFIED' AND a.relative_path IS NOT NULL "
        "AND a.contract_id IN (SELECT contract_id FROM artifacts WHERE research_id=? "
        "AND artifact_type='SKILL_PLAN' AND status='VERIFIED') "
        "AND a.artifact_type IN ('SKILL_PLAN','SKILL_RESULT','FIGURE') ORDER BY a.artifact_id",
        (research_id, research_id)).fetchall()
    files = []
    for row in selected:
        source = state.workspace.path(research_id, row["relative_path"])
        data = source.read_bytes()
        if sha256_bytes(data) != row["sha256"] or len(data) != row["size_bytes"]:
            raise ReleaseExportError("Skill artifact hash mismatch")
        relative = f"analysis_artifacts/{row['artifact_id']}{source.suffix.lower()}"
        if not _secret_free(relative, data):
            raise ReleaseExportError("secret-bearing Skill artifact excluded")
        target = (output / relative).resolve()
        if not target.is_relative_to(output.resolve()):
            raise ReleaseExportError("Skill artifact escaped release root")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files.append({"path": relative, "sha256": sha256_bytes(data), "size_bytes": len(data)})
    return files


def _copy_repair_artifacts(state: StateService, research_id: str, output: Path) -> list[dict[str, Any]]:
    config = state.runtime_step(research_id, "verification_repair_config")
    if config is None or not config["output"].get("enabled"):
        return []
    from .verification_repair import REPAIR_POLICY_VERSION
    if config["output"].get("version") != REPAIR_POLICY_VERSION:
        raise ReleaseExportError("repair policy version mismatch")
    files = []
    for row in state._db.execute(
            "SELECT artifact_id,relative_path,sha256,size_bytes FROM artifacts "
            "WHERE research_id=? AND artifact_type LIKE 'F3P_%' ORDER BY artifact_id", (research_id,)):
        record = state.file_artifact(row["artifact_id"], research_id)
        data = state.workspace.path(research_id, record["relative_path"]).read_bytes()
        if re.search(rb'[A-Za-z]:[\\/]|"\s*/', data):
            raise ReleaseExportError("machine-local path in repair audit")
        files.append(_write_text(output, f"repair_artifacts/{row['artifact_id']}.json",
                                 data.decode("utf-8", errors="strict")))
    steps = [{"step_key": row["step_key"], "status": row["status"],
              "contract_id": row["contract_id"], "output": from_json(row["output_json"]) if row["output_json"] else None}
             for row in state._db.execute(
                 "SELECT * FROM runtime_steps WHERE research_id=? AND "
                 "(step_key LIKE 'repair_%' OR step_key LIKE 'worker_plan:%' OR "
                 "step_key='verification_repair_config') ORDER BY rowid", (research_id,))]
    attempts = [{"mutation_id": row["mutation_id"], "contract_id": row["contract_id"],
                 "status": row["status"], "payload": from_json(row["payload_json"]),
                 "verification": from_json(row["verification_json"]) if row["verification_json"] else None}
                for row in state._db.execute(
                    "SELECT * FROM staged_mutations WHERE research_id=? ORDER BY rowid", (research_id,))]
    original_refs = {ref for step in steps if step["step_key"].startswith("repair_failure:")
                     for ref in step["output"].get("relevant_artifact_refs", [])}
    for artifact_id in sorted(original_refs):
        record = state._db.execute(
            "SELECT * FROM artifacts WHERE research_id=? AND artifact_id=? AND relative_path IS NOT NULL",
            (research_id, artifact_id)).fetchone()
        if record is None:
            raise ReleaseExportError("missing original repair artifact")
        source = state.workspace.path(research_id, record["relative_path"])
        data = source.read_bytes()
        if sha256_bytes(data) != record["sha256"] or len(data) != record["size_bytes"]:
            raise ReleaseExportError("original repair artifact hash mismatch")
        relative = f"repair_artifacts/original/{artifact_id}{source.suffix.lower()}"
        if not _secret_free(relative, data):
            raise ReleaseExportError("secret-bearing original repair artifact")
        target = (output / relative).resolve()
        if not target.is_relative_to(output.resolve()):
            raise ReleaseExportError("original repair artifact escaped release root")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files.append({"path": relative, "sha256": sha256_bytes(data), "size_bytes": len(data)})
    trace_text = to_json({"config": config["output"], "steps": steps, "attempts": attempts,
                         "procedure_approved_for_reuse": False}) + "\n"
    if re.search(r'[A-Za-z]:[\\/]|"\s*/', trace_text):
        raise ReleaseExportError("machine-local path in repair trace")
    files.append(_write_text(output, "repair_artifacts/repair_trace.json",
                             trace_text))
    return files


def export_release(state: StateService, research_id: str, output: str | Path, *, protected_values=None) -> dict[str, Any]:
    if protected_values is None and state.workspace is not None:
        from .control_plane import Credentials, ControlError
        refs = []
        if state._db.execute("SELECT 1 FROM sqlite_master WHERE name='control_configs'").fetchone():
            refs = [from_json(r[0]).get("credential_env_name") for r in state._db.execute("SELECT payload FROM control_configs WHERE kind='connection'")]
        try:
            protected_values = Credentials(Path(__file__).resolve().parents[2], state.workspace.root).active_secrets(refs)
        except (ControlError, OSError):
            raise ReleaseExportError("자격 증명 경계를 확인하지 못해 내보내기를 차단함") from None
    protected_values = protected_values or ()
    token = _PROTECTED_VALUES.set(tuple(protected_values))
    try:
        return _export_release(state, research_id, output)
    finally:
        _PROTECTED_VALUES.reset(token)


def _export_release(state: StateService, research_id: str, output: str | Path) -> dict[str, Any]:
    """자격 증명을 제외하고 연구 범위 안에서 종료된 연구를 내보낸다."""
    run = state._one("SELECT * FROM research_runs WHERE research_id=?", (research_id,))
    if run["run_status"] == "ACTIVE":
        raise ReleaseExportError("active research cannot be released")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    files = _copy_research_output(state, research_id, output)
    files.extend(_copy_skill_artifacts(state, research_id, output))
    files.extend(_copy_repair_artifacts(state, research_id, output))
    if state._db.execute("SELECT 1 FROM sqlite_master WHERE name='control_audit'").fetchone():
        searches = [dict(row) for row in state._db.execute("SELECT kind,payload,created_at FROM control_audit WHERE research_id=? AND kind LIKE 'SEARCH_%' ORDER BY seq", (research_id,))]
        if searches:
            files.append(_write_text(output, 'search_provenance.json', to_json(searches) + '\n'))
    if state._db.execute("SELECT 1 FROM sqlite_master WHERE name='control_runs'").fetchone():
        controlled = state._db.execute("SELECT snapshot FROM control_runs WHERE research_id=?", (research_id,)).fetchone()
        if controlled:
            snapshot = from_json(controlled[0])
            if snapshot.get("settings_version", 1) >= 2:
                from .report_pdf import render_pdf
                rendered = render_pdf(state, research_id, protected_values=_PROTECTED_VALUES.get())
                (output / "report.pdf").write_bytes(rendered["data"])
                files.append({"path":"report.pdf", "sha256":rendered["sha256"], "size_bytes":len(rendered["data"])})
            configuration = {k:snapshot.get(k) for k in ("models", "profile_revisions", "routing", "routing_revision", "adapter_versions", "fallback_policy")}
            traces = []
            for event in state._db.execute("SELECT kind,payload,created_at FROM control_audit WHERE research_id=? AND kind LIKE 'NORMALIZED_%' ORDER BY seq", (research_id,)):
                payload = from_json(event["payload"])
                payload.pop("reservation_id", None)
                parameters = payload.get("requested_parameters", {})
                parameters.pop("budget_reservation_id", None)
                traces.append({"kind":event["kind"],"created_at":event["created_at"],"payload":payload})
            files.append(_write_text(output, "model_configuration.json", to_json({"configuration":configuration,"traces":traces}) + "\n"))
            if snapshot.get("performance_profile"):
                from .report_ux import friendly_report, markdown_report
                view = friendly_report(state, research_id)
                files.append(_write_text(output, "friendly_report.md", markdown_report(view)))
                files.append(_write_text(output, "friendly_report.json", to_json(view) + "\n"))
                for image in view["images"]:
                    artifact = state.file_artifact(image["artifact_id"], research_id)
                    data = state.workspace.path(research_id, artifact["relative_path"]).read_bytes()
                    relative = image["release_path"]
                    if not _secret_free(relative, data):
                        raise ReleaseExportError("그림의 비밀 값 때문에 내보내기 차단")
                    target = output / relative
                    if not target.resolve().is_relative_to(output.resolve()):
                        raise ReleaseExportError("그림 경로가 내보내기 범위를 벗어남")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    files.append({"path": relative, "sha256": sha256_bytes(data), "size_bytes": len(data)})
                history = [dict(row) for row in state._db.execute("SELECT kind,payload,created_at FROM control_audit WHERE research_id=? AND kind IN ('RESEARCH_SETTINGS_REQUESTED','RESEARCH_SETTINGS_APPLIED','ADAPTIVE_OPTIONAL_REDUCED','ADAPTIVE_MODEL_SELECTED','ANALYSIS_PLAN_REVISION_CREATED') ORDER BY seq", (research_id,))]
                files.append(_write_text(output, "research_config_history.json", to_json(history) + "\n"))
                owner = state.runtime_step(research_id, "owner_analysis_plan_request")
                if owner:
                    files.append(_write_text(output, "owner_analysis_plan_request.json", to_json(owner["output"]) + "\n"))
    env = environment_status()
    models = sorted({(row["provider"], row["model"]) for row in state._db.execute(
        "SELECT provider,model FROM agent_runs WHERE research_id=? AND provider IS NOT NULL", (research_id,))})
    providers = sorted({row["provider"] for row in state._db.execute(
        "SELECT provider FROM sources WHERE research_id=?", (research_id,))})
    tool_versions: dict[str, str] = {}
    for row in state._db.execute("SELECT tool_name,result_json FROM tool_calls WHERE agent_run_id IN "
                                "(SELECT agent_run_id FROM agent_runs WHERE research_id=?)", (research_id,)):
        try:
            provenance = from_json(row["result_json"]).get("provenance", {})
            if provenance.get("tool_version"):
                tool_versions[row["tool_name"]] = provenance["tool_version"]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    datasets = [dict(row) for row in state._db.execute(
        "SELECT dataset_id,sha256,size_bytes,status FROM datasets WHERE research_id=?", (research_id,))]
    artifacts = [dict(row) for row in state._db.execute(
        "SELECT artifact_id,artifact_type,sha256,size_bytes,status FROM artifacts WHERE research_id=?", (research_id,))]
    manifest = {"release_version": "day4b-rc1", "research_id": research_id,
                "git_commit": _git_commit(), "schema_version": "006",
                "migration_version": _migration_version(state), "python_version": platform.python_version(),
                "tool_versions": tool_versions, "model_ids": [{"provider": provider, "model": model}
                                                                 for provider, model in models],
                "provider_ids": providers, "dataset_hashes": datasets, "artifact_hashes": artifacts,
                "test_status": env.get("gates", {}), "environment_validation": env,
                "created_at": datetime.now(timezone.utc).isoformat(), "files": files}
    slice_config = state.research_slice.config(research_id)
    if slice_config.claim_evidence_provenance or slice_config.verifier_dependency_catalog:
        manifest["research_slice"] = {"extension_schema_version": "1", "policy": slice_config.model_dump(mode="json")}
        if state.cycle5.enabled(research_id):
            manifest["research_slice"]["cycle5"] = {"enabled": True, "version": "1", "snapshot": "research_output/research_slice.json", "live_efficacy": "NOT_VALIDATED"}
    files.append(_write_text(output, "environment_status.json", to_json(env) + "\n"))
    readme = """# H-TRSA 출시 후보\n\n종료된 연구 하나를 자격 증명 없이 내보낸 결과다. research_output/은 정본의 조회 결과와 검증된 산출물이다.\n\n표시용 CSV는 필요한 경우 수식 시작 문자를 이스케이프한다. 원본 바이트는 canonical_text/*.csv.raw.txt에 보존하며 스프레드시트로 가져오지 않는다. 선언 파일은 원본·표시 해시를 별도로 기록한다.\n\n실행 모드와 환경 검증은 environment_status.json과 출시 선언 파일에 있다.\n"""
    reproduce = f"""# 재현\n\n저장소 루트에서 같은 Python 환경으로 실행한다.\n\n```powershell\npython -m htrsa.preflight validate-core\npython -m htrsa.demo --scenario adaptive_research\n```\n\n데모는 고정 로컬 자료와 모의 문헌 제공사를 사용한다. 실제 제공사 검증은 아니다. 읽기 전용 대시보드에서 ?research_id={research_id}로 조회한다.\n"""
    files.append(_write_text(output, "README.md", readme))
    files.append(_write_text(output, "REPRODUCE.md", reproduce))
    # 선언 파일 자체의 해시는 별도로 반환한다.
    
    manifest["files"] = files
    final_manifest = to_json(manifest) + "\n"
    manifest_path = output / "manifests" / "release_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(final_manifest.encode("utf-8", errors="strict"))
    manifest_hash = sha256_file(manifest_path)
    return {"research_id": research_id, "output": str(output),
            "manifest": str(manifest_path), "manifest_sha256": manifest_hash,
            "files": files, "release_ready": bool(env.get("release_ready")),
            "demo_ready": bool(env.get("demo_ready"))}


def export_release_from_paths(database: str | Path, workspace: str | Path,
                              research_id: str, output: str | Path) -> dict[str, Any]:
    db = initialize(database)
    try:
        return export_release(StateService(db, Workspace(workspace)), research_id, output)
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a terminal H-TRSA run")
    parser.add_argument("research_id")
    parser.add_argument("--database", type=Path, default=Path("state.sqlite"))
    parser.add_argument("--workspace", type=Path, default=Path("workspace"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(to_json(export_release_from_paths(args.database, args.workspace, args.research_id, args.output)))


if __name__ == "__main__":
    main()
