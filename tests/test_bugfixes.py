import pytest
import time
import uuid
from decimal import Decimal
from typing import Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from starlette.testclient import TestClient
import psycopg
from psycopg.rows import dict_row

from app.main import app
from app.config import settings, REFERENCE_PRICES, BUILDING_CONFIG
from app.auth import hash_password, create_session_token
from app.engine.matching import place_and_match_order, get_price_corridor, cancel_order
from app.engine.production import (
    ensure_user_entities,
    calculate_offline_production,
    upgrade_building,
    get_storage_cap,
)
from app.engine.guilds import create_guild
from app.engine.auctions import (
    ensure_kontor_auctions,
    place_kontor_auction_bid,
    resolve_kontor_auctions,
)


def create_test_user(db_conn, username_prefix: str, region_tag: str = "DANZ", balance: float = 5000.0) -> Dict[str, Any]:
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = %s", (region_tag,))
        reg = cur.fetchone()
        reg_id = reg["id"] if reg else 1
        username = f"{username_prefix}_{uuid.uuid4().hex[:6]}"
        cur.execute(
            """
            INSERT INTO users (username, password_hash, balance, region_id)
            VALUES (%s, %s, %s, %s)
            RETURNING id, username, balance, region_id
            """,
            (username, hash_password("secret123"), Decimal(str(balance)), reg_id),
        )
        user = cur.fetchone()
        ensure_user_entities(cur, user["id"])
        db_conn.commit()
        return user


def test_concurrent_matching_deadlock_free(db_conn):
    """
    Bugfix 1 Verification:
    Stress tests concurrent cross-order placement and matching between multiple
    simultaneous merchants. Verifies that deterministic ASC lock ordering
    completely eliminates PostgreSQL transaction deadlocks.
    """
    u1 = create_test_user(db_conn, "bf_trader1", "DANZ", balance=10000.0)
    u2 = create_test_user(db_conn, "bf_trader2", "DANZ", balance=10000.0)
    u3 = create_test_user(db_conn, "bf_trader3", "DANZ", balance=10000.0)
    u4 = create_test_user(db_conn, "bf_trader4", "DANZ", balance=10000.0)

    # Provide sellers with stock
    with db_conn.cursor() as cur:
        cur.execute("UPDATE inventories SET amount = 500.0 WHERE user_id IN (%s, %s) AND resource_type = 'wood'", (u2["id"], u4["id"]))
        # Zero passive production so test quantities remain pure
        cur.execute("UPDATE buildings SET production_rate = 0.0 WHERE user_id IN (%s, %s, %s, %s)", (u1["id"], u2["id"], u3["id"], u4["id"]))
        db_conn.commit()

    deadlock_errors = []

    def place_order_task(user_id: int, order_type: str, qty: int, price: float):
        try:
            with psycopg.connect(settings.conn_str, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    place_and_match_order(cur, user_id, order_type, "wood", qty, price)
                    conn.commit()
            return True
        except Exception as e:
            if "deadlock" in str(e).lower():
                deadlock_errors.append(str(e))
            return False

    # Execute concurrent conflicting orders
    tasks = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for _ in range(5):
            tasks.append(pool.submit(place_order_task, u1["id"], "BUY", 10, 5.00))
            tasks.append(pool.submit(place_order_task, u2["id"], "SELL", 10, 5.00))
            tasks.append(pool.submit(place_order_task, u3["id"], "BUY", 10, 5.00))
            tasks.append(pool.submit(place_order_task, u4["id"], "SELL", 10, 5.00))

        for t in as_completed(tasks):
            t.result()

    # Zero deadlocks must be caught!
    assert len(deadlock_errors) == 0, f"Deadlock occurred during matching: {deadlock_errors}"


def test_cumulative_warehouse_capacity_enforcement(db_conn):
    """
    Bugfix 2 Verification:
    Verifies that the storage cap enforces the cumulative sum of all stored commodities
    (sum(amount_r) <= storage_cap_effective), distributing remaining space proportionally
    and logging discarded excess volume in user_catchups.
    """
    user = create_test_user(db_conn, "wh_cap", "DANZ")
    uid = user["id"]
    storage_cap = get_storage_cap(1)  # 1000.0

    with db_conn.cursor() as cur:
        # Fill inventory to 950 units total (350 wood, 350 stone, 250 grain)
        cur.execute("UPDATE inventories SET amount = 350.0 WHERE user_id = %s AND resource_type = 'wood'", (uid,))
        cur.execute("UPDATE inventories SET amount = 350.0 WHERE user_id = %s AND resource_type = 'stone'", (uid,))
        cur.execute("UPDATE inventories SET amount = 250.0 WHERE user_id = %s AND resource_type = 'grain'", (uid,))
        cur.execute("UPDATE inventories SET amount = 0.0 WHERE user_id = %s AND resource_type IN ('iron', 'cloth')", (uid,))

        # Set last calculated to 10 hours ago so each active building produces > 100 units (total > 300)
        ten_hours_ago = datetime.now(timezone.utc) - timedelta(hours=10)
        cur.execute("UPDATE inventories SET last_calculated_at = %s WHERE user_id = %s", (ten_hours_ago, uid))
        db_conn.commit()

        # Run production calculation
        res = calculate_offline_production(cur, uid, record_catchup=True)
        db_conn.commit()

        # 1. Total across all goods must not exceed storage cap
        cur.execute("SELECT COALESCE(SUM(amount), 0) AS total_amt FROM inventories WHERE user_id = %s", (uid,))
        total_stored = float(cur.fetchone()["total_amt"])
        assert total_stored <= storage_cap + 0.05  # Within rounding delta
        assert total_stored == pytest.approx(storage_cap, abs=0.5)

        # 2. Catch-up records excess lost due to global cap
        catchup = res["catchup"]
        assert catchup["total_lost"] > 0.0


def test_price_corridor_3tier_fallback(db_conn):
    """
    Bugfix 3 Verification:
    Tests the 3-tier price corridor reference price hierarchy:
    Tier 1: 24h-VWAP (when 24h volume > 0)
    Tier 2: Most recent trade price (when 24h volume is 0)
    Tier 3: Canonical base price (when no trades exist)
    """
    user_a = create_test_user(db_conn, "ptier_a", "DANZ")
    user_b = create_test_user(db_conn, "ptier_b", "DANZ")

    with db_conn.cursor() as cur:
        # Clean trades for cloth to test pure Tier 3 base price
        cur.execute("DELETE FROM trades WHERE resource_type = 'cloth'")
        floor_t3, ceil_t3, ref_t3 = get_price_corridor(cur, "cloth")
        assert ref_t3 == REFERENCE_PRICES["cloth"]  # 8.00 Taler
        assert floor_t3 == round(0.50 * 8.00, 2)
        assert ceil_t3 == round(2.00 * 8.00, 2)

        # Insert a trade from 48 hours ago at price 26.00 Taler (Tier 2 candidate)
        forty_eight_hours_ago = datetime.now(timezone.utc) - timedelta(hours=48)
        cur.execute(
            """
            INSERT INTO trades (buyer_id, seller_id, resource_type, amount, price, fee, executed_at)
            VALUES (%s, %s, 'cloth', 10.0, 26.00, 0.52, %s)
            """,
            (user_a["id"], user_b["id"], forty_eight_hours_ago),
        )
        db_conn.commit()

        # Zero volume in last 24h -> must pick up Tier 2 (26.00 Taler), NOT Tier 3 (8.00 Taler)
        floor_t2, ceil_t2, ref_t2 = get_price_corridor(cur, "cloth")
        assert ref_t2 == 26.00
        assert floor_t2 == 13.00
        assert ceil_t2 == 52.00

        # Now insert a fresh trade in the last 2 hours at price 20.00 Taler (Tier 1 candidate)
        two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
        cur.execute(
            """
            INSERT INTO trades (buyer_id, seller_id, resource_type, amount, price, fee, executed_at)
            VALUES (%s, %s, 'cloth', 10.0, 20.00, 0.40, %s)
            """,
            (user_a["id"], user_b["id"], two_hours_ago),
        )
        db_conn.commit()

        # Active 24h volume -> must pick up Tier 1 VWAP (20.00 Taler)
        floor_t1, ceil_t1, ref_t1 = get_price_corridor(cur, "cloth")
        assert ref_t1 == 20.00
        assert floor_t1 == 10.00
        assert ceil_t1 == 40.00


def test_auction_deadline_rejection(db_conn):
    """
    Bugfix 4 Verification:
    Ensures bids submitted after auction expiration (NOW() >= epoch_end_at)
    are strictly rejected with ValueError in the engine and HTTP 422 in the route.
    """
    user = create_test_user(db_conn, "auc_exp", "DANZ", balance=5000.0)
    uid = user["id"]

    with db_conn.cursor() as cur:
        # Create guild
        g_tag = f"X{uuid.uuid4().hex[:4].upper()}"
        guild = create_guild(cur, uid, f"DeadlineGuild_{uuid.uuid4().hex[:4]}", g_tag)
        gid = guild["guild_id"]
        cur.execute("UPDATE guild_bank SET balance = 2000.0 WHERE guild_id = %s", (gid,))
        ensure_kontor_auctions(cur)
        db_conn.commit()

        cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
        danz_id = cur.fetchone()["id"]
        cur.execute("SELECT id FROM kontor_auctions WHERE region_id = %s AND status = 'ACTIVE'", (danz_id,))
        auc_id = cur.fetchone()["id"]

        # Expire the auction by setting epoch_end_at in the past
        past_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        cur.execute("UPDATE kontor_auctions SET epoch_end_at = %s WHERE id = %s", (past_time, auc_id))
        db_conn.commit()

        # Engine rejection check
        with pytest.raises(ValueError, match="Auktion ist bereits abgelaufen"):
            place_kontor_auction_bid(cur, uid, auc_id, 300.0)
        db_conn.rollback()

    # HTTP route rejection check (HTTP 422)
    with TestClient(app) as client:
        token = create_session_token(uid, user["username"])
        client.cookies.set(settings.COOKIE_NAME, token)
        client.get("/auth/register")
        csrf_token = client.cookies.get("imperium_csrf")
        headers = {"X-CSRF-Token": csrf_token} if csrf_token else {}

        resp = client.post(
            f"/guilds/auctions/{auc_id}/bid",
            data={"bid_amount": 350.0},
            headers=headers,
        )
        assert resp.status_code == 422
        assert "Auktion ist bereits abgelaufen" in resp.text


def test_decimal_type_casting_scaling_costs(db_conn):
    """
    Bugfix 5 Verification:
    Tests building upgrades at high levels with fractional exponential cost scaling
    (e.g., 1.5 ** (level - 1)). Verifies zero TypeError exceptions when modifying
    PostgreSQL NUMERIC balances, inventories, and production rates.
    """
    user = create_test_user(db_conn, "dec_type", "DANZ", balance=50000.0)
    uid = user["id"]

    with db_conn.cursor() as cur:
        # Give ample resources
        cur.execute("UPDATE inventories SET amount = 5000.0 WHERE user_id = %s", (uid,))
        db_conn.commit()

        # Upgrade lumberjack multiple times through fractional multipliers
        for expected_lvl in range(2, 6):
            res = upgrade_building(cur, uid, "lumberjack")
            db_conn.commit()
            assert res["success"] is True
            assert res["new_level"] == expected_lvl

        # Check DB state
        cur.execute("SELECT level, production_rate FROM buildings WHERE user_id = %s AND building_type = 'lumberjack'", (uid,))
        b = cur.fetchone()
        assert b["level"] == 5
        assert float(b["production_rate"]) > 0.0


def test_zero_yield_building_initialization(db_conn):
    """
    Bugfix 6 Verification:
    Verifies that newly registered users in specialized regions initialize
    0.0-multiplier commodity buildings at level 0 with rate 0.0,
    leaving warehouse and active regional resources at level 1.
    """
    # 1. Danzig: iron=0.0, cloth=0.0 -> mine and weaver must be level 0
    danz_user = create_test_user(db_conn, "init_danz", "DANZ")
    with db_conn.cursor() as cur:
        cur.execute("SELECT building_type, level, production_rate FROM buildings WHERE user_id = %s", (danz_user["id"],))
        danz_blds = {r["building_type"]: r for r in cur.fetchall()}

        assert danz_blds["mine"]["level"] == 0
        assert float(danz_blds["mine"]["production_rate"]) == 0.0
        assert danz_blds["weaver"]["level"] == 0
        assert float(danz_blds["weaver"]["production_rate"]) == 0.0

        assert danz_blds["lumberjack"]["level"] == 1
        assert float(danz_blds["lumberjack"]["production_rate"]) > 0.0
        assert danz_blds["warehouse"]["level"] == 1

    # 2. Visby: grain=0.0, cloth=0.0 -> farm and weaver must be level 0
    visb_user = create_test_user(db_conn, "init_visb", "VISB")
    with db_conn.cursor() as cur:
        cur.execute("SELECT building_type, level, production_rate FROM buildings WHERE user_id = %s", (visb_user["id"],))
        visb_blds = {r["building_type"]: r for r in cur.fetchall()}

        assert visb_blds["farm"]["level"] == 0
        assert float(visb_blds["farm"]["production_rate"]) == 0.0
        assert visb_blds["weaver"]["level"] == 0
        assert float(visb_blds["weaver"]["production_rate"]) == 0.0

        assert visb_blds["mine"]["level"] == 1
        assert float(visb_blds["mine"]["production_rate"]) > 0.0
        assert visb_blds["warehouse"]["level"] == 1
