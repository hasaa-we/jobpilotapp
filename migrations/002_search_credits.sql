-- AI token balance. Users are billed in tokens, not per search, so every kind of
-- usage draws from one balance. Run once in the Supabase SQL editor; safe to re-run.

-- Purchased tokens. Never expire, never reset.
ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_tokens BIGINT DEFAULT 0;

-- The free allowance is granted once per account and never refilled. Tracked apart
-- from purchases so it can never be confused with something a user paid for.
ALTER TABLE users ADD COLUMN IF NOT EXISTS free_tokens_used BIGINT DEFAULT 0;

UPDATE users SET ai_tokens = 0 WHERE ai_tokens IS NULL;
UPDATE users SET free_tokens_used = 0 WHERE free_tokens_used IS NULL;

-- Every purchase, as an audit trail. Stripe retries webhooks, so the unique event
-- id is what stops one payment being credited twice.
CREATE TABLE IF NOT EXISTS credit_purchases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    stripe_event_id TEXT UNIQUE NOT NULL,
    credits BIGINT NOT NULL,
    amount_cents INTEGER,
    currency TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now())
);

CREATE INDEX IF NOT EXISTS idx_credit_purchases_user_id ON credit_purchases(user_id);
