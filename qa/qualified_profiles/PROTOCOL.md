# 한정 연구 프로필 평가

## 사전 기준과 실행 구분

구현 전 기준선과 채택 기준은 `build/beginner-v4/initial-state.json`의 2026-10-03T04:00:07Z 기록에 고정했다. 정상 완료율 감소 허용은 5%p 이내, 정상 과잉 보류는 10% 이내다. 잘못된 승인이나 실제 검토 부담이 개선되어야 하며, 사용한 값 변경 시 무효화와 사용하지 않은 값 변경 시 유지가 필요하다. Live 비교를 실행하기 전에는 이 기준을 충족했다고 선언하지 않는다.

- `evaluate.py`: 실제 공개 자료·로컬 도구·StateService에 모의 Agent 결정과 명시적 결함을 주입한다. 성능·경쟁 우위 평가가 아니다.
- `architecture_benchmark.py`: 정상 4·결함 6·변경 3·모호하거나 미지원 3건의 동일 조건 입력과 평가자 전용 해답을 준비한다. 실제 결과 캡처를 비교하는 채점기를 제공한다. API를 호출하지 않는다.
- 개발 자료는 `tests/test_beginner_v4.py`, 평가 변형은 `build/beginner-v4/evaluation/`의 무작위 식별자, 실측 공개 원본은 `public/`이다. 모두 같은 NASA 제품의 변형이며 독립 연구로 세지 않는다.
- 비교 준비 입력은 `agent_visible/각시스템/무작위ID/`, 정답·결함 분류는 그 밖의 `evaluator_only/`이다. 실제 Agent 실행에서는 같은 작업 공간 경계를 적용해야 한다.

## 강한 단일 Agent와 공정성

단일 Agent에도 계획·전체 자료·같은 Python 통계 도구·자체 검토·수정 기회를 제공한다. 양쪽의 실제 frontier 모델 ID, 데이터, 출처 설명, 작업 공간, 재시도, 호출·도구·시간·비용 한도와 출력 요구가 같아야 한다. 대조군을 한 번의 텍스트 답변으로 제한하지 않는다.

준비 명령의 모델 문자열은 설정할 모델을 나타낼 뿐, 실제 모델 사용이나 최신성 검증의 증거가 아니다. 실제 실행 캡처에는 제공사 사용량과 자원 기록의 해시를 포함한다. 캡처 채점은 제공사 실행의 독립 인증이 아니다. 어댑터별 실제 실행 및 증빙 검토가 없으면 Live 효능 게이트는 `NOT_VALIDATED`다.

```powershell
.\.venv\Scripts\python.exe -X utf8 qa/qualified_profiles/evaluate.py
.\.venv\Scripts\python.exe -X utf8 qa/qualified_profiles/architecture_benchmark.py --folder build/architecture-pilot --model ACTUAL_FRONTIER_MODEL_ID --budget 0.10
.\.venv\Scripts\python.exe -X utf8 qa/qualified_profiles/architecture_benchmark.py --folder build/architecture-pilot --probe-capture PROBE_CAPTURE.json --single-agent-capture SINGLE_AGENT_CAPTURE.json
```

유료 실행과 외부 제품 계정 조작은 이 준비 명령에 포함하지 않는다.

## 지표와 해석

우선순위는 지원 범위의 유효한 완료, 잘못된 승인, 정상 과잉 보류다. 다음으로 목표·물리량·시공간 의미 유지, 오래된 결론 재사용, 재계산·복구, 실제 사람의 검토·수정 부담을 본다. 비용과 지연은 그다음이다. 문장·결론 표현·실행 경로의 일치를 비교하지 않는다.

화씨 편차는 1.8배이며 32를 더하지 않는다. 같은 잘못된 상수는 차이에서 상쇄되더라도 두 평균과 편차 의미가 틀릴 수 있다. 기준 기간을 일관되게 바꾸면 차이는 유지되지만 평균의 해석은 달라진다. 원래 질문에 승인된 짧은 기간을 사용한 결과는 불필요하게 보류하지 않는다.

의미/목표 검사, 주장 변경 영향, 제한 복구, 한정 절차의 각각 OFF/ON Live ablation은 미실행이다. 기존 F3-P 결함/복구 결과를 새 프로필의 효과로 귀속하지 않는다. 검증 안전장치를 실제 제품에서 끄는 방식으로 평가하지 않는다.

## 외부 제품과 사용성

Gemini Notebook, ChatGPT/Codex/Claude, DataClassroom/jamovi는 정상 제품 흐름으로 비교해야 한다. 기록 필드는 날짜·계정/기능 접근·준비 시간·수행 순서·수정/검토 시간·실제 한계다. 이번에는 계정 접근과 수행을 검증하지 않았으므로 모두 `NOT_VALIDATED`다.

학생·교사 사용성 검사도 `NOT_VALIDATED`다. 향후 첫 연구 완료 여부, 고급 설정을 열었는지, 실수·도움 요청, 설명 선택/재설정, 결론 범위 설명, 데이터 수정 후 재확인 시간, PDF 저장을 실제 관찰하고 기록한다. 모의 UI 검사와 사용자 연구를 구분한다.

## 공개 자료의 한계

원본은 NASA GISTEMP v4 전 지구 연간 L-OTI다. TXT의 0.01°C 저장 배율과 CSV의 소수 °C 값이 실제로 같은 연간 수치인지 검사했다. 조회 시점·해시는 `public/source.json`과 평가 결과에 남긴다. 현재 실행 어댑터는 공식 TXT를 지원한다. 임의 첨부·CSV·다른 제품은 이 프로필로 자동 분석하지 않으며 필요한 의미를 확인한다.

두 기간의 관측 평균 비교만 한정 절차가 제공한다. 인과 식별·미래 예측·지역 기온·절대 온도 복원·확증적 유의성은 지원하지 않는다. 재현은 계산·기록 일치의 확인이며 과학적 참의 증명이 아니다.
