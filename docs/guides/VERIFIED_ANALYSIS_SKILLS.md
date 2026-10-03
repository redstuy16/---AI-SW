# Verified Analysis Skill Pack v1 사용과 검증

선택적 확장은 기존 Manager → Coordinator → Worker → Tool → 잠정 변경 → Verifier → StateService 반영 경로에 고정 수치 절차 3개를 추가한다. 기본 OFF다. 새 Agent·DB 마이그레이션·제공사·대시보드는 추가하지 않는다.

## 연결 구조와 범위

Coordinator는 Worker에게 analysis.skill·evidence.record를 허용할 수 있다. Worker는 AnalysisPlan.skill_plan을 반환하고 실행기는 기존 운영 단계에 계획을 고정해 도구를 한 번 호출한다. 고정 NumPy/SciPy 연산으로 대기 상태의 계획·결과·그림을 만든다. 기존 검증기가 입력 해시·계획 지문·수치 출처·권한·예산·파일 해시·상태 버전을 확인해야 반영한다. verification=external_verifier_required는 검증 판정이 아니다. 별도 과금되는 하위 도구 호출은 없다.

지원 절차는 다음과 같다.

| ID | 지원 작업 | 고정 방법과 비교 | 불확실성 |
| --- | --- | --- | --- |
| `tabular_association_v1` | 독립 관측 표집이 문서화된 수치 변수 두 개 | 사전 선택한 Pearson/Spearman, 결측 쌍 제외 | Pearson SciPy 95% 구간, Spearman 구간 미추정 |
| `tabular_regression_evaluation_v1` | IID 조건이 문서화된 수치 회귀 | 시드 고정 단일 홀드아웃, 훈련 데이터만으로 평균 대체·표준화, ridge alpha=1과 훈련 평균 비교, MAE/RMSE | 단일 홀드아웃 구간 미추정 |
| `timeseries_backtest_v1` | 시간순·규칙적 시각의 수치 시계열 하나 | 양의 고정 지연, 확대 창 1단계 예측, ridge alpha=1과 직전 값 비교, MAE/RMSE | 의존 예측 오차 구간 미추정 |

집단·짝지은 자료·반복 개체·분류·인과·다변량/불규칙 시계열·모델 탐색·다단계 작업은 v1 범위 밖이다. 단위·행 ID·표집 근거를 선언해야 한다. 선언만으로 독립성과 누수 부재를 입증하지 않는다. 가정 누락·오류는 이유 코드와 needs_review·unsupported로 남기며 과학 실험으로 반영하지 않는다. 유의하지 않거나 null·음수·기준보다 나쁜 결과도 유효한 분석 결과일 수 있다.

SkillRequest는 연구·작업·계약 ID, plan_ref/version/hash, Skill ID/버전, 데이터 ID/SHA-256, 변수·단위·관측 ID/단위·표집 근거·시드·정책·행 예산을 포함한다. 데이터 ID·해시는 정본 저장소와 정확한 스냅샷에서 다시 확인한다. 열거한 방법·고정 매개변수만 허용한다. SkillResult는 적용 가능성·실행·외부 검증·이유·건수·분할/진단·지표·불확실성 한계·추적·출처를 구분한다. JSON은 NaN·Infinity를 허용하지 않는다.

## 활성화와 복구

실제 Agent 실행은 명시적으로 활성화한다.

```powershell
.\.venv\Scripts\python.exe -m htrsa.agent_cli build\skill-run.sqlite build\skill-workspace path\to\data.csv --goal "A fixed, documented analysis question" --verified-analysis-skills --target-usd 0.25 --soft-limit-usd 0.75 --hard-limit-usd 1.0
```

내장 호출은 `HTRSA_VERIFIED_ANALYSIS_SKILLS_ENABLED=1`도 사용할 수 있다. 기본값은 false다. 재개 시 동일 설정·Skill 버전이 필요하다. 적용한 설정·버전과 정확한 계획은 기존 실행 단계에 기록한다. 설정 누락/변경·호환되지 않는 계획 버전은 복구 오류로 차단한다. 완료한 도구·반영은 반복하지 않는다. 새로운 과학 반복 실험은 새 작업·실험 ID를 사용한다. 불확실한 실행의 별도 시도는 기존 복구 정책의 제한을 따른다.

Skill 계획·결과·그림은 기존 산출물 저장소에 등록한다. 보고서 수치는 검증된 결과·필드별 출처에서 해석한다. 활성화한 출시 내보내기는 검증된 호출마다 해시가 있는 analysis_artifacts 파일 3개를 포함한다. 재현에는 같은 데이터 스냅샷·해시·계획·지문·Skill/의존 버전·시드·분할 요약이 필요하다. 데이터 원본은 기존 공유 규칙대로 출시 파일에 포함하지 않는다. 내보내기는 데이터/산출물 해시·보고서 버전·비밀 패턴을 다시 검사한다.

절차는 기존 로컬 도구 정책에서 신뢰할 수 있는 고정 응용 코드로 실행한다. 임의 Python은 기존 Docker 샌드박스 기준을 따른다. 이 기능의 검증으로 Docker 격리나 실제 LLM 동작을 검증한 것으로 취급하지 않는다.

## 오프라인 검사 재현

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp build\pytest-verified-analysis
.\.venv\Scripts\python.exe qa\performance_smoke.py --verified-analysis-offline
.\.venv\Scripts\python.exe qa\qa_day1.py --output-dir build\verified-analysis-qa-run
```

사전 지정한 지표와 별도 12개 사례는 qa/fixtures/verified_analysis_eval_config.json, 실측 결과는 qa/results/verified_analysis_eval_results.json에 있다. 지원 작업 완료와 적절한 보류/거절을 구분한다. 같은 데이터·질문·제공사/모델·예산·시간·반복 조건의 실제 OFF/ON 비교에서 모든 호출·재시도·비용·검증 부담·실패를 기록하기 전에는 실제 Agent 효능이 NOT_VALIDATED다. 가격이 없으면 미상이며 0으로 기록하지 않는다.

실제 비교 명령은 승인과 자격 증명이 있는 환경에서 별도 새 DB 두 개에 동일 CSV·질문을 사용한다. 모델·제공사·예산을 고정하고 플래그만 변경한다. 기존 Agents SDK 경로는 모델 샘플링 시드를 노출하지 않아 엄격한 확률적 쌍 비교가 제한된다. 다중 제공사 작업대의 시드 설정 지원 여부는 해당 모델의 별도 확인이 필요하다.

```powershell
$env:HTRSA_VERIFIED_ANALYSIS_SKILLS_ENABLED='0'
.\.venv\Scripts\python.exe -m htrsa.agent_cli build\ablation-off.sqlite build\ablation-off-workspace path\to\fixed.csv --goal "Same fixed question" --target-usd 0.25 --soft-limit-usd 0.75 --hard-limit-usd 1.0
$env:HTRSA_VERIFIED_ANALYSIS_SKILLS_ENABLED='1'
.\.venv\Scripts\python.exe -m htrsa.agent_cli build\ablation-on.sqlite build\ablation-on-workspace path\to\fixed.csv --goal "Same fixed question" --verified-analysis-skills --target-usd 0.25 --soft-limit-usd 0.75 --hard-limit-usd 1.0
```

아래 명령은 실제 비교 준비용이며 오프라인 평가에서 실행하지 않았다. 요청 감소·비용 감소·지연 감소·Agent 성공률 향상을 측정했다고 주장하지 않는다.
