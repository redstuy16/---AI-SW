"""실제 작업 프로세스와 로컬 모의 HTTP를 검사한다. 실서비스 모델 검증은 아니다."""
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_autonomous_loop import CSV, fake_replies
from htrsa.control_plane import Connection, ModelProfile, ROLES
from htrsa.workbench import WorkbenchAPI


def main():
    folder = ROOT / "build" / ("gui-worker-" + uuid4().hex)
    replies, calls = deque(fake_replies()), []
    accepted, release = threading.Event(), threading.Event()

    class FixtureHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body["model"])
            reply = replies.popleft()
            if callable(reply):
                reply = reply({"input_text": body["messages"][-1]["content"]})
            if len(calls) == 1:
                accepted.set()
                if not release.wait(15):
                    raise TimeoutError("fixture release timeout")
            data = json.dumps({"id": "offline-fixture-" + str(len(calls)),
                "usage": {"prompt_tokens": 30, "completion_tokens": 10},
                "choices": [{"message": {"content": json.dumps(reply)}}]}).encode("utf-8", errors="strict")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app = WorkbenchAPI(folder / "state.sqlite", folder / "workspace", launch=True)
    try:
        (app.workspace / "inputs/data.csv").write_bytes(CSV.read_bytes())
        app.store.put("connection", "fixture", Connection(connection_id="fixture", display_name="Offline HTTP fixture",
            adapter_id="openai_compatible", base_url=f"http://127.0.0.1:{server.server_port}/v1",
            endpoint_class="loopback", destination_approved=True))
        model = ModelProfile(profile_id="fixture", connection_id="fixture", model_id="offline-fixture",
            protocol="chat", local_api_unmetered=True, capability_status="supported")
        app.store.put("model", "fixture", model)
        request = {"title": "Offline worker process QA", "question": "Association?", "source_relative": "data.csv",
                   "egress": "selected", "routing": {r: "fixture" for r in ROLES}}
        rid = app.create(request)["research_id"]
        command = {"expected_version": 0, "idempotency_key": "real-worker-start"}
        started = app.command(rid, "start", command)
        assert app.command(rid, "start", command) == started
        assert len(app.children) == 1
        assert accepted.wait(15), "first actual loopback request not received"
        run = app.store.run(rid)
        paused = app.command(rid, "pause", {"expected_version": run["version"], "idempotency_key": "pause-inflight"})
        assert paused["status"] == "PAUSE_REQUESTED"
        assert app.store.ledger(rid)["requests"][0]["status"] == "DISPATCHED"
        release.set()
        app.children[-1].wait(timeout=30)
        run = app.store.run(rid)
        assert run["status"] == "PAUSED", run
        assert len(calls) == 1
        app.command(rid, "resume", {"expected_version": run["version"], "idempotency_key": "resume-new-worker"})
        assert len(app.children) == 2
        app.children[-1].wait(timeout=60)
        run = app.store.run(rid)
        assert run["status"] == "COMPLETED", run["error"]
        assert not replies
        commits = app.store.db.execute("SELECT COUNT(*) FROM state_events WHERE research_id=?", (rid,)).fetchone()[0]
        assert commits == 2
        ledger = app.store.ledger(rid)
        assert len(ledger["requests"]) == len(calls)
        assert all(r["status"] == "SETTLED" for r in ledger["requests"])
        assert app.request("GET", f"/api/research/{rid}/report").body["available"]

        # 직접 작업 CLI에도 같은 가격 확인 경계를 적용한다.
        model.local_api_unmetered = False
        app.store.put("model", "fixture", model, 1)
        blocked_rid = app.create(request)["research_id"]
        app.store.db.execute("UPDATE control_runs SET status='STARTING' WHERE research_id=?", (blocked_rid,))
        before = len(calls)
        result = subprocess.run([sys.executable, "-m", "htrsa.workbench", str(app.database), str(app.workspace), "--worker", blocked_rid],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30, cwd=ROOT)
        assert result.returncode == 0
        assert app.store.run(blocked_rid)["status"] == "BUDGET_BLOCKED"
        assert app.store.run(blocked_rid)["error"] == "PRICE_REQUIRED"
        assert len(calls) == before and not app.store.ledger(blocked_rid)["requests"]
        report = {"passed": True, "provider": "offline loopback HTTP fixture", "live_local_model": "NOT_VALIDATED",
            "worker_processes": 3, "pause_inflight": True, "resumed_in_new_process": True,
            "duplicate_start_workers": 0, "http_dispatches": len(calls), "ledger_rows": len(ledger["requests"]),
            "commits": commits, "canonical_duplicate_commits": 0, "direct_worker_unpriced_dispatches": 0,
            "fixture_api_cost_usd": "0", "real_api_cost_usd": None, "research_id": rid}
        data = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict")
        (ROOT / "qa/results/gui_worker_results.json").write_bytes(data)
        print(data.decode("utf-8"))
    finally:
        release.set()
        for child in app.children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
        app.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
