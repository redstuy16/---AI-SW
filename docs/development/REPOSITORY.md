# 저장소 구성

```text
Probe/
├─ README.md                설치·실행·API 설정
├─ AGENTS.md                저장소 작업 지침
├─ Probe.wsf               Windows 실행 파일
├─ pyproject.toml           Python 의존성과 테스트 설정
├─ docs/
│  ├─ README.md             문서 목록
│  ├─ guides/               기능별 사용 안내
│  ├─ development/          구조·CLI·기능 동결
│  └─ history/              연구일지·경로 변경 기록
├─ src/probe/               앱·연구 실행 코드
├─ tests/                   회귀 검사와 CSV 입력
├─ qa/
│  ├─ *.py, *.cjs           검증 실행 코드
│  ├─ fixtures/             고정 평가 입력
│  ├─ results/              JSON 결과·구현 보고서
│  └─ live_api_test/        세션별 API 증거 패키지
├─ config/                  가격 등 설정
├─ db/                      스키마·마이그레이션
├─ schemas/                 공개 스키마
├─ demo_scenarios/          고정 데모 선언·자료
├─ docker/                  격리 실행 이미지
├─ build/                   연구 DB·작업 공간·검증 임시 자료
└─ output/                  화면 캡처 등 산출물
```

## 정리 기준

사용법은 `docs/guides/`, 개발 규칙은 `docs/development/`, 날짜별 기록은 `docs/history/`에 둡니다. QA 실행 코드는 `qa/`, 고정 입력은 `qa/fixtures/`, 결과·보고서는 `qa/results/`에 둡니다.

앱 데이터는 기본 `build/workbench/`에 있으므로 `build/` 전체를 임시 파일로 취급하면 안 됩니다. API 키의 기본 저장 위치는 저장소 밖이며 프로젝트 `.env`는 자동으로 읽지 않습니다.

2026-10-02 정리에서는 안내·기록·QA 파일 92개를 이동했습니다. 기존 JSON 원문과 고정 입력의 바이트는 보존했고 Markdown 링크와 QA의 읽기·쓰기 경로를 수정했습니다. 실행 코드·DB 마이그레이션·스키마·데모·기존 연구 데이터 경로와 검증 기준은 유지했습니다.

[경로 변경 기록](../history/repository_reorganization_2026-10-02.json) · [문서 목록](../README.md) · [QA 결과](../../qa/results/README.md)
