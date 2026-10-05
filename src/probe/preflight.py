"""환경 검사와 명시적으로 요청한 출시 검증."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from .sandbox import PythonSandboxTool, clean_environment
from .storage_errors import storage_error_code


ROOT = Path(__file__).resolve().parents[2]
VALIDATION_DIR = ROOT / "build" / "validation"
SEARCH_STATUSES = {"AVAILABLE", "RATE_LIMITED", "UNCONFIGURED", "FAILED", "VALIDATED"}


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    for encoding in ("utf-8", locale.getpreferredencoding(False)):
        try:
            return value.decode(encoding, errors="strict")
        except UnicodeError:
            pass
    return value.decode("utf-8", errors="replace")


def _run(command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True,
                                timeout=timeout, check=False, cwd=ROOT,
                                **({"env": clean_environment()} if command and Path(command[0]).stem.casefold() == "docker" else {}))
        return subprocess.CompletedProcess(getattr(result, "args", command), result.returncode,
                                           _decode_output(getattr(result, "stdout", None)),
                                           _decode_output(getattr(result, "stderr", None)))
    except OSError as error:
        return subprocess.CompletedProcess(command, 1, "", storage_error_code(error))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 1, "", "VALIDATION_TIMEOUT")


def _docker_identity() -> str | None:
    if shutil.which("docker") is None or _run(["docker", "info", "--format", "{{.ServerVersion}}"],
                                               timeout=8).returncode != 0:
        return None
    image = _run(["docker", "image", "inspect", PythonSandboxTool.image,
                  "--format", "{{.Id}}"], timeout=8)
    return image.stdout.strip() if image.returncode == 0 else None


def docker_preflight() -> dict:
    cli = shutil.which("docker") is not None
    daemon = cli and _run(["docker", "info", "--format", "{{.ServerVersion}}"],
                          timeout=8).returncode == 0
    image_id = _docker_identity() if daemon else None
    probe = False
    if image_id:
        build_dir = ROOT / "build"
        build_dir.mkdir(parents=True, exist_ok=True)
        if not build_dir.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("preflight temporary directory escaped workspace")
        with tempfile.TemporaryDirectory(dir=build_dir, prefix="docker-probe-") as directory:
            folder = Path(directory)
            script = folder / "ART-preflight-analysis.py"
            input_file = folder / "probe.txt"
            output = folder / "output"
            output.mkdir()
            code = ("import json,socket\n"
                    "with open('/work/input/probe.txt',encoding='utf-8') as f: mounted=f.read()=='mounted'\n"
                    "s=socket.socket(); s.settimeout(1)\n"
                    "try: s.connect(('1.1.1.1',80)); blocked=False\n"
                    "except OSError: blocked=True\n"
                    "with open('/work/output/result.json','w',encoding='utf-8') as f: json.dump({'mounted':mounted,'network_blocked':blocked},f)\n")
            code.encode("utf-8", errors="strict")
            script.write_text(code, encoding="utf-8")
            value = "mounted"
            value.encode("utf-8", errors="strict")
            input_file.write_text(value, encoding="utf-8")
            command = PythonSandboxTool.build_command(
                script, output, ["--mount", f"type=bind,src={input_file},dst=/work/input/probe.txt,readonly"])
            flags = {"--network=none", "--memory=512m", "--cpus=1", "--read-only"}
            from .sandbox_io import execute_container, SandboxFailure
            try:
                execute_container(command, output, 20)
                completed = subprocess.CompletedProcess(command, 0)
            except (SandboxFailure, OSError, subprocess.SubprocessError):
                completed = subprocess.CompletedProcess(command, 1)
            result_file = output / "result.json"
            if completed.returncode == 0 and result_file.is_file():
                try:
                    document = json.loads(result_file.read_text(encoding="utf-8", errors="strict"))
                    probe = (document == {"mounted": True, "network_blocked": True} and
                             flags <= set(command))
                except (OSError, ValueError, UnicodeError):
                    probe = False
    return {"cli_present": cli, "daemon_reachable": bool(daemon), "image_present": bool(image_id),
            "image": PythonSandboxTool.image, "image_id": image_id,
            "isolation_probe_passed": probe, "available": bool(probe)}


def api_preflight() -> dict:
    key = bool(os.environ.get("OPENAI_API_KEY"))
    manager = bool(os.environ.get("PROBE_MANAGER_MODEL"))
    sdk = importlib.util.find_spec("agents") is not None
    from .providers.openai_agents import OpenAIAgentsProvider

    structured = callable(getattr(OpenAIAgentsProvider, "run_structured", None))
    return {"api_key_present": key, "manager_model_configured": manager,
            "provider_importable": sdk, "structured_output_available": structured,
            "configured": key and manager and sdk and structured}


def search_status_from_error(error: BaseException | None) -> str:
    """제공사 실패를 대시보드에 안전한 준비 상태로 변환한다."""
    if error is None:
        return "AVAILABLE"
    if getattr(error, "status", None) == "RATE_LIMITED" or getattr(error, "status_code", None) == 429:
        return "RATE_LIMITED"
    return "FAILED"


def search_preflight() -> dict:
    """네트워크 요청 없이 실제 AI 검색 증빙과 현재 소스 표식을 대조한다."""
    marker = _validation_marker("search")
    source = _source_fingerprint()
    if marker and marker.get("source_fingerprint") == source:
        status = marker.get("status")
        if status in SEARCH_STATUSES:
            validated = status == "VALIDATED" and marker.get("provider") == "openai.web_search" and _native_search_evidence(marker.get("evidence"))
            if status == "VALIDATED" and not validated:
                return {"status": "FAILED", "validated": False, "provider": marker.get("provider"),
                        "error_code": "NATIVE_SEARCH_EVIDENCE_REQUIRED"}
            return {"status": status, "validated": validated,
                    "provider": marker.get("provider"), "error_code": marker.get("error_code")}
    return {"status": "UNCONFIGURED", "validated": False, "provider": None, "error_code": None}


def _native_search_evidence(evidence) -> bool:
    """실행 기록의 해시·응답·실사용량·정산이 같은 native 검색을 가리켜야 한다."""
    if not isinstance(evidence, dict) or evidence.get("execution") != "LIVE_USER_SESSION":
        return False
    runs = evidence.get("runs")
    if not isinstance(runs, list) or not 1 <= len(runs) <= 20:
        return False
    allowed = (ROOT / "build" / "science-live").resolve()
    seen = set()
    try:
        for run in runs:
            if not isinstance(run, dict) or not isinstance(run.get("research_id"), str) or not run["research_id"] or run["research_id"] in seen:
                return False
            seen.add(run["research_id"])
            relative = run.get("artifact_path")
            digest = run.get("artifact_sha256")
            if not isinstance(relative, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                return False
            path = ROOT / relative
            if Path(relative).is_absolute() or not path.resolve().is_relative_to(allowed) or any(
                    p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in (path, *path.parents)):
                return False
            if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
                return False
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                return False
            raw = json.loads(data.decode("utf-8", errors="strict"))
            if not isinstance(raw, dict) or raw.get("execution") != "LIVE_USER_SESSION" or raw.get("research_id") != run["research_id"] or raw.get("requirements_met") is not True:
                return False
            searches, providers, ledger = (raw.get(key) for key in ("search_measurements", "provider_measurements", "ledger"))
            if not all(isinstance(items, list) and all(isinstance(item, dict) for item in items) for items in (searches, providers, ledger)):
                return False
            matched = False
            for search in searches:
                usage, response_id, actions = search.get("usage"), search.get("response_id"), search.get("hosted_tool_actions")
                if search.get("provider") != "openai.web_search" or search.get("status") != "COMPLETED" or not isinstance(response_id, str) or not response_id or not isinstance(usage, dict):
                    continue
                if not all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("input_tokens", "output_tokens", "web_search_calls")):
                    continue
                calls = usage["web_search_calls"]
                if not 1 <= calls <= 3 or type(actions) is not int or not calls <= actions <= 3 or type(search.get("billed_search_calls")) is not int or search["billed_search_calls"] != calls:
                    continue
                for provider in providers:
                    if provider.get("response_id") != response_id or provider.get("usage") != usage or not isinstance(provider.get("resolved_model_id"), str) or not provider["resolved_model_id"] or not isinstance(provider.get("reservation_id"), str) or not provider["reservation_id"]:
                        continue
                    if any(item.get("id") == provider["reservation_id"] and item.get("status") == "SETTLED" and type(item.get("settled")) is int and item["settled"] > 0 for item in ledger):
                        matched = True
            if not matched:
                return False
    except (OSError, ValueError, UnicodeError, TypeError):
        return False
    return True


def record_search_validation(*, status: str, provider: str | None = None,
                             error_code: str | None = None, evidence: dict | None = None) -> dict:
    if status not in SEARCH_STATUSES:
        raise ValueError("invalid search validation status")
    if status == "VALIDATED" and (provider != "openai.web_search" or not _native_search_evidence(evidence)):
        raise ValueError("NATIVE_SEARCH_EVIDENCE_REQUIRED")
    marker = {"status": status, "validated": status == "VALIDATED",
              "provider": provider, "error_code": error_code,
              "source_fingerprint": _source_fingerprint(), "evidence": evidence}
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(marker, ensure_ascii=False, sort_keys=True)
    serialized.encode("utf-8", errors="strict")
    (VALIDATION_DIR / "search.json").write_text(serialized, encoding="utf-8")
    return marker


def record_demo_validation(scenario: str, passed: bool, details: dict | None = None) -> dict:
    if scenario not in {"a", "b"}:
        raise ValueError("demo scenario must be a or b")
    marker = {"passed": bool(passed), "scenario": scenario,
              "details": details or {}, "source_fingerprint": _source_fingerprint()}
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(marker, ensure_ascii=False, sort_keys=True)
    serialized.encode("utf-8", errors="strict")
    (VALIDATION_DIR / f"demo_{scenario}.json").write_text(serialized, encoding="utf-8")
    return marker


def record_qa_validation(name: str, passed: bool, details: dict | None = None) -> dict:
    if name not in {"stress", "artifact"}:
        raise ValueError("QA validation name must be stress or artifact")
    marker = {"passed": bool(passed), "details": details or {},
              "source_fingerprint": _source_fingerprint()}
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(marker, ensure_ascii=False, sort_keys=True)
    serialized.encode("utf-8", errors="strict")
    (VALIDATION_DIR / f"{name}.json").write_text(serialized, encoding="utf-8")
    return marker


def _source_fingerprint() -> str:
    paths = sorted((ROOT / "src" / "probe").rglob("*.py"))
    paths += sorted((ROOT / "src" / "probe" / "workbench_static").glob("*.*"))
    paths += sorted((ROOT / "tests").glob("test_*.py"))
    paths += sorted((ROOT / "db" / "migrations").glob("*.sql"))
    paths += [ROOT / "pyproject.toml", ROOT / "docker" / "Dockerfile.sandbox"]
    paths += [ROOT / "src" / "probe" / "product_catalog.json"]
    paths += [ROOT / "src" / "probe" / "workbench_static" / "pdfjs" / "manifest.json"]
    if (ROOT / "Probe.wsf").is_file():
        paths += [ROOT / "Probe.wsf"]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8", errors="strict"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _validation_marker(name: str) -> dict | None:
    path = VALIDATION_DIR / f"{name}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, ValueError, UnicodeError):
        return None


def native_environment_id():
    import getpass
    import platform
    value = platform.node() + ":" + getpass.getuser() + ":" + os.name
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def productization_status(source=None):
    source = source or _source_fingerprint()
    result = {}
    for name, required in (("windows_key", ("save", "read", "rotate", "restart", "delete", "child_environment_clean")),
                           ("default_browser", ("open", "exchange", "owner_list"))):
        record = _validation_marker(name) or {}
        passed = (os.name == "nt" and record.get("passed") is True and record.get("execution") == "NATIVE_WINDOWS"
                  and record.get("source_fingerprint") == source and record.get("environment_id") == native_environment_id()
                  and all(record.get("checks", {}).get(key) is True for key in required))
        result[name] = {"status": "VALIDATED" if passed else "NOT_VALIDATED", "passed": passed,
                        "checked_at": record.get("checked_at"), "error_code": record.get("error_code")}
    return result


def environment_status() -> dict:
    from .runtime_environment import interpreter_preflight
    interpreter = interpreter_preflight()
    docker = docker_preflight()
    provider = api_preflight()
    search = search_preflight()
    source = _source_fingerprint()
    core = _validation_marker("core")
    docker_mark = _validation_marker("docker")
    api_mark = _validation_marker("api")
    demo_a = _validation_marker("demo_a")
    demo_b = _validation_marker("demo_b")
    stress = _validation_marker("stress")
    artifact = _validation_marker("artifact")
    core_ok = bool(core and core.get("passed") and core.get("source_fingerprint") == source)
    docker_ok = bool(docker_mark and docker_mark.get("passed") and
                     docker_mark.get("source_fingerprint") == source and
                     docker_mark.get("image_id") == docker["image_id"] and docker["available"])
    api_ok = bool(api_mark and api_mark.get("passed") and
                  api_mark.get("source_fingerprint") == source and provider["configured"] and
                  api_mark.get("manager_model") == os.environ.get("PROBE_MANAGER_MODEL"))
    demo_a_ok = bool(demo_a and demo_a.get("passed") and demo_a.get("source_fingerprint") == source)
    demo_b_ok = bool(demo_b and demo_b.get("passed") and demo_b.get("source_fingerprint") == source)
    stress_ok = bool(stress and stress.get("passed") and stress.get("source_fingerprint") == source)
    artifact_ok = bool(artifact and artifact.get("passed") and artifact.get("source_fingerprint") == source)
    demo_ready = core_ok and stress_ok and demo_a_ok and demo_b_ok and artifact_ok
    productization = productization_status(source)
    release_ready = demo_ready and docker_ok and api_ok and search["validated"]
    return {"interpreter": interpreter, "docker": {**docker, "isolation_validated": docker_ok,
                       "status": "VALIDATED" if docker_ok else "NOT_VALIDATED"},
            "provider": {**provider, "live_smoke_validated": api_ok,
                         "status": "VALIDATED" if api_ok else "NOT_VALIDATED"},
            "search": search,
            "sandbox": {"fail_closed": True},
            "gates": {"CORE_TESTS_PASS": core_ok, "STRESS_TESTS_PASS": stress_ok,
                      "ARTIFACT_VALIDATION_PASS": artifact_ok, "DOCKER_ISOLATION_PASS": docker_ok,
                      "LIVE_API_SMOKE_PASS": api_ok, "LIVE_SEARCH_PASS": search["validated"],
                      "DEMO_A_PASS": demo_a_ok, "DEMO_B_PASS": demo_b_ok},
            "demo_ready": demo_ready,
            "release_ready": release_ready,
            "productization": productization,
            "product_release_ready": release_ready and all(v["passed"] for v in productization.values())}


def build_sandbox() -> dict:
    if shutil.which("docker") is None or _run(["docker", "info", "--format", "{{.ServerVersion}}"],
                                               timeout=8).returncode != 0:
        return {"built": False, "reason": "DOCKER_UNAVAILABLE"}
    result = _run(["docker", "build", "--pull=false", "-f", str(ROOT / "docker" / "Dockerfile.sandbox"),
                   "-t", PythonSandboxTool.image, str(ROOT)], timeout=300)
    return {"built": result.returncode == 0, "image": PythonSandboxTool.image,
            "image_id": _docker_identity() if result.returncode == 0 else None}


def _pytest_validation(name: str, marker: str, expected: int) -> dict:
    if name == "search":
        status = search_preflight()
        return {"passed": status["validated"], "skipped": not status["validated"],
                "reason": None if status["validated"] else "NATIVE_SEARCH_VALIDATION_REQUIRED",
                "status": status["status"], "error_code": status.get("error_code"),
                "validation_command": ".\\.venv\\Scripts\\python.exe -B -X utf8 qa/science_live_validation.py --live"}
    if name == "docker" and not docker_preflight()["available"]:
        return {"passed": False, "skipped": True, "reason": "DOCKER_UNAVAILABLE"}
    if name == "api" and not api_preflight()["configured"]:
        return {"passed": False, "skipped": True, "reason": "API_UNCONFIGURED"}
    build_dir = ROOT / "build"
    try:
        build_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return {"passed": False, "passed_count": 0, "skipped": False,
                "exit_code": 1, "error_code": storage_error_code(error)}
    # 병행 검증에서 다른 실행의 자료를 삭제하지 않는다.
    base = (build_dir / f"pytest-validation-{name}-{uuid4().hex}").resolve()
    if not base.is_relative_to(ROOT.resolve()):
        raise ValueError("pytest temporary directory escaped workspace")
    source_before = _source_fingerprint()
    result = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                   "--basetemp", str(base), "--tb=short", "--junitxml", str(base.with_suffix(".xml")), "-m", marker], timeout=3600 if name == "core" else 600)
    output = _decode_output(result.stdout)
    error_code = _decode_output(result.stderr) if result.returncode else None
    if error_code not in {"VALIDATION_TIMEOUT", "STORAGE_FULL", "STORAGE_ACCESS_DENIED", "STORAGE_UNAVAILABLE"}:
        error_code = None
    match = re.search(r"(\d+) passed", output)
    passed_count = int(match.group(1)) if match and not error_code else 0
    # 실패 이름만 남기며 매개변수·오류 본문에 포함될 수 있는 비밀값은 저장하지 않는다.
    failed_tests = []
    for line in output.splitlines():
        if line.startswith("FAILED "):
            node = line[7:].split(" ", 1)[0].split("[", 1)[0].replace("\\", "/")
            if re.fullmatch(r"tests/(?:[A-Za-z0-9_-]+/)*test_[A-Za-z0-9_-]+\.py(?:::[A-Za-z0-9_]+){1,3}", node) and node not in failed_tests:
                failed_tests.append(node)
    skipped = bool(re.search(r"\d+ skipped", output))
    source_after = _source_fingerprint()
    if source_after != source_before:
        error_code = "VALIDATION_SOURCE_CHANGED"
    passed = result.returncode == 0 and passed_count >= expected and not skipped and not error_code
    marker_data = {"passed": passed, "passed_count": passed_count, "failed_tests": failed_tests,
                   "source_fingerprint": source_before,
                   "image_id": _docker_identity() if name == "docker" else None,
                   "manager_model": os.environ.get("PROBE_MANAGER_MODEL") if name == "api" else None,
                   "status": None, "provider": None,
                   "error_code": error_code}
    serialized = json.dumps(marker_data, ensure_ascii=False, sort_keys=True)
    serialized.encode("utf-8", errors="strict")
    try:
        VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
        (VALIDATION_DIR / f"{name}.json").write_text(serialized, encoding="utf-8")
    except OSError as error:
        return {"passed": False, "passed_count": passed_count, "skipped": skipped,
                "exit_code": result.returncode, "error_code": storage_error_code(error)}
    validation_result = {"passed": passed, "passed_count": passed_count, "skipped": skipped,
                         "failed_tests": failed_tests, "exit_code": result.returncode}
    if error_code is not None:
        validation_result["error_code"] = error_code
    return validation_result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["docker", "api", "search", "status", "all", "build-sandbox",
                                             "validate-core", "validate-docker", "smoke-api", "validate-search"])
    command = parser.parse_args().command
    if command == "docker":
        result = docker_preflight()
    elif command == "api":
        result = api_preflight()
    elif command == "search":
        result = search_preflight()
    elif command in {"status", "all"}:
        result = environment_status()
    elif command == "build-sandbox":
        result = build_sandbox()
    elif command == "validate-core":
        result = _pytest_validation("core", "not live_api and not live_search and not docker_integration and not os_secret_integration", 1)
    elif command == "validate-search":
        result = _pytest_validation("search", "live_search", 1)
    elif command == "validate-docker":
        result = _pytest_validation("docker", "docker_integration", 4)
    else:
        result = _pytest_validation("api", "live_api", 1)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
