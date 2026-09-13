import pytest
import time
import uuid
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from starlette.testclient import TestClient

from app.main import app
from app.config import settings, BUILDING_CONFIG, SUPPORTED_RESOURCES, REFERENCE_PRICES
from app.auth import hash_password, create_session_token
from app.rate_limiter import limiter
from app.engine.production import ensure_user_entities, upgrade_building, get_effective_storage_cap
from app.engine.matching import place_and_match_order, get_price_corridor
from app.engine.ranking import compute_full_leaderboard, calculate_user_net_worth
from app.engine.caravans import dispatch_caravan, unload_caravan
from app.engine.auctions import ensure_active_auctions, get_kontor_auctions_overview


def create_test_merchant(db_conn, username_prefix: str, region_tag: str = "DANZ", balance: float = 5000.0):
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


def test_price_improvement_refund(db_conn):
    """
    Task 1 Verification:
    When an aggressive BUY order matches a resting SELL order at execution_price < buy_order.limit_price,
    the buyer is atomically refunded (buy_order.limit_price - execution_price) * trade_qty.
    """
    seller = create_test_merchant(db_conn, "price_seller", "DANZ", balance=1000.0)
    buyer = create_test_merchant(db_conn, "price_buyer", "DANZ", balance=1000.0)

    with db_conn.cursor() as cur:
        # Give seller 20 iron
        cur.execute("UPDATE inventories SET amount = 20.0 WHERE user_id = %s AND resource_type = 'iron'", (seller["id"],))
        # Clear resting iron orders
        cur.execute("DELETE FROM market_orders WHERE resource_type = 'iron'")
        db_conn.commit()

        # 1. Seller places resting SELL limit order for 10 iron @ 10.00 Taler
        sell_res = place_and_match_order(cur, seller["id"], "SELL", "iron", 10.0, 10.00)
        db_conn.commit()
        assert sell_res["status"] == "ACTIVE"

        # 2. Buyer places aggressive BUY order for 10 iron @ 15.00 Taler
        # Escrow initially deducted = 10 * 15.00 = 150.00 Taler
        # Execution price will be 10.00 Taler (resting order price)
        # Price improvement refund = (15.00 - 10.00) * 10 = 50.00 Taler
        # Net deduction for buyer should be exactly 100.00 Taler!
        buy_res = place_and_match_order(cur, buyer["id"], "BUY", "iron", 10.0, 15.00)
        db_conn.commit()

        assert buy_res["filled_amount"] == 10.0
        assert len(buy_res["trades"]) == 1
        trade = buy_res["trades"][0]
        assert trade["price"] == 10.00
        assert trade["price_improvement_refund"] == 50.00

        # Check buyer final balance: 1000.0 - 150.0 (escrow) + 50.0 (refund) = 900.0
        cur.execute("SELECT balance FROM users WHERE id = %s", (buyer["id"],))
        buyer_bal = float(cur.fetchone()["balance"])
        assert buyer_bal == pytest.approx(900.00, abs=0.01)

        # Check buyer inventory
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'iron'", (buyer["id"],))
        assert float(cur.fetchone()["amount"]) == 20.0  # 10 starter + 10 traded


def test_wash_trading_rejection(db_conn):
    """
    Task 1 Verification:
    Submitting an order that would cross and match against an existing resting order owned
    by the same user is strictly rejected with ValueError and HTTP 422 ("Eigenhandel ist an der Börse untersagt").
    """
    trader = create_test_merchant(db_conn, "wash_trader", "DANZ", balance=5000.0)
    uid = trader["id"]

    with db_conn.cursor() as cur:
        floor, ceiling, ref = get_price_corridor(cur, "wood")
        # Give user resources
        cur.execute("UPDATE inventories SET amount = 100.0 WHERE user_id = %s AND resource_type = 'wood'", (uid,))
        cur.execute("DELETE FROM market_orders WHERE resource_type = 'wood'")
        db_conn.commit()

        # 1. Trader places resting SELL order: 10 wood @ ref Taler
        place_and_match_order(cur, uid, "SELL", "wood", 10.0, ref)
        db_conn.commit()

        # 2. Engine Level: Trader attempts to place crossing BUY order: 5 wood @ ref Taler
        with pytest.raises(ValueError, match="Eigenhandel ist an der Börse untersagt"):
            place_and_match_order(cur, uid, "BUY", "wood", 5.0, ref)
        db_conn.rollback()

    # 3. HTTP Layer: Trader submits crossing order via POST /market/orders -> HTTP 422
    with TestClient(app) as client:
        token = create_session_token(uid, trader["username"])
        client.cookies.set(settings.COOKIE_NAME, token)

        init_res = client.get("/auth/register")
        csrf_token = client.cookies.get(settings.CSRF_COOKIE_NAME)
        headers = {"X-CSRF-Token": csrf_token} if csrf_token else {}

        res = client.post(
            "/market/orders",
            data={
                "order_type": "BUY",
                "resource_type": "wood",
                "amount": 5,
                "limit_price": ref,
            },
            headers=headers,
        )
        assert res.status_code == 422
        assert "Eigenhandel ist an der Börse untersagt" in res.text


def test_comprehensive_net_worth_valuation(db_conn):
    """
    Task 2 Verification:
    Verifies that net-worth valuation properly accounts for all 7 asset classes:
    1. Liquid balance
    2. Escrowed Taler in active BUY orders
    3. Warehouse inventories at VWAP/base price
    4. Escrowed commodities in active SELL orders
    5. In-transit & arrived caravan cargo
    6. Regional depot stockpiles
    7. Kontor building & warehouse sunk capital
    """
    merchant = create_test_merchant(db_conn, "nw_merchant", "DANZ", balance=2000.0)
    uid = merchant["id"]

    with db_conn.cursor() as cur:
        price_map = dict(REFERENCE_PRICES)  # wood:4, stone:5, iron:12, grain:3, cloth:8

        # 1. Upgrade lumberjack to level 2
        cur.execute("UPDATE inventories SET amount = 500.0 WHERE user_id = %s", (uid,))
        db_conn.commit()
        upgrade_building(cur, uid, "lumberjack")
        db_conn.commit()

        # 2. Place active BUY order: 10 cloth @ 8.00 = 80 Taler escrow
        cur.execute("DELETE FROM market_orders WHERE user_id = %s", (uid,))
        cur.execute("SELECT id FROM users WHERE id != %s LIMIT 1", (uid,))
        other_user = cur.fetchone()
        other_uid = other_user["id"] if other_user else uid + 999
        # Clear opposing cloth orders to ensure order rests
        cur.execute("DELETE FROM market_orders WHERE resource_type = 'cloth'")
        place_and_match_order(cur, uid, "BUY", "cloth", 10.0, 8.00)
        db_conn.commit()

        # 3. Place active SELL order: 10 stone @ 6.00 = 10 stone escrow
        cur.execute("DELETE FROM market_orders WHERE resource_type = 'stone'")
        place_and_match_order(cur, uid, "SELL", "stone", 10.0, 6.00)
        db_conn.commit()

        # 4. In-transit caravan: 15 iron
        cur.execute("SELECT id FROM regions WHERE tag = 'VISB'")
        visb_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO caravans (user_id, origin_region_id, destination_region_id, cargo, departure_at, arrival_at, status)
            VALUES (%s, 1, %s, '{"iron": 15.0}'::jsonb, NOW(), NOW() + INTERVAL '1 hour', 'EN_ROUTE')
            """,
            (uid, visb_id),
        )

        # 5. Regional depot stockpile: 20 grain in Visby
        cur.execute(
            """
            INSERT INTO regional_depots (user_id, region_id, resource_type, amount, last_updated_at)
            VALUES (%s, %s, 'grain', 20.0, NOW())
            ON CONFLICT (user_id, region_id, resource_type) DO UPDATE SET amount = 20.0
            """,
            (uid, visb_id),
        )
        db_conn.commit()

        # 6. Compute leaderboard and verify user entry
        leaderboard, prices = compute_full_leaderboard(cur)
        entry = next(e for e in leaderboard if e["user_id"] == uid)

        assert entry["escrow_taler"] == 80.00
        assert entry["escrow_balance"] == 80.00
        assert entry["escrow_commodity_value"] == pytest.approx(10.0 * prices["stone"], abs=0.1)
        assert entry["transit_commodity_value"] == pytest.approx(15.0 * prices["iron"], abs=0.1)
        assert entry["depot_commodity_value"] == pytest.approx(20.0 * prices["grain"], abs=0.1)
        assert entry["building_capital"] > 0.0

        # Total net worth must equal the sum of all elements
        expected_total = round(
            entry["liquid_balance"]
            + entry["escrow_balance"]
            + entry["commodity_value"]
            + entry["escrow_commodity_value"]
            + entry["transit_commodity_value"]
            + entry["depot_commodity_value"]
            + entry["building_capital"],
            2,
        )
        assert entry["total_net_worth"] == pytest.approx(expected_total, abs=0.05)


def test_prohibition_of_instant_teleportation_and_return_caravan(db_conn):
    """
    Task 3 Verification:
    - POST /caravans/depots/{region_id}/transfer route is removed (returns 404).
    - Return caravans can be dispatched from foreign depots back to home Kontor.
    - Cargo is deducted from regional_depots, takes transit time, and arrives at Kontor.
    - Unloading at home Kontor credits inventories, bounded by warehouse storage limits.
    """
    merchant = create_test_merchant(db_conn, "return_trader", "DANZ", balance=3000.0)
    uid = merchant["id"]

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
        danz_id = cur.fetchone()["id"]
        cur.execute("SELECT id FROM regions WHERE tag = 'VISB'")
        visb_id = cur.fetchone()["id"]

        # Stock foreign depot in Visby with 60 stone
        cur.execute(
            """
            INSERT INTO regional_depots (user_id, region_id, resource_type, amount, last_updated_at)
            VALUES (%s, %s, 'stone', 60.0, NOW())
            ON CONFLICT (user_id, region_id, resource_type) DO UPDATE SET amount = 60.0
            """,
            (uid, visb_id),
        )
        db_conn.commit()

        # 1. Verify instant transfer route is removed (HTTP 404)
        with TestClient(app) as client:
            token = create_session_token(uid, merchant["username"])
            client.cookies.set(settings.COOKIE_NAME, token)
            test_csrf = "csrf_token_test_123"
            client.cookies.set(settings.CSRF_COOKIE_NAME, test_csrf)
            headers = {"X-CSRF-Token": test_csrf}
            res_transfer = client.post(f"/caravans/depots/{visb_id}/transfer", headers=headers)
            assert res_transfer.status_code == 404

        # 2. Dispatch return caravan from Visby depot to Danzig home Kontor
        ret_caravan = dispatch_caravan(
            cur,
            user_id=uid,
            destination_region_id=danz_id,
            cargo={"stone": 40.0},
            origin_region_id=visb_id,
        )
        db_conn.commit()

        assert ret_caravan["is_return"] is True
        assert ret_caravan["origin_tag"] == "VISB"
        assert ret_caravan["dest_tag"] == "DANZ"
        assert ret_caravan["total_cargo"] == 40.0
        assert ret_caravan["status"] == "EN_ROUTE"

        # Verify depot was deducted: 60 - 40 = 20 remaining in Visby
        cur.execute(
            "SELECT amount FROM regional_depots WHERE user_id = %s AND region_id = %s AND resource_type = 'stone'",
            (uid, visb_id),
        )
        assert float(cur.fetchone()["amount"]) == 20.0

        # 3. Simulate arrival and unload at home Kontor
        caravan_id = ret_caravan["id"]
        cur.execute(
            "UPDATE caravans SET status = 'ARRIVED', arrival_at = NOW() - INTERVAL '10 seconds' WHERE id = %s",
            (caravan_id,),
        )
        db_conn.commit()

        # Record pre-unload stone in Kontor
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'stone'", (uid,))
        pre_stone = float(cur.fetchone()["amount"])

        # Unload return caravan
        unload_res = unload_caravan(cur, uid, caravan_id)
        db_conn.commit()

        assert unload_res["dest_tag"] == "DANZ"
        cur.execute("SELECT status FROM caravans WHERE id = %s", (caravan_id,))
        assert cur.fetchone()["status"] == "UNLOADED"

        # Verify stone was credited to home Kontor
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'stone'", (uid,))
        post_stone = float(cur.fetchone()["amount"])
        assert post_stone == pytest.approx(pre_stone + 40.0, abs=0.01)


def test_automated_rate_limit_cleanup(db_conn):
    """
    Task 4 Verification:
    Verifies that rate limit entries older than 1 hour are automatically or rolling-pruned
    from the rate_limits table to prevent unbounded database growth.
    """
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM rate_limits")
        # 1. Insert old record (2 hours ago)
        cur.execute(
            """
            INSERT INTO rate_limits (client_key, created_at)
            VALUES ('client_expired_key', NOW() - INTERVAL '2 hours')
            """
        )
        # 2. Insert fresh record (5 minutes ago)
        cur.execute(
            """
            INSERT INTO rate_limits (client_key, created_at)
            VALUES ('client_fresh_key', NOW() - INTERVAL '5 minutes')
            """
        )
        db_conn.commit()

    # 3. Execute rolling maintenance
    deleted = limiter.prune_expired()
    assert deleted >= 1

    with db_conn.cursor() as cur:
        cur.execute("SELECT client_key FROM rate_limits")
        remaining = [r["client_key"] for r in cur.fetchall()]
        assert "client_expired_key" not in remaining
        assert "client_fresh_key" in remaining


def test_idempotent_auction_initialization(db_conn):
    """
    Task 5 Verification:
    Verifies that ensure_active_auctions automatically creates active 7-day Kontor auctions
    with current_highest_bid = 0.0 for any region lacking one, and is fully idempotent.
    """
    with db_conn.cursor() as cur:
        cur.execute("SELECT id, name FROM regions WHERE tag = 'VISB'")
        visb = cur.fetchone()
        visb_id = visb["id"]

        # Delete active auction for Visby to test auto-creation
        cur.execute("DELETE FROM kontor_auctions WHERE region_id = %s AND status = 'ACTIVE'", (visb_id,))
        db_conn.commit()

        # Run ensure_active_auctions
        ensure_active_auctions(cur)
        db_conn.commit()

        # Verify active auction now exists with highest bid 0.0 and valid end_time
        cur.execute(
            """
            SELECT id, region_id, current_highest_bid, status, epoch_end_at, end_time
            FROM kontor_auctions
            WHERE region_id = %s AND status = 'ACTIVE'
            """,
            (visb_id,),
        )
        auction = cur.fetchone()
        assert auction is not None
        assert float(auction["current_highest_bid"]) == 0.0
        assert auction["status"] == "ACTIVE"
        assert auction["end_time"] is not None
        assert auction["end_time"] > datetime.now(timezone.utc) + timedelta(days=6)

        # Calling a second time must be completely idempotent (no duplicate rows)
        ensure_active_auctions(cur)
        db_conn.commit()

        cur.execute(
            "SELECT COUNT(*) AS count FROM kontor_auctions WHERE region_id = %s AND status = 'ACTIVE'",
            (visb_id,),
        )
        assert cur.fetchone()["count"] == 1
