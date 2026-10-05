"""그래프의 축과 실제 점이 계산 자료에서 벗어나면 정본 반영을 거부한다."""
import asyncio
import json

import pytest
from probe.agent_runtime import RuntimeFailure

from test_agent_runtime import runtime, MANAGER, coordinator_reply, worker_reply, CSV


@pytest.mark.parametrize("field,bad", [("x", "wrong_column"), ("point_count", 999), ("data_sha256", "0" * 64)])
def test_graph_metadata_tampering_fails_independent_verification(tmp_path, field, bad):
    agent, state, db, _ = runtime(tmp_path, [MANAGER, coordinator_reply, worker_reply])
    original = state.stage
    def altered(payload):
        record = dict(db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (payload.scientific.figure_artifact_id,)).fetchone())
        row = db.execute("SELECT result_json FROM tool_calls WHERE request_id=?", (record["producer_id"],)).fetchone()
        result = json.loads(row[0])
        result["result"]["plot"][field] = bad
        value = json.dumps(result, ensure_ascii=False)
        value.encode("utf-8", errors="strict")
        db.execute("UPDATE tool_calls SET result_json=? WHERE request_id=?", (value, record["producer_id"]))
        return original(payload)
    state.stage = altered
    with pytest.raises(RuntimeFailure, match="VERIFICATION_FAILED"):
        asyncio.run(agent.run("원자료와 일치하는 상관 분석 그림을 확인합니다.", CSV))
    verification = json.loads(db.execute("SELECT verification_json FROM staged_mutations ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert any(item["check_id"] == "FIGURE_SOURCE_MATCH" and not item["passed"] for item in verification["checks"])
    assert db.execute("SELECT COUNT(*) FROM experiments WHERE status='VERIFIED'").fetchone()[0] == 0
    db.close()
