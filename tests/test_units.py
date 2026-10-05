import json
import sqlite3

import pytest
from pydantic import ValidationError

from probe.generate_schemas import MODELS, generate
from probe.schemas import ContextRef, ResearchContract, Verdict
from probe.service import InvalidStateTransitionError


def test_invalid_contract_rejected():
    with pytest.raises(ValidationError):
        ResearchContract(contract_id="C", research_id="R", task_type="t", issued_by="m",
                         assigned_role="w", objective="", output_schema_id="out")


def test_malformed_context_ref_rejected():
    with pytest.raises(ValidationError):
        ContextRef(type="unknown", id="x")
    with pytest.raises(ValidationError):
        ContextRef(type="research", id="")


def test_checkpoint_and_rollback(cycle):
    db, state, research_id, _, payload = cycle
    checkpoint_id = state.checkpoint(research_id, "before analysis")
    row = db.execute("SELECT * FROM checkpoints WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
    assert row["state_version"] == 0
    mutation_id = state.stage(payload)
    state.rollback(mutation_id)
    assert state.mutation_status(mutation_id) == "ROLLED_BACK"
    assert state.verify(mutation_id).verdict == Verdict.FAIL
    with pytest.raises(InvalidStateTransitionError):
        state.commit(mutation_id)
    assert state.state_version(research_id) == 0


def test_foreign_keys_enabled(cycle):
    db, *_ = cycle
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO tasks VALUES ('TASK-x','R-missing',NULL,'role','ISSUED')")


def test_generated_schema_is_reproducible(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    generate(first)
    generate(second)
    assert len(list(first.glob("*.json"))) == len(MODELS)
    for name, model in MODELS.items():
        filename = f"{name}.schema.json"
        assert (first / filename).read_bytes() == (second / filename).read_bytes()
        assert json.loads((first / filename).read_text(encoding="utf-8")) == model.model_json_schema()
