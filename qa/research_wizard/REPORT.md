# 연구 설정·튜토리얼·검색 예산 수정

## 적용 내용

- 새 연구의 빠른 재열기와 늦은 응답을 처리하고, 업로드 완료가 재개한 질문을 덮어쓰지 않도록 수정했다. 기존 초안 버전과 제출 멱등성을 유지한다.
- 주제·질문·파일 / 모델·성능·웹 검색·예산 / 고급 설정의 3장으로 구성한다. 아래의 이전·다음 장 버튼으로 이동하고 마지막 장에서만 시작한다. 고급 설정 생략 안내를 표시한다.
- 켜진 연결·승인된 목적지·준비된 키를 가진 모델은 초록색 활성화, 나머지는 빨간색 비활성화로 표시한다. 키가 필요 없는 승인 로컬 연결도 활성화다. 이 표시는 실제 API 응답 검증을 뜻하지 않는다.
- 최초 튜토리얼을 표시하고 새 연구·설정에서 다시 열 수 있다. 건너뛰기·완료를 저장하며 연구나 모델 호출을 시작하지 않는다.
- 초보자 새 연구의 기본값은 선택한 분석 자료 전송이다. 구 API와 명시한 none은 유지한다. 공개 검색어 전송 동의는 별도이며 비공개 검색어·키·경로 차단을 유지한다.
- 검색 단가와 모델 완료 예산을 별도로 계산한다. 남은 연구·월간 예산에서 완료 비용과 미확정 비용을 보존하고 가능한 검색 횟수를 구한다. 전송 직전 비용·횟수 상한을 다시 검사한다.

현재 기본 Crossref는 확인된 무료 메타데이터 단가를 사용하므로 횟수 상한으로 제한한다. 유료 단가는 요청마다 올림하는 원장 단위로 계산한다. 단가가 없으면 차단하며 유료 검색 실패 비용은 UNRESOLVED로 보존한다.

기존 화면의 간결한 양식은 [Linear의 작업 생성 양식](https://linear.app/docs/issue-templates)을 참고했고, 단계 구성은 [W3C 여러 페이지 양식](https://www.w3.org/WAI/tutorials/forms/multi-page/)과 [GOV.UK 질문 페이지](https://design-system.service.gov.uk/patterns/question-pages/)를 참고했다.

## 수정 파일·재사용

- `src/htrsa/workbench_static/{workbench.js,product_ux.js,beginner_ux.js,workbench.css}`: 화면 생명주기·3장 구성·표시·튜토리얼·첨부 갱신.
- `src/htrsa/{product_policy.py,control_plane.py,research_settings.py,search_policy.py}`: 기본 전송 범위·별도 검색 동의·자동 횟수 계산·원자적 비용 차단.
- `tests/test_research_wizard_budget.py`: 신규 검색 비용·동의·상한·단가·실패 검사 24개.
- `qa/research_wizard_browser.cjs`, `qa/beginner_v4_browser.cjs`, `qa/ui_unblock_browser.cjs`, `qa/ui_unblock_browser_fixture.py`: 실제 Chrome 회귀와 명시적 모의 자격 증명.
- `docs/development/FEATURE_FREEZE.md`, `docs/history/연구일지.md`, `qa/README.md`, 이 보고서와 `results.json`: 범위와 실행 기록.

DB migration·새 dependency 없음. 기존 OwnerSession, 초안·첨부 저장소, 설정 해석, 문헌 제공사, 완료 비용 예약과 비용 원장을 재사용한다. 기존 분석 기능의 기본 OFF와 검증·복구·출처·출시 기준을 유지한다.

## 실행 명령

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_product_ux.py tests/test_ui_start_unblock.py tests/test_ui_unblock_policy_files.py tests/test_ui_unblock_search_pdf.py tests/test_beginner_v4.py -q -p no:cacheprovider --basetemp build/wizard-baseline-tests --junitxml build/wizard-baseline.xml
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_research_wizard_budget.py tests/test_product_ux.py tests/test_ui_start_unblock.py tests/test_ui_unblock_policy_files.py tests/test_ui_unblock_search_pdf.py tests/test_beginner_v4.py -q -p no:cacheprovider --basetemp build/wizard-final-tests --junitxml build/wizard-final.xml
node qa/research_wizard_browser.cjs
node qa/beginner_v4_browser.cjs
node qa/ui_unblock_browser.cjs
.\.venv\Scripts\python.exe -X utf8 -m htrsa.preflight validate-core
.\.venv\Scripts\python.exe -X utf8 qa/ui_unblock_offline_validation.py
```

## 관측 결과

수정 전 관련 테스트 144 PASS·1 SKIP / 73.23초. 최종 관련 테스트 168 PASS·1 SKIP / 90.58초. SKIP은 실제 Windows 키 저장 세션 검사다. 신규 24개는 모두 통과했다. 실제 Chrome은 새 흐름 38·초보자 기존 흐름 35·입력/연결 기존 흐름 53개가 통과했다. 화면 오류·브라우저 외부 요청 0이다.

빠른 close→reopen은 수정 전 양식 소실, 수정 후 유지로 관측됐다. 안내 저장 응답이 늦어 새 안내를 닫는 문제도 수정 전 소실·수정 후 유지로 재현했다. 첫 Core 실행은 이 수정 때문에 중단하고 최종 소스에서 다시 실행했다. 일반 탭 이동 후 재열기는 수정 전 재현되지 않았으며 빠른 재열기와 늦은 응답을 별도로 검사했다. 신규 테스트의 초기 실패는 단가 문자열 표기와 fixture의 설정 버전·완료 비용 가정 오류였으며 실제 값을 사용하는 fixture로 정정했다. 기존 테스트를 삭제하거나 보안·검증 조건을 낮추지 않았다.

전체 회귀·파괴적·데모·복구·내보내기 결과는 실행 종료 후 추가한다. 실제 API 과금 요청은 이 작업에서 보내지 않았다. 모의 전송·로컬 재현을 Live 효능으로 기록하지 않는다.
