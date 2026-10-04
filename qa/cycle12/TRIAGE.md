# Cycle 12 점검과 구현 범위

## 실행 명세와 기준선

사용자의 `ㄱ` 요청을 첨부 v5 보완 명세의 구현 지시로 적용했다. `review_summary.json`과 참조 ZIP은 진단 근거이며 운영 권한·검증 자격·Live 실행 지시로 사용하지 않는다. 원래 참조 파일은 `build/cycle12/reference_frozen/`에 해시를 유지한 채 보관한다. 작성 문서와 주석은 한국어로 쓴다.

수정 전 전체 검사는 **1010 passed, 5 skipped, 2 deselected / 686.17초**다. 명세에 적힌 과거 수를 기준선으로 쓰지 않았다. 첫 임시 폴더의 부모가 없던 실행은 **264 passed, 5 skipped, 2 deselected, 746 setup errors**였으며 앱 결함으로 분류하지 않았다. 실제 기준선은 별도의 정상 임시 폴더에서 다시 실행했다.

## 기존 구현 점검

| 영역 | 점검 당시 상태 | 기존 경로 | 이번 처리 |
|---|---|---|---|
| 일반 연구·모델 계획·도구·정본 | PRESENT_AND_TESTED | AgentRuntime, AutonomousResearchLoop, ToolRegistry, StateService | 구조 유지 |
| 기후 제품·기준·단위·연간 열 | PRESENT_AND_TESTED | climate_profile.py의 MEANING·parse_source | 실제 필드 검사를 유지하고 CSV 표현에 맞는 배율 추가 |
| 선택값·선택 중복 | PRESENT_NEEDS_WIRING | period_comparison.py, selected_rows | 타입이 있는 실패와 공통 키 검사 연결 |
| 다른 형식의 키별 대조 | MISSING | 기존 TXT 수집·프로필 검사 | 고정 CSV, 선택/필수 정책, 충돌 보존 추가 |
| 질문 변경 후 현재성 | PRESENT_NEEDS_WIRING | research_question, 연구 수정본·의존 그래프 | 원질문 보존, 지원 범위 해석, 소유자 변경·재검증 연결 |
| 자료 갱신의 원자성 | PRESENT_NEEDS_WIRING | 기존 planning transaction·invalidate | 자료 기록·의존 무효화·이전 근거 상태를 같은 트랜잭션에 저장 |
| F3-P·예산·복구·내보내기 | PRESENT_AND_TESTED | 기존 고정 도구 재실행·원장·반영 경계 | 새 엔진 없이 재검사·반영 직전 제어 경계 유지 |
| 천문 운영 경로 | NOT_APPLICABLE | 공통 선택·의미 함수, 고정 카탈로그 | 진단만 추가, 프로필 미등록 |
| Docker·Live·사용성 비교 | VALIDATION_BLOCKED | 실제 환경·기존 gate | 실제 실행 전에는 승격하지 않음 |

## 실제 앱 오류와 참조 제한

수정 전 새 회귀 검사 3개가 모두 실패했다. 선택값 `None`은 `TypeError`, 선택 연도 중복은 통과, 원문 질문 변경 뒤 결론 카드는 현재로 표시됐다. 증거는 `build/cycle12/reproduced_defects.xml`이다. 마지막 보안 점검에서는 정형 키 패턴이 아닌 등록 비밀값을 검색어에 넣으면 출처 대조 요청이 실행되는 결함도 재현했다(`credential_reproduction.xml`, 1 FAIL). 일반 검색과 같은 Credentials 경계를 전송 전에 연결했다. 기존 테스트 기대 결과는 수정하지 않았다.

참조 패키지에만 실행한 추가 진단은 별도다. 기준·집계 필드를 바꿔도 S2가 READY를 반환하고, 선택 None은 TypeError, 평균 그룹 순열은 GOAL_CONFLICT, 필수 대조 부재는 CHECK_PENDING이었다. 이를 앱의 수정 전 실행 결과로 바꾸어 보고하지 않는다.

## 적용·보류

- 기존 기후 한정 절차를 선택한 경우에만 새 검사·대조를 적용한다. 일반 연구에 기후 필드를 요구하지 않는다. Skills·F3-P·Ridge·Cycle 5의 기본 OFF를 유지한다.
- 기후 두 기간 절차의 기존 비중첩 조건을 유지한다. 공통 선택 동등성 함수는 그룹 간 비중첩을 보편 조건으로 강제하지 않는다.
- 질문·기간·기준의 변경은 소유자 결정과 새 수정본이다. 기술적 repair 성공으로 분류하지 않는다. `two_period_comparison`의 새 F3-P repair 템플릿은 추가하지 않았다.
- 정책상 필수인 대조는 없으면 대기한다. 선택 대조가 없으면 미수행과 한계를 기록하며, 존재하는 충돌은 무시하지 않는다. 정책은 Worker 출력에서 변경할 수 없다.
- HTTP 응답 정보는 실제 관측한 필드만 기록한다. 로컬 파일의 원래 수집은 미관측이며 출처 설명 HTML은 미수집이다. 두 표현은 같은 상위 제품을 공유한다.
- 실제 도구의 지정 파일 접근 제한을 검사한다. Docker·운영체제 수준의 프로세스 격리 검증과 구분한다.
- 새 DB·역할·실행기·그래프·모델 제공사·교사 승인 절차·보고서 형식 선택을 추가하지 않는다.

## 화면 참고

[Linear의 문서·변경 이력](https://linear.app/docs/documents)과 [Notion의 버전 확인](https://www.notion.com/help/duplicate-delete-and-restore-content)을 확인했다. 현재 결과를 먼저 보이고 자료 근거·선택값·변경 내역을 펼치는 기존 카드 구성을 사용한다. 새 설정 패널이나 통계적 신뢰도 점수는 추가하지 않는다.
