# QA 실행

명령은 저장소 루트에서 실행합니다. 기존 실행 파일 이름을 유지합니다.

| 위치 | 내용 |
|---|---|
| `*.py`, `*.cjs` | 회귀·복구·고장·화면 검사 실행 코드 |
| `fixtures/` | 고정 사례·평가 설정 JSON |
| `results/` | 로컬 검사 결과·과거 구현 보고서 (공개 제외) |
| `prepublish/` | 비밀·개인 경로를 제외한 공개 준비 보고서 |
| `live_api_test/` | 세션별 해시 검증 API 증거 패키지 |

## 자주 쓰는 명령

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m htrsa.preflight status
.\.venv\Scripts\python.exe qa/qa_day1.py
.\.venv\Scripts\python.exe qa/live_api_recording_probe.py
node qa/live_api_recording_visual_qa.cjs
node qa/connection_ux_visual_qa.cjs
.\.venv\Scripts\python.exe qa/prepublish_check.py --require-ready
node qa/hardening_visual_qa.cjs
```

연결 UI 검사는 모의 키 저장소와 실제 Chrome을 사용하며 실제 Windows 안전 저장 검증을 대신하지 않습니다.

기본 pytest와 모의 HTTP 검사는 실제 유료 API를 호출하지 않습니다. 실제 API 검사는 앱에서 검사 상한·실행 동의를 지정합니다. [첫 API 검사](../docs/guides/LIVE_API_TEST.md)

검사 결과는 기본 `qa/results/`에 저장합니다. `qa_day1.py --output-dir 경로`로 별도 결과 위치를 지정할 수 있습니다. 큰 작업 공간은 `build/`, 화면 캡처는 `output/`에 저장합니다.

[최신 구현·검증 보고서](prepublish/IMPLEMENTATION_REPORT.md) · [로컬 결과 목록](results/README.md) · [개발자 상세 명령](../docs/development/CLI.md)
