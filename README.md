# H-TRSA 연구 작업대

연구 질문을 입력하고, 자료·분석·결론의 검증 기록과 PDF를 확인하는 로컬 앱입니다. 공개 기후 자료의 두 기간 비교를 한정된 절차로 지원합니다.

## 설치

저장소 폴더에서 실행합니다. 가상환경이 이미 있으면 첫 줄은 생략합니다.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test,openai]"
```

## 앱 실행

**`H-TRSA.wsf`를 더블클릭**합니다. 실행되지 않으면 아래 명령을 사용합니다.

```powershell
.\.venv\Scripts\python.exe -m htrsa.workbench build/workbench/state.sqlite build/workbench/workspace
```

브라우저가 자동으로 열립니다. 자동 열기가 실패하면 터미널에 표시된 1회용 링크를 클릭합니다.

## API 설정

1. **설정 → AI 연결**에서 제공사와 키를 등록합니다.
2. **연구 → 새 연구 만들기**에서 질문을 적고 활성화된 모델을 선택합니다. 기본 예산이 자동 적용됩니다.
3. **연구 시작**을 누릅니다. 파일과 고급 설정은 선택 사항입니다. 모델·단가·전송 승인이 없으면 필요한 조치를 안내합니다.
4. 결론 검토 카드에서 범위와 현재 유효성을 확인하고 **보고서 → PDF 보고서 만들기**로 저장합니다.

실제 API 검사에는 키·전송 동의·확인된 단가·예산이 필요합니다. 자료가 부족하거나 방법이 지원 범위를 벗어나면 결론을 만들지 않습니다. [사용 안내](docs/guides/BEGINNER_GUIDE.md) · [v4 구현·검증 보고서](qa/qualified_profiles/IMPLEMENTATION_REPORT.md)

처음에는 **튜토리얼 보기**를 선택합니다. 상단 **도움말**에서 키 발급·연결·연구·오류 해결을 검색할 수 있습니다. 모델을 선택한 뒤 **설정 → AI 연결 → AI 연결 확인**에서 실제 응답을 검사합니다. 단가가 없으면 공식 가격을 확인하고 **단가 적용 후 AI 연결 확인**을 누릅니다. **단가만 적용**은 API를 호출하지 않습니다.

출처 형식 대조·질문 변경·현재 결론·동결 재현 보강은 [Cycle 12 구현 보고서](qa/cycle12/IMPLEMENTATION_REPORT.md)에 정리했습니다.

프로젝트의 `.env`는 자동으로 읽지 않습니다. 키는 앱에서 등록하거나 `OPENAI_API_KEY` 환경변수로 설정합니다. 앱 저장은 Windows 자격 증명 관리자, 사용할 수 없으면 접근 권한을 제한한 외부 평문 파일을 사용합니다. [API 검사 안내](docs/guides/LIVE_API_TEST.md)

## 요구 환경

Python 3.11 이상과 최신 브라우저가 필요합니다. 실제 모델 연구에는 제공사 API 키와 예산이 필요합니다. 고정 오프라인 데모에는 키가 필요하지 않습니다. 로컬 모델은 별도 서버가 필요하며 가중치를 자동 설치·로드하지 않습니다.

## 검사

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test,security]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe qa/prepublish_check.py
```

[검색·저사양·공개 준비 안내](docs/development/SECURITY.md) · [실제 검증 결과](qa/prepublish/IMPLEMENTATION_REPORT.md)

## 파일 위치

| 위치 | 용도 |
|---|---|
| `src/htrsa/` | 앱과 연구 실행 코드 |
| `tests/` | 회귀 테스트와 입력 자료 |
| `docs/` | 사용 안내·개발 문서·연구일지 |
| `qa/` | 검증 코드, `fixtures/` 고정 입력, `prepublish/` 공개 보고서 |
| `config/`, `db/`, `schemas/` | 설정·DB 마이그레이션·스키마 |
| `demo_scenarios/`, `docker/` | 고정 데모·격리 실행 설정 |
| `build/`, `output/` | 연구 데이터·검증 작업 공간·화면 캡처 |

기본 연구 데이터는 **`build/workbench/`**에 저장됩니다. 기존 연구가 있으면 이 폴더를 지우지 마세요.

## 자세한 안내

- [문서 목록](docs/README.md)
- [개발자 CLI·테스트·환경 검사](docs/development/CLI.md)
- [QA 실행과 결과](qa/README.md)
- [연구 흐름·저사양 실행](docs/guides/RESEARCH_FLOW.md)
- [연구일지](docs/history/연구일지.md)

Skills·F3-P·Ridge·Research Slice·Cycle 5는 기본 OFF입니다. 데모 통과와 실제 API·Docker·검색 검증은 각각 별도 기준이며 현재 상태는 앱의 **실행 환경** 또는 `python -m htrsa.preflight status`에서 확인합니다.
