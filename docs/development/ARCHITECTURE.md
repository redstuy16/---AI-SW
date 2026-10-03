# H-TRSA 결정적 실행 구조

첫 실행 순환은 `Agent Output → Speculative Mutation → Deterministic Verification → StateService → Atomic Canonical Commit`이다. Agent의 잠정 결과는 결정적 검증 후에만 정본에 반영한다.

Agent는 구조화 결과를 반환한다. `stage()`는 `staged_mutations`에만 저장한다. `verify()`는 계약·작업·역할·도구 권한·추적·mock.analysis 재계산·참조·수락 규칙·상태 버전을 검사한다. `commit()`은 사전 PASS를 요구하고 `BEGIN IMMEDIATE` 안에서 다시 검사한 뒤 산출물·감사 사건·버전 증가를 같은 트랜잭션에 기록한다. WARN·FAIL은 반영할 수 없으며 오래된 상태나 중복 반영은 되돌린다.

연구·작업·계약·실행 추적은 StateService를 통해 기록하는 운영 정보다. 검증된 Worker 산출물이 첫 버전 관리 과학 결과이며 반영마다 state_events 한 행을 만든다. Day 3A 저장 지점은 작은 실행 커서와 상태 버전을 보존한다. 재개는 SDK 대화 이력 없이 정본 기록에서 미완료 작업을 이어간다.

- Agent는 작업을 조정하고 도구는 계산한다. mock.analysis는 결정적 평균만 계산한다.
- 연구 상태는 SQLite에 저장하며 ContextBundle은 전달용 문맥 스키마다.
- 논리적 역할과 모델 인스턴스는 별개다. 모의 역할은 모델을 사용하지 않는다.
- Agent 출력은 PASS와 원자적 반영 전까지 잠정 결과다.

SQLite는 외래 키를 활성화하고 동기식 StateService 쓰기 담당자 하나를 전제한다. 분산 잠금·비동기 쓰기는 제공하지 않는다. JSON 키는 정렬하고 시각은 UTC ISO-8601로 기록한다.

## 실제 로컬 분석 경계

```text
                   신뢰하지 않음
                분석 Worker
                      |
                      v
               Python 샌드박스
                      |
                      v
             잠정 결과
                      |
         +------------+------------+
         v                         v
 신뢰한 Stats 도구       산출물 무결성
         +------------+------------+
                      v
                   Verifier
                      |
                    PASS
                      v
                StateService
                      v
               정본 상태
```

Worker가 생성한 Python은 신뢰하지 않는 실행이다. stats.run은 신뢰할 수 있는 결정적 계산이다. 검증기는 저장 해시·필드값·도구 호출 ID·데이터 ID와 해시를 독립적으로 확인한다. 정본 쓰기는 StateService만 수행한다. LLM·Worker가 작성한 수치보다 도구와 산출물의 출처 기록이 기준이다. Docker 샌드박스는 자신의 출력을 검증할 수 없으며 첫 과학 결과는 직접 통계 도구 경로로 검증한다.

data.import는 로컬 CSV의 크기·구조를 확인하고 UTF-8 바이트를 workspace/<research_id>/inputs/datasets/에 복사한다. 이후 도구는 데이터 ID로 검증된 경로를 조회한다. 데이터·파일 산출물은 결과 검증 전까지 대기 상태다. 프로파일·통계 JSON·PNG·선택적 Python 입력/출력의 SHA-256을 기록한다. 정본 수치 필드마다 통계 산출물·도구 호출·데이터 ID·데이터 SHA-256을 연결한다. 반영 트랜잭션 안에서 파일을 다시 검사하고 실험·근거·결과·사건·버전 하나를 함께 갱신한다.

ToolRegistry는 등록된 논리 이름 중 계약에서 허용한 도구만 실행한다. 중복 방지 키는 실행 전에 예약하며 실패한 호출의 키 재사용도 DuplicateToolRequestError로 차단한다. Day 3A 재개는 완료된 추적의 검증 후 재사용할 수 있다. 미완료 예약은 차단한다. 로컬 추적은 UTC 시작/종료·지연·상태·버전·추정 비용 0을 기록한다.

001_initial.sql은 원래 스키마, 002_real_tools.sql은 데이터·산출물·실험·근거 정보, 003_agent_runtime.sql은 Agent 사용량·예산·계약 상태·실행 사건·커서를 정의한다. schema_migrations가 적용 버전을 관리하고 미적용 마이그레이션마다 단일 트랜잭션으로 실행한다. db/schema.sql은 최초 기준 스냅샷이며 새 DB는 마이그레이션을 기준으로 한다.

004_research_loop.sql은 계획 상태·가설·그래프 간선·제한된 작업·문맥 지표·비판·출처 연결 요약을 추가한다. 계획 변경은 StateService의 결정적 검사와 별도 SQLite 트랜잭션에서 연구 버전을 올린다. 과학 실험은 stage→verify→commit 경로를 유지한다. 요약·비판은 운영 문맥이며 검증된 과학 근거가 아니다.

## Day 3A 실제 Agent 경계

AgentRuntime은 Manager→Coordinator→분석 계획 Worker 순서를 담당한다. 제공사 인터페이스는 구조화 출력 스키마를 받고 OpenAIAgentsProvider는 StateService에서 분리한다. Manager 계획·Coordinator의 제한된 계약 초안·Worker의 열/방법 선택을 검증한 뒤 계약을 발급한다. 모델 호출에 도구 실행이나 정본 DB 쓰기 권한을 직접 제공하지 않는다.

Coordinator의 data.import·data.profile은 검증된 데이터·프로파일 참조를 만든다. Worker가 Pearson/Spearman을 선택해도 수치는 stats.run이 계산한다. 시각화·근거도 검증된 통계 산출물에서 생성한다. 기존 과학 검증기는 해시·출처·수치·상태 버전을 검사하고 StateService.commit만 정본 버전을 올린다.

agent_runs는 제공사·모델·시각·사용량·확인된 추정 비용·상태·오류 코드·기준 버전을 저장한다. runtime_events는 운영 추적이며 state_events를 바꾸지 않는다. API 키·프롬프트·원시 제공사 예외는 저장하지 않는다. 가격 등록소는 비어 있으며 미상 가격은 null이다. 확인한 보수적 요청 비용 상한으로만 요청 전 금액 예측이 가능하다.

재시도·상위 판단은 규칙으로 제한한다. 일시적인 모델/도구 실패는 계약 상한 안에서 반복하고 잘못된 구조화 출력은 최대 한 번 수정한다. 반복 실패는 Coordinator 진단으로 전달하며 전략적 코드만 Manager에 올린다. 호출 중 정본 버전이 변하면 결과를 거절한다. 재개는 작업·계약·데이터·산출물·버전을 검증하고 완료한 도구는 추적에서 재사용한다. 반영과 완료 저장 사이의 충돌도 이미 반영한 변경을 인식한다.

샌드박스는 Docker만 사용한다. 네트워크 없음·비관리자 사용자·읽기 전용 루트·권한 제거·자원 상한·연구 범위 입력/스크립트/출력 마운트를 적용한다. Docker·데몬이 없으면 DOCKER_UNAVAILABLE로 차단한다. 실행은 --pull=never이므로 이미지를 미리 준비해야 한다. 생성 코드를 호스트 Python에서 실행하는 대체 경로는 없다. 실제 격리 검사는 데몬과 이미지가 필요하다.

## Day 3B 문맥과 연구 순환

ContextCompiler는 계약·필수 참조→무효화 경고→직접 의존 관계→최근 원시 사건→어휘 검색 배경 순으로 역할별 ContextBundle을 구성한다. entity_edges를 깊이 2까지 탐색한다. 검색 후보는 연구·상태·역할 가시성으로 먼저 거르고 유사성·중요도·최근성 가중치로 순위를 정한다. 항목마다 참조 유형·ID·상태·검색 시점 버전을 유지한다. 보수적인 UTF-8 추정으로 역할·계약 예산 중 더 낮은 값을 적용하며 필수 참조가 넘치면 자르지 않고 실패한다. 모델 호출마다 문맥 지표를 기록한다.

AutonomousResearchLoop는 구조화 작업, 기본 30회 상한과 SHA-256 작업 지문을 사용한다. 가설을 최대 3개로 추리고 결정적 점수로 활성 가설을 선택한다. 신뢰할 수 있는 통계 도구를 실행하고 완료된 핵심 실험만 검증 Coordinator에 전달한다. 후속 제안은 Coordinator 승인을 받아 새 제한 계약과 다른 지원 방법으로 실행한다. 가설 상태 변경은 Coordinator 결정과 검증된 근거를 요구한다. SUPPORTED·REJECTED에는 서로 다른 근거 2개가 필요하다. 최종 중단은 Manager가 결정한다. 결론 후보는 지원된 가설의 검증 근거만 인용하고 검증되지 않은 숫자를 넣지 않는다.

실험 무효화는 이력을 보존하고 관련 근거·산출물을 무효화하며 지원을 잃은 가설을 낮추고 해당 근거를 인용한 결론을 다시 검토한다. 검색은 무효 기록을 제외하되 이유를 경고로 유지한다. build_research_tree는 저장 관계의 읽기 전용 투영이다.

## Day 3C 충돌 복구와 출시 검증

005_runtime_recovery.sql은 운영 커서·재개 가능한 모델 단계·안정된 계약 키·작업 인덱스를 추가한다. 커서는 재개 정보이며 과학적 참이 아니다. 재개는 버전·사건 순서를 정본·계획 사건과 비교해 설명된 반영·알려진 계획 전이만 허용한다. 이미 반영한 Worker 변경은 모델·도구 재실행 전에 인식한다. 대기 변경은 기존 검증 내용과 commit의 무결성 재검사를 유지한다. 원래 계약·결정적 요청 키를 사용해 완료한 가져오기·분석 호출을 재사용하고 미완료 예약은 차단한다.

계획용 모델 출력은 구조화 운영 단계로 저장한다. 완료 전 중단한 호출은 실패 시도로 계산해 재시도·미상 가격 상한을 초기화하지 않는다. 실험별 비판은 중복되지 않으며 가설·작업·간선은 중복 없이 대조한다. 충돌 주입은 접수·도구·잠정 저장·검증·반영·비판·재계획·후속 계약·가설 경계를 새 실행기/제공사로 검사한다. 이전 실험이 무효화되면 재개도 인용하지 않고 종료한다. 동기식 쓰기 담당자 하나를 전제하며 독립적인 동시 재개 프로세스는 지원하지 않는다.

htrsa.preflight는 기계 판독 Docker·API 검사를 제공한다. Docker 검사는 PythonSandboxTool과 동일한 제한 형태를 사용한다. 실제 격리 검사 4개와 실제 Manager 검사 1개는 출시 기준이다. 소스 지문으로 개발 테스트와 출시 준비를 구분하며 Docker·API 설정이 없으면 release_ready=false다.

## Day 4A 문헌 근거와 보고서 경계

LiteratureResearchRuntime은 기존 자율 실험 전 제한된 문헌 검색과 활성 가설의 반박 검색을 수행한다. ScholarlySearchProvider는 OpenAlex·Crossref·모의 제공사를 분리한다. 응답은 정규화해 지속 캐시에 저장하고 정본 출처·문헌 근거는 StateService만 쓴다. DOI·OpenAlex ID·제한된 제목/연도 비교로 중복을 제거한다. 출처 SHA-256과 정확한 초록 문장 해시를 보존한다. 실제 CLI의 예산 제한 검토 모델도 원문·해시·주제·지원/반박 검사를 우회하지 못한다. 관련성·검증을 통과해야 VERIFIED이며 제목·메타데이터만으로 과학 주장을 입증하지 않는다.

문맥에는 제한된 검증 문헌의 출처·근거 ID·지원/반박·상태가 포함된다. Manager는 가설 선택 전에 문맥을 읽고 이후 간선으로 영향을 준 출처를 기록한다. 문헌 종합은 지원·반박을 모두 보존한다. 결론·보고서는 검증 문헌·실험에서 만들며 숫자 자리표시는 통계 산출물과 필드별 출처로만 해석한다. 보고서 생성 전 참조·해시를 검사하며 미해석 참조는 내보내기를 차단한다. 공개 텍스트는 이스케이프하고 실제 문헌 검사는 기존 Docker·API 기준을 바꾸지 않는다.

## Day 4B 대시보드·데모·출시 경계

htrsa.dashboard는 SQLite와 검증 산출물에서 개요·관계 트리·가설·근거·실험·검증·진행 기록·사용량·자료·보고서·환경을 조회한다. DashboardReadAPI는 최소 GET 경로만 제공하며 StateService 쓰기를 노출하지 않는다. 보고서 Markdown은 이스케이프한 텍스트로 표시하고 산출물 경로는 Workspace.path로만 해석한다.

오프라인 데모는 고정 CSV와 scholarly.fake를 사용한다. A는 문헌→가설→검증 실험 2회→비판/재계획→보고서다. B는 실험 하나 무효화→경고→대체 실험→수정 결론이다. demo_manifest는 종료 상태·참조·출처·무효화·논리 작업 중복을 검사한다. DEMO 성공은 실제 제공사 검증이 아니다.

htrsa.release는 연구 범위의 research_output만 복사하고 자격 증명 없는 선언 파일·재현 지침·환경 기준을 기록한다. release_ready는 핵심·Docker·실제 LLM 검증을 요구하며 문헌은 AVAILABLE·RATE_LIMITED·UNCONFIGURED·FAILED·VALIDATED로 별도 표시한다. HTTP 429는 제한된 Retry-After를 따르고 무한 반복하지 않는다.

저장소 루트에서 실행한다.

```text
python -m htrsa.generate_schemas
python -m pytest -q
python -m htrsa.runtime path/to/new.sqlite
python -m htrsa.real_runtime path/to/new.sqlite path/to/workspace tests/fixtures/temperature_growth.csv
```
