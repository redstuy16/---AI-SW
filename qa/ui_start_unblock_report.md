# 연구 시작·API 확인·입력·PDF 수정 결과

검증일: 2026-10-03 KST. 실제 작업대 서버와 Chrome을 사용했다. HTTP 전송은 명시적 모의 전송이며 라이브 API 성공을 뜻하지 않는다.

## 1. 왜 Start가 막혔는가

- 수정 전 재현 테스트에서 모델 `m`과 질문·유한 예산만 제출하면 `400 INVALID_REQUEST`가 발생했다. CSV와 제목을 요구하던 `NewResearch` 및 준비 경로가 원인이었다.
- 모델 체크박스 선택과 별도 등록된 런타임 프로필이 분리되어 있었다. 목록에서 선택해도 숨겨진 `model_profile_id`가 없을 수 있었고, 별도 등록과 역할 매핑이 필요했다.
- UI 주 성능과 기존 추론 적용 경로가 연결되어 고급 추론 값이 실행 요청에서 바뀔 수 있었다. 버전 2는 연구 범위와 모델 추론을 별도로 해석한다.
- `control_plane.py`, `product_policy.py`, `workbench.py`, `control_runtime.py`, `product_ux.js`에서 실제 요청·상속·검증 순서를 수정했다. 다른 가능한 원인을 확인된 원인으로 기록하지 않았다.

기준선 전체는 **847 PASS·5 SKIP·2 deselected / 529.24초**였다. 명세의 과거 565개를 현재 기준선으로 사용하지 않았다. 첫 실패 기록은 `build/unblock-reproduction.xml`에 보존했다.

## 2. 실제 통과한 사용자 흐름

1. 기존 승인된 연결을 사용하고 모델 하나를 선택한다.
2. 연구 질문과 유한 작업 한도를 입력한다. 파일은 없고 고급 설정은 닫혀 있다.
3. 한 번의 `질문 전송 · 연구 시작`으로 현재 선택을 해석·저장하고 기존 런타임을 시작한다.
4. 모의 HTTP 전송에서 최신 모델의 관리자 요청 1회를 관측했다. 실제 계약 스키마로 응답을 검사하고 관리자 단계가 완료됐다.
5. 근거가 없는 사례는 `INSUFFICIENT_DATA`로 종료했다. 실험·수치·문헌 결과를 생성하지 않았고 canonical 과학 상태 이벤트는 0건이었다.

`연결 확인 · API 호출`은 질문·파일·연구 생성 없이 별도의 고정 텍스트 요청 1회를 수행했다. 두 경로에서 실제 유료 호출은 0건이다. 첫 번째는 연구 계획 경로 검증, 두 번째는 작은 연결 검사 경로 검증이다.

Start는 현재 입력·업로드 완료·초안 revision 저장을 기다린다. 기존 제출 키·상태 버전 검사와 재시작 경계를 사용한다. 새로고침으로 중복 전송하지 않았고 성공한 초안은 재사용하지 않았다.

## 3. 모델 자동 배정과 실제 요청

- 승인되고 활성화된 기존 연결에 한해 목록 선택으로 최소 비밀 없는 프로필을 기존 `ControlStore`에 만든다. 연결·프로토콜·모델의 안정된 `AUTO-…` ID를 사용하며 재선택·새로고침으로 중복 생성하지 않는다.
- 새 프로필의 단가·API 키·라이브 검사 상태를 만들지 않는다. 실제 키·단가·전송 권한이 없으면 해당 기존 gate에서 구체적으로 차단된다.
- 선택 목록의 첫 유효 기본 모델을 보존한다. 다른 모델 추가로 기본 모델이나 자료 목적지가 변경되지 않는다. 기본 모델 제거 시 남은 선택 순서로 해석한다.
- 필수 역할 4개는 기본 모델을 상속한다. 명시적 역할 지정은 해당 역할만 바꾼다. 잘못된 명시적 지정은 다른 모델로 몰래 대체하지 않는다.

Chrome에서 관측한 비밀 없는 모의 전송 요약:

```json
{
  "selected_model_pool": ["m"],
  "routing": {
    "manager": "m",
    "experiment_coordinator": "m",
    "analysis_planner_worker": "m",
    "verification_coordinator": "m"
  },
  "attachments": [],
  "source": null,
  "search_required": true,
  "search_attempt_limit": 5,
  "report_format": "pdf",
  "observed_manager_model": "manual-m",
  "observed_request_keys": ["max_tokens", "messages", "model", "stream"]
}
```

이 기존 Chat 프로필은 `response_format`을 보내지 않는 어댑터 경로를 사용했다. 관리자 계약을 프롬프트에 포함하고 응답의 실제 스키마 검증을 유지했다. 제공사의 강제 structured output이나 실제 모델 성능을 검증한 것으로 표시하지 않았다.

별도 통합 테스트는 주 성능 `BALANCED`와 고급 성능 `DEEP`의 충돌에서 `DEEP`를 저장하고 실제 모의 요청의 `reasoning_effort=high`를 확인했다. `provider_default`에서는 `temperature`를 보내지 않았다.

## 4. 기본값·우선순위·호환성

[기본값 목록](ui_start_unblock_defaults.json)은 실제 resolver에서 얻은 **43개 설정과 역할 선택·추론 하위 항목 8개**를 기록한다. UI 위치, 필수 여부, 값·타입·출처, 스키마 제약, 사용자 지정 여부, 적용 시점을 포함한다. 선택 모델과 USD 0.10은 검사 fixture의 명시 값이며 사용자의 실환경 설정을 추정한 값이 아니다.

우선순위는 `명시적 역할 > 고급 연구 > 주 화면 > 유효한 소유자 기본값 > 버전별 앱 기본값`이다. 권한·기능·무결성·실제 예산 한도가 결과를 제한한다.

| 항목 | 새 연구의 해석 |
|---|---|
| 제목 | 질문에서 로컬 생성 |
| 모델·필수 역할 | 승인된 선택 모델 / 같은 모델 상속 |
| 연구 성능·고급 성능 | BALANCED / inherit |
| 추론·sampling | 실제 프로필의 지원 기본값 / provider_default, 기존 명시 프로필 값은 보존 |
| 새 자동 프로필 출력·시간 | 1,024 tokens / 120초, 기존 프로필의 유효한 한도는 유지 |
| 과금 재시도 | 모호한 수락 자동 재시도 0 |
| 자료 | 빈 배열, CSV 선택 사항 |
| 웹 검색·시도 상한 | AUTO / 5, 소유자의 더 작은 한도 적용 |
| 문헌 없는 연구 중단 | true |
| 예산 자동 조정 | true, 지출 승인 한도는 증가하지 않음 |
| 시간·추가 분석 | 기존 300초 / 최대 1회 기본값, 정책의 더 작은 한도 적용 |
| 데이터 전송 | 서버 기본 none, UI 시작은 관리자 질문에만 selected 허용 |
| 보고서 | pdf 고정 |
| Skills·F3-P·Ridge | 기존 선택 유지, 누락 값 false |
| 저사양 설정 | 기존 AUTO, 연구 범위·추론과 별개 |

`false`, 검색 상한 `0`, 빈 첨부 배열, 선택적 reviewer의 `null`을 유지한다. 예산이나 질문·API 키를 임의로 만들지 않는다. 소유자 한도가 없으면 유한 한도가 한 번 필요하다.

새 UI와 자료 없는 요청은 설정 버전 2다. 버전 없는 기존 CSV 요청은 버전 1의 검색·추론 기본값을 유지한다. PDF는 다음 제출에서 정규화하고 과거 원문·manifest는 수정하지 않는다. 기존 실행 중 설정 변경의 적용 경계와 과학 의미 변경 승인도 유지했다.

## 5. 화면 수정과 시각 검토

- 선택한 체크박스 행을 순서대로 맨 위에 한 번만 표시한다.
- 접힌 목록은 미선택 5개, 펼친 목록은 미선택 제공사 그룹이며 `더보기/접기`는 마지막이다. 선택 행이 이동해도 focus를 유지한다.
- 별도 모델 등록 패널을 없앴다. 고급 모델 선택에는 실제 상속 모델 이름과 승인된 프로필 선택지가 표시된다.
- range 조작 높이 64px, track 16px, thumb 36px다. 아래에는 현재 단계 하나만 표시한다. Arrow/Home/End와 접근성 현재 값도 검사했다.
- 고급 사용자 지정 표시와 상속 초기화를 추가했다. 비용 설명 문단은 없애고 기능 중심 도움말을 남겼다.
- 연구 성능 제목 옆에 `낮음/중간/높음`을 초록/노랑/주황으로 표시한다. 기존 범위 정책의 상대 호출 부담이며 모델 단가나 정확도 점수가 아니다. 고급 연구 범위를 반영한다.

화면 구성은 [Linear의 UI 정리](https://linear.app/changelog/2024-03-20-new-linear-ui), 파일 행은 [Slack 파일 처리](https://slack.com/help/articles/201330736-Add-files-to-Slack), range는 [W3C Slider Pattern](https://www.w3.org/WAI/ARIA/apg/patterns/slider/)을 참고했다.

실제 화면 8개를 검토했다: `output/playwright/ui_start_unblock/01-defaults-desktop.png`부터 `08-start-flow.png`까지다. 1280×720, 390×844, CSS zoom 200%에서 가로 넘침과 브라우저 오류가 없었다. OS 브라우저 자체 확대 검증은 수행하지 않았다.

## 6. 파일·저장·읽기·삭제

| 대상 | 구현 범위 |
|---|---|
| 일반 다중 선택 | 여러 차례 추가, 같은 이름은 서로 다른 ATT ID |
| CSV | 기존 안전한 열·행 읽기; 내부 제한 추출은 처음 100행과 실제 행 수 |
| UTF-8 TXT·MD·JSON | 1MiB 이내 안전한 제한 읽기 |
| ZIP·임의 PDF·DOCX 등 | 원본 첨부 보관, 직접 분석 미지원 표시; 압축 해제·임의 파서 실행 없음 |
| 분석 입력 선택 | 고유 CSV 하나는 사용 가능; 여러 표의 자동 병합 없음 |

파일당 **5MiB**, 초안당 **20MiB·10개**, 업로드 저장소 **100MiB·활성 기록 500개**다. 64KiB 단위 전송으로 배치 전체를 RAM에 읽지 않는다. 이름·hash·parser 상태를 따로 보존한다.

- 인증·Origin·CSRF·전송 길이·할당량을 먼저 검사한다. 사용자 MIME을 신뢰하지 않는다.
- 실행 파일·HTML·SVG·활성 내용, 경로 이탈·symlink/junction, 실제 비밀·합성 canary를 차단한다. 경로와 제한은 [OWASP File Upload 지침](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)을 참고했다.
- 사용 전 삭제는 서버 저장 바이트까지 지운다. Chrome에서 삭제된 자료의 실제 서버 파일 0개를 확인했다.
- 사용 중 자료는 `RETAINED`로 남겨 provenance를 보호한다. 표시만 지우고 바이트를 삭제했다고 응답하지 않는다.
- 전송 중 취소는 tombstone을 확인하며 늦게 도착한 업로드가 다시 붙지 않는다. 실패 행도 제거할 수 있다. 시작 시 오래된 임시 자료를 제한된 범위에서 정리한다.

일반 문서의 제한 추출을 과학적으로 검증된 근거나 임의 파일 분석으로 승격하지 않는다. 사용자의 원본 파일은 지우지 않는다.

## 7. 문헌·검색

새 연구의 `search_required=true`는 관리자 계획 후 근거 수집 단계에 적용한다. 작은 연결 확인에는 적용하지 않는다. 검증된 기존 문헌은 hash·출처 확인 후 재사용하며 추가 검색 0회를 확인했다.

실제 HTTP 시도와 재시도는 기존 `control_configs`의 연구별 원자적 카운터 하나를 공유한다. 예약 확인과 실제 dispatch 전에 상한을 검사한다. 재시작 후 남은 횟수를 유지한다. 소유자·연구 한도의 최솟값을 사용한다.

상한 0/1/2, retry·429, 합산 상한, 재개, 기존 문헌 재사용을 검사했다. 상한이 끝나거나 근거가 없으면 실제 상태로 중단한다. 누락 문헌이나 인용을 만들지 않는다.

현재 Search는 `UNCONFIGURED`다. 기본 Crossref의 메타데이터만으로 적격 abstract 문헌이 생기지 않는다. 사용 가능한 검증 문헌을 수집해야 하며, 명시적으로 정책을 끌 수 있다. 자료가 없는 상태는 관리자 계획 경로까지만 실행하고 분석 자료 부족을 알린다.

## 8. PDF

새 로컬 `ReportLab` 렌더러를 기존 release 검증 함수와 연결했다. API `GET /api/control/research/{id}/report.pdf`는 진짜 PDF 바이트를 반환한다. 보고서 형식 선택은 없고 PDF 다운로드가 사용자 보고서 동작이다.

원문 Markdown/JSON·추적은 재현용 내부 산출물로 보존한다. 버전 2 release에는 `report.pdf`와 SHA-256을 포함한다. 기존 버전 1의 17파일 export도 통과했다.

| 사례 | 실제 상태 | 페이지 | 검사 |
|---|---|---:|---|
| normal | PARTIALLY_SUPPORTED / COMPLETED | 4 | PASS |
| inconclusive | INCONCLUSIVE / INSUFFICIENT_DATA | 4 | PASS |
| partial | INCONCLUSIVE / INSUFFICIENT_DATA | 2 | PASS |
| repaired | 실제 F3-P 재실행·재검증·commit 후 BUDGET_EXHAUSTED | 4 | PASS |

위 4종 **14페이지** 모두 PDF 구조·한글 추출·Unicode font 삽입·현재 상태 버전·출처 링크·artifact hash를 검사했다. 렌더링한 페이지를 직접 검토하여 긴 한국어 질문, 넓은 표, 그림에서 누락 글자·겹침·가로 잘림을 발견하지 않았다.

산출물: `build/ui_start_unblock/pdf_checks/d458a368658b43e78fb5284a53b2713a/`. 각 PDF의 전체 SHA-256과 페이지 경로는 로컬 `ui_start_unblock_results.json`에 기록했다. 개인 PC 경로가 포함된 이 원본은 공개·제출 대상에서 제외한다.

검증된 수치·그림만 사용하고 XML 문자를 escape한다. 보고서 내용 2MiB, PDF 8MiB, 그림 5MiB·16M pixels 한도를 적용한다. 현재 hash·상태를 렌더링 전후 다시 확인한다. 위조·오래된 보고서·비밀 포함은 차단한다.

Windows의 로컬 맑은 고딕을 사용했으며 font를 다운로드하거나 배포하지 않았다. 다른 OS의 적격 한글 font는 미검증이다. renderer/font가 없으면 정확한 PDF 오류를 반환하며 연결 확인을 막거나 가짜 PDF를 만들지 않는다.

## 9. 실제 검증 명령과 결과

저장소 루트 PowerShell에서 실행했다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -p no:cacheprovider --basetemp build/unblock-baseline-tests --junitxml build/unblock-baseline.xml
.\.venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_ui_start_unblock.py tests/test_ui_unblock_policy_files.py tests/test_ui_unblock_search_pdf.py -p no:cacheprovider --basetemp build/unblock-new-fixed --junitxml build/unblock-new-fixed.xml
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -p no:cacheprovider --basetemp build/unblock-final-regression --junitxml build/unblock-final-regression.xml
.\.venv\Scripts\python.exe -X utf8 -m probe.preflight validate-core
.\.venv\Scripts\python.exe -X utf8 qa/ui_unblock_offline_validation.py
.\.venv\Scripts\python.exe -X utf8 qa/ui_unblock_defaults.py
.\.venv\Scripts\python.exe -X utf8 qa/ui_unblock_pdf_checks.py
node qa/ui_unblock_browser.cjs
node qa/connection_ux_visual_qa.cjs
node qa/hardening_visual_qa.cjs
node qa/live_api_recording_visual_qa.cjs --settings-layout
node qa/live_api_recording_visual_qa.cjs
node qa/productization_visual_qa.cjs
node qa/multi_provider_visual_qa.cjs
.\.venv\Scripts\python.exe -X utf8 qa/gui_visual_fixture.py
.\.venv\Scripts\python.exe -X utf8 qa/product_visual_fixture.py
node qa/product_visual_qa.cjs
node --check src/probe/workbench_static/product_ux.js
.\.venv\Scripts\python.exe -X utf8 -m probe.preflight status
.\.venv\Scripts\python.exe -X utf8 qa/prepublish_check.py --output build/ui_start_unblock/prepublish.json
```

| 검사 | 관측 결과 |
|---|---|
| 수정 전 전체 | 847 PASS·5 SKIP·2 deselected / 529.24초 |
| 신규 집중 검사 | 44 PASS / 15.71초 |
| 최종 전체 | **891 PASS·5 SKIP·2 deselected / 746.47초** |
| Core | **891 PASS**, skipped=false, exit 0 |
| 파괴적 테스트 | **116 PASS / 191.719초**, 실행 가능한 19시나리오 PASS |
| Demo A / B | **각 5/5 PASS**, 중복 작업·미해결 참조 0 |
| 새 프로세스 crash/resume | PASS |
| F3-P 오류 사례 / 복구 경계 | **8/8 / 6/6 PASS**, 모두 기대한 차단·복구 결과 |
| 기존 export | 17파일, hash mismatch·secret 파일 0 |
| F3-P canary / clean export | 각 28+24파일, 합계 104파일, 이상 0 |
| 신규 버전 2 PDF export | 실제 PDF·manifest hash 검사 PASS |
| Chrome | 8회 실행 **311개 검사 PASS**, 외부·유료 요청·노출 0 |
| PDF | 4종·14페이지 구조·한글·출처·시각 검토 PASS |
| 합성 키 검사기 확인 | 고정 합성 / 실제 활성 값 / 미등록 값 분류 3/3 PASS |

전체의 SKIP 5개는 Docker 4개와 실제 Windows 키 저장 세션 부재 1개다. deselected 2개는 기존 기본 명령의 live_api/live_search opt-in 제외다. 기존 단위 테스트는 삭제·수정·추가 deselect하지 않았다.

F3-P 결과는 단일 복구·기존 복구 성공 2개만 검증 후 각 commit 1회다. 복합 오류·공유 오류는 REPAIR_INCOMPLETE, 불량 검사기는 CHECKER_QUALIFICATION_FAILED, 의미 변경은 REPAIR_CONTRACT_MUTATION_BLOCKED, 부족한 예산은 REPAIR_NOT_STARTED_BUDGET, 미지원 복구는 REPAIR_UNRESOLVED로 차단됐다. 6개 crash 경계는 각각 새 프로세스에서 commit 1회를 유지했다. 라이브 효능은 NOT_VALIDATED다.

의도적으로 바꾼 기존 화면 단언:

| 파일 | 변경 이유와 유지한 검증 |
|---|---|
| connection_ux_visual_qa.cjs | 별도 등록 패널 → 승인 연결 선택 즉시 프로필. 전체 단계 → 현재 단계. 더보기 위치·단일 행·키 삭제·CSRF·canary 유지 |
| hardening_visual_qa.cjs | 필수 CSV → 선택 자료, 다단계 시작 → 한 번 시작, 형식 선택 → PDF, 비용 문단 → 배지. CSV 양성 사례는 문헌 OFF를 명시. 전송 동의·인증·위험 차단 유지 |
| live_api_recording_visual_qa.cjs | 등록 패널 대신 실제 인증 API로 승인된 missing-key 검사 연결을 만든 후 선택. 기존 키 없음·한도·동의·노출 검사 유지 |
| productization_visual_qa.cjs | 같은 자동 프로필 경로. 기존 OS 미검증·키 오류·브라우저 보안 검사 유지 |
| multi_provider_visual_qa.cjs | 모델 상세 설정 버튼을 고급 영역으로 이동. 7제공사·주소 잠금·기능 선택·수동 ID 유지 |
| product_visual_qa.cjs | 기술 보고서 선택 → 친화적 미리보기·고정 PDF. 과거 기술 metadata 호환과 내부 검증 상태 유지 |

처음 재현 실패와 수정 중 fixture 실패 기록은 보존했다. UI 동작을 바꾼 단언만 교체했으며 보안 조건을 없애지 않았다. 검사 실행 시간 차이는 병행 실행·다른 작업 부하를 포함하여 성능 향상/저하 측정으로 쓰지 않았다.

## 10. 비용·보안·환경·출시 gate

실제 유료 API·모델 다운로드·계정/키 변경·GitHub push·게시 **0건**이다. 이번 명세의 실행 범위에 따라 기존 Git 상태와 관련 없는 작업을 보존했다.

OwnerSession, bootstrap 소거, Origin/CSRF, SecretStore, 목적지 승인, 유한 예산 예약/정산·미확정 비용, StateService Verify → Commit, F3-P 재검증·복구·출시 검증을 유지했다.

| 현재 gate | 실제 상태 |
|---|---|
| Core / stress / artifact / Demo A·B | PASS |
| Docker / Live LLM | NOT_VALIDATED |
| Live Search | UNCONFIGURED |
| 실제 Windows 안전 키 저장 | NOT_VALIDATED, 기존 WINDOWS_ERROR_1312 기록 유지 |
| OS 기본 브라우저 통합 | NOT_VALIDATED |
| Skills / F3-P 라이브 효능 | NOT_VALIDATED |
| demo_ready | **true** |
| release_ready / product_release_ready | **false / false** |

최종 보고 파일을 포함한 공개 후보 **293개**를 검사하여 의심 비밀 값 **0개**, 현재 Core 검증 PASS를 확인했다. 공개 준비 상태는 **BLOCKED: LICENSE_PENDING_OWNER_CHOICE**다. 기존 라이선스는 소유자 선택이 필요하며 자동 선택하지 않았다. 합성 canary는 직접 검토한 정확한 SHA-256 하나를 기존 분류 목록에 추가했다. 실제 활성 키와 같은 값은 여전히 P1로 차단하고 일반 키 탐지 규칙을 유지했다.

## 11. 변경 파일·기존 인프라·의존성·남은 문제

이번 작업 전의 파일 hash와 비교했으며 이전 저사양·연구 흐름 변경을 이번 변경으로 합산하지 않았다.

| 파일 | 구현 |
|---|---|
| src/probe/control_plane.py, resource_policy.py | 버전 2 기본값·원자적 검색 dispatch·UI 기본 정책 |
| src/probe/product_policy.py | 단일 resolver·안정된 자동 프로필·역할/추론 상속·계획 예산 |
| src/probe/workbench.py, service.py | 선택 입력·현재 초안·멱등 생성·업로드·PDF API |
| src/probe/research_settings.py | 기존 실행 중 설정 경계의 새 필드 호환 |
| src/probe/control_runtime.py, autonomous_loop.py | 계획 후 근거 검사·질문 전송 범위·실제 부족 상태 |
| src/probe/provider_checks.py | 고정 연결 확인·기존 세션 범위·ID 처리 |
| src/probe/scholarly.py, search_policy.py | 실제 HTTP 재시도까지 공유 상한·적격 문헌 재사용 |
| src/probe/input_upload.py | 제한 저장·읽기·실제 삭제·취소·provenance |
| src/probe/release.py, report_pdf.py | 기존 export 검사 재사용·로컬 PDF·현재 snapshot 검사 |
| src/probe/workbench_static/product_ux.js, workbench.css | 모델·고급·슬라이더·파일·도움말·PDF UI |
| tests/test_ui_start_unblock.py, test_ui_unblock_policy_files.py, test_ui_unblock_search_pdf.py | 신규 44개 통합·부정·복구·export 검사 |
| qa/ui_unblock_browser.cjs, ui_unblock_browser_fixture.py | 실제 Chrome·실제 서버·명시적 모의 HTTP |
| qa/ui_unblock_defaults.py, ui_unblock_pdf_checks.py, ui_unblock_offline_validation.py | 기본값·실제 PDF·기존 파괴적/반복/복구 검증 실행 |
| qa/connection_ux_visual_qa.cjs, hardening_visual_qa.cjs, live_api_recording_visual_qa.cjs, productization_visual_qa.cjs, multi_provider_visual_qa.cjs, product_visual_qa.cjs | 변경된 UX 단언과 기존 보안 회귀 |
| qa/prepublish_check.py, prepublish/usability_visual_validation.json | 정확한 합성 키 분류·화면 결과 |
| README.md, qa/README.md, docs/development/FEATURE_FREEZE.md, docs/history/연구일지.md | 사용법·승인된 수정 범위·실행 기록 |
| qa/ui_start_unblock_report.md, ui_start_unblock_defaults.json, ui_start_unblock_results.json | 한국어 보고서·비밀 없는 공개 증거 |
| pyproject.toml | PDF 실행·검사 의존성 |

기존 ControlStore의 설정·예산 원장·캐시, RoutingProfile·어댑터, StateService, 검색 정규화, 복구 runner, canonical 보고서·export hash 검사를 재사용했다. 새 설정 저장소·예산 원장·연구 엔진은 만들지 않았다.

**DB migration 없음.** 추가 의존성은 `reportlab>=4.4,<5`, test extra의 `pypdf>=5,<7`·`pypdfium2>=4,<6`이다. 실행 환경의 실제 설치 버전은 각각 **4.5.1 / 6.19.0 / 5.13.0**이다.

남은 제한은 실제 키·정확한 단가·승인된 검색 경로가 필요한 라이브 검사, Docker, Windows 저장 세션·기본 브라우저, 다른 OS의 한글 font다. 임의 PDF/DOCX 파서·다중 표 자동 병합은 제공하지 않는다. CSV 없는 계획의 성공을 완성된 과학 연구나 라이브 Agent 효능으로 기록하지 않는다.
