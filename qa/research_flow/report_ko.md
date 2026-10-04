# 통합 연구 흐름 구현·검증 보고서

## 1. 구현 요약

기존 앱에 `연구 흐름` 탭을 추가했다. 현재 경로·전체·복구·검증의 네 보기를 제공하며 노드를 선택하면 오른쪽에 상세 내용이 열린다. 별도 과학 상태를 만들지 않고 저장된 연구 기록을 읽는다.

## 2. 실제 자료 연결

기존 연구·계약·모델 호출·도구 실행·가설·dataset·문헌·실험·근거·검증·F3-P·주장·결론·보고서·checkpoint·결정 기록을 연결한다. 모델 정보가 없는 도구 실행을 모델 호출로 표시하지 않는다. 문헌 검사와 기존 Cycle5 요약도 보존했다. 저장 당시 요약과 현재 재검증 결과는 구분한다.

## 3. 노드와 역할

관리·자료·실험·검증·도구의 다섯 역할 구역을 고정 순서로 배치한다. 역할 구역은 실제 동시에 실행 중인 Agent 수를 뜻하지 않는다. 실제 저장된 모델 실행만 펼쳐 볼 수 있다. 출처, 지원·반박·중립 근거, 무효화와 새 revision의 대체 관계를 각각 구분한다.

## 4. 현재 경로

실행 중인 기록, 현재 오류, 결론과 채택된 근거를 기준으로 관련 선행 기록과 인접 기록을 표시한다. 실패·예산 부족이 있으면 이전의 성공 근거 때문에 완료로 표시되지 않는다. 과거 상태와 현재 상태를 구분하고 저장되지 않은 진행률이나 계획 버전을 만들어 표시하지 않는다.

## 5. F3-P 실패와 수정

원래 실패 → 실제 수정 결정 → 새 revision → 필요한 재검증 → 검증된 확정 결과를 연결한다. 원래 계약·원래 결과·verifier의 세 부분을 상세 화면에서 나누어 확인할 수 있다. unresolved·checker fault·예산 부족·semantic mutation 차단은 완료로 표시하지 않는다. 직접 파일 및 의존 파일 변조는 기존 무결성 검사로 확인한다.

오프라인 평가 8개 fixture는 모두 기대 결과를 만족했다. wrong-to-right 2, right-to-wrong 0, correctly blocked 6, incorrectly blocked 0이었다. 예산 미완료·checker fault·semantic mutation 차단·unresolved는 각각 1건이었다. 4개 시도 사례 기준 repair success rate는 0.5, incompletion rate는 0.25다. 이는 Fake/typed tool 환경의 정확성 결과이며 Live Agent 효능은 NOT_VALIDATED다.

## 6. 상세 지연 조회

첫 그래프 응답에는 메타데이터만 포함한다. 선택한 노드에서만 artifact hash·검증·dataset 최대 5행·분석 결과·안전한 이미지·보고서를 조회한다. 기존 보고서 유효성 검사를 유지한다. 닫을 때 상세 DOM을 정리한다. 프롬프트 원문·추론 원문·키·hidden oracle은 노드 응답에 포함하지 않는다.

## 7. 갱신 방식

CSS와 SVG를 사용한다. 첫 진입 때만 흐름 스크립트를 불러온다. 일반 모드 120개, 저사양 모드 60개로 화면 노드 수를 제한한다. 구조가 같은 상태 변경에서는 기존 위치와 DOM을 유지한다. 화면이 보이고 실행 중일 때 10초, 일시 중지일 때 60초 주기로 갱신하며 완료된 연구는 자동 갱신하지 않는다.

기존 구성 참고: [GitHub Actions 실행 그래프](https://docs.github.com/en/actions/how-tos/monitor-workflows/use-the-visualization-graph), [Carbon 진행 표시 지침](https://www.carbondesignsystem.com/building-blocks/core/components/progress-indicator/guidelines). 의존 관계와 상태를 명시하고 근거 없는 진행 백분율은 넣지 않았다.

## 8. 성능 근거

939개 노드와 최소 596개 관계를 가진 실제 저장소 fixture를 사용했다. 합성 이력은 무효 실험·무효 근거로 생성했다. 현재 보기 15개 노드·30개 관계, 전체 보기 첫 페이지 최대 120개 노드를 표시했다. 초기 표시 92.882 ms, 전체 보기 110.9914 ms, 실제 상태 변경 반영 92.7842 ms, 현재 보기 전환 47.481 ms였다.

60.014382초 대기에서 Chrome renderer CPU 0.045119%, JS heap 2.459991 MiB를 관측했다. 이 수치는 전체 브라우저 RSS, GPU 메모리 또는 흐름 기능만의 추가 메모리를 뜻하지 않는다. backend는 전체 메타데이터에 대해 O(N+E)로 처리하며 이 장치에서 검증한 최대 규모는 위 fixture다.

## 9. 접근성 검증

실제 Chrome에서 방향키·Home·End·Enter, 보이는 focus, 접근성 이름, reduced motion, 390px 화면, 실제 CSS 200% 확대를 확인했다. 노드 선택과 상세 닫기, 모드 전환, 과거 이력, 현재 경로, F3-P 세 부분, 상태 변경 시 위치 보존을 검사했다. 새 흐름 31개 검사와 대형 흐름 10개 검사가 통과했다.

## 10. 실제 검증 명령

| 명령 | 결과 |
| --- | --- |
| `.venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_multi_provider.py tests/test_execution_flow.py -p no:cacheprovider --basetemp build/flow-local-limit-tests --junitxml build/flow-local-limit-tests.xml` | 209 passed / 44.42초 |
| `.venv\Scripts\python.exe -X utf8 qa/flow_offline_validation.py` | 파괴 116 passed, Demo A/B 각 5회 PASS, 새 프로세스 복구 PASS, release 17개 파일 PASS |
| `.venv\Scripts\python.exe -X utf8 qa/flow_browser_regression.py` | 기존 화면 10개 스크립트, 363개 검사 PASS |
| `node qa/flow_visual_qa.cjs` | 31개 검사 PASS |
| `node qa/flow_large_qa.cjs build/flow-large-final-fixture` | 10개 검사 PASS |
| `.venv\Scripts\python.exe -X utf8 qa/f3p_recovery_probe.py --all` | 새 프로세스 복구 경계 6개 PASS |
| `.venv\Scripts\python.exe -X utf8 qa/f3p_export_probe.py --evaluation-dir build/flow-final-f3p-v3` | canary 52개 파일, hash·참조·secret 이상 0 |
| `.venv\Scripts\python.exe -X utf8 qa/f3p_export_probe.py --clean --evaluation-dir build/flow-final-f3p-v3` | clean 52개 파일, hash·참조·secret 이상 0 |
| `.venv\Scripts\python.exe -X utf8 -m pytest -q -p no:cacheprovider --basetemp build/flow-final-regression-v3 --junitxml build/flow-final-regression-v3.xml` | 845 passed, 5 skipped, 2 deselected / 807.97초; 전역 로컬 한도 추가 전 |

기계 결과는 `qa/performance/low_spec/`와 기존 `qa/results/f3p_*results.json`·`f3p_export_validation.json`, 화면 결과는 `output/playwright/flow/result.json`에 저장했다. F3-P 평가·6개 복구 경계와 31개 흐름 화면 검사는 전역 로컬 한도 수정 이후 다시 실행해 통과했다. CORE 명령 `.venv\Scripts\python.exe -X utf8 -m htrsa.preflight validate-core`는 최종 847 passed, skipped=false였다. 기존 363개 화면 검사는 마지막 로컬 한도 수정 전에 실행했으며 화면 코드는 이후 변경하지 않았다.

## 11. 기존 구조와 회귀 수정

새 서버 모듈은 `research_flow.py`, `workbench_pages.py`, `resource_queue.py`이며 화면 모듈은 `workbench_static/research_flow.js`다. 기존 `workbench.py`, `dashboard.py`, `control_runtime.py`, `control_plane.py`, `resource_policy.py`, `agent_runtime.py`, `real_tools.py`, `providers/native.py`, 화면 CSS·JS와 Cycle5 요약을 필요한 부분만 수정했다. `tests/test_execution_flow.py`에 59개 검사를 추가했다.

기존 schema·ledger·audit·StateService·artifact hash·OwnerSession·Origin/CSRF/CSP·예산 예약과 정산·복구·release gate를 재사용했다. 새 의존성이나 과학 DB 마이그레이션은 없다. 기존 운영 초기화에 조회 인덱스 1개를 추가했다. 모델 감사 이벤트의 resolved model ID 누락을 수정했고 기존 provider 회귀 검사는 그대로 통과했다. 서로 다른 로컬 서버 간에도 추론 1개 한도를 적용하며 대기 중 비용 예약·요청이 없는 두 모드 검사를 추가해 신규 검사는 총 59개다. 기존 보고서와 이미지 유효성 검사도 유지했다.

## 12. 제한과 남은 검증

원래 구조에 계획 버전이 없으므로 `plan_version`은 null이다. 작업 참조가 없는 예산 이벤트는 해당 연구 범위에만 연결한다. 독립된 최근 이벤트 띠와 발표 모드는 선택 범위에서 제외했다. 전체 대형 그래프 메타데이터를 서버에서 읽는 비용은 남아 있다.

Docker·Live LLM·Live Search·Live F3-P 효능·실제 로컬 GPU·실제 저사양 장치·OS 기본 브라우저 실행은 새 검증 없이 통과로 기록하지 않는다. headless Chrome 검증은 OS 기본 브라우저 실행 검증이 아니다. Windows 실행 계정 오류 1909로 중단된 새 명령 실행은 복구됐으며 후속 검증을 재개했다. 최종 CORE·export·보안 상태는 마지막 결과를 기준으로 한다. 로컬 자원 한도는 같은 앱 DB 범위이며 다른 DB·외부 프로그램을 제어하지 않는다.


최종 gate: **demo_ready=true, release_ready=false, product_release_ready=false**. Docker는 NOT_VALIDATED, Live LLM은 NOT_VALIDATED, Live Search는 UNCONFIGURED다. OS 기본 브라우저·Windows 키 실환경은 NOT_VALIDATED, Skills/F3-P Live 효능은 NOT_VALIDATED다. 최종 기본 release 17개, F3-P canary·clean 104개 파일을 검증했고 hash·누락·참조·secret canary 이상은 0이었다. 최종 후처리 명령은 `.venv\Scripts\python.exe -X utf8 qa/performance/low_spec/finalize_validation.py`다.

공개 후보 최종 보안 검사: 281개 파일, 의심 비밀값 0. Git·이력 239개 검사 VALIDATED, 현재 CORE PASS. 공개 BLOCKED의 남은 사유는 LICENSE_PENDING_OWNER_CHOICE다. 기존 합성 경로 검출 P3 8개와 합성 키 INFO 22개를 보존했다. dependency·Bandit의 별도 재감사는 이번 변경에서 수행하지 않았다.
