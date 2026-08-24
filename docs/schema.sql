-- DOOH Tag Verification schema.
--
-- Generated from dooh/db/models.py. To regenerate after a model change:
--
--   python -c "from sqlalchemy.schema import CreateTable, CreateIndex; \
--     from sqlalchemy.dialects import postgresql; from dooh.db.models import Base; \
--     [print(CreateTable(t).compile(dialect=postgresql.dialect())) for t in Base.metadata.sorted_tables]"
--
-- This matches the tables the previous Drizzle implementation created, column for column, so
-- the existing database needs NO migration. This file is for provisioning a new one.

CREATE TABLE IF NOT EXISTS api_keys (
	id TEXT NOT NULL, 
	name TEXT NOT NULL, 
	key_hash TEXT NOT NULL, 
	key_prefix TEXT NOT NULL, 
	rate_limit_per_min INTEGER DEFAULT 60 NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	last_used_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS api_keys_key_hash_idx ON api_keys (key_hash);
CREATE INDEX IF NOT EXISTS api_keys_key_prefix_idx ON api_keys (key_prefix);

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
