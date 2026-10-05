"""연결·키 동시 등록과 제공사별 모델 선택의 저장 경계를 확인한다."""
import json

import pytest

from probe.control_plane import ControlError, Credentials
from probe.product_policy import catalog
from probe.providers.native import DEFINITIONS
from test_multi_provider import app as base_app


@pytest.fixture
def app(base_app, tmp_path):
    base_app.credentials = Credentials(tmp_path / "repository", base_app.workspace, tmp_path / "private/key.env")
    yield base_app


class KeyStore:
    available = True

    def __init__(self, fail=False):
        self.values = {}
        self.writes = 0
        self.fail = fail

    def read(self, name):
        return self.values.get(name), None

    def write(self, name, value):
        self.writes += 1
        if self.fail:
            raise ControlError("SECRET_READBACK_FAILED")
        if value is None:
            self.values.pop(name, None)
        else:
            self.values[name] = value


def register(app, provider="openai", **changes):
    value = {"connection_id": "connection-test", "display_name": "검사 연결", "adapter_id": provider,
             "credential_env_name": "PROBE_QA_REGISTRATION", "destination_approved": True}
    value.update(changes.pop("value", {}))
    return app.request("POST", "/api/control/connections/register", {
        "value": value, "api_key": "registration-canary-not-a-real-key-987654321", **changes})


@pytest.mark.parametrize("provider", [p for p in DEFINITIONS if p != "openai_compatible"])
def test_registration_saves_key_without_network_or_db_secret(app, monkeypatch, provider):
    monkeypatch.delenv("PROBE_QA_REGISTRATION", raising=False)
    store = app.credentials.os_store = KeyStore()
    response = register(app, provider)
    assert response.status == 200 and response.body["credential"]["saved"]
    conn = app.store.config("connection", "connection-test")
    assert conn["base_url"] == DEFINITIONS[provider]["base_url"]
    assert app.credentials.get("PROBE_QA_REGISTRATION") == store.values["PROBE_QA_REGISTRATION"]
    assert store.values["PROBE_QA_REGISTRATION"] not in json.dumps(response.body)
    assert store.values["PROBE_QA_REGISTRATION"].encode() not in app.database.read_bytes()
    assert app.store.db.execute("SELECT count(*) FROM spend_ledger").fetchone()[0] == 0


def test_save_failure_rolls_back_connection_and_audit(app):
    app.credentials.os_store = KeyStore(fail=True)
    response = register(app)
    assert response.status == 409 and response.body["error"] == "SECRET_READBACK_FAILED"
    assert app.store.configs("connection") == []
    assert app.store.configs("credential_change") == []
    assert app.store.db.execute("SELECT count(*) FROM control_audit").fetchone()[0] == 0


def test_stale_registration_never_changes_key(app):
    store = app.credentials.os_store = KeyStore()
    assert register(app).status == 200
    first = dict(store.values)
    response = register(app, api_key="different-private-canary-123456789")
    assert response.status == 409 and response.body["error"] == "CONFIG_STALE"
    assert store.writes == 1 and store.values == first


@pytest.mark.parametrize("case", ["extra", "metadata", "invalid", "url"])
def test_registration_rejects_invalid_or_leaking_values(app, case):
    store = app.credentials.os_store = KeyStore()
    changes = {"extra": {"unexpected": "value"},
               "metadata": {"value": {"display_name": "registration-canary-not-a-real-key-987654321"}},
               "invalid": {"api_key": "key\nINJECT=value"},
               "url": {"value": {"base_url": "https://evil.example"}}}[case]
    response = register(app, **changes)
    assert response.status in (400, 409)
    assert app.store.configs("connection") == [] and store.writes == 0


def test_local_key_delete_removes_saved_key_metadata(app, monkeypatch):
    monkeypatch.delenv("PROBE_QA_REGISTRATION", raising=False)
    app.credentials.os_store = KeyStore()
    assert register(app).status == 200
    assert app.connections()[0]["credential"]["saved"] is True
    response = app.request("POST", "/api/control/connections/connection-test/credential", {"delete": True})
    assert response.status == 200 and response.body["saved"] is False
    assert app.connections()[0]["credential"]["saved"] is False
    assert app.store.config("connection", "connection-test")


@pytest.mark.parametrize("provider", [p for p in DEFINITIONS if p != "openai_compatible"])
def test_provider_catalog_selection_preserves_unvalidated_status(app, provider):
    entry = next(m for m in catalog(app.store)["models"] if m["provider"] == provider)
    response = app.request("POST", "/api/control/catalog/select", {
        "provider": provider, "model_id": entry["model_id"], "approve_destination": True, "approve_price": True})
    assert response.status == 201 and response.body["status"] == "UNTESTED"
    model = app.store.config("model", response.body["profile_id"])
    connection = app.store.config("connection", model["connection_id"])
    assert connection["adapter_id"] == provider and model["capability_status"] == "unknown"
    assert app.store.db.execute("SELECT count(*) FROM spend_ledger").fetchone()[0] == 0
    if not entry.get("price_candidate"):
        assert model["price"] is None


def test_catalog_connection_provider_mismatch_is_blocked(app):
    app.credentials.os_store = KeyStore()
    assert register(app, "anthropic").status == 200
    response = app.request("POST", "/api/control/catalog/select", {"provider": "openai", "model_id": "gpt-6.1-sol",
        "connection_id": "connection-test", "approve_destination": True, "approve_price": True})
    assert response.status == 409 and response.body["error"] == "CATALOG_CONNECTION_MISMATCH"
    assert app.store.configs("model") == []


def test_curated_catalog_has_five_featured_and_six_providers(app):
    rows = catalog(app.store)["models"]
    featured = sorted((m for m in rows if m["featured_order"]), key=lambda m: m["featured_order"])
    assert len(featured) == 5 and [m["featured_order"] for m in featured] == [1, 2, 3, 4, 5]
    assert len({m["provider"] for m in rows}) == 6
    assert not any(m["model_id"].startswith(("gpt-4", "gemini-2", "claude-3")) for m in rows)


def test_file_readback_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    from probe.control_plane import Credentials
    credentials = Credentials(tmp_path / "repo", tmp_path / "workspace", tmp_path / "private/key.env")
    monkeypatch.setattr("probe.control_plane.os.replace", lambda *_a: None)
    with pytest.raises(ControlError, match="SECRET_READBACK_FAILED"):
        credentials.save("QA_KEY", "file-canary-not-a-real-key-987654321")


def test_catalog_does_not_invent_deepseek_schema_support(app):
    rows = catalog(app.store)["models"]
    deepseek = [m for m in rows if m["provider"] == "deepseek"]
    assert deepseek and all("structured_output" not in m["capabilities"] for m in deepseek)
    assert all(m["capabilities"]["json_mode"]["source"] == "STATIC_DOCS" for m in deepseek)
    assert all(not m["operational"] for m in rows)
