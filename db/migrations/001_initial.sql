PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS research_runs (
 research_id TEXT PRIMARY KEY, goal TEXT NOT NULL, state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version >= 0), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
 task_id TEXT PRIMARY KEY, research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 parent_task_id TEXT REFERENCES tasks(task_id), assigned_role TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contracts (
 contract_id TEXT PRIMARY KEY, research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id), contract_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
 artifact_id TEXT PRIMARY KEY, research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 contract_id TEXT NOT NULL REFERENCES contracts(contract_id), kind TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
 experiment_id TEXT PRIMARY KEY, research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 hypothesis_ref TEXT, dataset_ref TEXT, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
 evidence_id TEXT PRIMARY KEY, research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 source_id TEXT, experiment_id TEXT REFERENCES experiments(experiment_id), payload_json TEXT NOT NULL,
 CHECK(source_id IS NOT NULL OR experiment_id IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS decisions (
 decision_id TEXT PRIMARY KEY, research_id TEXT NOT NULL REFERENCES research_runs(research_id), payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
 agent_run_id TEXT PRIMARY KEY, contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
 actor_role TEXT NOT NULL, started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_calls (
 request_id TEXT PRIMARY KEY, agent_run_id TEXT NOT NULL REFERENCES agent_runs(agent_run_id),
 tool_name TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staged_mutations (
 mutation_id TEXT PRIMARY KEY, research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 contract_id TEXT NOT NULL REFERENCES contracts(contract_id), base_state_version INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('PENDING','COMMITTED','ROLLED_BACK')),
 payload_json TEXT NOT NULL, verification_json TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, operation TEXT NOT NULL,
 before_json TEXT, after_json TEXT NOT NULL, state_version INTEGER NOT NULL, created_at TEXT NOT NULL,
 mutation_id TEXT NOT NULL UNIQUE REFERENCES staged_mutations(mutation_id)
);
CREATE TABLE IF NOT EXISTS checkpoints (
 checkpoint_id TEXT PRIMARY KEY, research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 state_version INTEGER NOT NULL, created_at TEXT NOT NULL, reason TEXT NOT NULL
);
