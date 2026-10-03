# 첫 Live API 검사 기록

## 범위

설정 → **API 검사**에서 검사한다. 기본은 모델 목록과 짧은 텍스트 1회이며, 구조화 출력·검사 도구 왕복·스트리밍·Gateway 통합은 직접 선택한다. 기본 실행은 OFF이며 무료 사전 검사와 화면 조회는 생성 요청을 보내지 않는다.

이번 작업에서 승인된 전체 상한은 **0.10 USD**다. 각 세션은 지정한 상한과 기존 요청·월 예산을 모두 적용한다. 확인한 단가, 최대 출력량, 전송 승인과 자격 증명이 없으면 전송 전에 차단한다. 자동 재시도는 없다.

## 사용

먼저 작업대에서 모델을 준비하고 키를 등록한다. 키를 명령행·채팅·저장소에 적지 않는다.

```powershell
.\.venv\Scripts\python.exe -m htrsa.workbench build/workbench/state.sqlite build/workbench/workspace
```

실제 저장 프로필 ID를 `PROFILE_ID`에 넣는다. 아래 사전 검사는 유료 생성 요청을 보내지 않는다.

```powershell
.\.venv\Scripts\python.exe -m htrsa.live_api_test build/workbench/state.sqlite build/workbench/workspace --profile PROFILE_ID --budget-cap 0.10 --output-dir qa/live_api_test
```

목록 조회는 연결의 `discovery_unmetered` 승인도 필요하다. 제공사 문서·목록 비용을 확인하고 기존 연결 설정에서 승인한다. 목록 조회를 생략하려면 `--cases text`를 직접 지정한다.

```powershell
.\.venv\Scripts\python.exe -m htrsa.live_api_test build/workbench/state.sqlite build/workbench/workspace --profile PROFILE_ID --budget-cap 0.10 --live --consent --output-dir qa/live_api_test
```

상위 검사는 지원 근거가 있는 모델에서 직접 선택한다. 전체 상한은 계속 0.10 USD다.

```powershell
.\.venv\Scripts\python.exe -m htrsa.live_api_test build/workbench/state.sqlite build/workbench/workspace --profile PROFILE_ID --budget-cap 0.10 --live --consent --cases text,structured --output-dir qa/live_api_test
```

기존 세션을 다시 내보내면 HTTP 요청을 보내지 않는다.

```powershell
.\.venv\Scripts\python.exe -m htrsa.live_api_test build/workbench/state.sqlite build/workbench/workspace --budget-cap 0.10 --session SESSION_ID --output-dir qa/live_api_test
```

## 증거

세션마다 UUID와 전송 전 고정한 manifest hash를 기록한다. 같은 요청 키의 재실행은 기존 결과를 반환하고, 새 세션은 새 요청 키를 사용한다. 설정·키가 바뀌면 이전 기록은 역사적 결과로 보존하며 현재 연결 근거는 STALE로 표시한다.

패키지는 `manifest.json`, `summary.json`, `attempts.jsonl`, `capabilities.json`, `cost.json`, `latency.json`, `environment.json`, `redaction_scan.json`, `report_ko.md`, `artifact_manifest.json`으로 구성된다. Gateway 직렬화·예약·전송·응답·정산 단계는 기존 `control_audit`를 재사용한다.

요청 설정과 실제 직렬화된 매개변수를 구분한다. 제공사 적용 수준·모델 수정본·미보고 토큰·측정하지 않은 지연은 UNKNOWN으로 남긴다. reasoning 토큰을 output 비용에 중복 합산하지 않는다. 앱 단가 추정은 청구서가 아니며 미정산 노출이 있으면 총 관측 비용은 UNKNOWN이다.

비공개 추론과 프로토콜 연속성 원문은 일반 기록에 저장하지 않는다. 원응답은 hash 참조만 기록한다. 고정 검사 문장만 허용하고 임의 연구 자료·일반 연구 도구를 노출하지 않는다. L4는 `get_test_value`의 `key=probe` 계약만 검사한다.

## 통합과 기존 기준

DB 마이그레이션과 새 의존성은 없다. 기존 `ControlStore`, `Credentials`, `RoutedGateway`, 제공사 어댑터, 비용 원장, 소유자 인증과 CSRF 경계를 재사용한다. 새 strict schema·단일 도구 제어는 명시적으로 선택한 검사에만 적용한다. 기본 요청의 이전 캐시 키는 보존한다.

L5는 기존 Gateway와 앱 스키마 검증의 제한된 통합 검사다. 전체 Agent 연구, 과학 정본 반영과 품질 벤치마크를 수행하지 않는다. 저장 연구 설정을 참조할 경우 성능·자동 예산·Skills·F3-P·Ridge·연구 상한을 전송 전에 기록하지만 검사 자체의 연구 기능은 OFF다.

새 연결 결과는 해당 제공사·모델·선택 설정에 한정한다. 기존 SDK 구조화 smoke, Docker, 문헌 검색, 출시 게이트는 그대로 유지한다. 이 검사 도구는 기존 과금 경계 밖의 SDK 테스트를 자동 실행하지 않는다. Skills·F3-P·연구 품질의 라이브 효능은 NOT_VALIDATED다.

## 오프라인 검증

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_live_api_recording.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe qa/live_api_recording_probe.py
node qa/live_api_recording_visual_qa.cjs
.\.venv\Scripts\python.exe qa/qa_day1.py --output-dir build/live-record-final-source-qa
```

모의 HTTP 검사는 프로토콜·예약·복구·비밀·내보내기 경계의 오프라인 정확성만 확인한다. 속도·비용 절감·모델 성능 향상은 주장하지 않는다.

## 참고

- [OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning): 출력 상한과 reasoning 사용량
- [OpenAI 구조화 출력](https://developers.openai.com/api/docs/guides/structured-outputs): strict schema와 앱 검증 구분
- [OpenAI API 개요](https://developers.openai.com/api/reference/overview): 제공사 요청 ID
- [Langfuse 추적 구성](https://langfuse.com/docs/observability/best-practices): 세션별 호출 표와 상세 기록을 화면 구성에 참고

