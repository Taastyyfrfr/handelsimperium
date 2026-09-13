-- Migration 009: Phase 10 Dynamic Price Bands, Guild War Chest & Regional Kontor Auctions

-- 1. Create regional_controllers table
CREATE TABLE IF NOT EXISTS regional_controllers (
    region_id INT PRIMARY KEY REFERENCES regions(id) ON DELETE CASCADE,
    guild_id INT REFERENCES guilds(id) ON DELETE SET NULL,
    winning_bid NUMERIC(14, 2) NOT NULL DEFAULT 0.0,
    valid_until TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_regional_controllers_guild ON regional_controllers(guild_id, valid_until);

-- 2. Create kontor_auctions table
CREATE TABLE IF NOT EXISTS kontor_auctions (
    id SERIAL PRIMARY KEY,
    region_id INT NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
    current_highest_bid NUMERIC(14, 2) NOT NULL DEFAULT 0.0,
    highest_bidder_guild_id INT REFERENCES guilds(id) ON DELETE SET NULL,
    epoch_end_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_auction_status CHECK (status IN ('ACTIVE', 'RESOLVED'))
);

CREATE INDEX IF NOT EXISTS idx_kontor_auctions_status ON kontor_auctions(region_id, status, epoch_end_at);

-- 3. Create guild_contributions table (tracks member contributions to bank, monuments, and auctions)
CREATE TABLE IF NOT EXISTS guild_contributions (
    id SERIAL PRIMARY KEY,
    guild_id INT NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    contribution_type VARCHAR(32) NOT NULL, -- 'BANK_DEPOSIT', 'MONUMENT_PROJECT', 'AUCTION_BID'
    resource_type VARCHAR(32) NOT NULL,
    amount NUMERIC(14, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_guild_contributions_user ON guild_contributions(user_id);
CREATE INDEX IF NOT EXISTS idx_guild_contributions_guild ON guild_contributions(guild_id);

-- 4. Seed initial active auctions for each Hanseatic region if none exist
INSERT INTO kontor_auctions (region_id, current_highest_bid, highest_bidder_guild_id, epoch_end_at, status)
SELECT id, 0.0, NULL, NOW() + INTERVAL '7 days', 'ACTIVE'
FROM regions r
WHERE NOT EXISTS (
    SELECT 1 FROM kontor_auctions ka WHERE ka.region_id = r.id AND ka.status = 'ACTIVE'
);

-- 5. Permissions
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO handelsimperium_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO handelsimperium_user;
