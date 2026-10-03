// 검토된 자료 의미와 현재 검사를 기존 상세 화면에 표시한다.
function semanticPanel(v){
 if(!v?.enabled)return '';
 const kinds={absolute_temperature:'절대 온도',temperature_anomaly:'온도 편차',temperature_difference:'온도 차이',declared_other:'직접 지정한 물리량',unknown:'물리량 미확인'};
 const reviews={APPROVED:'사용자 검토 완료',NEEDS_REVIEW:'검토 대기',REJECTED:'승인 거절'};
 const goal=v.goal?.scope;
 const meaning=table(['열 / 물리량','단위 / 저장 배율','기준','지역 / 기간','의미 검토'],(v.sources||[]).map(s=>[esc(s.column+' · '+s.quantity_name+' / '+(kinds[s.quantity_kind]||s.quantity_kind)),esc(s.unit+' / '+s.storage_scale),esc(s.baseline||'미지정'),esc(s.spatial_scope+' / '+s.temporal_scope),esc(reviews[s.review_status]||s.review_status)]));
 const labels={SOURCE_SEMANTIC_MATCH:'자료 의미',TRANSFORMATION_LINEAGE:'변환 이력',GOAL_SCOPE_MATCH:'연구 질문 범위',CLAIM_SUPPORT:'주장 근거'};
 const checks=(v.current_checks||[]).flatMap(r=>r.checks.map(c=>[esc(labels[c.check_id]||c.check_id),tag(c.passed?'PASS':'FAIL'),esc(c.message)]));
 const transforms=(v.lineages||[]).map(r=>[esc(r.parent_id+' → '+r.child_id),tag(r.verification.passed?'PASS':'FAIL'),esc(r.verification.message)]);
 return `<section id="semantic-details" class="section"><h2>자료 의미와 연구 질문</h2><p class="muted">단위와 물리량 의미는 별도로 검토합니다. 현재 버전의 검사 결과입니다.</p>${meaning}${goal?fields({'연구 질문':v.goal.original_question,'분석 의도':goal.intent,'추정 대상':goal.estimand,'모집단':goal.population_scope,'지역':goal.spatial_scope,'기간':goal.temporal_scope,'제한 사항':goal.limitations.join('; ')||'정보 없음'}):'<p>연구 질문 검토 대기</p>'}${checks.length?table(['검사 범위','현재 상태','사유'],checks):'<p>자료 의미 검사 전</p>'}${transforms.length?table(['변환','현재 상태','사유'],transforms):''}<p class="muted">정답률과 반복 일관성은 별도 평가입니다. 실제 API에서의 성능은 확인하지 않았습니다.</p></section>`;
}
