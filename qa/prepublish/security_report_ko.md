# 공개 준비·보안 검사 결과

기록: 2026-10-03 02:36:39 KST. 실제 비밀 값을 출력하지 않았다.

## 공개 상태

**BLOCKED**. Git 저장소가 없어 tracked/history는 NOT_VALIDATED, LICENSE는 소유자 선택 대기다. 후보 검사·ignore 검증을 완료했다. 초기화·커밋·push·자료 삭제·이력 수정은 하지 않았다.

| 항목 | 실제 결과 |
|---|---|
| suspected_secrets | 공개 후보 0 |
| tracked_sensitive_files | 실제 목록 확인 불가 · NOT_VALIDATED |
| ignored_required_source_files | 0 |
| large_files | 공개 후보 0 · 기준 10MB |
| absolute_local_paths | P3 4 · 정규식/합성 fixture, 실제 사용자 경로 아님 |
| 합성 canary | INFO 11 |
| 확인된 신규 P0/P1 | 0 |
| Git 이력 | NOT_VALIDATED |
| 의존성 | 84개·알려진 취약점 0 |
| Bandit | 경고 43개(MEDIUM 18·LOW 25) 검토·보존 |
| CodeQL/GitHub 설정 | CONFIGURED_ONLY / NOT_VALIDATED |

합성 예외는 직접 검토한 고정 키의 SHA-256 10개만 허용한다. 키에 test/fake/offline/fixture/canary가 있어도 미승인 값은 차단한다. 미승인 5사례 마스킹·차단을 확인했다. Core 표식의 현재 통과·실패·stale·누락 4상태도 구분한다.

## 경계 검사

| 경계 | 실제 확인·제한 |
|---|---|
| 키·브라우저 인증 | Chrome 재사용/만료/경합/재시작·CSRF·저장소 검사, OS 저장은 모의 |
| 로컬 웹 | Host/Origin/CSP/CSRF/소유권 기존 검사와 새 upload 경로 유지 |
| 제공사 네트워크 | 주소·public IP/DNS·redirect/인증 전달 회귀. Live 미실행 |
| 검색 전송 | 공개 검색어·egress·정책·가격·전체 시도 한도·pause/stop |
| 검색 불신 자료 | 키 응답 저장 전 차단, 예산/권한 변경 없음, VERIFIED 뒤 반영 |
| 파일 | CSV 크기/UTF-8/구조/base64/비밀·traversal·심볼릭 링크 |
| 코드·도구 | shell 없는 argv·명시적 확정 상태 검사. Docker 미검증 |
| 콘텐츠 | 기존 불신 HTML/Markdown/SVG·원격 이미지·CSP 회귀 |
| 상태·복구 | stale·artifact/dataset/report 변조·중복·새 프로세스 복구 |
| 비용 | 예약/미정산/경합/전체 호출 상한, 모호 유료 비용 UNRESOLVED |
| export | 기본 17파일·F3-P 104파일, 해시/누락/비밀/개인 경로 0 |

Bandit의 SQL은 고정 구조·외부 값 바인딩, 실행은 argv다. PATH/실행 파일의 신뢰는 로컬 전제이고 Docker tmpfs도 미검증이다. 경고 0으로 표시하지 않는다.

## GitHub Actions

contents read·checkout credentials 비지속·CodeQL 분석만 security-events write. 공식 commit SHA·로컬 YAML 검사를 통과했다. PR에 제공사 키·유료 검사·pull_request_target·셸 입력 치환이 없다. GitHub 실행·기능 활성화는 확인하지 않았다.

## 근거·후속 조건

[검사 JSON](security_summary.json) · [보안 테스트](security_test_results.json) · [의존성](dependency_audit.json) · [정적 경고](code_findings.json) · [워크플로](workflow_findings.json) · [12항목 보고서](IMPLEMENTATION_REPORT.md)

`python qa/prepublish_check.py --require-ready`는 현재 BLOCKED·exit 1이다. 무시된 파일은 암호화되지 않는다. 실제 비밀이 발견되면 먼저 폐기/교체하고 추적/이력에서 제거할 계획을 세운 뒤 재검사한다. 이력 수정은 소유자 승인 후 진행한다.

패턴 검사로 모든 개인정보/비밀을 탐지할 수는 없다. 실제 연구·화면·청구 자료는 공개에서 제외하고 안전한 fixture를 유지했다. Git 생성 후 실제 tracked/history·라이선스를 확인해야 한다.
