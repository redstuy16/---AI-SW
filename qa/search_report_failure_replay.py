"""실제 실패의 저장 원문·검색 응답을 재사용하여 수정된 전체 경로를 무과금 검증한다."""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx

from probe.ai_web_search import canonical_search_url
from probe.control_runtime import RoutedGateway, execute
from probe.database import to_json
from probe.research_report import report_inputs, report_record
from probe.report_publication import report_root
from probe.service import StateService
from probe.storage import Workspace
from probe.web_sources import checked_web_document
from probe.workbench import WorkbenchAPI

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_RID = "R-901dc68d67ba4b9391b95452441f86e8"


def run():
    original_db = sqlite3.connect((ROOT / "build/workbench/state.sqlite").as_uri() + "?mode=ro", uri=True)
    original_db.row_factory = sqlite3.Row
    try:
        original_run = original_db.execute("SELECT snapshot,status,error FROM control_runs WHERE research_id=?", (ORIGINAL_RID,)).fetchone()
        snapshot = json.loads(original_run["snapshot"])
        original_state = StateService(original_db, Workspace(ROOT / "build/workbench/workspace"))
        source = original_db.execute("SELECT * FROM sources WHERE research_id=? AND provider='openai.web_search' ORDER BY rowid", (ORIGINAL_RID,)).fetchone()
        document, _ = checked_web_document(original_state, ORIGINAL_RID, source["source_id"])
        original_html = original_state.workspace.path(ORIGINAL_RID, document["html"]["relative_path"]).read_bytes()
        url = canonical_search_url(source["url"])
        query = original_db.execute("SELECT json_extract(payload,'$.query_hash') FROM control_audit WHERE research_id=? AND kind='SEARCH_COMPLETED' ORDER BY seq LIMIT 1", (ORIGINAL_RID,)).fetchone()[0]
        cache = original_db.execute("SELECT payload FROM control_configs WHERE kind='ai_search_cache' AND id=?", (query,)).fetchone()
        search_result = json.loads(cache[0])["result"]
    finally:
        original_db.close()

    destination = ROOT / "build/search-report-replay" / datetime.now().strftime("%Y%m%d-%H%M%S")
    destination.mkdir(parents=True, exist_ok=False)
    api = WorkbenchAPI(destination / "state.sqlite", destination / "workspace", launch=False)
    try:
        for identity, connection in snapshot["connections"].items():
            api.store.put("connection", identity, connection)
        for profile in {value["profile_id"]: value for value in snapshot["models"].values()}.values():
            api.store.put("model", profile["profile_id"], profile)
        credentials = SimpleNamespace(get=lambda *_: "qa-public-replay-key", active_secrets=lambda *_: [],
            metadata=lambda *_: {"configured": True})
        api.credentials = credentials
        created = api.create({"settings_version": 2, "execution_mode": "SCIENCE_AUTO", "question": snapshot["question"],
            "model_profile_id": snapshot["models"]["manager"]["profile_id"], "egress": "research",
            "public_search_consent": True, "public_search_query": snapshot["question"], "search_policy": "ALLOWED",
            "search_required": True, "run_limit_usd": "0.1", "source_fetch_attempt_limit": 10})
        rid = created["research_id"]
        api.store.db.execute("UPDATE control_runs SET status='STARTING' WHERE research_id=?", (rid,))
        model_calls, original_calls = [], []

        def model_response(request):
            payload = json.loads(request.content)
            model_calls.append("search" if payload.get("tools") else "report")
            if payload.get("tools"):
                output = [{"type": "web_search_call", "id": "replayed-search", "status": "completed", "action": {
                    "type": "search", "queries": [snapshot["question"]], "sources": [{"type": "url", "url": item["url"]} for item in search_result["search_sources"]]}}]
                # 완료 검색의 실제 URL 목록과 저장 HTML을 사용한다. 문맥 캐시 토큰은 오류 재현용이다.
                output.append({"type": "message", "content": [{"type": "output_text", "text": "Verified public source", "annotations": [
                    {"type": "url_citation", "url": url, "title": source["title"], "start_index": 0, "end_index": 22}]}]})
                return httpx.Response(200, json={"id": "qa-search", "model": payload["model"], "status": "completed", "output": output,
                    "usage": {"input_tokens": 12838, "output_tokens": 933, "input_tokens_details": {"cached_tokens": 1024}, "cache_write_tokens": 256}})
            evidence = report_inputs(api.read._state, rid)["evidence"][0]
            quote = " ".join(evidence["evidence_text"].split()[:20])
            decision = {"action": "COMPLETE", "rationale": "검증한 대학 원문을 바탕으로 조건과 원리를 설명합니다.", "research_question": snapshot["question"],
                "report_draft": {"report_type": "literature", "summary": "고무줄의 온도별 탄성은 분자 사슬의 엔트로피와 측정 조건을 함께 고려해 해석해야 한다.",
                    "purpose": "온도에 따른 고무줄의 탄성 변화를 설명하고, 길이를 고정한 경우와 하중을 고정한 경우의 해석 차이를 검토한다.",
                    "explanation": "고무의 분자 사슬은 구부러진 여러 배치를 취할 수 있다. 고무줄을 늘리면 가능한 배치가 줄어들며, 사슬은 가능한 배치가 많은 상태로 돌아가려 한다. 온도 변화와 복원력을 연결할 때에는 내부 에너지와 엔트로피를 함께 고려해야 한다. 대학의 공개 수업 자료는 온도별 측정과 열역학 관계를 사용해 고무줄을 분석하는 방법을 제시한다.",
                    "method": "검색으로 발견한 대학 수업 원문을 확보하고 고무줄, 온도, 장력에 관한 문단을 확인했다. 원문의 설명과 측정 조건을 비교했으며 직접 가열 실험이나 장력 측정을 수행하지 않았다.",
                    "results": "확인한 자료는 고무줄의 온도별 장력과 길이를 다루는 수업용 측정 활동이다. 이를 바탕으로 탄성의 온도 의존성을 검토할 수 있으나, 이 탐구에서 얻은 실측 결과는 없다. 길이를 고정하면 필요한 장력의 변화를, 하중을 고정하면 길이의 변화를 비교해야 한다. 서로 다른 통제 조건의 결과를 같은 의미로 해석할 수 없으며, 재료의 조성이나 변형 범위도 결과에 영향을 줄 수 있다.",
                    "conclusion": "고무줄의 온도별 탄성 변화는 분자 사슬의 배치와 열역학적 조건을 함께 고려해야 한다. 확보한 원문은 이 분석 방법을 뒷받침하지만, 해당 고무줄의 변화량이나 전체 온도 범위의 경향은 직접 측정 없이는 확정할 수 없다.",
                    "claims": [{"text": "대학 원문은 온도별 측정을 이용해 고무줄의 내부 에너지와 엔트로피 변화를 검토하는 방법을 제시한다.", "evidence_id": evidence["evidence_id"], "quote": quote}]}}
            text = to_json(decision)
            return httpx.Response(200, json={"id": "qa-report", "model": payload["model"], "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                "usage": {"input_tokens": 6000, "output_tokens": 3000, "input_tokens_details": {"cached_tokens": 1024}}})

        def original_response(request):
            original_calls.append(str(request.url))
            return (httpx.Response(200, content=original_html, headers={"Content-Type": "text/html; charset=utf-8"})
                if canonical_search_url(str(request.url)) == url else httpx.Response(403))

        import probe.source_documents as documents
        document_class = documents.PublicDocumentClient
        documents.PublicDocumentClient = lambda: document_class(transport=httpx.MockTransport(original_response))
        try:
            asyncio.run(execute(api.database, api.workspace, rid, provider_factory=lambda store, identity, effective:
                RoutedGateway(store, credentials, identity, effective,
                    client_factory=lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(model_response)))))
        finally:
            documents.PublicDocumentClient = document_class
        run_record = api.store.run(rid)
        report = report_record(api.read._state, rid)
        published = report_root(api.read._state, rid)
        assert run_record["status"] == "COMPLETED", run_record
        assert report["status"] == "READY" and report["current"] and len(report["draft"]["claims"]) >= 1
        assert model_calls == ["search", "report"]
        assert len(original_calls) <= 10
        assert not api.store.db.execute("SELECT 1 FROM spend_ledger WHERE research_id=? AND status!='SETTLED'", (rid,)).fetchone()
        assert "FATAL_ERROR" not in (published / "final_report.md").read_text(encoding="utf-8", errors="strict")
        from pypdf import PdfReader
        pdf = published / "report.pdf"
        pages = PdfReader(pdf).pages
        assert pages and "FATAL_ERROR" not in "".join(page.extract_text() for page in pages)
        result = {"original_research_id": ORIGINAL_RID, "original_record_changed": False, "research_id": rid,
            "validation_mode": "저장된 실제 검색·대학 HTML과 모의 AI 응답을 사용한 무과금 재현", "paid_calls": 0,
            "status": run_record["status"], "report_status": report["status"], "verified_evidence_count": len(report["source_ids"]),
            "model_routes": model_calls, "original_fetch_attempts": len(original_calls), "budget": api.store.ledger(rid),
            "pdf_pages": len(pages), "report_pdf": str(pdf), "report_markdown": str(published / "final_report.md")}
        encoded = (to_json(result) + "\n").encode("utf-8", errors="strict")
        (destination / "validation.json").write_bytes(encoded)
        print(json.dumps({key: value for key, value in result.items() if key != "budget"}, ensure_ascii=True))
    finally:
        api.close()


if __name__ == "__main__":
    run()
