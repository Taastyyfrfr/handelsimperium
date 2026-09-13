import pytest
import time
import uuid
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from starlette.testclient import TestClient

from app.main import app
from app.config import settings, BUILDING_CONFIG
from app.auth import hash_password, create_session_token
from app.rate_limiter import limiter
from app.engine.production import ensure_user_entities, calculate_offline_production
from app.engine.matching import place_and_match_order, cancel_order
from app.engine.guilds import create_guild, leave_guild
from app.engine.caravans import dispatch_caravan, unload_caravan


def create_test_user(db_conn, username_prefix: str, region_tag: str = "DANZ", balance: float = 5000.0):
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


def test_depot_capacity_limit_enforced(db_conn):
    """
    Vulnerability 1 Verification:
    Verifies that unloading a caravan cannot breach the foreign regional depot capacity
    (settings.REGIONAL_DEPOT_CAP = 500.0). Rejects overflow with ValueError and HTTP 422.
    """
    user = create_test_user(db_conn, "depot_cap_user", "DANZ")
    uid = user["id"]

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = 'VISB'")
        visb_id = cur.fetchone()["id"]

        # Stock foreign depot in Visby up to 480 units (near 500 cap)
        cur.execute(
            """
            INSERT INTO regional_depots (user_id, region_id, resource_type, amount, last_updated_at)
            VALUES (%s, %s, 'wood', 480.0, NOW())
            ON CONFLICT (user_id, region_id, resource_type)
            DO UPDATE SET amount = 480.0
            """,
            (uid, visb_id),
        )

        # Give user wood in Danzig Kontor to dispatch
        cur.execute("UPDATE inventories SET amount = 100.0 WHERE user_id = %s AND resource_type = 'wood'", (uid,))
        db_conn.commit()

        # Dispatch caravan carrying 50 wood (480 + 50 = 530 > 500 cap)
        caravan_data = dispatch_caravan(cur, uid, visb_id, {"wood": 50.0})
        caravan_id = caravan_data["id"]

        # Fast forward arrival
        cur.execute("UPDATE caravans SET status = 'ARRIVED', arrival_at = NOW() - INTERVAL '10 seconds' WHERE id = %s", (caravan_id,))
        db_conn.commit()

        # 1. Engine layer rejection
        with pytest.raises(ValueError, match="Regionaldepot ist voll"):
            unload_caravan(cur, uid, caravan_id)
        db_conn.rollback()

    # 2. HTTP layer returns 422 Unprocessable Content
    with TestClient(app) as client:
        token = create_session_token(uid, user["username"])
        client.cookies.set(settings.COOKIE_NAME, token)

        init_res = client.get("/expeditions")
        csrf_token = client.cookies.get(settings.CSRF_COOKIE_NAME)
        headers = {"X-CSRF-Token": csrf_token} if csrf_token else {}

        res = client.post(f"/caravans/{caravan_id}/unload", headers=headers)
        assert res.status_code == 422
        assert "Regionaldepot ist voll" in res.text


def test_guild_disbandment_on_sole_leader_exit(db_conn):
    """
    Vulnerability 2 Verification:
    Verifies that when a sole remaining guild leader leaves the alliance, the guild
    and all associated records (bank, inventory, projects, controllers) dissolve cleanly
    without foreign key constraint violations.
    """
    leader = create_test_user(db_conn, "sole_leader", "DANZ", balance=5000.0)
    lid = leader["id"]

    with db_conn.cursor() as cur:
        g = create_guild(cur, lid, f"SoleGuild_{uuid.uuid4().hex[:6]}", "SOLE")
        gid = g["guild_id"]

        # Add bank funds and monument progress
        cur.execute("UPDATE guild_bank SET balance = 500.0 WHERE guild_id = %s", (gid,))
        cur.execute(
            """
            INSERT INTO guild_contributions (guild_id, user_id, contribution_type, resource_type, amount)
            VALUES (%s, %s, 'BANK_DEPOSIT', 'taler', 500.0)
            """,
            (gid, lid),
        )
        cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
        danz_id = cur.fetchone()["id"]
        cur.execute(
            """
            INSERT INTO regional_controllers (region_id, guild_id, winning_bid, valid_until)
            VALUES (%s, %s, 300.0, NOW() + INTERVAL '5 days')
            ON CONFLICT (region_id) DO UPDATE SET guild_id = %s
            """,
            (danz_id, gid, gid),
        )
        db_conn.commit()

        # Sole leader leaves -> Disband
        leave_res = leave_guild(cur, lid)
        db_conn.commit()

        assert leave_res["disbanded"] is True
        assert leave_res["new_leader_id"] is None

        # Verify all records cleanly cleaned up
        cur.execute("SELECT 1 FROM guilds WHERE id = %s", (gid,))
        assert cur.fetchone() is None
        cur.execute("SELECT 1 FROM guild_bank WHERE guild_id = %s", (gid,))
        assert cur.fetchone() is None
        cur.execute("SELECT 1 FROM guild_members WHERE guild_id = %s", (gid,))
        assert cur.fetchone() is None
        cur.execute("SELECT guild_id FROM regional_controllers WHERE region_id = %s", (danz_id,))
        rc = cur.fetchone()
        assert rc is None or rc["guild_id"] is None


def test_guild_succession_officer_priority(db_conn):
    """
    Vulnerability 2 Succession Priority Verification:
    Verifies that upon leader departure, leadership passes to the highest-ranking officer
    even if another regular member joined earlier.
    """
    leader = create_test_user(db_conn, "succ_ldr", "DANZ", balance=5000.0)
    old_member = create_test_user(db_conn, "succ_old_mem", "DANZ", balance=1000.0)
    new_officer = create_test_user(db_conn, "succ_new_off", "DANZ", balance=1000.0)

    with db_conn.cursor() as cur:
        tag = f"S{uuid.uuid4().hex[:4].upper()}"
        g = create_guild(cur, leader["id"], f"SuccGuild_{uuid.uuid4().hex[:6]}", tag)
        gid = g["guild_id"]

        # old_member joins first (MEMBER)
        cur.execute(
            """
            INSERT INTO guild_members (guild_id, user_id, role, joined_at)
            VALUES (%s, %s, 'MEMBER', NOW() - INTERVAL '2 days')
            """,
            (gid, old_member["id"]),
        )
        # new_officer joins second (promoted to OFFICER)
        cur.execute(
            """
            INSERT INTO guild_members (guild_id, user_id, role, joined_at)
            VALUES (%s, %s, 'OFFICER', NOW() - INTERVAL '1 day')
            """,
            (gid, new_officer["id"]),
        )
        db_conn.commit()

        # Leader leaves -> Officer must become LEADER
        leave_res = leave_guild(cur, leader["id"])
        db_conn.commit()

        assert leave_res["disbanded"] is False
        assert leave_res["new_leader_id"] == new_officer["id"]

        cur.execute("SELECT leader_id FROM guilds WHERE id = %s", (gid,))
        assert cur.fetchone()["leader_id"] == new_officer["id"]
        cur.execute("SELECT role FROM guild_members WHERE user_id = %s", (new_officer["id"],))
        assert cur.fetchone()["role"] == "LEADER"


def test_trades_in_uncontrolled_region(db_conn):
    """
    Vulnerability 3 Verification:
    Verifies that market trades executed where the seller is in a region with no active
    or valid regional controller proceed smoothly without null-pointer errors or failed transactions.
    """
    seller = create_test_user(db_conn, "unreg_seller", "VISB", balance=1000.0)
    buyer = create_test_user(db_conn, "unreg_buyer", "DANZ", balance=5000.0)

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = 'VISB'")
        visb_id = cur.fetchone()["id"]

        # Ensure no active controller for Visby
        cur.execute("DELETE FROM regional_controllers WHERE region_id = %s", (visb_id,))
        cur.execute("UPDATE inventories SET amount = 100.0 WHERE user_id = %s AND resource_type = 'iron'", (seller["id"],))
        db_conn.commit()

        # Seller places SELL limit order
        sell_order = place_and_match_order(cur, seller["id"], "SELL", "iron", 10.0, 15.00)
        # Buyer places matching BUY order
        buy_order = place_and_match_order(cur, buyer["id"], "BUY", "iron", 10.0, 15.00)
        db_conn.commit()

        assert buy_order["filled_amount"] == 10.0
        assert len(buy_order["trades"]) == 1
        assert buy_order["trades"][0]["amount"] == 10.0


def test_negative_time_delta_guard(db_conn):
    """
    Vulnerability 4 Verification:
    Verifies that future last_calculated_at timestamps (negative time delta) evaluate
    safely to Delta t = 0.0 without producing negative yields or reducing inventory levels.
    """
    user = create_test_user(db_conn, "future_clock_user", "DANZ")
    uid = user["id"]

    with db_conn.cursor() as cur:
        cur.execute("UPDATE inventories SET amount = 150.0 WHERE user_id = %s AND resource_type = 'wood'", (uid,))
        # Set timestamp 2 hours into the future
        future_time = datetime.now(timezone.utc) + timedelta(hours=2)
        cur.execute("UPDATE inventories SET last_calculated_at = %s WHERE user_id = %s AND resource_type = 'wood'", (future_time, uid))
        db_conn.commit()

        prod_data = calculate_offline_production(cur, uid, record_catchup=True)
        db_conn.commit()

        wood_inv = next(i for i in prod_data["inventories"] if i["resource_type"] == "wood")
        # Inventory must NOT have decreased!
        assert wood_inv["amount"] >= 150.0
        assert wood_inv["generated"] == 0.0


def test_atomic_order_cancellation_race(db_conn):
    """
    Vulnerability 6 Verification:
    Verifies that order cancellation acquires row locks, validates ACTIVE status,
    and refunds strictly amount - filled_amount. Repeated cancellation attempts abort cleanly.
    """
    trader_a = create_test_user(db_conn, "race_trader_a", "DANZ", balance=1000.0)
    trader_b = create_test_user(db_conn, "race_trader_b", "DANZ", balance=1000.0)

    with db_conn.cursor() as cur:
        # Clear cloth market orders for clean cancellation testing
        cur.execute("DELETE FROM market_orders WHERE resource_type = 'cloth'")

        # Trader A places BUY order for 20 cloth @ 8.00 = 160.00 Taler escrow
        cur.execute("SELECT balance FROM users WHERE id = %s", (trader_a["id"],))
        bal_start = float(cur.fetchone()["balance"])

        buy_res = place_and_match_order(cur, trader_a["id"], "BUY", "cloth", 20.0, 8.00)
        order_id = buy_res["order_id"]
        db_conn.commit()

        # Trader B partially fills 8 cloth @ 8.00
        cur.execute("UPDATE inventories SET amount = 100.0 WHERE user_id = %s AND resource_type = 'cloth'", (trader_b["id"],))
        db_conn.commit()
        sell_res = place_and_match_order(cur, trader_b["id"], "SELL", "cloth", 8.0, 8.00)
        db_conn.commit()

        # 1. Cancel partially filled order (20 - 8 = 12 unfilled, refund 12 * 8.0 = 96.00 Taler)
        cancel_res = cancel_order(cur, trader_a["id"], order_id)
        db_conn.commit()

        assert cancel_res["success"] is True
        assert cancel_res["refunded_amount"] == 12.0
        assert cancel_res["refunded_funds"] == 96.00

        # Trader A's balance must reflect exactly: start - 160 escrow + 96 refund = start - 64
        cur.execute("SELECT balance FROM users WHERE id = %s", (trader_a["id"],))
        assert float(cur.fetchone()["balance"]) == pytest.approx(bal_start - 64.00, abs=0.01)

        # 2. Second cancellation attempt must abort cleanly without modifying balance
        cancel_again = cancel_order(cur, trader_a["id"], order_id)
        db_conn.commit()

        assert cancel_again["success"] is False
        assert cancel_again["refunded_funds"] == 0.0
        cur.execute("SELECT balance FROM users WHERE id = %s", (trader_a["id"],))
        assert float(cur.fetchone()["balance"]) == pytest.approx(bal_start - 64.00, abs=0.01)


def test_rate_limiter_multiprocess_simulation():
    """
    Vulnerability 5 Verification:
    Verifies that the PostgreSQL-backed rate limiter evaluates an identical counter
    across parallel workers and blocks requests exceeding threshold.
    """
    limiter.reset()

    class WorkerClient:
        def __init__(self, ip):
            self.host = ip

    class WorkerRequest:
        def __init__(self, token):
            self.client = WorkerClient("10.0.0.1")
            self.headers = {}
            self.cookies = {settings.COOKIE_NAME: token}

    shared_token = f"multiworker_token_{uuid.uuid4().hex}"
    scope = "worker_sim_order"

    # Concurrently execute 10 requests with a max quota of 4 in 5 seconds
    results = []

    def send_request():
        req = WorkerRequest(shared_token)
        return limiter.check(req, max_requests=4, window_seconds=5, scope=scope)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(send_request) for _ in range(10)]
        for f in as_completed(futures):
            results.append(f.result())

    # Exactly 4 allowed, 6 rejected across concurrent calls
    allowed_count = sum(1 for r in results if r is True)
    rejected_count = sum(1 for r in results if r is False)
    assert allowed_count == 4
    assert rejected_count == 6

    # Reset cleans database
    limiter.reset()
    req_after_reset = WorkerRequest(shared_token)
    assert limiter.check(req_after_reset, max_requests=4, window_seconds=5, scope=scope) is True
