# 보고서 일괄 재작성

현재 작업대에 등록된 종료 연구의 보고서를 새 수정본으로 작성한다. 휴지통, 실행 중인 연구, QA 작업대와 과거 보존 수정본은 제외한다. 기존 데이터·실행 기록·비용 원장·보고서는 보존한다.

실제 적용·검증·배치 결과와 미완료 조건은 [Probe 개선 적용 결과](PROBE_IMPROVEMENT_RESULTS.md)에서 확인한다.

저장소 루트에서 앱과 같은 가상환경 실행기를 사용한다.

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m probe.report_maintenance --database build/workbench/state.sqlite --workspace build/workbench/workspace --all --dry-run
.\.venv\Scripts\python.exe -B -X utf8 -m probe.report_maintenance --database build/workbench/state.sqlite --workspace build/workbench/workspace --all
.\.venv\Scripts\python.exe -B -X utf8 -m probe.report_maintenance --database build/workbench/state.sqlite --workspace build/workbench/workspace --resume BATCH_ID
```

`--dry-run`은 대상·제외 사유·예산을 확인하며 DB를 변경하거나 모델을 호출하지 않는다. `--resume BATCH_ID --dry-run`은 저장된 배치의 고정 목록을 확인한다. 실제 배치는 목록을 고정하고 한 연구씩 재검증 → 재작성 → PDF·선언 검증 → 현재 포인터 교체를 진행한다. 새 검색이나 추가 분석은 실행하지 않는다.

기존 연구별 예산, 월간 한도, 가격 승인, 전송 동의와 비용 원장을 사용한다. 가격 미확인·예산 부족·미확정 비용은 차단한다. 무효 자료만 남은 연구는 모델 호출 없이 정량 결론 없는 한계 보고서를 만든다.

| 항목 상태 | 의미 |
|---|---|
| `QUEUED` | 고정 목록에서 실행 대기 |
| `RUNNING` | 요청 키와 실행 소유자를 기록하고 진행 중 |
| `COMPLETED` | 새 수정본·PDF·파일 선언 검증 및 발행 완료 |
| `BLOCKED` | 입력 변경·예산·가격·정산·환경 확인 필요 |
| `FAILED` | 작성·검증·저장·발행 실패 |

모든 항목이 `COMPLETED`여야 배치도 `COMPLETED`가 된다. 차단·실패가 남으면 `PARTIAL`이며 CLI 종료 코드는 `2`다. 취소된 배치는 `INTERRUPTED`로 기록한다.

요청 키는 배치·연구·검증 버전·입력 지문으로 결정한다. 완료 항목은 재사용하고 입력이 바뀌면 `STALE_INPUT`으로 차단한다. 차단·실패 요청은 재개만으로 자동 재전송하지 않는다. 사유를 해결한 뒤 현재 입력으로 새 배치를 만든다. 미확정 비용은 기존 정산 흐름에서 먼저 해결한다.

앱 시작 시 중단된 재작성의 소유자를 확인한다. 검증 가능한 완성 수정본이 있으면 발행·완료 상태를 복구한다. 미완료 작업은 중단으로 정리하며 비용이 불확실한 원장은 정산 대기로 유지한다.

새 결과는 `WORKSPACE/RESEARCH_ID/report_revisions/REVISION/`에서 완성한다. `research_output/current.json`의 작은 포인터를 원자적으로 교체하며 조회·PDF·내보내기는 선택한 수정본 하나를 읽는다. 이전 수정본은 보존하지만 최신 검증에 실패한 보고서를 현재 검증 결과로 표시하지 않는다.

보고서 기록과 입력 지문은 `validation_version=2`를 포함한다. 숫자 선언은 JSON Pointer `location`에 표시 위치를 지정한다. 문헌 수치는 검증된 원문 인용으로 표시하고, 관측 수치는 검증된 지표·값·단위로 생성한 문장을 사용한다. 이전 형식은 이력으로 읽되 현재 검증 통과로 취급하지 않는다.

출시 내보내기는 임시 디렉터리에서 검증한 후 발행한다. 대상 폴더에 파일이 있으면 `EXPORT_OUTPUT_NOT_EMPTY`로 차단한다. 기존 파일을 삭제하거나 혼합하지 않는다.

오프라인 검증은 `python -m probe.preflight validate-core`로 실행한다. Chrome 검사는 `node qa/research_report_browser.cjs`, 같은 명령의 `--weak`, `node qa/science_browser.cjs`, `node qa/cycle12/pdf_currentness_browser.cjs`를 사용한다. 기존 보고서 API 호환 검사에서는 `LEGACY` 실행 모드를 명시하며 현재 과학 흐름은 별도로 검사한다.

오프라인·모의 제공사 검증은 실제 API 품질이나 실제 Docker 격리 검증을 대신하지 않는다. 실행하지 않은 환경은 `NOT_VALIDATED`로 기록한다. 앱·PDF·계산 자식의 실행기·의존성·기동 확인 실패 시 실행을 차단한다.

적용 전 DB와 작업 공간을 보존한다. 실패 시 새 발행을 중단하고 이전 산출물을 보존한다. 비용 원장은 코드 롤백이나 재실행으로 초기화하거나 과거 백업으로 되돌리지 않는다.

입력 제한의 근거: [HTTPX 원시 스트림](https://www.python-httpx.org/async/), [Python 제한 압축 해제](https://docs.python.org/3/library/zlib.html), [Docker tmpfs](https://docs.docker.com/engine/storage/tmpfs/).
