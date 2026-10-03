"""별도의 DEMO 작업 공간에 실제 오프라인 복구 기록을 만든다."""
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_verification_repair import prepare_case, corrupt_first_output
from htrsa.workbench import WorkbenchAPI
from htrsa.control_plane import Connection, ModelProfile


def main():
    root = ROOT / "build" / ("gui-visual-" + uuid4().hex)
    root.mkdir(parents=True)
    db, state, runtime, prepared = prepare_case(root)
    corrupt_first_output(state)
    asyncio.run(runtime.resume(prepared["research_id"]))
    state.stop_research(prepared["research_id"], "BUDGET_EXHAUSTED")
    from htrsa.final_report import export_final_report
    export_final_report(state, prepared["research_id"])
    db.close()
    app = WorkbenchAPI(root / "state.sqlite", root / "workspace", mode="DEMO", launch=False)
    (app.workspace / "inputs/data.csv").write_bytes((root / "pair.csv").read_bytes())
    app.create({"title": "장기 관측 자료의 온도와 생장 사이 관계 및 대안 설명을 검토하는 한국어 연구 이름 " * 3,
                "question": "긴 한국어 제목과 좁은 화면의 줄바꿈을 확인하는 실제 저장된 QA 연구", "source_relative": "data.csv"})
    app.store.put("connection", "local", Connection(connection_id="local", display_name="로컬 연구 서버 · 아직 연결 미검사", adapter_id="openai_compatible",
                    base_url="http://127.0.0.1:1234/v1", endpoint_class="loopback"))
    app.store.put("model", "local-model", ModelProfile(profile_id="local-model", connection_id="local", model_id="manual-model-id", protocol="chat"))
    app.close()
    metadata = {"database": str(root / "state.sqlite"), "workspace": str(root / "workspace"), "research_id": prepared["research_id"],
                "mode": "DEMO", "note": "Actual offline FakeProvider repair and deterministic tools; no live or paid requests."}
    path = ROOT / "build/gui_visual_fixture.json"
    path.write_bytes((json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict"))
    print(str(path))


if __name__ == "__main__":
    main()
