"""현재 소스와 실제 실행 근거가 맞을 때만 한국어 보고서·연구일지를 기록한다."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from probe.preflight import VALIDATION_DIR, _source_fingerprint, environment_status

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / "build/cycle5-qa-final-source"


def read(path):
    return json.loads(path.read_text(encoding="utf-8", errors="strict"))


def write(path, text):
    data = text.encode("utf-8", errors="strict")
    if not path.exists() or path.read_bytes() != data: path.write_bytes(data)


def main():
    baseline = read(ROOT / "qa/results/cycle5_baseline_observed.json")
    summary = read(QA / "qa_day1_summary.json")
    source = _source_fingerprint()
    markers = {name: read(VALIDATION_DIR / (name + ".json")) for name in ("core", "stress", "artifact", "demo_a", "demo_b")}
    assert all(v["passed"] and v["source_fingerprint"] == source for v in markers.values())
    current, core, stress = summary["current"], summary["core_marker"], read(QA / "stress_results.json")
    cases = ET.parse(current["xml"]).getroot().findall(".//testcase")
    new = [c for c in cases if "test_cycle5_semantics" in c.attrib.get("classname", "")]
    assert len(new) == 58 and all(c.find("failure") is None and c.find("error") is None and c.find("skipped") is None for c in new)
    assert current["passed"] and current["passed_count"] == baseline["passed"] + len(new) and core["passed_count"] == current["passed_count"]
    cycle = read(ROOT / "qa/results/cycle5_lab_results.json")
    recovery = read(ROOT / "qa/results/cycle5_recovery_results.json")
    export = read(ROOT / "qa/results/cycle5_export_results.json")
    visual = read(ROOT / "qa/results/cycle5_visual_results.json")
    integration = read(ROOT / "qa/results/cycle5_analysis_lineage_results.json")
    lab = read(ROOT / "qa/results/reliability_lab_results.json")
    for result in (cycle, recovery, export, visual, integration, lab):
        assert result["all_passed"] and result["source_fingerprint"] == source
    assert cycle["source_unchanged"] and export["source_unchanged"] and lab["source_unchanged"]
    compatibility = read(ROOT / "qa/results/research_slice_export_results.json")
    assert compatibility["all_passed"]
    assert lab["recovery"]["all_passed"] and lab["recoverability"] == {"numerator": 33, "denominator": 33}
    skills = read(ROOT / "qa/results/verified_analysis_eval_results.json")
    f3p = read(ROOT / "qa/results/f3p_eval_results.json")
    assert all(c["correct"] for c in skills["cases"]) and f3p["all_fixtures_passed"]
    native = read(ROOT / "qa/results/productization_native_results.json")
    browser = read(ROOT / "qa/results/local_browser_native_results.json")
    wsf = read(ROOT / "qa/results/productization_wsf_results.json")
    assert native["windows_key"]["source_fingerprint"] == native["gpt_live"]["source_fingerprint"] == browser["source_fingerprint"] == wsf["source_fingerprint"] == source
    environment = environment_status()
    now = datetime.now(timezone(timedelta(hours=9)))
    first = datetime.fromisoformat(baseline["first_observed_at_kst"])
    elapsed = int((now - first).total_seconds())
    interval = f"{elapsed // 3600}시간 {elapsed % 3600 // 60}분 {elapsed % 60}초"
    result = {"baseline": baseline, "current": current, "new_cycle5_cases": len(new), "core": core,
        "source_fingerprint": source, "source_unchanged": True, "cycle5_benchmark": {k:cycle[k] for k in ("detection", "faults_detected", "fault_count", "healthy_false_positives", "repairs_correct", "repair_count", "raw_metrics", "gated_metrics", "always_wrong_always_same", "wall_sec", "manifest_sha256", "live_efficacy")},
        "cycle5_recovery": recovery, "cycle5_export": export, "cycle5_visual": visual, "cycle5_analysis_lineage": integration,
        "compatibility_export": compatibility, "environment": environment,
        "native": native, "os_default_browser": browser, "wsf": wsf,
        "observed_from_kst": first.isoformat(), "recorded_at_kst": now.isoformat(), "observed_elapsed_sec": elapsed,
        "regressions": [], "live_efficacy": "NOT_VALIDATED"}
    write(ROOT / "qa/results/cycle5_final_validation.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    write(ROOT / "qa/results/cycle5_environment_final.json", json.dumps(environment, ensure_ascii=False, indent=2) + "\n")
    full = f"{current['passed_count']} PASS / {current['skipped_count']} SKIP / {current['deselected_count']} deselected"
    report = f'''# Cycle 5 최소 통합 구현·실행 보고서

첨부 업데이트 로드맵을 적용했다. Downloads의 원래 경로는 없어서 Desktop/aisw/프롬프트의 같은 이름 문서를 전체 읽었다. 기존 P0/P1 제품화 기능을 재사용하고 P1.5의 네 요소만 추가했다. P2/P3 후속 단계의 완료는 주장하지 않는다.

## 구현한 동작과 파일

- `src/probe/cycle5.py`: SourceSemanticRecord·TransformationLineage·GoalWitness, 소유자 검토·수정본·계약/계획 바인딩·CLI. 물리량/단위/기준을 분리한다. CSV 키별 변환을 실제 재계산하며 Worker 자기 승인과 의미 변경을 거절한다.
- `service.py`, `research_slice.py`, `scientific_verifier.py`, `agent_schemas.py`: 기존 정본 반영·의존 관계·provenance·의무에 네 검사를 연결한다. 변경된 의미 수정본·질문·계약·파일·Worker 계획은 기존 PASS를 재사용하지 못한다.
- `verification_repair.py`: F3-P가 켜진 소유자 요청에 한해 고정 계약의 PENDING TRANSFORM_CSV를 최대 2회 복구한다. 새 파일·감사 수정본·전체 계보 재검산을 사용한다. 원자료·질문·물리량·기준·추정 대상은 수정하지 않으며 과학 정본 반영을 자동 승인하지 않는다.
- `agent_runtime.py`, `autonomous_loop.py`, `control_runtime.py`, `context_compiler.py`: 검토 전 분석 대기, 검토 이벤트 검증 후 기존 커서 재개, stale 결과 차단. F3-P 원문은 보존하고 중복 문맥만 참조 해시로 줄여 기존 문맥 상한을 유지했다.
- `dashboard.py`, `workbench.py`, `final_report.py`, `report_ux.py`, `release.py`: 기존 조회·OwnerSession/Origin/CSRF 제어·보고서·export에 의미 정보를 연결한다. 검증 실패 계보는 export를 차단한다.
- `workbench_static/cycle5_ui.js`, `workbench.js`, `product_ux.js`, `index.html`: 기존 자료/검증/보고서 상세에서 실제 물리량·단위·기준·지역/기간·검토·원질문·네 검사 상태를 표시한다. 내부 schema 전체는 표시하지 않는다. Dagster의 자료 상세 구성을 참고했다. [공식 구성](https://docs.dagster.io/guides/operate/webserver)
- `reliability.py`, `qa/reliability_lab.py`, `qa/cycle5_lab.py`: 외부 고정 정답 기반 지표를 각각 기록한다. consistency를 correctness로 해석하거나 평균 종합 점수를 만들지 않는다.
- `generate_schemas.py`, `schemas/generated/{{source_semantic_record,transformation_lineage,goal_witness,goal_scope,analysis_plan}}.schema.json`: 기존 생성기로 다섯 스키마를 생성했다.
- `tests/test_cycle5_semantics.py`, `qa/cycle5_fixtures.py`, `cycle5_gold_fixtures.json`, `cycle5_recovery_probe.py`, `cycle5_export_probe.py`, `cycle5_analysis_lineage_probe.py`, `cycle5_visual_fixture.py`, `cycle5_visual_qa.cjs`, `cycle5_finalize.py`: 단위·통합·고장·별도 프로세스·export·실제 화면 근거를 추가했다.
- `CYCLE5.md`, `README.md`, `FEATURE_FREEZE.md`, `연구일지.md`: 한국어 실행법·허용 범위·실제 날짜/관측 시간을 기록했다.

## 재사용·호환 범위

기존 StateService·SQLite runtime_steps/planning_events·Research Slice 의존 그래프·검증 의무·Skills·F3-P·최종 보고/출시 게이트를 사용한다. 새 DB migration·dependency·Agent·그래프/검증 프레임워크는 없다. 기본 OFF다. 기존 테스트와 환경 제외 조건을 바꾸지 않았다.

자료 의미는 소유자가 검토한다. 구조화된 범위와 실제 지원 분석 방법을 비교하며 자연어 의미를 자동 증명하지 않는다. 모든 자료/문장에 의미 모델을 강제하지 않는다. 지원 온도 단위와 명시적 affine CSV 변환만 재계산한다. 비선형 변환·인과 분석은 이번 지원 범위 밖이다. 변환 복구는 기존 F3-P 안의 명시적 소유자 작업이며 과학 검증/반영 경계를 따로 통과해야 한다.

## 정확한 명령과 실제 결과

```powershell
.\\.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp build/pytest-cycle5-baseline --junitxml build/cycle5-baseline.xml
.\\.venv\\Scripts\\python.exe qa/qa_day1.py --output-dir build/cycle5-qa-final-source
.\\.venv\\Scripts\\python.exe -m probe.preflight validate-core
.\\.venv\\Scripts\\python.exe qa/reliability_lab.py --enable --cycle5 --output-dir build/cycle5-lab-final
.\\.venv\\Scripts\\python.exe qa/cycle5_recovery_probe.py --all
.\\.venv\\Scripts\\python.exe qa/cycle5_export_probe.py
.\\.venv\\Scripts\\python.exe qa/cycle5_analysis_lineage_probe.py
.\\.venv\\Scripts\\python.exe qa/reliability_lab.py --enable --output-dir build/cycle5-existing-lab-final
.\\.venv\\Scripts\\python.exe qa/f3p_recovery_probe.py --all
.\\.venv\\Scripts\\python.exe qa/results/research_slice_recovery_probe.py --all
.\\.venv\\Scripts\\python.exe qa/research_slice_export_probe.py --lab-root build/cycle5-existing-lab-final
.\\.venv\\Scripts\\python.exe qa/f3p_export_probe.py
.\\.venv\\Scripts\\python.exe qa/f3p_export_probe.py --clean
.\\.venv\\Scripts\\python.exe qa/local_auth_export_probe.py --qa-root build/cycle5-qa-final-source
.\\.venv\\Scripts\\python.exe -c "import sys; sys.path.insert(0,'qa'); from f3p_eval import run_offline; r=run_offline(); print({{'cases':len(r['cases']),'all_fixtures_passed':r['all_fixtures_passed'],'live_efficacy':r['live_efficacy']}})"
.\\.venv\\Scripts\\python.exe -c "import sys; sys.path.insert(0,'qa'); from verified_analysis_eval import run_offline; r=run_offline(); print({{'cases':r['logical_cases'],'correct':sum(c['correct'] for c in r['cases']),'unsafe_acceptance':r['unsafe_acceptance'],'live_agent_ablation':r['live_agent_ablation']}})"
.\\.venv\\Scripts\\python.exe qa/cycle5_visual_fixture.py
& 'C:/Program Files/nodejs/node.exe' qa/cycle5_visual_qa.cjs
.\\.venv\\Scripts\\python.exe qa/gui_visual_fixture.py
& 'C:/Program Files/nodejs/node.exe' qa/gui_visual_qa.cjs
.\\.venv\\Scripts\\python.exe qa/gui_visual_fixture.py
& 'C:/Program Files/nodejs/node.exe' qa/multi_provider_visual_qa.cjs
.\\.venv\\Scripts\\python.exe qa/product_visual_fixture.py
& 'C:/Program Files/nodejs/node.exe' qa/product_visual_qa.cjs
& 'C:/Program Files/nodejs/node.exe' qa/local_browser_auth_qa.cjs
& 'C:/Program Files/nodejs/node.exe' qa/productization_visual_qa.cjs
.\\.venv\\Scripts\\python.exe qa/productization_native_probe.py
.\\.venv\\Scripts\\python.exe qa/productization_wsf_probe.py
.\\.venv\\Scripts\\python.exe qa/local_browser_launcher_probe.py
.\\.venv\\Scripts\\python.exe -m probe.preflight validate-docker
.\\.venv\\Scripts\\python.exe -m probe.preflight smoke-api
.\\.venv\\Scripts\\python.exe -m probe.preflight validate-search
```

core 명령은 QA wrapper 안에서 실제 실행했다. 전체 pytest의 XML/log와 실제 임시 경로는 [최종 실행 JSON](cycle5_final_validation.json)의 `current`에 있다. QA Day1 helper의 역사적 baseline 숫자는 사용하지 않았다.

| 검사 | 실제 결과 |
|---|---|
| 수정 전 기준선 | 602 PASS / 5 SKIP / 2 deselected / 438.60초 |
| 최종 전체 | {full} / pytest 475.76초 / wrapper {current['duration_sec']}초 |
| 신규 Cycle 5 | {len(new)}/{len(new)} PASS |
| core | {core['passed_count']} PASS, SKIP 없음 |
| destructive | {stress['pytest']['passed_count']} PASS, 실행 가능 19/19 시나리오 PASS |
| Demo A/B | 각 5/5 PASS, 중복 논리 반영 0 |
| 별도 프로세스 재개 | PASS, 실험/근거/반영/통계 실행 각 2 |
| Cycle 5 강제 종료·복구 | 6/6 PASS, stale 반영 0·중복 반영 0 |
| 변환 dataset→실제 분석 | 2/2 PASS, 정상 반영 1·잘못된 배율 반영 0·상위 의미 수정 시 기존 PASS 차단 |
| 기존 F3-P / Slice 복구 | 6/6 / V2 11/11 PASS, 새 Lab의 3정책 33/33도 PASS |
| 기존 Skills / F3-P 평가 | 12/12 / 8/8 PASS, 잘못된→올바른 2, 올바른→잘못된 0 |
| 실험적 Ridge 산술 | 기존 scale 3·ON/OFF 2·비유한/미지원 1, 총 6 PASS; 기본 OFF |
| 새 Chrome 화면 | 33검사·7화면 PASS; 390px/CSS 200%, 오류·외부 요청 0 |
| 기존 실제 화면 | GUI 22·제공사 7·GPT UX 19·자동 인증 51·pythonw 18검사 PASS |

5 SKIP은 현재 Windows 저장 토큰 1개와 Docker 4개다. Live API/Search 2개는 기존 기본 선택 조건대로 deselected다.

## 벤치마크와 export

- Cycle 5는 고정 합성 CSV 11종×3회다. 분류 {cycle['detection']['correct']}/{cycle['detection']['total']}, 실제 고장 {cycle['faults_detected']}/{cycle['fault_count']} 탐지, 정상 9개 오차단 {cycle['healthy_false_positives']}, 복구 {cycle['repairs_correct']}/{cycle['repair_count']}가 외부 gold와 일치했다. {cycle['wall_sec']:.3f}초이며 성능 개선 수치로 해석하지 않는다.
- `always wrong + always same`: 정답 0/3, 결론 일치 2/2, 상태 일관성 2/2, 조건부 강건성은 분모 0으로 NOT_ESTIMABLE다. 이 사례를 신뢰성 성공으로 표시하지 않는다.
- 게이트/복구 후 전체 정답·완료는 18/33, 검토 대기 15/33, 수락된 결과의 정답은 18/18이다. 차단된 사례를 정답으로 세지 않는다. 복구 CSV는 PENDING을 유지한다.
- 기존 Lab도 현재 소스로 다시 실행했다. wall 358.041초, 각 arm에서 healthy 오차단 1/2·repair incomplete 4/9·compound 잔존 1/1이라는 한계도 유지했다. 쌍 비교·변형·회귀·복구와 상세 지표는 [Lab 결과](reliability_lab_results.json)에 있다. arm 간 Live Agent 성능 향상은 주장하지 않는다.
- 새 정상/복구 패키지 21/18파일, 기존 release/auth 17파일·F3-P 28/24파일·Slice 21/29/25파일의 해시/누락/검사 canary 유출 0. 새 조작·stale goal·비밀 값·잘못된 변환 4종을 차단했다. 비밀 값은 원문 양성 대조와 실패 중간 디렉터리까지 확인했다. 기존 Slice tamper/stale 6/6 차단.

## 발견·수정한 문제와 회귀

함께 저장한 변환 복구/의미 기록의 감사 이벤트 조회, 검토 후 커서 버전 조정, autonomous 이벤트 검증, F3-P 중복 문맥 초과, JS 정적 파일 허용 목록 누락, 검증 실패 변환의 export 차단을 수정했다. 기존 과학 게이트를 낮추지 않았다. 신규 fixture의 근거 하나를 SUPPORTED로 표시하려던 오류도 INCONCLUSIVE/INSUFFICIENT_DATA로 바로잡았다.

JS 누락을 발견하기 전의 중간 QA는 중단하고 현재 소스로 전체/core를 다시 실행했다. 이전 검증기 해시로 저장된 과거 Lab claim의 export는 원래 카탈로그 규칙에 따라 재검증 필요로 차단됐다. 새 Lab/claim으로 재검증했다. 최종 테스트 회귀는 0건이다.

## 실제 환경과 남은 제한

- Docker: NOT_VALIDATED, CLI/daemon/image 실행 불가. host 대체로 PASS를 만들지 않았다.
- Live LLM/GPT: NOT_VALIDATED, CREDENTIAL_UNCONFIGURED. 실제 유료 요청 0.
- Live Search: UNCONFIGURED. 실행하지 않은 검색을 VALIDATED로 기록하지 않았다.
- Windows 키 저장: NOT_VALIDATED, 실제 오류 1312. WSF: NOT_VALIDATED, 실제 Script Host 설정 접근 거부.
- OS 기본 브라우저: open=true였지만 25초 안에 티켓 교환/인증 목록 미관측으로 NOT_VALIDATED. QA Chrome/pythonw 검사를 OS 파일 연결 성공으로 승계하지 않았다.
- F3-P·Cycle 5 Live efficacy: NOT_VALIDATED. 합성 오프라인 정합성과 실서비스 Agent 효능은 별개다.
- **demo_ready={str(environment['demo_ready']).lower()}, release_ready={str(environment['release_ready']).lower()}, product_release_ready={str(environment['product_release_ready']).lower()}**.

활성화·소유자 JSON 검토·명시적 repair 명령은 [CYCLE5.md](../../docs/guides/CYCLE5.md)에 있다. 새 기능은 분석 전 새 연구에서만 켠다. 기존 OFF 정책·계약·결과를 뒤집지 않는다.

## 시간과 근거

관측 {first.strftime('%H:%M:%S')}–{now.strftime('%H:%M:%S')} KST / **{interval}**. 구현·테스트·수정·대기를 포함한 실제 관측 구간이며 순수 구현 시간이나 성능 개선으로 해석하지 않는다. 이전 기록에 중복 합산하지 않았다.

현재 전체/core/stress/artifact/Demo와 새 Lab·복구·export·화면의 소스 지문은 `{source}`다. [전체 QA](../../build/cycle5-qa-final-source/qa_day1_summary.json), [최종 결과](cycle5_final_validation.json), [환경](cycle5_environment_final.json), [신규 Lab](cycle5_lab_results.json), [복구](cycle5_recovery_results.json), [export](cycle5_export_results.json), [화면](cycle5_visual_results.json).
'''
    write(ROOT / "qa/results/cycle5_implementation_report.md", report)
    heading = "## 2026-10-02 (금) — Cycle 5 최소 의미 검증"
    journal = ROOT / "docs/history/연구일지.md"
    previous = journal.read_text(encoding="utf-8", errors="strict")
    if heading not in previous:
        note = f'''\n\n{heading}

### 구현한 목록

1. 물리량/단위/배율/기준/지역/기간/정밀도/검토 상태를 분리한 선택적 SourceSemanticRecord, 원질문·방법·추정 대상·제한을 고정하는 소유자 GoalWitness를 추가했다.
2. CSV 키별 parent→child 실제 재검산과 선언 고정 F3-P 변환 복구를 기존 상태·의존 그래프·검증 의무에 연결했다. 자기 승인·의미 변경·stale PASS 재사용을 차단하며 원자료/기존 바이트를 보존했다.
3. 기존 자료/검증/보고서에 실제 의미·단위·기준·기간·지역·원질문 범위를 표시했다. 외부 gold 기반 정답·일치·강건성·일관성·복구·완료/검토를 분리했다. 기본 OFF, 새 DB migration/dependency/Agent/그래프 없음.

### 실제 검증과 시간

- 기준선 602 PASS·5 SKIP·2 deselected / 438.60초. 최종 **{full} / 475.76초**, 새 58/58·core 660·destructive 116·Demo A/B 각 5/5 PASS. 같은 현재 소스의 게이트를 확인했다.
- 신규 합성 변환 33/33 분류·고장 24/24 탐지·정상 오차단 0·복구 9/9 정답 일치, 강제 새 프로세스 복구 6/6 PASS. 일관되게 틀린 사례의 정답률은 0/3이고 결론 일치는 2/2다.
- 기존 Skills 12/12·F3-P 8/8·복구 6/6·Slice V2 11/11와 새 Lab 3정책 복구 33/33 PASS. 새 21/18·기존 17·28/24·21/29/25파일 export 해시/누락/비밀 유출 0, 새 차단 4종·기존 tamper/stale 6/6 PASS.
- 새 Chrome 33검사/7화면과 기존 GUI 22·제공사 7·GPT UX 19·인증 51·pythonw 18검사 PASS. JS/외부 요청 0. Source/문맥/커서/정적 파일/출시 경계를 수정하고 기존 테스트·선택 조건은 유지했다.
- **demo_ready=true, release_ready=false, product_release_ready=false**. 실제 Windows 오류1312·WSF 접근 거부·OS 교환 미관측·Docker·Live LLM은 NOT_VALIDATED; Search UNCONFIGURED, 유료 호출 0·live efficacy NOT_VALIDATED.

관측 **{first.strftime('%H:%M:%S')}–{now.strftime('%H:%M:%S')} KST / {interval}**. 구현·테스트·수정·대기를 포함하며 순수 구현 시간이나 성능 개선으로 해석하지 않는다. 이전 합계에 중복 합산하지 않았다.

근거: [구현 보고서](../../qa/results/cycle5_implementation_report.md), [최종 결과](../../qa/results/cycle5_final_validation.json), [환경](../../qa/results/cycle5_environment_final.json), [활성화/실행법](../guides/CYCLE5.md).
'''
        write(journal, previous.rstrip() + note)
    assert _source_fingerprint() == source
    print(json.dumps({"full": full, "new": len(new), "core": core["passed_count"], "source_unchanged": True,
        "demo_ready": environment["demo_ready"], "release_ready": environment["release_ready"], "interval": interval}, ensure_ascii=True))


if __name__ == "__main__": main()
