"""현재 Demo를 메모리 인증 canary와 함께 실제 내보내고 검사한다."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

from probe.release import export_release, _secret_free
from probe.workbench import OwnerSession, WorkbenchAPI, redact


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--qa-root", type=Path, default=ROOT / "build/browser-auth-qa-final")
    args = parser.parse_args()
    record = json.loads((args.qa_root / "artifact_validation.json").read_text(encoding="utf-8"))
    folder = Path(record["clean_database"]).parent
    original_key = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "qa-api-canary-" + secrets.token_hex(16)
    session = OwnerSession()
    ticket = session.issue_bootstrap()
    session.exchange(ticket)
    values = [ticket, session.cookie, session.csrf, os.environ["OPENAI_API_KEY"]]
    app = WorkbenchAPI(folder / "state.sqlite", folder / "workspace", launch=False)
    try:
        rid = app.store.db.execute("SELECT research_id FROM research_runs").fetchone()[0]
        blocked = 0
        for value in values:
            encoded = json.dumps({"value": value}).replace(value[0], "\\u" + format(ord(value[0]), "04x"), 1).encode("utf-8")
            for name, data in (("report.md", value.encode()), ("trace.json", encoded), ("figure.png", b"\x89PNG\r\n" + value.encode())):
                assert not _secret_free(name, data)
                blocked += 1
        assert not any(value in json.dumps(redact({"diagnostic": values})) for value in values)
        for value in values[:3]:
            response = app.request("POST", "/api/control/defaults", {"value": {"research_depth": value}})
            assert response.status == 409 and response.body["error"] == "SECRET_IN_CONFIG"
        output = ROOT / "build/local-auth-export" / uuid4().hex
        exported = export_release(app.read._state, rid, output)
        manifest = json.loads(Path(exported["manifest"]).read_text(encoding="utf-8"))
        mismatches, leaks = [], []
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != item["sha256"]: mismatches.append(item["path"])
            if any(value.encode() in data for value in values): leaks.append(item["path"])
        for name in ("/api/control/settings", "/api/control/environment", f"/api/research/{rid}/report", f"/api/research/{rid}/timeline"):
            response = app.request("GET", name)
            assert not any(value in json.dumps(response.body) for value in values)
        persistent = [app.database, *[p for p in app.workspace.rglob("*") if p.is_file()]]
        assert all(value.encode() not in p.read_bytes() for p in persistent for value in values)
        result = {"passed": not mismatches and not leaks, "file_count": len(manifest["files"]),
                  "manifest": exported["manifest"], "hash_mismatches": mismatches, "canary_leaks": leaks,
                  "memory_positive_controls_blocked": blocked, "auth_input_blocks": 3,
                  "api_views_checked": 4, "db_workspace_exposures": 0, "paid_live_calls": 0}
        data = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        assert not any(value in data for value in values)
        (ROOT / "qa/results/local_auth_export_results.json").write_bytes(data.encode("utf-8", errors="strict"))
        print(json.dumps(result, ensure_ascii=True))
    finally:
        app.close(); session.close()
        if original_key is None: os.environ.pop("OPENAI_API_KEY", None)
        else: os.environ["OPENAI_API_KEY"] = original_key


if __name__ == "__main__":
    main()
