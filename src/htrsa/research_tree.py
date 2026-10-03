"""저장된 연구 그래프의 읽기 전용 투영."""
from __future__ import annotations

from .service import StateService


def build_research_tree(state: StateService, research_id: str) -> dict:
    version = state.state_version(research_id)
    hypotheses = []
    for hypothesis in state._db.execute(
        "SELECT hypothesis_id,statement,status FROM hypotheses WHERE research_id=? ORDER BY rowid",
        (research_id,)):
        evidence = []
        for link in state._db.execute(
            "SELECT to_id FROM entity_edges WHERE research_id=? AND from_type='hypothesis' AND from_id=? AND edge_type='supports' AND to_type='evidence' ORDER BY rowid",
            (research_id, hypothesis["hypothesis_id"])):
            row = state._db.execute(
                "SELECT evidence_id,experiment_id,claim,status,source_ref FROM evidence WHERE evidence_id=? AND research_id=?",
                (link["to_id"], research_id)).fetchone()
            if row is None:
                continue
            experiment = state._db.execute(
                "SELECT experiment_id,dataset_id,method,status,result_artifact_id FROM experiments WHERE experiment_id=? AND research_id=?",
                (row["experiment_id"], research_id)).fetchone()
            evidence.append({"ref_type": "evidence", "ref_id": row["evidence_id"],
                             "status": row["status"], "claim": row["claim"],
                             "source_ref": row["source_ref"],
                             "experiment": {"ref_type": "experiment", "ref_id": experiment["experiment_id"],
                                            "status": experiment["status"], "dataset_id": experiment["dataset_id"],
                                            "method": experiment["method"],
                                            "result_artifact_id": experiment["result_artifact_id"]}
                             if experiment else None})
        hypotheses.append({"ref_type": "hypothesis", "ref_id": hypothesis["hypothesis_id"],
                           "status": hypothesis["status"], "statement": hypothesis["statement"],
                           "evidence": evidence})
    nodes = [{"id": research_id, "type": "research", "label": "Research",
              "status": state._one("SELECT run_status FROM research_runs WHERE research_id=?", (research_id,))["run_status"],
              "parent_id": None}]
    for item in hypotheses:
        nodes.append({"id": item["ref_id"], "type": "hypothesis", "label": item["statement"],
                      "status": item["status"], "parent_id": research_id})
        for evidence_item in item["evidence"]:
            nodes.append({"id": evidence_item["ref_id"], "type": "evidence",
                          "label": evidence_item["claim"], "status": evidence_item["status"],
                          "parent_id": item["ref_id"]})
            experiment = evidence_item.get("experiment")
            if experiment:
                nodes.append({"id": experiment["ref_id"], "type": "experiment",
                              "label": experiment["method"] or experiment["ref_id"],
                              "status": experiment["status"], "parent_id": item["ref_id"]})
    nodes.append({"id": f"{research_id}:conclusion", "type": "conclusion", "label": "Final Conclusion",
                  "status": state._one("SELECT run_status FROM research_runs WHERE research_id=?", (research_id,))["run_status"],
                  "parent_id": research_id})
    edges = [dict(row) for row in state._db.execute(
        "SELECT from_type,from_id,edge_type,to_type,to_id,status FROM entity_edges WHERE research_id=? ORDER BY rowid",
        (research_id,))]
    return {"research_id": research_id, "state_version": version, "hypotheses": hypotheses,
            "root_id": research_id, "nodes": nodes, "edges": edges}
