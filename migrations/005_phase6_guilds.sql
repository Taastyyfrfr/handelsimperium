-- Migration 005: Phase 6 Schema Extensions (Merchant Guilds & Monuments)

-- 1. Create guilds table
CREATE TABLE IF NOT EXISTS guilds (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) UNIQUE NOT NULL,
    tag VARCHAR(6) UNIQUE NOT NULL,
    description TEXT,
    leader_id INT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_guilds_leader ON guilds(leader_id);

-- 2. Create guild_members table (one guild per merchant)
CREATE TABLE IF NOT EXISTS guild_members (
    id SERIAL PRIMARY KEY,
    guild_id INT NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,
    user_id INT UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role VARCHAR(16) NOT NULL DEFAULT 'MEMBER',
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_guild_members_user ON guild_members(user_id);
CREATE INDEX IF NOT EXISTS idx_guild_members_guild ON guild_members(guild_id);

-- 3. Create guild_bank table (treasury)
CREATE TABLE IF NOT EXISTS guild_bank (
    guild_id INT PRIMARY KEY REFERENCES guilds(id) ON DELETE CASCADE,
    balance NUMERIC(14, 2) NOT NULL DEFAULT 0.0
);

-- 4. Create guild_bank_inventory table (resource storage)
CREATE TABLE IF NOT EXISTS guild_bank_inventory (
    guild_id INT NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,
    resource_type VARCHAR(32) NOT NULL,
    amount NUMERIC(14, 2) NOT NULL DEFAULT 0.0,
    PRIMARY KEY (guild_id, resource_type)
);

-- 5. Create guild_projects table (Cooperative Monuments)
CREATE TABLE IF NOT EXISTS guild_projects (
    id SERIAL PRIMARY KEY,
    guild_id INT NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,
    project_type VARCHAR(32) NOT NULL,
    stage INT NOT NULL DEFAULT 1,
    target_costs JSONB NOT NULL,
    invested_resources JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_completed BOOLEAN NOT NULL DEFAULT FALSE,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_guild_project_type UNIQUE (guild_id, project_type)
);

CREATE INDEX IF NOT EXISTS idx_guild_projects_lookup ON guild_projects(guild_id, is_completed);
CREATE INDEX IF NOT EXISTS idx_guild_projects_perk ON guild_projects(guild_id, project_type, is_completed);

-- 6. Permissions grant
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO handelsimperium_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO handelsimperium_user;
