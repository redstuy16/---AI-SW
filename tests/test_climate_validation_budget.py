"""미정산 비용을 없애지 않고 최대 예약액으로 예산을 보호한다."""
from decimal import Decimal
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qa'))
import science_validation_budget as budget_module


@pytest.mark.parametrize('status,conservative,blocked', [
    ('UNRESOLVED', False, True), ('UNRESOLVED', True, False), ('DISPATCHED', True, True)])
def test_unresolved_upper_bound_preserves_default_gate(tmp_path, monkeypatch, status, conservative, blocked):
    monkeypatch.setattr(budget_module, 'WORKSPACE_ROOT', tmp_path)
    root = tmp_path / 'runs'
    prior = root / 'prior'
    prior.mkdir(parents=True)
    db = sqlite3.connect(prior / 'state.sqlite')
    db.execute('CREATE TABLE spend_ledger(id TEXT,status TEXT,reserved INTEGER,settled INTEGER)')
    db.execute('INSERT INTO spend_ledger VALUES(?,?,?,?)', ('request-1', status, 80000, None))
    db.commit()
    gate = budget_module.LiveValidationBudget(root, root/'next', '.10', conservative_upper_bounds=conservative)
    if blocked:
        with pytest.raises(budget_module.LiveValidationBudgetError, match='NEEDS_RECONCILIATION'):
            gate.acquire()
    else:
        with gate:
            assert gate.available_usd == Decimal('.02')
            assert gate.metadata['previous_protected_usd'] == '0.08'
    assert db.execute('SELECT status,reserved,settled FROM spend_ledger').fetchone() == (status, 80000, None)
    db.close()
