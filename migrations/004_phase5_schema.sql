-- Migration 004: Phase 5 Schema Extensions
-- 1. Create notifications table
CREATE TABLE IF NOT EXISTS notifications (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_type VARCHAR(32) NOT NULL, -- 'TRADE_EXECUTED', 'CONTRACT_FULFILLED', 'STORAGE_OVERFLOW'
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, created_at DESC);

-- 2. Create export_contracts table ("Handelskarawanen")
CREATE TABLE IF NOT EXISTS export_contracts (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    contract_date DATE NOT NULL,
    resource_type VARCHAR(32) NOT NULL,
    title VARCHAR(128) NOT NULL,
    target_amount NUMERIC(10, 2) NOT NULL,
    reward_taler NUMERIC(12, 2) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'AVAILABLE', -- 'AVAILABLE', 'FULFILLED', 'EXPIRED'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    fulfilled_at TIMESTAMPTZ,
    CONSTRAINT uq_user_contract_daily UNIQUE (user_id, contract_date, resource_type)
);

CREATE INDEX IF NOT EXISTS idx_export_contracts_user_date ON export_contracts(user_id, contract_date, status);

-- 3. Permissions grant
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO handelsimperium_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO handelsimperium_user;
