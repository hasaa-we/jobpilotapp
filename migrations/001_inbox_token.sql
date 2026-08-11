-- Adds the per-user forwarding token used by the email-forwarding inbox.
-- Run this once in the Supabase SQL editor on an existing database.
-- schema.sql already includes the column for fresh installs.

ALTER TABLE users ADD COLUMN IF NOT EXISTS inbox_token TEXT;

-- Lookups go address -> user on every inbound email, and two users must never
-- share a token or one person's applications would land in another's account.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_inbox_token
    ON users(inbox_token)
    WHERE inbox_token IS NOT NULL;
