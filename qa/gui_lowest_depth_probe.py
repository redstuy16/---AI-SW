"""최소 연구 깊이에서도 필수 검사와 미해결 비판을 보존한다."""
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_autonomous_loop import CSV, MANAGER, shortlist, initial_coordinator, worker, first_critic
from probe.control_plane import Connection, ModelProfile, ROLES
from probe.control_runtime import execute
from probe.providers.fake import FakeProvider
from probe.workbench import WorkbenchAPI


def main():
    folder = ROOT / "build" / ("gui-lowest-" + uuid4().hex)
    app = WorkbenchAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    try:
        (app.workspace / "inputs/data.csv").write_bytes(CSV.read_bytes())
        app.store.put("connection", "fixture", Connection(connection_id="fixture", display_name="Offline fixture",
            adapter_id="openai_compatible", base_url="http://127.0.0.1:1234/v1", endpoint_class="loopback", destination_approved=True))
        app.store.put("model", "fixture", ModelProfile(profile_id="fixture", connection_id="fixture", model_id="fake",
            protocol="chat", local_api_unmetered=True, capability_status="supported"))
        rid = app.create({"title": "Lowest depth QA", "question": "Association?", "source_relative": "data.csv",
            "research_depth": "explore", "egress": "selected", "routing": {role: "fixture" for role in ROLES}})["research_id"]
        proposal = shortlist()
        proposal["hypotheses"] = proposal["hypotheses"][:1]
        def status(call):
            active = json.loads(call["input_text"])["active_state"]
            return {"hypothesis_id": active["active_hypotheses"][0]["hypothesis_id"], "new_status": "INCONCLUSIVE",
                "rationale": "Required sensitivity follow-up exceeds this lowest-depth branch limit.",
                "evidence_refs": [{"type": "evidence", "id": row["evidence_id"]} for row in active["verified_evidence"]]}
        provider = FakeProvider([MANAGER, proposal, initial_coordinator, worker("pearson_correlation"), first_critic, status,
            {"stop": True, "reason": "UNRESOLVED_VERIFICATION", "rationale": "The required follow-up remains open."}])
        app.command(rid, "start", {"expected_version": 0, "idempotency_key": "lowest-start"})
        asyncio.run(execute(app.database, app.workspace, rid, provider_factory=lambda *_: provider))
        db = app.store.db
        verifications = [json.loads(r[0]) for r in db.execute("SELECT verification_json FROM staged_mutations WHERE research_id=? AND status='COMMITTED'", (rid,))]
        assert len(verifications) == 1
        checks = verifications[0]["checks"]
        assert verifications[0]["verdict"] == "PASS" and all(c["passed"] for c in checks)
        assert any(c["check_id"] == "NUMERIC_PROVENANCE" for c in checks)
        assert db.execute("SELECT COUNT(*) FROM critic_reviews WHERE research_id=?", (rid,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM hypotheses WHERE research_id=?", (rid,)).fetchone()[0] == 1
        assert app.store.run(rid)["status"] == "VALIDATION_INCOMPLETE"
        report = {"passed": True, "research_depth": "explore", "hypotheses": 1, "verified_experiments": 1,
            "automatic_checks": len(checks), "all_required_checks_passed": True, "numeric_provenance_checked": True,
            "critic_reviews": 1, "scientific_followup_unresolved": True, "control_status": "VALIDATION_INCOMPLETE",
            "fake_provider_calls": len(provider.calls), "live_api_calls": 0, "live_efficacy": "NOT_VALIDATED"}
        data = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict")
        (ROOT / "qa/results/gui_lowest_depth_results.json").write_bytes(data)
        print(data.decode("utf-8"))
    finally:
        app.close()


if __name__ == "__main__":
    main()
