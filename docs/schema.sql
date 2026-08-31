-- DOOH Tag Verification schema.
--
-- Generated from tagverify/db/models.py. To regenerate after a model change:
--
--   python -c "from sqlalchemy.schema import CreateTable, CreateIndex; \
--     from sqlalchemy.dialects import postgresql; from tagverify.db.models import Base; \
--     [print(CreateTable(t).compile(dialect=postgresql.dialect())) for t in Base.metadata.sorted_tables]"
--
-- This file provisions a NEW database. It no longer describes the history of an existing one:
-- since api_keys.scopes there are Alembic revisions under migrations/, and an existing
-- database is brought forward with `alembic upgrade head`.
--
-- Both are kept, and they answer different questions. This says what the schema IS; the
-- revisions say how a database that predates a change gets there. A new column belongs in
-- both, or provisioning and migrating diverge and only one of them is ever exercised.

CREATE TABLE IF NOT EXISTS api_keys (
	id TEXT NOT NULL, 
	name TEXT NOT NULL, 
	key_hash TEXT NOT NULL, 
	key_prefix TEXT NOT NULL, 
	rate_limit_per_min INTEGER DEFAULT 60 NOT NULL, 
	scopes JSONB DEFAULT '[]'::jsonb NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	last_used_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS api_keys_key_hash_idx ON api_keys (key_hash);
CREATE INDEX IF NOT EXISTS api_keys_key_prefix_idx ON api_keys (key_prefix);

CREATE TABLE IF NOT EXISTS content_tags (
	slug TEXT NOT NULL, 
	label TEXT NOT NULL, 
	description TEXT NOT NULL, 
	positives JSONB NOT NULL, 
	negatives JSONB NOT NULL, 
	rationale TEXT, 
	sigmoid_floor REAL, 
	status TEXT DEFAULT 'active' NOT NULL, 
	sort_order INTEGER DEFAULT 0 NOT NULL, 
	extra JSONB DEFAULT '{}'::jsonb NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	created_by TEXT, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_by TEXT, 
	PRIMARY KEY (slug)
);
CREATE INDEX IF NOT EXISTS content_tags_status_idx ON content_tags (status);

CREATE TABLE IF NOT EXISTS eval_images (
	id TEXT NOT NULL, 
	image_hash TEXT NOT NULL, 
	storage_ref TEXT NOT NULL, 
	tag_slug TEXT NOT NULL, 
	label BOOLEAN NOT NULL, 
	note TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS eval_images_hash_tag_idx ON eval_images (image_hash, tag_slug);
CREATE INDEX IF NOT EXISTS eval_images_tag_idx ON eval_images (tag_slug);

-- The non-tag half of inference/packs.json: $comment, prompt_template, shared_distractors
-- and defaults. `body` is TEXT and not JSONB on purpose -- packs_version hashes the exported
-- file's bytes, and JSONB does not preserve key order. See tagverify/db/models.py.
-- Populated by `dooh seed-tags`, not by this file.
CREATE TABLE IF NOT EXISTS pack_header (
	id SMALLINT DEFAULT 1 NOT NULL,
	body TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_by TEXT,
	PRIMARY KEY (id),
	CONSTRAINT pack_header_singleton CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS tag_thresholds (
	slug TEXT NOT NULL, 
	threshold_low REAL DEFAULT 0.3 NOT NULL, 
	threshold_high REAL DEFAULT 0.55 NOT NULL, 
	sigmoid_floor REAL DEFAULT 0.005 NOT NULL, 
	escalate BOOLEAN DEFAULT true NOT NULL, 
	calibrated BOOLEAN DEFAULT false NOT NULL, 
	precision REAL, 
	recall REAL, 
	packs_version_seen TEXT, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_by TEXT, 
	PRIMARY KEY (slug)
);

CREATE TABLE IF NOT EXISTS usage_counters (
	api_key_id TEXT NOT NULL, 
	window_start TIMESTAMP WITH TIME ZONE NOT NULL, 
	count INTEGER DEFAULT 0 NOT NULL, 
	PRIMARY KEY (api_key_id, window_start)
);

CREATE TABLE IF NOT EXISTS analyses (
	request_id TEXT NOT NULL, 
	api_key_id TEXT, 
	image_hash TEXT NOT NULL, 
	tags_requested JSONB NOT NULL, 
	results JSONB NOT NULL, 
	packs_version TEXT, 
	latency_ms INTEGER, 
	cached BOOLEAN DEFAULT false NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (request_id), 
	FOREIGN KEY(api_key_id) REFERENCES api_keys (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS analyses_api_key_created_idx ON analyses (api_key_id, created_at);
CREATE INDEX IF NOT EXISTS analyses_image_hash_idx ON analyses (image_hash);
