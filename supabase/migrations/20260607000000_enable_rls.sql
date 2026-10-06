-- Defense-in-depth: enable RLS (deny-all to unprivileged roles) to match
-- db/schema.sql. The server connects as the postgres role (BYPASSRLS), so this
-- only hardens against accidental exposure via a restricted role / PostgREST.
-- Idempotent; on Supabase RLS is also auto-enabled for new tables by trigger.
ALTER TABLE laws ENABLE ROW LEVEL SECURITY;
ALTER TABLE law_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE articles ENABLE ROW LEVEL SECURITY;
ALTER TABLE article_embeddings ENABLE ROW LEVEL SECURITY;
