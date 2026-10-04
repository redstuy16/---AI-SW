"""실제 작업대에 고정 공개 자료와 모의 Agent를 연결한다. 외부 호출은 하지 않는다."""
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from htrsa.workbench import WorkbenchAPI, OwnerSession, create_server
from htrsa.final_report import export_final_report
from htrsa.qualified_workflow import execute_profile
from htrsa.schemas import utc_now
from test_beginner_v4 import prepare, SOURCE


def main():
    folder = Path(sys.argv[1])
    folder.mkdir(parents=True, exist_ok=True)
    api = WorkbenchAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    rid, snapshot, runtime, provider = prepare(api)
    asyncio.run(execute_profile(runtime, rid, snapshot, source_text=SOURCE.read_text(encoding="utf-8"),
        secondary_text=(SOURCE.parent / "gistemp.csv").read_text(encoding="utf-8")))
    api.read._state.stop_research(rid, "QUALIFIED_PROCEDURE_COMPLETED")
    export_final_report(api.read._state, rid)
    data = json.dumps({"research_id": rid, "fake_model_calls": len(provider.calls), "paid_calls": 0,
                      "execution": "OFFLINE_REAL_TOOLS_FAKE_AGENT", "created_at": utc_now().isoformat()}, ensure_ascii=False).encode("utf-8", errors="strict")
    (folder / "execution.json").write_bytes(data)
    session = OwnerSession()
    server = create_server(api, session=session)
    print("http://" + server.RequestHandlerClass.authority + "/#bootstrap=" + session.issue_bootstrap(), flush=True)
    try:
        server.serve_forever()
    finally:
        session.close()
        server.server_close()
        api.close()


if __name__ == "__main__":
    main()
