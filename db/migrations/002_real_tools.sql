CREATE TABLE datasets (
 dataset_id TEXT PRIMARY KEY,
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 source_type TEXT NOT NULL,
 original_name TEXT NOT NULL,
 stored_path TEXT NOT NULL,
 sha256 TEXT NOT NULL,
 size_bytes INTEGER NOT NULL,
 format TEXT NOT NULL,
 row_count INTEGER,
 column_count INTEGER,
 schema_json TEXT,
 created_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('IMPORTED','PROFILED','INVALID'))
);

ALTER TABLE artifacts ADD COLUMN producer_type TEXT;
ALTER TABLE artifacts ADD COLUMN producer_id TEXT;
ALTER TABLE artifacts ADD COLUMN artifact_type TEXT;
ALTER TABLE artifacts ADD COLUMN relative_path TEXT;
ALTER TABLE artifacts ADD COLUMN sha256 TEXT;
ALTER TABLE artifacts ADD COLUMN size_bytes INTEGER;
ALTER TABLE artifacts ADD COLUMN created_at TEXT;
ALTER TABLE artifacts ADD COLUMN status TEXT;

ALTER TABLE experiments ADD COLUMN dataset_id TEXT REFERENCES datasets(dataset_id);
ALTER TABLE experiments ADD COLUMN task_id TEXT REFERENCES tasks(task_id);
ALTER TABLE experiments ADD COLUMN method TEXT;
ALTER TABLE experiments ADD COLUMN status TEXT;
ALTER TABLE experiments ADD COLUMN result_artifact_id TEXT REFERENCES artifacts(artifact_id);
ALTER TABLE experiments ADD COLUMN created_at TEXT;

ALTER TABLE evidence ADD COLUMN claim TEXT;
ALTER TABLE evidence ADD COLUMN polarity TEXT;
ALTER TABLE evidence ADD COLUMN source_type TEXT;
ALTER TABLE evidence ADD COLUMN source_ref TEXT;
ALTER TABLE evidence ADD COLUMN status TEXT;
ALTER TABLE evidence ADD COLUMN provenance_json TEXT;

ALTER TABLE tool_calls ADD COLUMN started_at TEXT;
ALTER TABLE tool_calls ADD COLUMN finished_at TEXT;
ALTER TABLE tool_calls ADD COLUMN latency_ms REAL;
ALTER TABLE tool_calls ADD COLUMN status TEXT;
ALTER TABLE tool_calls ADD COLUMN estimated_cost_usd REAL DEFAULT 0;
ALTER TABLE tool_calls ADD COLUMN idempotency_key TEXT;
CREATE UNIQUE INDEX idx_tool_calls_idempotency ON tool_calls(idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE tool_dispatches (
 idempotency_key TEXT PRIMARY KEY,
 request_id TEXT NOT NULL UNIQUE,
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 tool_name TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('STARTED','FINISHED','FAILED')),
 created_at TEXT NOT NULL
);
