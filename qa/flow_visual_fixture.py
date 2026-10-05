"""브라우저 검증용 실제 오프라인 데모·복구 기록을 만든다."""
from pathlib import Path
import argparse
import asyncio
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from probe.workbench import WorkbenchAPI,OwnerSession,create_server


def main():
    parser=argparse.ArgumentParser();parser.add_argument('folder',type=Path);parser.add_argument('--prepare',action='store_true');args=parser.parse_args()
    folder=args.folder;folder.mkdir(parents=True,exist_ok=True)
    if args.prepare:
        from f3p_eval import prepare
        from probe.demo import run_demo_a,run_demo_b
        db,state,agent,prepared,_=prepare(folder)
        original=state.stage;count=[0]
        def changed(payload):
            count[0]+=1
            if count[0]==1:
                from copy import deepcopy
                payload.agent_result.output=deepcopy(payload.agent_result.output)
                payload.agent_result.output['metrics']['estimate']=.125
            return original(payload)
        state.stage=changed
        assert asyncio.run(agent.resume(prepared['research_id']))['verdict']=='PASS';db.close()
        a=run_demo_a(folder/'state.sqlite',folder/'workspace');b=run_demo_b(folder/'state.sqlite',folder/'workspace')
        value={'a':a['research_id'],'b':b['research_id'],'repair':prepared['research_id'],'execution':'OFFLINE_FAKE_PROVIDER'}
        text=json.dumps(value,ensure_ascii=False,indent=2)+'\n';text.encode('utf-8',errors='strict');(folder/'fixture.json').write_text(text,encoding='utf-8');print('오프라인 화면 자료 준비 완료');return
    api=WorkbenchAPI(folder/'state.sqlite',folder/'workspace',launch=False)
    session=OwnerSession();server=create_server(api,session=session)
    print('http://'+server.RequestHandlerClass.authority+'/#bootstrap='+session.issue_bootstrap(),flush=True)
    try:server.serve_forever()
    finally:session.close();server.server_close();api.close()


if __name__=='__main__':main()
