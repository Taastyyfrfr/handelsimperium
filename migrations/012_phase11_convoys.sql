-- Migration 012: Phase 11 Autonomous Hanseatic NPC Trade Convoys
CREATE TABLE IF NOT EXISTS npc_convoys (
    id SERIAL PRIMARY KEY,
    convoy_name VARCHAR(64) NOT NULL,
    origin_region_id INT NOT NULL REFERENCES regions(id),
    destination_region_id INT NOT NULL REFERENCES regions(id),
    resource_type VARCHAR(32) NOT NULL,
    cargo_amount NUMERIC(12, 2) NOT NULL,
    departure_at TIMESTAMPTZ NOT NULL,
    arrival_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'IN_TRANSIT',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_npc_convoy_status CHECK (status IN ('IN_TRANSIT', 'LIQUIDATED'))
);

CREATE INDEX IF NOT EXISTS idx_npc_convoys_status_arrival ON npc_convoys(status, arrival_at);
