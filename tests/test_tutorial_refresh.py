"""발급 안내와 새로고침 시 화면 파일의 누락을 검사한다."""
from hashlib import sha256
from pathlib import Path
import socket
import threading

from probe.tutorial_guide import tutorial_catalog, render_tutorial_guide
from probe.workbench import create_server


def test_api_issuance_has_emphasis_links_and_next_action():
    catalog = tutorial_catalog()
    guide = render_tutorial_guide()
    assert catalog['invitation']['title'] == catalog['invitation']['accept'] == '튜토리얼 보기'
    for provider in catalog['providers']:
        if provider['id'] == 'openai_compatible':
            continue
        assert '**비밀번호 같은 긴 글자**' in provider['key_intro']
        assert '**키 준비 완료**' in provider['key_finish']
        assert provider['key_intro'] in guide and provider['key_terms'] in guide
        assert provider['keys'].startswith('https://') and provider['keys'] in guide
        assert any('**' in step for step in provider['key_steps'])


def test_research_modes_share_files_and_have_distinct_guidance():
    routes = tutorial_catalog()['coach_targets']
    simple = routes['research_input']['targets']
    detailed = routes['research_design']['targets']
    assert simple[0]['target'] == detailed[0]['target'] == '#research-input-mode'
    files = [entry for entry in simple + detailed if entry['target'].endswith('[name=attachment_files]')]
    assert len(files) == 2 and files[0]['text'] == files[1]['text']
    assert all('필요한 파일' in entry['text'] and 'CSV' not in entry['text'] for entry in files)
    assert any(entry['target'] == '#design-review' for entry in detailed)
    query = next(entry for entry in routes['research_model']['targets'] if entry['target'].endswith('[name=search_policy]'))
    assert 'AI' in query['detail'] and '한국어·영어' in query['detail'] and '비공개' in query['detail']
    assert not any('public_search_consent' in entry['target'] or 'public_search_query' in entry['target'] for entry in routes['research_model']['targets'])
    egress = next(entry for entry in routes['research_start']['targets'] if entry['target'].endswith('[name=egress]'))
    assert egress['fallback'] == '#research-advanced>summary'


def test_reload_asset_burst_is_queued_without_missing_scripts():
    server = create_server(object())
    connections = []
    thread = None
    origin = f'127.0.0.1:{server.server_port}'
    names = ['bootstrap.js', 'cycle5_ui.js', 'workbench.js', 'provider_settings.js',
             'product_ux.js', 'live_api_test.js', 'tutorial_content.js', 'beginner_ux.js',
             'tutorial.js', 'research_design.js', 'workbench.css', 'research_flow.js']
    try:
        # 아직 처리 중인 요청이 있어도 브라우저의 동시 연결을 대기열에 받아야 한다.
        for name in names:
            connection = socket.create_connection(('127.0.0.1', server.server_port), timeout=2)
            connection.sendall(f'GET /assets/{name} HTTP/1.0\r\nHost: {origin}\r\n\r\n'.encode('ascii'))
            connections.append(connection)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        static = Path(__file__).resolve().parents[1] / 'src/probe/workbench_static'
        for name, connection in zip(names, connections):
            response = b''
            while chunk := connection.recv(65536):
                response += chunk
            header, body = response.split(b'\r\n\r\n', 1)
            assert header.startswith(b'HTTP/1.0 200')
            assert b'Cache-Control: no-store' in header
            assert sha256(body).digest() == sha256((static / name).read_bytes()).digest()
    finally:
        for connection in connections:
            connection.close()
        if thread is not None:
            server.shutdown()
            thread.join(5)
        server.server_close()
