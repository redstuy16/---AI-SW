"""로컬 티켓·세션·웹 경계의 실제 HTTP 검사."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import secrets
import threading

import httpx
import pytest

from probe.control_plane import ControlError
from probe.local_auth import contains_auth_material
from probe.release import _secret_free
from probe.workbench import OwnerSession, WorkbenchAPI, create_server, open_browser, redact


class ProtectedString(str):
    def __repr__(self):
        return "'[인증 테스트 값] '"


class LocalCase(dict):
    def __repr__(self):
        return "<로컬 인증 fixture>"


@pytest.fixture
def local(tmp_path):
    shared, ready = LocalCase(), threading.Event()
    def run():
        api = WorkbenchAPI(tmp_path / "state.sqlite", tmp_path / "workspace", launch=False)
        clock = [0.0]
        session = OwnerSession(clock=lambda: clock[0])
        ticket = ProtectedString(session.issue_bootstrap())
        server = create_server(api, session=session)
        shared.update(api=api, session=session, ticket=ticket, server=server, clock=clock)
        ready.set()
        try:
            server.serve_forever(poll_interval=0.01)
        finally:
            session.close()
            api.close()
    thread = threading.Thread(target=run)
    thread.start()
    assert ready.wait(10)
    shared["origin"] = "http://" + shared["server"].RequestHandlerClass.authority
    try:
        with httpx.Client(base_url=shared["origin"], trust_env=False) as client:
            shared["client"] = client
            yield shared
    finally:
        shared["server"].shutdown()
        shared["server"].server_close()
        thread.join(10)
        assert not thread.is_alive()


def exchange(local, **extra):
    headers = {"Origin": local["origin"], "X-Probe-Bootstrap": "1", **extra}
    return local["client"].post("/auth/bootstrap", json={"ticket": local["ticket"]}, headers=headers)


def test_auth_boot_01_valid_single_exchange(local):
    assert local["server"].server_address[0] == "127.0.0.1"
    assert local["server"].server_port > 0
    response = exchange(local)
    assert response.status_code == 200
    assert set(response.json()) == {"authenticated", "csrf"}
    assert not secrets.compare_digest(local["ticket"], local["session"].cookie)
    cookie = response.headers["set-cookie"]
    assert all(x in cookie for x in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=43200"))
    assert "Domain=" not in cookie and "Secure" not in cookie
    assert response.headers["cache-control"] == "no-store"
    assert local["client"].get("/api/control/research").status_code == 200


def test_auth_boot_02_replay_rejected(local):
    assert exchange(local).status_code == 200
    before = local["session"].cookie
    response = exchange(local)
    assert response.status_code == 409 and response.json()["error"] == "BOOTSTRAP_ALREADY_USED"
    assert local["session"].cookie == before


def test_auth_boot_03_expired_rejected(local):
    local["clock"][0] = 60
    response = exchange(local)
    assert response.status_code == 410 and response.json()["error"] == "BOOTSTRAP_EXPIRED"
    assert local["session"].cookie is None


@pytest.mark.parametrize("kind", ["random", "empty", "unicode", "object", "long"])
def test_auth_boot_04_invalid_ticket(local, kind):
    value = {"random": secrets.token_urlsafe(32), "empty": "", "unicode": "가" * 43, "object": {}, "long": "x" * 1000}[kind]
    response = local["client"].post("/auth/bootstrap", json={"ticket": value}, headers={"Origin": local["origin"], "X-Probe-Bootstrap": "1"})
    assert response.status_code == 403
    assert not local["session"].used


def test_auth_boot_05_no_auth_and_default_manual_disabled(local):
    assert local["client"].get("/api/control/settings").status_code == 401
    assert local["client"].get("/auth/bootstrap").status_code == 405
    assert local["client"].get("/auth/status").json() == {"manual_pairing": False}
    assert local["client"].post("/api/session", json={"pairing_code": "invalid"}, headers={"Origin": local["origin"]}).status_code == 403


@pytest.mark.parametrize("host", ["evil.example", "localhost", "127.0.0.1", "0.0.0.0:1234"])
def test_auth_boot_06_unexpected_host(local, host):
    assert exchange(local, Host=host).status_code == 403
    assert not local["session"].used


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost:8766", "http://127.0.0.1:1"])
def test_auth_boot_07_unexpected_origin(local, origin):
    assert exchange(local, Origin=origin).status_code == 403
    assert not local["session"].used


def test_auth_boot_08_cross_origin_cors(local):
    response = local["client"].options("/auth/bootstrap", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-probe-bootstrap"})
    assert response.status_code == 403
    assert not any(k.startswith("access-control-allow") for k in response.headers)


def test_auth_boot_09_10_no_logs_or_persistence(local, capsys):
    assert exchange(local).status_code == 200
    tokens = [local["ticket"], local["session"].cookie, local["session"].csrf]
    assert local["client"].get("/api/control/settings").status_code == 200
    logs = capsys.readouterr()
    assert all(t not in logs.out + logs.err for t in tokens)
    assert local["session"].ticket_digest == sha256(local["ticket"].encode("ascii")).hexdigest()
    assert not any(local["ticket"] == v for v in vars(local["session"]).values())
    assert all(t.encode("ascii") not in local["api"].database.read_bytes() for t in tokens)
    assert all(t.encode("ascii") not in p.read_bytes() for p in local["api"].workspace.rglob("*") if p.is_file() for t in tokens)


def test_auth_boot_12_restart_invalidates_context():
    old, fresh = OwnerSession(), OwnerSession()
    try:
        ticket = old.issue_bootstrap()
        old.exchange(ticket)
        header = old.cookie_name + "=" + old.cookie
        assert not fresh.authorized(header)
        with pytest.raises(ControlError, match="BOOTSTRAP_DENIED"):
            fresh.exchange(ticket)
        old.close()
        assert not old.authorized(header)
    finally:
        old.close(); fresh.close()


def test_auth_boot_13_concurrent_exchange_once():
    session = OwnerSession()
    try:
        ticket = session.issue_bootstrap()
        def call(_):
            try:
                session.exchange(ticket)
                return "PASS"
            except ControlError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(call, range(2)))
        assert sorted(outcomes) == ["BOOTSTRAP_ALREADY_USED", "PASS"]
    finally:
        session.close()


def test_auth_boot_14_multiple_instances_isolated():
    a, b = OwnerSession(), OwnerSession()
    try:
        ta, tb = a.issue_bootstrap(), b.issue_bootstrap()
        assert a.cookie_name != b.cookie_name and a.instance_id != b.instance_id
        with pytest.raises(ControlError, match="BOOTSTRAP_DENIED"):
            b.exchange(ta)
        a.exchange(ta); b.exchange(tb)
        assert not b.authorized(a.cookie_name + "=" + a.cookie)
        assert not a.authorized(b.cookie_name + "=" + b.cookie)
    finally:
        a.close(); b.close()


@pytest.mark.parametrize("route", ["/api/control/research", "/api/control/defaults", "/api/control/connections/local/credential", "/api/control/research/R-absent/pause", "/api/control/research/R-absent/export"])
def test_csrf_01_cross_origin_mutations_rejected(local, route):
    assert exchange(local).status_code == 200
    response = local["client"].post(route, json={"value": {}}, headers={"Origin": "https://evil.example", "X-CSRF-Token": local["session"].csrf})
    assert response.status_code == 403
    assert not local["session"].requests


def test_csrf_02_03_04_valid_request_and_headers(local):
    assert exchange(local).status_code == 200
    for token in (None, "invalid"):
        headers = {"Origin": local["origin"]}
        if token: headers["X-CSRF-Token"] = token
        assert local["client"].post("/api/control/defaults", json={"value": {}}, headers=headers).status_code == 403
    response = local["client"].post("/api/control/defaults", json={"value": {}}, headers={"Origin": local["origin"], "X-CSRF-Token": local["session"].csrf})
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_bootstrap_custom_header_missing_origin_and_rate_limit(local):
    assert local["client"].post("/auth/bootstrap", json={"ticket": local["ticket"]}).status_code == 403
    assert local["client"].post("/auth/bootstrap", json={"ticket": local["ticket"]}, headers={"Origin": local["origin"]}).status_code == 403
    for _ in range(5):
        assert local["client"].post("/auth/bootstrap", json={"ticket": secrets.token_urlsafe(32)}, headers={"Origin": local["origin"], "X-Probe-Bootstrap": "1"}).status_code == 403
    assert exchange(local).status_code == 429
    local["clock"][0] = 61
    assert exchange(local).status_code == 410


def test_auth_query_fragment_and_redirect_never_accepted(local):
    for path in ("/?bootstrap=" + local["ticket"], "/auth/bootstrap?ticket=" + local["ticket"], "/auth/bootstrap#" + local["ticket"]):
        response = local["client"].get(path)
        assert response.status_code in (400, 405)
        assert local["ticket"] not in response.text
    response = local["client"].post("/auth/bootstrap", json={"ticket": local["ticket"], "redirect": "https://evil.example"}, headers={"Origin": local["origin"], "X-Probe-Bootstrap": "1"})
    assert response.status_code == 403 and "location" not in response.headers


@pytest.mark.parametrize("kind", ["ticket", "cookie", "csrf"])
def test_auth_canary_redaction_export_and_owner_input(local, kind):
    assert exchange(local).status_code == 200
    value = ProtectedString(local[kind] if kind == "ticket" else getattr(local["session"], kind))
    assert contains_auth_material("prefix-" + value + "-suffix")
    assert value not in json.dumps(redact({"diagnostic": value, "trace": "#bootstrap=" + value}))
    for name, payload in (("report.md", value.encode()), ("trace.json", json.dumps({"x": value}).encode()), ("figure.png", b"\x89PNG\r\n" + value.encode())):
        assert not _secret_free(name, payload)
    response = local["client"].post("/api/control/defaults", json={"value": {"research_depth": value}}, headers={"Origin": local["origin"], "X-CSRF-Token": local["session"].csrf})
    assert response.status_code == 409 and response.json()["error"] == "SECRET_IN_CONFIG"
    assert value not in response.text


def test_session_expiry_and_ticket_not_reissued():
    clock = [0]
    session = OwnerSession(clock=lambda: clock[0])
    try:
        ticket = session.issue_bootstrap(); session.exchange(ticket)
        header = session.cookie_name + "=" + session.cookie
        assert session.authorized(header)
        clock[0] = 43200
        assert not session.authorized(header)
        with pytest.raises(ControlError, match="BOOTSTRAP_ALREADY_ISSUED"):
            session.issue_bootstrap()
    finally:
        session.close()


@pytest.mark.parametrize("result", [True, False, "error"])
def test_ux_auth_auto_open_or_clickable_fallback(capsys, result):
    session = OwnerSession()
    calls = []
    try:
        def opener(url, **options):
            calls.append((url, options))
            if result == "error": raise OSError("browser unavailable")
            return result
        opened = open_browser(session, "http://127.0.0.1:1234", opener=opener)
        assert calls[0][1] == {"new": 2, "autoraise": True}
        output = capsys.readouterr()
        if result is True:
            assert opened and "bootstrap=" not in output.out
        else:
            assert not opened and output.out.count("http://127.0.0.1:1234/#bootstrap=") == 1
        assert not output.err
    finally:
        session.close()


def test_ux_auth_headless_has_one_clickable_url(capsys):
    session = OwnerSession()
    try:
        assert not open_browser(session, "http://127.0.0.1:1234", no_browser=True, opener=lambda *_args, **_kwargs: pytest.fail("headless browser launch"))
        assert capsys.readouterr().out.count("http://127.0.0.1:1234/#bootstrap=") == 1
    finally:
        session.close()
