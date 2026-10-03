# 개발자 CLI와 검증

명령은 저장소 루트에서 실행한다. 일반 앱 사용은 [빠른 시작](../../README.md)을 참고한다. 이 문서는 기존 CLI·제한·복구·검증 설명을 보존한다.


선택적 자료 의미·변환 계보·원질문 범위 검사는 [Cycle 5 안내](../guides/CYCLE5.md)를 참고한다. 기본 OFF다.

실제 Agent 경로는 Manager → Experiment Coordinator → 분석 계획 Worker → 결정적 CSV·통계·시각화 도구 → 과학 검증기 → 정본 반영 순서로 실행한다. 기존 결정적 모의 경로와 실제 CSV 경로도 사용할 수 있다.

## Windows 앱 실행

가상환경 설치 후 저장소의 **`H-TRSA.wsf`를 더블클릭**한다. 기존 작업대가 콘솔 없는 `pythonw`로 실행되며 자료는 `build/workbench`에 보관한다. 설정에서 GPT 모델 준비 → 키 등록 → 명시적 연결 검사 → 예산 확인을 수행한다. 화면 조회·키 저장·모델 준비·예산 저장에는 유료 호출이 없다.

실제 `pythonw`·Chrome 연결은 검사했으며 Windows 파일 연결과 OS 기본 브라우저의 인증 완료는 미검증이다. 현재 Codex 토큰에서는 Script Host 설정 읽기가 Access is denied로 막혔다. 자동 열기 실패 시 다시 열기/취소 안내를 제공한다. 설치·CLI 대체 실행·실환경 제한은 [제품화 실행 안내](../guides/PRODUCTIZATION.md)를 참고한다.

## 설치와 설정

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[test,openai]'
```

환경변수 `OPENAI_API_KEY`, `HTRSA_MANAGER_MODEL`, `HTRSA_COORDINATOR_MODEL`, `HTRSA_WORKER_MODEL`을 설정한다. `.env.example`은 키 값 없이 변수 이름만 제공한다. 모델 ID는 실행 코드에 고정하지 않는다. Agents SDK 어댑터는 `Agent(output_type=...)`와 `Runner.run`을 사용한다. [공식 Agent 정의](https://developers.openai.com/api/docs/guides/agents/define-agents), [빠른 시작](https://developers.openai.com/api/docs/guides/agents/quickstart)을 참고한다.

제한된 Day 3B 연구 순환은 `HTRSA_VERIFICATION_MODEL`도 설정하고 `--autonomous`로 실행한다.

```powershell
.\.venv\Scripts\python.exe -m htrsa.agent_cli state.sqlite workspace tests\fixtures\monotonic_nonlinear.csv --goal "온도와 성장의 관계를 분석해 주세요." --autonomous
```

가설을 최대 3개로 추리고 하나를 활성화한다. 검증된 실험에 대한 검증 Coordinator의 비판과 승인된 후속 실험을 수행한다. 기본 상한은 작업 30회, 가설 5개, 활성 가설 2개, 분기 깊이 2, 후속 실험 2회다. 문맥 검색은 제한된 어휘 검색과 깊이 2의 의존 관계 탐색을 사용한다. 검색 가중치·역할별 예산은 `ContextConfig`로 설정한다. 토큰은 보수적인 UTF-8 바이트 추정으로 계산한다. 문맥 지표와 실행 추적은 정본 `state_events`와 분리해 저장한다.

```powershell
.\.venv\Scripts\python.exe -m htrsa.agent_cli state.sqlite workspace tests\fixtures\temperature_growth.csv --goal "온도와 성장의 관계를 분석해 주세요."
.\.venv\Scripts\python.exe -m htrsa.agent_cli state.sqlite workspace --resume RESEARCH_ID
.\.venv\Scripts\python.exe -m htrsa.agent_cli state.sqlite workspace --autonomous --resume RESEARCH_ID
```

확인한 모델 가격은 `config/pricing.json`의 `entries`에 `provider`, `model`, `input_per_million`, `cached_input_per_million`, `output_per_million`, 보수적인 `max_call_usd`로 등록한다. 다른 파일은 `HTRSA_PRICING_FILE`로 지정한다. 가격 누락 시 추정 비용은 미상이며 사용 토큰은 기록한다. 기존 Agent CLI는 이미 기록한 지출이 한도에 도달한 경우만 금액 한도로 차단한다. 실제 예산 제어에는 확인한 가격을 등록한다.

기존 CLI의 미상 가격 모델은 연구당 요청 20회, Manager 요청 5회, 입력 100,000토큰, 출력 20,000토큰으로 제한한다. 요청 전에 입력 4,000·출력 1,000토큰을 예약한다. `UnknownPriceLimits`로 변경할 수 있다. 응답이 예약량을 넘으면 실제 사용량을 기록하고 추가 호출을 중단한다. 사용량과 호출 횟수는 SQLite에 유지된다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q -m live_api
```

기본 테스트는 실제 API 검사를 제외한다. 모의 응답을 사용하여 API 비용이 들지 않는다. 실제 검사는 `OPENAI_API_KEY`와 `HTRSA_MANAGER_MODEL`이 없으면 건너뛴다.

Day 3A Worker 저장·복구와 자율 다중 실험의 `--autonomous --resume RESEARCH_ID`를 지원한다. 저장된 커서와 정본 사건·계약·도구 추적·잠정 변경·검증 기록을 대조하고 완료한 효과는 반복하지 않는다. 대기 변경은 기존 기록에서 검증·반영한다. 설명되지 않는 상태 변화는 `RESUME_STATE_CONFLICT`로 차단한다. 무효화된 실험은 오래된 근거를 인용하지 않고 `INSUFFICIENT_DATA`로 종료한다. 중단된 제공사 시도도 재시도·미상 가격 요청 할당량을 소비한다.

## 문헌과 최종 보고서

`--literature`는 자율 실험 전에 문헌 검색 3회, 활성 가설 선택 뒤 반박 검색 1회를 수행한다. OpenAlex를 기본으로 사용하고 Crossref는 검색 대체·DOI 조회를 제공한다. `OPENALEX_API_KEY`, `CROSSREF_MAILTO`는 선택 사항이다. 검색 결과는 SQLite에 저장해 재개 시 재사용한다. 상한은 검색 4회, 검색당 결과 10개, 관련 출처 12개, 검증된 문헌 8개다. 실패는 `LITERATURE_INCOMPLETE`로 기록하며 근거를 만들지 않는다.

```powershell
.\.venv\Scripts\python.exe -m htrsa.agent_cli state.sqlite workspace tests\fixtures\monotonic_nonlinear.csv --goal "인과성을 추론하지 말고 온도와 성장의 관계를 분석해 주세요." --literature
.\.venv\Scripts\python.exe -m htrsa.agent_cli state.sqlite workspace --resume RESEARCH_ID --literature
.\.venv\Scripts\python.exe -m pytest -q -m live_search
```

공개 문헌 검사는 `HTRSA_LIVE_SEARCH=1`로 명시적으로 켠다. 제공사를 사용할 수 없으면 건너뛴다. `--literature`는 `HTRSA_LITERATURE_REVIEW_MODEL` 또는 기존 `HTRSA_VERIFICATION_MODEL`을 사용한다. 문장별 구조화 검토 뒤에 원문·해시·지원/반박 검사를 통과해야 검증된다. 모의 테스트는 제한된 결정적 검토기를 사용한다. 보고서와 구조화 결과는 `workspace/RESEARCH_ID/research_output/`에 생성된다. 메타데이터·초록만 있는 자료는 본문 타당성이나 인과성을 입증하지 않는다. 중요한 주장은 외부 공개 전에 분야 전문가의 검토가 필요하다. 참조·해시·실험 산출물·수치 출처가 해석되지 않으면 내보내기를 차단한다.

## 환경과 출시 검사

```powershell
.\.venv\Scripts\python.exe -m htrsa.preflight docker
.\.venv\Scripts\python.exe -m htrsa.preflight build-sandbox
.\.venv\Scripts\python.exe -m htrsa.preflight api
.\.venv\Scripts\python.exe -m htrsa.preflight validate-core
.\.venv\Scripts\python.exe -m htrsa.preflight validate-docker
.\.venv\Scripts\python.exe -m htrsa.preflight smoke-api
.\.venv\Scripts\python.exe -m htrsa.preflight status
```

`build-sandbox`는 `docker/Dockerfile.sandbox`로 `htrsa-sandbox:3.13` 이미지를 만든다. `pytest -m docker_integration`은 실제 격리 검사 4개, `pytest -m live_api`는 구조화 Manager 요청 1회를 수행한다. 둘 다 선택적이며 환경이 없으면 건너뛴다. `pytest -m live_search`는 문헌 제공사를 별도로 검사한다. 소스 지문이 포함된 결과는 `build/validation/`에 저장한다. `demo_ready`는 핵심·파괴적·데모 A/B·산출물 검사를 요구한다. `release_ready`는 현재 소스와 환경에서 Docker·실제 API·실제 문헌 검증도 요구한다. 상태·사전 검사는 API 키를 출력하지 않는다.

**P0 환경 검증 대기:** 이 PC에서 Docker를 사용할 수 없어 격리 검사 4개를 건너뛰었다. 출시 전에 실행 가능한 데몬과 이미지로 검사해야 한다. `DOCKER_UNAVAILABLE`는 실행을 차단하며 호스트 대체 실행을 허용하지 않는다. 키·Manager 모델이 없으면 실제 API도 미검증이다.

## Day 4B 대시보드와 출시 후보

대시보드는 StateService의 읽기 전용 투영이다. 브라우저에 SQLite 연결이나 정본 쓰기를 제공하지 않는다. 실행 명령은 다음과 같다.

```powershell
python -m htrsa dashboard state.sqlite workspace --mode DEMO
```

API는 `/api/research/<research_id>`의 `tree`, `hypotheses`, `evidence`, `experiments`, `verification`, `timeline`, `usage`, `artifacts`, `report`와 `/api/environment`를 제공한다. `LIVE`, `DEMO`, `OFFLINE-CACHED`를 명시한다. 보고서는 이스케이프해 표시하고 산출물 경로는 연구 범위의 작업 공간에서 검증한다.

네트워크 없이 고정 데모를 실행한다.

```powershell
python -m htrsa demo --scenario adaptive_research
python -m htrsa demo --scenario invalidation_recovery
```

고정 자료·모의 문헌 제공사로 참조·수치 출처·비판/재계획·무효화를 검증하고 `demo_manifest`를 저장한다. 데모 성공을 실제 제공사 검증으로 취급하지 않는다.

종료된 연구를 자격 증명 없이 내보낸다.

```powershell
python -m htrsa export RESEARCH_ID --database state.sqlite --workspace workspace --output release
```

내보내기는 `research_output/`, `manifests/release_manifest.json`, `environment_status.json`, `README.md`, `REPRODUCE.md`를 작성한다. 비밀 파일을 거절하고 해시·스키마·마이그레이션·모델·제공사·데이터·산출물·테스트·환경 상태를 기록한다. 변경 허용 범위는 [FEATURE_FREEZE.md](FEATURE_FREEZE.md)를 참고한다.

## QA Day 1 회귀 검증

고정 파괴적 검사, 각 데모 5회, 새 프로세스 충돌·복구, 출시 내보내기와 전체 회귀 검사를 실행한다.

```powershell
.\.venv\Scripts\python.exe qa\qa_day1.py
.\.venv\Scripts\python.exe qa\performance_smoke.py
.\.venv\Scripts\python.exe qa\secret_scan.py
.\.venv\Scripts\python.exe qa\literature_tamper_probe.py
```

검증기는 `qa/results/stress_results.json`, `qa/results/demo_repeatability.json`, `qa/results/artifact_validation.json`, `qa/results/environment_validation.json`, `qa/results/qa_day1_summary.json`을 저장한다. 실행마다 `build/qa-day1/` 아래 새 디렉터리를 사용한다. Docker·실제 API의 환경이 없으면 `NOT_VALIDATED`로 남긴다. 출시 내보내기는 정본 버전·보고서·산출물·데이터·출처를 다시 검사하고 데모 선언 파일의 로컬 절대 경로를 제거한다.

## 선택적 F3-P v0.3

기존 Agent CLI의 `--verification-repair`는 검증 실패 뒤 고정 계획의 제한된 재실행을 활성화한다. Skills와 실험적 Ridge는 별도 플래그다. 수정본은 전체 검증과 반영 직전 검사를 통과해야 하며 원래 실패 시도는 감사 기록에 남긴다. 설정·복구·예산·평가·실제 비교 준비는 [F3P.md](../guides/F3P.md)를 참고한다.
## 선택적 한국어 연구 작업대

인증된 GUI와 실행 제어는 [WORKBENCH.md](../guides/WORKBENCH.md)에 설명한다.
`python -m htrsa.workbench DATABASE WORKSPACE`로 실행한다. 기존 CLI·데모도 사용할 수 있다. Skills·F3-P·실험적 Ridge는 새 연구에서 명시적으로 켜야 한다.

## 선택적 Research Slice v2

주장·근거 연결, 수정본 무효화, 검증 의무·의존 정보와 오프라인 쌍 비교 실험실은 [RESEARCH_SLICE.md](../guides/RESEARCH_SLICE.md)에 설명한다. 기능은 기본 OFF이며 Skills·F3-P·Ridge와 독립적으로 활성화한다.

## 다중 제공사 API·모델 설정

7개 어댑터·기능별 검사·숙고 매핑·가격·복구·보안 경계는 [MULTI_PROVIDER.md](../guides/MULTI_PROVIDER.md)를 참고한다. 설정 저장만으로 모델을 호출하지 않으며 실제 검사는 별도 비용 동의를 요구한다.
