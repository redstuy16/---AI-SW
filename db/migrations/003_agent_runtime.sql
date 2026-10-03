ALTER TABLE agent_runs ADD COLUMN research_id TEXT REFERENCES research_runs(research_id);
ALTER TABLE agent_runs ADD COLUMN provider TEXT;
ALTER TABLE agent_runs ADD COLUMN model TEXT;
ALTER TABLE agent_runs ADD COLUMN finished_at TEXT;
ALTER TABLE agent_runs ADD COLUMN latency_ms REAL;
ALTER TABLE agent_runs ADD COLUMN status TEXT;
ALTER TABLE agent_runs ADD COLUMN request_count INTEGER;
ALTER TABLE agent_runs ADD COLUMN input_tokens INTEGER;
ALTER TABLE agent_runs ADD COLUMN cached_input_tokens INTEGER;
ALTER TABLE agent_runs ADD COLUMN output_tokens INTEGER;
ALTER TABLE agent_runs ADD COLUMN reasoning_tokens INTEGER;
ALTER TABLE agent_runs ADD COLUMN estimated_cost_usd REAL;
ALTER TABLE agent_runs ADD COLUMN error_code TEXT;
ALTER TABLE agent_runs ADD COLUMN base_state_version INTEGER;
ALTER TABLE agent_runs ADD COLUMN provider_run_id TEXT;

ALTER TABLE contracts ADD COLUMN status TEXT NOT NULL DEFAULT 'ISSUED';
ALTER TABLE checkpoints ADD COLUMN cursor_json TEXT;

CREATE TABLE research_budgets (
 research_id TEXT PRIMARY KEY REFERENCES research_runs(research_id),
 target_usd REAL NOT NULL CHECK(target_usd >= 0),
 soft_limit_usd REAL NOT NULL CHECK(soft_limit_usd >= 0),
 hard_limit_usd REAL NOT NULL CHECK(hard_limit_usd >= 0),
 spent_usd REAL NOT NULL DEFAULT 0 CHECK(spent_usd >= 0),
 manager_calls INTEGER NOT NULL DEFAULT 0 CHECK(manager_calls >= 0)
);

CREATE TABLE runtime_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 event_type TEXT NOT NULL,
 details_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
