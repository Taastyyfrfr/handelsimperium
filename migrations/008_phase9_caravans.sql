-- Migration 008: Phase 9 Caravan Expeditions, Travel Durations & Regional Depots

-- 1. Create caravans table
CREATE TABLE IF NOT EXISTS caravans (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    origin_region_id INT NOT NULL REFERENCES regions(id),
    destination_region_id INT NOT NULL REFERENCES regions(id),
    cargo JSONB NOT NULL DEFAULT '{}'::jsonb,
    departure_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    arrival_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'EN_ROUTE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_caravan_status CHECK (status IN ('EN_ROUTE', 'ARRIVED', 'UNLOADED', 'CANCELLED'))
);

-- 2. Create regional_depots table
CREATE TABLE IF NOT EXISTS regional_depots (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    region_id INT NOT NULL REFERENCES regions(id),
    resource_type VARCHAR(32) NOT NULL,
    amount NUMERIC(14, 2) NOT NULL DEFAULT 0.0,
    last_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_regional_depots UNIQUE (user_id, region_id, resource_type)
);

-- 3. Create indexes
CREATE INDEX IF NOT EXISTS idx_caravans_user_status_arrival ON caravans(user_id, status, arrival_at);
CREATE INDEX IF NOT EXISTS idx_regional_depots_user_region ON regional_depots(user_id, region_id);

-- 4. Grant permissions to application user
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO handelsimperium_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO handelsimperium_user;
