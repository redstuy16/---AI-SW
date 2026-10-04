# Cycle 12 v5 구현·실행 보고서

관측일: 2026-10-03 KST. 첨부 v5를 `ㄱ` 요청의 실행 명세로 적용했다. `review_summary.json`과 참조 ZIP은 진단 근거로 사용했다. [최종 실제 결과](final_validation.json) · [32개 요구·검사 경로](PROTOCOL.md) · [수정 전 점검](TRIAGE.md)

## 1. 유지한 앱 목적·차별화·범용 엔진 경계

질문·계획·실제 도구·검증·정본·변경 이력을 연결하는 자율 연구 앱을 유지했다. 일반 연구를 기후 계산기로 바꾸지 않았다. 지원하는 기후 한정 절차의 출처·질문·현재성 경계만 보강했으며 미지원 분야의 일반 엔진을 유지했다. 정본은 기존 Verify→Commit과 StateService를 통해서만 변경한다.

## 2. 기존 구현 재사용 / 새 구현 / 미적용·보류 항목

AgentRuntime·AutonomousResearchLoop·ToolRegistry·StateService·Research Slice·공개 기후 프로필·비용 원장·검색 정책·소유자 인증·PDF·release·복구·F3-P 평가기를 재사용했다. 새 구현은 키별 TXT/CSV 대조, 적용 의미 검사, 승인한 질문 수정본, 원자적 무효화, 지연 자료 조회와 현재/이전 결과 연결이다.

새 역할·실행기·DB·그래프·제공사·천문 운영 프로필·교사 승인·보고서 형식 선택은 추가하지 않았다. Skills·F3-P·실험용 Ridge·Cycle 5 기본 OFF를 유지했다. 구 API 프로필 기본 DISABLED와 초보자 UI의 기존 AUTO 정책을 유지한다. 새로운 출처 대조는 기후 한정 절차 안에서만 적용한다. 큰 Live 비교·외부 제품/학생·교사 평가는 미실행이다.

## 3. 실제 앱에서 재현한 오류와 참조 코드에만 있던 제한의 구분

수정 전 앱 검사 **3 FAIL**: 선택 `None`의 원시 TypeError, 선택 연도 중복의 통과, 질문 원문 변경 뒤 카드가 현재로 남는 오류다. 추가 보안 검사 **1 FAIL**: 정형 키 패턴이 아닌 등록 비밀값을 포함한 검색어가 출처 대조 요청을 막지 못했다. 모두 실제 경계에서 재현하고 수정했다.

참조 코드의 별도 5진단은 기준만 변경 READY, 집계만 변경 READY, 평균 순열 GOAL_CONFLICT, 선택 결측 TypeError, 필수 대조 부재 CHECK_PENDING이다. 이 결과를 앱의 수정 전 결과나 Live Agent 성능으로 보고하지 않았다.

전체 중간 회귀에서 기존 Skills 기본 OFF 검사 **1 FAIL·1075 PASS**를 발견했다. 계약 없는 도구 목록 조회의 호환성을 복구하고 파일 접근은 차단했다. 기존 테스트 기대 결과를 바꾸지 않았다. 최종 전체 회귀에는 실패가 없다.

## 4. 결측값·순서 정규화·기준/집계/의미 검사 수정

선택값의 None·빈 문자열·비수치·bool·NaN·Infinity, 요청/선택 키 중복·누락·교체를 계산 전에 타입이 있는 오류로 처리한다. 초안과 캡처를 보존하고 0으로 채우지 않는다. 비선택 결측만으로 정상 계산을 막지 않는다.

평균의 같은 그룹 구성원 순열은 허용하되 원본 행 순서는 보존한다. 가중치·그룹 이동·방향·순서 의존 방법에는 확대하지 않는다. 기후 두 기간의 기존 비중첩 조건을 유지하며 공통 함수에 보편 비중첩 조건을 강제하지 않는다.

제품·물리량·단위·저장 배율·기준·공간 범위·연간 해상도·J-D 열을 실제 비교한다. 의미 필드 부재는 검토 필요, 불일치는 충돌이다. TXT의 0.01°C와 CSV의 °C를 형식별로 해독한다. 화씨 편차 변환에 32를 더하지 않으며 기준 변경은 실제 기준 행에서 다시 계산한다.

## 5. 출처 대조의 적용 조건·선택/필수·잔여 공통 오류

고정 NASA CSV를 기존 검색 동의·전송·횟수·완료 비용 예약·원장 경계에서만 수집한다. 선택 연도와 변환 기준 연도의 값까지 키별로 대조하고 원본 두 형식을 보존한다. 존재하는 선택 대조의 충돌도 무시하지 않는다.

선택 대조가 없으면 `performed=false / NOT_PRESENT_OPTIONAL`과 한계를 남기고 나머지 필수 검사로 제한된 결과를 허용한다. 필수 대조 부재는 CHECK_PENDING이며 Worker가 필수 조건을 낮출 수 없다. 정책은 승인 계획에 고정된다. 실제 HTTP 취득 그룹과 300초 시간 차이를 확인하지만 같은 발행본을 독립 인증하지는 못한다.

저장 해시는 전송 압축 해제·UTF-8 해독 후 저장 텍스트 바이트에 적용한다. 관측한 HTTP 상태·시각·헤더만 기록한다. 로컬 원본의 원래 HTTP 취득과 출처 설명 HTML은 미관측·미수집이다. TXT/CSV는 같은 상위 NASA 자료이며 독립 관측이 아니다. 양쪽 원자료·질문 해석·검증기가 공유하는 오류는 남는다.

## 6. 원 질문·승인 범위·변경 이력·권한 처리

원래 goal은 보존하고 현재 질문·계획·질문 수정본·검증기 버전·원본 해시를 연결했다. 지원 문장 해석과 승인 기간/단위/기준을 비교한다. 일반 자연어 의미를 완전히 보장하는 검증기로 표시하지 않는다.

소유자 `amend-question`은 현재 state_version을 확인하고 실제 지원 범위의 새 계획을 저장한다. 추가 권한/정책 필드는 거절한다. Worker가 제출한 owner 승인·qualification·필수 조건은 반영 경계에서 차단한다. 질문 변경은 새 연구 수정본이며 기술적 repair 성공으로 세지 않는다.

## 7. F3-P·StateService·예산·재검증 통합

자료/질문 기록과 의존 무효화·근거/실험 상태 변경을 기존 트랜잭션에서 함께 처리한다. 결함 주입 rollback, 검증 도중 상태 변경, 오래된 재개 결과, 반영 직전 예산/중단 경계를 실제 검사했다. 사용한 행·의미·질문·검증기가 바뀌면 현재 근거를 재사용하지 않는다. 비선택 행·별도 연구의 알려진 안전한 변경은 수치를 유지한다.

기존 F3-P **8/8 기대 상태**가 일치한다. 실제 repair 완료는 2사례이며 복합 결함·공통 오류·검증기 결함·의미 변경·예산 부족·미지원 repair의 안전한 미반영도 기대 결과에 포함한다. 최대 2회와 필수 의무 재검사를 유지했다. 새 기후 평균 repair 템플릿은 추가하지 않았다.

F3-P 새 프로세스 복구 **6/6**: AFTER_FAILURE_EVIDENCE는 아직 결정하지 않은 모의 모델 호출 1회, 나머지 5경계는 재개 호출 0회다. 기후 Verify/Commit 직후 강제 종료·재개 **2/2**는 각각 commit 1회·재개 모델 호출 0회·필수 대조 유지다. 실제 유료 호출은 없다.

## 8. 초보자 UI·PDF·이전/현재 결론 확인

기존 결론 카드 6문항을 유지하고 형식 대조의 실제 수행/생략·현재성·잔여 한계를 표시한다. 자료 상세는 펼칠 때, 선택값은 별도 조회할 때 읽으며 최대 200행으로 제한한다. 기존 버튼 간격·설정 탭·3장 연구 설정·low/medium/high/max·튜토리얼·자료 기본 전송을 유지했다.

질문 변경 직후 이전 결론은 이력, 새 질문은 재확인 대기로 표시하고 현재 PDF를 차단한다. 재계산 후 실제 제외/추가 키와 변경/유지 수치를 보여준다. 실제 카드 관측값은 다음과 같다.

| 수치 | 1981~2000 / 2001~2020 | 1986~1995 / 2011~2020 |
|---|---:|---:|
| A 평균(°C 편차) | 0.3235 | 0.324 |
| B 평균(°C 편차) | 0.7285 | 0.836 |
| B−A(°C 편차) | 0.405 | 0.512 |

실제 Chrome **25+35+38+53+27=178개** 검사가 통과했다. 새 25개는 초기 분석을 실제 도구로 시드한 모의 Agent 결과에서 질문 변경→현재성 차단→재계산→PDF를 검사한다. 기존 시작/API 검사는 실제 로컬 서버와 명시적 모의 HTTP를 쓴다. 사용자/교사 실험이나 Live Agent E2E로 세지 않는다. PDF **5페이지·164,367바이트**를 렌더링해 한글·표·그림·페이지 번호를 확인했고 잘림·겹침을 발견하지 않았다. 교사 승인 표시는 없다.

## 9. 기후 정상 경로와 천문 공통 구조 테스트의 별도 지원 상태

기존 v4 평가 함수를 그대로 호출한 **16/16 기대 상태**: 정상 4·결함 6·변경 3·모호/미지원 3이다. 같은 기후 자료의 설계 변형이며 독립 연구 16건이나 성능 향상률이 아니다. 기존 평가 파일은 덮어쓰지 않았다.

천문은 원본 바이트를 보존한 TRAPPIST-1 CSV/JSON 7행·1카탈로그의 공통 구조 진단이다. pl_name 키·공전 주기 물리량·day→hour(×24)·평균 순열·같은 평균의 키 교환·작성자 선택 수정본을 실제 계산했다. 원래 차이 **8.9478055833 day / 214.747334 hour**, 수정본 **8.1302353 day / 195.1256472 hour**다. 오차·rowupdate·HTML 참조를 보존하되 HTML을 실행하지 않는다. 공분산 불확도 전파·Live 수집·운영 천문 qualification·교사 승인은 미지원/미검증이다. 일반 엔진으로 라우팅된다.

## 10. 변경 파일, DB migration 및 dependency 변경

| 파일 | 이번 변경 |
|---|---|
| `src/htrsa/period_comparison.py` | 타입 있는 선택값·키/순서/의미 공통 함수 |
| `src/htrsa/climate_profile.py` | CSV 해독·의미·필수 정책·키별 대조·bounded capture·현재성 검사 |
| `src/htrsa/qualified_workflow.py` | 관측 취득·검색/비밀/원장 경계·소유자 질문 변경·승인/재검증 |
| `src/htrsa/qualified_profiles.py` | Worker 권한 차단·지연 자료 조회·현재/이전 결론·변경 수치 |
| `src/htrsa/service.py` | 자료/질문 변경과 의존 무효화의 기존 트랜잭션 통합 |
| `src/htrsa/agent_runtime.py`, `real_tools.py` | 실제 qualified import의 허용 파일 경계·Skills 호환성·타입 있는 도구 실패 |
| `src/htrsa/final_report.py`, `qualified_replay.py`, `report_pdf.py` | 두 원본·최근 캡처·승인 계획 export/replay·질문/결과 변경 PDF |
| `src/htrsa/workbench.py`, `workbench_static/beginner_ux.js` | 기존 인증 API·질문 변경·자료 상세·카드/이력 UI |
| `tests/test_cycle12.py` | 32개 요구에 대응하는 매개변수 검사 67개 |
| `qa/cycle12/` | 실행 코드·고정 천문 자료·규약·실제 결과·보고서 |
| `qa/prepublish_check.py` | 직접 생성·검토한 합성 키 2개의 고정 SHA만 기존 INFO 분류에 추가 |
| `README.md`, `qa/README.md`, `docs/development/FEATURE_FREEZE.md`, `docs/history/연구일지.md` | 사용/검사 링크·승인한 동결 범위·실제 시간 기록 |

이번 DB migration·dependency 추가는 **0**이다. 기존 001~006 migration과 이미 설치한 PDF/통계/제공사 의존성을 유지했다. 과거 미커밋 작업과 연구 파일을 보존했다. 소스/테스트 13파일의 UTF-8과 패치/충돌 표시 부재를 확인했고 JS 문법 검사는 종료 코드 0이다. 합성 키의 고정 예외라도 활성 환경값이면 P1, 미등록 키도 P1로 차단함을 별도 3검사로 확인했다.

## 11. 정확한 테스트 명령과 관측 결과·실패·skip·분모

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m pytest -q -p no:cacheprovider --basetemp build/cycle12-baseline-valid --junitxml=build/cycle12/observed_baseline.xml --tb=short
.\.venv\Scripts\python.exe -B -X utf8 -m pytest tests/test_verified_analysis_skills.py tests/test_cycle12.py tests/test_beginner_v4.py tests/test_real_tools.py -q -p no:cacheprovider --basetemp build/cycle12-final-boundary --junitxml=build/cycle12/final_boundary.xml --tb=short
.\.venv\Scripts\python.exe -B -X utf8 -m pytest -q -p no:cacheprovider --basetemp build/cycle12-final-compatible-full --junitxml=build/cycle12/final_compatible_full.xml --tb=short
.\.venv\Scripts\python.exe -B -X utf8 -m htrsa.preflight validate-core
.\.venv\Scripts\python.exe -B -X utf8 qa/cycle12/offline_validation.py
.\.venv\Scripts\python.exe -B -X utf8 qa/cycle12/evaluate_v4.py
.\.venv\Scripts\python.exe -B -X utf8 qa/cycle12/reference_review.py
.\.venv\Scripts\python.exe -B -X utf8 qa/cycle12/cross_domain.py
node qa/cycle12/browser.cjs
.\.venv\Scripts\python.exe -B -X utf8 qa/cycle12/export_probe.py
node qa/beginner_v4_browser.cjs
node qa/research_wizard_browser.cjs
node qa/ui_unblock_browser.cjs
node qa/model_activation_browser.cjs
node --check src/htrsa/workbench_static/beginner_ux.js
```

| 실행 | 실제 최종 결과 |
|---|---|
| 수정 전 전체 | 1010 PASS·5 SKIP·2 deselected / 686.17초 |
| 최종 관련 | 140 PASS / 150.66초. 새 67개 포함 |
| 최종 전체 | **1077 PASS·5 SKIP·2 deselected / 1003.71초** |
| 공식 Core | **1077 PASS·skipped=false·종료 코드 0** |
| 파괴적 | **116 PASS / 222.669초**, 실행 가능한 19시나리오 PASS·Docker 1시나리오 NOT_VALIDATED |
| Demo A/B | **각 5/5**, 중복 논리 동작·미해결 참조 0 |
| 일반 새 프로세스 resume | PASS, 실험/근거/반영/stats 각 2 |
| F3-P 평가/복구 | 기대 상태 **8/8**, 복구 **6/6** |
| 기후 강제 종료·재개 | **2/2**, 각 반영 1·재개 모델 호출 0·필수 대조 유지 |
| 기본 release | **17파일**, hash/누락/비밀/미해결 참조 이상 0 |
| F3-P canary/clean export | **104파일** 검사, hash/누락/비밀/절대 경로/참조 이상 0 |
| 변경 후 기후 release | **27파일**, hash/canary 이상 0·replay PASS·변조/오래된 상태 차단 |
| v4 평가 / 새 Chrome | **16/16 기대 상태 / 25검사**, 별도 분모 |
| 기존 Chrome | **35+38+53+27=153검사**, 별도 분모 |
| 참조 재실행 | **32패키지/19동결 파일 일치·24조건/2자료군/72정책 평가** |

전체의 SKIP은 Docker 4개와 현재 Windows 자격 증명 세션 부재 1개다. 기본 pytest의 기존 Live API/Live Search 제외 2개를 유지했다. Core는 원래 환경 marker 정책으로 실행했으며 새 제외를 추가하지 않았다. 새 67개·관련 140개·파괴적 116개·전체 1077개는 겹치므로 합산하지 않는다. 기존 테스트의 삭제·약화·기대 결과 변경은 없다.

첫 명령은 `--basetemp build/cycle12/baseline-tests` 부모 부재로 264 PASS·5 SKIP·2 deselected·746 setup errors였고 정상 폴더에서 기준선을 다시 실행했다. 새 검사 초기 4개/2개 실패는 kwargs·실제 schema·최대 repair 2회 등 검사 코드 가정을 바로잡았다. 앱 결함과 분리했다. 변경 전 소스를 불러온 진행 중 full/Core/offline은 중단 후 최종 소스로 다시 실행했으며 통과로 합산하지 않았다.

병행 Chrome 실행에서 초안 새로고침 후 `#list-new` 10초 대기 실패 1회(27검사까지 PASS)가 있었다. 소스/기존 검사 변경 없이 같은 명령을 재실행해 38개가 통과했다. 일시적 실패 원인은 확정하지 않았다. 참조 JSON 재생성은 Windows CRLF 때문에 바이트 해시가 달랐지만 JSON 의미와 개행 정규화 후 바이트가 일치했다. CSV는 원래 바이트와 일치하며 동결 파일을 바꾸지 않았다.

## 12. 실제 Live/E2E 실행 여부·비용·허가 범위

실제 로컬 SQLite·도구·파일·검증·release·HTTP 서버·Chrome·PDF와 모의 Agent/모의 HTTP를 실행했다. 실제 유료 API 호출은 **0회**다. 이번 구현 요청으로 잔액을 사용하거나 Live 캠페인을 자동 실행하지 않았다. 실제 모델의 질문 해석/계획/판단부터 수정 결과까지의 E2E는 **NOT_VALIDATED**다. 구현·로컬 정확성과 Live Agent 효능을 구분한다.

아래 실제 명령은 환경 사전 조건에서 건너뛰었다. 키/manager model 미설정은 검증 프로세스의 CLI 상태이며 사용자의 작업대 API 등록 여부를 대체하지 않는다.

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m htrsa.preflight smoke-api
.\.venv\Scripts\python.exe -B -X utf8 -m htrsa.preflight validate-search
.\.venv\Scripts\python.exe -B -X utf8 -m htrsa.preflight validate-docker
.\.venv\Scripts\python.exe -B -X utf8 -m htrsa.preflight status
```

각 결과는 `API_UNCONFIGURED / SEARCH_UNCONFIGURED / DOCKER_UNAVAILABLE`, passed=false·skipped=true다. 실제 모델 성능·비용 개선·경쟁 우위·사용자/교사 사용 효과는 측정하지 않았다. 비교 규약과 선택적 사용 명령은 [PROTOCOL.md](PROTOCOL.md)와 [README.md](README.md)에 있다.

## 13. 남은 원자료/해석/검증기 한계와 환경 gate

| 상태 | 현재 관측 |
|---|---|
| CORE / STRESS / ARTIFACT / DEMO A / DEMO B | 모두 true |
| Docker 격리 | NOT_VALIDATED, CLI/daemon/image 없음 |
| Live LLM | NOT_VALIDATED, 검증 프로세스의 키/manager model 미설정 |
| Live Search | UNCONFIGURED, 검증되지 않음 |
| Windows 실제 키 저장 / OS 기본 브라우저 | NOT_VALIDATED, 기존 저장 관측 `WINDOWS_ERROR_1312` 유지 |
| Skills / F3-P / Cycle 12 Live 효능 | NOT_VALIDATED |
| demo_ready | **true** |
| release_ready / product_release_ready | **false / false** |

공통 원자료·해석·검증기 오류, 미수집 설명 HTML, 시간만으로 발행 버전을 보장하지 못하는 대조, 지원 문장 밖의 의미 검토, 인과·예측·확증적 유의성·불확도 전파는 한계다. 캡처 해시와 재계산 일치가 과학적 참을 보장하지 않는다. 실제 도구의 oracle.csv 접근은 차단했지만 운영체제/Docker 프로세스 격리를 통과로 올리지 않았다.

공개 준비 후보 **352파일**, 의심 비밀값 **0**, Git 이력 **369개 VALIDATED**, 현재 Core 일치를 확인했다. 공개는 라이선스 소유자 선택 대기 때문에 **BLOCKED**다. [실제 공개 검사](publication_security.json) GitHub 업데이트는 이번 환경의 `.git` 쓰기 제한 때문에 미실행이며 공개/출시 준비 완료로 보고하지 않는다.

## 14. 제출 가능한 실제 동작 증거와 미확인 주장

실제 동작 자료는 다음 네 기능으로 묶는다: **탐구 계획과 자료 선택 / 자료 의미와 분석 검토 / 결론 검토 카드 / 변경 후 재검증과 재현**. Goal·계획·도구·상태·검증/수정 경계를 기존 기록에서 확인할 수 있다. 모의 결정을 실제 AI 판단 영상으로 제시하지 않는다.

- [최종 실행 결과](final_validation.json), [검사 규약](PROTOCOL.md), [실제 앱 결함 구분](TRIAGE.md)
- [기후 변경 후 release manifest](../../build/cycle12/qualified-export/888d47358cc9475a881f59da74be1331/release/manifests/release_manifest.json), [기후 replay 결과](../../build/cycle12/qualified_export.json)
- [새 Chrome 결과](../../build/cycle12/browser_results.json), [결과 PDF](../../build/cycle12/browser/da9d1cf8-6624-4508-bb84-cbe7cecaa9e2/report.pdf), [5페이지 렌더 검사](../../build/cycle12/pdf/visual_validation.json)
- [파괴적·Demo·F3-P·복구·export](../../build/cycle12/offline_results.json), [기존 v4 함수 16사례](../../build/cycle12/v4_evaluation.json)
- [참조 분리 결과](../../build/cycle12/reference_review.json), [천문 공통 진단](../../build/cycle12/cross_domain_results.json)

첨부 대회 V1.0 DOCX의 3~5핵심 기능·관측 가능한 Agent 요소·5개 대표 사례·실제 동작 요구와 제출 분량은 준비 기준으로만 사용했다. 이후 공식 공지를 다시 확인한 현재 규정이라고 주장하지 않는다. 5페이지 개발 보고서·1페이지 기술 설명·3분 실제 Live 영상·10페이지 발표자료·교사 사용 결과는 이번 실행에서 제작/검증하지 않았다. 이 14항목 구현 보고서와 앱 결과 PDF를 그 제출물의 완성으로 세지 않는다.

관측 구간: 첫 기준선 시도 **22:11:52 KST**부터 최종 환경 확인 **23:23:17 KST**, 약 **1시간 11분 25초**. 구현·검사·수정·대기를 포함하며 문서 작성 시간·순수 구현 시간·성능 개선 측정은 아니다. 이전 작업 구간과 합산하지 않았다.
