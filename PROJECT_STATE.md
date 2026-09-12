# Project State: Handelsimperium

**Generated:** 2026-09-12T22:05:00+02:00  
**Repository Branch:** `master`  
**Current Phase:** Phase 5 (Export Contracts, Notifications, Telemetry, and State Persistence)  
**Production Host:** `80.158.79.44` (`ssh server`)  
**Public Endpoint:** [http://80.158.79.44/](http://80.158.79.44/)

---

## 1. System Architecture

| Tier | Technology | Specification / Configuration |
| :--- | :--- | :--- |
| **Operating System** | Ubuntu 24.04 LTS | Linux 6.8.0-124-generic (`ecs-server` / `80.158.79.44`) |
| **Reverse Proxy** | Caddy v2 | Listens `:80`, reverse-proxies to `127.0.0.1:8000`, serves `/static/*` |
| **Application Server** | Uvicorn + FastAPI | 2 workers on `127.0.0.1:8000`, managed by systemd (`handelsimperium.service`) |
| **Runtime Environment** | Python 3.12 | Virtualenv at `/opt/handelsimperium/.venv` |
| **Database** | PostgreSQL 16 | Localhost:5432, db `handelsimperium`, user `handelsimperium_user` |
| **Connection Pooling** | `psycopg_pool.ConnectionPool` | Min: 4, Max: 20 connections |
| **Frontend Architecture** | Jinja2 + HTMX + Tailwind CSS | Server-side rendering, partial DOM swaps, mobile responsive |
| **PWA Layer** | Web App Manifest + Service Worker | `/static/manifest.json`, `/static/sw.js`, `/static/icon.svg` |
| **Firewall** | Linux `ufw` | ALLOW: 22, 80, 443; DENY: 5432 (PostgreSQL), 8000 (Uvicorn) |
| **Backups** | `cron` + `pg_dump` + `gzip -9` | `/var/backups/handelsimperium/` daily at 03:00 UTC, 7-day retention |

---

## 2. Database Schema Snapshot

### 2.1 `users`
- `id`: `SERIAL PRIMARY KEY`
- `username`: `VARCHAR(64) UNIQUE NOT NULL`
- `password_hash`: `VARCHAR(255) NOT NULL`
- `balance`: `NUMERIC(14, 2) NOT NULL DEFAULT 200.00`
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- `last_active_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`

### 2.2 `buildings`
- `id`: `SERIAL PRIMARY KEY`
- `user_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `building_type`: `VARCHAR(32) NOT NULL` (`warehouse`, `lumberjack`, `quarry`, `mine`, `farm`, `weaver`)
- `level`: `INT NOT NULL DEFAULT 1`
- `production_rate`: `NUMERIC(10, 4) NOT NULL DEFAULT 0.0`
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Constraint:* `UNIQUE(user_id, building_type)`

### 2.3 `inventories`
- `id`: `SERIAL PRIMARY KEY`
- `user_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `resource_type`: `VARCHAR(32) NOT NULL` (`wood`, `stone`, `iron`, `grain`, `cloth`)
- `amount`: `NUMERIC(14, 2) NOT NULL DEFAULT 0.0`
- `last_calculated_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Constraint:* `UNIQUE(user_id, resource_type)`

### 2.4 `market_orders`
- `id`: `SERIAL PRIMARY KEY`
- `user_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `order_type`: `VARCHAR(8) NOT NULL` (`BUY`, `SELL`)
- `resource_type`: `VARCHAR(32) NOT NULL`
- `amount`: `NUMERIC(12, 2) NOT NULL`
- `filled_amount`: `NUMERIC(12, 2) NOT NULL DEFAULT 0.0`
- `limit_price`: `NUMERIC(10, 2) NOT NULL`
- `status`: `VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'` (`ACTIVE`, `FILLED`, `CANCELLED`)
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Index:* `idx_market_orders_matching ON (resource_type, status, order_type, limit_price, created_at)`

### 2.5 `trades`
- `id`: `SERIAL PRIMARY KEY`
- `buyer_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `seller_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `resource_type`: `VARCHAR(32) NOT NULL`
- `amount`: `NUMERIC(12, 2) NOT NULL`
- `price`: `NUMERIC(10, 2) NOT NULL`
- `fee`: `NUMERIC(10, 2) NOT NULL DEFAULT 0.0`
- `executed_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Index:* `idx_trades_resource_time ON (resource_type, executed_at DESC)`

### 2.6 `user_catchups`
- `id`: `SERIAL PRIMARY KEY`
- `user_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `offline_seconds`: `NUMERIC(10, 1) NOT NULL`
- `production_delta`: `JSONB NOT NULL`
- `trade_delta`: `JSONB NOT NULL`
- `dismissed`: `BOOLEAN NOT NULL DEFAULT FALSE`
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Index:* `idx_user_catchups_lookup ON (user_id, dismissed)`

### 2.7 `notifications` (Phase 5)
- `id`: `SERIAL PRIMARY KEY`
- `user_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `event_type`: `VARCHAR(32) NOT NULL` (`TRADE_EXECUTED`, `CONTRACT_FULFILLED`, `STORAGE_OVERFLOW`)
- `payload`: `JSONB NOT NULL DEFAULT '{}'::jsonb`
- `is_read`: `BOOLEAN NOT NULL DEFAULT FALSE`
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Index:* `idx_notifications_user ON (user_id, is_read, created_at DESC)`

### 2.8 `export_contracts` (Phase 5)
- `id`: `SERIAL PRIMARY KEY`
- `user_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `contract_date`: `DATE NOT NULL`
- `resource_type`: `VARCHAR(32) NOT NULL`
- `title`: `VARCHAR(128) NOT NULL`
- `target_amount`: `NUMERIC(10, 2) NOT NULL`
- `reward_taler`: `NUMERIC(12, 2) NOT NULL`
- `status`: `VARCHAR(16) NOT NULL DEFAULT 'AVAILABLE'` (`AVAILABLE`, `FULFILLED`, `EXPIRED`)
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- `expires_at`: `TIMESTAMPTZ NOT NULL`
- `fulfilled_at`: `TIMESTAMPTZ`
- *Constraints:* `UNIQUE(user_id, contract_date, resource_type)`
- *Index:* `idx_export_contracts_user_date ON (user_id, contract_date, status)`

---

## 3. Core Economic Equations

### 3.1 On-Demand Offline Production
$$\Delta t = T_{now} - \text{last\_calculated\_at}$$
$$\text{amount}_{new} = \min(\text{storage\_cap}, \text{amount}_{old} + (\Delta t \times \text{production\_rate}))$$

### 3.2 Warehouse Storage Scaling
$$\text{storage\_cap}(\text{level}) = \text{round}(1000.0 \times 1.5^{\text{level} - 1})$$

### 3.3 Building Upgrade Cost Scaling
$$\text{cost}_i(\text{level}) = \text{round}(\text{base\_cost}_i \times 1.5^{\text{level} - 1}, 2)$$
$$\text{production\_rate}(\text{level}) = \text{round}(\text{base\_rate} \times 1.25^{\text{level} - 1}, 4)$$

### 3.4 24-Hour Volume-Weighted Average Price (VWAP)
$$\text{VWAP}_{24h}(r) = \frac{\sum_{t \in \text{Trades}_{24h}(r)} (\text{amount}_t \times \text{price}_t)}{\sum_{t \in \text{Trades}_{24h}(r)} \text{amount}_t}$$
*Fallback Reference Prices:* Wood: 4.00, Stone: 5.00, Iron: 12.00, Grain: 3.00, Cloth: 8.00 Taler.

### 3.5 3-Pillar Merchant Net Worth
$$\text{Net Worth} = \text{Liquid Balance} + \sum_{r} (\text{inventory}_r \times \text{Price}(r)) + \sum_{b} \text{SunkCapital}(b, L)$$
$$\text{SunkCapital}(b, L) = \sum_{k=1}^{L-1} \left[ \text{cost}_{\text{balance}}(b, k) + \sum_{r} (\text{cost}_{r}(b, k) \times \text{Price}(r)) \right]$$

### 3.6 Deflationary Sinks
- **Market Fee:** $2\%$ deduced on executed maker/taker matches, burned permanently from circulation.
- **Export Contracts ("Handelskarawanen"):** 3 daily contracts expiring midnight UTC; consumed resources are permanently deleted from circulation in exchange for guaranteed Taler payouts.

---

## 4. Implemented Features & Endpoints

### Phase 1: Foundational Trading Platform
- `POST /auth/register`, `POST /auth/login`, `GET /auth/logout`
- `GET /` (Dashboard), `GET /resources/overview`
- `POST /market/orders` (Atomic order book matching with `SELECT ... FOR UPDATE`, 2% fee)

### Phase 2: Progression & Order Management
- `POST /buildings/{id}/upgrade` (Multi-resource upgrade scaling)
- `POST /market/orders/{id}/cancel` (Atomic escrow refund)
- `GET /market/book` (Aggregated price depth ladder)

### Phase 3: Catch-Up, Discovery, Balancing & Rate Limiting
- `GET /resources/catchup`, `POST /resources/catchup/dismiss` ("While You Were Away" modal)
- 24-Hour VWAP and recent 10 trade ledger
- Starter allocation (200 Taler, 50 Wood, 50 Stone) + Idempotent CLI seeder (`seed_market.py`)
- In-memory sliding-window rate limiter (15 orders/10s, 5 logins/60s)

### Phase 4: Ranking, Security Hardening & PWA
- `GET /ranking` (Top 50 merchant leaderboard backed by 5-minute in-memory cache)
- CSRF middleware (`X-CSRF-Token` header / double-submit cookie)
- Hardened cookies (`HttpOnly`, `SameSite=Lax`, dynamic `Secure`)
- Linux UFW firewall rules (22, 80, 443 allowed; 5432, 8000 denied)
- Mobile/PWA (`/static/manifest.json`, `/static/sw.js`, `/static/icon.svg`, single-tap quick-fill)

### Phase 5: Export Contracts, Notifications & Telemetry
- `GET /market/contracts` (Daily Handelskarawanen partial)
- `POST /market/contracts/{id}/fulfill` (Atomic commodity burn and payout)
- `GET /notifications` (Dropdown inbox partial)
- `GET /notifications/badge` (Dynamic unread count badge)
- `POST /notifications/read-all` (Mark all notifications read)
- `GET /admin/economy` (HTTP Basic Auth macro-economic telemetry)

---

## 5. Test Suite Metrics

All tests execute cleanly directly against PostgreSQL on the production server:
- **Total Test Files:** 6
- **Total Tests:** 27
- **Pass Rate:** 100% (27 passed in 7.32s)

| Test File | Tests | Coverage Scope |
| :--- | :--- | :--- |
| `tests/test_concurrent_orders.py` | 1 | Concurrent multi-threaded order matching ACID verification |
| `tests/test_e2e_http.py` | 1 | Full end-to-end HTTP registration, building upgrade, and trade matching |
| `tests/test_market_and_auth.py` | 3 | Password hashes, session tokens, building upgrades, order cancellation |
| `tests/test_phase3_features.py` | 6 | Offline catch-up, VWAP metrics, order input validation, rate limiter, seeder |
| `tests/test_phase4_features.py` | 5 | Net worth math, capital conservation, ranking cache, CSRF middleware, PWA assets |
| `tests/test_phase5_features.py` | 4 | Export contracts, trade notification dispatch, economic telemetry, HTMX flow |
| `tests/test_production.py` | 2 | Offline production delta calculation and storage cap enforcement |
| `tests/test_progression_and_cancel.py` | 5 | Multi-resource upgrade sufficiency/rollback, warehouse cap, aggregated depth |

---

## 6. Outstanding Backlog & Roadmap

1. **Merchant Guilds (Alliances):** Shared guild treasury, collective guild projects, and cooperative trade pacts.
2. **Dynamic Price Bands & Volatility Limits:** Circuit breakers preventing drastic market manipulation during sudden low-liquidity shocks.
3. **Regional Trade Routes & Travel Delays:** Multi-city map with geographic distance, caravan travel time, and regional price arbitrage.
4. **Automated Continuous Integration (CI):** GitHub Actions workflow running `pytest` against test PostgreSQL containers on pull requests.
