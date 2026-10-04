"""질문 변경 후 기후 결과의 실제 release·해시·canary·변조·현재성 경계를 검사한다."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from htrsa.workbench import WorkbenchAPI
from htrsa.qualified_profiles import conclusion_card
from htrsa.qualified_workflow import amend_question
from htrsa.qualified_replay import replay
from htrsa.release import export_release, ReleaseExportError
from htrsa.storage import sha256_file
from htrsa.preflight import _source_fingerprint


def main():
    observed = json.loads((ROOT / "build/cycle12/browser_results.json").read_text(encoding="utf-8"))
    source = (ROOT / observed["output_dir"]).resolve()
    base = (ROOT / "build/cycle12/qualified-export" / uuid4().hex).resolve()
    assert source.is_relative_to(ROOT / "build/cycle12/browser") and base.is_relative_to(ROOT / "build/cycle12")
    base.mkdir(parents=True)
    with sqlite3.connect(source / "state.sqlite") as original, sqlite3.connect(base / "state.sqlite") as copied:
        original.backup(copied)
    shutil.copytree(source / "workspace", base / "workspace")
    rid = json.loads((source / "execution.json").read_text(encoding="utf-8"))["research_id"]
    app = WorkbenchAPI(base / "state.sqlite", base / "workspace", launch=False)
    state = app.read._state
    assert conclusion_card(state, rid)["current"]
    canary = "sk-" + uuid4().hex + uuid4().hex
    variable = "HTRSA_CYCLE12_EXPORT_CANARY_KEY"
    previous = os.environ.get(variable)
    os.environ[variable] = canary
    try:
        output = base / "release"
        result = export_release(state, rid, output)
        manifest = output / "manifests/release_manifest.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        for record in document["files"]:
            path = (output / record["path"]).resolve()
            assert path.is_relative_to(output) and sha256_file(path) == record["sha256"]
            assert canary.encode("utf-8") not in path.read_bytes()
        frozen = output / "research_output/replay_manifest.json"
        checked = replay(frozen)
        assert checked["representation_status"] == "MATCHED_SELECTED_KEYS"
        binding = json.loads(frozen.read_text(encoding="utf-8"))["binding"]
        capture = frozen.parent / "qualified_sources" / Path(binding["source_relative"]).name
        original_bytes = capture.read_bytes()
        capture.write_bytes(original_bytes + b"tamper")
        try:
            replay(frozen)
        except ValueError:
            tamper = True
        else:
            raise AssertionError("변조된 기후 내보내기를 승인했습니다")
        finally:
            capture.write_bytes(original_bytes)
        question = conclusion_card(state, rid)["question"].replace("2011~2020", "2012~2020")
        amend_question(state, rid, question, expected_version=state.state_version(rid))
        try:
            export_release(state, rid, base / "stale-release")
        except ReleaseExportError:
            stale = True
        else:
            raise AssertionError("질문 변경 전 보고서를 현재 release로 승인했습니다")
        record = {"passed": True, "source_fingerprint": _source_fingerprint(), "execution": "OFFLINE_FAKE_AGENT_CURRENT_QUALIFIED_RELEASE",
                  "file_count": len(document["files"]), "hash_mismatches": [], "canary_leaks": [], "replay": checked,
                  "tamper_blocked": tamper, "stale_blocked": stale, "manifest": manifest.relative_to(ROOT).as_posix(),
                  "manifest_sha256": sha256_file(manifest), "paid_calls": 0, "live_efficacy": "NOT_VALIDATED"}
        text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
        (ROOT / "build/cycle12/qualified_export.json").write_bytes(text.encode("utf-8", errors="strict"))
        print(json.dumps(record, ensure_ascii=False))
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous
        app.close()


if __name__ == "__main__":
    main()
