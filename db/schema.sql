-- Ukraine Laws MCP — Postgres schema (MVP-0)
-- Idempotent: safe to run repeatedly. Applied by PostgresRepository.ensure_schema()
-- and mirrored by the first Alembic migration.
--
-- Backend: Supabase (managed Postgres + pgvector) in prod, pgvector/pgvector:pg16 locally.

-- Extensions ---------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Ukrainian full-text configuration ---------------------------------------
-- Postgres ships no Ukrainian dictionary, so we build a 'ukrainian' config on
-- top of 'simple' + 'unaccent' (lowercasing + tokenizing, no stemming). The
-- recall lost to the missing stemmer is recovered by the vector channel
-- (hybrid search) added in MVP-1.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_ts_config WHERE cfgname = 'ukrainian'
    ) THEN
        CREATE TEXT SEARCH CONFIGURATION ukrainian (COPY = simple);
        ALTER TEXT SEARCH CONFIGURATION ukrainian
            ALTER MAPPING FOR hword, hword_part, word
            WITH unaccent, simple;
    END IF;
END
$$;

-- Catalog of laws ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS laws (
    law_id          text PRIMARY KEY,
    title           text NOT NULL,
    url             text NOT NULL DEFAULT '',
    print_url       text,
    category        text,
    volatile        boolean NOT NULL DEFAULT false,
    cache_ttl_days  integer NOT NULL DEFAULT 30,
    accepted_at     date,
    source          text NOT NULL DEFAULT 'zakon_rada',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Redactions / revisions of each law --------------------------------------
CREATE TABLE IF NOT EXISTS law_versions (
    id              bigserial PRIMARY KEY,
    law_id          text NOT NULL REFERENCES laws(law_id) ON DELETE CASCADE,
    amendment_date  date,
    effective_from  date,
    effective_to    date,
    status          text NOT NULL DEFAULT 'current',  -- current | superseded | repealed
    content_hash    text NOT NULL,
    full_text       text NOT NULL,
    char_count      integer NOT NULL DEFAULT 0,
    source          text,
    source_policy   text,
    adapter         text NOT NULL DEFAULT 'zakon_rada',
    retrieved_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT law_versions_dedup UNIQUE (law_id, content_hash)
);

-- At most one 'current' (чинна) redaction per law.
CREATE UNIQUE INDEX IF NOT EXISTS law_versions_one_current
    ON law_versions (law_id)
    WHERE status = 'current';

CREATE INDEX IF NOT EXISTS law_versions_law_id ON law_versions (law_id);

-- Articles of a redaction --------------------------------------------------
CREATE TABLE IF NOT EXISTS articles (
    id          bigserial PRIMARY KEY,
    version_id  bigint NOT NULL REFERENCES law_versions(id) ON DELETE CASCADE,
    law_id      text NOT NULL,                -- denormalized for fast filtering
    article_num text NOT NULL,               -- '5', '5-1' (matches SearchEngine keys)
    ordinal     integer NOT NULL DEFAULT 0,
    body        text NOT NULL,
    tsv         tsvector GENERATED ALWAYS AS (to_tsvector('ukrainian', body)) STORED,
    CONSTRAINT articles_unique UNIQUE (version_id, article_num)
);

CREATE INDEX IF NOT EXISTS articles_tsv_gin ON articles USING gin (tsv);
CREATE INDEX IF NOT EXISTS articles_law_id ON articles (law_id);

-- Per-article embeddings (populated from MVP-1; dimension pinned to the
-- active model — 768 (gemini-embedding-001 default; gemini-embedding-2 / EmbeddingGemma also 768).
CREATE TABLE IF NOT EXISTS article_embeddings (
    article_id  bigint PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    embedding   vector(768) NOT NULL,
    model       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS article_emb_hnsw
    ON article_embeddings USING hnsw (embedding vector_cosine_ops);

-- Defense-in-depth -----------------------------------------------------------
-- Enable RLS (deny-all to unprivileged roles). The server connects as the
-- table owner / postgres role, which BYPASSRLS, so reads/writes are unaffected;
-- this only hardens against accidental exposure via a restricted role or the
-- Supabase PostgREST API (the corpus is served exclusively through the MCP
-- server). ENABLE ROW LEVEL SECURITY is idempotent.
ALTER TABLE laws ENABLE ROW LEVEL SECURITY;
ALTER TABLE law_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE articles ENABLE ROW LEVEL SECURITY;
ALTER TABLE article_embeddings ENABLE ROW LEVEL SECURITY;
