# Probe 연구 작업대

## 실행

기존 Python 환경을 사용합니다. GUI는 별도 opt-in entry point입니다.
기존 CLI와 읽기 전용 dashboard 명령은 유지됩니다.

```powershell
New-Item -ItemType Directory -Force build/workbench/workspace/inputs
Copy-Item -LiteralPath tests/fixtures/temperature_growth.csv -Destination build/workbench/workspace/inputs/data.csv
.\.venv\Scripts\python.exe -m probe.workbench build/workbench/state.sqlite build/workbench/workspace
```

기본 브라우저가 자동으로 열리고 코드 입력 없이 연구 목록으로 연결됩니다.
`127.0.0.1`의 OS 배정 포트를 사용하며, `--port 8766`처럼 고정 포트도 지정할 수 있습니다.
60초짜리 1회용 티켓을 URL 조각으로 전달하고 화면에서 즉시 지운 뒤 세션으로 교환합니다.
세션은 인스턴스별 HttpOnly/SameSite=Strict cookie와 프로세스 메모리로만 인증하며 12시간 뒤 만료됩니다.
HTTP이므로 Secure 보호를 주장하지 않습니다. 같은 Host/Origin과 CSRF 검사를 유지합니다.
자동 열기 실패 또는 `--no-browser`에서는 출력된 1회용 링크만 클릭하면 됩니다.
개발자 수동 연결은 `--manual-pairing`을 명시해야 사용할 수 있습니다.
LAN/HTTPS 원격 배포는 제공하지 않습니다. 자세한 경계는 [로컬 인증](LOCAL_BROWSER_AUTH.md)에 있습니다.

실제 offline F3-P 기록을 별도 DEMO 공간에 생성해 검토할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe qa/gui_visual_fixture.py
$guiFixture = Get-Content -LiteralPath build/gui_visual_fixture.json -Raw -Encoding utf8 | ConvertFrom-Json
.\.venv\Scripts\python.exe -m probe.workbench $guiFixture.database $guiFixture.workspace --mode DEMO
```

DEMO 표시가 유지되며 이 모드의 GUI 유료 연구 실행은 차단됩니다.

## 설정

- **모델**: 상단 5개 모델, 제공사별 더보기, 연결 선택, 월·요청 예산.
- **API 연결**: 연결·키 동시 등록, 키 교체·삭제.
- **API 검사**: 사전 검사, 유료 검사 동의, 검사 이력.
- **고급 설정**: 직접 모델 ID, 역할별 구성.
- **실행 환경**: 패키지·검증·보안·저장 상태.

설명 문단 없이 항목·상태·입력·버튼을 표시합니다. 과금·전송 동의는 유지합니다.

## 실제 동작

- 연구 목록/개요/진행 기록/근거/실험/검증/보고서/자료 보관함은 실제 SQLite와
  hash 검증된 산출물을 읽습니다. 화면 조회는 모델을 호출하지 않습니다.
- 새 연구에서 선택한 `workspace/inputs`의 UTF-8 CSV를 사용합니다.
  외부 경로·symlink·5 MB 초과·비밀 패턴을 차단하며 source hash를 고정합니다.
- 연결과 모델은 별개입니다. OpenAI·Anthropic·Gemini·xAI·DeepSeek·Mistral 공식 프로토콜과 제한된 호환
  프로토콜을 제공합니다. 구조화 결과는 기존 Pydantic 모델로 검사합니다.
  호환 Chat은 provider-enforced schema라고 표시하지 않습니다.
- 수동 모델 ID를 사용할 수 있습니다. 저장만으로 기능 검사를 통과하지
  않습니다. 실제 짧은 응답 검사는 명시적 비용 동의와 동일 원장을 사용합니다.
  도구 호출·스트리밍·숙고는 각각 별도 검사하며, 미검사 상태는 미확인입니다.
- 모델 목록 조회의 무과금을 확인하지 않으면 차단하고 수동 ID 경로를
  제공합니다. 확인된 무과금 로컬 목록 조회도 예약·dispatch 기록을 남깁니다.
- 연결 주소 수정은 이전 snapshot에 전파되지 않습니다. active run에서
  연결이 달라지면 destination 재승인 필요 상태로 차단합니다. fallback 없음.
- Skills/F3-P/Ridge는 각각 기본 OFF입니다. 과거 실행의 resolved flag는
  기존 runtime_steps 기록을 사용합니다. global 설정으로 수정하지 않습니다.
- F3-P 원래 분석·실패 증거·qualification·결정·수정본·재검증을 분리합니다.
  commit 및 필수 검사 전체 완료가 확인되어야 verified로 표시합니다.
  audit hash가 손상되면 성공 표시하지 않습니다. 전역 절차 승인을 만들지 않습니다.

## 깊이와 실행

미검증 제안 preset을 기존 실행기의 더 엄격한 상한과 결합합니다.
실제 GUI 경로는 최대 가설 5, shortlist 3, 활성 가설 2, 가설별 branch depth 2,
후속 검토 2, 모델 시도 20입니다. 탐색은 더 낮은 상한을 적용합니다.
문헌 검색은 이 실행 경로에서 꺼져 있으므로 자료 검토 상한은 0이고,
선택한 CSV를 기존 고정 도구로 분석합니다. 상세 snapshot에 제안값과
실제 적용 상한을 별도로 남깁니다. 기존 문헌 CLI/demo는 유지됩니다.

실행은 AutonomousResearchLoop를 그대로 사용하며 일시정지/중지는
새 배정을 차단하고 현재 작업 뒤 안전한 경계에서 반영합니다.
이미 수락된 원격 요청은 과금될 수 있습니다. 창 닫기/새로고침은 실행을
중복 시작하지 않습니다. start/resume은 idempotency key와 상태 version을
검사합니다. 원격 과금의 exactly-once를 보장하지 않습니다.
실행 중 깊이 변경은 GUI/owner API에서 요청하고 안전한 경계에서 감사 기록과
함께 적용합니다. 돈·egress·과거 snapshot은 자동으로 늘리거나 바꾸지 않습니다.

## 예산과 복구

제어 테이블과 원장은 **기존 SQLite**에 additive schema v1로 생성합니다.
처음 적용할 때 `.pre-workbench.sqlite` 호환 백업을 보존합니다.
기존 001–006 마이그레이션을 바꾸지 않습니다. 백업 복원은 앱을 중지하고
현재 원장의 미정산/정산 노출을 보존·대조하는 유지보수 절차가 필요합니다.
연구 checkpoint rollback이나 연구 export는 원장을 포함하지 않습니다.

GUI와 workbench worker CLI의 모든 구현된 모델 요청은 RoutedGateway를
통과합니다. Decimal을 보수적으로 반올림한 integer micro-USD 예약을
BEGIN IMMEDIATE에서 월·연구·요청 한도와 함께 검사합니다. 연결별 동시성은 1.
가격은 owner가 확인한 USD rate/source/revision/timestamp만 사용하고
30일 초과·가격 누락·무한 출력량·context 초과는 차단합니다.
현재 환율을 만들지 않고 USD 원액을 보존합니다. 원화 청구액을 보장하지 않습니다.

SDK/HTTP 내부 자동 retry 없음. timeout/usage 누락/거절의 불확실한 노출은
UNRESOLVED로 유지합니다. 재시작 시 RESERVED/DISPATCHED도 노출을 보존하며
자동 재요청하지 않습니다. 성공 결과와 정산은 원자적으로 저장해
동일 입력·계약·모델 요청의 재개 시 재과금 없이 결과를 재사용합니다.
미정산 금액을 근거 없이 무료로 바꾸는 UI는 없습니다.
기존 legacy agent CLI의 기존 BudgetController 정책은 호환성을 위해 유지합니다.

완료·자료 부족·예산 중단·작업 한도·재검증 미완료 상태를 구분합니다.
worker가 시작되지 않거나 종료되면 과금 liability와 typed 실패 사유를 보존합니다.
모델 응답에 활성 제공사 키가 포함되면 저장 전에 차단하고 미정산으로 남깁니다.

Release exporter는 manifest에 선언한 연구 출력만 복사합니다. spreadsheet용
CSV의 수식 시작 셀은 apostrophe로 이스케이프하고 원본 바이트는 별도
`canonical_text/*.csv.raw.txt`에 보존합니다. portable manifest는 원본/표시 hash를
분리합니다. 이 raw archive는 spreadsheet로 import하지 않아야 하며 canonical
workspace와 DB의 scientific hash는 바뀌지 않습니다.

## 키·보안·환경

키는 환경변수 → Windows 자격 증명 관리자 → repository/workspace 밖의 사용자 전용
보호된 평문 `secrets.env` 순서로 읽습니다. 환경변수가 우선합니다.
Windows owner/SYSTEM/Administrators/OWNER RIGHTS ACL, POSIX mode를 검사하고
atomic write 합니다. sandbox 권한상 사용자 설정 경로를 쓸 수 없으면
저장은 실패하며 환경변수 경로를 표시합니다. 프로젝트 내부 fallback 없음.
연결 생성 화면에서 API 키를 함께 저장합니다. 공식 서버 주소와 키 참조는 자동으로 채우고 고급 설정에 둡니다.
삭제한 키는 저장된 로컬 키 목록에서 제거하며 연결 정보는 유지합니다. 로컬 삭제는 제공사 폐기가 아닙니다.

키는 SQLite/snapshot/프롬프트/브라우저 저장소/export/로그에 저장하지 않습니다.
전용 password 입력은 성공/취소 후 제거합니다. 관리형 메모리 zeroization을
보장하지 않습니다. 문헌/모델은 권한·설정·예산을 바꿀 API/session을 받지 않습니다.
Docker CLI/분석 container는 제공사 비밀 환경변수를 상속하지 않습니다.

모델 URL은 단일 승인 origin으로 제한하고 DNS 주소를 검증·pin하며
TLS SNI를 유지합니다. redirect 및 다른 origin으로 Authorization 전달 없음.
public research-fetch 권한으로 재사용하지 않습니다. 원격 이미지/HTML/SVG
자동 실행 없이 text escape와 self-only CSP를 적용합니다.
외부 SDK tracing은 Agent 초기화 전에 꺼집니다.

엄격한 `외부 전송 안 함`은 independently managed local daemon의 cloud
forwarding을 검증할 수 없어 실행 차단합니다. loopback만으로 no-egress를
보증하지 않습니다. 임의 생성 코드·자동 설치·모델 다운로드·GPU 임대 없음.
Docker/Live LLM/Live Search/Skill 및 F3-P live efficacy는 실제 실행한
gate만 사용합니다. 연결 성공은 연구 품질 PASS가 아닙니다.

## 화면 근거와 검증

공식 [Linear](https://linear.app/docs/conceptual-model)의 목록·상태,
[Zotero](https://www.zotero.org/support/quick_start_guide)의 자료/상세 패널,
[VS Code](https://code.visualstudio.com/docs/editing/getting-started/userinterface)의
작업대 탐색, [MLflow](https://mlflow.org/docs/latest/ml/tracking/)의 실행/산출물 연결,
[Carbon](https://carbondesignsystem.com/components/data-table/usage/)의 표 동작을 참고했습니다.
제품 자산은 복사하지 않았습니다. OpenAI tracing 정책은
[공식 Agents 문서](https://developers.openai.com/api/docs/guides/agents/integrations-observability)와
설치된 SDK RunConfig를 확인했습니다.

실제 Chrome screenshot/검증 명령:

```powershell
node qa/gui_visual_qa.cjs
```

기존 번들 Playwright 경로를 사용합니다. 앱 dependency를 설치하지 않습니다.
결과는 `qa/results/gui_visual_results.json`, 화면은 `output/playwright`입니다.
200% reflow는 1280×900 기준 640×450 CSS viewport에서 검사합니다.
OS/browser native zoom 조작을 실제 수행한 것으로 주장하지 않습니다.
