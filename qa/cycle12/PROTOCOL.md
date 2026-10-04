# Cycle 12 검사 범위와 재현

이 규약은 구현 중 확인한 적용 범위와 검사 분모를 기록한다. 외부 사전 등록이나 Live 비교의 사전 결과를 뜻하지 않는다. 기존 [v4 평가 규약](../qualified_profiles/PROTOCOL.md)의 실제 모델·자료·한도 동일성 및 평가자 정답 분리 조건을 유지한다.

## 실행과 판정

- 일반 연구 엔진·4개 역할·기존 검증과 정본 반영 경계를 유지한다. 지원하는 기후 질문에 선택적 한정 절차를 연결한다. 구 API 기본 프로필은 DISABLED이며 새 초보자 UI의 AUTO 정책을 유지한다.
- Skills·F3-P·Ridge·Cycle 5의 기본 OFF를 유지한다. 새 출처 대조는 기후 한정 절차 안에서만 적용하며 새 사용자 스위치를 요구하지 않는다.
- 정상 지원 입력은 실제 도구 결과·현재 근거로 제한된 결론에 도달해야 한다. 충돌·필수 부재·현재성 실패는 정본에 반영하면 안 된다. 선택 대조의 부재는 미수행과 한계를 남긴다.
- 평균 그룹의 순열은 같은 구성원·가중치·방향에만 허용한다. 시계열·회귀·그룹 이동·키 교체에 확대하지 않는다. 자료의 원래 행 순서는 보존한다.
- 같은 상위 자료의 TXT·CSV 일치는 독립 관측이 아니다. 양쪽 원자료·질문 해석·검증기가 공유하는 오류는 잔여 한계다.
- 선택 범위의 결측·중복·비수치·비유한 값은 계산 전에 타입이 있는 실패로 반환한다. 범위 밖 결측만으로 정상 계산을 막지 않는다.

## 분모를 분리하는 기록

| 기록 | 대상과 제한 |
|---|---|
| 참조 재실행 | 24조건·2자료군·3정책=72평가. 기존 참조 정책만 실행하며 앱·Agent 점수에 합산하지 않는다. |
| 참조 추가 진단 | 5개 경계 진단. 원래 24조건에 합산하지 않는다. |
| 앱 pytest | 매개변수화된 개별 테스트 수. 문서의 32개 요구 행과 같지 않다. |
| 모의 Agent+실제 도구 | 일반 작업대·기후 절차·F3-P의 오프라인 정확성. 실제 모델 판단 효능이 아니다. |
| 실제 Chrome | 소유자 인증·화면·실제 로컬 API·PDF. 모의 키 저장소·모의 Agent 또는 모의 HTTP 사용 여부를 표시한다. |
| 새 프로세스 복구 | 강제 종료 경계와 재개 후 반영·호출 수. 단일 프로세스 예외 처리 검사와 분리한다. |
| 천문 공통 진단 | 같은 TRAPPIST-1 카탈로그의 CSV·JSON 7행. 운영 프로필·Live 수집·교사 승인이 아니다. |
| 실제 제공사/E2E | 실행하지 않으면 NOT_VALIDATED. 이 패치는 자동 유료 실행을 승인하지 않는다. |

## 32개 요구와 실제 검사 경로

`tests/test_cycle12.py`의 아래 함수는 `test_` 접두사를 생략해 적었다. 매개변수 사례와 기존 검사를 포함한 최종 수는 실제 XML에서 보고한다.

| 요구 | 검사 함수 또는 실제 실행 | 판정 범위 |
|---|---|---|
| C12-01 | c12_01_real_tools_keyed_pair_and_frozen_policy | 모의 Agent 1회·실제 도구·반영 1회 |
| C12-02/03 | c12_02_03_12_recomputed_capture_hash_cannot_hide_key_conflict | 새 해시로도 잘못된 값·배율 차단 |
| C12-04/05/06 | c12_04_05_06_applicable_meaning_fields_are_checked, c12_05_optional_present_semantic_conflict_is_not_ignored | 물리량·기준·연간 열·집계·단위 실제 필드 검사 |
| C12-07 | c12_07_conversion_direction_and_baseline_recalculated_from_data, cross_domain.py | 기후 기준 변경·배율 및 day→hour 방향 |
| C12-08/09 | c12_08_source_row_permutation_preserves_mean_dependencies, c12_08_09_method_aware_group_order | 평균 순열 허용·순서 의존 방법 거절 |
| C12-10/11 | c12_10_bad_selected_values_are_typed, c12_10_missing_selected_value_preserves_actual_draft_and_capture, c12_11_unselected_missing_value_does_not_block | 타입 있는 실패·실제 초안과 원본 보존 |
| C12-12 | c12_12_scope_membership_is_not_silently_changed, c12_12_selected_duplicate_is_not_deduplicated, c12_02_03_12_recomputed_capture_hash_cannot_hide_key_conflict | 중복·누락·키 교체·그룹 이동·같은 평균의 키별 교환 |
| C12-13/14 | c12_13_optional_absence_is_recorded_without_false_check_pass, c12_14_required_absent_cannot_complete, c12_14_requirement_frozen_through_source_update | 선택 미수행·필수 대기·정책 고정 |
| C12-15 | c12_15_missing_meaning_is_review_not_default_success | 의미 근거 부재는 검토 필요 |
| C12-16/21 | c12_16_21_typed_selection_wrong_against_question_cannot_commit | 지원하는 문장 범위와 선택의 실제 대조·보편 언어 이해 보장은 아님 |
| C12-17/18 | c12_17_owner_amendment_changes_selection_history_and_pdf, c12_18_raw_question_change_invalidates_current_result | 소유자 승인 변경·원문 보존·이력·다시 검증 |
| C12-19 | c12_19_worker_authority_fields_rejected_at_commit_boundary, c12_19_worker_cannot_change_required_policy_via_owner_route | Worker 권한 위조·정책 하향 차단 |
| C12-20 | c12_20_shared_wrong_source_roots_remain_explicit_limit | 양쪽이 같은 오류일 때 잔여 한계를 유지 |
| C12-22/23 | c12_22_checker_change_and_latest_capture_tamper_are_stale, c12_23_unused_change_reuses_numbers_and_unrelated_research_does_not_stale | 현재 검증기·파일 해시·사용 행 의존성·안전한 재사용 |
| C12-24 | c12_24_context_change_between_verify_and_commit_blocks, c12_24_source_update_and_invalidation_roll_back_together | 변경 중 반영 차단·트랜잭션 rollback |
| C12-25/26 | c12_25_compound_fault_repair_rechecks_every_obligation, c12_26_budget_boundary_after_verify_cannot_commit_partial; 기존 F3-P 8사례 | 기존 최대 2회 repair·복합 결함·필수 의무 재검사·자원 부족 차단 |
| C12-27 | c12_22_27_stale_result_cannot_be_returned_as_resumed_current; recovery_probe.py; 기존 F3-P 6경계 | 실제 새 프로세스·중복 반영 없음·현재 질문과 필수 대조 보존 |
| C12-28 | c12_28_secondary_cannot_bypass_search_egress_or_secret_policy, c12_28_registered_opaque_secret_blocks_secondary_before_request | 검색 OFF·전송·횟수·등록 비밀값·원장 전송 전 차단 |
| C12-29 | c12_29_inspector_is_lazy_and_currentness_is_not_pdf_fabrication; browser.cjs | 현재 PDF·이력 수치·화면·교사 승인 미표시 |
| C12-30 | c12_30_secondary_replay_failures_never_refetch, c12_30_latest_unused_capture_missing_also_blocks_frozen_replay | 원본/최근 파일 누락·변조 실패·몰래 재수집 없음 |
| C12-31 | c12_31_astronomy_uses_generic_selected_values_without_climate_fields; cross_domain.py | 기후 전역 스키마 없이 공통 키·물리량·변환 검사 |
| C12-32 | c12_32_beginner_start_has_no_new_required_witness_fields; 기존 ui_unblock_browser.cjs·research_wizard_browser.cjs·model_activation_browser.cjs | 일반 시작·API 모의 HTTP·튜토리얼·기존 모델 활성화 보존 |

## 출처와 취득 호환성

기후 TXT와 고정 CSV의 제품·범위·물리량·단위·기준·연간 열을 형식별로 검사한다. 값 비교는 선택 연도와 변환 기준에 필요한 연도를 모두 포함한다. 두 실제 HTTP 취득은 같은 수집 그룹이며 시간 차이가 300초 이내여야 한다. 버전 정보가 없는 NASA 공개 파일에서 시간만으로 같은 발행본을 보장하지 못한다. 대조 불일치는 어느 쪽이 진실인지 결정하지 않는다. 로컬 제공 파일의 원래 HTTP 취득과 출처 설명 HTML은 미관측·미수집으로 표시한다.

해시는 전송 압축 해제·UTF-8 해독 후 저장한 UTF-8 텍스트 바이트에 적용한다. 원래 압축 응답 바이트의 해시로 표시하지 않는다. 관측한 HTTP 헤더·상태·취득 시각만 저장한다.

## 재현 명령

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m pytest tests/test_cycle12.py tests/test_beginner_v4.py tests/test_real_tools.py -q -p no:cacheprovider --basetemp build/cycle12-reproduce-targeted
.\.venv\Scripts\python.exe -B -X utf8 -m pytest -q -p no:cacheprovider --basetemp build/cycle12-reproduce-full
.\.venv\Scripts\python.exe -B -X utf8 -m htrsa.preflight validate-core
.\.venv\Scripts\python.exe -B -X utf8 qa/cycle12/offline_validation.py
.\.venv\Scripts\python.exe -B -X utf8 qa/cycle12/reference_review.py
.\.venv\Scripts\python.exe -B -X utf8 qa/cycle12/cross_domain.py
.\.venv\Scripts\python.exe -B -X utf8 qa/cycle12/evaluate_v4.py
node qa/cycle12/browser.cjs
.\.venv\Scripts\python.exe -B -X utf8 qa/cycle12/export_probe.py
node qa/beginner_v4_browser.cjs
node qa/research_wizard_browser.cjs
node qa/ui_unblock_browser.cjs
node qa/model_activation_browser.cjs
```

참조 ZIP은 명세에 고정한 SHA-256과 모든 패키지/동결 파일을 확인한다. `reference_review.py`는 알려진 참조 코드만 자격 증명을 제거한 평가자 자식 환경에서 실행한다. 참조 `oracle.json`은 평가자만 읽으며 실제 Worker의 입력 파일 목록에 포함하지 않는다. 실제 qualified data.import의 허용 파일 밖 `oracle.csv` 요청은 차단한다. 이는 Docker 프로세스 격리 검증과 다르다.

## Live 비교와 채택 기준

현재 실제 모델·competitor·학생/교사 평가 결과는 없다. 향후 명시적으로 승인된 같은 모델·자료·한도·허용 재시도로 v4 평가기를 실행할 때 정상 완료율·잘못된 승인·불필요한 대기를 각각 분모와 함께 비교한다. 새 질문/자료 변경은 두 시스템에 같은 범위로 제공한다. 총 비용·시간·검토 동작 수는 실제 관측만 쓴다. 정상 완료나 검토 부담이 악화되면 선택적 상태를 유지한다. 여기의 작성자 설계 자료·72개 참조 평가를 성능 우위나 과학적 신뢰도 점수로 사용하지 않는다.
