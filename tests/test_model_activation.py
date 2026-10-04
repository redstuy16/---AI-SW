"""연결 교체 시 모델 재등록과 기존 동의·가격·검증 경계를 확인한다."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from htrsa.control_plane import Connection, ControlError, ModelProfile
from htrsa import product_policy
from test_workbench import app


MODEL = "gpt-6.1-sol"


def add_connection(app, identity, *, enabled=True, approved=True):
    app.store.put("connection", identity, Connection(connection_id=identity,
        display_name=identity, adapter_id="openai", enabled=enabled,
        destination_approved=approved, credential_env_name="QA_UNUSED_KEY"))


def add_previous_profile(app):
    add_connection(app, "previous", enabled=False)
    app.store.put("model", "previous-model", ModelProfile(profile_id="previous-model",
        connection_id="previous", model_id=MODEL, protocol="responses"))
    return app.store.config("model", "previous-model")


def model_rows(app):
    return [m for m in product_policy.catalog(app.store)["models"]
            if m["provider"] == "openai" and m["model_id"] == MODEL]


def test_new_connection_can_register_existing_model_without_changing_history(app):
    previous = add_previous_profile(app)
    add_connection(app, "current")
    assert any(m["profile_id"] is None for m in model_rows(app))
    result = app.request("POST", "/api/control/catalog/resolve",
        {"provider": "openai", "model_id": MODEL, "connection_id": "current"})
    assert result.status == 200
    identity = result.body["profile_id"]
    profile = app.store.config("model", identity)
    assert profile["connection_id"] == "current" and profile["model_id"] == MODEL
    assert profile["capability_status"] == "unknown" and profile["price"] is None
    assert app.store.config("model", "previous-model") == previous
    assert not app.store.ledger()["requests"]
    repeated = app.request("POST", "/api/control/catalog/resolve",
        {"provider": "openai", "model_id": MODEL, "connection_id": "current"})
    assert repeated.body["profile_id"] == identity
    assert len(app.store.configs("model")) == 2


def test_single_registered_connection_does_not_add_duplicate_catalog_row(app):
    add_previous_profile(app)
    assert [m["profile_id"] for m in model_rows(app)] == ["previous-model"]


def test_disabled_unused_connection_does_not_offer_new_model_profile(app):
    add_previous_profile(app)
    add_connection(app, "unused", enabled=False)
    assert [m["profile_id"] for m in model_rows(app)] == ["previous-model"]


@pytest.mark.parametrize("enabled,approved", [(True, False), (False, True), (False, False)])
def test_model_resolution_still_rejects_disabled_or_unapproved_connection(app, enabled, approved):
    previous = add_previous_profile(app)
    add_connection(app, "current", enabled=enabled, approved=approved)
    with pytest.raises(ControlError, match="CONNECTION_REQUIRED"):
        product_policy.resolve_catalog_profile(app.store,
            {"provider": "openai", "model_id": MODEL, "connection_id": "current"})
    assert len(app.store.configs("model")) == 1
    assert app.store.config("model", "previous-model") == previous
    assert not app.store.ledger()["requests"]


def test_multiple_approved_connections_require_explicit_selection(app):
    add_previous_profile(app)
    add_connection(app, "current")
    add_connection(app, "another")
    with pytest.raises(ControlError, match="CONNECTION_SELECTION_REQUIRED"):
        product_policy.resolve_catalog_profile(app.store, {"provider": "openai", "model_id": MODEL})
    assert len(app.store.configs("model")) == 1


def test_connection_approval_keeps_revision_check(app):
    add_connection(app, "current", approved=False)
    previous = app.store.configs("connection")[0]
    value = {k: v for k, v in previous.items() if k != "revision"}
    value["destination_approved"] = True
    rejected = app.request("POST", "/api/control/connections", {"value": value, "expected_revision": 0})
    assert rejected.status == 409 and rejected.body["error"] == "CONFIG_STALE"
    assert app.store.configs("connection") == [previous]
    accepted = app.request("POST", "/api/control/connections", {"value": value, "expected_revision": previous["revision"]})
    assert accepted.status == 200
    assert app.store.config("connection", "current")["destination_approved"] is True
    assert not app.store.ledger()["requests"]


def test_expired_catalog_cannot_reuse_old_model_on_new_connection(app, monkeypatch):
    previous = add_previous_profile(app)
    add_connection(app, "current")
    rules = json.loads(Path(product_policy.__file__).with_name("product_catalog.json").read_text(encoding="utf-8"))
    after_expiry = datetime.fromisoformat(rules["expires_at"]) + timedelta(days=1)

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return after_expiry.astimezone(tz or timezone.utc)

    monkeypatch.setattr(product_policy, "datetime", ExpiredClock)
    with pytest.raises(ControlError, match="CATALOG_RECHECK_REQUIRED"):
        product_policy.resolve_catalog_profile(app.store,
            {"provider": "openai", "model_id": MODEL, "connection_id": "current"})
    assert len(app.store.configs("model")) == 1
    assert app.store.config("model", "previous-model") == previous
    assert not app.store.ledger()["requests"]
