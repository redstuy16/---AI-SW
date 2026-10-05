# 저사양 실행 최적화 구현·검증 보고서

## 1. 기존 실행 기준

수정 전 전체 테스트는 `788 passed, 5 skipped, 2 deselected`였으며 실행 시간은 502.28초였다. 측정 원본은 `baseline.json`에 보관했다.

## 2. 확인한 병목

연구 목록과 검증·실험 목록에서 반복 조회가 확인됐다. 목록에는 메타데이터만 조회하고 상세 내용은 선택할 때 읽도록 변경했다.

## 3. 구현 범위

실행 자원 대기열, 실행 모드, 목록 페이지 처리, 상세 지연 조회, 사용량 집계, 디스크 상태 표시, 분석 그림 정리를 구현했다. 시간 절감을 확인한 일괄 목록·verifier·실험 조회는 KEEP, 시작 import·유휴 CPU는 NO_MEANINGFUL_GAIN으로 판정했다. 고정 fixture에서 과학 결과·상태의 동등성을 검사했으며 새 캐시는 없다.

- 신규 실행 모듈: `src/probe/resource_queue.py`, `src/probe/workbench_pages.py`. 흐름 모듈과 화면 변경은 연구 흐름 보고서에 함께 정리했다.
- 기존 실행 경로: `resource_policy.py`, `control_runtime.py`, `real_tools.py`, `agent_runtime.py`, `providers/native.py`, `control_plane.py`, `dashboard.py`, `workbench.py`.
- 화면: `workbench_static/workbench.js`, `product_ux.js`, `workbench.css`, `cycle5_ui.js`. 기존 API·연구 설정·성능 슬라이더는 유지했다.
- 검증: `tests/test_execution_flow.py` 59개, `qa/performance/low_spec/{probe.py,browser_probe.cjs,bundle_probe.cjs,compare.py}`, `qa/flow_offline_validation.py`, `qa/flow_browser_regression.py`, `qa/flow_visual_fixture.py`, `qa/flow_visual_qa.cjs`, `qa/flow_large_probe.py`, `qa/flow_large_qa.cjs`.

자원 한도는 다른 앱 DB·외부 프로그램까지 제어하지 못한다. 화면 페이지 요약은 상세 재검증을 대신하지 않는다. 이 범위를 안내하고 실제 hash·예산·출처 검사는 기존 경로로 실행한다.

## 4. 제외 범위

새 분석 캐시, 별도 과학 상태 저장소, 프로세스 풀, 프레임워크 교체는 도입하지 않았다.

## 5. 실행 모드

`AUTO`, `LOW_SPEC`, `NORMAL`을 지원하며 기존 `ON`·`OFF` 설정도 읽는다. AUTO는 로컬에서 CPU와 RAM을 모두 확인할 수 있을 때 CPU ≤4 또는 RAM ≤8GiB이면 LOW_SPEC, 정보가 부족하면 NORMAL을 선택한다. LOW_SPEC은 기본 조회 25개·화면 흐름 노드 60개·전체 원격 요청 1개, NORMAL은 기본 조회 50개·화면 흐름 노드 120개·전체 원격 요청 최대 4개다. 실행 모드는 연구 low / medium / high / max 및 검증·예산·외부 통신 설정과 별개다.

## 6. 로컬 실행 정책

같은 앱 DB에서 서로 다른 로컬 서버를 포함한 추론 전체와 무거운 분석은 각각 1개씩 실행한다. 같은 원격 서버도 1개로 제한한다. 요청은 자원 확보 후 기존 예산 예약·호출 경로를 따른다. 서버나 모델 가중치를 앱이 자동 실행·종료하지 않는다. 대기열은 활성 작업 32개, 종료 표시 메타데이터 128개로 제한하며 원래 감사·비용 기록은 보존한다.

기존 운영 설정·감사 기록과 BEGIN IMMEDIATE로 여러 프로세스의 자원을 원자적으로 확보한다. 기다리는 동안 비용을 예약하거나 요청을 전송하지 않으며 취소·timeout·dead owner를 구분한다. 살아 있는 PID를 시간 경과만으로 회수하지 않는다. OOM은 RESOURCE_EXHAUSTED로 구분하고 동일 요청 반복이나 cloud 전환을 자동 수행하지 않는다. 이미 전송한 요청의 불확실한 비용은 UNRESOLVED로 보존한다.

## 7. 화면 변경

목록을 페이지로 나누고 상세 내용·검증·보고서·그림은 선택할 때 불러온다. 보고서와 그림은 기존 artifact를 읽고 hash·상태 유효성 검사를 유지한다. 연구 흐름 화면은 처음 열 때 로드하며 보이는 실행 중 연구는 10초, 일시 중지는 60초, 완료는 자동 갱신 없음으로 처리한다. 기존 문헌·Cycle5 판정을 유지하고 저장 당시 요약과 현재 상세 재검증을 구분한다.

## 8. 서버 변경

기존 자료형과 저장소를 재사용했다. 의존성 및 과학 상태 DB 마이그레이션은 추가하지 않았으며 migration 6개를 유지한다. 기존 운영 초기화에 `idx_ui_evidence_experiment(research_id,experiment_id)` 인덱스 1개를 추가했고 EXPLAIN에서 사용을 확인했다. 사용량 목록만 페이지로 나누며 예산 exposure 합계는 전체 원장 SQL로 계산한다.

시작 경로의 numpy·scipy·matplotlib·pandas·sklearn·statsmodels import는 수정 전후 모두 0개였다. 기존 지연 import를 유지했다. 시작 시 provider/model/API 검사를 추가하지 않았다. disk 여유 공간 512MiB 이하 경고와 검사 시각을 표시하되 자동 삭제하지 않는다. 상세에서는 실제 파일을 검증하고 과학 판정에 mtime 캐시를 사용하지 않는다.

## 9. 수정 전·후 실측

중앙값과 표본의 최소–최대를 함께 표시한다. 차이는 수정 후−수정 전이다. CPU는 한 코어 100% 기준이며 Demo RSS는 Python과 자식 프로세스 합계다. peak는 100ms 표본으로 짧은 최고점을 놓칠 수 있다.

| 항목 | 수정 전 | 수정 후 | 차이 |
| --- | ---: | ---: | ---: |
| 새 프로세스 시작 3회 (ms) | 523.490 (513.553–545.734) | 488.542 (484.345–587.598) | -34.948 |
| 서버 준비 3회 (ms) | 523.490 (513.553–545.734) | 488.542 (484.345–587.598) | -34.948 |
| Chrome 준비 3회; 조회 범위 다름 (ms) | 890.910 (882.227–1045.308) | 720.478 (711.786–734.823) | -170.433 |
| Python 본체 60초 유휴 RAM (MiB) | 56.287109 | 56.505859 | +0.219 |
| Python 본체 60초 유휴 CPU (%) | 0.000000 | 0.000000 | +0.000 |
| 유휴 프로세스 수 (개) | 1 | 1 | +0.000 |
| 동일 전체 연구 목록; 최초 제외 5회 (ms) | 73.189 (72.873–73.821) | 68.777 (67.837–71.134) | -4.411 |
| 동일 활동 API; 최초 제외 5회 (ms) | 7.990 (7.527–12.404) | 8.489 (8.390–8.640) | +0.499 |
| 동일 전체 검증 API; 최초 제외 5회 (ms) | 124.869 (122.544–132.010) | 83.140 (82.027–84.162) | -41.729 |
| 동일 전체 실험 API; 최초 제외 5회 (ms) | 287.583 (286.501–288.495) | 191.760 (187.901–194.643) | -95.824 |
| 동일 전체 근거 API; 최초 제외 5회 (ms) | 486.656 (474.777–493.536) | 471.496 (470.652–477.402) | -15.160 |
| 동일 보고서 API; 최초 제외 5회 (ms) | 1.477 (1.377–1.873) | 1.690 (1.446–1.799) | +0.213 |
| 동일 fixture DB (MiB) | 2.539062 | 2.554688 | +0.016 |
| 동일 fixture trace (MiB) | 0.054878 | 0.054878 | +0.000 |
| 동일 fixture workspace (MiB) | 0.220862 | 0.220862 | +0.000 |
| Demo peak RSS (MiB) A; 각 3회 | 158.504 (158.320–158.754) | 159.125 (158.613–159.148) | +0.621 |
| Demo peak RSS (MiB) B; 각 3회 | 160.195 (160.062–160.699) | 160.637 (160.172–160.840) | +0.441 |
| Demo peak CPU (%) A; 각 3회 | 109.100 (103.400–114.900) | 108.600 (106.300–115.600) | -0.500 |
| Demo peak CPU (%) B; 각 3회 | 103.600 (99.100–113.000) | 105.300 (104.000–110.800) | +1.700 |

전체 연구 목록 SQL은 1,447→19회, 검증 API는 604→8회, 실험 API는 1,805→312회였다. 데이터 수와 기존 판정을 유지하며 반복 조회를 줄였다. 동일 API에서 검증 124.869→83.140ms, 실험 287.583→191.760ms를 관측했다. 시작·유휴·Demo 메모리의 유의미한 개선은 입증하지 않았다. 상세·활동·보고서의 작은 지연 증가는 표에도 그대로 기록했다.

화면 첫 페이지 API는 결과 범위가 달라 전체 API와 개선율로 비교하지 않는다. Chrome 기준선 본문 크기는 비동기 수집 누락 가능성이 있어 전송량 개선 근거로 쓰지 않았다. 캐시를 끈 별도 CDP 관측은 수정 후 12개 응답·헤더 포함 165,869 bytes이며 동등한 수정 전 전송량은 NOT_VALIDATED다.

939개 노드 대형 흐름에서 초기 표시 92.882ms, 전체 첫 페이지 110.9914ms, 실제 상태 변경 92.7842ms, 현재 보기 전환 47.481ms였다. 60.014382초 대기 중 renderer CPU 0.045119%, JS heap 2.459991MiB였다. 이는 전체 Chrome RSS·GPU 또는 흐름 기능만의 추가 메모리가 아니다. 대형 흐름 측정 이후 모델 감사·전역 로컬 한도를 수정했으며 그래프/UI 코드는 그대로다.

원본은 baseline.json·after.json·comparison.json·browser_baseline.json·browser_after.json·bundle_after.json·flow_large.json이다. 마지막 cold 첫 표본 일부는 관련 테스트의 끝부분과 겹쳤으며 이후 두 cold 표본과 목록·Demo 측정에는 해당 테스트가 끝난 상태였다. 따라서 시작 시간 개선을 단정하지 않는다. OS 파일 캐시는 초기화하지 않았다. 실제 저사양 장치·GPU·로컬 모델을 측정하지 않았다.

## 10. 실제 검증

| 검사 | 실제 명령 | 결과 |
| --- | --- | --- |
| 수정 전 전체 | `.venv\Scripts\python.exe -X utf8 -m pytest -q -p no:cacheprovider --basetemp build/flow-opt-baseline-tests --junitxml build/flow-opt-baseline.xml` | 788 passed, 5 skipped, 2 deselected / 502.28초 |
| 새 기능·기존 provider 회귀 | `.venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_multi_provider.py tests/test_execution_flow.py -p no:cacheprovider --basetemp build/flow-local-limit-tests --junitxml build/flow-local-limit-tests.xml` | 209 passed / 44.42초 |
| 파괴·반복·복구·export | `.venv\Scripts\python.exe -X utf8 qa/flow_offline_validation.py` | 파괴 테스트 116 passed / 190.962초, Demo A/B 각 5회 PASS, 새 프로세스 복구 PASS, release 17개 파일 PASS |
| 기존 Chrome 화면 | `.venv\Scripts\python.exe -X utf8 qa/flow_browser_regression.py` | 기존 스크립트 10개, 363개 검사 PASS; JS·CSP·외부 통신·유료 호출 오류 0 |
| 새 Chrome 흐름 | `node qa/flow_visual_qa.cjs` | 31개 검사 PASS |
| 대형 Chrome 흐름 | `node qa/flow_large_qa.cjs build/flow-large-final-fixture` | 10개 검사 PASS |
| 전역 로컬 한도 추가 전 전체 회귀 | `.venv\Scripts\python.exe -X utf8 -m pytest -q -p no:cacheprovider --basetemp build/flow-final-regression-v3 --junitxml build/flow-final-regression-v3.xml` | 845 passed, 5 skipped, 2 deselected / 807.97초 |
| CORE gate | `.venv\Scripts\python.exe -X utf8 -m probe.preflight validate-core` | 최종 전체 오프라인 CORE 847 passed, skipped=false |

중간 전체 검사에서는 provider replay의 마지막 감사 이벤트에 모델 ID가 없는 문제로 7 failed, 838 passed, 5 skipped, 2 deselected가 발생했다. 실행 자원 완료 기록에 실제 `resolved_model_id`를 기록하도록 수정했고 관련 209개 검사를 통과했다. 기존 테스트는 수정하지 않았다. 그림 저장의 OSError·MemoryError를 주입한 4개 검사에서는 생성한 Matplotlib 그림을 닫고 성공 결과를 확정하지 않는 것을 확인했다.

재현용 성능 명령: `.venv\Scripts\python.exe -X utf8 qa/performance/low_spec/probe.py --output qa/performance/low_spec/after.json`. 대형 fixture 준비 명령: `.venv\Scripts\python.exe -X utf8 qa/flow_large_probe.py --prepare --folder build/flow-large-final-fixture`.

## 11. 기존 검증 보존

기존 테스트를 변경하지 않았다. NORMAL과 LOW_SPEC에서 같은 FakeProvider 연구의 실험·근거·확정 상태가 동등한 것을 검사했다. 연구 성능 단계 및 Skills/F3-P/검증·egress 설정도 실행 모드 변경으로 바뀌지 않는다. 분석·출처·복구·예산·보안·release 기준을 유지했다. 실제 별도 프로세스 자원 대기·dead owner, 실제 HTTP 인증·foreign Origin 차단, F3-P 직접/의존 파일 변조, 수정 예산 부족·checker fault·semantic mutation 차단을 검사했다. 실행하지 않은 Docker·실제 API 검증을 통과로 기록하지 않는다.

## 12. 제한 사항

측정 장치는 논리 CPU 20개, RAM 약 47.8 GiB의 Windows 장치다. 실제 저사양 장치, 로컬 GPU 처리량 및 Live Agent 효능은 검증하지 않았다. 최종 검증 중 Windows 실행 계정 오류 1909가 잠시 발생했다. 실행 환경이 복구된 뒤 남은 검증을 재개했다. 마지막 CORE·export·보안 결과는 다음 기록을 기준으로 한다.

실행 방법: 기존 앱 실행 명령을 유지하며 **설정 → 고급 설정 → 성능 최적화**에서 자동·저사양·일반을 선택한다. 연구의 성능 단계·검증·Skills·F3-P opt-in 설정은 그대로다. 로컬 추론·자원 한도는 같은 앱 DB 범위이며 다른 앱 DB·외부 프로그램을 제어하지 않는다. 살아 있는 PID는 시간 경과만으로 회수하지 않아 PID 재사용 시 보수적으로 대기할 수 있다.


최종 gate: **demo_ready=true, release_ready=false, product_release_ready=false**. Docker는 NOT_VALIDATED, Live LLM은 NOT_VALIDATED, Live Search는 UNCONFIGURED다. OS 기본 브라우저·Windows 키 실환경은 NOT_VALIDATED, Skills/F3-P Live 효능은 NOT_VALIDATED다. 최종 기본 release 17개, F3-P canary·clean 104개 파일을 검증했고 hash·누락·참조·secret canary 이상은 0이었다. 최종 후처리 명령은 `.venv\Scripts\python.exe -X utf8 qa/performance/low_spec/finalize_validation.py`다.

공개 후보 최종 보안 검사: 281개 파일, 의심 비밀값 0. Git·이력 239개 검사 VALIDATED, 현재 CORE PASS. 공개 BLOCKED의 남은 사유는 LICENSE_PENDING_OWNER_CHOICE다. 기존 합성 경로 검출 P3 8개와 합성 키 INFO 22개를 보존했다. dependency·Bandit의 별도 재감사는 이번 변경에서 수행하지 않았다.
