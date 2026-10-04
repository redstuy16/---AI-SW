"""현재 출처 검증을 통과한 네 종류의 PDF를 생성하고 구조·한글을 검사한다."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import json
from hashlib import sha256

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"src"),str(ROOT/"qa")]
from htrsa.database import initialize
from htrsa.demo import run_demo_a,run_demo_b
from htrsa.final_report import export_final_report
from htrsa.report_pdf import render_pdf
from htrsa.service import StateService
from htrsa.storage import Workspace
from f3p_eval import prepare
from pypdf import PdfReader
import pypdfium2 as pdfium


def main():
    from uuid import uuid4
    folder=ROOT/"build/ui_start_unblock/pdf_checks"/uuid4().hex
    folder.mkdir(parents=True)
    demo=folder/"demo"
    a=run_demo_a(demo/"state.sqlite",demo/"workspace")
    b=run_demo_b(demo/"state.sqlite",demo/"workspace")
    database=initialize(demo/"state.sqlite")
    state=StateService(database,Workspace(demo/"workspace"))
    partial=state.create_research("긴 한글 연구 질문과 측정 단위의 검토 "+("광역 관측 지점의 평균 농도 및 기준 범위 " * 22))
    state.configure_budget(partial,0.1,0.1,0.1)
    state.stop_research(partial,"INSUFFICIENT_DATA")
    export_final_report(state,partial)
    records=[("normal",state,a["research_id"]),("partial",state,partial)]
    db3,state3,healthy,healthy_prepared,_=prepare(folder/"inconclusive")
    assert asyncio.run(healthy.resume(healthy_prepared["research_id"]))["verdict"]=="PASS"
    state3.stop_research(healthy_prepared["research_id"],"INSUFFICIENT_DATA")
    export_final_report(state3,healthy_prepared["research_id"])
    from htrsa.report_ux import friendly_report
    assert friendly_report(state3,healthy_prepared["research_id"])["support_level"]=="INCONCLUSIVE"
    records.insert(1,("inconclusive",state3,healthy_prepared["research_id"]))
    db2,state2,agent,prepared,_=prepare(folder/"repaired")
    original=state2.stage
    injected=[False]
    def fault(payload):
        if not injected[0]:
            injected[0]=True
            payload.agent_result.output=deepcopy(payload.agent_result.output)
            payload.agent_result.output["metrics"]["estimate"]=-.2
        return original(payload)
    state2.stage=fault
    assert asyncio.run(agent.resume(prepared["research_id"]))["verdict"]=="PASS"
    state2.stop_research(prepared["research_id"],"BUDGET_EXHAUSTED")
    export_final_report(state2,prepared["research_id"])
    records.append(("repaired",state2,prepared["research_id"]))
    result=[]
    for kind,current,rid in records:
        output=render_pdf(current,rid)
        target=folder/(kind+".pdf")
        target.write_bytes(output["data"])
        reader=PdfReader(target)
        extracted="\n".join(page.extract_text() for page in reader.pages)
        assert "연구 결과" in extracted and "핵심 결론" in extracted and "SHA-256" in extracted
        assert "%PDF-"==output["data"][:5].decode("ascii")
        assert b"/EmbeddedFile" not in output["data"] and b"/JavaScript" not in output["data"]
        fonts=[font.get_object() for page in reader.pages for font in page["/Resources"]["/Font"].get_object().values()]
        assert any("/ToUnicode" in font for font in fonts)
        document=pdfium.PdfDocument(target)
        page_indices=range(len(document))
        images=[]
        for index in page_indices:
            png=folder/(kind+f"-page-{index+1}.png")
            document[index].render(scale=1.5).to_pil().save(png)
            images.append(png.relative_to(ROOT).as_posix())
        result.append({"kind":kind,"support_level":friendly_report(current,rid)["support_level"],"path":target.relative_to(ROOT).as_posix(),"pages":len(reader.pages),
            "sha256":sha256(output["data"]).hexdigest(),"state_version":output["state_version"],"korean_text":True,
            "embedded_unicode_font":True,"rendered_pages":images,"visual_review":"PENDING"})
    db2.close();db3.close();database.close()
    text=json.dumps(result,ensure_ascii=False,indent=2)+"\n";text.encode("utf-8",errors="strict")
    (ROOT/"build/ui_start_unblock/pdf_results.json").write_text(text,encoding="utf-8")
    print(text)


if __name__=="__main__":main()
