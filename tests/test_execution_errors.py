"""실행·검증의 저장 실패와 Windows 출력 인코딩을 확인한다."""
from __future__ import annotations

import errno
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from probe import desktop, preflight
from probe.dashboard import APIResponse
from probe.storage_errors import storage_error_code
from probe.workbench import WorkbenchAPI


def sqlite_error(code, message="공개하지 않을 경로와 API 키"):
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize("code,expected", [
    (sqlite3.SQLITE_FULL, "STORAGE_FULL"),
    (sqlite3.SQLITE_BUSY, "DATABASE_BUSY"),
    (sqlite3.SQLITE_LOCKED, "DATABASE_BUSY"),
    (sqlite3.SQLITE_BUSY | (2 << 8), "DATABASE_BUSY"),
    (sqlite3.SQLITE_READONLY, "STORAGE_ACCESS_DENIED"),
    (sqlite3.SQLITE_PERM, "STORAGE_ACCESS_DENIED"),
    (sqlite3.SQLITE_CORRUPT, "DATABASE_INVALID"),
    (sqlite3.SQLITE_NOTADB, "DATABASE_INVALID"),
    (sqlite3.SQLITE_CANTOPEN, "DATABASE_UNAVAILABLE"),
])
def test_sqlite_primary_and_extended_codes_are_safe(code, expected):
    assert storage_error_code(sqlite_error(code)) == expected


@pytest.mark.parametrize("number,expected", [
    (errno.ENOSPC, "STORAGE_FULL"),
    (errno.EACCES, "STORAGE_ACCESS_DENIED"),
    (errno.EPERM, "STORAGE_ACCESS_DENIED"),
    (errno.EROFS, "STORAGE_ACCESS_DENIED"),
    (errno.EIO, "STORAGE_UNAVAILABLE"),
])
def test_os_storage_errors_do_not_expose_details(number, expected):
    assert storage_error_code(OSError(number, "비밀 오류 상세")) == expected


@pytest.mark.parametrize("number", [39, 112])
def test_windows_disk_full_codes(number):
    error = OSError("비밀 오류 상세")
    error.winerror = number
    assert storage_error_code(error) == "STORAGE_FULL"


@pytest.mark.parametrize("message,expected", [
    ("database or disk is full", "STORAGE_FULL"),
    ("database is locked", "DATABASE_BUSY"),
    ("database table is locked", "DATABASE_BUSY"),
])
def test_legacy_sqlite_errors_without_numeric_code(message, expected):
    assert storage_error_code(sqlite3.OperationalError(message)) == expected


@pytest.mark.parametrize("error,fragment", [
    (OSError(errno.ENOSPC, "secret-canary"), "저장 공간"),
    (PermissionError(errno.EACCES, "secret-canary"), "접근 권한"),
    (sqlite_error(sqlite3.SQLITE_BUSY), "사용 중"),
    (sqlite_error(sqlite3.SQLITE_FULL), "저장 공간"),
    (sqlite_error(sqlite3.SQLITE_CORRUPT), "DB"),
    (ImportError("secret-canary"), "패키지"),
])
def test_desktop_handles_startup_failure_without_traceback(tmp_path, monkeypatch, capsys, error, fragment):
    monkeypatch.setattr(desktop, "ROOT", tmp_path)
    messages = []
    def fail(*args, **kwargs):
        raise error
    assert desktop.main(["--data-dir", str(tmp_path / "app")], runner=fail, dialog=messages.append) == 1
    assert len(messages) == 1 and fragment in messages[0]
    assert "secret-canary" not in messages[0] and "공개하지 않을" not in messages[0]
    assert capsys.readouterr().out == "" and not (tmp_path / "app/state.sqlite").exists()


@pytest.mark.parametrize("error,expected", [
    (sqlite_error(sqlite3.SQLITE_FULL), "STORAGE_FULL"),
    (sqlite_error(sqlite3.SQLITE_BUSY), "DATABASE_BUSY"),
    (OSError(errno.ENOSPC, "secret-canary"), "STORAGE_FULL"),
    (PermissionError(errno.EACCES, "secret-canary"), "STORAGE_ACCESS_DENIED"),
])
def test_api_storage_failure_returns_safe_response(error, expected):
    api = WorkbenchAPI.__new__(WorkbenchAPI)
    def fail():
        raise error
    api.research_list = fail
    result = api._request("GET", "/api/control/research")
    assert result.status == 409 and result.body == {"error": expected}


def test_secret_scrubbing_db_failure_does_not_send_success_body():
    api = WorkbenchAPI.__new__(WorkbenchAPI)
    api._request = lambda *args: APIResponse(200, {"secret": "secret-canary"})
    def fail(*args):
        raise sqlite_error(sqlite3.SQLITE_BUSY)
    api.credentials = SimpleNamespace(active_secrets=fail)
    api.store = SimpleNamespace(configs=lambda *args: [])
    result = api.request("GET", "/api/control/settings")
    assert result.status == 409 and result.body == {"error": "DATABASE_BUSY"}


def test_real_child_mixed_utf8_cp949_and_invalid_bytes(monkeypatch):
    monkeypatch.setattr(preflight.locale, "getpreferredencoding", lambda *args: "cp949")
    result = preflight._run([sys.executable, "-B", "-c",
        "import sys;sys.stdout.buffer.write('검증 완료: 1 passed'.encode('utf-8'));"
        "sys.stderr.buffer.write('저장 공간 부족'.encode('cp949'))"])
    assert result.returncode == 0 and result.stdout == "검증 완료: 1 passed"
    assert result.stderr == "저장 공간 부족"
    damaged = preflight._run([sys.executable, "-B", "-c", "import sys;sys.stdout.buffer.write(bytes([255]));sys.exit(3)"])
    assert damaged.returncode == 3 and damaged.stdout == "\ufffd" and damaged.stderr == ""


def test_process_result_without_captured_fields_keeps_reported_exit_code(monkeypatch):
    monkeypatch.setattr(preflight.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=3))
    command = ["fixture-command"]
    result = preflight._run(command)
    assert result.args == command and result.returncode == 3
    assert result.stdout == "" and result.stderr == ""


def test_runner_launch_failure_and_timeout_do_not_become_success(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, "secret-canary")
    monkeypatch.setattr(preflight.subprocess, "run", fail)
    result = preflight._run(["unavailable"])
    assert result.returncode == 1 and result.stderr == "STORAGE_FULL"
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["secret-canary"], 1)
    monkeypatch.setattr(preflight.subprocess, "run", timeout)
    result = preflight._run(["unavailable"])
    assert result.returncode == 1 and result.stderr == "VALIDATION_TIMEOUT"


def test_validation_missing_stdout_keeps_gate_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    monkeypatch.setattr(preflight, "VALIDATION_DIR", tmp_path / "validation")
    monkeypatch.setattr(preflight, "_source_fingerprint", lambda: "offline-source")
    monkeypatch.setattr(preflight, "_run", lambda *a, **k: subprocess.CompletedProcess([], 0, None, None))
    result = preflight._pytest_validation("core", "offline", 1)
    marker = json.loads((tmp_path / "validation/core.json").read_text(encoding="utf-8"))
    assert result["passed"] is False and result["passed_count"] == 0 and marker["passed"] is False


def test_validation_preserves_only_failed_test_names_without_parameters_or_error_body(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, 'ROOT', tmp_path)
    monkeypatch.setattr(preflight, 'VALIDATION_DIR', tmp_path / 'validation')
    monkeypatch.setattr(preflight, '_source_fingerprint', lambda: 'offline-source')
    secret = 'credential-canary-must-never-be-written'
    stdout = ('F\nFAILED tests/test_real_cycle.py::test_dataset_tamper_after_stage_blocks_commit[' + secret + '] - ' + secret + '\n' +
        'FAILED tests/test_real_cycle.py::test_dataset_tamper_after_stage_blocks_commit[other-parameter] - failed\n' +
        'FAILED tests/test_preflight.py::TestGate::test_marker[private query] - ' + secret + '\n' +
        'FAILED /private/' + secret + ' - failed\n1 failed, 1459 passed\n')
    monkeypatch.setattr(preflight, '_run', lambda *a, **k: subprocess.CompletedProcess([], 1, stdout, secret))
    result = preflight._pytest_validation('core', 'offline', 1)
    marker = json.loads((tmp_path / 'validation/core.json').read_text(encoding='utf-8', errors='strict'))
    expected = ['tests/test_real_cycle.py::test_dataset_tamper_after_stage_blocks_commit',
                'tests/test_preflight.py::TestGate::test_marker']
    assert not result['passed'] and result['passed_count'] == 1459
    assert result['failed_tests'] == marker['failed_tests'] == expected
    assert secret not in json.dumps(result) + json.dumps(marker)
    assert '[' not in ''.join(marker['failed_tests']) and 'private query' not in json.dumps(marker)


@pytest.mark.parametrize("boundary", ["build", "marker", "write"])
def test_validation_storage_failure_is_reported_without_promoting_gate(tmp_path, monkeypatch, boundary):
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    monkeypatch.setattr(preflight, "VALIDATION_DIR", tmp_path / "validation")
    monkeypatch.setattr(preflight, "_source_fingerprint", lambda: "offline-source")
    calls = []
    monkeypatch.setattr(preflight, "_run", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess([], 0, "1 passed", ""))
    mkdir = Path.mkdir
    def storage_full(path, *args, **kwargs):
        if path == tmp_path / ("build" if boundary == "build" else "validation"):
            raise OSError(errno.ENOSPC, "secret-canary")
        return mkdir(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", storage_full)
    write_text = Path.write_text
    def failed_write(path, *args, **kwargs):
        if boundary == "write" and path == tmp_path / "validation/core.json":
            raise OSError(errno.ENOSPC, "secret-canary")
        return write_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", failed_write)
    result = preflight._pytest_validation("core", "offline", 1)
    assert result["passed"] is False and result["error_code"] == "STORAGE_FULL"
    assert len(calls) == (0 if boundary == "build" else 1)
    assert not (tmp_path / "validation/core.json").exists() and "secret-canary" not in json.dumps(result)


def test_real_sqlite_lock_is_identified_without_changing_database(tmp_path):
    path = tmp_path / "locked.sqlite"
    owner = sqlite3.connect(path)
    contender = sqlite3.connect(path, timeout=0)
    try:
        owner.execute("CREATE TABLE preserved(value TEXT)")
        owner.execute("INSERT INTO preserved VALUES('보존할 자료')")
        owner.commit()
        owner.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError) as failure:
            contender.execute("INSERT INTO preserved VALUES('실패할 변경')")
        assert storage_error_code(failure.value) == "DATABASE_BUSY"
        owner.rollback()
        assert owner.execute("SELECT value FROM preserved").fetchall() == [("보존할 자료",)]
    finally:
        contender.close()
        owner.close()
