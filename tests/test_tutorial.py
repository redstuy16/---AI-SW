"""학습 안내의 내용·상태·보안·기존 연구 경계를 검사한다."""
from copy import deepcopy
import csv
import hashlib
from io import StringIO
import json
from pathlib import Path
import statistics

import pytest
from pydantic import ValidationError

from htrsa.providers.native import DEFINITIONS
from htrsa.resource_policy import UIPreferences, preferences
from htrsa.tutorial_guide import TutorialProgress, tutorial_catalog, tutorial_lesson, render_tutorial_guide
from test_workbench import app, configure, create


ROOT = Path(__file__).resolve().parents[1]
CATALOG = tutorial_catalog()
STEPS = [step for chapter in CATALOG["chapters"] for step in chapter["steps"]]


def test_all_supported_providers_have_account_key_and_failure_instructions():
    assert {provider["id"] for provider in CATALOG["providers"]} == set(DEFINITIONS)
    for provider in CATALOG["providers"]:
        assert provider["account"] and provider["key_steps"] and provider["billing"] and provider["trouble"]
        assert provider["sources"]
        if provider["id"] != "openai_compatible":
            assert provider["console"].startswith("https://") and provider["keys"].startswith("https://")
    assert {tool["id"] for tool in CATALOG["local_tools"]} == {"LM_STUDIO", "OLLAMA"}
    assert all(tool["address"].startswith("http://127.0.0.1:") for tool in CATALOG["local_tools"])


def test_guide_document_matches_canonical_content():
    assert (ROOT / "docs/guides/BEGINNER_GUIDE.md").read_text(encoding="utf-8") == render_tutorial_guide()
    assert len(CATALOG["chapters"]) == 9
    assert len(STEPS) == len({step["id"] for step in STEPS}) == 30
    assert len(CATALOG["topics"]) == 41


def test_visual_course_titles_match_the_single_guide_source():
    guide = render_tutorial_guide()
    for step in CATALOG["quick_start"]:
        assert step["action_title"] in guide
        assert 0 < len(step["action_title"]) <= 30
        assert 0 < len(step["action_label"]) <= 30
    assert all(topic["step_id"] in {step["id"] for step in STEPS} for topic in CATALOG["topics"])
    assert {"API", "CSV", "PDF", "휴지통", "AI 연결", "계산 다시 확인하기"} <= {topic["title"] for topic in CATALOG["topics"]}


def test_core_demo_fixture_hash_rows_and_calculations_are_real():
    from htrsa.demo import FIXTURE
    fixture = {"source": str(FIXTURE.relative_to(ROOT)),
               "sha256": "390564522d80e826f147a23ad93bc41c3b7f0104931454a4e8b8bd05f1e60576",
               "rows": [{"temperature": str(x), "growth": str(y)} for x, y in [(10, 2), (12, 3), (14, 5), (16, 7), (18, 9), (20, 12)]],
               "pearson": 0.99061, "spearman": 1}
    raw = (ROOT / fixture["source"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == fixture["sha256"]
    rows = list(csv.DictReader(StringIO(raw.decode("utf-8"))))
    assert rows == fixture["rows"]
    x = [float(row["temperature"]) for row in rows]
    y = [float(row["growth"]) for row in rows]
    assert round(statistics.correlation(x, y), 6) == fixture["pearson"]
    ranks = lambda values: [sorted(values).index(value) + 1 for value in values]
    assert statistics.correlation(ranks(x), ranks(y)) == fixture["spearman"]
    assert "examples" not in CATALOG


@pytest.mark.parametrize("step", STEPS, ids=[step["id"] for step in STEPS])
def test_every_learning_step_has_an_action_result_and_recovery(step):
    for name in ["ready", "actions", "example", "expected", "trouble"]:
        assert step[name], (step["id"], name)
    assert len(step["actions"]) >= 3
    assert step["screen"] in {"none", "connection", "connections", "research", "check", "research_list", "settings_help"}
    assert step["page"] in {None, 0, 1, 2}


@pytest.mark.parametrize("value", [
    {"step_id": "unknown"}, {"chapter_id": "results"}, {"completed_steps": ["unknown"]},
    {"completed_steps": ["overview", "overview"]}, {"provider_id": "unknown"}, {"course_version": 5},
    {"api_key": "qa-tutorial-canary"}, {"step_id": "qa_tutorial_canary"},
    {"completed_steps": ["qa-tutorial-canary"]}, {"status": "COMPLETED"}, {"status": "VALIDATED"},
    {"local_tool": "remote"}, {"mode": "LIVE"}, {"chapter_id": "basics", "step_id": "files"},
])
def test_invalid_progress_or_secret_fields_are_rejected(value):
    with pytest.raises(ValidationError):
        TutorialProgress.model_validate(value)


def test_legacy_completion_remains_a_display_preference():
    legacy = UIPreferences.model_validate({"tutorial_completed": True})
    assert legacy.tutorial_completed and legacy.tutorial_progress is None
    assert not legacy.tutorial_do_not_ask
    assert not legacy.new_research_explanations


def test_tutorial_invitation_and_visual_hints_match_the_guide():
    guide = render_tutorial_guide()
    assert not UIPreferences().tutorial_do_not_ask
    for value in CATALOG["invitation"].values():
        assert value in guide
        value.encode("utf-8", errors="strict")
    assert "설정 → 도움말 및 안내" in CATALOG["invitation"]["reminder"]


@pytest.mark.parametrize("route", CATALOG["coach_targets"].values(), ids=CATALOG["coach_targets"].keys())
def test_each_visual_hint_has_a_short_instruction_and_target(route):
    guide = render_tutorial_guide()
    assert route["targets"]
    for target in route["targets"]:
        assert target["target"] and 0 < len(target["text"]) <= 70
        assert target["text"] in guide
        assert set(target) <= {"target", "fallback", "text", "local_text", "detail"}
        assert "value" not in target
        if target.get("detail"):
            assert target["detail"] in guide
            target["detail"].encode("utf-8", errors="strict")
        if target.get("local_text"):
            assert target["local_text"] in guide and "API 키" not in target["local_text"]


@pytest.mark.parametrize("never_ask", [True, False])
def test_invitation_preference_preserves_learning_research_and_execution_state(app, never_ask):
    configure(app)
    rid = create(app)
    progress = TutorialProgress(chapter_id="input", step_id="files", mode="GUIDED")
    assert app.request("POST", "/api/control/preferences", {
        "tutorial_progress": progress.model_dump(), "low_spec_mode": "LOW_SPEC",
    }).status == 200
    run, ledger, connections = deepcopy(app.store.run(rid)), deepcopy(app.store.ledger()), deepcopy(app.connections())
    before = preferences(app.store)
    assert app.request("POST", "/api/control/preferences", {"tutorial_do_not_ask": never_ask}).status == 200
    assert preferences(app.store) == {**before, "tutorial_do_not_ask": never_ask}
    assert app.store.run(rid) == run and app.store.ledger() == ledger and app.connections() == connections
    assert app.store.db.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0


def test_skipping_and_reading_all_are_distinct():
    skipped = TutorialProgress(status="DISMISSED", completed_steps=["overview"])
    assert skipped.status != "COMPLETED"
    completed = TutorialProgress(mode="EXAMPLE", status="COMPLETED", completed_steps=[step["id"] for step in STEPS])
    assert completed.status == "COMPLETED"
    with pytest.raises(ValidationError):
        TutorialProgress(status="COMPLETED", completed_steps=[step["id"] for step in STEPS])


def test_progress_api_preserves_research_credentials_budget_and_gates(app):
    configure(app)
    rid = create(app)
    before = deepcopy(app.store.run(rid))
    ledger = deepcopy(app.store.ledger())
    connections = deepcopy(app.connections())
    old_files = sorted(str(path) for path in app.workspace.rglob("*"))
    progress = TutorialProgress(chapter_id="input", step_id="files", mode="GUIDED", completed_steps=["overview"])
    response = app.request("POST", "/api/control/preferences", {"tutorial_progress": progress.model_dump()})
    assert response.status == 200
    assert preferences(app.store)["tutorial_progress"]["step_id"] == "files"
    assert not preferences(app.store)["tutorial_completed"]
    assert app.store.run(rid) == before
    assert app.store.ledger() == ledger and app.connections() == connections
    assert sorted(str(path) for path in app.workspace.rglob("*")) == old_files
    assert app.store.db.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0


def test_invalid_api_progress_does_not_overwrite_a_checkpoint(app):
    good = TutorialProgress(mode="EXAMPLE", completed_steps=["overview"])
    assert app.request("POST", "/api/control/preferences", {"tutorial_progress": good.model_dump()}).status == 200
    before = preferences(app.store)
    bad = app.request("POST", "/api/control/preferences", {"tutorial_progress": {"api_key": "qa-tutorial-canary"}})
    assert bad.status >= 400 and preferences(app.store) == before
    assert "qa-tutorial-canary" not in json.dumps(app.store.configs("ui_preferences"))


def test_new_guide_assets_are_packaged():
    from importlib.resources import files
    for name in ("tutorial_content.js", "tutorial.js"):
        assert files("htrsa").joinpath("workbench_static", name).is_file()


def test_progress_and_display_changes_preserve_each_other(app):
    assert app.request("POST", "/api/control/preferences", {
        "new_research_explanations": True, "low_spec_mode": "LOW_SPEC",
    }).status == 200
    progress = TutorialProgress(mode="GUIDED", completed_steps=["overview"])
    assert app.request("POST", "/api/control/preferences", {"tutorial_progress": progress.model_dump()}).status == 200
    saved = preferences(app.store)
    assert saved["new_research_explanations"] and saved["low_spec_mode"] == "LOW_SPEC"
    assert app.request("POST", "/api/control/preferences", {"new_research_explanations": False}).status == 200
    saved = preferences(app.store)
    assert not saved["new_research_explanations"]
    assert saved["tutorial_progress"] == progress.model_dump()
    assert saved["low_spec_mode"] == "LOW_SPEC"


def test_explicit_reset_still_resets_all_display_and_tutorial_preferences(app):
    progress = TutorialProgress(mode="GUIDED", completed_steps=["overview"])
    assert app.request("POST", "/api/control/preferences", {
        "new_research_explanations": True, "tutorial_progress": progress.model_dump(), "tutorial_do_not_ask": True,
    }).status == 200
    assert app.request("POST", "/api/control/preferences/reset", {}).status == 200
    assert preferences(app.store) == UIPreferences().model_dump(mode="json")


@pytest.mark.parametrize("tool", CATALOG["local_tools"], ids=[tool["id"] for tool in CATALOG["local_tools"]])
@pytest.mark.parametrize("step_id", ["provider_account", "provider_key", "save_connection"])
def test_local_lessons_use_server_instructions_and_actual_tool_address(tool, step_id):
    base = next(step for step in STEPS if step["id"] == step_id)
    lesson = tutorial_lesson(base, "openai_compatible", tool["id"])
    assert len(lesson["actions"]) >= 3
    assert tool["address"] in lesson["example"] and tool["name"] in lesson["example"]
    assert not any("API 키 발급 화면" in action or "API 키 붙여 넣기" in action for action in lesson["actions"])
    assert "{local_" not in json.dumps(lesson)
    assert all(lesson[name] for name in ("ready", "expected", "trouble"))
    if step_id != "provider_account":
        assert lesson["target"] == "[name=base_url]"
    assert lesson["title"] in render_tutorial_guide()

def test_visual_course_has_four_steps_and_keeps_all_reference_topics():
    quick = CATALOG["quick_start"]
    assert [step["id"] for step in quick] == ["provider_key", "save_connection", "first_path", "question"]
    assert {step["id"] for step in quick} <= {step["id"] for step in STEPS}
    lessons = [lesson for step in quick for lesson in step["lessons"]]
    assert len(lessons) == len(set(lessons))
    assert set(lessons) < {step["id"] for step in STEPS}
    assert {"provider_account", "provider_key", "save_connection", "check_connection", "first_path",
            "question", "files", "model", "performance", "search", "budget", "optional_features"} <= set(lessons)
    assert all(len(step["actions"]) <= 2 and len(step["summary"]) <= 60 for step in quick)
    assert len(CATALOG["topics"]) == 41


def test_quick_completion_does_not_require_reading_thirty_reference_articles():
    quick = [step["id"] for step in CATALOG["quick_start"]]
    progress = TutorialProgress(course_version=4, chapter_id="input", step_id="question",
                                mode="GUIDED", status="COMPLETED", completed_steps=quick)
    assert len(progress.completed_steps) == 4
    previous = TutorialProgress(course_version=3, chapter_id="results", step_id="conclusion",
                                mode="GUIDED", status="COMPLETED", completed_steps=CATALOG["legacy_quick_steps"])
    assert len(previous.completed_steps) == 5
    with pytest.raises(ValidationError):
        TutorialProgress(course_version=2, mode="GUIDED", status="COMPLETED", completed_steps=quick)


@pytest.mark.parametrize("version", [3, 4])
@pytest.mark.parametrize("value", [
    {"completed_steps": ["overview"]},
    {"completed_steps": ["provider_key", "provider_key"]},
    {"status": "COMPLETED", "completed_steps": ["provider_key"]},
    {"api_key": "qa-tutorial-canary"},
])
def test_quick_course_rejects_invalid_completion_and_secret_fields(value, version):
    with pytest.raises(ValidationError):
        TutorialProgress.model_validate({"course_version": version, **value})


def test_quick_progress_api_keeps_old_progress_readable_and_preserves_research(app):
    configure(app)
    rid = create(app)
    before = deepcopy(app.store.run(rid))
    ledger = deepcopy(app.store.ledger())
    old = TutorialProgress(mode="GUIDED", completed_steps=["overview"])
    assert app.request("POST", "/api/control/preferences", {"tutorial_progress": old.model_dump()}).status == 200
    assert preferences(app.store)["tutorial_progress"]["course_version"] == 2
    quick = TutorialProgress(course_version=3, chapter_id="connection", step_id="save_connection",
                             mode="GUIDED", completed_steps=["provider_key"])
    assert app.request("POST", "/api/control/preferences", {"tutorial_progress": quick.model_dump()}).status == 200
    assert preferences(app.store)["tutorial_progress"] == quick.model_dump()
    current = TutorialProgress(course_version=4, chapter_id="basics", step_id="first_path",
                               mode="GUIDED", completed_steps=["provider_key", "save_connection"])
    assert app.request("POST", "/api/control/preferences", {"tutorial_progress": current.model_dump()}).status == 200
    assert preferences(app.store)["tutorial_progress"] == current.model_dump()
    assert app.store.run(rid) == before
    assert app.store.ledger() == ledger
    assert app.store.db.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0


@pytest.mark.parametrize("provider", [value for value in CATALOG["providers"] if value["id"] != "openai_compatible"], ids=lambda value: value["id"])
def test_provider_issuance_is_detailed_and_synced(provider):
    assert len(provider["account"]) >= 2 and len(provider["key_steps"]) >= 5
    assert "복사" in " ".join(provider["key_steps"])
    assert provider["billing"] and provider["trouble"]
    guide = render_tutorial_guide()
    assert all(value in guide for value in provider["account"] + provider["key_steps"])
    assert provider["keys"] in guide and provider["console"] in guide
