"""설정 상속과 실제 업로드·삭제·공유 검색 예약의 회귀 검사."""
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from pydantic import ValidationError
from htrsa.control_plane import ControlError, ControlStore, Defaults, ROLES
from htrsa.input_upload import Attachments
from htrsa.product_policy import resolve_effective_settings
from htrsa.database import initialize
from test_workbench import app, configure


def draft(**values):
    return {"settings_version":2,"question":"관계 검토","model_profile_id":"m","run_limit_usd":".10", **values}


@pytest.mark.parametrize("value",[0,1,5])
def test_defaults_false_zero_pdf_and_role_inheritance(app,value):
    configure(app)
    effective,origins=resolve_effective_settings(app.store,draft(search_attempt_limit=value,search_required=False,adaptive_budget=False,attachments=[],report_format="markdown"))
    assert effective["search_attempt_limit"]==value
    assert not effective["search_required"] and not effective["adaptive_budget"]
    assert effective["attachments"]==[] and effective["report_format"]=="pdf"
    assert effective["routing"]=={role:"m" for role in ROLES}
    assert effective["sampling_mode"]=="provider_default"
    assert effective["reviewer_profile_id"] is None if "reviewer_profile_id" in effective else True


@pytest.mark.parametrize("value",[-1,1.5,"0",True])
def test_search_count_rejects_non_integer(app,value):
    configure(app)
    with pytest.raises(ValidationError):
        resolve_effective_settings(app.store,draft(search_attempt_limit=value))


def test_advanced_precedence_independent_reasoning_and_low_spec(app):
    configure(app)
    app.store.put("model","m2",{**app.store.config("model","m"),"profile_id":"m2","model_id":"manual-second"})
    body=draft(performance_profile="BALANCED",advanced_performance_profile="DEEP",model_reasoning="AUTO",
        manual_role_override=True,routing={"analysis_planner_worker":"m2"})
    value=app.prepare(body)
    assert value["performance_profile"]=="DEEP"
    assert value["models"]["manager"]["reasoning_policy"]=="AUTO"
    assert value["routing"]["manager"]=="m" and value["routing"]["analysis_planner_worker"]=="m2"
    assert value["field_sources"]["performance_profile"]=="explicit advanced"
    assert value["field_sources"]["routing.analysis_planner_worker"]=="explicit role"
    assert app.store.configs("ui_preferences")==[]
    body["advanced_performance_profile"]=None
    assert app.prepare(body)["performance_profile"]=="BALANCED"


def test_durable_submission_deduplicates_and_rejects_changed_payload(app):
    configure(app)
    body=draft(submission_key="submission-test-0001")
    first=app.create(body);second=app.create(body)
    assert first["research_id"]==second["research_id"]
    assert app.store.db.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0]==1
    with pytest.raises(ControlError):
        app.create({**body,"question":"다른 질문"})


@pytest.mark.parametrize("field,value,expected",[
    ("egress","none","LOCAL_EGRESS_NOT_VALIDATED"),
    ("run_limit_usd","0","INVALID_REQUEST"),
    ("role_reasoning",{"manager":"HIGH"},"REASONING_UNSUPPORTED"),
])
def test_real_blockers_remain(app,field,value,expected):
    configure(app)
    response=app.request("POST","/api/control/research/preflight",draft(**{field:value}))
    assert response.body.get("error")==expected or expected in response.body.get("reasons",[])
    assert app.store.db.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0]==0


def upload(app,name,data,draft_id="draft-test-0001"):
    files=Attachments(app.store,app.workspace)
    item=files.begin({"filename":name,"size_bytes":len(data),"draft_id":draft_id})
    return files,files.receive(item["attachment_id"],io.BytesIO(data),len(data))


@pytest.mark.parametrize("name,data,parser",[
    ("자료.csv",b"x,y\n1,2\n","csv"),("메모.txt","한글 내용".encode("utf-8"),"txt"),
    ("읽기.md","## 자료\n한글".encode("utf-8"),"md"),("설정.json",b'{"x":[1,2]}',"json"),
    ("보관.zip",b"PK-opaque",None),("자료.pdf",b"%PDF-opaque",None)])
def test_multi_type_truthful_status_hash_delete_and_same_name(app,name,data,parser):
    files,item=upload(app,name,data)
    assert item["status"]==("READ" if parser else "OPAQUE") and item["supported_parser"]==parser
    record=files.get(item["attachment_id"])
    if parser:
        content=files.read_content(item["attachment_id"],"draft-test-0001")
        assert content["parser"]==parser
        assert content.get("text")=="한글 내용" if parser=="txt" else True
    else:
        with pytest.raises(ControlError,match="ATTACHMENT_PARSER_UNAVAILABLE"):
            files.read_content(item["attachment_id"],"draft-test-0001")
    assert files.path(record).is_file()
    deleted=files.delete(item["attachment_id"],"draft-test-0001")
    assert deleted["bytes_deleted"] and not files.path(record).exists()
    _,another=upload(app,name,data)
    assert another["attachment_id"]!=item["attachment_id"]


@pytest.mark.parametrize("name,data,code",[
    ("fake.csv",b"<html><script>active</script></html>","UPLOAD_ACTIVE_CONTENT_BLOCKED"),
    ("image.svg",b"<svg/>","UPLOAD_ACTIVE_CONTENT_BLOCKED"),
    ("program.exe",b"MZ-binary","UPLOAD_ACTIVE_CONTENT_BLOCKED"),
    ("key.txt",b"sk-qa_canary_123456789012345","SECRET_IN_SOURCE")])
def test_upload_policy_removes_blocked_bytes(app,name,data,code):
    files=Attachments(app.store,app.workspace)
    item=files.begin({"filename":name,"size_bytes":len(data),"draft_id":"draft-test-0001"})
    with pytest.raises(ControlError,match=code):
        files.receive(item["attachment_id"],io.BytesIO(data),len(data))
    record=files.get(item["attachment_id"])
    assert record["status"]=="BLOCKED" and not files.path(record).exists()
    assert files.delete(item["attachment_id"],"draft-test-0001")["bytes_deleted"]


def test_failed_parse_retention_tamper_and_owner(app):
    files,bad=upload(app,"bad.json",b"{bad")
    assert bad["status"]=="FAILED"
    assert files.delete(bad["attachment_id"],"draft-test-0001")["bytes_deleted"]
    files,item=upload(app,"shared.csv",b"x,y\n1,2\n")
    identity=item["attachment_id"]
    for invalid in [None,"other-draft-001"]:
        with pytest.raises(ControlError):
            files.delete(identity,invalid)
    files.reference([identity],"R-test")
    assert files.delete(identity,"draft-test-0001")["retained_for_provenance"]
    assert files.path(files.get(identity)).is_file()
    files.validate([identity],"draft-test-0001")
    files.path(files.get(identity)).write_bytes(b"x,y\n9,2\n")
    with pytest.raises(ControlError,match="SOURCE_HASH_MISMATCH"):
        files.validate([identity],"draft-test-0001")


def test_upload_cancel_race_cleans_bytes(app):
    files=Attachments(app.store,app.workspace)
    item=files.begin({"filename":"note.txt","size_bytes":4,"draft_id":"draft-test-0001"})
    class CancelStream:
        def read(self,_):
            files.delete(item["attachment_id"],"draft-test-0001")
            return b"note"
    with pytest.raises(ControlError,match="UPLOAD_CANCELLED"):
        files.receive(item["attachment_id"],CancelStream(),4)
    record=files.get(item["attachment_id"])
    assert record["status"]=="DELETED" and not files.path(record).exists() and not files.path(record,temporary=True).exists()


@pytest.mark.parametrize("name",["../secret.txt","C:/key.txt","a\\b.csv","\x00.csv"])
def test_filename_path_boundary(app,name):
    with pytest.raises(ControlError,match="UPLOAD_FILENAME_INVALID"):
        Attachments(app.store,app.workspace).begin({"filename":name,"size_bytes":1,"draft_id":"draft-test-0001"})


def test_shared_dispatch_counter_is_atomic_durable_and_zero_guard(app):
    rid="R-search-test"
    def dispatch(_):
        database=initialize(app.database)
        store=ControlStore(database)
        try:
            reservation=store.reserve(rid=rid,connection="search",model="search",role="literature",purpose="search",
                bound=0,run_limit=".10",request_limit=".10",monthly_limit="20",attempts=20,revision="fixture")
            try:
                count=store.dispatch_search(reservation,rid,5,"fixture")
                store.transition(reservation,"SETTLED",settled=0)
                return count["used"]
            except ControlError as exc:
                store.transition(reservation,"RELEASED")
                return exc.code
        finally:
            database.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(dispatch,range(8)))
    assert sorted(x for x in results if isinstance(x,int))==[1,2,3,4,5]
    assert results.count("SEARCH_ATTEMPT_LIMIT")==3
    assert app.store.config("search_attempts",rid)["used"]==5
    assert dispatch(9)=="SEARCH_ATTEMPT_LIMIT"
