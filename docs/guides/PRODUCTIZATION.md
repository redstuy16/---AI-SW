# 제품화 첫 단계 실행 안내

## 실행

Windows에서 가상환경 설치 후 저장소의 `Probe.wsf`를 더블클릭한다. `pythonw -m probe.desktop`로 기존 작업대를 열고 기본 자료는 `build/workbench/state.sqlite`, `build/workbench/workspace`에 저장한다. 새 연구 엔진이나 GUI 의존성은 없다.

설치와 CLI 대체 실행:

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[test,openai]'
.\.venv\Scripts\python.exe -m probe.workbench build/workbench/state.sqlite build/workbench/workspace
```

콘솔 없는 실행의 자료 위치를 직접 지정하려면 다음을 사용한다. 실제 저장소 내부 경로만 허용하며 심볼릭 링크·junction은 차단한다.

```powershell
.\.venv\Scripts\pythonw.exe -m probe.desktop --data-dir build/my-workbench
```

자동 브라우저 열기 실패 시 다시 열기/취소를 제공한다. 취소하면 작업대 서버를 닫는다. 이미 실행한 연구 Worker는 별도로 중지해야 한다. 브라우저 탭만 닫아도 Worker가 자동 중지되지는 않는다. WSF 파일 연결이나 기본 브라우저의 실제 인증 완료를 모의 검사로 통과 표시하지 않는다. 현재 Codex 토큰에서는 실제 `cscript`가 Script Host 설정 읽기에서 Access is denied로 막혔으므로 WSF 구문·더블클릭 실행도 미검증이다. `pythonw` 경로는 실제 Chrome QA로 확인했다.

## GPT 연결

1. **설정 → 모델**에서 GPT 모델을 선택하고 공식 전송 주소·단가 확인 후 준비한다. 상단 5개와 제공사별 더보기를 제공한다.
2. **API 연결**에서 제공사·API 키를 함께 저장한다. 생성된 연결을 모델에 선택한다. 키 입력은 즉시 지우며 저장 키를 다시 표시하지 않는다.
3. 검사 전체 상한을 정하고 유료 요청·메타데이터 조회에 동의한 뒤 **API 검사 → GPT 종합 검사 → GPT 연결 검사**을 누른다.
4. 월/요청 상한을 확인해 저장한다.
5. 새 연구에서 CSV·질문·모델·성능·연구 예산을 선택한다.

목록 조회, 텍스트, 구조화 출력, 고정 도구 왕복, 지원되는 LOW 숙고를 검사한다. 생성 요청은 최대 5회이며 같은 원장 연구 ID와 전체 상한을 사용한다. 기본 전체 상한은 USD 0.10, 최대 USD 0.25이다. 기존 월·요청 상한도 적용한다. 메타데이터의 무과금 여부는 소유자 승인에 근거한다. 실제 제공사의 전체 청구액을 보장하는 장치는 아니므로 제공사 지출 한도도 적용한다.

실패 후 자동 재시도하지 않는다. 같은 멱등 키의 재전송은 새 호출을 만들지 않는다. 프로세스 중단 시 미정산 원장은 `UNRESOLVED`, 검사는 `NEEDS_RECONCILIATION`으로 남긴다. 복구가 이전 외부 호출을 다시 보내지 않는다. 소유자 설정·키 교체 후 과거 성공은 `STALE`이 된다. 환경변수 키의 검증은 현재 프로세스에 한정하며 재시작 후 재확인이 필요하다. 외부에서 현재 프로세스의 환경변수 값을 직접 바꾸는 개발 코드는 별도 재검사해야 한다.

`VALIDATED`는 실제 HTTP 연결 검사 완료, `OFFLINE_VALIDATED`는 모의 HTTP 검사 완료다. 연결 성공으로 연구 정확성·Skills·F3-P 효능을 통과 표시하지 않는다. 다른 제공사도 모델 탭에서 선택한다. 모델 ID 수동 입력과 역할별 구성은 고급 설정에 유지한다.

## 키와 실행 경계

환경변수 → Windows Credential Manager → 권한을 검증한 보호 파일 순서로 키를 읽는다. Windows 저장 후 다시 읽어 확인하며, 기존 fallback 파일의 권한 오류는 OS 키 변경 전에 차단한다. OS 저장을 사용할 수 없는 지정 오류에만 기존 보호 파일 경로를 적용한다. 파일은 암호화 파일이 아니며 검사된 ACL에 의존한다.

키의 활성 출처·저장 여부·변경 시각만 반환한다. raw key는 SQLite·연구 자료·브라우저 저장소·진단·내보내기에 저장하지 않는다. 연구 subprocess/container의 환경 비밀 제거와 기존 전송·예산·검증·복구 기준을 유지한다. 로컬 키 삭제는 제공사 키 폐기가 아니다.

## 재사용한 제품 기능

- 기본 균형 성능 슬라이더와 접힌 역할별 고급 설정.
- `completion_reserve`로 필수 검증·재검증·반영·최소 보고서·내보내기 여유 보호.
- 안전한 경계의 연구 설정 수정과 의미 변경 시 별도 AnalysisPlan 연구 승인.
- 검증된 수치·그림·근거로 생성하는 9개 섹션 보고서, 기술 원문·추적 링크.
- 설정별 무엇/언제/비용/주의/추천 도움말, F3-P 실패·복구·재검증 표시.

이 기능의 상한·실험 기능 기본 OFF·기존 과학 계약을 바꾸지 않았다. 온보딩은 저장된 상태를 읽으며 진행 화면 조회로 유료 검사하지 않는다. 시작 안내 구성은 [VS Code 공식 시작 화면](https://code.visualstudio.com/docs/getstarted/overview)을 참고했다. API 형식은 [구조화 출력](https://developers.openai.com/api/docs/guides/structured-outputs), [함수 호출](https://developers.openai.com/api/docs/guides/function-calling), [숙고](https://developers.openai.com/api/docs/guides/reasoning) 문서를 확인했다.

기존 GPT 후보의 API 식별자와 단가는 [공식 모델 목록](https://developers.openai.com/api/docs/models), [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol) 안내를 다시 확인했다. 문서 확인은 사용자의 키로 모델을 호출한 근거가 아니며 실제 기능 검사를 별도로 수행해야 한다.

## 실환경 관측과 출시

```powershell
.\.venv\Scripts\python.exe qa/productization_native_probe.py
.\.venv\Scripts\python.exe qa/local_browser_launcher_probe.py
.\.venv\Scripts\python.exe -m probe.preflight status
```

첫 명령은 사용자 키를 변경하지 않는 별도 Windows canary와 새 QA DB의 소규모 GPT 연결 검사를 수행한다. GPT 키가 없다면 HTTP 요청 없이 미검증을 기록한다. 두 번째 명령은 실제 기본 브라우저 열기·티켓 교환·인증 목록 조회를 모두 관측해야 통과한다. 결과에는 현재 소스·사용자/컴퓨터 지문을 넣는다. 검사 canary를 Worker 환경·명령 인수·결과 파일에 전달하지 않는다.

기존 `release_ready` 게이트는 유지한다. 제품 출시는 추가로 `product_release_ready=true`가 필요하며 Windows 실제 저장/교체/삭제/재시작·기본 브라우저 인증까지 요구한다. 둘 다 실제 환경에 따라 `NOT_VALIDATED`로 남을 수 있다. 현재 실행 근거와 제한은 [제품화 구현 보고서](../../qa/results/productization_implementation_report.md)에 기록한다.

로드맵 1차 완료의 실제 GPT smoke·Windows 저장·OS 브라우저 조건이 남아 있으면 제품화 전체 완료를 주장하지 않는다. 문서 순서상 이후 단계인 Ollama/LM Studio의 실연결·공유 자원 검증·품질/전체 비용 비교, 추가 제공사 실검증·발표 모드는 이번 첫 단계 이후 범위다. 기존 호환 어댑터를 실행 가능한 로컬 연구 엔진으로 새로 검증했다고 표시하지 않는다.
