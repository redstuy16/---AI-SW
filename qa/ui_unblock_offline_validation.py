"""기존 파괴적·반복 데모·새 프로세스 복구·출시 검사를 별도 결과로 실행한다."""
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"src"),str(ROOT/"qa")]
import qa_day1 as qa
import f3p_eval
from f3p_recovery_probe import BOUNDARIES
from probe.preflight import record_qa_validation,environment_status,_source_fingerprint


def save(path,value):
    def portable(item):
        if isinstance(item,str) and item.startswith((str(ROOT)+"\\",str(ROOT)+"/")):
            return Path(item).relative_to(ROOT).as_posix()
        if isinstance(item,dict):return {k:portable(v) for k,v in item.items()}
        if isinstance(item,list):return [portable(v) for v in item]
        return item
    text=json.dumps(portable(value),ensure_ascii=False,indent=2)+"\n"
    text.encode("utf-8",errors="strict");path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding="utf-8")


def run(command):
    process=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding="utf-8",timeout=180)
    if process.returncode:raise RuntimeError(process.stderr[-2000:]+process.stdout[-1000:])
    return process


def main():
    base=ROOT/"build/ui_start_unblock/offline"/uuid4().hex
    qa.QA=base/"qa";qa.QA.mkdir(parents=True)
    stress=qa.stress_suite()
    print(json.dumps({"stress":stress["pytest"],"executable":stress["all_executable_passed"]}),flush=True)
    repeat,exemplars=qa.demo_repeatability()
    fresh=base/"fresh-process"
    for phase in ("crash","resume"):
        run([sys.executable,"-X","utf8","qa/qa_day1.py","--probe",phase,"--root",str(fresh)])
    resume=json.loads((fresh/"resume.json").read_text(encoding="utf-8"))
    release=qa.validate_release(exemplars["A"])
    config=json.loads(f3p_eval.CONFIG.read_text(encoding="utf-8"))
    evaluation=base/"f3p-evaluation"
    cases=[f3p_eval.run_case(case,evaluation/"cases"/case["id"],config["dataset_seed"]) for case in config["cases"]]
    repaired=all(case["fixture_correct"] for case in cases)
    save(base/"f3p_eval_results.json",{"all_fixtures_passed":repaired,"cases":cases,"live_efficacy":"NOT_VALIDATED"})
    boundaries=[]
    for boundary in BOUNDARIES:
        folder=base/"f3p-recovery"/boundary
        for phase in ("crash","resume"):
            run([sys.executable,"-X","utf8","qa/f3p_recovery_probe.py","--phase",phase,"--folder",str(folder),"--boundary",boundary])
        boundaries.append(json.loads((folder/"resume.json").read_text(encoding="utf-8")))
    exports=[]
    for clean in (False,True):
        command=[sys.executable,"-X","utf8","qa/f3p_export_probe.py","--evaluation-dir",str(evaluation)]
        if clean:command.append("--clean")
        result=run(command)
        exports.append({"clean":clean,"result":json.loads(result.stdout)})
    record_qa_validation("stress",stress["all_executable_passed"] and resume["passed"],{"scenarios":len(stress["scenarios"]),"fresh_process_resume":resume["passed"]})
    record_qa_validation("artifact",release["passed"],{"file_count":release["file_count"]})
    value={"source_fingerprint":_source_fingerprint(),"stress":stress,"demo_repeatability":repeat,"fresh_process_resume":resume,
        "release":release,"f3p_faults":cases,"f3p_recovery":boundaries,"f3p_exports":exports,"environment":environment_status(),
        "output_dir":str(base)}
    save(ROOT/"build/ui_start_unblock/offline_results.json",value)
    passed=stress["all_executable_passed"] and all(repeat["passed"].values()) and resume["passed"] and release["passed"] and repaired and all(item["passed"] for item in boundaries)
    print(json.dumps({"passed":passed,"repeat":repeat["passed"],"resume":resume["passed"],"release":release["passed"],
        "f3p_fault_cases":len(cases),"f3p_boundaries":len(boundaries),"live":"NOT_VALIDATED"}))
    if not passed:raise SystemExit(1)


if __name__=="__main__":main()
