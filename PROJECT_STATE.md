# Project State: Handelsimperium

**Generated:** 2026-09-13T10:07:00+02:00  
**Repository Branch:** `main`  
**Current Phase:** Phase 10 (Dynamic Price Bands, Guild Territory & Kontor Auctions)  
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
| **Frontend Architecture** | Jinja2 + HTMX + Tailwind CSS | Server-side rendering, partial DOM swaps, mobile responsive, inline SVGs |
| **PWA Layer** | Web App Manifest + Service Worker | `/static/manifest.json`, `/static/sw.js`, `/static/icon.svg` |
| **Firewall** | Linux `ufw` | ALLOW: 22, 80, 443; DENY: 5432 (PostgreSQL), 8000 (Uvicorn) |
| **Backups** | `cron` + `pg_dump` + `gzip -9` | `/var/backups/handelsimperium/` daily at 03:00 UTC, 7-day retention |

---

## 2. Database Schema Snapshot

### 2.1 `regions` (Phase 8)
- `id`: `SERIAL PRIMARY KEY`
- `name`: `VARCHAR(64) UNIQUE NOT NULL`
- `tag`: `VARCHAR(4) UNIQUE NOT NULL`
- `description`: `TEXT NOT NULL`
- `coord_x`: `INT NOT NULL`
- `coord_y`: `INT NOT NULL`
- `resource_multipliers`: `JSONB NOT NULL`
- *Seed Data (4 Hanseatic Regions):*
  - **Ostseeküste (Danzig)** `[DANZ] (250, 120)`: `wood: 1.5`, `grain: 1.5`, `stone: 0.8`, `iron: 0.0`, `cloth: 0.0`
  - **Skandinavien (Visby)** `[VISB] (280, 50)`: `iron: 1.6`, `stone: 1.4`, `wood: 1.0`, `grain: 0.0`, `cloth: 0.0`
  - **Flandern (Brügge)** `[BRUG] (60, 200)`: `cloth: 1.8`, `grain: 1.0`, `wood: 0.6`, `stone: 0.0`, `iron: 0.0`
  - **Rheinland (Köln)** `[KOEL] (110, 220)`: `stone: 1.5`, `cloth: 1.2`, `iron: 0.8`, `wood: 0.0`, `grain: 0.0`

### 2.2 `users`
- `id`: `SERIAL PRIMARY KEY`
- `username`: `VARCHAR(64) UNIQUE NOT NULL`
- `password_hash`: `VARCHAR(255) NOT NULL`
- `balance`: `NUMERIC(14, 2) NOT NULL DEFAULT 200.00`
- `region_id`: `INT REFERENCES regions(id)` (Phase 8, fallback to `DANZ`)
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- `last_active_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Index:* `idx_users_region_id ON users(region_id)`

### 2.3 `buildings`
- `id`: `SERIAL PRIMARY KEY`
- `user_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `building_type`: `VARCHAR(32) NOT NULL` (`warehouse`, `lumberjack`, `quarry`, `mine`, `farm`, `weaver`)
- `level`: `INT NOT NULL DEFAULT 1`
- `production_rate`: `NUMERIC(10, 4) NOT NULL DEFAULT 0.0`
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Constraint:* `UNIQUE(user_id, building_type)`

### 2.4 `inventories`
- `user_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `resource_type`: `VARCHAR(32) NOT NULL` (`wood`, `stone`, `iron`, `grain`, `cloth`)
- `amount`: `NUMERIC(14, 2) NOT NULL DEFAULT 0.0`
- `last_calculated_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Primary Key:* `PRIMARY KEY (user_id, resource_type)`
- *Constraint:* `CHECK (amount >= 0)`

### 2.5 `market_orders`
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

### 2.6 `trades`
- `id`: `SERIAL PRIMARY KEY`
- `buyer_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `seller_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `resource_type`: `VARCHAR(32) NOT NULL`
- `amount`: `NUMERIC(12, 2) NOT NULL`
- `price`: `NUMERIC(10, 2) NOT NULL`
- `fee`: `NUMERIC(10, 2) NOT NULL DEFAULT 0.0`
- `executed_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Index:* `idx_trades_resource_time ON (resource_type, executed_at DESC)`

### 2.7 `user_catchups`
- `id`: `SERIAL PRIMARY KEY`
- `user_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `offline_seconds`: `NUMERIC(10, 1) NOT NULL`
- `production_delta`: `JSONB NOT NULL`
- `trade_delta`: `JSONB NOT NULL`
- `dismissed`: `BOOLEAN NOT NULL DEFAULT FALSE`
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Index:* `idx_user_catchups_lookup ON (user_id, dismissed)`

### 2.8 `notifications` (Phase 5)
- `id`: `SERIAL PRIMARY KEY`
- `user_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `event_type`: `VARCHAR(32) NOT NULL` (`TRADE_EXECUTED`, `CONTRACT_FULFILLED`, `STORAGE_OVERFLOW`, `GUILD_CREATED`, `GUILD_JOINED`, `MONUMENT_COMPLETED`, `TUTORIAL_REWARD_CLAIMED`, `CARAVAN_DISPATCHED`, `CARAVAN_ARRIVED`, `CARAVAN_UNLOADED`, `DEPOT_TRANSFERRED`)
- `payload`: `JSONB NOT NULL DEFAULT '{}'::jsonb`
- `is_read`: `BOOLEAN NOT NULL DEFAULT FALSE`
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Index:* `idx_notifications_user ON (user_id, is_read, created_at DESC)`

### 2.9 `export_contracts` (Phase 5)
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

### 2.10 `guilds` (Phase 6)
- `id`: `SERIAL PRIMARY KEY`
- `name`: `VARCHAR(64) UNIQUE NOT NULL`
- `tag`: `VARCHAR(6) UNIQUE NOT NULL`
- `description`: `TEXT`
- `leader_id`: `INT NOT NULL REFERENCES users(id)`
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Index:* `idx_guilds_leader ON (leader_id)`

### 2.11 `guild_members` (Phase 6)
- `id`: `SERIAL PRIMARY KEY`
- `guild_id`: `INT NOT NULL REFERENCES guilds(id) ON DELETE CASCADE`
- `user_id`: `INT UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `role`: `VARCHAR(16) NOT NULL DEFAULT 'MEMBER'` (`LEADER`, `OFFICER`, `MEMBER`)
- `joined_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Index:* `idx_guild_members_user ON (user_id)`
- *Index:* `idx_guild_members_guild ON (guild_id)`

### 2.12 `guild_bank` & `guild_bank_inventory` (Phase 6)
- `guild_bank.guild_id`: `INT PRIMARY KEY REFERENCES guilds(id) ON DELETE CASCADE`
- `guild_bank.balance`: `NUMERIC(14, 2) NOT NULL DEFAULT 0.0`
- `guild_bank_inventory`: `(guild_id INT, resource_type VARCHAR(32), amount NUMERIC(14, 2) DEFAULT 0.0)`
- *Constraint:* `PRIMARY KEY (guild_id, resource_type)`

### 2.13 `guild_projects` (Phase 6)
- `id`: `SERIAL PRIMARY KEY`
- `guild_id`: `INT NOT NULL REFERENCES guilds(id) ON DELETE CASCADE`
- `project_type`: `VARCHAR(32) NOT NULL` (`FREIHAFEN`, `SPEICHERSTADT`)
- `stage`: `INT NOT NULL DEFAULT 1`
- `target_costs`: `JSONB NOT NULL`
- `invested_resources`: `JSONB NOT NULL DEFAULT '{}'::jsonb`
- `is_completed`: `BOOLEAN NOT NULL DEFAULT FALSE`
- `completed_at`: `TIMESTAMPTZ`
- *Constraints:* `UNIQUE(guild_id, project_type)`
- *Index:* `idx_guild_projects_lookup ON (guild_id, is_completed)`
- *Index:* `idx_guild_projects_perk ON (guild_id, project_type, is_completed)`

### 2.14 `user_tutorials` (Phase 7, 9 & 10)
- `user_id`: `INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE`
- `current_step`: `INT NOT NULL DEFAULT 1`
- `completed_steps`: `JSONB NOT NULL DEFAULT '[]'::jsonb`
- `is_finished`: `BOOLEAN NOT NULL DEFAULT FALSE`
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Index:* `idx_user_tutorials_lookup ON (user_id, is_finished)`

### 2.15 `caravans` (Phase 9)
- `id`: `SERIAL PRIMARY KEY`
- `user_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `origin_region_id`: `INT NOT NULL REFERENCES regions(id)`
- `destination_region_id`: `INT NOT NULL REFERENCES regions(id)`
- `cargo`: `JSONB NOT NULL DEFAULT '{}'::jsonb`
- `departure_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- `arrival_at`: `TIMESTAMPTZ NOT NULL`
- `status`: `VARCHAR(16) NOT NULL DEFAULT 'EN_ROUTE'` (`EN_ROUTE`, `ARRIVED`, `UNLOADED`, `CANCELLED`)
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Constraint:* `CHECK (status IN ('EN_ROUTE', 'ARRIVED', 'UNLOADED', 'CANCELLED'))`
- *Index:* `idx_caravans_user_status_arrival ON caravans(user_id, status, arrival_at)`

### 2.16 `regional_depots` (Phase 9)
- `id`: `SERIAL PRIMARY KEY`
- `user_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `region_id`: `INT NOT NULL REFERENCES regions(id)`
- `resource_type`: `VARCHAR(32) NOT NULL`
- `amount`: `NUMERIC(14, 2) NOT NULL DEFAULT 0.0`
- `last_updated_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Constraint:* `UNIQUE (user_id, region_id, resource_type)`
- *Index:* `idx_regional_depots_user_region ON regional_depots(user_id, region_id)`

### 2.17 `kontor_auctions` (Phase 10)
- `id`: `SERIAL PRIMARY KEY`
- `region_id`: `INT NOT NULL REFERENCES regions(id)`
- `start_time`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- `end_time`: `TIMESTAMPTZ NOT NULL` (7-day epoch)
- `current_highest_bid`: `NUMERIC(14, 2) NOT NULL DEFAULT 0.0`
- `highest_bidder_guild_id`: `INT REFERENCES guilds(id) ON DELETE SET NULL`
- `status`: `VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'` (`ACTIVE`, `RESOLVED`, `CANCELLED`)
- `created_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Constraint:* `CHECK (status IN ('ACTIVE', 'RESOLVED', 'CANCELLED'))`
- *Index:* `idx_kontor_auctions_region_status ON kontor_auctions(region_id, status)`
- *Index:* `idx_kontor_auctions_end_status ON kontor_auctions(end_time, status)`

### 2.18 `regional_controllers` (Phase 10)
- `region_id`: `INT PRIMARY KEY REFERENCES regions(id)`
- `guild_id`: `INT NOT NULL REFERENCES guilds(id) ON DELETE CASCADE`
- `winning_bid`: `NUMERIC(14, 2) NOT NULL`
- `assigned_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- `valid_until`: `TIMESTAMPTZ NOT NULL`
- *Index:* `idx_regional_controllers_valid ON regional_controllers(guild_id, valid_until)`

### 2.19 `guild_contributions` (Phase 10)
- `id`: `SERIAL PRIMARY KEY`
- `guild_id`: `INT NOT NULL REFERENCES guilds(id) ON DELETE CASCADE`
- `user_id`: `INT NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `amount`: `NUMERIC(14, 2) NOT NULL`
- `contributed_at`: `TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- *Index:* `idx_guild_contributions_lookup ON guild_contributions(guild_id, user_id)`

---

## 3. Core Economic Equations

### 3.1 On-Demand Offline Production
$$\Delta t = T_{now} - \text{last\_calculated\_at}$$
$$\text{amount}_{new} = \min(\text{storage\_cap}_{\text{effective}}, \text{amount}_{old} + (\Delta t \times \text{production\_rate}))$$

### 3.2 Warehouse Storage Scaling & Guild Speicherstadt Buff
$$\text{storage\_cap}_{\text{base}}(\text{level}) = \text{round}(1000.0 \times 1.5^{\text{level} - 1})$$
$$\text{storage\_cap}_{\text{effective}} = \begin{cases} \text{round}(\text{storage\_cap}_{\text{base}} \times 1.10) & \text{if member has active } \text{SPEICHERSTADT} \\ \text{storage\_cap}_{\text{base}} & \text{otherwise} \end{cases}$$

### 3.3 Building Upgrade Cost & Regional Production Rate Scaling
$$\text{cost}_i(\text{level}) = \text{round}(\text{base\_cost}_i \times 1.5^{\text{level} - 1}, 2)$$
$$\text{production\_rate}(\text{level}) = \begin{cases} 0.0 & \text{if } \text{multiplier}_{\text{region}} == 0.0 \\ \text{round}(\text{base\_rate} \times 1.25^{\text{level} - 1} \times \text{multiplier}_{\text{region}}, 4) & \text{otherwise} \end{cases}$$

*Zero-Yield Guard:* Upgrading any building whose commodity has a regional multiplier of `0.0` is blocked with:
> *"Dieser Rohstoff kann in Eurer Region nicht gewonnen werden."*
Warehouses (`Zentrallager`) have `resource = None` and remain upgradable across all regions.

### 3.4 24-Hour Volume-Weighted Average Price (VWAP)
$$\text{VWAP}_{24h}(r) = \frac{\sum_{t \in \text{Trades}_{24h}(r)} (\text{amount}_t \times \text{price}_t)}{\sum_{t \in \text{Trades}_{24h}(r)} \text{amount}_t}$$
*Fallback Reference Prices:* Wood: 4.00, Stone: 5.00, Iron: 12.00, Grain: 3.00, Cloth: 8.00 Taler.

### 3.5 3-Pillar Merchant Net Worth
$$\text{Net Worth} = \text{Liquid Balance} + \sum_{r} (\text{inventory}_r \times \text{Price}(r)) + \sum_{b} \text{SunkCapital}(b, L)$$
$$\text{SunkCapital}(b, L) = \sum_{k=1}^{L-1} \left[ \text{cost}_{\text{balance}}(b, k) + \sum_{r} (\text{cost}_{r}(b, k) \times \text{Price}(r)) \right]$$

### 3.6 Deflationary Sinks & Transaction Taxes
- **Standard Market Fee:** $2.0\%$ deducted on executed maker/taker matches, burned permanently from circulation.
- **Alliance Freihafen Perk:** Reduces seller market fee down to $1.5\%$ if the seller's guild has completed the `FREIHAFEN` monument.
- **Export Contracts ("Handelskarawanen"):** 3 daily contracts expiring midnight UTC; consumed resources are permanently deleted from circulation in exchange for guaranteed Taler payouts.
- **Guild Founding Fee:** $500.00$ Taler permanently deducted upon founding an alliance.

### 3.7 Caravan Transit & Regional Logistics (Phase 9)
$$\text{distance} = \sqrt{(x_2 - x_1)^2 + (y_2 - y_1)^2} \quad \text{[in Seemeilen / sm, gerundet auf 2 Dezimalstellen]}$$
$$\text{duration\_seconds} = \text{round}(\text{distance} \times 12.0 \times \text{SpeedMultiplier})$$
$$\text{arrival\_at} = \text{departure\_at} + \Delta t_{\text{duration}}$$
$$\text{Total Cargo} = \sum_{r \in \text{Resources}} \text{amount}_r \le 250.0 \quad \text{[Max. Karawanen-Zuladung]}$$

- **Atomic Inventory Deduction:** Cargo is locked and subtracted from Kontor inventory at departure via `SELECT ... FOR UPDATE`.
- **Deterministic Arrival Resolution:** On queries or actions, status transitions from `EN_ROUTE` to `ARRIVED` whenever `NOW() >= arrival_at`.
- **Regional Depots:** Arrived caravans unload goods into the destination region's `regional_depots` record via atomic upsert (`ON CONFLICT (user_id, region_id, resource_type) DO UPDATE`).
- **Kontor Transfer:** Stockpiled depot commodities can be transferred back into the home Kontor warehouse, bounded by available warehouse storage capacity.

### 3.8 Dynamic Price Bands & Kontor Territory Privileges (Phase 10)
- **Dynamic Price Bands (Market Volatility Circuit Breakers):**
  $$\text{ReferencePrice}(r) = \begin{cases} \text{VWAP}_{24h}(r) & \text{if volume}_{24h}(r) > 0 \\ \text{BasePrice}(r) & \text{otherwise} \end{cases}$$
  $$\text{Price Floor}(r) = \text{round}(0.50 \times \text{ReferencePrice}(r), 2)$$
  $$\text{Price Ceiling}(r) = \text{round}(2.00 \times \text{ReferencePrice}(r), 2)$$
  $$\text{Valid Limit Order Price} \in [\text{Price Floor}(r), \text{Price Ceiling}(r)]$$
  Orders submitted outside the corridor are rejected with HTTP 422 (`Handelsspanne überschritten: Das Angebot weicht zu stark vom 24h-Marktwert ab`).
- **Kontor Auction Bidding & Atomic Outbid Refunds:**
  $$\text{Minimum Bid} = \begin{cases} 100.00 \text{ Taler} & \text{if } \text{current\_highest\_bid} == 0.0 \\ \text{current\_highest\_bid} + 50.00 \text{ Taler} & \text{otherwise} \end{cases}$$
  Bids are funded from the Guild Bank (War Chest) with atomic escrow. If a guild is outbid, its previous bid is immediately and atomically refunded back into its `guild_bank.balance`.
- **Territorial Privileges of the Regional Controller:**
  - **25% Expedition Transit Speedup:** Caravans departing from or heading toward a region controlled by the merchant's guild receive a $0.75\times$ duration reduction ($\text{SpeedMultiplier} = 0.75$):
    $$\text{duration}_{\text{effective}} = \max(1, \text{round}(\text{duration}_{\text{base}} \times 0.75))$$
  - **0.5% Regional Trade Tax Dividend:** For every executed market trade where the seller belongs to the controlled region, $0.5\%$ of the total trade value is automatically credited to the controlling guild's bank treasury.

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

### Phase 6: Merchant Guilds, Cooperative Monuments & Alliance Buffs
- `GET /guilds` (Guild Hall for members, recruitment directory and founding form for unaffiliated)
- `POST /guilds/create` (Found guild for 500 Taler, sets creator as `LEADER`)
- `POST /guilds/{id}/join` (Join open merchant alliance)
- `POST /guilds/leave` (Leave guild with automatic leadership succession)
- `POST /guilds/projects/{id}/contribute` (Atomic resource & Taler contribution to monuments)
- Cooperative Monument Perks:
  - `FREIHAFEN`: Market trading fee reduced from 2.0% to 1.5% for all guild members.
  - `SPEICHERSTADT`: +10% flat storage capacity on all warehouse levels for all guild members.

### Phase 7: Visual Overhaul, Quest Onboarding Tutorial & Merchant Handbook
- **Visual & Graphic Overhaul:**
  - Mercantile terminal styling: Deep maritime slate (`slate-950`, `slate-900`) and amber accents (`amber-500`, `amber-600`).
  - Crisp inline SVG icon system (`components/icons.html`) replacing text labels and emojis.
  - Dual-column financial trading terminal layout for `/market/book`.
  - Detailed capacity percentage bars across all commodity stores and warehouse tiers.
- **Modular Onboarding Quest Engine ("Kaufmannslehre"):**
  - `GET /tutorial/widget` (Persistent, dismissible quest widget on the dashboard).
  - `POST /tutorial/claim` (Atomic eligibility verification, milestone progression, and reward disbursement).
  - Progressive Milestones: Bestandsaufnahme (25 Taler), Expansion (50 Taler), Marktzugang (25 Wood/Stone), Fernhandel (100 Taler), Zunftbeitritt (150 Taler).
- **Living In-Game Merchant Handbook ("Das Kontor-Handbuch"):**
  - `GET /handbuch` (Indexed reference manual directly exposing backend formulas, building costs, reference prices, and guild perks).

### Phase 8: Regional Specialization & Asymmetric Resource Scarcity
- **Database Migration (`007_phase8_regions.sql`):**
  - `regions` table with coordinates, tags, and JSONB multiplier maps.
  - Foreign key `region_id` on `users` table with migration fallback to `Ostseeküste (Danzig)`.
  - 4 historical Hanseatic trade centers: Danzig (Wood/Grain 1.5x, Stone 0.8x, Iron/Cloth 0.0x), Visby (Iron 1.6x, Stone 1.4x, Wood 1.0x, Grain/Cloth 0.0x), Brügge (Cloth 1.8x, Grain 1.0x, Wood 0.6x, Stone/Iron 0.0x), Köln (Stone 1.5x, Cloth 1.2x, Iron 0.8x, Wood/Grain 0.0x).
- **Asymmetric Core Production Engine (`app/engine/production.py`):**
  - `ensure_user_entities` scales initial rates by regional multipliers.
  - `upgrade_building` enforces regional feasibility: upgrades for 0.0-multiplier commodities are strictly rejected.
  - Effective production rate scales deterministically: $\text{round}(\text{base\_rate} \times 1.25^{\text{level}-1} \times \text{multiplier}_{\text{region}}, 4)$.
- **Interactive Registration & UI Hydration:**
  - `GET /auth/register` & `POST /auth/register`: Card-based region selector displaying coordinates, lore, bonuses, and deficits; validates selection against database.
  - Top navigation bar & Kontor resource overview render merchant's Home Region badge with Tag and Coordinates.
  - Commodity cards and building tables dynamically badge active modifiers (`+50% Bonus`, `-20% Malus`, `0.0x Reine Importware`) and disable upgrades for non-indigenous resources with *"Nicht förderbar"*.

### Phase 9: Caravan Expeditions, Travel Durations & Regional Depots
- **Database Migration (`008_phase9_caravans.sql`):**
  - `caravans` table tracking origin, destination, JSONB cargo, departure, arrival timestamps, and status (`EN_ROUTE`, `ARRIVED`, `UNLOADED`, `CANCELLED`).
  - `regional_depots` table providing persistent storage per merchant, region, and commodity with unique constraints.
  - Composite indexes for rapid arrival status resolution and depot lookups.
- **Logistics & Transit Engine (`app/engine/caravans.py`):**
  - `calculate_distance`: Euclidean metric $\sqrt{\Delta x^2 + \Delta y^2}$.
  - `calculate_travel_duration`: $\text{round}(\text{distance} \times 12.0)$ seconds.
  - `dispatch_caravan`: Enforces 250-unit capacity cap, non-negative amounts, and distinct regions; atomically locks and deducts inventory from the Kontor warehouse.
  - `resolve_caravan_statuses`: Transitions expired transit routes from `EN_ROUTE` to `ARRIVED` with `CARAVAN_ARRIVED` notifications.
  - `unload_caravan`: Safely transfers cargo into `regional_depots` at destination and updates status to `UNLOADED`.
  - `transfer_depot_to_kontor`: Moves stockpiled goods from foreign depots into Kontor inventories up to warehouse storage capacity.
- **Frontend Logistics Terminal (`GET /expeditions` & `app/templates/components/expeditions.html`):**
  - Dedicated navigation tab with SVG compass rose icon.
  - Active caravans monitor with live countdown timers, animated progress bars, and "Waren im Depot entladen" action buttons.
  - Interactive expedition console with destination selector, real-time cargo total calculation, and 250-unit capacity guard.
  - Foreign regional depots overview with single-click "Depot in Kontor überführen" action.
- **Synchronized Tutorial & Living Handbook:**
  - Tutorial Step 6 ("6. Die erste Expedition"): Disburses 100.00 Taler & 30.00 Tuch upon dispatching an overseas expedition.
  - Merchant Handbook (`/handbuch`): Added Section 8 ("Logistik, Übersee-Expeditionen & Regionaldepots") with formulas, capacity limits, and depot mechanics.

### Phase 10: Dynamic Price Bands, Guild Territory & Kontor Auctions
- **Database Migration (`009_phase10_auctions.sql`):**
  - `kontor_auctions`: 7-day cyclical bidding epochs for territorial control over each region with current bid, highest bidder guild, and status.
  - `regional_controllers`: Tracks active guild sovereignty, winning bid amount, and validity expiration per region.
  - `guild_contributions`: Audit ledger recording user donations into their guild's War Chest treasury.
- **Dynamic Price Corridor Engine (`app/engine/matching.py`):**
  - Circuit breakers strictly enforce $[0.50 \times \text{VWAP}_{24h}, 2.00 \times \text{VWAP}_{24h}]$ corridor, falling back to base reference prices when 24h trading volume is zero.
  - `POST /market/orders` rejects outlier orders with HTTP 422 and renders warning toast.
  - Order form displays dynamic admissible price corridor guidance for selected commodity.
- **War Chest Treasury & Kontor Auction Engine (`app/engine/auctions.py`):**
  - `POST /guilds/bank/deposit`: Deducts merchant balance, increments `guild_bank.balance`, and logs contribution record.
  - `POST /guilds/auctions/{id}/bid`: Places alliance bid from guild war chest; enforces minimum bid (100 Taler or current bid + 50 Taler); executes instant atomic refund to previous highest bidder guild.
  - `resolve_kontor_auctions`: Deterministically concludes expired auctions, crowns controlling guild in `regional_controllers` for 7 days, and instantiates next auction epoch.
- **Territorial Privileges & Royal UI Badging:**
  - **Transit Speedup:** 25% duration reduction ($0.75\times$) for caravans travelling to/from controlled territories.
  - **Trade Tax Dividend:** 0.5% regional trade dividend credited to controlling guild's treasury on market sales.
  - Crown badges (`👑 [TAG]`) render next to controlling regions on top nav bar, expedition cards, and Kontor screens.
- **Synchronized Tutorial & Handbook:**
  - Tutorial Step 7 ("7. Die Macht der Hanse"): Disburses 200.00 Taler & 40.00 Eisen upon founding or joining an alliance.
  - Merchant Handbook (`/handbuch`): Section 9 ("Territoriale Kontrolle, Kontor-Auktionen & Kriegskasse") details price band math, auction mechanics, outbid guarantees, and territorial perks.

### Stability & Core Engine Patches (Post-Phase 10 Integrity Patch)
- **Deterministic Lock Ordering & Deadlock Prevention (`app/engine/matching.py`):**
  - Eliminated concurrent PostgreSQL transaction deadlocks during simultaneous maker/taker matching and order execution.
  - User IDs, order IDs, and inventory records are deterministically sorted in strict numerical/lexicographical ascending order (`ORDER BY ... ASC`) before executing `SELECT ... FOR UPDATE` locks.
  - Corrected schema alignment across `matching.py` by removing erroneous references to a non-existent `id` column on the `inventories` table (keyed by `(user_id, resource_type)`).
- **Cumulative Warehouse Capacity Enforcement (`app/engine/production.py` & `app/engine/caravans.py`):**
  - Transformed warehouse storage cap from a per-commodity limit into a global cumulative ceiling: $\sum_{r \in \text{Commodities}} \text{amount}_r \le \text{storage\_cap}_{\text{effective}}$.
  - In `calculate_offline_production`, if total generation across all commodities exceeds remaining warehouse volume ($\text{storage\_cap} - \sum \text{amount}_{\text{current}}$), surplus intake is distributed proportionally based on building generation rates, excess volume is discarded, and discarded units are tracked under `production_delta[r]["lost"]`.
  - In `transfer_depot_to_kontor` (`app/engine/caravans.py`), transfer requests that would exceed the cumulative storage capacity are strictly rejected with `ValueError` (`"Nicht genügend Lagerkapazität im Zentrallager vorhanden"`).
- **Three-Tier Price Corridor Fallback Hierarchy (`app/engine/matching.py`):**
  - Hardened reference price discovery against zero-liquidity distortion:
    1. **Tier 1 (24h-VWAP):** Volume-Weighted Average Price over preceding 24 hours if $\text{volume}_{24h} > 0$.
    2. **Tier 2 (Historical Trade Price):** Most recent trade execution price when 24h volume is zero.
    3. **Tier 3 (Canonical Base Price):** Configuration baseline price (`REFERENCE_PRICES[r]`) when no trading history exists.
  - Price corridors remain strictly bounded to $[0.50 \times \text{ReferencePrice}, 2.00 \times \text{ReferencePrice}]$.
- **Auction Expiration Verification (`app/engine/auctions.py` & `app/routes/guilds.py`):**
  - Bids placed at or after epoch expiration (`NOW() >= epoch_end_at`) are rejected immediately with `ValueError` ("Auktion ist bereits abgelaufen").
  - `handle_place_auction_bid` maps expiration errors to HTTP 422 Unprocessable Content.
  - Automatic on-demand epoch resolution triggers immediately upon detecting an expired auction.
- **Strict Decimal Numeric Type Casting:**
  - Resolved `TypeError` incompatibilities between Python `float` and PostgreSQL `Decimal` types across `caravans.py`, `auctions.py`, `production.py`, `guilds.py`, and `matching.py`.
  - All database inputs and arithmetic conversions cast values to `Decimal(str(round(val, 2)))` or `Decimal(str(round(val, 4)))` prior to execution.
- **Zero-Yield Building Initialization (`app/engine/production.py` & `app/routes/auth_routes.py`):**
  - For commodities where the merchant's home region has a `0.0` multiplier (import-only goods), buildings are initialized with `level = 0` and `production_rate = 0.0000` (instead of level 1 with 0 rate), reflecting that non-indigenous extraction infrastructure does not exist in the home settlement.

---

## 5. Test Suite Metrics

All tests execute cleanly directly against PostgreSQL on the production server:
- **Total Test Files:** 14
- **Total Tests:** 61
- **Pass Rate:** 100% (61 passed in 17.74s)

| Test File | Tests | Coverage Scope |
| :--- | :--- | :--- |
| `tests/test_bugfixes.py` | 6 | Deadlock-free matching concurrency, Cumulative warehouse capacity, 3-tier price corridor hierarchy, Auction deadline rejection, Decimal precision casting, Zero-yield building initialization |
| `tests/test_concurrent_orders.py` | 1 | Concurrent multi-threaded order matching ACID verification |
| `tests/test_e2e_http.py` | 1 | Full end-to-end HTTP registration, building upgrade, and trade matching |
| `tests/test_market_and_auth.py` | 3 | Password hashes, session tokens, building upgrades, order cancellation |
| `tests/test_phase3_features.py` | 6 | Offline catch-up, VWAP metrics, order input validation, rate limiter, seeder |
| `tests/test_phase4_features.py` | 5 | Net worth math, capital conservation, ranking cache, CSRF middleware, PWA assets |
| `tests/test_phase5_features.py` | 4 | Export contracts, trade notification dispatch, economic telemetry, HTMX flow |
| `tests/test_phase6_guilds.py` | 5 | Guild founding, succession, monument contributions, Freihafen fee perk, Speicherstadt cap perk, HTMX flow |
| `tests/test_phase7_tutorial.py` | 4 | Tutorial quest progression & rewards, duplicate claim prevention, handbook accuracy, SVG template integrity |
| `tests/test_phase8_regions.py` | 6 | Registration validation, regional yield scaling, 0.0-yield upgrade blocking, warehouse universal upgrades, matrix & UI badges |
| `tests/test_phase9_caravans.py` | 6 | Euclidean distance & transit duration math, capacity limits, atomic deduction, arrival status resolution, depot unloading & transfer, tutorial step 6 claim, HTTP rendering |
| `tests/test_phase10_auctions_and_limits.py` | 7 | Dynamic price bands [0.5x, 2.0x VWAP], War Chest deposits, Kontor auction bidding & outbid refund, epoch resolution, speed bonuses, regional trade tax dividends, tutorial step 7, HTTP endpoints |
| `tests/test_production.py` | 2 | Offline production delta calculation and storage cap enforcement |
| `tests/test_progression_and_cancel.py` | 5 | Multi-resource upgrade sufficiency/rollback, warehouse cap, aggregated depth |

---

## 6. Outstanding Backlog & Roadmap

1. **Automated Continuous Integration (CI):** GitHub Actions workflow running `pytest` against ephemeral PostgreSQL test containers on pull requests.
2. **Automated NPC Caravans & Hanseatic Trade Convoys:** Dynamic scheduled NPC cargo fleets navigating between regional Kontors to stimulate trade volume and provide counterparty liquidity.
3. **Naval Blockades & Piracy Risk Events:** Dynamic sea-lane hazard conditions modifying caravan transit durations and cargo insurance mechanisms.
4. **Historical Price Charts & Candlesticks:** Lightweight client-side charting of price discovery history and 24h VWAP trends using canvas/SVG.
