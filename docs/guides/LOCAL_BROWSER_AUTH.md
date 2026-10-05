# 로컬 브라우저 자동 인증

## 실행

```powershell
.\.venv\Scripts\python.exe -m probe.workbench build/workbench/state.sqlite build/workbench/workspace
```

`127.0.0.1`에 OS 배정 포트로 서버를 열고 기본 브라우저로 연구 목록에 연결한다. 정상 경로에서는 코드 입력이 없다. 고정 포트는 `--port 8766`처럼 지정할 수 있다. 기존 worker·연구·제공사·키 저장·예산·검증·복구·export는 그대로 사용한다.

## 연결 경계

- 티켓은 CSPRNG 256비트, 60초, 한 번만 사용한다. 프로세스 메모리에 해시만 보관한다. SQLite·설정·연구 산출물에 저장하지 않는다.
- URL 조각을 읽은 즉시 `history.replaceState`로 지운다. 같은 탭의 조각 변경도 처리한다. `POST /auth/bootstrap`은 정확한 Origin·JSON·사용자 지정 헤더를 요구하며 외부 redirect를 따르지 않는다.
- 티켓을 소비한 뒤 별도의 무작위 세션·CSRF 값을 만든다. 세션은 인스턴스별 쿠키 이름, host-only·HttpOnly·SameSite=Strict·Path=/·12시간 상한을 사용한다. HTTP이므로 Secure를 표시하지 않는다.
- 실제 `127.0.0.1:<port>` Host만 허용한다. 변경 요청은 Origin·세션·CSRF 검사를 함께 통과해야 한다. wildcard CORS는 없으며 CSP·프레임 차단·no-store·no-referrer를 유지한다.
- 티켓·세션·CSRF 값의 메모리 해시를 기존 설정 입력·공개 응답·diagnostic·export 비밀 검사에 연결했다. 인증 메타데이터의 CSRF와 Set-Cookie 전달은 정상 인증 프로토콜이며 로그에 기록하지 않는다.
- 브라우저 localStorage·sessionStorage·IndexedDB·service-worker 저장을 사용하지 않는다. 정상 시작은 티켓을 출력하지 않는다.

## 대체 경로

```powershell
.\.venv\Scripts\python.exe -m probe.workbench build/workbench/state.sqlite build/workbench/workspace --no-browser
```

자동 열기 실패나 headless 실행에서는 터미널의 60초짜리 1회용 링크를 클릭한다. 이 명시적 링크 출력은 일반 로그와 구분한다. 만료·재사용 시 원래 티켓을 화면에 표시하지 않고 Probe 재실행을 안내한다. 브라우저에서 새 티켓을 무인 발급하는 API는 없다.

기존 수동 연결이 필요한 개발 환경만 `--manual-pairing`을 사용한다. 일반 화면은 수동 입력 창을 열지 않는다. 프로세스 재시작은 새 티켓·세션·쿠키 이름을 만든다. 다른 인스턴스의 티켓과 세션은 인증에 사용할 수 없다.

## 확인한 범위와 제한

실제 Chrome과 등록한 QA 브라우저 컨트롤러에서 자동 연결·같은 탭·병렬 탭·인스턴스 격리·재시작·만료·headless·열기 실패·프레임·저장소를 검사했다. 실제 OS 기본 브라우저 함수도 호출했지만, 이번 실행에서는 반환값 true만 관측했고 25초 내 자동 교환·인증 목록 조회는 관측되지 않았다. 이 환경 항목은 NOT_VALIDATED다. `--no-browser` 링크 경로는 실제 Chrome에서 통과했다.

LAN·HTTPS 원격 배포는 지원하지 않는다. 침해된 OS 계정·동일 권한 악성코드·관리자·침해된 브라우저로부터의 보호를 주장하지 않는다. HttpOnly는 인증 쿠키에 적용되며 OS/브라우저 저장 자체를 암호화하는 기능이 아니다. 브라우저 종료나 backend 종료가 이미 수락한 연구 worker·원격 과금을 취소하지 않는다.

화면의 상태 안내는 [VS Code 신뢰 상태 UI](https://code.visualstudio.com/docs/editing/workspaces/workspace-trust)를 참고했다. 쿠키와 주소 처리 근거는 [MDN Set-Cookie](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie), [replaceState](https://developer.mozilla.org/en-US/docs/Web/API/History/replaceState)다.

검증 결과: [구현 보고서](../../qa/results/local_browser_auth_implementation_report.md).
