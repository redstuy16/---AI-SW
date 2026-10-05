# QA 실행

명령은 저장소 루트에서 실행합니다. 기존 실행 파일 이름을 유지합니다.

Cycle 12의 [구현 보고서](cycle12/IMPLEMENTATION_REPORT.md)와 [32개 요구·검사 규약](cycle12/PROTOCOL.md)은 출처 형식 대조·질문 변경·현재 결론·동결 재현의 실제 검사를 정리합니다.

초보자 v4의 [실행 보고서](qualified_profiles/IMPLEMENTATION_REPORT.md)와 [평가 규약](qualified_profiles/PROTOCOL.md)을 확인합니다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_beginner_v4.py -q
node qa/beginner_v4_browser.cjs
.\.venv\Scripts\python.exe -X utf8 qa/qualified_profiles/evaluate.py
```

| 위치 | 내용 |
|---|---|
| `*.py`, `*.cjs` | 회귀·복구·고장·화면 검사 실행 코드 |
| `fixtures/` | 고정 사례·평가 설정 JSON |
| `results/` | 로컬 검사 결과·과거 구현 보고서 (공개 제외) |
| `prepublish/` | 비밀·개인 경로를 제외한 공개 준비 보고서 |
| `live_api_test/` | 세션별 해시 검증 API 증거 패키지 |

## 고교 과학 루프·현재 AI 검색·시각 보고서

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m pytest tests/test_science_loop.py tests/test_ai_web_search.py tests/test_science_gateway.py tests/test_science_reports_examples.py tests/test_science_figure_proof.py -q -p no:cacheprovider --basetemp build/science-regression
node qa/science_browser.cjs
.\.venv\Scripts\python.exe -B -X utf8 qa/science_live_validation.py --smoke
.\.venv\Scripts\python.exe -B -X utf8 qa/science_single_luna.py --offline
.\.venv\Scripts\python.exe -B -X utf8 -m probe.preflight validate-core
.\.venv\Scripts\python.exe -B -X utf8 -m probe.preflight validate-search
```

현행 Chrome 검사는 실제 과학 실행 경로와 화면·PDF 도식/그림 일치, 390px·200% 확대를 확인한다. 모의 결과와 실제 API 증빙을 구분한다. 현재 Windows 사용자 세션의 실제 검증은 프로젝트의 `Probe-과학검증.wsf` 또는 실행기의 `--live --open-results`를 사용한다. 재실행을 포함한 누적 $3 상한이며 앱에 설정한 키를 재등록하지 않는다. 독립 단일 LUNA는 앱의 판단 순환을 호출하지 않고 같은 도구·검증 경계에서 자체 검토와 수정 기회를 갖는다. [실제 검증 가이드](../docs/guides/SCIENCE_LIVE_VALIDATION.md) · [구현·P0/P1/P2 감사](results/science_inquiry_20261005.md)

아래 과거 무료 검색·이전 화면 QA는 당시 경로의 회귀 자료이며 현재 AI 검색이나 모델 성능 검증을 대신하지 않는다.

## 자주 쓰는 명령

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m probe.preflight status
.\.venv\Scripts\python.exe qa/qa_day1.py
.\.venv\Scripts\python.exe qa/live_api_recording_probe.py
node qa/live_api_recording_visual_qa.cjs
node qa/connection_ux_visual_qa.cjs
.\.venv\Scripts\python.exe qa/prepublish_check.py --require-ready
node qa/hardening_visual_qa.cjs
```

연결 UI 검사는 모의 키 저장소와 실제 Chrome을 사용하며 실제 Windows 안전 저장 검증을 대신하지 않습니다.

기본 pytest와 모의 HTTP 검사는 실제 유료 API를 호출하지 않습니다. 실제 API 검사는 앱에서 검사 상한·실행 동의를 지정합니다. [첫 API 검사](../docs/guides/LIVE_API_TEST.md)

검사 결과는 기본 `qa/results/`에 저장합니다. `qa_day1.py --output-dir 경로`로 별도 결과 위치를 지정할 수 있습니다. 큰 작업 공간은 `build/`, 화면 캡처는 `output/`에 저장합니다.

루트의 `*_results.json`도 로컬 검사 원본으로 보관하며 Git과 제출용 ZIP에서 제외합니다.

## 연구 시작·파일·PDF 검사

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_ui_start_unblock.py tests/test_ui_unblock_policy_files.py tests/test_ui_unblock_search_pdf.py
node qa/ui_unblock_browser.cjs
.\.venv\Scripts\python.exe -X utf8 qa/ui_unblock_pdf_checks.py
.\.venv\Scripts\python.exe -X utf8 qa/ui_unblock_offline_validation.py
```

새 Chrome 검사는 실제 작업대 서버와 명시적 모의 HTTP 전송을 사용합니다. 라이브 API 성능 검사가 아닙니다. 결과는 `build/ui_start_unblock/`, 화면은 `output/playwright/ui_start_unblock/`에 저장합니다. [실행 보고서](ui_start_unblock_report.md)

[최신 구현·검증 보고서](prepublish/IMPLEMENTATION_REPORT.md) · [로컬 결과 목록](results/README.md) · [개발자 상세 명령](../docs/development/CLI.md)

## 연구 설정 3단계·검색 예산 검사

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_research_wizard_budget.py -q
node qa/research_wizard_browser.cjs
node qa/beginner_v4_browser.cjs
```

모델 비용과 검색 단가를 별도로 계산하며 완료 비용·월간 한도·미확정 비용·공유 시도 수를 보존한다. Chrome 검사는 자동 튜토리얼, 페이지 이동, 활성 표시, 초안·첨부 복구와 390px·200% 확대를 확인한다. [실행 결과](research_wizard/REPORT.md)

## 모델 활성화 표시 검사

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m pytest tests/test_model_activation.py -q
node qa/model_activation_browser.cjs
```

모의 키 저장소와 실제 Chrome으로 전송 동의 기본값, 키 삭제·재등록, 연결 교체와 이전 모델 구성 보존을 확인한다. 유료 호출·실제 키 검증은 수행하지 않는다. [수정 보고서](results/model_activation_fix_report.md)

## 튜토리얼·사용 안내 검사

```powershell
.\.venv\Scripts\python.exe -B -X utf8 qa/sync_tutorial_guide.py
.\.venv\Scripts\python.exe -B -X utf8 qa/tutorial_link_check.py
.\.venv\Scripts\python.exe -B -X utf8 -m pytest tests/test_tutorial.py -q
node qa/tutorial_browser.cjs
node qa/tutorial_zoom_browser.cjs
```

안내 원본은 `src/probe/workbench_static/tutorial_content.js`다. 수정 후 `qa/sync_tutorial_guide.py --write`로 Markdown을 동기화한다. Chrome 검사는 별도 작업 공간의 모의 키·오류를 사용하며 학습 저장·초안·첨부 보존과 유료 실행 경계를 확인한다. 확대 검사는 사용자 Chrome 프로필을 건드리지 않고 별도 프로필의 실제 200% 확대를 사용한다.

처음에는 독립 팝업에서 참여 여부를 묻는다. 이번만 건너뛰기와 다시 보지 않기를 구분하며 설정에서 재실행할 수 있다. API 발급 → 앱 연결 → 새 연구 → 항목 입력의 4개 과정을 실제 화면의 화살표와 안내 상자로 진행한다. 발급 안내는 선택한 제공사의 필요한 순서를 먼저 표시하고 계정·결제·키 관리 정보는 아래에서 펼쳐 읽는다. 상세 안내 30개·도움말 41개와 버전 2/3의 이전 과정 상태를 보존한다. `node qa/help_api_browser.cjs`는 단가 적용·반복 연결 확인을 실제 앱과 모의 제공사로 검사한다.

[구현·검증 보고서](results/tutorial_implementation_report.md) · [최종 결과](results/tutorial_final_validation.json). 큰 원시 결과는 `build/tutorial/`, 캡처는 `output/playwright/tutorial/`에 저장한다.

## 자동 검색·AI 보고서 검사

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m pytest tests/test_research_report_flow.py -q -p no:cacheprovider
node qa/research_report_browser.cjs
node qa/research_report_browser.cjs --weak
node qa/research_report_browser.cjs --many
node qa/research_report_native_zoom_browser.cjs
.\.venv\Scripts\python.exe -B -X utf8 qa/science_live_validation.py --smoke
.\.venv\Scripts\python.exe -B -X utf8 qa/science_single_luna.py --offline
.\.venv\Scripts\python.exe -B -X utf8 -m probe.preflight validate-core
.\.venv\Scripts\python.exe -B -X utf8 qa/cycle12/offline_validation.py
```

Chrome 검사는 모의 제공사와 실제 실행기를 사용한다. 현행 과학 루프·AI 검색은 `science_live_validation.py --smoke`로 과금 없이 검사한다. 실제 사용자 세션의 비교는 `--live`로 명시 실행하며 전체 $3 상한을 적용한다. `probe.preflight validate-search`는 현재 native 응답·사용량·정산 증빙만 조회하며 유료 요청을 자동 실행하지 않는다. [과거 실행 결과](results/research_search_report_redesign.md)

## AI 검색·공개 원문·페이지 근거 검사

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m pytest tests/test_free_search_recovery.py tests/test_research_report_flow.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -B -X utf8 -m pytest tests/test_ai_web_search.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -B -X utf8 qa/free_search_evaluate.py
.\.venv\Scripts\python.exe -B -X utf8 qa/free_search_recovery_probe.py
node qa/free_source_browser.cjs
node qa/free_source_browser.cjs --zoom
.\.venv\Scripts\python.exe -B -X utf8 -m probe.preflight validate-search
```

평가는 고정 모의 자료의 확보율·관련성·인용·보고서·요청 수치 확보를 각각 기록한다. Chrome에서 보고서와 공개 원문을 동시에 읽고 페이지 이동·390px·브라우저 자체 200%·키보드·무료 조회를 검사한다. 과거 `research_report_live_search_probe.py`, `free_search_live_probe.py`와 `test_live_search.py`는 `PROBE_LEGACY_LIVE_SEARCH=1`을 별도로 지정한 LEGACY 검사로만 실행하며 현행 AI 검색 출시 표식을 만들지 않는다. 기존 `PROBE_LIVE_SEARCH` 값만으로는 켜지지 않는다. [사용 안내](../docs/guides/RESEARCH_REPORTS.md)

## 연구 기본 화면 v8 검사

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m pytest -q -p no:cacheprovider tests/test_research_base_screen.py tests/test_execution_flow.py
node qa/research_base_browser.cjs
node qa/research_report_browser.cjs
node qa/research_report_browser.cjs --weak
node qa/research_report_browser.cjs --many
node qa/research_report_native_zoom_browser.cjs
node qa/cycle12/browser.cjs
node qa/cycle12/pdf_currentness_browser.cjs
```

별도 고정 작업 공간에서 목록 60개·페이지 이동·긴 제목·공식 상태·범주·7단계·읽기 전용 조회·자동 상태 갱신·선택과 스크롤 보존·오래된 보고서 차단을 확인한다. 상태 전환 주입은 오프라인 화면 검사이며 실제 Live Agent 실행 증명이 아니다. [실행 보고서](results/research_base_v8_report.md)
