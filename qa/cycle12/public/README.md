# 천문 공통 구조의 고정 자료

첨부 Cycle 12 참조 ZIP의 `sources/trappist1_default.csv`와 `.json`을 원본 바이트 그대로 보관한다. 운영 천문 프로필이나 최신 자료 수집을 의미하지 않는다. 원본 취득 기록은 참조 작성자가 남긴 `astro_acquisition_manifest.json`이며 이번 작업에서 원래 HTTP 취득을 관측하지 않았다.

| 파일 | SHA-256 |
|---|---|
| trappist1_default.csv | `147b89cda0970e00435d8b316542ca32afbe11d3f2bd638ad71275f9a5fa928b` |
| trappist1_default.json | `4dbb44370638af750a6bc41543f6a0244ab5b4de3fbcfa3f785f8f59f8a91277` |

자료는 NASA Exoplanet Archive `ps`의 `hostname='TRAPPIST-1' and default_flag=1`인 7행이다. `pl_name`을 키로 하고 `pl_orbper`를 공전 주기의 day 단위 점 추정값으로 해석한다. `pl_refname` HTML·`rowupdate`·오차 열은 그대로 보존하지만 HTML을 실행하지 않는다. 오차 공분산을 반영한 통계 추론은 구현하지 않는다.

`cross_domain.py`는 CSV·JSON을 공통 선택·키별 대조·물리량 함수에 넣는다. 원래 b/c/d 대 e/f/g/h와 작성자가 지정한 b/c 대 d/e/f/g/h 수정본의 평균을 실제로 계산하고 day→hour는 24를 곱한다. 두 형식은 같은 카탈로그이므로 독립 관측이 아니다. 추가 선택은 소유자의 실제 연구 승인이나 교사 검토 결과로 표시하지 않는다.

```powershell
.\.venv\Scripts\python.exe -B -X utf8 qa/cycle12/cross_domain.py
```
