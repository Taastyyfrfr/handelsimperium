-- Migration 006: Phase 7 Schema Extensions (Modular Onboarding Tutorial)

-- 1. Create user_tutorials table
CREATE TABLE IF NOT EXISTS user_tutorials (
    user_id INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    current_step INT NOT NULL DEFAULT 1,
    completed_steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_finished BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_tutorials_lookup ON user_tutorials(user_id, is_finished);

-- 2. Permissions grant
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO handelsimperium_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO handelsimperium_user;
