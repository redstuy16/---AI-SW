"""실제 브라우저 흐름에 모의 키 저장과 기존 FakeProvider 실행을 연결한다."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from probe.workbench import WorkbenchAPI, OwnerSession, create_server
from probe.control_plane import Connection, ModelProfile, Credentials, ControlError
from probe.resource_policy import save_preferences


class MemoryStore:
    available = True
    def __init__(self):
        self.values = {}
    def read(self, name):
        return self.values.get(name), None
    def write(self, name, value):
        if value is None:
            self.values.pop(name, None)
        else:
            self.values[name] = value


class OfflineAPI(WorkbenchAPI):
    def command(self, rid, action, body):
        result = super().command(rid, action, body)
        if action in {'start', 'resume'}:
            from probe.control_runtime import execute
            from probe.providers.fake import FakeProvider
            from test_autonomous_loop import fake_replies, initial_coordinator
            replies = fake_replies()
            def bounded_coordinator(call):
                value = initial_coordinator(call)
                value['max_cost_usd'] = 0.01
                return value
            replies[2] = bounded_coordinator
            asyncio.run(execute(self.database, self.workspace, rid,
                        provider_factory=lambda *_: FakeProvider(replies)))
        return result


def main():
    folder = Path(sys.argv[1])
    api = OfflineAPI(folder / 'state.sqlite', folder / 'workspace', launch=False)
    api.credentials.os_store = MemoryStore()
    api.credentials.file = folder / 'private-unused.env'
    api.store.put('connection', 'offline', Connection(connection_id='offline', display_name='오프라인 검증 서버', adapter_id='openai_compatible', base_url='http://127.0.0.1:1234/v1', endpoint_class='loopback', destination_approved=True, auth_strategy='none', credential_env_name=None))
    api.store.put('model', 'offline', ModelProfile(profile_id='offline', connection_id='offline', model_id='offline-fixed', display_name='오프라인 검증 모델', protocol='chat', capability_status='supported', local_api_unmetered=True))
    save_preferences(api.store, {'model_profile_id': 'offline'})
    api.request("POST","/api/control/preferences",{"explanation_prompt_dismissed":True})
    session = OwnerSession()
    server = create_server(api, session=session)
    print('http://' + server.RequestHandlerClass.authority + '/#bootstrap=' + session.issue_bootstrap(), flush=True)
    try:
        server.serve_forever()
    finally:
        session.close()
        server.server_close()
        api.close()


if __name__ == '__main__':
    main()
