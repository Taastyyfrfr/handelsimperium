-- Migration 011: Phase 10 Economic Integrity & Return Expeditions
-- 1. Ensure composite index on rate_limits for rolling maintenance
CREATE INDEX IF NOT EXISTS idx_rate_limits_key_created ON rate_limits (client_key, created_at);

-- 2. Ensure end_time column on kontor_auctions and sync with epoch_end_at
ALTER TABLE kontor_auctions ADD COLUMN IF NOT EXISTS end_time TIMESTAMPTZ;
UPDATE kontor_auctions SET end_time = epoch_end_at WHERE end_time IS NULL;
ALTER TABLE kontor_auctions ALTER COLUMN end_time SET DEFAULT (NOW() + INTERVAL '7 days');

-- 3. Composite index on kontor_auctions for end_time and status
CREATE INDEX IF NOT EXISTS idx_kontor_auctions_end_time_status ON kontor_auctions (end_time, status);
