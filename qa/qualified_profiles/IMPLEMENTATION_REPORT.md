# 초보자 중심 제품 v4 구현·검증 보고서

> 2026-10-03. 첨부 v4 문서를 실행 명세로 적용했다. 기존 과학·보안·출처·복구·예산·출시 기준과 앞선 작업을 보존했다. 이 보고서의 로컬 성공은 실제 Agent 성능이나 경쟁 제품 우위를 뜻하지 않는다.

## 1. 구현한 제품 차별점

원래 질문·자료 의미·계산·결론 범위·현재 유효성을 연결한 결론 검토 카드와 자료 변경 후 영향 확인·재검증을 구현했다. 역할 수나 다중 Agent 자체를 제품 우위로 주장하지 않는다. 공개 기후 자료의 두 기간 비교부터 한정 절차를 제공한다.

## 2. 폐기한 가정

모든 질문에 결과를 만들 수 있다는 가정, 연구 전 역할별 모델을 수동 배치해야 한다는 가정, 복수 Agent의 결론 일치가 정답이라는 가정을 제품 흐름에서 제거했다. 미지원·근거 부족·의미 충돌·예산 부족을 구분한다. 계산 재현은 과학적 참의 증명이 아니다.

## 3. 제거한 초보자 필수 입력

정상 화면의 필수 사용자 입력은 연구 질문이다. 제목·CSV·문헌 검색어·역할 배치·보고서 형식·상세 추론 설정을 요구하지 않는다. 파일은 선택 사항이다. 실제 과금 전에는 사용 가능한 연결·모델·확인된 단가·전송 승인·유한한 예산이 필요하며 이 기준을 자동 우회하지 않는다.

## 4. 기본값

기존 단일 resolver와 버전 2 우선순위를 재사용했다. 고급 명시 값 > 주 설정 명시 값 > 소유자 설정 > 앱 기본값이며 false와 0을 보존한다. 실제 resolver로 38개 항목을 분류했다: [설정 목록](defaults.json).

초보자 화면은 BALANCED, 연구당 0.10 USD 기본 상한, 검색 5회 공유 상한, AUTO 검색·한정 프로필 선택을 적용한다. 실제 사용 가능한 기존 모델만 자동 선택하며 모델·가격을 만들어 넣지 않는다. 기존 API/CLI는 beginner_mode=false·research_profile_mode=DISABLED를 유지한다. Skills·F3-P·실험적 Ridge는 기본 OFF다. 새 한정 절차의 필수 주장/변경 검사는 새 연구에서 적용하며 이미 고정된 정책과 충돌하면 정책을 바꾸지 않고 차단한다.

## 5. 고급 설정

예산·검색·검색어·시간 한도·세부 추론·후속 검토·실험 기능을 접힌 고급 설정으로 옮겼다. 설정의 정상 흐름은 AI 연결·예산·저사양·도움말로 정리했다. 기존 기술 화면은 문제 해결 정보에서 접근한다.

## 6. 새 연구 설명 선택

설명 표시 여부를 처음 물으며 선택·닫기·다시 묻지 않기·설정에서 변경을 지원한다. 설명을 켜지 않아도 연구를 시작할 수 있다. 필수 안전 조치와 오류는 설명 선호와 별도로 표시한다. 설정 재설정은 연구·키를 삭제하지 않는다.

## 7. 자동 역할 배치

승인된 모델 하나를 기존 필수 역할이 상속한다. 일반 사용자는 Agent 역할을 배치하지 않는다. 기후 한정 절차는 실제 유료 결정 역할을 Manager 1개로 한정하며 로컬 도구 계약·Coordinator/Worker 권한은 기존 구조를 따른다. 일반 엔진의 기존 역할과 사전 비용 계산을 유지했다. 역할 구역이 동시에 실행하는 Agent 수를 뜻하지 않는다.

## 8. 모델 선택

연구 설정에서 모델을 선택한다. 접힌 상태에서는 선택 모델 한 행만 고정하고, 펼치면 제공사별 그룹의 원래 위치로 돌아가 중복을 없앤다. 더보기로 목록을 확장한다. API 연결 화면의 모델·라우팅은 내부 설정이며 정상 화면에서 중복 선택을 요구하지 않는다. 제공사 카탈로그·기존 승인 모델을 재사용했다.

## 9. 성능 슬라이더

큰 조작 영역과 두꺼운 트랙을 적용하고 아래에는 현재 단계만 표시한다. 키보드 조작과 200% 확대를 확인했다. 내부 성능·추론 매핑은 기존 값과 호환한다.

## 10. 검색 한도

검색 상한은 고급 설정에 둔다. 기존 공유 카운터·bounded retry·429 분류·명시 0 처리를 유지했다. 지원되는 고정 기후 자료 절차에서는 문헌 검색을 NOT_APPLICABLE로 분류한다. 이것은 일반 연구의 문헌 필수 정책 해제가 아니다.

## 11. 파일 처리

선택적 다중 파일 찾아보기·제한 저장·취소·삭제·사용 중 출처 보존을 기존 파일 서비스에 연결했다. TXT/MD/JSON/CSV의 기존 읽기 기능을 재사용했다. 임의 CSV의 열·단위·지역·기간을 추측해 기후 프로필에 넣지 않는다. PDF/DOCX/ZIP 등의 저장만 가능한 첨부는 분석 완료로 표시하지 않는다.

## 12. 도움말·사용 안내·튜토리얼

41개 주제의 실제 UI 사용 안내와 [사용 안내 문서](../../docs/guides/BEGINNER_GUIDE.md)를 연결했다. 튜토리얼은 실제 연구 입력·설정 화면으로 이동해 해당 조작을 강조하며 이전·다음·건너뛰기·다시 보기를 제공한다. 유료 연습 호출은 하지 않는다. 초보자 도움말에 상세 비용 교육을 반복하지 않는다.

## 13. API 시작 차단 원인과 수정

확인된 원인은 연구 전용 설정의 부족과 연결 검사를 같은 맥락으로 다루고, 모델·역할·자료의 기술적 준비를 초보자가 수동 처리해야 했던 경로다. 독립 연결 검사와 단일 preflight 분류(AUTO_FIXABLE/WARNING/USER_ACTION/HARD_BLOCK/NOT_APPLICABLE)를 적용했다. 자동 수정 가능한 기본값만 채우며 실제 단가·동의·키 부재는 필요한 조치로 남긴다.

명세가 언급한 PRICE_TRANSFER_CONSENT는 현재 코드에서 존재하거나 재현되는 오류가 아니었다. 해당 이름의 버그를 재현·수정했다고 주장하지 않는다. PRICE_REQUIRED 등의 실제 가격 차단은 유지한다. 실제 서버·Chrome에서 연구 설정 없이 독립 연결 검사와 질문만 입력한 시작을 모의 HTTP로 확인했다. 제공사 실계정 연결·유료 호출은 미실행이다.

## 14. 예산 UX

정상 설정에서 연구당·월 예산을 정리하고 월 한도 변경 제안은 승인·취소로 처리한다. 예산 부족으로 멈춘 연구는 추가 과금 없이 현재 결과로 끝낼 수 있다. 저장된 실행 상한을 월 한도 변경만으로 몰래 높이지 않는다. 실행 중에는 일시 정지가 필요하고, 미확정 비용과 예약은 삭제·정리 전에 확인한다.

## 15. 휴지통과 보관

연구 목록의 제목·필터·최종 수정·이름 변경을 통합했다. 삭제는 30일 휴지통 보관이며 복원·확인 후 영구 삭제·만료 정리를 제공한다. 날짜는 서버 UTC로 기록한다. 활성 작업·실행 예약·미확정 비용은 삭제를 차단한다.

연구 ID에 귀속된 경로만 정리하고 symlink/junction/reparse·상위 경로·중첩 링크를 검사한다. 공유 입력 파일과 비용/감사 이력은 보존한다. 격리 이동·FK transaction·PURGING 재시도로 DB 또는 물리 삭제 실패의 복구를 지원한다. AI 연결도 실제 삭제·revision 검사·공유 키 보존·사용 중 차단을 적용했다.

## 16. 사용자 보고서는 PDF

보고서 만들기·미리보기·다운로드를 PDF로 고정했다. 내부 Markdown/JSON/manifest는 출처·검증·export 용도로 유지했다. 결론 카드 6문항과 기존 9개 보고서 절을 PDF에 포함한다. 최신성 검사를 생성 전후로 실행하며 stale 결론을 현재 보고서로 내보내지 않는다.

실제 생성한 한정 절차 4페이지와 기존 4종 14페이지를 렌더링해 페이지 모음에서 한글 누락·잘림·겹침을 검토했다. PDF 구조·임베드 글꼴·해시·주장 연결은 자동 검사와 별도다: [시각 검토](pdf_visual_results.json). 기존 ReportLab·pypdf·pypdfium2와 Windows 로컬 한글 글꼴을 재사용했으며 글꼴을 다운로드하거나 배포하지 않았다.

## 17. 비용 부담 표시

필요한 연구 설정 옆에 낮음·보통·높음의 상대 범주만 표시하고 녹색·노란색·주황색으로 구분했다. 실제 요금이나 성능 향상률의 추정치가 아니다. 완료·긍정 알림의 녹색 정책을 유지했다.

## 18. QualifiedResearchProfile 경계

일반 registry·Protocol·저장된 plan/binding·profile 검사·typed Claim을 추가했다. 기존 AutonomousLoop의 역할 계약·모델 호출·도구 registry·dispatch와 StateService의 Stage→Verify→Commit을 재사용한다. 원래 질문·실행 절차·출처 해시·숫자 provenance를 고정하며 새 DOMAIN 필드를 공통 ResearchState에 넣지 않았다.

일반 목표의 GOAL_ANSWERED 판정과 최소 근거 기준을 완화하지 않았다. 한정 절차 완료는 QUALIFIED_PROCEDURE_COMPLETED라는 별도 이유와 현재 카드 검사로 처리한다. 기존 Research Slice 정책은 고정된 값을 보존하며 필수 검사와 충돌하면 PROFILE_POLICY_INCOMPATIBLE로 차단한다. 기존 F3-P/Skills 경로와 검사는 유지하지만 두 기간 비교를 지원하지 않는 기존 repair 템플릿에 억지로 적용하지 않는다.

## 19. 첫 기후 프로필

[NASA GISTEMP v4 공식 연간 자료](https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.txt)의 전 지구 L-OTI·달력 연간 J-D·1951–1980 기준 편차를 지원한다. TXT 저장값은 0.01°C 배율이다. 실제 조회한 공식 CSV의 소수 °C 값과 유한한 연간 관측값 전체가 일치했다: [원본 기록](public/source.json).

한국어 두 기간 질문을 원래 기간·단위·범위에 고정한다. °F 편차 변환은 1.8배이며 32를 더하지 않는다. 질문에 명시된 1961–1990 기준 변환은 같은 offset으로 두 기간에 적용한다. 사용 연도의 누락·중복·비유한 값·열 오정렬·배율 충돌은 차단한다. Python 도구 계산과 별도의 Decimal 합계로 두 평균·차이를 검사한다. [NASA 1차 설명](https://data.giss.nasa.gov/gistemp/faq/)에 따른 편차 의미를 유지한다.

공식 고정 GET 요청은 질문·키·사적 자료를 전송하지 않는다. 승인된 egress, timeout, 크기 상한, redirect 비허용을 적용했다. 공개 자료 조회 성공은 Live Scholarly Search 게이트의 통과가 아니다.

## 20. 공통 코어의 일반성

공통 ResearchState에 기후 전용 필드가 없고 registry에 다른 profile을 등록할 수 있음을 검사했다. 기존 일반 상관·회귀·문헌·가설·비판·복구·export 데모가 그대로 통과했다. 현재 실행 어댑터는 공식 기후 자료 한 종류만 완성됐으며 향후 등록 가능성을 다른 분야의 실행 지원으로 광고하지 않는다.

## 21. 결론 검토 카드

무엇을 알아본 결과인지, 어떤 자료인지, 무엇을 계산했는지, 어디까지 말할 수 있는지, 무엇을 확인하지 못했는지, 현재 유효한지의 6문항을 질문·source·typed Claim·tool artifact·검증 결과에 연결한다. 일반 엔진도 저장된 근거로 카드를 표시하며 미확정 상태를 현재 확정 결론처럼 꾸미지 않는다. 정상 진행 6단계는 실제 저장된 도구·검증·완료 상태를 투영한다.

## 22. 변경 영향·재검증

새 원본의 사용 연도·기준 변환 관측값·의미를 비교한다. 사용한 값 변경은 연결된 주장과 report를 stale로 만들고, 사용하지 않은 값만 바뀌면 현재 결론을 유지한다. 의미 변경은 임의 수정 대신 확인을 요구한다. A→B→A 원본 이력도 시간 순으로 보존한다.

재검증은 저장한 질문·plan·결정을 재사용해 영향을 받은 계산과 검증을 수행한다. 로컬 재계산에는 HTTP나 LLM 호출이 없다. 이전 결론과 manifest가 포함된 보고서 이력을 보존하고 현재·이전을 구분한다. 외부 재현 CLI는 export 원본과 manifest 해시·안전한 상대 경로·계산·claim 일치를 확인한다. 독립 진위 인증은 아니다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -m htrsa.qualified_replay PATH_TO_REPLAY_MANIFEST.json
```

## 23. 벤치마크 프로토콜과 실행

구현 전 2026-10-03T04:00:07Z에 기록한 채택 기준은 정상 완료율 감소 5%p 이내, 정상 과잉 보류 10% 이내, 잘못된 승인 또는 실제 검토 부담 개선, 사용 값 변경 무효화·미사용 값 변경 유지다. Live 비교 이전에는 채택 기준 달성을 선언하지 않는다.

[프로토콜](PROTOCOL.md)에 강한 단일 Agent의 계획·전체 자료·동일 도구·자체 검사·수정 기회, 동일 실제 모델·한도·재시도·시간·비용·출력 요구를 고정했다. 정상 4·결함 6·변경 3·모호/미지원 3건의 opaque 입력을 양쪽에 동일하게 준비했고 평가자 정답은 Agent 경로 밖에 둔다. 16건은 같은 제품의 변형으로 독립 연구 16개가 아니다.

실제 로컬 도구와 공개 자료에 scripted Fake Agent 결정을 사용한 16건이 모두 기대한 상태를 보였다. 정상 두 평균·차이, 잘못된 offset/배율/범위/기간/결측/인과 확대 차단, 사용·미사용 변경, 모호성 처리를 확인했다: [실행 기록](results.json). 이 기록은 Live 성능 비교가 아니다. 비교 준비 입력의 동일성·정답 분리·누락 case 거부·비 Live 캡처 거부를 확인했다: [준비 검증](benchmark_integrity.json).

실제 frontier 모델 양쪽 실행, 의미/목표·주장 변경·복구·한정 절차별 OFF/ON Live ablation, 외부 제품 정상 흐름, 실제 사람의 검토 시간 비교는 NOT_VALIDATED다. 실행 adapter/증빙·계정·유료 실행 권한이 없어 결과를 만들지 않았다. 비용은 유효 완료·잘못된 승인·과잉 보류·의미 유지·검토 부담 다음 지표다.

## 24. 실제 대표 사례 5개

| 사례 | 실제 조작·입력 | 관측 결과 | 상태 |
|---|---|---|---|
| comparison | 두 기간의 전 지구 평균 편차 | 두 평균 0.3235°C / 0.7285°C, 차이 0.405°C | CURRENT |
| storage | 공식 TXT 저장 배율의 충돌 주입 | 계산·commit 전에 차단; TXT/CSV 실제 저장 단위 일치 확인과 구분 | BLOCKED |
| ambiguous | 출처 의미와 주어진 메타데이터의 충돌 | 질문·의미 확인 필요; 기존 계산값을 결론으로 승인하지 않음 | CLARIFICATION_REQUIRED |
| used | 사용한 2005년 값 변경 주입 | 이전 결론 stale → 재검증 후 차이 0.410°C; 재계산 유료 호출 0 | REVALIDATED |
| fahrenheit | 원래 질문에 화씨 편차 명시 | 차이 0.729°F; 32를 더하지 않음 | CURRENT |

원본은 실제 공개 자료이며 계산·검증·DB commit·export·replay는 실제 로컬 실행이다. 자료 변경·충돌은 명시적인 fault injection이고 Agent 판단은 모의 응답이다. 실제 NASA가 해당 변경 사건을 일으켰거나 Live Agent가 판단했다고 주장하지 않는다. TXT/CSV 저장 단위 비교는 실제 공개 제품의 형식 차이를 사용한다.

## 25. 사용성 관측

실제 Chrome·소유자 인증·HTTP 서버에서 새 v4 35개, 기존 시작 53개, hardening 41개, 로컬 인증 51개 검사를 통과했다. 새 v4는 질문만 시작·설명 선택·모델 중복 제거·카드·변경/재검증·PDF·휴지통·독립 연결·튜토리얼·200% 확대를 포함한다. JS 오류와 브라우저 외부 전송이 없었다. HTTP 제공사 응답은 명시적 MOCK이다.

실제 학생·교사 연구는 수행하지 않아 NOT_VALIDATED다. 모의 UI 성공을 학습 효과·초보자 완료율·사람의 수정 시간 개선으로 환산하지 않는다. 화면 구성은 [Linear 공식 UI 사례](https://linear.app/now/how-we-redesigned-the-linear-ui)의 정보 계층과 단순한 내비게이션을 참고했다.

## 26. 회귀·복구·출시 결과와 변경 파일

| 검사 | 관측 결과 |
|---|---|
| 수정 전 전체 기본 pytest | 891 PASS·5 SKIP·2 deselected, stdout 561.01초 / XML 560.924초 |
| 중간 전체 기본 pytest | 934 PASS·5 SKIP·2 deselected, stdout 1072.07초 / XML 1071.785초. 마지막 정책 수정·5개 검사 추가 전이며 최종 소스 통과로 사용하지 않음 |
| 최종 소스 신규 검사 | 48 PASS, stdout 80.04초 / XML 80.010초 |
| 최종 전체 오프라인 Core | **939 PASS**, skipped=false, exit 0, wrapper 870.273초 |
| 최종 destructive | **116 PASS**, skip/deselected 0, 216.319초; 실행 가능한 19시나리오 PASS, Docker 시나리오 NOT_VALIDATED |
| Demo A/B | 각각 5/5, 실패·중복 논리 작업·미해결 참조 0 |
| 기존 새 프로세스 resume | PASS; 통계 실행·실험·근거·state commit 중복 없음 |
| F3-P 결함/복구 | 기대 상태 8/8, 실제 새 프로세스 경계 6/6; 실제 API 호출 0 |
| 새 한정 절차 복구 | 실제 프로세스 종료·재개 2/2, 각 commit 1, 재개 모델 호출 0 |
| 기본 release | 17파일, hash mismatch·missing·secret·미해결 참조 0 |
| F3-P canary/clean export | 28+24파일씩 2환경 = 104파일; hash·secret·절대 경로·DB/dashboard canary 이상 0 |
| 최종 공개 자료 로컬 평가 | 16/16 기대 상태 일치; 모의 Agent와 명시적 결함 주입 |
| 실제 Chrome | v4 35·기존 시작 53·hardening 41·로컬 인증 51개 검사 통과 |
| 실제 PDF | 한정 절차 4페이지·기존 14페이지 렌더/시각 검토 및 자동 구조 검사 |

Core 시작·종료·최종 offline 검증의 동일 소스 SHA-256: `2086d74304e2a0c0eb771cee2125ab16af5bc6f3fbec64bdb488da0c313cadd2`. 기존 최종 목표, tamper·stale-state·secret-canary·수치 provenance·Stage→Verify→Commit 검사를 완화하지 않았다. 최종 Core와 offline 결과가 같은 소스에 속하므로 중간 전체 검사 수를 최종 수치로 재사용하지 않았다. 신규 확인된 회귀 실패는 없다.


### 실제 실행 명령

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -p no:cacheprovider --basetemp build/beginner-v4-baseline-tests --junitxml build/beginner-v4-baseline.xml
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -p no:cacheprovider --basetemp build/beginner-v4-final-tests --junitxml build/beginner-v4-final.xml
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_beginner_v4.py -q -x -p no:cacheprovider --basetemp build/beginner-v4-policy-final --junitxml build/beginner-v4-verified.xml
.\.venv\Scripts\python.exe -X utf8 -m htrsa.preflight validate-core
.\.venv\Scripts\python.exe -X utf8 qa/ui_unblock_offline_validation.py
.\.venv\Scripts\python.exe -X utf8 qa/qualified_profiles/evaluate.py
.\.venv\Scripts\python.exe -X utf8 qa/qualified_profiles/recovery_probe.py
node qa/beginner_v4_browser.cjs
node qa/ui_unblock_browser.cjs
node qa/hardening_visual_qa.cjs
node qa/local_browser_auth_qa.cjs
.\.venv\Scripts\python.exe -X utf8 qa/ui_unblock_pdf_checks.py
.\.venv\Scripts\python.exe -X utf8 qa/prepublish_check.py --output qa/qualified_profiles/security_summary.json
.\.venv\Scripts\python.exe -X utf8 -m htrsa.preflight validate-docker
.\.venv\Scripts\python.exe -X utf8 -m htrsa.preflight smoke-api
.\.venv\Scripts\python.exe -X utf8 -m htrsa.preflight validate-search
.\.venv\Scripts\python.exe -X utf8 -m htrsa.preflight status
```

Core 명령은 기존 환경 marker 제외 기준을 그대로 사용한다. 전체 기본 pytest의 5 SKIP은 Docker 4개·현재 Windows credential 세션 부재 1개이며 기존 2 deselected는 opt-in Live API/Search다. 새 기능 때문에 기존 테스트를 삭제·약화·추가 제외하지 않았다. 기존 browser fixture는 새 설명 팝업의 저장 상태와 v4 제공사 그룹·도움말·정상/기술 화면을 실제로 사용하도록 수정하고 원래 안전 검사 항목을 유지했다.

주요 새 파일: beginner_controls.py, beginner_policy.py, research_lifecycle.py, period_comparison.py, qualified_profiles.py, climate_profile.py, qualified_workflow.py, qualified_replay.py, workbench_static/beginner_ux.js, tests/test_beginner_v4.py와 qa/qualified_profiles/*. 변경된 기존 연동은 control_plane/control_runtime, product_policy, workbench/workbench_pages/static, real_schemas/real_tools, scientific_verifier/service/research_slice, final_report/report_ux/report_pdf/release다. [이번 작업 변경 목록](changed_files.json)에 이전 작업과 구분했다.

v4 자체 DB migration·새 dependency는 없다. 운영 config·기존 연구 표·기존 migration 6개를 재사용한다. 앞선 작업에서 추가한 PDF dependency를 v4의 새 의존성으로 다시 세지 않았다. README·사용 안내·연구일지·Feature Freeze와 QA 기록을 한국어로 갱신했다.

### 앱 실행과 사용

```powershell
.\.venv\Scripts\python.exe -m htrsa.workbench build/workbench/state.sqlite build/workbench/workspace
```

AI 연결을 등록하고 연구 → 새 연구 만들기에서 질문을 입력한다. 예: “1981~2000년과 2001~2020년의 전 지구 연간 기온 편차 평균을 비교해 주세요.” 정상 UI는 beginner_mode=true, research_profile_mode=AUTO를 명시적으로 사용한다. 기존 API를 쓰면 두 값을 명시해야 새 흐름을 사용하며 설정하지 않은 기존 API/CLI는 그대로다.

## 27. 아직 지원하지 않는 분야

현재 한정 실행 프로필은 공식 NASA TXT의 전 지구 연간 편차 두 기간 평균 비교다. 지역 기온·인과 식별·미래 예측·절대 온도 복원·확증적 유의성·임의 자료의 자동 의미 추론·다른 도메인 한정 실행은 미지원이다. 공통 일반 연구 엔진은 계속 제공하며 그 결과도 기존 실제 근거와 검증 기준을 만족해야 한다.

## 28. 미검증 제품 주장과 남은 조건

| 항목 | 실제 상태 |
|---|---|
| CORE / STRESS / ARTIFACT / Demo A/B | 모두 PASS, 현재 소스 해시 일치 |
| Docker | NOT_VALIDATED; CLI·daemon·격리 image 없음. validate-docker 결과 SKIPPED/DOCKER_UNAVAILABLE |
| Live LLM | NOT_VALIDATED; 현재 프로세스 key/model 미설정. smoke-api 결과 SKIPPED/API_UNCONFIGURED |
| Live Search | UNCONFIGURED, validated=false; validate-search 결과 SKIPPED/SEARCH_UNCONFIGURED |
| Windows credential 저장 | 기존 NOT_VALIDATED/WINDOWS_ERROR_1312 유지 |
| OS 기본 브라우저/WSF | 기존 NOT_VALIDATED 유지; 실제 Chrome 자동화 성공과 별개 |
| Skills / F3-P / 새 프로필 Live 효능 | NOT_VALIDATED |
| 실험적 Ridge | 기존 오프라인 회귀 보존, 새 프로필에는 미적용; Live 효능 미검증 |
| 강한 단일 Agent·native 제품·ablation·학생/교사 | NOT_VALIDATED |
| 공개 후보 검사 | 의심 비밀 0개, Git 이력 316개 VALIDATED, 공개 후보 325파일; 최종 Core 연결 PASS |
| 공개 | BLOCKED; LICENSE_PENDING_OWNER_CHOICE |
| demo_ready / release_ready / product_release_ready | **true / false / false** |

공개 소스 자료 조회와 모의 HTTP/UI 검사 성공을 Live Search/Live LLM 환경 검증으로 기록하지 않았다. 현재 작업 환경은 .git 쓰기를 허용하지 않아 커밋·GitHub 반영을 수행하지 못했다. 기존 공개 검사도 LICENSE_PENDING_OWNER_CHOICE로 차단된다. 라이선스 선택·실계정 유료 비교는 수행하지 않았다. 환경 확인 중 존재하지 않는 validate-api 명령은 CLI가 거부했고 실제 명령 smoke-api로 다시 확인했다. 코드·검증 실패와 구분한다.


강한 단일 Agent보다 더 정확하거나 빠르다는 주장, 초보자·교사에게 더 쉽다는 주장, 다중 Agent가 성능을 높인다는 주장, 새 의미/변경/복구 기능의 Live ablation 이득은 모두 NOT_VALIDATED다. 재현성·manifest 일치·로컬 fault test 성공을 실제 효능이나 과학적 참으로 확대하지 않는다. 실행할 수 없는 항목을 통과로 기록하지 않았다.
