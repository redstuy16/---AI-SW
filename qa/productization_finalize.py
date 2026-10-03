"""실행한 최종 결과와 소스 지문이 일치할 때만 보고서·연구일지를 기록한다."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from htrsa.preflight import VALIDATION_DIR, _source_fingerprint, environment_status


ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / "build/productization-qa-final"


def read(path):
    return json.loads(path.read_text(encoding="utf-8", errors="strict"))


def write(path, text):
    data = text.encode("utf-8", errors="strict")
    path.write_bytes(data)


def main():
    baseline = read(ROOT / "qa/results/productization_baseline_observed.json")
    summary = read(QA / "qa_day1_summary.json")
    current, core, stress = summary["current"], summary["core_marker"], read(QA / "stress_results.json")
    source = _source_fingerprint()
    for name in ("core", "stress", "artifact", "demo_a", "demo_b"):
        marker = read(VALIDATION_DIR / (name + ".json"))
        assert marker["passed"] and marker["source_fingerprint"] == source, name
    native = read(ROOT / "qa/results/productization_native_results.json")
    assert native["windows_key"]["source_fingerprint"] == native["gpt_live"]["source_fingerprint"] == source
    browser = read(ROOT / "qa/results/local_browser_native_results.json")
    assert browser["source_fingerprint"] == source
    faults = read(ROOT / "qa/results/productization_fault_results.json")
    assert faults["passed"] and faults["source_fingerprint"] == source
    visual = read(ROOT / "qa/results/productization_visual_results.json")
    wsf = read(ROOT / "qa/results/productization_wsf_results.json")
    assert wsf["source_fingerprint"] == source
    auth_visual = read(ROOT / "qa/results/local_browser_auth_visual_results.json")
    recovery = read(ROOT / "qa/results/f3p_recovery_results.json")
    auth_export = read(ROOT / "qa/results/local_auth_export_results.json")
    f3p_export = read(ROOT / "qa/results/f3p_export_validation.json")
    clean_export = read(ROOT / "qa/results/f3p_clean_export_validation.json")
    slice_export = read(ROOT / "qa/results/research_slice_export_results.json")
    first = datetime.fromisoformat(baseline["first_observed_at_kst"])
    for name in ("f3p_recovery_results.json", "local_auth_export_results.json", "f3p_export_validation.json", "f3p_clean_export_validation.json", "research_slice_export_results.json"):
        assert (ROOT / "qa/results" / name).stat().st_mtime > first.timestamp(), "과거 실행을 이번 실행으로 기록하지 않음: " + name
    assert current["passed"] and core["passed"] and stress["all_executable_passed"]
    assert recovery["all_passed"] and auth_export["passed"] and f3p_export["all_passed"] and clean_export["all_passed"] and slice_export["all_passed"]
    assert visual["passed"] and auth_visual["passed"] and summary["release_validation"]["passed"]
    environment = environment_status()
    assert environment["demo_ready"]
    cases = list(ET.parse(current["xml"]).iter("testcase"))
    new = [c for c in cases if c.attrib.get("classname", "").endswith("test_productization")]
    assert new and all(not any(c.find(t) is not None for t in ("failure", "error", "skipped")) for c in new)
    now = datetime.now(timezone(timedelta(hours=9)))
    elapsed = int((now - first).total_seconds())
    interval = f"{elapsed // 3600}시간 {(elapsed % 3600) // 60}분 {elapsed % 60}초"
    changed = ["src/htrsa/productization.py", "src/htrsa/desktop.py", "src/htrsa/control_plane.py",
        "src/htrsa/provider_checks.py", "src/htrsa/workbench.py", "src/htrsa/preflight.py", "src/htrsa/product_policy.py",
        "src/htrsa/workbench_static/product_ux.js", "src/htrsa/workbench_static/workbench.css", "tests/test_productization.py",
        "H-TRSA.wsf", "README.md", "docs/development/FEATURE_FREEZE.md", "docs/guides/PRODUCTIZATION.md",
        "qa/results/productization_baseline_observed.json", "qa/productization_native_probe.py", "qa/local_browser_launcher_probe.py",
        "qa/productization_visual_qa.cjs", "qa/productization_fault_probe.py", "qa/productization_wsf_probe.py", "qa/productization_finalize.py"]
    for name in changed:
        (ROOT / name).read_bytes().decode("utf-8", errors="strict").encode("utf-8", errors="strict")
    record = {"scope": "로드맵의 첫 제품화 단위와 기존 P0/P1 제품 기능의 회귀 검증", "source_fingerprint": source,
        "source_unchanged": True, "baseline": baseline, "full_regression": current, "core": core,
        "new_productization_cases": len(new), "destructive": stress["pytest"], "demo_repeatability": read(QA / "demo_repeatability.json"),
        "release": summary["release_validation"], "f3p_recovery": recovery, "auth_export": auth_export,
        "f3p_export": f3p_export, "f3p_clean_export": clean_export, "slice_export": slice_export,
        "faults": faults, "native": native, "native_browser": browser, "visual": visual, "wsf": wsf, "auth_visual": auth_visual,
        "environment": environment, "changed_files": changed, "db_migrations": [], "dependencies_added": [],
        "live_efficacy": "NOT_VALIDATED", "paid_live_requests": native["gpt_live"]["paid_requests"],
        "observed_interval": {"start_kst": first.isoformat(), "end_kst": now.isoformat(), "seconds": elapsed, "duration": interval},
        "limitation": "실환경 미검증과 후속 로컬·추가 제공사·모델 성능 평가를 완료로 기록하지 않았다."}
    write(ROOT / "qa/results/productization_final_validation.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    write(ROOT / "qa/results/productization_environment_final.json", json.dumps(environment, ensure_ascii=False, indent=2) + "\n")
    full = f"{current['passed_count']} PASS / {current['skipped_count']} SKIP / {current['deselected_count']} deselected"
    fault_rows = "\n".join(f"| {r['scenario']} | {r['status']} | {r['error_code'] or '없음'} | {r['generation_requests']} | {r['same_key_redispatch']} |" for r in faults["scenarios"])
    files = "\n".join(f"- [{name}](../../{name})" for name in changed)
    exports = ", ".join(str(c["file_count"]) for c in clean_export["cases"])
    slice_counts = ", ".join(str(c["files"]) for c in slice_export["cases"])
    tamper = sum(sum(v is True for v in c["blocked_faults"].values()) for c in slice_export["cases"])
    full_folder = Path(current["xml"]).parent
    full_command = f'& "{ROOT / ".venv/Scripts/python.exe"}" -m pytest -q -p no:cacheprovider --basetemp "{full_folder / "pytest-full-regression"}" --junitxml "{current["xml"]}"'
    report = f'''# 제품화 첫 단계 구현·검증 보고서

## 범위와 완료 판단

로드맵 13절의 첫 단위인 Windows 키 저장·GPT 실제 연결 경로·GPT 우선 모델 선택을 구현했다. 기존 성능 슬라이더·완료 비용 예약·연구별 설정·보고서·도움말을 재사용했다. 실환경 키 저장과 기본 브라우저 인증, GPT live smoke가 남아 있어 로드맵 1차 완료를 주장하지 않는다. 이후 단계인 Ollama/LM Studio qualification·공유 자원 검증·품질/전체 비용 비교·추가 제공사·발표 모드와 필요 시 P3 연구 확장은 후속 범위다.

## 구현한 동작

1. Windows 키 저장 뒤 다시 읽어 확인한다. 기존 보호 파일의 권한 오류는 OS 키 변경 전에 차단한다. UTF-8 키 비교에서 `compare_digest(str)`의 비ASCII 오류를 바이트 비교로 수정했다. 환경변수·OS 저장·보호 파일의 기존 우선순위와 subprocess/container 비밀 제거를 유지했다.
2. GPT 종합 검사를 기존 어댑터·Gateway·원장에 연결했다. 모델 목록·텍스트·구조화 출력·고정 도구 왕복·LOW 숙고·사용량을 같은 연구 ID와 USD 0.10 기본 전체 한도로 확인한다. 최대 USD 0.25와 기존 월/요청 상한을 함께 적용한다. 생성 요청 최대 5회, 자동 재시도 0이다.
3. 동의·키·전송 승인·가격이 없으면 호출 전에 차단한다. 같은 멱등 키의 재전송은 기존 결과를 반환하고 다른 본문은 충돌로 차단한다. 프로세스 중단은 `NEEDS_RECONCILIATION`과 `UNRESOLVED`로 남기고 외부 호출을 다시 보내지 않는다.
4. 설정/키 변경은 과거 검사 성공을 `STALE`로 표시한다. 환경변수 검증은 해당 프로세스에 한정한다. 모의 HTTP `OFFLINE_VALIDATED`, 실제 HTTP `VALIDATED`, 연구 효능 `NOT_VALIDATED`를 구분한다.
5. 기본 선택에 권장·빠른·고성능 GPT를 표시하고 수동 ID는 개발자 옵션에 유지한다. 5단계 온보딩, 검사 비용/동의, 월/요청 예산 저장, Windows·기본 브라우저 미검증 표시를 추가했다. 출처 링크는 공식 OpenAI HTTPS 주소만 실행한다.
6. `H-TRSA.wsf`와 `htrsa.desktop`을 추가했다. 기존 작업대를 `pythonw`로 열고 실패 시 한국어 다시 열기/취소를 제공한다. 실제 `pythonw`+Chrome은 PASS, WSF 파일 연결과 실제 OS 기본 브라우저 인증 완료는 NOT_VALIDATED다.
7. 기존 출시 게이트를 유지한다. `product_release_ready`는 기존 `release_ready`와 현재 소스·사용자/컴퓨터의 Windows 저장/교체/삭제/재시작·기본 브라우저 인증을 추가로 요구한다. 오래된 표식·부분 검사·모의 실행을 통과로 사용하지 않는다.

## 재사용과 호환성

`ControlStore.control_configs`, `spend_ledger`, 제공사 `REGISTRY`, `PinnedTransport`, `RoutedGateway`, `provider_checks`, `OwnerSession`, `WorkbenchAPI`, 기존 제품 정책·설정·보고서·내보내기를 재사용했다. DB migration·새 외부 dependency·새 연구 엔진·역할은 **0**이다. 기존 명령과 개별 제공사 검사는 호환된다. Skills·F3-P·Ridge 기본 OFF와 Verify → Commit, provenance, 필수 재검증·보안·복구 기준을 유지했다.

소스 지문: `{source}`. 기존 테스트의 단언·선택 조건은 변경하지 않았다. 새 테스트의 원장 필드/XML/CSRF 기대값과 브라우저 QA의 선택자·비동기 대기 오류를 고쳐 최종 실행했다. 전체 회귀 실패·오류는 0이다.

## 실제 명령과 결과

| 명령 | 관측 결과 |
|---|---|
| `{baseline['command']}` | {baseline['passed']} PASS / {baseline['skipped']} SKIP / {baseline['deselected']} deselected, {baseline['pytest_duration_sec']}초 |
| `.\\.venv\\Scripts\\python.exe -m pytest -q tests/test_productization.py tests/test_preflight.py -p no:cacheprovider --basetemp build/pytest-productization-focus-frozen --junitxml build/productization-focus-final.xml` | 41 PASS, 10.91초 |
| `.\\.venv\\Scripts\\python.exe qa/qa_day1.py --output-dir build/productization-qa-final` | destructive {stress['pytest']['passed_count']} PASS / {stress['pytest']['duration_sec']}초 wrapper, 실행 가능 19/19 PASS; Demo A/B 각 5/5; 새 프로세스 재개·17파일 export PASS |
| `{full_command}` | {full}, {current['duration_sec']}초 wrapper; 신규 제품화 {len(new)} PASS |
| `.\\.venv\\Scripts\\python.exe -m htrsa.preflight validate-core` | {core['passed_count']} PASS, skipped={str(core['skipped']).lower()}, exit={core['exit_code']} |
| `.\\.venv\\Scripts\\python.exe qa/productization_fault_probe.py` | 모의 HTTP 14/14 PASS, 같은 키 재전송 0, 검사 canary 노출 0 |
| `node qa/productization_visual_qa.cjs` | 실제 pythonw·Chrome 18검사·3화면 PASS, 390px/CSS 200%, 저장/오류/외부 요청/비밀 노출 0 |
| `node qa/gui_visual_qa.cjs` / `node qa/product_visual_qa.cjs` / `node qa/multi_provider_visual_qa.cjs` | 22검사·21화면 / 19검사·19화면 / 7검사 PASS |
| `node qa/local_browser_auth_qa.cjs` | 실제 Chrome 51검사·4화면 PASS, QA 컨트롤러·만료·재사용·동시 탭·재시작·대체 링크 확인 |
| `.\\.venv\\Scripts\\python.exe qa/f3p_recovery_probe.py --all` | 새 프로세스 복구 6/6 PASS, 외부 exactly-once는 NOT_CLAIMED |
| `.\\.venv\\Scripts\\python.exe qa/local_auth_export_probe.py --qa-root build/productization-qa-final` | 17파일 PASS, 메모리 양성 대조 12개·인증 입력 3개 차단, canary·해시 불일치 0 |
| `.\\.venv\\Scripts\\python.exe qa/f3p_export_probe.py` / `--clean` | F3-P {exports}파일 각각 canary/clean PASS |
| `.\\.venv\\Scripts\\python.exe qa/research_slice_export_probe.py --lab-root build/product-lab-final-source` | Slice {slice_counts}파일 PASS, tamper/stale {tamper}/6 차단 |
| `.\\.venv\\Scripts\\python.exe qa/productization_native_probe.py` | Windows {native['windows_key']['status']} / {native['windows_key'].get('error_code')}; GPT {native['gpt_live']['status']} / {native['gpt_live'].get('error_code')} |
| `.\\.venv\\Scripts\\python.exe qa/local_browser_launcher_probe.py` | 실제 열기 true, 25초 내 교환/목록 미관측 → NOT_VALIDATED |
| `.\\.venv\\Scripts\\python.exe qa/productization_wsf_probe.py` | 실제 cscript 설정 읽기 Access is denied / exit 1 → 구문·WSF 실행 NOT_VALIDATED |
| `.\\.venv\\Scripts\\python.exe -m htrsa.preflight validate-docker` / `smoke-api` / `validate-search` | SKIPPED: DOCKER_UNAVAILABLE / API_UNCONFIGURED / SEARCH_UNCONFIGURED |

5 SKIP은 Docker 4개와 실제 Windows credential 1개, 2 deselected는 기존 Live API/Search 선택 조건이다. 전체 실행 로그·XML은 [최종 QA](../../build/productization-qa-final/qa_day1_summary.json)의 경로를 따른다. 이전 QA wrapper의 역사적 135 baseline 필드는 이번 기준선으로 사용하지 않았다.

## 고장·비용 관측

| 시나리오 | 결과 | 안전한 오류 코드 | 생성 요청 | 같은 키 재전송 |
|---|---|---|---:|---:|
{fault_rows}

정상 모의 경로는 메타데이터 1회+생성 5회, 입력 200/출력 60 fixture 토큰, USD 0.001 fixture 정산이다. 이는 실제 모델 비용·성능 개선 근거가 아니다. 전체 예산이 처음부터 부족하면 0회, 중간에 부족하면 2회 이후 차단했다. 미정산 사용량은 삭제하거나 0으로 바꾸지 않았다. [모의 검사 근거](productization_fault_results.json)의 wall time도 오프라인 검사 시간이다. 로컬 모델의 queue/cold load/prefill/generation/Tool/Verifier/repair/Manager 추가 API·전체 wall 비교는 **NOT_VALIDATED**다.

## 환경과 남은 조건

| 항목 | 실제 상태 |
|---|---|
| Windows 실제 키 저장 | NOT_VALIDATED: 현재 토큰의 오류 1312, 사용자 키 변경 없음 |
| OS 기본 브라우저 인증 | NOT_VALIDATED: 열기 반환 true만 관측, 자동 교환·목록 조회 미관측 |
| WSF Script Host·파일 연결 | NOT_VALIDATED: 현재 토큰의 설정 읽기 Access is denied, 앱 dispatch 안 함 |
| Docker 격리 | {environment['docker']['status']}: CLI/daemon/image 미구성 |
| GPT 연결 / 기존 Live LLM | {native['gpt_live']['status']} / {environment['provider']['status']}: 실제 키·기존 Manager 모델 없음 |
| Live Search | {environment['search']['status']} |
| Skills / F3-P / 연구 live efficacy | NOT_VALIDATED |
| demo_ready / release_ready / product_release_ready | {str(environment['demo_ready']).lower()} / {str(environment['release_ready']).lower()} / {str(environment['product_release_ready']).lower()} |

유료 실서비스 요청은 **{native['gpt_live']['paid_requests']}회**, 검사 범위의 secret exposure는 0이다. 모의 provider를 실제 사용 가능·연구 효능 통과로 표시하지 않았다. WSF shell association, 네이티브 브라우저 확대, 보호 파일의 실제 Windows fallback 재시작을 새로 검증했다고 주장하지 않는다. 저장 키 삭제는 제공사 폐기가 아니며 브라우저/작업대 종료는 이미 실행한 Worker 중지가 아니다. 환경변수의 프로세스 내 직접 교체는 개발자가 재검사해야 한다.

실행법: 저장소 **`H-TRSA.wsf` 더블클릭**, 설정의 GPT 준비/키 등록/유료 연결 검사/예산 확인. CLI 대체·명시적 opt-in은 [제품화 안내](../../docs/guides/PRODUCTIZATION.md)에 있다.

## 변경 파일

{files}

기계 판독 결과: [최종 검증](productization_final_validation.json), [현재 환경](productization_environment_final.json), [실환경 관측](productization_native_results.json), [브라우저 관측](local_browser_native_results.json), [화면](productization_visual_results.json).

## 관측 시간

{first.strftime('%H:%M:%S')} KST부터 {now.strftime('%H:%M:%S')} KST까지 **{interval}**. 테스트·수정·대기를 포함하는 관측 구간이다. 순수 구현 시간·모델 성능 개선으로 해석하지 않으며 과거 합계와 중복 합산하지 않았다.
'''
    write(ROOT / "qa/results/productization_implementation_report.md", report)
    heading = "## 2026-10-01 (목) — 제품화 첫 단계"
    journal = ROOT / "docs/history/연구일지.md"
    previous = journal.read_text(encoding="utf-8", errors="strict")
    if heading not in previous:
        note = f'''

{heading}

### 구현한 목록

1. Windows 키 저장 후 다시 읽기 확인과 기존 fallback 권한의 사전 검사를 추가했다. UTF-8 키 비교 오류를 고쳤다.
2. 기존 어댑터·Gateway·비용 원장으로 GPT 목록/텍스트/구조화/고정 도구/숙고/사용량 종합 검사를 구현했다. USD 0.10 기본 전체 상한, 최대 생성 5회, 동의·멱등성·미정산 중단 복구·설정/키의 오래된 성공 차단을 적용했다.
3. 친숙한 GPT 선택, 5단계 온보딩·예산 확인, Windows/기본 브라우저 미검증 표시와 `H-TRSA.wsf`/`pythonw` 실행을 추가했다. 기존 제품 정책·보고서·연구 설정·도움말과 과학 검증/복구/출시 기준을 유지했다. 새 DB migration·dependency는 없다.

### 실제 검증과 시간

- 기준선 565 PASS·5 SKIP·2 deselected / 405.98초. 최종 **{full} / {current['duration_sec']}초 wrapper**, 신규 {len(new)} PASS, core {core['passed_count']} PASS, destructive 116 PASS·19/19, Demo A/B 각 5/5·새 프로세스 재개 PASS.
- 모의 연결/고장/예산 14/14, F3-P 복구 6/6, auth release 17·F3-P {exports}·Slice {slice_counts}파일 PASS. 변조/오래된 상태 6/6 차단, 해시 불일치·검사 canary 유출 0.
- 실제 pythonw·Chrome 신규 18검사/3화면, 기존 GUI 22·GPT UX 19·제공사 7·자동 인증 51검사 PASS. 390px/CSS 200%·브라우저 저장소·외부 요청·JS 오류·비밀 노출을 확인했다.
- **demo_ready={str(environment['demo_ready']).lower()}, release_ready={str(environment['release_ready']).lower()}, product_release_ready={str(environment['product_release_ready']).lower()}**. Windows 오류1312, Script Host 설정 접근 거부, 기본 브라우저 교환/목록 미관측, 키 없는 GPT 및 Docker는 NOT_VALIDATED; Search UNCONFIGURED. 실제 유료 요청 {native['gpt_live']['paid_requests']}·live efficacy NOT_VALIDATED.
- 로드맵 첫 단위의 오프라인 구현을 마쳤다. 실제 GPT·Windows/브라우저와 이후 로컬 모델·추가 제공사·성능 평가 조건이 남아 있어 전체 제품화 완료를 주장하지 않는다. 이번 최종 marker와 소스 지문은 동일하다.

관측 **{first.strftime('%H:%M:%S')}–{now.strftime('%H:%M:%S')} KST / {interval}**. 테스트·수정·대기를 포함하며 순수 구현 시간이나 성능 향상으로 해석하지 않는다. 과거 합계에 중복 합산하지 않았다.

근거: [구현 보고서](../../qa/results/productization_implementation_report.md), [최종 결과](../../qa/results/productization_final_validation.json), [환경](../../qa/results/productization_environment_final.json), [실행법](../guides/PRODUCTIZATION.md).
'''
        write(journal, previous.rstrip() + note)
    assert _source_fingerprint() == source
    print(json.dumps({"full": full, "core": core["passed_count"], "new_cases": len(new), "source_unchanged": True,
                      "demo_ready": environment["demo_ready"], "release_ready": environment["release_ready"],
                      "product_release_ready": environment["product_release_ready"], "interval": interval}, ensure_ascii=True))


if __name__ == "__main__":
    main()
