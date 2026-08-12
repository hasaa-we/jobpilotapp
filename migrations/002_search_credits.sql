-- Search credits. One credit = one job search.
-- Run once in the Supabase SQL editor; safe to re-run.
-- schema.sql carries the same columns for fresh installs.

-- Purchased credits. Never expire, never reset.
ALTER TABLE users ADD COLUMN IF NOT EXISTS search_credits INTEGER DEFAULT 0;

-- The free monthly allowance is tracked separately from purchased credits, so a
-- monthly reset can never wipe something a user paid for.
ALTER TABLE users ADD COLUMN IF NOT EXISTS free_searches_used INTEGER DEFAULT 0;
-- Which month those free searches belong to, as 'YYYY-MM'. When the stored month
-- differs from today's, the allowance has rolled over. Storing the period rather
-- than a "reset date" means no scheduled job is needed — the rollover happens on
-- the next search, whenever that is.
ALTER TABLE users ADD COLUMN IF NOT EXISTS free_period TEXT;

UPDATE users SET search_credits = 0 WHERE search_credits IS NULL;
UPDATE users SET free_searches_used = 0 WHERE free_searches_used IS NULL;

-- Every purchase, kept as an audit trail. Stripe retries webhooks, so the unique
-- event id is what stops one payment being credited twice.
CREATE TABLE IF NOT EXISTS credit_purchases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    stripe_event_id TEXT UNIQUE NOT NULL,
    credits INTEGER NOT NULL,
    amount_cents INTEGER,
    currency TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_credit_purchases_user_id ON credit_purchases(user_id);
