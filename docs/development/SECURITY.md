# 보안 검사와 공개 준비

## 로컬 검사

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test,security]"
.\.venv\Scripts\python.exe qa/prepublish_check.py
.\.venv\Scripts\python.exe -m pip_audit --skip-editable --cache-dir build/audit-cache
.\.venv\Scripts\python.exe -m bandit -r src -f json -o build/bandit.json
```

공개 준비를 강제 검사할 때 `--require-ready`를 추가합니다. 크기 기준은 `--max-bytes`로 정합니다. 기본 10MB입니다. 검사기는 삭제·Git 초기화·커밋·이력 수정·전송을 하지 않습니다. 의심 비밀의 값은 보고하지 않습니다. 합성 canary는 별도로 표시합니다.

`.gitignore`는 이미 추적된 자료를 제거하지 않습니다. 검사기는 추적 파일과 전체 Git 객체의 텍스트 이력을 별도로 검사합니다. 읽을 수 없거나 큰 이력 객체는 `NOT_VALIDATED`입니다. Git 저장소가 없으면 파일 후보만 검사하고 공개 준비는 `BLOCKED`입니다. 무시된 로컬 파일은 암호화되지 않습니다. 사용자 연구·검사 세션·화면 캡처·청구 내역은 공개 대상에서 제외합니다.

실제 비밀이 발견되면 제공사에서 폐기·교체하고 추적 파일과 이력에서 제거할 계획을 세운 뒤 다시 검사합니다. 이력 수정은 소유자 승인 후 진행합니다. 패턴 검사는 모든 비밀과 개인정보를 탐지하지 못하므로 공개 자료를 직접 검토해야 합니다.

## GitHub에 만든 뒤 확인할 항목

- Secret scanning과 Push protection
- Dependency graph, Dependabot alerts와 security updates
- CodeQL, dependency review, 필요한 브랜치 보호 규칙
- Actions의 기본 권한 `contents: read`, 신뢰할 수 없는 PR의 비밀 접근 차단

현재 로컬 설정 파일만 준비했습니다. GitHub에서 활성화하거나 실행했다는 의미가 아닙니다. 프로젝트 LICENSE는 소유자가 선택할 때까지 미정입니다. 로컬 모델 가중치·엔진·데이터의 라이선스는 각각 확인합니다.

[GitHub 보안 설정](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-security-and-analysis-settings-for-your-repository) · [Actions 보안 지침](https://docs.github.com/en/actions/reference/security/secure-use) · [의존성 검토](https://github.com/actions/dependency-review-action) · [CodeQL](https://github.com/github/codeql-action)

워크플로는 공식 Actions의 확인한 commit SHA를 고정합니다. PR에는 제공사 키나 유료 API 작업이 없습니다. CodeQL은 결과 업로드 작업에만 `security-events: write`를 사용합니다. 로컬 Bandit 발견과 GitHub CodeQL 실행은 별도 결과입니다.

## 검색과 자료 경계

웹 검색은 기존 Crossref 공개 문헌 메타데이터만 사용합니다. 일반 뉴스 검색이나 원문 임의 다운로드는 제공하지 않습니다. 공개 검색어만 직접 승인하고 `현재 연구 자료` 전송을 선택해야 합니다. CSV 내용·로컬 경로·API 키를 검색어로 만들지 않습니다. AUTO는 문헌·최신 출처가 필요한 질문에서만 검색합니다. ALLOWED도 항상 검색하는 설정은 아닙니다. DISABLED는 모델 API 자체를 끄는 설정이 아닙니다.

Crossref 공개 API는 [무료 메타데이터 정책](https://www.crossref.org/services/metadata-retrieval/)에 따라 API 단가 0으로 기록합니다. 다른 제공사의 미확인 단가는 `PRICE_UNKNOWN`이며 전송하지 않습니다. 모델이 근거를 읽는 토큰 비용은 기존 모델 비용으로 별도 계산합니다. 검색 결과는 불신 자료로 다루고 기존 출처·근거 검증을 통과해야 정본에 반영합니다.

## 자원 정책

설정·목록 조회에서 과학 도구를 미리 로드하지 않습니다. 작업대의 진행 기록은 최대 100개, 저사양 모드는 25개씩 조회합니다. 전체 감사·검증 기록은 보존합니다. 자동 모드는 CPU 논리 코어가 4개 이하일 때 적용하며 하드웨어 정보를 외부에 보내지 않습니다. 저사양 모드는 연구를 하나씩 시작하고 과학 검증·출처·비용 기준은 유지합니다. 완료·일시정지 상태는 자동 갱신하지 않고 실행 중일 때만 10초 간격으로 조회합니다.
