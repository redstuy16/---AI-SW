"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import json
import hashlib
import errno
import runpy
import subprocess
import sys

import pytest

from probe import preflight


def native_search_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, 'ROOT', tmp_path)
    monkeypatch.setattr(preflight, 'VALIDATION_DIR', tmp_path / 'build' / 'validation')
    monkeypatch.setattr(preflight, '_source_fingerprint', lambda: 'current-source')
    usage = {'input_tokens': 100, 'output_tokens': 50, 'web_search_calls': 1}
    raw = {'research_id': 'R-native', 'execution': 'LIVE_USER_SESSION', 'requirements_met': True,
        'provider_measurements': [{'reservation_id': 'reservation-native', 'response_id': 'response-native',
            'resolved_model_id': 'gpt-6-luna', 'usage': usage}],
        'search_measurements': [{'provider': 'openai.web_search', 'response_id': 'response-native',
            'status': 'COMPLETED', 'billed_search_calls': 1, 'hosted_tool_actions': 3, 'usage': usage}],
        'ledger': [{'id': 'reservation-native', 'status': 'SETTLED', 'settled': 10035}]}
    path = tmp_path / 'build' / 'science-live' / 'offline-fixture' / 'raw' / 'R-native.json'
    path.parent.mkdir(parents=True)
    data = json.dumps(raw, ensure_ascii=False).encode('utf-8', errors='strict')
    path.write_bytes(data)
    evidence = {'execution': 'LIVE_USER_SESSION', 'runs': [{'research_id': 'R-native',
        'artifact_path': path.relative_to(tmp_path).as_posix(), 'artifact_sha256': hashlib.sha256(data).hexdigest()}]}
    return evidence, raw, path


def test_missing_docker_and_api_report_unready_without_secrets(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PROBE_MANAGER_MODEL", raising=False)
    monkeypatch.setattr(preflight, "_validation_marker", lambda name: None)
    status = preflight.environment_status()
    assert status["docker"]["available"] is False
    assert status["docker"]["isolation_validated"] is False
    assert status["provider"]["configured"] is False
    assert status["sandbox"]["fail_closed"] is True
    assert status["release_ready"] is False
    assert not any(status["gates"].values())


def test_api_preflight_reports_presence_without_key_value(monkeypatch):
    secret = "test-secret-never-print"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("PROBE_MANAGER_MODEL", "fake-manager")
    status = preflight.api_preflight()
    assert status["api_key_present"] is True
    assert status["manager_model_configured"] is True
    assert secret not in json.dumps(status)


def test_validation_runners_skip_unavailable_external_dependencies(monkeypatch):
    monkeypatch.setattr(preflight, "docker_preflight", lambda: {"available": False})
    monkeypatch.setattr(preflight, "api_preflight", lambda: {"configured": False})
    assert preflight._pytest_validation("docker", "docker_integration", 4) == {
        "passed": False, "skipped": True, "reason": "DOCKER_UNAVAILABLE"}
    assert preflight._pytest_validation("api", "live_api", 1) == {
        "passed": False, "skipped": True, "reason": "API_UNCONFIGURED"}


@pytest.mark.parametrize("name,expected_timeout", [("core", 3600), ("docker", 600), ("api", 600)])
def test_validation_extends_only_the_core_timeout(tmp_path, monkeypatch, name, expected_timeout):
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    monkeypatch.setattr(preflight, "VALIDATION_DIR", tmp_path / "validation")
    monkeypatch.setattr(preflight, "_source_fingerprint", lambda: "offline-source")
    monkeypatch.setattr(preflight, "docker_preflight", lambda: {"available": True})
    monkeypatch.setattr(preflight, "api_preflight", lambda: {"configured": True})
    monkeypatch.setattr(preflight, "_docker_identity", lambda: "offline-image")
    monkeypatch.delenv("PROBE_MANAGER_MODEL", raising=False)
    timeouts = []
    def completed(command, *, timeout):
        timeouts.append(timeout)
        return subprocess.CompletedProcess(command, 0, "1 passed", "")
    monkeypatch.setattr(preflight, "_run", completed)
    result = preflight._pytest_validation(name, "offline", 1)
    assert timeouts == [expected_timeout]
    assert result == {"passed": True, "passed_count": 1, "skipped": False, "failed_tests": [], "exit_code": 0}
    marker = json.loads((tmp_path / "validation" / f"{name}.json").read_text(encoding="utf-8"))
    assert marker["error_code"] is None


@pytest.mark.parametrize("fault,error_code", [
    ("timeout", "VALIDATION_TIMEOUT"),
    (errno.ENOSPC, "STORAGE_FULL"),
    (errno.EACCES, "STORAGE_ACCESS_DENIED"),
    (errno.ENOENT, "STORAGE_UNAVAILABLE"),
])
def test_validation_preserves_only_safe_launch_error_codes(tmp_path, monkeypatch, fault, error_code):
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    monkeypatch.setattr(preflight, "VALIDATION_DIR", tmp_path / "validation")
    monkeypatch.setattr(preflight, "_source_fingerprint", lambda: "offline-source")
    canary = "비밀값과 경로를 대신하는 시험 문자열"
    def fail(command, **kwargs):
        assert kwargs["timeout"] == 3600
        if fault == "timeout":
            raise subprocess.TimeoutExpired(command, 3600, output=f"9 passed {canary}", stderr=canary)
        raise OSError(fault, canary)
    # 실제 프로세스나 네트워크를 시작하지 않고 실행기 실패를 주입한다.
    monkeypatch.setattr(preflight.subprocess, "run", fail)
    result = preflight._pytest_validation("core", "offline", 1)
    marker = json.loads((tmp_path / "validation/core.json").read_text(encoding="utf-8"))
    assert result["passed"] is False and result["passed_count"] == 0
    assert result["failed_tests"] == [] and result["error_code"] == error_code
    assert marker["passed"] is False and marker["error_code"] == error_code
    assert canary not in json.dumps(result, ensure_ascii=False) + json.dumps(marker, ensure_ascii=False)


@pytest.mark.parametrize("stderr", ["VALIDATION_TIMEOUT\n", "STORAGE_FULL private-canary", "private-canary", "DATABASE_BUSY", ""])
def test_validation_does_not_record_arbitrary_stderr(tmp_path, monkeypatch, stderr):
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    monkeypatch.setattr(preflight, "VALIDATION_DIR", tmp_path / "validation")
    monkeypatch.setattr(preflight, "_source_fingerprint", lambda: "offline-source")
    monkeypatch.setattr(preflight, "_run", lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", stderr))
    result = preflight._pytest_validation("core", "offline", 1)
    marker = json.loads((tmp_path / "validation/core.json").read_text(encoding="utf-8"))
    assert result == {"passed": False, "passed_count": 0, "skipped": False, "failed_tests": [], "exit_code": 1}
    assert marker["error_code"] is None
    assert "private-canary" not in json.dumps(result) + json.dumps(marker)


def test_successful_validation_keeps_contract_even_with_code_in_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    monkeypatch.setattr(preflight, "VALIDATION_DIR", tmp_path / "validation")
    monkeypatch.setattr(preflight, "_source_fingerprint", lambda: "offline-source")
    monkeypatch.setattr(preflight, "_run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "1 passed", "STORAGE_FULL"))
    result = preflight._pytest_validation("core", "offline", 1)
    marker = json.loads((tmp_path / "validation/core.json").read_text(encoding="utf-8"))
    assert result == {"passed": True, "passed_count": 1, "skipped": False, "failed_tests": [], "exit_code": 0}
    assert marker["error_code"] is None


def test_source_change_during_validation_cannot_mark_current_code_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, 'ROOT', tmp_path)
    monkeypatch.setattr(preflight, 'VALIDATION_DIR', tmp_path / 'validation')
    fingerprints = iter(['before', 'after'])
    monkeypatch.setattr(preflight, '_source_fingerprint', lambda: next(fingerprints))
    monkeypatch.setattr(preflight, '_run', lambda *args, **kwargs: subprocess.CompletedProcess([], 0, '1 passed', ''))
    result = preflight._pytest_validation('core', 'offline', 1)
    marker = json.loads((tmp_path / 'validation/core.json').read_text(encoding='utf-8'))
    assert not result['passed'] and result['error_code'] == 'VALIDATION_SOURCE_CHANGED'
    assert marker['source_fingerprint'] == 'before' and not marker['passed']


def test_release_gate_requires_stress_artifact_and_live_search(monkeypatch, tmp_path):
    evidence, _, _ = native_search_evidence(tmp_path, monkeypatch)
    fingerprint = preflight._source_fingerprint()
    markers = {name: {"passed": True, "source_fingerprint": fingerprint}
               for name in ("core", "demo_a", "demo_b", "docker", "api")}
    markers["docker"]["image_id"] = "qa-image"
    markers["api"]["manager_model"] = None
    monkeypatch.setattr(preflight, "_validation_marker", lambda name: markers.get(name))
    monkeypatch.setattr(preflight, "docker_preflight", lambda: {"available": True, "image_id": "qa-image"})
    monkeypatch.setattr(preflight, "api_preflight", lambda: {"configured": True})
    first = preflight.environment_status()
    assert not first["demo_ready"] and not first["release_ready"]
    for name in ("stress", "artifact"):
        markers[name] = {"passed": True, "source_fingerprint": fingerprint}
    second = preflight.environment_status()
    assert second["demo_ready"] and not second["release_ready"]
    markers['search'] = {'status': 'VALIDATED', 'provider': 'openai.web_search',
                         'source_fingerprint': fingerprint, 'evidence': evidence}
    assert preflight.environment_status()["release_ready"]


@pytest.mark.parametrize('provider', ['scholarly.openalex', 'scholarly.crossref', 'openai.web_search'])
def test_legacy_or_unproven_search_marker_never_satisfies_current_gate(monkeypatch, provider):
    monkeypatch.setattr(preflight, '_source_fingerprint', lambda: 'current-source')
    monkeypatch.setattr(preflight, '_validation_marker', lambda _: {
        'status': 'VALIDATED', 'provider': provider, 'source_fingerprint': 'current-source'})
    status = preflight.search_preflight()
    assert not status['validated'] and status['error_code'] == 'NATIVE_SEARCH_EVIDENCE_REQUIRED'


@pytest.mark.parametrize('fault', ['mock', 'response', 'usage', 'zero_search', 'unknown_usage', 'actions', 'unresolved', 'zero_cost', 'hash'])
def test_native_search_marker_requires_real_matching_response_usage_and_settlement(tmp_path, monkeypatch, fault):
    evidence, raw, path = native_search_evidence(tmp_path, monkeypatch)
    if fault == 'mock': raw['execution'] = 'MOCK_SIMULATION'
    elif fault == 'response': raw['search_measurements'][0]['response_id'] = 'other-response'
    elif fault == 'usage': raw['provider_measurements'][0]['usage'] = {**raw['provider_measurements'][0]['usage'], 'input_tokens': 101}
    elif fault == 'zero_search': raw['search_measurements'][0]['usage']['web_search_calls'] = 0
    elif fault == 'unknown_usage': raw['search_measurements'][0]['usage']['input_tokens'] = None
    elif fault == 'actions': raw['search_measurements'][0]['hosted_tool_actions'] = 0
    elif fault == 'unresolved': raw['ledger'][0]['status'] = 'UNRESOLVED'
    elif fault == 'zero_cost': raw['ledger'][0]['settled'] = 0
    data = json.dumps(raw, ensure_ascii=False).encode('utf-8', errors='strict')
    path.write_bytes(data + (b' ' if fault == 'hash' else b''))
    evidence['runs'][0]['artifact_sha256'] = hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError, match='NATIVE_SEARCH_EVIDENCE_REQUIRED'):
        preflight.record_search_validation(status='VALIDATED', provider='openai.web_search', evidence=evidence)
    assert not preflight.VALIDATION_DIR.exists()


def test_native_search_marker_records_only_verified_current_proof(tmp_path, monkeypatch):
    evidence, _, path = native_search_evidence(tmp_path, monkeypatch)
    marker = preflight.record_search_validation(status='VALIDATED', provider='openai.web_search', evidence=evidence)
    assert marker['validated'] and preflight.search_preflight()['validated']
    path.write_bytes(path.read_bytes() + b'tamper')
    assert not preflight.search_preflight()['validated']


def test_validate_search_only_reads_native_proof_and_never_runs_legacy_network(monkeypatch):
    monkeypatch.setenv('PROBE_LIVE_SEARCH', '1')
    monkeypatch.setenv('PROBE_LEGACY_LIVE_SEARCH', '1')
    monkeypatch.setattr(preflight, '_validation_marker', lambda _: None)
    monkeypatch.setattr(preflight, '_source_fingerprint', lambda: 'current-source')
    def forbidden(*args, **kwargs): raise AssertionError('검증 표식 조회에서 네트워크를 실행하면 안 됩니다.')
    monkeypatch.setattr(preflight, '_run', forbidden)
    result = preflight._pytest_validation('search', 'live_search', 1)
    assert not result['passed'] and result['skipped']
    assert result['reason'] == 'NATIVE_SEARCH_VALIDATION_REQUIRED'
    assert 'science_live_validation.py --live' in result['validation_command']


@pytest.mark.parametrize('script', ['research_report_live_search_probe.py', 'free_search_live_probe.py'])
def test_legacy_live_probes_require_separate_opt_in_before_any_workspace_or_network(monkeypatch, script):
    monkeypatch.delenv('PROBE_LEGACY_LIVE_SEARCH', raising=False)
    monkeypatch.setenv('PROBE_LIVE_SEARCH', '1')
    monkeypatch.setattr(sys, 'path', list(sys.path))
    import probe.workbench
    def forbidden(*args, **kwargs): raise AssertionError('LEGACY 실행 동의 전 작업 공간을 만들면 안 됩니다.')
    monkeypatch.setattr(probe.workbench, 'WorkbenchAPI', forbidden)
    with pytest.raises(SystemExit, match='LEGACY_LIVE_SEARCH_DISABLED'):
        runpy.run_path(str(preflight.ROOT / 'qa' / script), run_name='__main__')
