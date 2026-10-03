# F3-P v0.3 실행과 검증 범위

F3-P는 기본 OFF이며 Verified Analysis Skill Pack v1과 독립적으로 켠다.
기존 Coordinator와 Worker 역할, 등록된 분석 도구, staged mutation, StateService 검증·commit 경계를 사용한다.
DB migration과 dependency 추가는 없다.

## 실행

```powershell
.\.venv\Scripts\python.exe -m htrsa.agent_cli build/f3p-live.sqlite build/f3p-live-workspace input.csv --verified-analysis-skills --verification-repair --goal "문서화된 관측 단위와 sampling 조건에 따라 분석하라"
.\.venv\Scripts\python.exe -m htrsa.agent_cli build/f3p-live.sqlite build/f3p-live-workspace --resume RESEARCH_ID --verified-analysis-skills --verification-repair
```

실제 Agent 실행에는 기존 provider 자격 증명과 model 설정이 필요하다.
Skills를 끈 legacy 경로에도 `--verification-repair`만 적용할 수 있다.
환경 변수는 `HTRSA_VERIFICATION_REPAIR_ENABLED=1`이다.
Ridge 검사는 별도로 `--ridge-arithmetic-check` 또는 `HTRSA_F3P_RIDGE_ARITHMETIC_CHECK=1`을 설정한다.
Ridge 플래그는 Repair ON을 요구하며, Repair ON만으로 자동 활성화되지 않는다.
Resume에서는 저장된 플래그와 정책 버전을 일치시켜야 한다. 누락·불일치 시 오류로 중단하며 최신 정책으로 대체하지 않는다.

## 수리 경로

1. 기존 전체 검증을 수행한다. 실패 evidence는 immutable JSON artifact와 runtime step으로 저장한다.
2. 원래 staged mutation을 rollback한다. 공유 profile 입력은 유지하고 원래 결과·plan·figure·evidence는 INVALIDATED 상태로 보존한다.
3. 원래 계약 hash, 고정 plan hash, dataset identity를 확인한다. 의미 변경과 불명확한 checker 불일치는 자동 수리하지 않는다.
4. 기존 experiment_coordinator 역할의 제한된 판단 계약으로 RepairDecision을 받는다. 추가 도구 권한은 없다.
5. REPAIR는 고정된 plan의 trusted tool 재실행만 허용한다. 새 Worker 계약·request identity·artifact·revision을 만든다.
6. 모든 필수 검증을 다시 수행하고 StateService.commit이 다시 fresh 검증한다. PASS만 canonical state에 반영한다.

자동 수리는 최대 2 revision이다. 도구별 원래 retry·permission·runtime 제약은 새 계약에도 유지한다.
수리 실패·예산 부족·unsupported·checker qualification 실패는 가설의 과학적 기각으로 해석하지 않는다.
성공한 결과는 그 실행에 한정되며 reusable Skill·memory·training data·global policy로 승격하지 않는다.

### 구조에 맞춘 제한

현재 분석 경로는 고정된 trusted 구현이다. F3-P는 임의 Python 코드 생성·host 실행·verifier 수정 대신 같은 plan을 새 identity로 재실행한다.
지속적인 구현 결함은 2회 이내에서 해결되지 않으면 incomplete로 남고, 별도의 검토된 코드 수정이 필요하다.
Coordinator의 CREATE_NEW_PLAN_REVISION 등은 명시적 미완료 disposition이며, 자동 수리 안에서 의미를 바꾸거나 신규 실험을 승인하지 않는다.
원본 시도·실패·판단·revalidation은 기존 artifact/runtime_step 시설을 사용하며 새 DB 상태 체계를 만들지 않는다.

## 예산

Proposal의 알려진 최대 call 비용과 로컬 recovery reserve USD 0.01을 합해 사전 확인한다.
로컬 reserve는 실행·전체 재검증·artifact·state·report를 위한 보수적인 정책 buffer이며 실제 청구 비용 측정값이 아니다.
가격이 없으면 기존 unknown-price call/token cap을 적용하고 API 비용은 unknown으로 유지한다.
Proposal 완료 후 이미 소비한 call 비용을 다시 예약하지 않고 나머지 전체 recovery 여유를 재확인한다.
예산이 부족하면 새 분석을 시작하지 않거나 명시적으로 incomplete로 중단하며 commit하지 않는다.
이 preflight는 동시 연구 작업 전체에 대한 transactional resource reservation을 제공하지 않는다.

## Checker와 Ridge

고정 Pearson/Spearman association estimate에는 NumPy correlation reference를 사용한다. Spearman은 rank 변환 후 correlation을 확인한다.
정확한 analysis와 고장난 checker fixture가 불일치하면 자동으로 값을 바꾸지 않고 qualification 실패로 차단한다.
analysis와 artifact checker가 같은 잘못된 estimate를 공유해도 independent estimate check가 막는다.
이 검사는 p-value 전체, 독립성 가정, 인과성, 연구 가설의 참을 증명하지 않는다.
독립 reference가 없는 불일치는 unresolved로 남긴다.

실험적 Ridge 검사는 `tabular_regression_evaluation_v1`의 고정 alpha=1 holdout 경로에만 적용한다.
현재 구현의 augmented design, penalty의 intercept=0·feature=1 규칙으로 training preprocessing, 정규방정식 residual, intercept residual과 holdout MAE/RMSE 재구성을 확인한다.
v1은 개별 prediction을 저장하지 않으므로 기록된 coefficient/preprocessing에서 prediction을 재구성하고 기록된 metric과 비교한다.
normalized residual 및 수치 비교 허용오차는 1e-10이다. 조건수 >1e12 또는 nonfinite 상태는 needs_reference_check로 남기며 PASS로 취급하지 않는다.
개발 테스트 범위는 31개 행의 target scale 1e-4, 1, 1e6이며 전체 수치 범위를 보장하지 않는다.
산술 일관성은 formal verification·IID 증명·모든 leakage 부재 증명이 아니다.

## 검증 명령

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp build/pytest-f3p tests/test_verification_repair.py
.\.venv\Scripts\python.exe qa/performance_smoke.py --verified-analysis-offline
.\.venv\Scripts\python.exe qa/performance_smoke.py --f3p-offline
.\.venv\Scripts\python.exe qa/f3p_recovery_probe.py --all
.\.venv\Scripts\python.exe qa/qa_day1.py --output-dir build/f3p-qa-day1
```

기존 12개 Skill case와 신규 8개 F3-P case는 별도 집계한다.
평가 oracle은 평가기만 사용하고 repair request에는 전달하지 않는다.
FakeProvider 테스트와 로컬 timing은 Live Agent 효능·속도 향상 근거가 아니다.
실행 결과는 `qa/results/f3p_eval_results.json`, `qa/results/f3p_recovery_results.json` 및 최종 구현 보고서에 기록한다.

Live 비교 실행 준비:

```powershell
.\.venv\Scripts\python.exe qa/f3p_live_ablation.py --csv input.csv --goal "동일한 연구 질문" --output build/f3p-paired-live --repeats 3 --hard-limit-usd 1 --timeout-sec 300
```

동일 dataset snapshot·model/provider·질문·budget·timeout·search 접근 조건에서 Skills ON/F3-P OFF와 ON을 실행한다.
현재 provider adapter에는 seed 설정 인터페이스가 없다. API 비용·token·tool·repair call·wall time을 수집하고 최종 연구 결과의 독립 판정 전에는 live efficacy를 NOT_VALIDATED로 유지한다.
Docker, Live LLM, Live Search와 release_ready는 기존 gate의 실제 검증 결과만 사용한다.
