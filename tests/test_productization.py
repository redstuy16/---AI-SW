"""제품 연결의 비용·복구·키 저장·콘솔 없는 실행 경계를 검사한다."""
import asyncio
from decimal import Decimal
from hashlib import sha256
import json
import secrets
import subprocess
import sys
import xml.etree.ElementTree as ET

import httpx
import pytest

from probe.control_plane import ControlError, Credentials
from probe.desktop import ROOT, fallback_link, main
from probe.productization import onboarding, qualification_view, qualify, revision
from probe.workbench import OwnerSession, WorkbenchAPI, open_browser
from test_multi_provider import app, document
from test_local_browser_auth import local, exchange


def prepare(app, monkeypatch, *, key=True):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if key:
        monkeypatch.setenv("OPENAI_API_KEY", "offline-productization-canary-987654321")
    response = app.request("POST", "/api/control/catalog/select", {
        "model_id": "gpt-6.1-sol", "approve_price": True, "approve_destination": True})
    assert response.status == 201
    return response.body["profile_id"]


def body(**changes):
    return {"consent": True, "approve_discovery": True, "max_total_usd": "0.10",
            "idempotency_key": "product-test-request", **changes}


def transport(calls, fault=None):
    def respond(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "gpt-6.1-sol"}]})
        payload = json.loads(request.content)
        tools = payload.get("tool_choice") == "required"
        value = document("responses", tools=tools)
        if fault in {401, 429, 503}:
            return httpx.Response(fault, json={"error": {"message": "offline-productization-canary-987654321"}})
        if fault == "timeout":
            raise httpx.ReadTimeout("검사 응답 미수신")
        if fault == "usage":
            value.pop("usage")
        if fault == "truncated":
            return httpx.Response(200, content=b'{"partial":')
        if fault == "incomplete":
            value["status"] = "incomplete"
        if fault == "empty":
            value["output"][0]["content"][0]["text"] = ""
        if fault == "secret":
            value["output"][0]["content"][0]["text"] = "offline-productization-canary-987654321"
        if fault == "bad_json" and "text" in payload:
            value["output"][0]["content"][0]["text"] = '{"status":'
        if fault == "bad_tool" and tools:
            value["output"][0]["arguments"] = '{"key":"unapproved"}'
        return httpx.Response(200, json=value)
    return lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(respond))


def run(app, identity, calls, request=None, fault=None):
    return asyncio.run(qualify(app, identity, request or body(), client_factory=transport(calls, fault)))


def test_free_onboarding_and_friendly_catalog_do_not_claim_live(app, monkeypatch):
    identity = prepare(app, monkeypatch)
    settings = app.request("GET", "/api/control/settings").body
    view = settings["onboarding"]
    assert [s["id"] for s in view["steps"]] == ["key", "connection", "model", "budget", "research"]
    assert [s["complete"] for s in view["steps"]] == [True, False, True, False, False]
    assert view["gpt_connectivity"] == view["research_efficacy"] == "NOT_VALIDATED"
    labels = [m["picker_label"] for m in settings["catalog"]["models"]]
    assert any("빠른 GPT" in label for label in labels)
    assert any("고성능 GPT" in label for label in labels)
    assert all("gpt-6" not in label for label in labels)
    assert qualification_view(app, identity)["status"] == "NOT_VALIDATED"
    assert not app.store.ledger()["requests"]


@pytest.mark.parametrize("problem,expected", [("consent", "PAID_TEST_CONSENT_REQUIRED"),
    ("key", "CREDENTIAL_UNCONFIGURED"), ("discovery", "DISCOVERY_COST_NOT_BOUNDED_MANUAL_ID_ALLOWED")])
def test_owner_preconditions_block_before_dispatch(app, monkeypatch, problem, expected):
    identity = prepare(app, monkeypatch, key=problem != "key")
    request = body(consent=problem != "consent", approve_discovery=problem != "discovery")
    calls = []
    with pytest.raises(ControlError, match=expected):
        run(app, identity, calls, request)
    assert not calls and not app.store.ledger()["requests"]


def test_full_probe_shares_one_budget_and_separates_mock_from_live(app, monkeypatch):
    identity, calls = prepare(app, monkeypatch), []
    result = run(app, identity, calls)
    assert result["status"] == "OFFLINE_VALIDATED" and result["execution"] == "MOCK_HTTP"
    assert [c["mode"] for c in result["checks"]] == ["discovery", "text", "structured", "tools", "reasoning"]
    assert len(calls) == 6 and len([c for c in calls if c.method == "POST"]) == 5
    assert len({r["research_id"] for r in app.store.ledger()["requests"]}) == 1
    assert all(r["status"] == "SETTLED" for r in app.store.ledger()["requests"])
    assert sum(r["settled"] for r in app.store.ledger()["requests"]) <= 100000
    usage = [u for check in result["checks"] for u in check["usage"] or []]
    assert sum(u["input_tokens"] for u in usage) == 200
    assert sum(u["output_tokens"] for u in usage) == 60
    assert json.loads(calls[-1].content)["reasoning"]["effort"] == "low"
    assert onboarding(app)["gpt_connectivity"] == "NOT_VALIDATED"
    assert not onboarding(app)["steps"][1]["complete"]
    assert "offline-productization-canary" not in json.dumps(result)
    assert "offline-productization-canary" not in app.database.read_bytes().decode("utf-8", errors="ignore")


def test_same_request_replay_and_conflicting_body_never_redispatch(app, monkeypatch):
    identity, calls = prepare(app, monkeypatch), []
    run(app, identity, calls)
    assert run(app, identity, calls)["replayed"] and len(calls) == 6
    with pytest.raises(ControlError, match="IDEMPOTENCY_CONFLICT"):
        run(app, identity, calls, body(max_total_usd=".09"))
    assert len(calls) == 6


def test_environment_credential_proof_does_not_survive_process_change(app, monkeypatch):
    import os
    identity, calls = prepare(app, monkeypatch), []
    run(app, identity, calls)
    previous_pid = os.getpid()
    monkeypatch.setattr(os, "getpid", lambda: previous_pid + 1)
    assert qualification_view(app, identity)["status"] == "STALE"
    assert run(app, identity, calls)["status"] == "STALE" and len(calls) == 6


@pytest.mark.parametrize("change", ["model", "connection", "credential"])
def test_changed_owner_configuration_invalidates_old_success(app, monkeypatch, change):
    identity, calls = prepare(app, monkeypatch), []
    run(app, identity, calls)
    kind, name = ("model", identity) if change == "model" else ("connection", "gpt-default") if change == "connection" else ("credential_change", "gpt-default")
    value = app.store.config(kind, name) if change != "credential" else {"changed_at": "새 키"}
    if change != "credential":
        value["display_name"] = "변경된 표시 이름"
    app.store.put(kind, name, value, revision(app.store, kind, name))
    assert qualification_view(app, identity)["status"] == "STALE"
    assert run(app, identity, calls)["status"] == "STALE" and len(calls) == 6


@pytest.mark.parametrize("fault", [401, 429, 503, "timeout", "usage", "truncated", "incomplete", "empty", "secret", "bad_json", "bad_tool"])
def test_faults_never_claim_success_or_automatically_retry(app, monkeypatch, fault):
    identity, calls = prepare(app, monkeypatch), []
    result = run(app, identity, calls, fault=fault)
    assert result["status"] == "NOT_VALIDATED" and not result["automatic_retry"]
    assert result["research_efficacy"] == "NOT_VALIDATED"
    count = len(calls)
    assert count <= 5
    assert run(app, identity, calls, fault=fault)["replayed"] and len(calls) == count
    assert "offline-productization-canary" not in json.dumps(result)
    assert not onboarding(app)["steps"][1]["complete"]


@pytest.mark.parametrize("cap", [".00001", ".0107"])
def test_whole_suite_cap_holds_before_or_between_calls(app, monkeypatch, cap):
    identity, calls = prepare(app, monkeypatch), []
    result = run(app, identity, calls, body(discovery=False, max_total_usd=cap))
    assert result["status"] == "NOT_VALIDATED"
    assert result["error_code"] == "BUDGET_BLOCKED"
    assert len(calls) == (0 if cap == ".00001" else 2)
    assert sum(r["settled"] if r["settled"] is not None else r["reserved"] for r in app.store.ledger()["requests"]) <= Decimal(cap) * 1000000


def test_interrupted_probe_recovers_without_external_reexecution(app, monkeypatch):
    identity = prepare(app, monkeypatch)
    rid = "QUALIFICATION-crash"
    reservation = app.store.reserve(rid=rid, connection="gpt-default", model="gpt-6.1-sol", role="settings",
        purpose="settings_smoke", bound=".01", run_limit=".10", monthly_limit=20, request_limit=".25", attempts=6, revision="qa")
    app.store.transition(reservation, "DISPATCHED")
    key = sha256((identity + ":" + body()["idempotency_key"]).encode("utf-8", errors="strict")).hexdigest()
    from probe.productization import QualificationRequest
    record = {"request_key": key, "profile_id": identity, "run_id": rid, "owner_pid": -1,
              "status": "RUNNING", "request_fingerprint": sha256(QualificationRequest.model_validate(body()).model_dump_json().encode("utf-8", errors="strict")).hexdigest()}
    app.store.put("qualification_run", key, record)
    recovered = WorkbenchAPI(app.database, app.workspace, launch=False, credential_file=app.credentials.file)
    try:
        calls = []
        replay = run(recovered, identity, calls)
        assert replay["status"] == "NEEDS_RECONCILIATION" and not calls
        assert recovered.store.ledger(rid)["requests"][0]["status"] == "UNRESOLVED"
    finally:
        recovered.close()


class MemoryStore:
    available = True

    def __init__(self, drop=False):
        self.value, self.drop, self.writes = None, drop, 0

    def read(self, name):
        return self.value, None

    def write(self, name, value):
        self.writes += 1
        if not self.drop:
            self.value = value


def test_os_write_readback_failure_is_not_save_success(tmp_path):
    credentials = Credentials(tmp_path / "repo", tmp_path / "workspace", tmp_path / "private/key.env")
    credentials.os_store = MemoryStore(drop=True)
    with pytest.raises(ControlError, match="SECRET_READBACK_FAILED"):
        credentials.save("OPENAI_API_KEY", "offline-only-value")
    assert not credentials.metadata("OPENAI_API_KEY")["saved"]


def test_utf8_key_readback_uses_bytes_without_type_error(tmp_path, monkeypatch):
    monkeypatch.delenv("PROBE_QA_KEY", raising=False)
    credentials = Credentials(tmp_path / "repo", tmp_path / "workspace", tmp_path / "private/key.env")
    credentials.os_store = MemoryStore()
    assert credentials.save("PROBE_QA_KEY", "유효한UTF8테스트값")["saved"]
    assert credentials.save("PROBE_QA_KEY", None)["configured"] is False


def test_existing_unsafe_fallback_blocks_before_os_mutation(tmp_path, monkeypatch):
    credentials = Credentials(tmp_path / "repo", tmp_path / "workspace", tmp_path / "private/key.env")
    credentials.file.parent.mkdir()
    credentials.file.write_bytes("기존 파일\n".encode("utf-8", errors="strict"))
    credentials.os_store = MemoryStore()
    def insecure(*args):
        raise ControlError("SECRET_ACL_UNVERIFIED")
    monkeypatch.setattr(credentials, "_secure", insecure)
    with pytest.raises(ControlError, match="SECRET_ACL_UNVERIFIED"):
        credentials.save("OPENAI_API_KEY", "offline-only-value")
    assert credentials.os_store.writes == 0


def test_native_launcher_uses_existing_server_quietly(tmp_path, monkeypatch):
    import probe.desktop as desktop
    monkeypatch.setattr(desktop, "ROOT", tmp_path)
    calls = []
    assert main(["--data-dir", str(tmp_path / "app")], runner=lambda *a, **k: calls.append((a, k))) == 0
    assert calls[0][1]["quiet"] is True and callable(calls[0][1]["fallback"])
    assert calls[0][0][0] == [str(tmp_path / "app/state.sqlite"), str(tmp_path / "app/workspace")]
    messages = []
    assert main(["--data-dir", str(tmp_path.parent)], runner=lambda *_: pytest.fail("실행 금지"), dialog=lambda m: messages.append(m)) == 1
    assert messages


def test_hidden_wsf_contains_no_key_ticket_or_shell_command():
    tree = ET.parse(ROOT / "Probe.wsf")
    script = tree.find("./script").text
    assert "pythonw.exe" in script and "probe.desktop" in script
    assert ", 0, False" in script
    assert "cmd.exe" not in script and "OPENAI_API_KEY" not in script and "bootstrap=" not in script


@pytest.mark.parametrize("choice,opened", [(False, False), (True, False), (True, True)])
def test_native_browser_fallback_cancels_or_reopens_without_printing(choice, opened, capsys):
    seen, dialogs = [], []
    private = "http://127.0.0.1:1234/#bootstrap=" + secrets.token_urlsafe(32)
    def dialog(message, **kwargs):
        dialogs.append(message)
        return choice
    def opener(url, **kwargs):
        seen.append(url)
        return opened
    assert fallback_link(private, opener=opener, dialog=dialog) == (choice and opened)
    assert len(seen) == int(choice) and all("bootstrap=" not in m for m in dialogs)
    assert capsys.readouterr().out == ""
    session = OwnerSession()
    open_browser(session, "http://127.0.0.1:1234", opener=lambda *_a, **_k: False, fallback=lambda url: seen.append(url))
    assert capsys.readouterr().out == ""


def test_new_endpoints_retain_owner_auth_and_csrf(local):
    client = local["client"]
    assert client.get("/api/control/onboarding").status_code == 401
    path = "/api/control/models/not-saved/qualify"
    assert client.post(path, json=body(), headers={"Origin": local["origin"]}).status_code == 403
    csrf = exchange(local).json()["csrf"]
    assert client.get("/api/control/onboarding").json()["gpt_connectivity"] == "NOT_VALIDATED"
    assert client.post(path, json=body(), headers={"Origin": local["origin"]}).status_code == 403
    assert client.post(path, json=body(), headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf}).status_code == 403
    response = client.post(path, json=body(), headers={"Origin": local["origin"], "X-CSRF-Token": csrf})
    assert response.status_code == 409 and response.json()["error"] == "CONFIG_MISSING"


def test_probe_crash_in_fresh_process_keeps_uncertain_charge_and_no_retry(app, monkeypatch):
    from probe.productization import QualificationRequest
    from probe.sandbox import clean_environment
    identity = prepare(app, monkeypatch)
    key = sha256((identity + ":" + body()["idempotency_key"]).encode("utf-8", errors="strict")).hexdigest()
    fingerprint = sha256(QualificationRequest.model_validate(body()).model_dump_json().encode("utf-8", errors="strict")).hexdigest()
    code = '''import os,sys
from probe.workbench import WorkbenchAPI
a=WorkbenchAPI(sys.argv[1],sys.argv[2],launch=False,credential_file=sys.argv[3])
r=a.store.reserve(rid='QUALIFICATION-process-crash',connection='gpt-default',model='gpt-6.1-sol',role='settings',purpose='settings_smoke',bound='.01',run_limit='.10',monthly_limit=20,request_limit='.25',attempts=6,revision='qa')
a.store.put('qualification_run',sys.argv[4],{'request_key':sys.argv[4],'profile_id':sys.argv[5],'run_id':'QUALIFICATION-process-crash','request_fingerprint':sys.argv[6],'owner_pid':os.getpid(),'status':'RUNNING'})
a.store.transition(r,'DISPATCHED')
os._exit(86)
'''
    child = subprocess.run([sys.executable, "-c", code, str(app.database), str(app.workspace), str(app.credentials.file), key, identity, fingerprint],
                           capture_output=True, env=clean_environment(), timeout=20)
    assert child.returncode == 86 and not child.stdout
    recovered = WorkbenchAPI(app.database, app.workspace, launch=False, credential_file=app.credentials.file)
    try:
        calls = []
        result = run(recovered, identity, calls)
        assert result["status"] == "NEEDS_RECONCILIATION" and result["replayed"]
        assert result["ledger"]["requests"][0]["status"] == "UNRESOLVED"
        assert not calls
    finally:
        recovered.close()


@pytest.mark.parametrize("failure", ["mock", "stale", "partial"])
def test_product_native_gate_rejects_mock_stale_or_partial_observation(monkeypatch, failure):
    from probe import preflight
    record = {"passed": True, "execution": "NATIVE_WINDOWS", "source_fingerprint": "current",
              "environment_id": preflight.native_environment_id(),
              "checks": {k: True for k in ("save", "read", "rotate", "restart", "delete", "child_environment_clean")}}
    if failure == "mock":
        record["execution"] = "MOCK_HTTP"
    elif failure == "stale":
        record["source_fingerprint"] = "old"
    else:
        record["checks"]["restart"] = False
    monkeypatch.setattr(preflight, "_validation_marker", lambda name: record if name == "windows_key" else None)
    assert preflight.productization_status("current")["windows_key"]["status"] == "NOT_VALIDATED"
