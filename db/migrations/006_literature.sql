CREATE TABLE sources (
 source_id TEXT PRIMARY KEY,
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 source_type TEXT NOT NULL DEFAULT 'LITERATURE',
 title TEXT NOT NULL,
 authors_json TEXT NOT NULL,
 publication_year INTEGER,
 doi TEXT,
 openalex_id TEXT,
 source_name TEXT,
 abstract TEXT,
 url TEXT,
 is_open_access INTEGER,
 cited_by_count INTEGER,
 referenced_works_json TEXT NOT NULL DEFAULT '[]',
 related_works_json TEXT NOT NULL DEFAULT '[]',
 provider TEXT NOT NULL,
 provider_ids_json TEXT NOT NULL,
 retrieved_at TEXT NOT NULL,
 metadata_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('DISCOVERED','RELEVANT','IRRELEVANT','EVIDENCE_EXTRACTED','VERIFIED','INVALIDATED')),
 relevance_json TEXT,
 collision_warning TEXT,
 UNIQUE(research_id,doi),
 UNIQUE(research_id,openalex_id)
);
CREATE INDEX idx_sources_research_status ON sources(research_id,status);
CREATE TABLE scholarly_search_cache (
 research_id TEXT NOT NULL REFERENCES research_runs(research_id),
 cache_key TEXT NOT NULL,
 provider TEXT NOT NULL,
 request_json TEXT NOT NULL,
 result_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(research_id,cache_key)
);
CREATE TABLE literature_syntheses (
 research_id TEXT PRIMARY KEY REFERENCES research_runs(research_id),
 synthesis_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
ALTER TABLE evidence ADD COLUMN source_metadata_hash TEXT;
ALTER TABLE evidence ADD COLUMN text_field TEXT;
ALTER TABLE evidence ADD COLUMN text_hash TEXT;
ALTER TABLE evidence ADD COLUMN evidence_text TEXT;
ALTER TABLE evidence ADD COLUMN evidence_location TEXT;
ALTER TABLE evidence ADD COLUMN limitations_json TEXT;
ALTER TABLE evidence ADD COLUMN target_hypothesis_id TEXT;
ALTER TABLE evidence ADD COLUMN verification_json TEXT;
