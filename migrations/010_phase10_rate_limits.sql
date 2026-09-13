-- Migration 010: Database-Backed Shared Sliding-Window Rate Limiter
CREATE TABLE IF NOT EXISTS rate_limits (
    id BIGSERIAL PRIMARY KEY,
    client_key VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rate_limits_key_time ON rate_limits(client_key, created_at DESC);

GRANT ALL PRIVILEGES ON TABLE rate_limits TO handelsimperium_user;
GRANT ALL PRIVILEGES ON SEQUENCE rate_limits_id_seq TO handelsimperium_user;
