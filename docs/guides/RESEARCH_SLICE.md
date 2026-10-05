# Research Slice v2

기본 OFF인 StateService 확장이다. 새 Agent·runtime·graph DB·multiverse는 없다.
기존 Skill, F3-P, 과학 검증, canonical commit, dependency edge, resume,
보고서·release 검증을 재사용한다.

## 사용

```powershell
.\.venv\Scripts\python.exe -m probe.agent_cli build/slice.sqlite build/slice-workspace input.csv --claim-evidence-provenance --verifier-dependency-catalog --goal "문서화된 관측 단위를 분석하라"
.\.venv\Scripts\python.exe -m probe.agent_cli build/slice.sqlite build/slice-workspace --resume RESEARCH_ID --claim-evidence-provenance --verifier-dependency-catalog
.\.venv\Scripts\python.exe qa/reliability_lab.py --enable --output-dir build/reliability-lab
.\.venv\Scripts\python.exe qa/research_slice_recovery_probe.py --all
```

Agent 실행에는 기존 provider 자격 증명·model이 필요하다. Skills는
`--verified-analysis-skills`, F3-P는 `--verification-repair`, 실험적 Ridge는
`--ridge-arithmetic-check`로 별도 활성화한다. 기본값을 바꾸지 않는다.
환경 변수는 `PROBE_CLAIM_EVIDENCE_PROVENANCE=1`,
`PROBE_VERIFIER_DEPENDENCY_CATALOG=1`이다. Reliability Lab은 CLI의
`--enable` 없이는 실행되지 않는다. `ResearchSliceConfig`의 내부 평가
옵션으로 P1/P2 invalidation과 V1/V2 dependency metadata를 비교한다.
Resume는 저장된 flag와 정책 버전의 누락·변경을 차단한다.

## 저장과 검증

기존 SQLite에 opt-in component migration v1을 적용한다. 기존 core migration
001–006은 유지하며 외부 dependency는 추가하지 않는다. GUI control-plane과
같은 additive migration 방식이다. 신규 테이블은 component schema marker,
Claim revision, Binding, working staging, obligation current/history이다.
기본 OFF인 신규 DB에는 이 테이블을 만들지 않는다.

숫자는 실제 `/result/...` field, artifact identity/revision/hash, method,
estimand, unit, named tolerance에 연결된다. material 숫자 집합은 실행 결과와
독립적인 fixture manifest의 denominator로 평가한다. null은 zero가 아니다.
correlation은 dimensionless, n은 observations, predictive MAE/RMSE는 target
단위이며 미상 단위는 UNKNOWN이다. exact, 1e-10 absolute tolerance,
명시한 precision의 half-even 반올림만 허용한다.

association Claim은 인과 효과로 변환하지 않는다. prediction Claim은
target/features·split·Skill/version·alpha와 전체 고정 plan을 보존한다.
시계열 Claim은 lag·origin·cutoff·horizon·metric trace를 보존한다.
검증된 실행이 과학적 참이나 sampling 가정의 참을 증명하지 않는다.

문헌은 기존의 literal abstract span + deterministic/model review 경계를
사용한다. FULL_TEXT/SNIPPET/METADATA_ONLY는 schema에서 구분하지만 현재
source ingest가 검증 가능한 것은 ABSTRACT뿐이다. URL만으로 지원하지 않는다.
동일한 canonical hypothesis에 연결된 검증된 반박 span은 CONTRADICTS로
보존한다. 일반 paraphrase·새로운 의미 추론은 자동 지원하지 않는다.

부모 dataset/result/plan/source/verifier qualification 변경 시 기존
`entity_edges.depends_on`을 순회하여 현재 Claim의 새 revision과 STALE Binding을
만든다. 과거 revision과 snapshot은 보존한다. revalidation은 남은 활성
지원·반박·제한을 다시 확인한다. 하나의 지원이 사라졌다는 이유만으로
Claim을 false로 만들지 않는다. F3-P의 failed staging과 새 검증 revision은
별개이며 이전 지원을 새 결과로 복사하지 않는다.

읽기 projection도 artifact/parent integrity를 확인한다. 보고서에서 현재
근거로 사용하는 material Claim이 missing/stale이면 보고서 export가 차단된다.
read-only API는 `/api/research/RESEARCH_ID/research-slice`이며 일반 화면에
문장별 graph를 추가하지 않는다. report의 Material Claim Trace와
`research_output/research_slice.json`에 current/history, source snapshot/span,
numeric slots, obligations, dependency/qualification history, Plan Lock이 있다.
release manifest는 opt-in extension schema/policy와 기존 파일 hash를 보존한다.
PROV-O/RO-Crate 적합성을 주장하지 않는다.

## 검증 의무 목록

각 기존 verifier check의 exact staged revision, outcome, implementation hash,
dataset revision, result dependency를 기록한다. F3-P NumPy reference는 실제
library version/function을 기록한다. unknown model/dependency/qualification은
UNKNOWN이며 provider 이름에서 독립성을 추론하지 않는다. 동일 구현 wrapper는
shared dependency로 표시된다. 필수 FAIL/UNKNOWN/NOT_RUN, 누락, stale revision,
FAILED qualification은 commit을 차단한다. 서로 다른 obligation을 투표로
대체하지 않는다. qualification의 UNKNOWN은 재사용 승인으로 승격되지 않는다.

Analysis Precommit은 내부 frozen Contract/Plan의 hash, estimand, dataset,
method/split/stopping/seed/budget/model/prompt/tool versions와 outcome access
UNKNOWN을 기록한다. 외부 사전등록이 아니며 결과 미열람을 추정하지 않는다.

## 오프라인 평가

fixture manifest는 `qa/fixtures/reliability_lab_fixtures.json`이다. 기존 F3-P 8개 fault
case와 healthy control을 V0/V1/V2에서 실행한다. 10개 provenance fault/control을
P0/P1/P2에서 실행한다. 실제 contract/plan/dataset hash를 비교하고 fixture의
ID counter·seed를 고정한다. 5개 의미 보존 변형에서는 실제 전달된 evidence set을
확인한다. 5개 evidence 변경은 재검증을 요구한다. 11개 crash 경계를 각 V arm의
별도 프로세스에서 실행한다.

P0는 새 metadata를 검사하지 않는다. 이 부재는 missing trace coverage이며
과학적 출력 오류가 발생한 것처럼 취급하지 않는다. gold 결과와 oracle은
fixed fixtures와 held-out NumPy estimate를 사용하며 repair Agent에는 전달하지
않는다. false acceptance/rejection, repair, omission, binding, contradiction,
answer coverage, hard gate violations, wall/local time을 각 raw numerator/
denominator로 기록한다. 임의 가중 점수는 없다. 같은 wrong conclusion의
consistency는 correctness가 아니다. zero-variance error correlation은
NOT_ESTIMABLE이며 0 correlation/독립성으로 바꾸지 않는다.

FakeProvider 로컬 검증은 live Agent efficacy·실제 지출·production error rate의
근거가 아니다. API tokens/cost는 null이다. 현재 live provenance/reliability
efficacy는 NOT_VALIDATED이다. Docker/LLM/Search와 release readiness는 기존
실제 환경 gate만 사용한다.
