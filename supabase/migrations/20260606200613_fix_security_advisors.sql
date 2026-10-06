-- Revoke public access to the Supabase-managed rls_auto_enable() SECURITY DEFINER
-- function. Our tables are accessed only via the postgres role (bypasses RLS),
-- so no anon/authenticated policies are needed — this is intentional. Mirrors
-- the migration applied to the production project (kept in VCS for reproducibility).
REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM anon;
REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM authenticated;
