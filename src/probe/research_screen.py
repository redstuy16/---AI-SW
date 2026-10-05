"""연구 기본 화면의 읽기 전용 요약. 실행·과학 상태는 기존 기록을 사용한다."""
import json

from .research_flow import observed_control, public_fields

ACTIVE = {'ACTIVE', 'STARTING', 'RUNNING', 'RESUMING', 'PAUSE_REQUESTED', 'STOP_REQUESTED'}
STATUS = {
    'DRAFT': ('준비 중', 'waiting'), 'ACTIVE': ('진행 중', 'running'),
    'STARTING': ('진행 중', 'running'), 'RUNNING': ('진행 중', 'running'),
    'RESUMING': ('진행 중', 'running'), 'PAUSE_REQUESTED': ('일시정지 요청 중', 'running'),
    'STOP_REQUESTED': ('중단 요청 중', 'running'), 'PAUSED': ('일시정지', 'waiting'),
    'STOPPED': ('중단됨', 'failed'), 'FAILED': ('확인 필요', 'failed'),
    'INSUFFICIENT_DATA': ('자료 부족으로 종료', 'review'), 'BUDGET_BLOCKED': ('예산 부족', 'review'),
    'NEEDS_RECONCILIATION': ('비용 확인 필요', 'review'), 'PREFLIGHT_BLOCKED': ('확인 필요', 'review'),
    'NEEDS_REVALIDATION': ('재검증 필요', 'review'), 'NEEDS_SEMANTIC_REVIEW': ('확인 필요', 'review'),
    'COMPLETED': ('완료', 'complete'),
}


def status_badge(status, *, design_only=False, stale=False, partial=False):
    if stale and status not in ACTIVE:
        label, tone = '재검증 필요', 'review'
    elif design_only and status == 'COMPLETED':
        label, tone = '설계안 완료', 'review'
    elif partial and status == 'COMPLETED':
        label, tone = '부분 완료', 'review'
    else:
        label, tone = STATUS.get(status, ('확인 필요', 'review'))
    return {'code': status, 'label': label, 'tone': tone}


def saved_report_stale(state, rid, version):
    """목록에는 저장 보고서의 버전·파일 해시를 조회하고 상세 검사는 연구를 열 때 수행한다."""
    from .storage import sha256_file
    from .report_publication import report_root
    root = report_root(state, rid)
    manifest = root / 'manifests/artifact_manifest.json'
    if not manifest.is_file():
        return False
    try:
        record = json.loads(manifest.read_text(encoding='utf-8', errors='strict'))
        return record.get('validation_version') != 2 or record['state_version'] != version or any(
            sha256_file(root / path) != digest
            for path, digest in record['files'].items())
    except (OSError, ValueError, KeyError):
        return True


def screen_summary(api, rid):
    from .beginner_controls import progress
    from .dashboard import project_overview
    from .research_report import execution_summary
    from .resource_policy import activity_page
    state, db = api.read._state, api.store.db
    api.read._research(rid)
    stored = db.execute('SELECT status,version,pid FROM control_runs WHERE research_id=?', (rid,)).fetchone()
    control = observed_control(api, rid, stored)
    overview = project_overview(state, rid).model_dump(mode='json')
    if control:
        value = execution_summary(api, rid)
    else:
        value = {**progress(api, rid), 'question': overview['question'], 'status': overview['status'],
                 'version': None, 'state_version': overview['state_version'], 'report': None,
                 'completion_kind': 'RESEARCH', 'blocker': None, 'additional_errors': [],
                 'search': {'status': 'WAITING', 'last': None}, 'counts': {}}
    status = control['status'] if control else overview['status']
    value['status'] = status
    counts = dict(value['counts'])
    counts.update(sources=db.execute('SELECT COUNT(*) FROM sources WHERE research_id=?', (rid,)).fetchone()[0],
                  verified=overview['verified_evidence_count'], experiments=db.execute('SELECT COUNT(*) FROM experiments WHERE research_id=?', (rid,)).fetchone()[0],
                  verified_experiments=overview['verified_experiment_count'])
    if not control:
        counts.update(relevant=db.execute("SELECT COUNT(*) FROM sources WHERE research_id=? AND status IN ('RELEVANT','EVIDENCE_EXTRACTED','VERIFIED')", (rid,)).fetchone()[0],
                      readable=db.execute("SELECT COUNT(*) FROM sources WHERE research_id=? AND COALESCE(abstract,'')<>'' AND status<>'IRRELEVANT'", (rid,)).fetchone()[0], requests=0)
    counts['review'] = db.execute("SELECT COUNT(*) FROM hypotheses WHERE research_id=? AND status IN ('INCONCLUSIVE','CONFLICTED','NEEDS_REVALIDATION')", (rid,)).fetchone()[0]
    checks = db.execute("SELECT COUNT(*),COALESCE(SUM(status='COMMITTED' AND json_extract(verification_json,'$.verdict')='PASS'),0) FROM staged_mutations WHERE research_id=? AND verification_json IS NOT NULL", (rid,)).fetchone()
    counts.update(checks_total=checks[0], checks_passed=checks[1])
    citations = db.execute("SELECT COUNT(*),COALESCE(SUM(status='VERIFIED' AND json_extract(verification_json,'$.passed')=1),0) FROM evidence WHERE research_id=? AND source_type='LITERATURE' AND verification_json IS NOT NULL", (rid,)).fetchone()
    counts['checks_total'] += citations[0]
    counts['checks_passed'] += citations[1]
    counts['tools'] = db.execute('SELECT COUNT(DISTINCT tool_name) FROM tool_dispatches WHERE research_id=?', (rid,)).fetchone()[0]
    counts['artifacts'] = db.execute('SELECT COUNT(*) FROM artifacts WHERE research_id=?', (rid,)).fetchone()[0]
    card, report = value['card'], value.get('report')
    from .report_publication import report_root
    saved_report = (report_root(state, rid) / 'manifests/artifact_manifest.json').is_file()
    stale = bool(card.get('available') and not card.get('current') or report and not report.get('current')
                 or status not in ACTIVE | {'DRAFT','PAUSED'} and saved_report and not value['report_ready'])
    design_only = value.get('completion_kind') == 'DESIGN_ONLY'
    counts['revalidation'] = int(stale)
    counts['review'] += int(bool(value.get('blocker'))) + counts['revalidation']
    value.update(counts=counts, official_status=status_badge(status, design_only=design_only, stale=stale, partial=bool(report and report['status']=='PARTIAL')),
                 currentness='STALE' if stale else 'CURRENT', controlled=bool(control), overview=overview)
    timeline = activity_page(state, rid, limit=5, offset=0, summary=True)['items']
    labels = {'AGENT_RUN_COMPLETED': 'AI 작업 완료', 'SEARCH_COMPLETED': '자료 검색 완료',
              'SEARCH_DISPATCHED': '자료 검색 시작', 'SEARCH_POLICY_DECISION': '검색 설정 확인',
              'REPORT_DRAFT_COMPLETED': '보고서 초안 작성 완료', 'REPORT_WRITING_LIMITED': '부분 보고서 저장',
              'REPORT_WRITING_STARTED': '보고서 작성 시작', 'REPORT_WRITING_COMPLETED': '보고서 작성 완료',
              'LITERATURE_DESIGN_READY': '실험 설계 저장', 'LITERATURE_ACQUISITION_LIMITATION': '자료 확보 제한 확인',
              'REPORT_LOCAL_REBASED': '현재 계산으로 부분 보고서 갱신', 'RUNTIME_RECONCILED': '저장 기록 복구',
              'RECOVERY_COMPLETED': '복구 완료', 'PROCESS_RECOVERY_STARTED': '저장 기록 복구 시작',
              'ACTION_REPLAY_SKIPPED': '완료한 작업 재사용', 'RESEARCH_INPUT_LIMITATION': '자료 범위 확인',
              'CONTROL_COMPLETED': '연구 실행 종료'}
    value['timeline'] = [{**event, 'label': labels.get(event['event_type'], event.get('label') or '연구 기록')} for event in timeline]
    value['updated_at'] = max((e['timestamp'] for e in timeline if e.get('timestamp')), default=None)
    tools = {r[0]: r[1] for r in db.execute('SELECT tool_name,status FROM tool_dispatches WHERE research_id=? ORDER BY rowid', (rid,))}
    finished = {name for name, tool_status in tools.items() if tool_status == 'FINISHED'}
    phases = {p['id']: p['status'] for p in value.get('phases', [])}
    running = status in ACTIVE
    done = status not in ACTIVE | {'DRAFT', 'PAUSED'}
    rows = db.execute("SELECT c.contract_id,c.status,json_extract(c.contract_json,'$.assigned_role') AS role,json_extract(c.contract_json,'$.objective') AS objective FROM contracts c WHERE research_id=? ORDER BY rowid DESC LIMIT 25", (rid,)).fetchall()
    current = next((dict(row) for row in rows if row['status'] in {'RUNNING','ISSUED','WAITING_RETRY'}), None) if running else None
    last = dict(rows[0]) if rows else None
    for task in (current, last):
        if task:
            task['objective'] = (task.get('objective') or '').split('\n', 1)[0][:160]
    report_job = db.execute("SELECT status FROM runtime_steps WHERE research_id=? AND step_key LIKE 'report-draft:%' ORDER BY rowid DESC LIMIT 1", (rid,)).fetchone()
    search_status = phases.get('search', 'COMPLETED' if 'literature.search' in finished else 'SEARCH_NOT_NEEDED' if done else 'WAITING')
    design_ready = bool(report and report.get('draft') or 'analysis.plan' in finished or counts['experiments'])
    stage_rows = [
        ('question', '질문 정리', phases.get('question', 'COMPLETED' if overview['action_count'] else 'WAITING')),
        ('search', '자료 찾기', search_status),
        ('evidence', '자료 확인', 'COMPLETED' if counts['verified'] or 'data.profile' in finished else 'EMPTY' if done else 'WAITING'),
        ('analysis', '분석 설계', 'COMPLETED' if design_ready else 'RUNNING' if current and current['role']=='analysis_planner_worker' else 'WAITING'),
        ('execution', '실행', 'COMPLETED' if counts['verified_experiments'] or 'stats.run' in finished else 'SKIPPED' if done and design_ready and not counts['experiments'] else 'WAITING'),
        ('verification', '검증', 'NEEDS_REVIEW' if stale else 'COMPLETED' if counts['checks_passed'] or counts['verified'] else 'SKIPPED' if done and design_only else 'WAITING'),
        ('report', '보고서', 'NEEDS_REVIEW' if stale else 'RUNNING' if report_job and report_job[0]=='RUNNING' and running else (report or {}).get('status', 'COMPLETED' if value['report_ready'] else 'WAITING'))]
    if not running:
        stage_rows = [(key, name, 'FAILED' if status=='FAILED' else 'WAITING') if state_name=='RUNNING' else (key,name,state_name) for key,name,state_name in stage_rows]
    value['stages'] = [{'id': key, 'label': name, 'status': state_name} for key,name,state_name in stage_rows]
    active_stage = next((s for s in value['stages'] if s['status']=='RUNNING'), None)
    pending_stage = next((s for s in value['stages'] if s['status']=='WAITING'), None) if running else None
    value['work'] = {'current': current, 'last': last, 'stage': active_stage['label'] if active_stage else '작업 중' if running else '실행 종료' if done else '작업 대기',
                     'next': pending_stage['label'] if pending_stage else None}
    value['conclusion_use'] = 'STALE' if stale else 'DESIGN_ONLY' if design_only else 'CURRENT' if card.get('available') and card.get('current') and (not report or bool((report.get('draft') or {}).get('claims'))) else 'UNCONFIRMED'
    claims = []
    for row in db.execute("SELECT h.hypothesis_id,h.statement,h.status,COUNT(e.evidence_id) AS evidence_count,SUM(e.status='VERIFIED') AS verified_count,MIN(e.evidence_id) AS evidence_id FROM hypotheses h LEFT JOIN evidence e ON e.research_id=h.research_id AND e.target_hypothesis_id=h.hypothesis_id WHERE h.research_id=? GROUP BY h.hypothesis_id ORDER BY h.rowid LIMIT 25", (rid,)):
        claims.append(dict(row))
    if not claims and report and report.get('current') and not stale and value['report_ready']:
        for item in report.get('draft', {}).get('claims', [])[:25]:
            claims.append({'statement':item['text'], 'status':'VERIFIED', 'evidence_count':1, 'verified_count':1, 'evidence_id':item['evidence_id']})
    value['claims'] = claims
    if stale and report and report.get('current') and not value['report_ready']:
        value['report'] = {**report, 'draft': None}
    value['overview'].pop('current_contract', None)
    value.pop('recent', None)
    value.pop('phases', None)
    return public_fields(value)
