# Cycle 12 보완 검증

[구현 보고서](IMPLEMENTATION_REPORT.md) · [32개 요구와 검사 규약](PROTOCOL.md) · [수정 전 점검](TRIAGE.md) · [천문 고정 자료](public/README.md)

## 실행 코드

| 파일 | 실행 범위 |
|---|---|
| `reference_review.py` | ZIP SHA-256·32개 패키지/19개 동결 파일·기존 72평가·별도 5개 진단 |
| `cross_domain.py` | 고정 천문 7행·키별 CSV/JSON 대조·평균·단위·선택 수정본 |
| `evaluate_v4.py` | 기존 v4 평가 함수의 정상 4·결함 6·변경 3·모호/미지원 3개 기대 상태 |
| `offline_validation.py` | 기존 파괴적 116개·Demo A/B 5회씩·복구·기본 release·F3-P 8사례/6복구/2조건 export |
| `recovery_probe.py` | 필수 형식 대조를 유지한 기후 Verify/Commit 직후 실제 종료·새 프로세스 재개 |
| `export_probe.py` | 질문 변경 후 기후 release·원본 hash·secret canary·변조·오래된 상태의 반영 차단 |
| `browser.cjs`, `browser_fixture.py` | 실제 Chrome·소유자 인증·결론·지연 자료 조회·질문 변경·재검증·PDF |
| `pdf_currentness_browser.cjs` | 실제 PDF 저장·검증된 보고서 미리보기·최신 현재성 조회·파일 변조·CSP 18개 검사 |
| `../../tests/test_cycle12.py` | 실제 앱 저장/검증 경계의 매개변수 회귀 |
| `../../tests/test_report_preview.py` | PDF와 미리보기의 동일 수치·해시·현재성·변조·비밀 값 차단 |

큰 작업 공간·JUnit·참조 원본·화면·PDF는 `build/cycle12/`에 저장한다. 기존 QA 실행 결과와 원래 사용자 연구를 덮어쓰거나 삭제하지 않는다. 새 파일·문서는 쓰기 전에 UTF-8 가능 여부를 검사한다.

## 선택적 사용

```powershell
.\.venv\Scripts\python.exe -m htrsa.workbench build/workbench/state.sqlite build/workbench/workspace
```

새 연구에서 기존 모델·예산을 설정하고 다음과 같은 지원 질문을 입력한다.

```text
1981~2000년과 2001~2020년의 전 지구 연간 기온 편차 평균을 비교해 주세요.
```

기후 한정 절차는 기존 AUTO 경로에서만 후보가 된다. 지원 범위 밖 질문은 일반 연구로 연결된다. 실제 외부 요청에는 기존 키·목적지 동의·가격·예산·검색 횟수·완료 비용 조건이 필요하다. 이 문서의 앱 실행 명령만으로 유료 연구나 API 검사를 자동 실행하지 않는다.

현재 결과에서 **질문 변경 → 저장 → 다시 계산**으로 승인한 기간/단위/기준 변경을 적용한다. 원래 질문과 이전 결과는 이력으로 남고 새 현재 결론은 실제 검증 후에만 표시한다. **자료 갱신**도 같은 변경 영향 경계를 사용한다. 자료 상세와 선택값은 펼칠 때 로컬에서 읽는다. 정책상 선택 대조가 없으면 미수행·한계를 표시하며 필수 대조의 부재는 결과 반영을 막는다.

```powershell
.\.venv\Scripts\python.exe -m htrsa.qualified_replay PATH_TO_EXPORTED_REPLAY_MANIFEST
.\.venv\Scripts\python.exe -m htrsa.preflight status
```

`PATH_TO_EXPORTED_REPLAY_MANIFEST`는 실제 내보낸 `replay_manifest.json` 경로로 바꾼다. replay는 저장한 원본·정책·승인 수정본·수치를 다시 검사하며 네트워크로 누락 파일을 대체하지 않는다. 이전 검증기 버전의 기후 결과는 현재성 검사 후 필요한 경우 로컬 재검증한다. 일반 연구·기존 보고서·과거 파일은 유지한다.

새 DB migration과 dependency는 없다. Skills·F3-P·Ridge·Cycle 5 기본 OFF, 일반 엔진·역할·정본 작성자·출시 게이트를 유지한다. Live 효능·실제 제공사 E2E·외부 제품/학생·교사 비교는 별도 미검증이다.
