-- Handelsimperium Initial Database Schema

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    balance NUMERIC(14, 2) NOT NULL DEFAULT 1000.00 CHECK (balance >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS inventories (
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    resource_type VARCHAR(32) NOT NULL,
    amount NUMERIC(14, 2) NOT NULL DEFAULT 0.00 CHECK (amount >= 0),
    last_calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, resource_type)
);

CREATE TABLE IF NOT EXISTS buildings (
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    building_type VARCHAR(32) NOT NULL,
    level INT NOT NULL DEFAULT 1 CHECK (level >= 0),
    production_rate NUMERIC(10, 4) NOT NULL DEFAULT 1.0000,
    PRIMARY KEY (user_id, building_type)
);

CREATE TABLE IF NOT EXISTS market_orders (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    order_type VARCHAR(4) NOT NULL CHECK (order_type IN ('BUY', 'SELL')),
    resource_type VARCHAR(32) NOT NULL,
    amount NUMERIC(14, 2) NOT NULL CHECK (amount > 0),
    filled_amount NUMERIC(14, 2) NOT NULL DEFAULT 0.00 CHECK (filled_amount >= 0 AND filled_amount <= amount),
    limit_price NUMERIC(14, 2) NOT NULL CHECK (limit_price > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'FILLED', 'CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    buyer_id INT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    seller_id INT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    resource_type VARCHAR(32) NOT NULL,
    amount NUMERIC(14, 2) NOT NULL CHECK (amount > 0),
    price NUMERIC(14, 2) NOT NULL CHECK (price > 0),
    fee NUMERIC(14, 2) NOT NULL DEFAULT 0.00 CHECK (fee >= 0),
    executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Composite Index on market_orders for efficient order book queries and matching engine
CREATE INDEX IF NOT EXISTS idx_market_orders_book ON market_orders(resource_type, status, limit_price);

-- Index for querying user-specific orders
CREATE INDEX IF NOT EXISTS idx_market_orders_user ON market_orders(user_id, status);

-- Index for trade history ordered by execution time
CREATE INDEX IF NOT EXISTS idx_trades_executed_at ON trades(executed_at DESC);
