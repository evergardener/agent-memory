CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS evidence;
CREATE SCHEMA IF NOT EXISTS memory;
CREATE SCHEMA IF NOT EXISTS retrieval;
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE core.namespaces (
  id uuid PRIMARY KEY,
  stable_key text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE evidence.events (
  id uuid PRIMARY KEY,
  namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
  source_profile text NOT NULL,
  session_id text NOT NULL,
  turn_id text NOT NULL,
  event_type text NOT NULL,
  redacted_content text NOT NULL,
  payload_hash text NOT NULL,
  ingest_key text NOT NULL UNIQUE,
  occurred_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memory.entities (
  id uuid PRIMARY KEY,
  namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
  canonical_name text NOT NULL,
  normalized_name text NOT NULL,
  UNIQUE(namespace_id, normalized_name)
);

CREATE TABLE memory.facts (
  id uuid PRIMARY KEY,
  namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
  evidence_id uuid NOT NULL REFERENCES evidence.events(id),
  statement text NOT NULL,
  memory_state text NOT NULL DEFAULT 'candidate',
  source_profile text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(evidence_id, statement)
);

CREATE TABLE memory.fact_entities (
  fact_id uuid NOT NULL REFERENCES memory.facts(id) ON DELETE CASCADE,
  entity_id uuid NOT NULL REFERENCES memory.entities(id) ON DELETE CASCADE,
  PRIMARY KEY(fact_id, entity_id)
);

CREATE TABLE retrieval.documents (
  id uuid PRIMARY KEY,
  namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
  fact_id uuid NOT NULL UNIQUE REFERENCES memory.facts(id) ON DELETE CASCADE,
  text_redacted text NOT NULL,
  embedding vector(8) NOT NULL,
  search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', text_redacted)) STORED
);
CREATE INDEX retrieval_documents_fts ON retrieval.documents USING gin(search_vector);

CREATE TABLE ops.jobs (
  id uuid PRIMARY KEY,
  namespace_id uuid NOT NULL REFERENCES core.namespaces(id),
  evidence_id uuid NOT NULL REFERENCES evidence.events(id),
  kind text NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  status text NOT NULL DEFAULT 'pending',
  lease_until timestamptz,
  attempt_count integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
