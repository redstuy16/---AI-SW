"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import json

from htrsa import preflight


def test_missing_docker_and_api_report_unready_without_secrets(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("HTRSA_MANAGER_MODEL", raising=False)
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
    monkeypatch.setenv("HTRSA_MANAGER_MODEL", "fake-manager")
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


def test_release_gate_requires_stress_artifact_and_live_search(monkeypatch):
    fingerprint = preflight._source_fingerprint()
    markers = {name: {"passed": True, "source_fingerprint": fingerprint}
               for name in ("core", "demo_a", "demo_b", "docker", "api")}
    markers["docker"]["image_id"] = "qa-image"
    markers["api"]["manager_model"] = None
    monkeypatch.setattr(preflight, "_validation_marker", lambda name: markers.get(name))
    monkeypatch.setattr(preflight, "docker_preflight", lambda: {"available": True, "image_id": "qa-image"})
    monkeypatch.setattr(preflight, "api_preflight", lambda: {"configured": True})
    monkeypatch.setattr(preflight, "search_preflight", lambda: {"validated": False, "status": "NOT_VALIDATED"})
    first = preflight.environment_status()
    assert not first["demo_ready"] and not first["release_ready"]
    for name in ("stress", "artifact"):
        markers[name] = {"passed": True, "source_fingerprint": fingerprint}
    second = preflight.environment_status()
    assert second["demo_ready"] and not second["release_ready"]
    monkeypatch.setattr(preflight, "search_preflight", lambda: {"validated": True, "status": "VALIDATED"})
    assert preflight.environment_status()["release_ready"]
