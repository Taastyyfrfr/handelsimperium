# Handelsimperium

> **A medieval Hanseatic idle trading game with an atomic limit order book, asymmetric regional production, autonomous NPC convoys, overseas caravan logistics, and cooperative merchant guilds.**

[![CI](https://github.com/Taastyyfrfr/handelsimperium/actions/workflows/ci.yml/badge.svg)](https://github.com/Taastyyfrfr/handelsimperium/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-v1.11.1-amber.svg)](https://github.com/Taastyyfrfr/handelsimperium/releases)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-emerald.svg)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-blue.svg)](https://www.postgresql.org/)

**Live Production Endpoint:** [http://80.158.79.44/](http://80.158.79.44/)

---

## Overview

**Handelsimperium** simulates the bustling mercantile economy of the 14th-century Hanseatic League. Players establish trading kontors in historical trade centers (Danzig, Visby, Brügge, Köln), manage commodity extraction, dispatch overseas trade expeditions across the Baltic and North Seas, and trade real commodities on an atomic, market-clearing limit order book.

---

## Key Features

- **Asymmetric Regional Yields & Deficits:**
  - **Ostseeküste (Danzig):** Wood & Grain (+50%), Stone (-20%), Iron & Cloth (0.0x, pure imports).
  - **Skandinavien (Visby):** Iron (+60%), Stone (+40%), Wood (1.0x), Grain & Cloth (0.0x).
  - **Flandern (Brügge):** Cloth (+80%), Grain (1.0x), Wood (-40%), Stone & Iron (0.0x).
  - **Rheinland (Köln):** Stone (+50%), Cloth (+20%), Iron (-20%), Wood & Grain (0.0x).
  - Enforces zero-yield guards preventing unfeasible production upgrades.

- **Atomic Limit Order Book & Financial Matching:**
  - Real-time bids and asks ladder with 2.0% transaction fee burn.
  - Dynamic Volatility Price Corridor $[0.50 \times \text{VWAP}_{24h}, 2.00 \times \text{VWAP}_{24h}]$ circuit breakers.
  - Strict wash-trading guard preventing self-crossing maker/taker orders.
  - Price improvement escrow refunds: buyers submitting aggressive taker orders receive instant refunds for favorable maker prices.
  - Zero-dependency server-rendered inline SVG price charts.

- **Overseas Caravans & Regional Depots:**
  - Coordinate-based transit duration ($\Delta t = \text{round}(d \times 12.0)$ seconds) and 250-unit cargo cap.
  - Foreign regional depots (500-unit cap) and return caravans.

- **Autonomous NPC Trade Convoys:**
  - Automated merchant fleet navigating deficit routes ($>1.0x \to 0.0x$) across the Hanseatic network.
  - Arriving cargo deterministically matches against resting player BUY orders.

- **Merchant Guilds ("Zünfte") & Kontor Auctions:**
  - Guild founding (500 Taler fee) and monument projects (*Freihafen* 1.5% fee discount, *Speicherstadt* +10% warehouse capacity).
  - 7-day cyclical Kontor auctions for territorial control with atomic outbid refunds to the Guild War Chest.

- **Onboarding Quest Engine & Living Merchant Handbook:**
  - Dynamic "Kaufmannslehre" tutorial milestones with instant reward claims.
  - Indexed, in-game manual exposing all mathematical formulas and constants directly at `/handbuch`.

---

## Technology Stack

- **Backend:** Python 3.12, FastAPI, Uvicorn, Starlette, Pydantic
- **Database:** PostgreSQL 16 with Connection Pooling (`psycopg_pool`), Row-level Locking (`FOR UPDATE`), and Advisory Locks
- **Frontend:** Jinja2 SSR, HTMX 1.9.11, Tailwind CSS CDN, Inline SVGs
- **PWA:** Web App Manifest (`/static/manifest.json`), Service Worker (`/static/sw.js`), SVG App Icon
- **Reverse Proxy & Host:** Caddy v2, Ubuntu 24.04 LTS, Systemd (`handelsimperium.service`)

---

## Local Development & Setup

### Prerequisites
- Python 3.12+
- PostgreSQL 16+

### Quick Start
```bash
# 1. Clone repository
git clone https://github.com/Taastyyfrfr/handelsimperium.git
cd handelsimperium

# 2. Virtual environment setup
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Environment configuration
cp .env.example .env  # Configure DB_HOST, DB_USER, DB_PASS, DB_NAME

# 4. Run tests
pytest -v

# 5. Start development server
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

---

## Production Architecture

```
Internet -> [Port 80/443] -> Caddy v2 -> [Port 8000] -> Uvicorn (2 workers) -> FastAPI -> PostgreSQL 16
```

Managed via systemd service `handelsimperium.service` with automated daily database backups at 03:00 UTC.