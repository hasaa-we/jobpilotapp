-- ============================================
-- JobPilot Database Schema
-- ============================================

-- 1. Users Table
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    full_name TEXT,
    plan TEXT DEFAULT 'free' CHECK (plan IN ('free', 'pro', 'premium')),
    resume_text TEXT,
    target_role TEXT,
    target_industry TEXT,
    location TEXT,
    skills TEXT,
    experience_level TEXT CHECK (experience_level IN ('entry', 'mid', 'senior', 'lead', 'executive')),
    onboarding_complete BOOLEAN DEFAULT FALSE,
    free_resume_used BOOLEAN DEFAULT FALSE,
    free_interview_used BOOLEAN DEFAULT FALSE,
    free_apps_count INTEGER DEFAULT 0,
    gmail_token TEXT,
    last_email_sync TIMESTAMP WITH TIME ZONE,
    job_alerts_enabled BOOLEAN DEFAULT FALSE,
    last_job_alert TIMESTAMP WITH TIME ZONE,
    linkedin_profile_url TEXT,
    -- Private token in the user's forwarding address (u<token>@inbox.<domain>).
    -- Mail sent there is matched back to this user, so treat it as a secret.
    inbox_token TEXT UNIQUE,
    -- Purchased AI tokens. Never expire and are never reset.
    ai_tokens BIGINT DEFAULT 0,
    -- Free monthly allowance, tracked apart from purchases so a reset can never
    -- wipe something paid for. free_period is 'YYYY-MM'; a different month means
    -- the allowance has already rolled over, so no scheduled job is needed.
    free_tokens_used BIGINT DEFAULT 0,
    free_period TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now())
);

-- 2. Applications Table
CREATE TABLE applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT DEFAULT 'applied' CHECK (status IN ('applied', 'responded', 'interview', 'offer', 'rejected', 'ghosted')),
    job_url TEXT,
    job_description TEXT,
    salary_range TEXT,
    notes TEXT,
    applied_date TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()),
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()),
    follow_up_sent BOOLEAN DEFAULT FALSE,
    follow_up_date TIMESTAMP WITH TIME ZONE
);

-- 3. Generated Resumes & Cover Letters
CREATE TABLE generated_docs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    application_id UUID REFERENCES applications(id) ON DELETE SET NULL,
    doc_type TEXT CHECK (doc_type IN ('resume', 'cover_letter', 'follow_up')),
    original_text TEXT,
    generated_text TEXT,
    match_score INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now())
);

-- 4. Interview Prep Sessions
CREATE TABLE interview_preps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    application_id UUID REFERENCES applications(id) ON DELETE SET NULL,
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    company_research TEXT,
    questions_and_answers TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now())
);

CREATE TABLE gmail_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    email_address TEXT NOT NULL,
    encrypted_token TEXT NOT NULL,
    last_sync TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()),
    UNIQUE(user_id, email_address)
);

-- Indexes for fast lookups
CREATE INDEX idx_applications_user_id ON applications(user_id);
CREATE INDEX idx_applications_status ON applications(status);
CREATE INDEX idx_applications_company ON applications(company);
CREATE INDEX idx_generated_docs_user_id ON generated_docs(user_id);
CREATE INDEX idx_interview_preps_user_id ON interview_preps(user_id);
CREATE INDEX idx_gmail_accounts_user_id ON gmail_accounts(user_id);
