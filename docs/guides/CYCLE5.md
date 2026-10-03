# Cycle 5 최소 의미 검증

기본 OFF다. 기존 StateService·SQLite·Research Slice·검증 의무·F3-P·보고서·export를 확장한다. 새 Agent·DB·그래프 프레임워크는 추가하지 않는다.

## 지원 범위

- `SourceSemanticRecord`: 물리량과 단위를 분리하고 저장 배율·기준·지역·기간·열 의미·원자료 정밀도·검토 상태를 기록한다. 단위만으로 물리량을 추론하지 않는다.
- `TransformationLineage`: 선언한 CSV 키·일대일 열 매핑·유한 affine 변환을 행별로 재검산한다. 중복·누락·추가·키 교환·잘못된 배율/offset을 차단한다. 지원 온도 변환은 `degC`, `degF`, `K`이며 편차·차이에 절대 온도 offset을 더하지 않는다.
- `GoalWitness`: 원질문·현재 질문·분석 의도·방법·추정 대상·물리량·기준·모집단·지역·기간·단위·제한 사항을 소유자 검토에 연결한다. Worker는 고정 범위를 바꾸거나 스스로 승인할 수 없다.
- 기존 의무에 `SOURCE_SEMANTIC_MATCH`, `TRANSFORMATION_LINEAGE`, `GOAL_SCOPE_MATCH`, `CLAIM_SUPPORT`를 추가한다. 수정본·내용 해시·계약·계획이 바뀌면 다시 검증해야 한다.
- 신뢰성 실험실은 외부 고정 정답으로 정답률·결론 일치·조건부 강건성·결과 상태 일관성·복구율·완료/검토 비율을 각각 기록한다. 종합 평균 점수는 없다.

분석에 사용하는 저장 배율은 1이어야 한다. 배율이 있는 원자료는 먼저 별도 child CSV와 변환 선언으로 정규화한다. 원자료를 덮어쓰지 않는다. 지원하지 않는 비선형 변환·인과 분석·자동 의미 추론은 검토 대상으로 남는다.

## 명시적으로 켜기

분석 시작 전의 새 연구에서 실행한다. `DB`, `WORKSPACE`, `RESEARCH_ID`는 실제 작업대 값으로 바꾼다. 예시 작업대는 `build/workbench/state.sqlite`, `build/workbench/workspace`다.

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py -m htrsa.cycle5 DB WORKSPACE RESEARCH_ID enable
& $py -m htrsa.cycle5 DB WORKSPACE RESEARCH_ID show
```

기존 Research Slice 정책이 아직 저장되지 않았으면 provenance·의존 카탈로그를 함께 켠다. 이미 저장된 OFF 정책이나 이미 시작한 분석의 정책은 바꾸지 않는다. 새 연구를 만든다.

`show`의 dataset ID·해시·열을 사용하여 JSON을 작성한다. 필드 정의는 `schemas/generated/source_semantic_record.schema.json`, `schemas/generated/goal_witness.schema.json`, `schemas/generated/transformation_lineage.schema.json`에 있다. source JSON의 `proposed_by`는 CLI의 소유자 요청이면 `owner`, 최초 `revision`은 1, `review_status`는 `NEEDS_REVIEW`다.

```powershell
& $py -m htrsa.cycle5 DB WORKSPACE RESEARCH_ID source --json source-column.json
& $py -m htrsa.cycle5 DB WORKSPACE RESEARCH_ID review --record-id SM-column --revision 1
& $py -m htrsa.cycle5 DB WORKSPACE RESEARCH_ID goal --json goal-witness.json
& $py -m htrsa.cycle5 DB WORKSPACE RESEARCH_ID lineage --json transformation.json
```

승인은 새 수정본으로 기록된다. 예를 들어 첫 source 제안 1을 승인하면 승인된 수정본은 2다. lineage의 `semantic_refs`에는 승인된 수정본 번호를 쓴다. `original_question`은 저장된 원질문과 정확히 같아야 한다. Worker `AnalysisPlan.semantic_scope`는 소유자 `GoalWitness.scope`와 같아야 한다. 의미 검토 대기는 기존 작업대의 PAUSED 경계에서 처리하고 검토 후 재개한다.

HTTP 조회는 `GET /api/research/RESEARCH_ID/cycle5`다. 변경은 기존 OwnerSession·Origin·CSRF를 요구하는 `POST /api/control/research/RESEARCH_ID/cycle5`에서 `enable`, `source`, `review`, `goal`, `lineage`, `repair`로 요청한다. 브라우저 역할 문자열로 소유자 권한을 부여하지 않는다.

## 제한된 변환 복구

F3-P v0.3이 별도로 켜져 있고 원래 계약이 명확할 때만 `PENDING` 상태의 `TRANSFORM_CSV` 산출물을 최대 2회 복구한다. 입력 dataset·질문·기준·추정 대상·물리량·범위는 바꾸지 않는다. 의미 변경은 새 AnalysisPlan/검토가 필요하다.

```powershell
& $py -m htrsa.cycle5 DB WORKSPACE RESEARCH_ID repair --record-id TL-transform --revision 1
```

복구는 새 파일에 기록하며 기존 입력·산출물 바이트를 보존한다. 복구 후 전체 계보를 재검산하고 산출물은 PENDING을 유지한다. 과학 결과의 PASS·정본 반영은 기존 검증 경계를 별도로 통과해야 한다. 재시작 시 완료된 복구를 중복 반영하지 않는다.

## 오프라인 재현

```powershell
& $py -m pytest tests/test_cycle5_semantics.py -q -p no:cacheprovider
& $py qa/reliability_lab.py --enable --cycle5 --output-dir build/cycle5-lab
& $py qa/cycle5_recovery_probe.py --all
& $py qa/cycle5_export_probe.py
```

고정 합성 CSV와 FakeProvider를 사용한다. 외부 gold는 `qa/fixtures/cycle5_gold_fixtures.json`이다. 내부 검증 PASS를 정답으로 사용하지 않는다. 첨부 문서의 120/120 실험은 이번 실행 결과가 아니다. 현재 결과는 [구현·검증 보고서](../../qa/results/cycle5_implementation_report.md)에서 확인한다.

자료·검증 상세와 보고서는 실제 기록된 의미·단위·기준·지역·기간·원질문 범위를 표시한다. 의미/계보가 맞더라도 자료 자체의 과학적 진실, 원질문의 인과 타당성, Live Agent 효능을 증명하지 않는다. `live_efficacy=NOT_VALIDATED`를 유지한다.
