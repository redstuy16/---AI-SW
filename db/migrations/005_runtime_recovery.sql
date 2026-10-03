ALTER TABLE research_actions ADD COLUMN action_index INTEGER;
ALTER TABLE research_actions ADD COLUMN logical_key TEXT;
ALTER TABLE research_actions ADD COLUMN status TEXT NOT NULL DEFAULT 'COMPLETED';
ALTER TABLE research_actions ADD COLUMN task_id TEXT;
ALTER TABLE research_actions ADD COLUMN contract_id TEXT;
ALTER TABLE research_actions ADD COLUMN input_refs_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE research_actions ADD COLUMN output_refs_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE research_actions ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1;
ALTER TABLE research_actions ADD COLUMN started_at TEXT;
ALTER TABLE research_actions ADD COLUMN finished_at TEXT;
ALTER TABLE research_actions ADD COLUMN error_code TEXT;
CREATE UNIQUE INDEX idx_research_action_key ON research_actions(research_id,logical_key);
CREATE UNIQUE INDEX idx_research_action_index ON research_actions(research_id,action_index);

ALTER TABLE contracts ADD COLUMN runtime_key TEXT;
CREATE UNIQUE INDEX idx_contract_runtime_key ON contracts(research_id,runtime_key);
CREATE UNIQUE INDEX idx_critic_experiment ON critic_reviews(research_id,experiment_id);

CREATE TABLE research_runtime_state (
 research_id TEXT PRIMARY KEY REFERENCES research_runs(research_id),
 cursor_json TEXT NOT NULL,
 base_state_version INTEGER NOT NULL,
 last_event_seq INTEGER NOT NULL,
 last_planning_seq INTEGER NOT NULL,
 updated_at TEXT NOT NULL
);

CREATE TABLE runtime_steps (
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 step_key TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','WAITING_RETRY','WAITING_ESCALATION','COMPLETED','FAILED','CANCELLED')),
 output_json TEXT,
 contract_id TEXT REFERENCES contracts(contract_id),
 attempt INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(research_id,step_key)
);
