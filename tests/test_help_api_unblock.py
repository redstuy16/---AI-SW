"""도움말 정리와 공식 단가 적용이 호출·예산·정산 경계를 유지하는지 검사한다."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import pytest

from htrsa import product_policy
from htrsa.control_plane import Connection, ControlError, ModelProfile, PriceRecord
from htrsa.provider_checks import check_model
from htrsa.schemas import utc_now
from htrsa.tutorial_guide import TutorialProgress, tutorial_catalog, render_tutorial_guide
from test_multi_provider import document
from test_workbench import app


def setup_model(app, monkeypatch, model_id="gpt-6-luna"):
    monkeypatch.setenv("QA_PRICE_API_KEY", "qa-price-key-canary-0123456789")
    app.store.put("connection", "openai-price", Connection(connection_id="openai-price", display_name="검사용 OpenAI",
        adapter_id="openai", destination_approved=True, credential_env_name="QA_PRICE_API_KEY"))
    response = app.request("POST", "/api/control/catalog/resolve",
        {"provider": "openai", "model_id": model_id, "connection_id": "openai-price"})
    assert response.status == 200
    return response.body["profile_id"]


def quote(app, identity):
    response = app.request("GET", f"/api/control/models/{identity}/pricing")
    assert response.status == 200
    return response.body


def approve(app, identity, value):
    return app.request("POST", f"/api/control/models/{identity}/pricing",
        {"approve_price": True, "expected_revision": value["expected_revision"], "quote_id": value["quote_id"]})


def test_registered_key_missing_price_reproduces_then_explicit_apply_unblocks(app, monkeypatch):
    identity = setup_model(app, monkeypatch)
    calls = []
    factory = lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json=document("responses"))))
    with pytest.raises(ControlError, match="PRICE_REQUIRED"):
        asyncio.run(check_model(app, identity, {"mode": "text", "consent": True, "budget_cap_usd": ".10"}, client_factory=factory))
    assert not calls and app.store.ledger()["requests"] == []
    before = app.store.db.total_changes
    value = quote(app, identity)
    assert value["required"] and value["candidate"]["input_per_million"] == "0.1"
    assert value["candidate"]["output_per_million"] == "0.5"
    assert value["candidate"]["owner_verified"] is False
    assert app.store.db.total_changes == before
    response = approve(app, identity, value)
    assert response.status == 200 and response.body["paid_calls"] == 0
    assert not calls and not app.store.ledger()["requests"]
    result = asyncio.run(check_model(app, identity, {"mode": "text", "consent": True, "budget_cap_usd": ".10"}, client_factory=factory))
    assert result["status"] == "LIVE_CAPABILITY_TEST_PASSED" and len(calls) == 1
    ledger = app.store.ledger()
    assert ledger["spent"] == "0.00001" and ledger["requests"][0]["status"] == "SETTLED"
    assert app.store.db.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0
    assert app.store.db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == 0
    assert "qa-price-key-canary" not in json.dumps(value) and b"qa-price-key-canary" not in app.database.read_bytes()


@pytest.mark.parametrize("fault", ["no_consent", "wrong_quote", "old_revision"])
def test_price_approval_requires_consent_current_revision_and_quote(app, monkeypatch, fault):
    identity = setup_model(app, monkeypatch)
    value = quote(app, identity)
    body = {"approve_price": True, "expected_revision": value["expected_revision"], "quote_id": value["quote_id"]}
    if fault == "no_consent":
        body["approve_price"] = False
    elif fault == "wrong_quote":
        body["quote_id"] = "f" * 64
    else:
        body["expected_revision"] -= 1
    response = app.request("POST", f"/api/control/models/{identity}/pricing", body)
    assert response.status >= 400
    assert app.store.config("model", identity)["price"] is None and not app.store.ledger()["requests"]


@pytest.mark.parametrize("fault", ["expired_catalog", "old_price", "unknown_model", "proxy"])
def test_unknown_or_stale_prices_never_gain_approval(app, monkeypatch, fault, tmp_path):
    identity = setup_model(app, monkeypatch)
    if fault in {"expired_catalog", "old_price"}:
        rules = json.loads(Path(product_policy.__file__).with_name("product_catalog.json").read_text(encoding="utf-8"))
        rules["expires_at" if fault == "expired_catalog" else "checked_at"] = (
            datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        folder = tmp_path / "rules"; folder.mkdir()
        text = json.dumps(rules, ensure_ascii=False)
        (folder / "product_catalog.json").write_bytes(text.encode("utf-8", errors="strict"))
        monkeypatch.setattr(product_policy, "__file__", str(folder / "product_policy.py"))
    else:
        profile = app.store.config("model", identity)
        revision = next(m["revision"] for m in app.store.configs("model") if m["profile_id"] == identity)
        if fault == "unknown_model":
            profile["model_id"] = "unknown-price-model"
        else:
            app.store.put("connection", "proxy", Connection(connection_id="proxy", display_name="개별 서버",
                adapter_id="openai_compatible", base_url="http://127.0.0.1:1234/v1",
                endpoint_class="loopback", destination_approved=True))
            profile["connection_id"] = "proxy"
        app.store.put("model", identity, ModelProfile.model_validate(profile), revision)
    value = quote(app, identity)
    assert value["required"] and value["candidate"] is None
    assert approve(app, identity, value).status >= 400
    assert not app.store.ledger()["requests"]


def test_manual_valid_price_is_preserved_and_old_quote_cannot_replace_it(app, monkeypatch):
    identity = setup_model(app, monkeypatch)
    old = quote(app, identity)
    profile = ModelProfile.model_validate(app.store.config("model", identity))
    profile.price = PriceRecord(input_per_million="1.5", output_per_million="3",
        source="소유자 확인", checked_at=utc_now(), revision="manual-price", owner_verified=True)
    app.store.put("model", identity, profile, old["expected_revision"])
    current = deepcopy(app.store.config("model", identity))
    assert not quote(app, identity)["required"]
    assert approve(app, identity, old).body["error"] == "CONFIG_STALE"
    assert app.store.config("model", identity) == current


@pytest.mark.parametrize("cap", ["0", "-1", "NaN", "1", ".000001"])
def test_price_fix_does_not_bypass_smoke_budget(app, monkeypatch, cap):
    identity = setup_model(app, monkeypatch)
    assert approve(app, identity, quote(app, identity)).status == 200
    calls = []
    factory = lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json=document("responses"))))
    with pytest.raises(ControlError):
        asyncio.run(check_model(app, identity, {"mode": "text", "consent": True, "budget_cap_usd": cap}, client_factory=factory))
    assert not calls


def test_help_has_no_free_example_and_legacy_learning_state_migrates():
    catalog = tutorial_catalog()
    guide = render_tutorial_guide()
    assert "examples" not in catalog and "무료 예시" not in guide and "학습용 예시" not in guide
    assert TutorialProgress(mode="EXAMPLE").mode == "GUIDED"
    root = Path(__file__).resolve().parents[1] / "src/htrsa/workbench_static"
    source = (root / "tutorial.js").read_text(encoding="utf-8")
    assert "guide-example" not in source and "showExample" not in source and "learning-example" not in source
    assert "9개 과정" not in source and "사용 안내 검색" not in source
    assert "상세 검사" not in (root / "workbench.js").read_text(encoding="utf-8")
    assert len(catalog["topics"]) == 41


def research_request(identity):
    return {"question": "공개 자료의 관계를 검토해 주세요.", "model_profile_id": identity,
            "selected_model_pool": [identity], "settings_version": 2, "run_limit_usd": ".10",
            "egress": "selected", "question_only": True, "research_profile_mode": "AUTO"}


def test_research_reloads_price_after_connection_check_and_can_start(app, monkeypatch):
    identity = setup_model(app, monkeypatch)
    body = research_request(identity)
    preflight = lambda: app.request("POST", "/api/control/research/preflight", body).body
    blocked = preflight()
    assert blocked["first_blocker"] == "PRICE_REQUIRED"
    assert blocked["price_required_profiles"] == [identity]
    entry = lambda: next(m for m in product_policy.catalog(app.store)["models"] if m.get("profile_id") == identity)
    assert not entry()["operational"]
    assert approve(app, identity, quote(app, identity)).status == 200
    factory = lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=document("responses"))))
    asyncio.run(check_model(app, identity, {"mode": "text", "consent": True, "budget_cap_usd": ".10"}, client_factory=factory))
    assert entry()["operational"]
    assert app.store.config("model", identity)["capability_status"] == "unknown"
    current = preflight()
    assert current["ready"] and current["price_required_profiles"] == []
    created = app.request("POST", "/api/control/research", body)
    assert created.status == 201 and created.body["preflight"]["ready"]
    rid = created.body["research_id"]
    start = app.request("POST", f"/api/control/research/{rid}/start",
        {"idempotency_key": "price-repaired-start", "expected_version": 0})
    assert start.status == 200 and start.body["status"] == "STARTING"
    assert len(app.store.ledger()["requests"]) == 1


def test_price_guidance_identifies_other_role_model_without_weakening_gate(app, monkeypatch):
    identity = setup_model(app, monkeypatch)
    assert approve(app, identity, quote(app, identity)).status == 200
    raw = app.store.config("model", identity)
    other = ModelProfile.model_validate(raw).model_copy(update={"profile_id": "other-role", "price": None})
    app.store.put("model", other.profile_id, other)
    body = research_request(identity) | {"manual_role_override": True,
        "routing": {"verification_coordinator": other.profile_id}, "selected_model_pool": [identity, other.profile_id]}
    result = app.request("POST", "/api/control/research/preflight", body)
    assert result.status == 200
    assert result.body["price_required_profiles"] == [other.profile_id]
    assert not result.body["ready"] and "PRICE_REQUIRED" in result.body["reasons"]
    assert not app.store.ledger()["requests"]
