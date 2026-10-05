"""두 저장 경계에서 실제 프로세스를 종료하고 새 프로세스로 재개한다."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/"src"),str(ROOT/"tests")]
from probe.autonomous_loop import AutonomousResearchLoop
from probe.control_plane import ROLES
from probe.providers.fake import FakeProvider
from probe.qualified_workflow import execute_profile
from probe.qualified_profiles import conclusion_card
from probe.recovery import FaultInjector, InjectedCrash
from probe.workbench import WorkbenchAPI
from test_workbench import configure


def save(path,value):
    text=json.dumps(value,ensure_ascii=False,indent=2)+"\n"
    text.encode("utf-8",errors="strict")
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding="utf-8")


def phase(folder,boundary,action):
    app=WorkbenchAPI(folder/"state.sqlite",folder/"workspace",launch=False)
    if action=="crash":
        configure(app)
        study=app.create({"beginner_mode":True,"question":"1981~2000년과 2001~2020년의 전 지구 연간 기온 편차 평균 비교","egress":"selected"})
        save(folder/"request.json",study)
    else:
        study=json.loads((folder/"request.json").read_text(encoding="utf-8"))
    provider=FakeProvider([{"decision":"PROCEED","reason":"모의 결정"}] if action=="crash" else [])
    runtime=AutonomousResearchLoop(app.read._state,provider,models={r:"manual-id" for r in ROLES})
    if action=="crash":
        runtime.faults=FaultInjector(lambda name: (_ for _ in ()).throw(InjectedCrash()) if name==boundary else None)
    try:
        asyncio.run(execute_profile(runtime,study["research_id"],study["snapshot"],
            source_text=(ROOT/"qa/qualified_profiles/public/gistemp.txt").read_text(encoding="utf-8") if action=="crash" else None))
    except InjectedCrash:
        os._exit(79)
    rid=study["research_id"];state=app.read._state
    commits=state._db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?",(rid,)).fetchone()[0]
    current=conclusion_card(state,rid)["current"]
    record={"boundary":boundary,"resume_model_calls":len(provider.calls),"commit_count":commits,"current":current,
            "fresh_process":True,"passed":not provider.calls and commits==1 and current,"execution":"OFFLINE_FAKE_AGENT_REAL_PROCESS"}
    save(folder/"resume.json",record);app.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--phase",choices=["crash","resume"])
    parser.add_argument("--folder",type=Path);parser.add_argument("--boundary");args=parser.parse_args()
    if args.phase:return phase(args.folder,args.boundary,args.phase)
    base=ROOT/"build/beginner-v4/profile-recovery"/uuid4().hex
    records=[]
    for boundary in ["profile_after_verify","profile_after_commit"]:
        folder=base/boundary
        for action in ["crash","resume"]:
            result=subprocess.run([sys.executable,"-X","utf8",str(Path(__file__)),"--phase",action,"--folder",str(folder),"--boundary",boundary],
                cwd=ROOT,capture_output=True,text=True,encoding="utf-8",timeout=90)
            expected=79 if action=="crash" else 0
            if result.returncode!=expected:raise RuntimeError(result.stderr[-2000:])
        records.append(json.loads((folder/"resume.json").read_text(encoding="utf-8")))
    save(ROOT/"qa/qualified_profiles/recovery_results.json",{"boundaries":records,"all_passed":all(r["passed"] for r in records)})
    print(json.dumps({"passed":all(r["passed"] for r in records),"boundaries":len(records),"execution":"FRESH_PROCESS_OFFLINE"}))


if __name__=="__main__":
    main()
