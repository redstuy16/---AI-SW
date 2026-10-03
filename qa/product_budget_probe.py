"""실제 Gateway·SQLite·분석 도구의 오프라인 예산 비교. 라이브 성능 측정은 아니다."""
from collections import deque
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_autonomous_loop import CSV, fake_replies
from htrsa.control_plane import Connection, ModelProfile, PriceRecord
from htrsa.control_runtime import RoutedGateway, execute
from htrsa.product_policy import completion_budget, effective_snapshot
from htrsa.research_settings import queue
from htrsa.report_ux import friendly_report
from htrsa.release import export_release
from htrsa.schemas import utc_now
from htrsa.storage import sha256_file
from htrsa.workbench import WorkbenchAPI


def probe(name, adaptive, cap="1", tighten=None):
    folder = ROOT / "build" / ("product-budget-" + name + "-" + uuid4().hex[:10])
    app = WorkbenchAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    (app.workspace / "inputs/data.csv").write_bytes(CSV.read_bytes())
    connection = Connection(connection_id="offline", display_name="오프라인 모의 제공사", adapter_id="openai_compatible",
        base_url="http://127.0.0.1:1234/v1", endpoint_class="loopback", destination_approved=True)
    app.store.put("connection", "offline", connection)
    price = PriceRecord(input_per_million=1, output_per_million=1, checked_at=utc_now(), source="오프라인 평가 단가", revision="offline-1", owner_verified=True)
    profile = ModelProfile(profile_id="offline", connection_id="offline", model_id="offline-fixture-model", protocol="chat", capability_status="supported", price=price)
    app.store.put("model", "offline", profile)
    created = app.create({"title": "오프라인 예산 검증", "question": "측정값의 관계는?", "source_relative": "data.csv",
        "performance_profile": "BALANCED", "model_profile_id": "offline", "run_limit_usd": cap, "adaptive_budget": adaptive, "egress": "selected"})
    rid, replies, calls = created["research_id"], deque(fake_replies()), []
    def status(call):
        active = json.loads(call["input_text"])["active_state"]
        evidence = active["verified_evidence"]
        return {"hypothesis_id": active["active_hypotheses"][0]["hypothesis_id"], "new_status": "SUPPORTED" if len(evidence) >= 2 else "INCONCLUSIVE",
                "rationale": "관측된 검증 근거 범위만 반영", "evidence_refs": [{"type": "evidence", "id": e["evidence_id"]} for e in evidence]}
    source_replies = list(replies); source_replies[8] = status; replies = deque(source_replies)
    initial = completion_budget(app.store, rid, created["snapshot"])
    def factory(store, rid, snapshot):
        def transport(request):
            body = json.loads(request.content)
            reply = replies.popleft()
            if callable(reply): reply = reply({"input_text": body["messages"][-1]["content"]})
            calls.append({"model": body["model"], "index": len(calls) + 1})
            if tighten and len(calls) == 5:
                queue(app.store, rid, {"expected_version": app.store.run(rid)["version"], "value": {
                    "performance_profile": "BALANCED", "adaptive_budget": adaptive, "run_limit_usd": tighten,
                    "max_elapsed_sec": 300, "egress": "selected", "max_followups": 1}})
                if adaptive:
                    replies.popleft(); replies.popleft(); replies.popleft()
            return httpx.Response(200, json={"id": "offline-" + str(len(calls)), "usage": {"prompt_tokens": 30, "completion_tokens": 10},
                "choices": [{"message": {"content": json.dumps(reply)}}]})
        return RoutedGateway(store, app.credentials, rid, snapshot, client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    app.store.db.execute("UPDATE control_runs SET status='STARTING' WHERE research_id=?", (rid,))
    asyncio.run(execute(app.database, app.workspace, rid, provider_factory=factory))
    view = friendly_report(app.read._state, rid)
    exported = export_release(app.read._state, rid, folder / "release")
    assert all(sha256_file(folder / "release" / f["path"]) == f["sha256"] for f in exported["files"])
    result = {"case": name, "adaptive": adaptive, "initial_budget": initial, "http_dispatches": len(calls),
        "status": app.store.run(rid)["status"], "stop_reason": view["stop_reason"], "complete": view["complete"],
        "verified_experiments": len(view["analyses"]), "numbers": len(view["numbers"]), "ledger": app.store.ledger(rid),
        "file_count": len(exported["files"]), "hash_mismatches": 0, "report_mode": view["mode"], "live_efficacy": "NOT_VALIDATED",
        "database": str(app.database), "workspace": str(app.workspace), "research_id": rid}
    app.close()
    return result


def main():
    results = [probe("ample-on", True), probe("tightened-on", True, tighten=".08"),
               probe("tightened-off", False, tighten=".08"), probe("mandatory-short", True, cap=".01")]
    assert results[0]["http_dispatches"] == 10 and results[0]["status"] == "COMPLETED"
    assert results[1]["http_dispatches"] == 7 and not results[1]["complete"]
    assert results[2]["http_dispatches"] == 10 and results[2]["status"] == "COMPLETED"
    assert results[3]["http_dispatches"] == 0 and results[3]["status"] == "BUDGET_BLOCKED"
    report = {"results": results, "paid_live_calls": 0, "environment": "httpx.MockTransport + 실제 Gateway/SQLite/분석 도구",
              "interpretation": "호출 수 차이는 선택 후속 분석 보류의 결과이며 정확도나 라이브 성능 향상을 증명하지 않는다.", "live_efficacy": "NOT_VALIDATED"}
    data = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    (ROOT / "qa/results/product_budget_results.json").write_bytes(data.encode("utf-8", errors="strict"))
    print(json.dumps({"cases": [{k: r[k] for k in ("case", "http_dispatches", "status", "stop_reason", "hash_mismatches")} for r in results], "paid_live_calls": 0}, ensure_ascii=True))


if __name__ == "__main__":
    main()
