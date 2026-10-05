"""사용자 키를 수정하지 않고 Windows 별도 canary와 GPT 설정을 실제 확인한다."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import secrets
import subprocess
import sys
from uuid import uuid4

from probe.control_plane import Credentials, ControlError
from probe.preflight import VALIDATION_DIR, _source_fingerprint, native_environment_id
from probe.sandbox import clean_environment
from probe.schemas import utc_now
from probe.secret_store import WindowsCredentialStore, SecretStoreUnavailable
from probe.workbench import WorkbenchAPI


ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def windows_key():
    namespace = "Probe-Product-QA-" + uuid4().hex
    name = "PROBE_QA_CANARY"
    store = WindowsCredentialStore(namespace)
    first, second = secrets.token_urlsafe(36), secrets.token_urlsafe(36)
    record = {"passed": False, "status": "NOT_VALIDATED", "execution": "NATIVE_WINDOWS",
              "checked_at": utc_now().isoformat(), "source_fingerprint": _source_fingerprint(),
              "environment_id": native_environment_id(), "checks": {}, "canary_exposures": 0,
              "namespace": "별도 일회성 QA namespace", "user_credentials_changed": False}
    checks = record["checks"]
    code = '''import hashlib,json,os,sys
from probe.secret_store import WindowsCredentialStore
expected=json.loads(sys.stdin.read())
store=WindowsCredentialStore(sys.argv[1])
value,_=store.read('PROBE_QA_CANARY')
print(json.dumps({'read':bool(value and hashlib.sha256(value.encode('utf-8',errors='strict')).hexdigest()==expected['digest']),'credential_environment_empty':not any(k.endswith('API_KEY') or k=='PROBE_QA_CANARY' for k in os.environ)}))
'''
    code.encode("utf-8", errors="strict")
    try:
        if not store.available:
            record["error_code"] = "WINDOWS_STORE_UNAVAILABLE"
            return record
        store.write(name, first)
        checks["save"] = True
        checks["read"] = secrets.compare_digest(store.read(name)[0] or "", first)
        store.write(name, second)
        checks["rotate"] = secrets.compare_digest(store.read(name)[0] or "", second)
        environment = clean_environment()
        child = subprocess.run([sys.executable, "-c", code, namespace], input=json.dumps({"digest": sha256(second.encode("utf-8", errors="strict")).hexdigest()}),
                               capture_output=True, text=True, encoding="utf-8", env=environment, timeout=20)
        if any(v in child.stdout or v in child.stderr for v in (first, second)):
            record["canary_exposures"] += 1
        observed = json.loads(child.stdout) if child.returncode == 0 else {}
        checks["restart"] = observed.get("read") is True
        checks["child_environment_clean"] = observed.get("credential_environment_empty") is True and all(v not in environment.values() for v in (first, second))
        store.write(name, None)
        checks["delete"] = store.read(name)[0] is None
        record["passed"] = all(checks.get(k) is True for k in ("save", "read", "rotate", "restart", "delete", "child_environment_clean")) and record["canary_exposures"] == 0
        record["status"] = "VALIDATED" if record["passed"] else "NOT_VALIDATED"
    except (SecretStoreUnavailable, OSError) as exc:
        record["error_code"] = "WINDOWS_ERROR_" + str(exc.errno)
    except (ValueError, subprocess.SubprocessError):
        record["error_code"] = "PROBE_INCOMPLETE"
    finally:
        try:
            WindowsCredentialStore(namespace).write(name, None)
        except (SecretStoreUnavailable, OSError):
            record["cleanup_available"] = False
        assert first not in json.dumps(record) and second not in json.dumps(record)
    return record


def gpt_live():
    record = {"status": "NOT_VALIDATED", "checked_at": utc_now().isoformat(), "source_fingerprint": _source_fingerprint(),
              "research_efficacy": "NOT_VALIDATED", "paid_requests": 0, "max_total_usd": "0.10", "automatic_retry": False}
    credentials = Credentials(ROOT, ROOT / "build/workbench/workspace")
    record["credential"] = credentials.metadata("OPENAI_API_KEY")
    if not record["credential"]["configured"]:
        record["error_code"] = "CREDENTIAL_UNCONFIGURED"
        return record
    folder = ROOT / "build/productization-live" / uuid4().hex
    app = WorkbenchAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    try:
        response = app.request("POST", "/api/control/catalog/select", {
            "model_id": "gpt-6.1-sol", "approve_price": True, "approve_destination": True})
        if response.status != 201:
            raise ControlError(response.body["error"])
        from probe.productization import qualify
        result = asyncio.run(qualify(app, response.body["profile_id"], {"consent": True, "approve_discovery": True,
            "idempotency_key": uuid4().hex, "max_total_usd": "0.10"}))
        record.update(status=result["status"], error_code=result.get("error_code"), checks=result["checks"], ledger=result["ledger"])
        record["paid_requests"] = len([r for r in result["ledger"]["requests"] if r["purpose"] != "model_discovery"])
    except ControlError as exc:
        record["error_code"] = exc.code
    finally:
        app.close()
    return record


def main():
    windows, live = windows_key(), gpt_live()
    save(VALIDATION_DIR / "windows_key.json", windows)
    save(ROOT / "qa/results/productization_native_results.json", {"windows_key": windows, "gpt_live": live})
    print(json.dumps({"windows_key": windows["status"], "windows_error": windows.get("error_code"),
                      "gpt_live": live["status"], "gpt_error": live.get("error_code"), "paid_requests": live["paid_requests"],
                      "canary_exposures": windows["canary_exposures"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
