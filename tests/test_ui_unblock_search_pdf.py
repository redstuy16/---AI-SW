from probe.report_publication import report_root
"""실제 HTTP 재시도 상한과 PDF의 위조·오래된 상태 차단을 검사한다."""
import asyncio
from decimal import Decimal
import io
import pytest
import httpx
from probe.control_plane import ControlError,Defaults
from probe.report_pdf import render_pdf
from probe.release import ReleaseExportError,export_release
from probe.search_policy import PolicyProvider,qualified_literature,run_search
from probe.scholarly import CrossrefProvider,ScholarlyHTTPClient,SearchRequest
from probe.database import to_json
from test_workbench import app,configure
from test_qa_day1 import qa_demo_a


@pytest.mark.parametrize("cap,expected",[(0,0),(1,1),(2,2)])
def test_retry_counts_actual_transport_and_durable_exhaustion(app,cap,expected):
    configure(app)
    created=app.create({"settings_version":2,"question":"최근 문헌 관계 검토","model_profile_id":"m","run_limit_usd":".10",
        "egress":"research","public_search_query":"public relationship","search_required":True,"search_attempt_limit":cap})
    rid,snapshot=created["research_id"],created["snapshot"]
    calls=[]
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(503 if len(calls)==1 else 200,json={"message":{"items":[]}})
    client=ScholarlyHTTPClient(retries=2,transport=httpx.MockTransport(respond))
    provider=PolicyProvider(CrossrefProvider(client),app.store,app.credentials,snapshot,price=Decimal(0),price_source="offline-price")
    request=SearchRequest(research_id=rid,query="public relationship")
    if cap<2:
        with pytest.raises(ControlError,match="SEARCH_ATTEMPT_LIMIT"):
            asyncio.run(provider.search(request))
    else:
        assert asyncio.run(provider.search(request)).sources==[]
    assert len(calls)==expected
    assert client.dispatch_guard is None
    if cap:
        assert app.store.config("search_attempts",rid)["used"]==expected
    with pytest.raises(ControlError,match="SEARCH_ATTEMPT_LIMIT"):
        asyncio.run(provider.search(request))
    assert len(calls)==expected


def test_harder_global_cap_and_no_form_dispatch(app):
    configure(app)
    app.store.put("defaults","global",Defaults(search_attempt_limit=2))
    body={"question":"새 질문","model_profile_id":"m","run_limit_usd":".10","search_attempt_limit":5}
    saved=app.request("POST","/api/control/research/draft",{"draft":body,"expected_revision":0})
    assert saved.status==200
    value=app.prepare(body)
    assert value["search_attempt_limit"]==2
    assert app.store.db.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0]==0


def test_imported_verified_literature_requires_zero_search(qa_demo_a):
    _,result,state=qa_demo_a
    rid=result["research_id"]
    assert qualified_literature(state,rid)
    class Forbidden:
        name="must-not-dispatch"
        async def search(self,*_):
            raise AssertionError("검증된 문헌이 있어 검색을 보내면 안 됩니다")
    asyncio.run(run_search(state,None,None,rid,{"search_required":True,"search_attempt_limit":0},provider=Forbidden()))


def test_pdf_structure_unicode_deterministic_and_hash(qa_demo_a):
    from pypdf import PdfReader
    _,result,state=qa_demo_a
    first=render_pdf(state,result["research_id"])
    second=render_pdf(state,result["research_id"])
    assert first["data"]==second["data"] and first["sha256"]==second["sha256"]
    reader=PdfReader(io.BytesIO(first["data"]))
    text="\n".join(p.extract_text() for p in reader.pages)
    assert "결론" in text and "SHA-256" not in text and "NOT_VALIDATED" not in text
    sources=__import__("json").loads((report_root(state, result["research_id"]) / "evidence/sources.json").read_text(encoding="utf-8"))
    links=[str(item.get_object().get("/A",{}).get("/URI","")) for page in reader.pages for item in page.get("/Annots",[])]
    assert all(source["url"] in text and source["url"] in links for source in sources if source.get("url","").startswith("https://"))


@pytest.mark.parametrize("fault",["tamper","stale","secret"])
def test_pdf_current_integrity_gate(qa_demo_a,fault):
    _,result,state=qa_demo_a;rid=result["research_id"]
    path=(report_root(state, rid) / "final_report.md")
    original=path.read_bytes();version=state.state_version(rid)
    try:
        if fault=="stale":
            state._db.execute("UPDATE research_runs SET state_version=state_version+1 WHERE research_id=?",(rid,))
        elif fault=="tamper":
            path.write_bytes(original+b"\nUnverified number 999")
        if fault=="secret":
            with pytest.raises(ControlError,match="PDF_CONTENT_BLOCKED"):
                render_pdf(state,rid,protected_values=["How are temperature"])
        else:
            with pytest.raises(ReleaseExportError):
                render_pdf(state,rid)
    finally:
        path.write_bytes(original)
        state._db.execute("UPDATE research_runs SET state_version=? WHERE research_id=?",(version,rid))


def test_schema2_export_includes_real_pdf_and_manifest_hash(qa_demo_a,tmp_path):
    from probe.control_plane import ControlStore
    from hashlib import sha256
    _,result,state=qa_demo_a;rid=result["research_id"]
    store=ControlStore(state._db)
    store.db.execute("INSERT INTO control_runs(research_id,title,status,snapshot,created_at) VALUES(?,?,?,?,?)",
        (rid,"보고서 검사","COMPLETED",to_json({"settings_version":2}),"2026-10-03T00:00:00+00:00"))
    try:
        exported=export_release(state,rid,tmp_path/"export")
        data=(tmp_path/"export/report.pdf").read_bytes()
        record=next(i for i in exported["files"] if i["path"]=="report.pdf")
        assert data.startswith(b"%PDF-") and record["sha256"]==sha256(data).hexdigest()
    finally:
        state._db.execute("DELETE FROM control_runs WHERE research_id=?",(rid,))
