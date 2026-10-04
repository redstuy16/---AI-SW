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

## 자주 쓰는 명령

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m htrsa.preflight status
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

안내 원본은 `src/htrsa/workbench_static/tutorial_content.js`다. 수정 후 `qa/sync_tutorial_guide.py --write`로 Markdown을 동기화한다. Chrome 검사는 별도 작업 공간의 모의 키·오류를 사용하며 학습 저장·초안·첨부 보존과 유료 실행 경계를 확인한다. 확대 검사는 사용자 Chrome 프로필을 건드리지 않고 별도 프로필의 실제 200% 확대를 사용한다.

처음에는 독립 팝업에서 참여 여부를 묻는다. 이번만 건너뛰기와 다시 보지 않기를 구분하며 설정에서 재실행할 수 있다. API 발급 → 앱 연결 → 새 연구 → 항목 입력의 4개 과정을 실제 화면의 화살표와 안내 상자로 진행한다. 발급 안내는 선택한 제공사의 필요한 순서를 먼저 표시하고 계정·결제·키 관리 정보는 아래에서 펼쳐 읽는다. 상세 안내 30개·도움말 41개와 버전 2/3의 이전 과정 상태를 보존한다. `node qa/help_api_browser.cjs`는 단가 적용·반복 연결 확인을 실제 앱과 모의 제공사로 검사한다.

[구현·검증 보고서](results/tutorial_implementation_report.md) · [최종 결과](results/tutorial_final_validation.json). 큰 원시 결과는 `build/tutorial/`, 캡처는 `output/playwright/tutorial/`에 저장한다.
