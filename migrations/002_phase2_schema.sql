-- Migration 002: Phase 2 Schema Enhancements
-- 1. Add surrogate ID to buildings table if not exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'buildings' AND column_name = 'id'
    ) THEN
        ALTER TABLE buildings ADD COLUMN id SERIAL UNIQUE;
    END IF;
END $$;

-- 2. Seed baseline warehouse building for existing users
INSERT INTO buildings (user_id, building_type, level, production_rate)
SELECT u.id, 'warehouse', 1, 0.0000
FROM users u
ON CONFLICT (user_id, building_type) DO NOTHING;
