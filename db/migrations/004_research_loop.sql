ALTER TABLE research_runs ADD COLUMN research_question TEXT;
ALTER TABLE research_runs ADD COLUMN run_status TEXT NOT NULL DEFAULT 'ACTIVE';
ALTER TABLE research_runs ADD COLUMN stop_reason TEXT;
ALTER TABLE research_runs ADD COLUMN conclusion_json TEXT;

ALTER TABLE decisions ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE';
ALTER TABLE decisions ADD COLUMN created_at TEXT;
ALTER TABLE decisions ADD COLUMN state_version INTEGER;
ALTER TABLE decisions ADD COLUMN visibility TEXT NOT NULL DEFAULT 'all';

ALTER TABLE evidence ADD COLUMN quality_flags_json TEXT;
ALTER TABLE evidence ADD COLUMN confidence REAL;
ALTER TABLE evidence ADD COLUMN visibility TEXT NOT NULL DEFAULT 'all';

ALTER TABLE research_budgets ADD COLUMN unknown_price_calls INTEGER NOT NULL DEFAULT 0;
ALTER TABLE research_budgets ADD COLUMN unknown_price_input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE research_budgets ADD COLUMN unknown_price_output_tokens INTEGER NOT NULL DEFAULT 0;

CREATE TABLE hypotheses (
 hypothesis_id TEXT PRIMARY KEY,
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 parent_hypothesis_id TEXT REFERENCES hypotheses(hypothesis_id),
 statement TEXT NOT NULL,
 rationale TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('PROPOSED','SHORTLISTED','ACTIVE','SUPPORTED','WEAKENED','REJECTED','INCONCLUSIVE','INVALIDATED')),
 score_json TEXT NOT NULL,
 created_by TEXT NOT NULL,
 visibility TEXT NOT NULL DEFAULT 'all',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE entity_edges (
 edge_id TEXT PRIMARY KEY,
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 from_type TEXT NOT NULL,
 from_id TEXT NOT NULL,
 edge_type TEXT NOT NULL CHECK(edge_type IN ('depends_on','supports','contradicts','generated_from','uses_dataset','produced_by','supersedes','invalidates','tests')),
 to_type TEXT NOT NULL,
 to_id TEXT NOT NULL,
 created_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'ACTIVE',
 UNIQUE(research_id,from_type,from_id,edge_type,to_type,to_id)
);
CREATE INDEX idx_edges_from ON entity_edges(research_id,from_type,from_id);
CREATE INDEX idx_edges_to ON entity_edges(research_id,to_type,to_id);

CREATE TABLE research_actions (
 action_id TEXT PRIMARY KEY,
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 action_type TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 details_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX idx_actions_fingerprint ON research_actions(research_id,fingerprint);

CREATE TABLE context_metrics (
 bundle_id TEXT PRIMARY KEY,
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 target_role TEXT NOT NULL,
 estimated_tokens INTEGER NOT NULL,
 mandatory_count INTEGER NOT NULL,
 dependency_count INTEGER NOT NULL,
 semantic_count INTEGER NOT NULL,
 recent_event_count INTEGER NOT NULL,
 invalidated_warning_count INTEGER NOT NULL,
 state_version INTEGER NOT NULL,
 estimator TEXT NOT NULL,
 created_at TEXT NOT NULL
);

CREATE TABLE planning_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 event_type TEXT NOT NULL,
 entity_type TEXT NOT NULL,
 entity_id TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 state_version INTEGER NOT NULL,
 created_at TEXT NOT NULL
);

CREATE TABLE critic_reviews (
 review_id TEXT PRIMARY KEY,
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
 contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
 output_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);

CREATE TABLE milestone_summaries (
 summary_id TEXT PRIMARY KEY,
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 kind TEXT NOT NULL,
 summary_text TEXT NOT NULL,
 covered_refs_json TEXT NOT NULL,
 state_version INTEGER NOT NULL,
 generated_by TEXT NOT NULL,
 created_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'ACTIVE'
);
