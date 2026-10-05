"""고정 합성 상태와 별도 프로세스로 시작·조회·자원을 반복 측정한다."""
from __future__ import annotations
import argparse,json,os,shutil,sqlite3,statistics,subprocess,sys,time
from pathlib import Path
from threading import Event,Thread
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'src'))
def write(p,v):
 s=json.dumps(v,ensure_ascii=False,indent=2)+'\n';s.encode('utf-8',errors='strict');p.parent.mkdir(parents=True,exist_ok=True);p.write_text(s,encoding='utf-8')
def summary(values):return {'median':statistics.median(values),'min':min(values),'max':max(values),'samples':values}
def prepare(folder):
 from probe.demo import run_demo_a,run_demo_b
 from probe.workbench import WorkbenchAPI
 folder.mkdir(parents=True,exist_ok=True)
 a=run_demo_a(folder/'state.sqlite',folder/'workspace')
 b=run_demo_b(folder/'state.sqlite',folder/'workspace')
 api=WorkbenchAPI(folder/'state.sqlite',folder/'workspace',launch=False)
 db=api.store.db
 db.execute('BEGIN IMMEDIATE')
 now='2026-10-03T00:00:00+00:00'
 for i in range(101):
  db.execute('INSERT INTO research_runs(research_id,goal,created_at,research_question,run_status) VALUES(?,?,?,?,?)',(f'R-PERF-{i}',f'합성 성능 자료 {i}',now,f'합성 성능 자료 {i}','PAUSED'))
 large='R-PERF-0'
 exp=dict(db.execute('SELECT * FROM experiments WHERE research_id=? LIMIT 1',(a['research_id'],)).fetchone())
 evidence=dict(db.execute('SELECT * FROM evidence WHERE experiment_id IS NOT NULL LIMIT 1').fetchone())
 mutation=dict(db.execute('SELECT * FROM staged_mutations LIMIT 1').fetchone())
 def insert(table,row):
  keys=list(row);db.execute('INSERT INTO '+table+'('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',tuple(row[k] for k in keys))
 for i in range(300):
  eid=f'EXP-PERF-{i}'
  insert('experiments',exp|{'experiment_id':eid,'research_id':large,'dataset_id':None,'dataset_ref':None,'task_id':None,'result_artifact_id':None,'status':'INVALIDATED','payload_json':'{}'})
  insert('evidence',evidence|{'evidence_id':f'E-PERF-{i}','research_id':large,'experiment_id':eid,'source_id':None,'source_ref':None,'status':'INVALIDATED','claim':f'합성 기록 {i}','payload_json':'{}'})
  insert('staged_mutations',mutation|{'mutation_id':f'MUT-PERF-{i}','research_id':large,'status':'ROLLED_BACK','base_state_version':0,'payload_json':json.dumps({'scientific':{'experiment_id':eid}}),'verification_json':json.dumps({'verdict':'FAIL','checks':[]})})
 for i in range(5000):db.execute('INSERT INTO runtime_events(research_id,event_type,details_json,created_at) VALUES(?,?,?,?)',(large,'SYNTHETIC_PROFILE',json.dumps({'iteration':i}),now))
 db.execute('COMMIT')
 api.close()
 write(folder/'fixture.json',{'small':a['research_id'],'large':large,'demo_a':a['validation'],'demo_b':b['validation'],'synthetic':True,'note':'복제한 합성 대형 행은 INVALIDATED/ROLLED_BACK. 과학 정답/출시 통과 자료로 쓰지 않음'})
def cold(folder,idle):
 started=time.perf_counter()
 from probe.workbench import WorkbenchAPI
 api=WorkbenchAPI(folder/'cold.sqlite',folder/'workspace',launch=False)
 ready=(time.perf_counter()-started)*1000
 import psutil
 p=psutil.Process();cpu=[];rss=[];p.cpu_percent()
 for _ in range(idle):cpu.append(p.cpu_percent(interval=1));rss.append(p.memory_info().rss/1024**2)
 if not rss:rss=[p.memory_info().rss/1024**2];cpu=[p.cpu_percent(interval=.1)]
 result={'cold_start_ms':ready,'backend_ready_ms':ready,'idle_cpu_percent':statistics.mean(cpu),'idle_cpu_max_percent':max(cpu),'idle_ram_mb':statistics.median(rss),'idle_seconds':idle,'process_count':1+len(p.children(recursive=True)),'heavy_modules':[k for k in ('numpy','scipy','matplotlib','pandas','sklearn','statsmodels') if k in sys.modules]}
 api.close();return result
def demo(folder,label):
 import psutil
 p=psutil.Process();stop=Event();samples=[]
 def sample():
  p.cpu_percent()
  while not stop.wait(.1):
   children=p.children(recursive=True)
   samples.append((sum(q.memory_info().rss for q in [p,*children] if q.is_running())/1024**2,p.cpu_percent(),1+len(children)))
 t=Thread(target=sample,daemon=True);t.start()
 try:
  from probe.demo import run_demo_a,run_demo_b
  start=time.perf_counter();v=(run_demo_a if label=='A' else run_demo_b)(folder/'demo.sqlite',folder/'workspace');elapsed=(time.perf_counter()-start)*1000
 finally:stop.set();t.join()
 return {'duration_ms':elapsed,'passed':v['validation']['passed'],'peak_ram_mb':max(x[0] for x in samples),'peak_cpu_percent':max(x[1] for x in samples),'peak_process_count':max(x[2] for x in samples)}
def loads(fixture,folder):
 from probe.workbench import WorkbenchAPI
 shutil.copytree(fixture/'workspace',folder/'workspace');shutil.copy2(fixture/'state.sqlite',folder/'state.sqlite')
 api=WorkbenchAPI(folder/'state.sqlite',folder/'workspace',launch=False)
 ids=json.loads((fixture/'fixture.json').read_text(encoding='utf-8'))
 paths={'research_list':'/api/control/research','research_detail':f"/api/research/{ids['large']}",'activity_view_small':f"/api/control/research/{ids['small']}/activity?limit=25",'activity_view_large':f"/api/control/research/{ids['large']}/activity?limit=25",'verification_view_open':f"/api/research/{ids['large']}/verification",'report_open':f"/api/research/{ids['small']}/report",'experiments_view_open':f"/api/research/{ids['large']}/experiments",'evidence_view_open':f"/api/research/{ids['large']}/evidence"}
 paths.update(ui_research_list='/api/control/research?limit=25',ui_verification=f"/api/control/research/{ids['large']}/items?kind=verification&limit=25",ui_experiments=f"/api/control/research/{ids['large']}/items?kind=experiments&limit=25",ui_evidence=f"/api/control/research/{ids['large']}/items?kind=evidence&limit=25",ui_activity=f"/api/control/research/{ids['large']}/activity?summary=1&limit=25")
 result={};queries={}
 for key,path in paths.items():
  times=[];qs=[]
  for _ in range(6):
   count=[0];api.store.db.set_trace_callback(lambda _:count.__setitem__(0,count[0]+1));start=time.perf_counter();response=api.request('GET',path);times.append((time.perf_counter()-start)*1000);qs.append(count[0]);api.store.db.set_trace_callback(None);assert response.status==200,(path,response.body)
  result[key+'_first_ms']=times[0];result[key+'_repeat_ms']=summary(times[1:]);queries[key]=qs
 result['queries']=queries
 result['fixture_counts']={table:api.store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('research_runs','runtime_events','experiments','evidence','staged_mutations')}
 result['evidence_query_plan']=[tuple(x) for x in api.store.db.execute('EXPLAIN QUERY PLAN SELECT * FROM evidence WHERE research_id=? AND experiment_id=?',(ids['large'],'EXP-PERF-0'))]
 result['query_plan']=[tuple(x) for x in api.store.db.execute('EXPLAIN QUERY PLAN SELECT * FROM runtime_events WHERE research_id=? ORDER BY seq DESC LIMIT 25',(ids['large'],))]
 api.close()
 files=[p for p in (folder/'workspace').rglob('*') if p.is_file()]
 result.update(sqlite_size_mb=(folder/'state.sqlite').stat().st_size/1024**2,trace_size_mb=sum(p.stat().st_size for p in files if p.suffix=='.jsonl')/1024**2,workspace_size_mb=sum(p.stat().st_size for p in files)/1024**2)
 return result
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--fixture',type=Path,default=ROOT/'build/flow-opt-fixture');parser.add_argument('--output',type=Path);parser.add_argument('--prepare',action='store_true');parser.add_argument('--worker',choices=['cold','A','B']);parser.add_argument('--idle',type=int,default=0);parser.add_argument('--folder',type=Path);args=parser.parse_args()
 if args.prepare:prepare(args.fixture);print('합성 기준 자료 생성 완료');return
 if args.worker:
  args.folder.mkdir(parents=True,exist_ok=True);v=cold(args.folder,args.idle) if args.worker=='cold' else demo(args.folder,args.worker);print(json.dumps(v));return
 folder=(ROOT/'build/flow-opt-measurements'/uuid4().hex);folder.mkdir(parents=True)
 values={'cold':[],'A':[],'B':[]}
 for label in values:
  for i in range(3):
   command=[sys.executable,str(Path(__file__).resolve()),'--worker',label if label!='cold' else 'cold','--folder',str(folder/(label+str(i)))]
   if label=='cold' and i==0:command+=['--idle','60']
   c=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=180);assert c.returncode==0,c.stderr;values[label].append(json.loads(c.stdout))
 result=loads(args.fixture,folder/'loads')
 from probe.preflight import _source_fingerprint
 result.update(source_fingerprint=_source_fingerprint(),environment={'python':sys.version.split()[0],'os':os.name,'logical_cpu':os.cpu_count()},cold=values['cold'],demos={'A':values['A'],'B':values['B']},local_model_metrics='NOT_APPLICABLE',gpu_metrics='NOT_APPLICABLE',browser_ready_ms='MEASURE_SEPARATELY',scope='별도 Python 본체+분석 자식 프로세스 · CPU는 한 코어 100% · 브라우저 RAM 제외',fixture=str(args.fixture.relative_to(ROOT)))
 write(args.output,result);print(json.dumps({'output':str(args.output.resolve().relative_to(ROOT)),'cold_median_ms':statistics.median(v['cold_start_ms'] for v in values['cold']),'list_repeat_ms':result['research_list_repeat_ms'],'counts':result['fixture_counts']}))
if __name__=='__main__':main()
