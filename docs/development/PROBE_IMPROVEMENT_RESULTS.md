# Probe 개선 적용 결과

2026-10-05 기준으로 19개 코드 문제의 개선과 실행 환경 사전점검을 적용했다. 최종 소스에서 오프라인 테스트 1,612건이 통과했다. 실제 보고서 배치는 연결키 미등록으로 완료하지 못했으며, 새 부분 보고서와 항목별 실패 사유를 보존했다.

## 적용 범위

기존 미커밋 변경을 보존하고 필요한 부분만 수정했다. 화면 재설계는 수행하지 않았다. 단건 재작성 API를 유지하고 `probe.report_maintenance` CLI를 추가했다. 사용법과 상태 정의는 [보고서 일괄 재작성](REPORT_MAINTENANCE.md)에 기록했다.

| 번호 | 적용 내용 | 주요 구현 파일 |
|---|---|---|
| 1 | 자동 커밋 유지, 최상위 `BEGIN IMMEDIATE`·중첩 `SAVEPOINT`, 묶음 저장 실패 시 롤백 | `database.py`, `service.py`, `control_plane.py` |
| 2 | 데이터 무효화 전파, 종료 연구 재실행 방지, 수치 조회 시 원본·산출물 해시 재확인 | `service.py`, `research_slice.py`, `final_report.py` |
| 3 | 모든 표시 필드의 숫자·위치·분류·출처 검증, 관측 문장·단위 제한, 검증 버전 2 | `research_report.py`, `research_schemas.py` |
| 4 | 요청 키·실행 소유자·후보 수정본 저장, 완료 복구·미완료 중단, 불확실 비용 보존 | `research_report.py`, `workbench.py` |
| 5 | Ridge 결측값 보정에 훈련 평균만 사용, 훈련 열 전체 결측 차단 | `analysis_skills.py` |
| 6 | 회귀 표준오차 0에서 `slope_t=null`, `ZERO_STANDARD_ERROR` 기록 | `real_tools.py` |
| 7 | 지지·반박에 대응하는 검증 근거 요구, 중립 근거 의존 관계 유지 | `service.py`, `research_slice.py` |
| 8 | 분석 전 구조화 판정 기준 고정, 기준 없음·적용 불가·비유의 결과는 중립 | `research_schemas.py`, `service.py` |
| 9 | 최대 8개 요청 처리, 요청별 SQLite 연결, 시작 시 한 번 복구, 인증 잠금·본문 전 검사·읽기 제한 | `workbench.py` |
| 10 | 별도 계산 프로세스, 공통 마감 시각, 종료 확인 후 자원 반환, 종료 실패 시 차단 | `analysis_process.py`, `resource_queue.py`, `real_tools.py` |
| 11 | 실제 자식의 준비 확인 후 PID 확정, 준비 5초 제한, 명령·실행 상태 동시 실패 처리 | `workbench.py` |
| 12 | 일시적 원문 실패만 제한 재시도, 다운로드한 PDF 재사용, 추출 재시도 최대 2회 | `source_documents.py` |
| 13 | 원시 응답·64KiB 제한 압축 해제, 압축 전후 크기 확인, 미지원 압축 차단 | `bounded_http.py`, `scholarly.py`, `qualified_workflow.py` |
| 14 | HTML 깊이 128·기존 태그/크기 제한·2초 파싱 제한 | `web_sources.py` |
| 15 | 새 수정본 완성·파일 선언 검증 후 원자적 현재 포인터 교체, 조회·PDF·내보내기 일관성 | `report_publication.py`, `final_report.py`, `report_ux.py` |
| 16 | 임시 디렉터리 검증 후 내보내기 발행, 비어 있지 않은 대상은 `EXPORT_OUTPUT_NOT_EMPTY` | `release.py` |
| 17 | 바이너리 콘솔 합계 100,000바이트·파일 합계 10,000,000바이트, UTF-8 오류 구조화 | `sandbox.py`, `sandbox_io.py` |
| 18 | 제한된 `tmpfs`·회수 완료 확인·경로/링크/크기 검증·실패 정리, 미지원 환경 차단 | `sandbox_io.py`, `resource_queue.py` |
| 19 | Gemini 텍스트 파트 순차 전달, 최종 응답과 스트리밍 텍스트 일치 검사 | `providers/native.py`, `providers/streams.py` |
| 20 | 앱과 자식의 가상환경 실행기 일치, 의존성·자식 기동 사전점검 | `runtime_environment.py`, `preflight.py`, `source_documents.py` |

구현 파일은 `src/probe/` 아래에 있다. 회귀 시나리오는 `tests/test_reliability_v2.py`, `tests/test_reliability_boundaries.py`와 기존 분석·보고서·복구·서버 테스트에 포함했다. 저장 중간 실패, 데이터 변조, Unicode 숫자, 표본 수의 단위 바꾸기, 반대 방향 판정, 중지·시간 초과, 압축 폭탄, 깊은 HTML, 발행 실패, 비어 있지 않은 내보내기, 배치 재개·입력 변경·예산·정산을 검사했다. 샌드박스 코드 검증과 실제 Docker 검증은 구분했다.

## 검증 결과

| 검사 | 결과 | 범위 |
|---|---|---|
| 전체 오프라인 | **1,612 통과**, 실패·오류·건너뜀 0 | 55개 테스트 파일을 4개 프로세스로 분할 실행. 실제 API·검색·Docker·OS 비밀 저장 환경 검사 7건은 명시적으로 선택 제외 |
| Chrome 기존 보고서 흐름 | **44 통과** | 기존 API 호환 흐름, 모의 제공사 |
| Chrome 약한 근거 흐름 | **47 통과** | 부분 보고서·근거 부족 상태, 모의 제공사 |
| Chrome 현재 과학 흐름 | **83 통과** | 현재 기본 연구 흐름, 모의 제공사 |
| Chrome PDF 현재 수정본 | **18 통과** | 현재 포인터·재작성·PDF 조회 일치 |
| Windows CP949 | **2 통과** | UTF-8 모드를 끈 CP949 환경에서 PDF 제한·추출 실패 및 텍스트/스캔 구분 검사. 전체 Windows 환경 검증을 의미하지 않음 |
| 실제 배치 산출물 | **부분 보고서 검증 통과** | 검증 버전 2·선언·해시·4쪽 PDF 및 전체 페이지 시각 확인. AI 작성 완료를 의미하지 않음 |
| 실제 Docker | `NOT_VALIDATED` | Docker 실행기 없음. 실제 컨테이너 격리·`tmpfs` 검증 미실행 |
| 실제 API 품질·실시간 검색 | `NOT_VALIDATED` | 이번 작업에서 실제 품질 검사 미실행 |
| 공개 준비 검사 | `BLOCKED` | 핵심 회귀 검증 통과·의심 비밀 0건. `LICENSE_PENDING_OWNER_CHOICE`가 남음 |

최종 소스 지문은 `90051b62ba8e49551b7cc15b5c9a3cc0f5520d0bfba0ff11d55f6bc2121647c9`이며, 오프라인 실행 전후 일치했다. 결과는 다음 로컬 기록에서 확인한다.

- [전체 오프라인 결과](../../build/maintenance/implementation-cc7403f7d926/offline-final-53847a1f1abc4819ac20132f36f0c1d4/result.json): 각 실행의 로그·JUnit XML·테스트 파일 목록 포함
- [현재 핵심 검증 기록](../../build/validation/core.json)
- [기존 보고서 Chrome](../../build/research-report/browser/42d628b3-2c31-46b2-89c3-0ba2fb3a69f9/browser.json), [약한 근거 Chrome](../../build/research-report/browser/fa8c3e66-cb89-4996-97b3-2837615a1808/browser.json), [과학 흐름 Chrome](../../build/science/browser/eb3a2cc0-b45a-46a8-b5dd-0f6eb9469507/browser.json)
- [PDF 현재 수정본 Chrome](../../output/playwright/pdf-currentness/84a6d76c-a045-4593-88ef-05c496e6e25d/result.json)
- [CP949](../../build/maintenance/implementation-cc7403f7d926/cp949-final.json), [실제 부분 보고서 PDF 검증](../../build/maintenance/implementation-cc7403f7d926/pdf-verification.json), [공개 준비 검사](../../build/maintenance/implementation-cc7403f7d926/security-final.json)

## 실제 보고서 배치

배치 `BATCH-5caf8e602e6f4add84ca85ef13f63dba`는 기본 작업대에서 현재 조회 가능한 종료 연구 1건을 처리했다. 앞서 확인한 종료 연구 중 다른 1건은 현재 휴지통에 있어 `RESEARCH_IN_TRASH`로 제외했다. QA 결과와 과거 수정본은 대상에 포함하지 않았다.

| 연구 | 배치 항목 상태 | 확정 결과·해결 조건 |
|---|---|---|
| `R-8ea12b5769d444d4acbff94f7f8f9ad7` | `FAILED` | `CREDENTIAL_UNCONFIGURED`. 선택된 `gpt-6-luna` 연결키를 작업대 설정에 등록한 뒤 새 배치 필요 |
| `R-d26db61e731748b78653138113f8775e` | 제외 | `RESEARCH_IN_TRASH`. 휴지통 연구는 현재 배치 대상에서 제외 |

배치 상태는 **`PARTIAL`**이며 AI 재작성 완료는 **0건**이다. 실패 연구에는 `LOCAL_FALLBACK`의 **수정본 2·부분 보고서**를 새 디렉터리에 만들고 검증했다. 실제 관측값이 없어 정량 결론을 확정하지 않았으며 미검토 문헌은 주장 근거로 표시하지 않았다. 현재 포인터는 이 부분 보고서를 참조한다.

새 수정본 경로는 `build/workbench/workspace/R-8ea12b5769d444d4acbff94f7f8f9ad7/report_revisions/f3f07cacddfe4213a05f52cd91c0b655/`다. PDF는 4쪽이며 선언·해시·본문 추출과 Poppler 전체 페이지 검사를 통과했다.

실제 모델 요청은 전송되지 않았다. 비용 원장 14개 기존 항목과 원본 보고서 파일 28개가 그대로 유지됐고, 새 원장 항목·추가 정산 비용은 **0**이다. 데이터셋·원문·근거·실험 수에도 변경이 없다. [배치 결과](../../build/maintenance/implementation-cc7403f7d926/batch-result.json)와 [보존 감사](../../build/maintenance/implementation-cc7403f7d926/batch-audit.json)에 항목별 사유와 해결 조건을 기록했다.

연결키 등록 후 가격·남은 연구 예산·월간 한도·전송 동의·미확정 비용을 다시 확인하고 아래 명령으로 새 배치를 만든다. 기존 `FAILED` 항목은 `--resume`만으로 재전송하지 않는다. API 키는 작업대의 기존 비밀 저장 흐름을 사용한다.

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m probe.report_maintenance --database build/workbench/state.sqlite --workspace build/workbench/workspace --all --dry-run
.\.venv\Scripts\python.exe -B -X utf8 -m probe.report_maintenance --database build/workbench/state.sqlite --workspace build/workbench/workspace --all
```

## 보존과 복구

작업 전 기준점은 `build/maintenance/implementation-cc7403f7d926/`에 보존했다. `source_hashes.json`의 기존 617개 파일 해시, `existing_changes.diff`의 기존 Git 차이, `source-before/`, `state-before.sqlite`, `workspace-before/`를 유지했다. 실제 배치 직전에는 `state-before-batch.sqlite`와 `batch-before.json`을 추가로 보존했다.

실패한 배치를 완료로 표시하거나 실제 API·Docker 검증으로 승격하지 않았다. 이전 데이터·보고서·비용 원장을 되돌리지 않았다. 추가 적용 실패 시 새 발행을 중단하고 보존 산출물과 실패 기록으로 확인하며, 비용 원장은 과거 백업으로 덮어쓰지 않는다.
