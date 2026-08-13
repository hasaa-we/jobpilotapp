-- CRITICAL. Without this, no Stars purchase can ever be credited.
--
-- credit_purchases has row-level security enabled but no INSERT policy, so every
-- insert fails with "new row violates row-level security policy" (42501). A real
-- customer paid 150 Stars on 2026-08-13 and received nothing because of this.
--
-- The table is written only by this server (never by a browser), and the key the
-- server uses already writes to `users`, so an insert policy here matches the
-- security posture the rest of the schema already has.
--
-- Run once in the Supabase SQL editor. Safe to re-run.

ALTER TABLE credit_purchases ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "server records purchases" ON credit_purchases;
CREATE POLICY "server records purchases"
    ON credit_purchases
    FOR INSERT
    WITH CHECK (true);

DROP POLICY IF EXISTS "server reads purchases" ON credit_purchases;
CREATE POLICY "server reads purchases"
    ON credit_purchases
    FOR SELECT
    USING (true);

-- Verify: this must return two rows.
-- SELECT policyname, cmd FROM pg_policies WHERE tablename = 'credit_purchases';
