# 사용성·저사양·검색·GitHub 보안 보강 구현 보고서

기록: 2026-10-03 02:36:39 KST. 실제 기준선부터 실행했다. 기존 실행기·StateService·SQLite·비용 원장·출처 검증을 재사용한다. [최종 JSON](final_validation.json)

## 1. 사용 간편화

- 연구 설정에서 모델을 선택한다. 상단 5개·제공사별 더보기·직접 등록을 제공하고 일반 설정의 중복 모델·예산 입력을 통합했다.
- CSV는 파일 찾아보기로 선택한다. 무료 준비 검사에서 키·모델·가격·예산·전송·자료 문제와 수정 버튼을 표시한다. 준비 검사에는 유료 요청·연구 생성이 없다.
- 복구 가능한 오류·화면 재열기 후 질문·선택·파일을 유지한다. 연구 시작 후 환경설정 저장만 실패해도 완료한 시작을 실패로 표시하지 않는다.
- 성능 조작 높이 52px, 트랙 14px, 손잡이 30px로 확대했다. 아래에는 현재 추론 단계만 표시한다. low·medium·high·max 이름은 유지한다.
- 고급 설정은 기본 접힘, 역할은 최상위 1·중간 2·하위 1로 묶었다. 현재 고정 4역할 구조가 지원하지 않는 임의 증감은 제외했다.
- 검증은 분석 도구·Ridge·F3-P 순서로 짧은 설명·비용 영향을 표시한다. Ridge는 F3-P가 필요하다. 보고서 두 형식도 짧게 구분했다.
- 도움말은 주제 1개만 표시한다. 설정은 API 연결·API 검사·예산·성능 최적화·실행 환경으로 정리했다.
- 기본값은 medium·자동 검색·읽기 쉬운 보고서다. 모델·표시 선택만 기억하며 예산·전송 동의·추가 검증은 자동 재승인하지 않는다.

이전 클릭 수는 계측하지 않아 감소율을 제시하지 않는다. 중복 모델·예산 결정과 정상 제공사의 URL 입력을 제거했다. 실제 과금 실행에는 키·모델 응답 검사·단가·자료 전송 동의가 필요하다. [사용성 증거](usability_audit_ko.md)

## 2. 웹 검색 설정

| 정책 | 동작 |
|---|---|
| AUTO | 문헌·최신 출처가 필요한 질문에서만 검사 후 검색 |
| DISABLED | 검색하지 않음. 필수 문헌 목표와 충돌하면 차단 |
| ALLOWED | 필요한 검색을 허용하며 항상 검색하지는 않음 |

연구 정책은 생성 스냅샷에 저장하고 작업 정책은 이를 더 제한할 수 있다. 작업이 연구 DISABLED·필수 근거 조건을 우회하지 못한다. 기존 API는 기본 DISABLED, 새 UI는 AUTO다.

현재 연구 자료 전송 승인과 소유자가 작성한 공개 검색어가 필요하다. CSV·경로·키·내부 자료를 검색어로 자동 변환하지 않는다. 비밀 패턴·활성 키·개인정보가 있는 검색어/응답은 차단한다. 단가 미확인은 `PRICE_UNKNOWN`, 비용이 모호한 유료 전송은 `UNRESOLVED`로 유지하고 자동 재시도하지 않는다. 모델+검색 전체 시도 한도는 기존 값을 유지한다.

기존 Crossref 공개 메타데이터 제공사·LiteratureCoordinator를 재사용한다. 공식 무료 정책 확인이 유효할 때만 검색 단가 0이며 문헌을 모델에 입력하는 비용은 별도다. low/medium은 검색어 2개, high/max는 3개, 검색어당 최대 5개 결과, 직렬 실행이다. 예산 압박에서는 선택적 문헌 작업부터 줄이고 필수 검증은 보존한다.

VERIFIED 출처·메타데이터 해시·요청·24시간 freshness가 일치하는 기존 캐시만 재사용한다. 요청 ID·제공사·검색어 해시·승인 참조·결과 URL/출처 ID·메타데이터 digest·시각·지연·비용을 감사에 기록하고 export에 포함한다. pause/stop 경계를 요청 전과 검색마다 검사한다.

일반 뉴스 검색·임의 원문 다운로드는 구현하지 않았다. 실제 검색 HTTP는 이번 실행에서 수행하지 않아 Live Search는 미검증이다. [Crossref 무료 정책](https://www.crossref.org/services/metadata-retrieval/) · [접근·인증](https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/)

## 3. 저사양 최적화

| 항목 | 수정 전 | 수정 후 | 차이 |
|---|---:|---:|---:|
| Python 시작 (초) | 1.636 | 0.533 | -1.103 |
| 유휴 RAM (MiB) | 128.211 | 56.832 | -71.379 |
| 유휴 CPU (%) | 0.000 | 0.000 | +0.000 |
| 목록 조회 (ms) | 7.111 | 1.317 | -5.794 |
| 상세 조회 (ms) | 36.574 | 5.541 | -31.033 |
| 활동 조회 (ms) | 95.207 | 14.938 | -80.269 |
| 큰 활동 전체 3109행 (ms) | 1372.228 | 1235.840 | -136.388 |
| 큰 활동 화면 (ms) | 1372.228 | 12.244 | -1359.984 |
| Demo A 최고 RAM (MiB) | 165.195 | 164.207 | -0.988 |
| Demo A 최고 CPU (%) | 112.200 | 102.300 | -9.900 |
| Demo A 실행 (초) | 6.334 | 4.589 | -1.745 |
| 브라우저 준비 (초) | 1.942 | 0.854 | -1.088 |
| SQLite (바이트) | 831488.000 | 974848.000 | +143360.000 |
| trace (바이트) | 25582.000 | 25582.000 | +0.000 |
| workspace (바이트) | 99314.000 | 99314.000 | +0.000 |

각 항목은 단회 관측이다. 기준 측정 중 pytest가 실행되어 시간·CPU 간섭 가능성이 있다. 메모리·CPU는 Python 본체 기준이며 브라우저·별도 worker 합산이 아니다. CPU는 한 코어 100% 기준이다. 브라우저 준비는 실제 Chrome headless·루프백 인증이며 OS 기본 브라우저는 미검증이다.

큰 활동 화면은 전체 3109행에서 페이지 25행으로 바뀌었다. 원본 runtime event 3058개와 전체 API는 유지했다. 1372→12ms를 같은 전체 조회의 개선으로 해석하지 않는다. 동일 전체 API의 수정 후 값은 1235.84ms다. SQLite 인덱스로 DB 크기는 증가했고 trace·workspace 바이트는 동일했다.

새 프로세스의 단순 시작·목록에서 numpy/scipy/matplotlib/sklearn/pandas를 로드하지 않는 것을 검증했다. 숨긴 Timeline은 투영하지 않는다. 활동 페이지는 최대 100개·저사양 25개다. 정본·검증·비용·실패 기록을 삭제하거나 합치지 않는다. 완료·일시정지 시 polling을 멈추고 실행 중에만 10초마다 조회한다.

저사양 AUTO/ON/OFF를 제공한다. AUTO는 논리 CPU 4개 이하이며 저사양은 연구 시작을 하나씩 처리한다. 실제 `EXPLAIN QUERY PLAN` 확인 후 `idx_runtime_research_seq`를 추가해 SCAN→인덱스 SEARCH가 됐다. schema 6 유지·새 migration 0. 로컬 가중치 설치/로드 기능은 기존 구조에 없으며 추가하지 않았다. [실측 JSON](performance_before_after.json)

## 4. `.gitignore`

비밀·env·실행 DB·사용자 작업 공간·로그·build/output·개인 QA 결과·Live API 세션·가중치·가상환경·캐시·IDE 자료를 제외했다. 원본 자료를 삭제하지 않았다. 사전 inventory의 build 186081파일·약 12.1GB는 공개하지 않는다.

소스·문서·테스트·migration·schema·안전한 fixture·빈 `.env.example`·워크플로는 추적 가능하다. GitIgnoreSpec 등가 검사 13경로와 비밀/큰 파일/개인 경로/추적 처리 검사를 통과했다.

`.gitignore`는 이미 추적된 비밀을 제거하지 않는다. 현재 폴더는 Git 저장소가 아니므로 실제 tracked/history는 `NOT_VALIDATED`다. 빈 tracked_sensitive 목록을 실제 추적 비밀 0의 증거로 쓰지 않는다.

## 5. GitHub 공개 준비

**BLOCKED**: Git 저장소가 없어 실제 추적·이력을 확인할 수 없고 LICENSE는 소유자 선택 대기다. Git 초기화·커밋·push·LICENSE 임의 선택·이력 수정은 하지 않았다.

`python qa/prepublish_check.py`는 비파괴 검사다. `--require-ready`는 BLOCKED이면 exit 1이며 기본 실행은 의심 비밀·추적 민감 파일·필수 소스 누락이 없으면 exit 0이다. 기본 큰 파일 기준 10MB, `--max-bytes`로 변경한다. 공개 후보의 큰 파일·필수 소스 ignore 누락은 0이었다. 현재 소스의 Core가 실패·누락·stale이면 공개도 차단한다. 네 상태를 별도로 검사했다.

공개 후 Secret scanning·Push protection·Dependency graph·Dependabot alerts/security updates·CodeQL·dependency review·브랜치 규칙을 실제 GitHub에서 확인해야 한다. 현재 활성화했다고 주장하지 않는다. [공개 준비 안내](../../docs/development/SECURITY.md)

## 6. 비밀·보안 검사

공개 후보의 의심 실제 비밀 0, 확인된 신규 P0/P1 0이다. 패턴 P3 4개는 탐지 정규식·합성 개인 경로 fixture이며 실제 사용자 경로가 아니다. INFO 11개는 직접 검토한 고정 합성 키 10종의 출현이다. test/fake 등의 단어만으로 키를 예외 처리하지 않으며 미승인 5종 차단·마스킹을 확인했다. 보고서에 실제 비밀 값·개인 화면·청구 내역을 넣지 않았다. Git 이력은 미검증이다.

기존 Host/Origin/소유자 세션/CSRF/CSP·주소/redirect/SSRF·경로/심볼릭 링크·불신 HTML·stale commit·중복 호출·비용 예약 경계를 유지했다. CSV는 승인 입력 폴더 안에서 파일명·base64·UTF-8·열 구조·5MB·활성 비밀 검사를 거쳐 저장한다. 검색 결과로 예산·권한·도구 실행을 바꾸지 않는다. 검색 응답의 키는 캐시·정본·감사 본문 저장 전에 차단한다.

canary 포함 2개와 clean 2개 export, 총 104파일의 해시 불일치·누락·비밀·개인 절대 경로·미해결 참조는 0이었다. 브라우저 영구 저장소·화면·콘솔·로그 검사를 통과했다. 모의 OS 저장을 실제 Windows 검증으로 승격하지 않았다. [보안 상세](security_report_ko.md)

## 7. 의존성 보안

pip-audit 2.10.1로 실제 가상환경 84개 의존성의 알려진 취약점 0, exit 0이다. editable htrsa는 취약점 DB 조회에서 제외했다. 알려지지 않은 취약점이 없다는 뜻은 아니다.

런타임 의존성은 유지했다. test extra에 pathspec 1.1.1·PyYAML 6.0.3, security extra에 pathspec 1.1.1·pip-audit 2.10.1·Bandit 1.9.4·psutil 7.2.2·PyYAML 6.0.3을 추가했다. 실제 `pip install -e '.[security]'`가 통과했다.

pip/GitHub Actions에 월별 Dependabot 설정을 추가했다. 공식 dependency-review SHA, moderate 이상 차단, PR 댓글 없음이다. GitHub 실행은 CONFIGURED_ONLY. [의존성 결과](dependency_audit.json)

## 8. 코드 보안 검사

Bandit 1.9.4: **43개 경고, MEDIUM 18·LOW 25, 도구 오류 0, exit 1**. 경고를 숨기지 않았다. 기준선은 42개였으며 페이지 SQL 경고 2개 추가·runtime assert 경고 1개 제거다.

전부 직접 검토했다. SQL은 고정 구조·외부 값 바인딩, 실행은 shell 없는 argv다. PATH/로컬 실행 파일·Docker 신뢰는 실환경 전제다. Docker tmpfs 경고도 미검증으로 보존한다. 정본 반영 전 확정 상태 `assert`는 최적화 실행에서도 유지되는 명시적 예외로 수정했다. [경고·검토](code_findings.json)

CodeQL은 Python·JavaScript/TypeScript와 build-mode none으로 구성했다. GitHub에서 실행하지 않아 **CONFIGURED_ONLY / NOT_VALIDATED**다.

## 9. GitHub Actions 보안

공식 checkout·setup-python·dependency-review·CodeQL commit SHA를 당시 공식 저장소와 비교했다. contents read, checkout credentials 비지속, CodeQL 분석 job만 security-events write다. pull_request_target·write-all·PR 문자열 셸 삽입·제공사 키·유료 API 작업은 없다.

GitHub 실행·fork PR 권한·보안 설정은 실제 미검증이다. 로컬 YAML·권한·SHA·비밀 참조 검사는 통과했다. [워크플로 결과](workflow_findings.json) · [공식 지침](https://docs.github.com/en/actions/reference/security/secure-use)

## 10. 테스트

### 정확한 명령

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -p no:cacheprovider --basetemp build/hardening-baseline --junitxml build/hardening-baseline.xml
.\.venv\Scripts\python.exe -X utf8 qa/qa_day1.py --output-dir build/hardening-gates
.\.venv\Scripts\python.exe -X utf8 -m htrsa.preflight validate-core
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_hardening.py -q -p no:cacheprovider --basetemp build/hardening-last-search --junitxml build/hardening-last-search.xml
.\.venv\Scripts\python.exe -X utf8 qa/qa_day1.py --probe crash --root build/hardening-gates-final/fresh-process
.\.venv\Scripts\python.exe -X utf8 qa/qa_day1.py --probe resume --root build/hardening-gates-final/fresh-process
.\.venv\Scripts\python.exe -X utf8 qa/f3p_recovery_probe.py --all
.\.venv\Scripts\python.exe -X utf8 qa/f3p_export_probe.py --evaluation-dir build/hardening-f3p-evaluation
.\.venv\Scripts\python.exe -X utf8 qa/f3p_export_probe.py --clean --evaluation-dir build/hardening-f3p-evaluation
.\.venv\Scripts\python.exe -X utf8 -m pip_audit --skip-editable --cache-dir build/hardening-audit-cache -f json -o build/hardening-dependency-final.json
.\.venv\Scripts\python.exe -X utf8 -m bandit -r src -f json -o build/hardening-bandit-final.json -q
.\.venv\Scripts\python.exe -X utf8 qa/prepublish_check.py
.\.venv\Scripts\python.exe -X utf8 qa/prepublish_check.py --require-ready
```

| 검사 | 실제 결과 |
|---|---|
| 편집 전 기준선 | 731 PASS·5 SKIP·2 deselected / 콘솔 645.78초 |
| 전체 회귀 | 788 PASS·5 SKIP·2 deselected / 콘솔 622.90초·wrapper 624.582초 |
| 최종 동결 소스 Core | 788 PASS·SKIP 없음 / exit 0 |
| 신규 보강 | 57 PASS / 23.14초 |
| 공개 helper 변경 뒤 관련 검사 | 15 PASS·신규 중 관련 없는 42 deselected / 1.00초 |
| 최종 destructive | 116 PASS·0 SKIP·0 deselected / wrapper 229.710초 |
| destructive 시나리오 | 실행 가능 19/19 PASS·Docker 1개 NOT_VALIDATED |
| 최종 Demo A/B | 새 작업 공간에서 각각 5/5 PASS·논리 중복 0 |
| 최종 새 프로세스 crash/resume | PASS·실험/근거/commit/stats 각 2 |
| 기본 release | 17파일·해시/누락/비밀/미해결 참조 0 |
| F3-P 고장·복구 | 8/8 fixture 일치·6/6 새 프로세스 복구 |
| F3-P export | canary/clean 각 2종·총 104파일·검증 이상 0 |
| pip-audit | exit 0·84개·알려진 취약점 0 |
| Bandit | exit 1·경고 43개 보존·scanner 오류 0 |
| prepublish | 기본 exit 0·공개 BLOCKED, require-ready exit 1 |

최종 stress·Demo·release는 기존 `qa_day1.stress_suite()`, `demo_repeatability()`, `validate_release()`를 현재 소스로 재호출했다. 전체 회귀 이후 작은 수정이 있어 최종 Core를 독립 재실행했다. Core의 기존 제외 조건은 `not live_api and not live_search and not docker_integration and not os_secret_integration`이다.

전체 pytest의 실제 SKIP은 Windows 저장 세션 1개·Docker 4개다. 기존 Live API/Search 2개 제외를 유지했다. 기존 pytest 파일을 삭제·수정·완화하지 않고 신규 57개를 추가했다. UI QA는 승인한 화면 이동에 맞춰 선택자·경로를 바꾸되 동의·키·보안 검사를 보존했다.

기준선 645.78초가 기존 Core 600초 한도를 넘으므로 Core 대기 한도만 1200초로 늘렸다. 테스트·판정 조건은 같고 다른 API/Docker 한도는 600초다. 기존 QA wrapper의 고정 baseline 135는 새 기준선으로 사용하지 않았다.

F3-P는 기존 `qa/f3p_eval.py`의 `run_offline(Path('build/hardening-f3p-evaluation'))`를 실제 호출했다. 오답→정답 2·정답→오답 0·올바른 차단 6·잘못된 차단 0, 수리 성공률 0.5·미완료율 0.25다. 합성 FakeProvider 평가이며 토큰·실제 API 비용은 null, live efficacy는 NOT_VALIDATED다. Ridge는 기존 산술 검사 회귀에 포함되며 기본 OFF다.

### 실제 Chrome 명령

| 명령 | 실제 결과 |
|---|---|
| `node qa/hardening_visual_qa.cjs` | 41개 검사, exit 0 |
| `node qa/connection_ux_visual_qa.cjs` | 66개 검사, exit 0 |
| `node qa/product_visual_qa.cjs` | 19개 검사, exit 0 |
| `node qa/gui_visual_qa.cjs` | 22개 검사, exit 0 |
| `node qa/productization_visual_qa.cjs` | 21개 검사, exit 0 |
| `node qa/live_api_recording_visual_qa.cjs` | 33개 검사, exit 0 |
| `node qa/live_api_recording_visual_qa.cjs --settings-layout` | 70개 검사, exit 0 |
| `node qa/local_browser_auth_qa.cjs` | 51개 검사, exit 0 |
| `node qa/multi_provider_visual_qa.cjs` | 7개 검사, exit 0 |
| `node qa/cycle5_visual_qa.cjs` | 33개 검사, exit 0 |

합계 363검사. JS 오류·예상하지 않은 외부 요청·실제 유료 호출 0이다. 일부 검사는 모의 OS 저장소·오프라인 provider를 사용한다. 결과·화면 원본은 로컬에 보존했다.

## 11. 기존 검증 게이트

| 게이트 | 현재 상태 |
|---|---|
| CORE / STRESS / ARTIFACT | PASS |
| Demo A / Demo B | PASS |
| Docker 격리 | NOT_VALIDATED · Docker daemon/image 없음 |
| Live LLM | NOT_VALIDATED · API_UNCONFIGURED |
| Live Search | UNCONFIGURED / NOT_VALIDATED |
| Windows 키 저장 | NOT_VALIDATED · 모의 저장과 구분 |
| OS 기본 브라우저·WSF | NOT_VALIDATED |
| Skills/F3-P live efficacy | NOT_VALIDATED |
| demo_ready | true |
| release_ready | false |
| product_release_ready | false |

최종 소스 지문과 validation marker 일치를 확인했다. 오프라인 데모 통과를 실제 제공사·Docker·출시 통과로 바꾸지 않았다.

## 12. 남은 제한사항

- 공개에는 Git 추적/이력 검사와 LICENSE 선택이 필요하다. GitHub 기능·Actions 실행은 미검증이다.
- Windows 저장·기본 브라우저·WSF·Docker·Live LLM/Search 환경 조건이 남는다. 이번 실제 유료 요청 0이다.
- 검색은 Crossref 문헌 메타데이터다. 일반 뉴스·임의 원문 fetch는 없다. 휴리스틱 AUTO는 질문을 놓칠 수 있어 필수 근거와 공개 검색어로 명시한다.
- Crossref 무료 정책 확인은 30일 유효하며 만료 후 가격 미확인으로 차단한다. 무료 검색도 모델 입력 비용은 늘 수 있다.
- 저사양 AUTO는 CPU 수 기준이다. 단회·프로세스 범위 제한으로 일반적인 성능 향상률을 주장하지 않는다.
- 고정 4역할을 유지했다. 새 실행 조정기·검색 엔진·가중치 로더·광범위 결과 캐시는 추가하지 않았다.
- 입력·파일 복구는 같은 앱 세션에 한정한다. 브라우저 영구 저장소에 키·질문·CSV를 저장하지 않는다.
- 패턴·Bandit·pip-audit·CodeQL 설정은 모든 취약점/개인정보가 없음을 보장하지 않는다. 공개 자료 수동 검토가 필요하다.

### 변경 파일·재사용

새 runtime은 `input_upload.py`, `resource_policy.py`, `search_policy.py`다. 기존 workbench/control_plane/control_runtime/research_settings/live_api_test, scholarly/literature, release/dashboard/runtime/product_policy/preflight와 작업대 JS/CSS·인증 화면을 수정했다. 새 테스트·공개 검사·계측·Chrome helper, `.github/`·문서를 추가했다. [전체 파일 목록](final_validation.json)의 changed_files에 기록했다.

StateService 정본 쓰기·SQLite·문헌 Coordinator·VERIFIED 출처·Gateway·비용 원장/완료 예약·소유자 인증·검증/복구/export/소스 지문 게이트를 재사용했다. 새 DB migration·런타임 의존성·Agent 역할은 없다. 기존 사용자 작업과 데이터는 유지했다.
