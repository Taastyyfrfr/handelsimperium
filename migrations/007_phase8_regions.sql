-- Migration 007: Phase 8 Regional Specialization & Asymmetric Resource Scarcity

-- 1. Create regions table
CREATE TABLE IF NOT EXISTS regions (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) UNIQUE NOT NULL,
    tag VARCHAR(4) UNIQUE NOT NULL,
    description TEXT NOT NULL,
    coord_x INT NOT NULL,
    coord_y INT NOT NULL,
    resource_multipliers JSONB NOT NULL
);

-- 2. Seed 4 Historical Hanseatic Trade Regions
INSERT INTO regions (name, tag, description, coord_x, coord_y, resource_multipliers)
VALUES
    (
        'Ostseeküste (Danzig)',
        'DANZ',
        'Kornkammer und Holzreichtum der Hanse. Eisen und Tuch müssen über See importiert werden.',
        250,
        120,
        '{"wood": 1.5, "grain": 1.5, "stone": 0.8, "iron": 0.0, "cloth": 0.0}'::jsonb
    ),
    (
        'Skandinavien (Visby)',
        'VISB',
        'Nordische Bergwerke und Steinbrüche. Nahrungsmittel und Gewebe sind Mangelware.',
        280,
        50,
        '{"iron": 1.6, "stone": 1.4, "wood": 1.0, "grain": 0.0, "cloth": 0.0}'::jsonb
    ),
    (
        'Flandern (Brügge)',
        'BRUG',
        'Welthandelsplatz und Tuchmacherei. Steine und Erze existieren im Tiefland nicht.',
        60,
        200,
        '{"cloth": 1.8, "grain": 1.0, "wood": 0.6, "stone": 0.0, "iron": 0.0}'::jsonb
    ),
    (
        'Rheinland (Köln)',
        'KOEL',
        'Kathedralenbau und Rheinhandel. Bauholz und Weizenfelder sind rar gesät.',
        110,
        220,
        '{"stone": 1.5, "cloth": 1.2, "iron": 0.8, "wood": 0.0, "grain": 0.0}'::jsonb
    )
ON CONFLICT (tag) DO UPDATE SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    coord_x = EXCLUDED.coord_x,
    coord_y = EXCLUDED.coord_y,
    resource_multipliers = EXCLUDED.resource_multipliers;

-- 3. Add region_id to users table
ALTER TABLE users ADD COLUMN IF NOT EXISTS region_id INT REFERENCES regions(id);

-- 4. Fallback assignment for existing users
UPDATE users
SET region_id = (SELECT id FROM regions WHERE tag = 'DANZ')
WHERE region_id IS NULL;

-- 5. Indexing and permissions
CREATE INDEX IF NOT EXISTS idx_users_region_id ON users(region_id);
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO handelsimperium_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO handelsimperium_user;
