"""연구별 비용 표시가 원장·페이지·복구 경계를 유지하는지 검사한다."""
from decimal import Decimal

from htrsa.workbench import WorkbenchAPI
from test_workbench import app, configure, create


def reserve(app, rid, *, role="manager", amount=".02", connection="cost-fixture"):
    return app.store.reserve(rid=rid, connection=connection, model="offline-cost-model",
        role=role, purpose="web_search" if role == "search" else "analysis",
        bound=amount, run_limit="1", monthly_limit="10", request_limit="1",
        attempts=50, revision="offline-cost-v1")


def settle(app, rid, *, amount=".02", role="manager"):
    identity = reserve(app, rid, role=role)
    app.store.transition(identity, "DISPATCHED")
    app.store.transition(identity, "SETTLED", settled=amount)
    return identity


def listed(app):
    response = app.request("GET", "/api/control/research?limit=50")
    assert response.status == 200
    return {r["research_id"]: r for r in response.body["items"]}


def test_costs_use_ledger_include_search_and_separate_researches(app):
    configure(app)
    first, second = create(app), create(app)
    settle(app, first, amount=".000001")
    settle(app, first, amount=".01", role="search")
    settle(app, second, amount=".015")
    reserve(app, first, connection="pending-fixture", amount=".005")
    pending = reserve(app, first)
    app.store.transition(pending, "DISPATCHED")
    app.store.transition(pending, "UNRESOLVED")
    before = app.store.db.total_changes
    rows = listed(app)
    assert rows[first]["cost"] == {"basis": "LEDGER", "spent": "0.010001",
                                  "reserved": "0.005", "unresolved": "0.02"}
    assert rows[second]["cost"]["spent"] == "0.015"
    assert app.store.db.total_changes == before
    assert app.store.db.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    for rid in [first, second]:
        usage = app.request("GET", f"/api/control/research/{rid}/usage?limit=1").body
        assert all(usage[key] == rows[rid]["cost"][key] for key in ["spent", "reserved", "unresolved"])


def test_cancelled_reservation_and_empty_research_do_not_count_as_spent(app):
    configure(app)
    rid = create(app)
    assert listed(app)[rid]["cost"]["spent"] == "0"
    identity = reserve(app, rid)
    app.store.transition(identity, "RELEASED")
    cost = listed(app)[rid]["cost"]
    assert all(cost[key] == "0" for key in ["spent", "reserved", "unresolved"])


def test_usage_pagination_keeps_page_size_and_totals_under_filters(app):
    configure(app)
    rid = create(app)
    for i in range(4):
        settle(app, rid, amount=".001", role="search" if i % 2 else "manager")
    pages = [app.request("GET", f"/api/control/research/{rid}/usage?limit=2&offset={offset}").body for offset in [0, 2]]
    assert [p["limit"] for p in pages] == [2, 2]
    assert [p["next_offset"] for p in pages] == [2, None]
    assert len({r["id"] for p in pages for r in p["requests"]}) == 4
    filtered = app.request("GET", f"/api/control/research/{rid}/usage?limit=2&role=search").body
    assert len(filtered["requests"]) == 2 and filtered["spent"] == "0.004"
    assert all(r["role"] == "search" for r in filtered["requests"])
    assert all(p["spent"] == filtered["spent"] for p in pages)


def test_cost_history_survives_restart_and_recovery_preserves_exposure(app):
    configure(app)
    rid = create(app)
    settle(app, rid, amount=".004")
    pending = reserve(app, rid)
    app.store.transition(pending, "DISPATCHED")
    app.store.db.execute("UPDATE control_runs SET status='RUNNING',pid=NULL WHERE research_id=?", (rid,))
    database, workspace = app.database, app.workspace
    app.close()
    reopened = WorkbenchAPI(database, workspace, launch=False)
    try:
        cost = listed(reopened)[rid]["cost"]
        assert cost["spent"] == "0.004" and cost["unresolved"] == "0.02"
        assert cost["reserved"] == "0"
        usage = reopened.store.ledger(rid)
        assert Decimal(usage["spent"]) + Decimal(usage["unresolved"]) == Decimal(".024")
        assert len(usage["requests"]) == 2
    finally:
        reopened.close()
