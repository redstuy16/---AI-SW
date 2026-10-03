"""실제 복구 보고서를 재사용하고 설정 대기 연구를 별도로 만든다. 유료 호출 없음."""
import json
from pathlib import Path

from htrsa.control_plane import Connection, ModelProfile
from htrsa.workbench import WorkbenchAPI


ROOT = Path(__file__).resolve().parents[1]
fixture = json.loads((ROOT / "build/gui_visual_fixture.json").read_text(encoding="utf-8"))
app = WorkbenchAPI(fixture["database"], fixture["workspace"], mode="DEMO", launch=False)
app.store.put("connection", "qa-gpt", Connection(connection_id="qa-gpt", display_name="GPT · 오프라인 화면 fixture",
    credential_env_name="HTRSA_QA_BROWSER_KEY", destination_approved=True))
app.store.put("model", "qa-gpt", ModelProfile(profile_id="qa-gpt", connection_id="qa-gpt", model_id="gpt-6.1-sol",
    display_name="GPT-6.1 Sol · 실제 호출 미확인", protocol="responses"))
created = app.create({"title": "화면과 설정 검증용 연구 · 유료 호출 없음", "question": "현재 검증된 측정값의 관계와 한계는?",
    "source_relative": "data.csv", "performance_profile": "BALANCED", "model_profile_id": "qa-gpt"})
fixture["controlled_research_id"] = created["research_id"]
fixture["canary_connection"] = "qa-gpt"
app.close()
data = json.dumps(fixture, ensure_ascii=False, indent=2) + "\n"
(ROOT / "build/product_visual_fixture.json").write_bytes(data.encode("utf-8", errors="strict"))
print("오프라인 화면 fixture 준비 완료")
