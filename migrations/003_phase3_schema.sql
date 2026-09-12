-- Migration 003: Phase 3 Schema Enhancements
-- 1. Add last_active_at to users table if not exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'users' AND column_name = 'last_active_at'
    ) THEN
        ALTER TABLE users ADD COLUMN last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
    END IF;
END $$;

-- 2. Update default balance for new accounts to 200.00
ALTER TABLE users ALTER COLUMN balance SET DEFAULT 200.00;

-- 3. Create user_catchups table for offline summary modals
CREATE TABLE IF NOT EXISTS user_catchups (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    offline_seconds NUMERIC(10, 1) NOT NULL,
    production_delta JSONB NOT NULL,
    trade_delta JSONB NOT NULL,
    dismissed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_catchups_lookup ON user_catchups(user_id, dismissed);

-- 4. Create composite index on trades for fast VWAP and recent trade discovery
CREATE INDEX IF NOT EXISTS idx_trades_resource_time ON trades(resource_type, executed_at DESC);

-- 5. Grant permissions to application role
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO handelsimperium_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO handelsimperium_user;

