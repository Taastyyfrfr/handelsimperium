import pytest
import time
import uuid
from datetime import datetime, timezone, timedelta
from starlette.testclient import TestClient

from app.main import app
from app.auth import create_session_token
from app.config import REFERENCE_PRICES, settings
from app.engine.production import ensure_user_entities
from app.engine.matching import place_and_match_order, get_price_corridor
from app.engine.guilds import create_guild, join_guild
from app.engine.auctions import (
    ensure_kontor_auctions,
    resolve_kontor_auctions,
    deposit_to_guild_bank,
    place_kontor_auction_bid,
    get_kontor_auctions_overview,
    get_user_travel_speed_multiplier,
    credit_regional_trade_tax,
)
from app.engine.caravans import (
    dispatch_caravan,
    calculate_distance,
    calculate_travel_duration,
)
from app.engine.tutorial import (
    ensure_user_tutorial,
    is_step_eligible,
    claim_tutorial_reward,
)


def create_test_merchant(db_conn, username_suffix: str, region_tag: str = "DANZ", balance: float = 1000.0):
    """Helper to create a merchant in the specified region with initialized entities."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = %s", (region_tag,))
        reg = cur.fetchone()
        region_id = reg["id"]

        cur.execute(
            """
            INSERT INTO users (username, password_hash, balance, region_id)
            VALUES (%s, 'hash', %s, %s)
            RETURNING id, username, balance, region_id
            """,
            (f"merchant_{username_suffix}", balance, region_id),
        )
        user = cur.fetchone()
        uid = user["id"]
        ensure_user_entities(cur, uid)
        db_conn.commit()
        return user


def test_dynamic_price_band_rejection_and_acceptance(db_conn):
    """
    Verifies that limit orders outside [0.50 * VWAP, 2.00 * VWAP] are rejected
    with ValueError in matching engine and HTTP 422 via FastAPI route.
    """
    ts = int(time.time() * 1000)
    user = create_test_merchant(db_conn, f"pb_{ts}", "DANZ", balance=2000.0)
    uid = user["id"]

    with db_conn.cursor() as cur:
        # Give user plenty of inventory
        cur.execute("UPDATE inventories SET amount = 500.0 WHERE user_id = %s", (uid,))
        db_conn.commit()

        # Get corridor for wood
        floor, ceiling, ref = get_price_corridor(cur, "wood")
        assert floor == round(0.50 * ref, 2)
        assert ceiling == round(2.00 * ref, 2)

        # 1. Order below floor should raise ValueError
        with pytest.raises(ValueError, match="außerhalb der zulässigen Handelsspanne"):
            place_and_match_order(cur, uid, "SELL", "wood", 5.0, floor - 0.50)
        db_conn.rollback()

        # 2. Order above ceiling should raise ValueError
        with pytest.raises(ValueError, match="außerhalb der zulässigen Handelsspanne"):
            place_and_match_order(cur, uid, "SELL", "wood", 5.0, ceiling + 1.00)
        db_conn.rollback()

        # 3. Order within corridor should succeed
        order = place_and_match_order(cur, uid, "SELL", "wood", 5.0, ref)
        db_conn.commit()
        assert order["order_id"] is not None
        assert order["status"] in ("ACTIVE", "FILLED")
        cur.execute("DELETE FROM market_orders WHERE user_id = %s", (uid,))
        db_conn.commit()

    # 4. HTTP Route rejection test (HTTP 422)
    with TestClient(app) as client:
        token = create_session_token(uid, user["username"])
        client.cookies.set(settings.COOKIE_NAME, token)
        client.get("/auth/register")
        csrf_token = client.cookies.get("imperium_csrf")
        headers = {"X-CSRF-Token": csrf_token} if csrf_token else {}

        # Below floor -> 422
        resp_low = client.post(
            "/market/orders",
            data={"order_type": "BUY", "resource_type": "wood", "amount": 2.0, "limit_price": floor - 1.00},
            headers=headers,
        )
        assert resp_low.status_code == 422
        assert "Handelsspanne" in resp_low.text

        # Above ceiling -> 422
        resp_high = client.post(
            "/market/orders",
            data={"order_type": "BUY", "resource_type": "wood", "amount": 2.0, "limit_price": ceiling + 2.00},
            headers=headers,
        )
        assert resp_high.status_code == 422
        assert "Handelsspanne" in resp_high.text

        # Within corridor -> 200 OK
        resp_ok = client.post(
            "/market/orders",
            data={"order_type": "BUY", "resource_type": "wood", "amount": 2.0, "limit_price": ref},
            headers=headers,
        )
        assert resp_ok.status_code == 200


def test_guild_bank_deposit_and_contributions(db_conn):
    """
    Verifies that merchants can deposit Taler into the guild bank (War Chest),
    deducting from user balance and recording in guild_contributions.
    """
    ts = int(time.time() * 1000)
    user = create_test_merchant(db_conn, f"gb_{ts}", "DANZ", balance=1000.0)
    uid = user["id"]

    with db_conn.cursor() as cur:
        # Non-member cannot deposit
        with pytest.raises(ValueError, match="Mitglied einer Gilde"):
            deposit_to_guild_bank(cur, uid, 100.0)
        db_conn.rollback()

        # Create guild (costs 500 Taler)
        guild = create_guild(cur, uid, f"Gilde_{ts}", f"G{uuid.uuid4().hex[:4].upper()}")
        gid = guild["guild_id"]
        db_conn.commit()

        # Remaining balance: 1000 - 500 = 500
        cur.execute("SELECT balance FROM users WHERE id = %s", (uid,))
        assert float(cur.fetchone()["balance"]) == 500.0

        # Guild bank starts at 0
        cur.execute("SELECT balance FROM guild_bank WHERE guild_id = %s", (gid,))
        assert float(cur.fetchone()["balance"]) == 0.0

        # Deposit 200.00 Taler
        dep_res = deposit_to_guild_bank(cur, uid, 200.0)
        db_conn.commit()

        assert dep_res["amount"] == 200.0
        assert dep_res["new_bank_balance"] == 200.0

        # Check balances
        cur.execute("SELECT balance FROM users WHERE id = %s", (uid,))
        assert float(cur.fetchone()["balance"]) == 300.0

        cur.execute("SELECT balance FROM guild_bank WHERE guild_id = %s", (gid,))
        assert float(cur.fetchone()["balance"]) == 200.0

        # Check guild_contributions
        cur.execute(
            """
            SELECT contribution_type, amount FROM guild_contributions
            WHERE user_id = %s AND contribution_type = 'BANK_DEPOSIT'
            """,
            (uid,),
        )
        gc = cur.fetchone()
        assert gc is not None
        assert float(gc["amount"]) == 200.0

        # Over-deposit fails
        with pytest.raises(ValueError, match="Unzureichendes Taler-Guthaben"):
            deposit_to_guild_bank(cur, uid, 500.0)
        db_conn.rollback()


def test_kontor_auction_bidding_and_outbid_refund(db_conn):
    """
    Verifies Kontor auction bidding:
    - Guild A bids 300 -> deducted from Guild A bank.
    - Guild B outbids with 400 -> deducted from Guild B bank, Guild A bank refunded 300.
    - Same guild raising bid only pays the difference.
    - MEMBER cannot bid (only LEADER/OFFICER).
    """
    ts = int(time.time() * 1000)
    user_a = create_test_merchant(db_conn, f"auc_a_{ts}", "DANZ", balance=2000.0)
    user_b = create_test_merchant(db_conn, f"auc_b_{ts}", "VISB", balance=2000.0)
    user_c = create_test_merchant(db_conn, f"auc_c_{ts}", "DANZ", balance=1000.0)

    with db_conn.cursor() as cur:
        # Guild A and Guild B
        g_a = create_guild(cur, user_a["id"], f"HanseA_{ts}", f"A{uuid.uuid4().hex[:4].upper()}")
        g_b = create_guild(cur, user_b["id"], f"HanseB_{ts}", f"B{uuid.uuid4().hex[:4].upper()}")

        # User C joins Guild A as MEMBER
        join_guild(cur, user_c["id"], g_a["guild_id"])

        # Fund both guild banks
        cur.execute("UPDATE guild_bank SET balance = 1000.0 WHERE guild_id = %s", (g_a["guild_id"],))
        cur.execute("UPDATE guild_bank SET balance = 1000.0 WHERE guild_id = %s", (g_b["guild_id"],))
        db_conn.commit()

        # Ensure auctions exist
        ensure_kontor_auctions(cur)
        db_conn.commit()

        # Get Danzig auction
        cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
        danz_id = cur.fetchone()["id"]
        cur.execute("SELECT id FROM kontor_auctions WHERE region_id = %s AND status = 'ACTIVE'", (danz_id,))
        auc = cur.fetchone()
        auc_id = auc["id"]
        cur.execute("UPDATE kontor_auctions SET current_highest_bid = 0.0, highest_bidder_guild_id = NULL WHERE id = %s", (auc_id,))
        db_conn.commit()

        # 1. Member cannot bid
        with pytest.raises(ValueError, match="Nur Gildenleiter und Offiziere"):
            place_kontor_auction_bid(cur, user_c["id"], auc_id, 200.0)
        db_conn.rollback()

        # 2. Guild A leader bids 300
        bid1 = place_kontor_auction_bid(cur, user_a["id"], auc_id, 300.0)
        db_conn.commit()
        assert bid1["bid_amount"] == 300.0

        # Guild A bank deducted by 300 -> 700 left
        cur.execute("SELECT balance FROM guild_bank WHERE guild_id = %s", (g_a["guild_id"],))
        assert float(cur.fetchone()["balance"]) == 700.0

        # 3. Guild B leader outbids with 400
        bid2 = place_kontor_auction_bid(cur, user_b["id"], auc_id, 400.0)
        db_conn.commit()
        assert bid2["bid_amount"] == 400.0

        # Guild B bank deducted by 400 -> 600 left
        cur.execute("SELECT balance FROM guild_bank WHERE guild_id = %s", (g_b["guild_id"],))
        assert float(cur.fetchone()["balance"]) == 600.0

        # Guild A bank refunded 300 -> back to 1000.0
        cur.execute("SELECT balance FROM guild_bank WHERE guild_id = %s", (g_a["guild_id"],))
        assert float(cur.fetchone()["balance"]) == 1000.0

        # 4. Guild B raises its own bid to 500 (delta = 100)
        bid3 = place_kontor_auction_bid(cur, user_b["id"], auc_id, 500.0)
        db_conn.commit()
        assert bid3["bid_amount"] == 500.0

        # Guild B bank deducted only by delta (100) -> 600 - 100 = 500
        cur.execute("SELECT balance FROM guild_bank WHERE guild_id = %s", (g_b["guild_id"],))
        assert float(cur.fetchone()["balance"]) == 500.0


def test_auction_epoch_resolution_and_controller_assignment(db_conn):
    """
    Verifies deterministic settlement of expired auctions:
    - Auction marked RESOLVED
    - regional_controllers updated with winning guild for 7 days
    - Next ACTIVE auction epoch created
    """
    ts = int(time.time() * 1000)
    user = create_test_merchant(db_conn, f"res_{ts}", "VISB", balance=2000.0)
    uid = user["id"]

    with db_conn.cursor() as cur:
        guild = create_guild(cur, uid, f"VisbyGuild_{ts}", f"V{uuid.uuid4().hex[:4].upper()}")
        gid = guild["guild_id"]
        cur.execute("UPDATE guild_bank SET balance = 1000.0 WHERE guild_id = %s", (gid,))
        ensure_kontor_auctions(cur)
        db_conn.commit()

        cur.execute("SELECT id FROM regions WHERE tag = 'VISB'")
        visb_id = cur.fetchone()["id"]
        cur.execute("SELECT id FROM kontor_auctions WHERE region_id = %s AND status = 'ACTIVE'", (visb_id,))
        auc_id = cur.fetchone()["id"]

        # Place bid
        place_kontor_auction_bid(cur, uid, auc_id, 350.0)
        db_conn.commit()

        # Fast-forward auction expiration to the past
        cur.execute(
            "UPDATE kontor_auctions SET epoch_end_at = NOW() - INTERVAL '1 minute' WHERE id = %s",
            (auc_id,),
        )
        db_conn.commit()

        # Resolve
        count = resolve_kontor_auctions(cur)
        db_conn.commit()
        assert count >= 1

        # Check old auction is RESOLVED
        cur.execute("SELECT status FROM kontor_auctions WHERE id = %s", (auc_id,))
        assert cur.fetchone()["status"] == "RESOLVED"

        # Check regional_controllers has winning guild
        cur.execute(
            "SELECT guild_id, winning_bid, valid_until FROM regional_controllers WHERE region_id = %s",
            (visb_id,),
        )
        rc = cur.fetchone()
        assert rc is not None
        assert rc["guild_id"] == gid
        assert float(rc["winning_bid"]) == 350.0
        assert rc["valid_until"] > datetime.now(timezone.utc)

        # Check new ACTIVE auction exists
        cur.execute(
            "SELECT id, current_highest_bid, status FROM kontor_auctions WHERE region_id = %s AND status = 'ACTIVE'",
            (visb_id,),
        )
        new_auc = cur.fetchone()
        assert new_auc is not None
        assert float(new_auc["current_highest_bid"]) == 0.0


def test_controlling_guild_perks(db_conn):
    """
    Verifies territorial privileges of the controlling guild:
    - 25% transit time reduction (0.75x duration) for members
    - 0.5% trade tax dividend credited to controlling guild upon market execution
    """
    ts = int(time.time() * 1000)
    user_a = create_test_merchant(db_conn, f"perk_a_{ts}", "DANZ", balance=2000.0)
    user_b = create_test_merchant(db_conn, f"perk_b_{ts}", "VISB", balance=2000.0)

    with db_conn.cursor() as cur:
        guild = create_guild(cur, user_a["id"], f"Lords_{ts}", f"L{uuid.uuid4().hex[:4].upper()}")
        gid = guild["guild_id"]

        cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
        danz_id = cur.fetchone()["id"]
        cur.execute("SELECT id FROM regions WHERE tag = 'VISB'")
        visb_id = cur.fetchone()["id"]

        # Assign Danzig control to Guild A
        cur.execute(
            """
            INSERT INTO regional_controllers (region_id, guild_id, winning_bid, valid_until)
            VALUES (%s, %s, 500.0, NOW() + INTERVAL '7 days')
            ON CONFLICT (region_id) DO UPDATE SET guild_id = %s, valid_until = NOW() + INTERVAL '7 days'
            """,
            (danz_id, gid, gid),
        )
        db_conn.commit()

        # 1. Check speed multiplier
        speed_member = get_user_travel_speed_multiplier(cur, user_a["id"], danz_id, visb_id)
        assert speed_member == 0.75

        speed_non_member = get_user_travel_speed_multiplier(cur, user_b["id"], danz_id, visb_id)
        assert speed_non_member == 1.0

        # Verify dispatch_caravan applies the speed reduction
        cur.execute("UPDATE inventories SET amount = 100.0 WHERE user_id = %s AND resource_type = 'wood'", (user_a["id"],))
        c_res = dispatch_caravan(cur, user_a["id"], visb_id, {"wood": 10.0})
        db_conn.commit()

        base_dur = calculate_travel_duration(c_res["distance"])
        expected_dur = max(1, int(round(base_dur * 0.75)))
        assert c_res["duration_seconds"] == expected_dur

        # 2. Check 0.5% trade tax dividend
        # Guild bank balance starts at 0
        cur.execute("SELECT balance FROM guild_bank WHERE guild_id = %s", (gid,))
        bal_before = float(cur.fetchone()["balance"])

        # Execute trade with seller in Danzig: 10 units @ 15.00 = 150.00 Taler
        # Tax dividend: round(150.00 * 0.005, 2) = 0.75 Taler
        credit_regional_trade_tax(cur, seller_id=user_a["id"], trade_value=150.00)
        db_conn.commit()

        cur.execute("SELECT balance FROM guild_bank WHERE guild_id = %s", (gid,))
        bal_after = float(cur.fetchone()["balance"])
        assert bal_after == round(bal_before + 0.75, 2)


def test_tutorial_step7_completion(db_conn):
    """
    Verifies that Tutorial Step 7 ("Macht der Hanse") requires guild contribution
    or leadership, and awards 200.00 Taler + 40.00 Eisen upon claim.
    """
    ts = int(time.time() * 1000)
    user = create_test_merchant(db_conn, f"tut7_{ts}", "DANZ", balance=1000.0)
    uid = user["id"]

    with db_conn.cursor() as cur:
        # Initialize tutorial and set to step 7
        ensure_user_tutorial(cur, uid)
        cur.execute(
            "UPDATE user_tutorials SET current_step = 7, completed_steps = '[1,2,3,4,5,6]'::jsonb WHERE user_id = %s",
            (uid,),
        )
        db_conn.commit()

        # Step 7 not eligible initially
        assert is_step_eligible(cur, uid, 7) is False
        with pytest.raises(ValueError, match="Aufgabe noch nicht erfüllt"):
            claim_tutorial_reward(cur, uid)
        db_conn.rollback()

        # Found a guild -> immediately qualifies as guild leader
        g = create_guild(cur, uid, f"Pioneers_{ts}", f"P{uuid.uuid4().hex[:4].upper()}")
        db_conn.commit()
        assert is_step_eligible(cur, uid, 7) is True

        # Claim Step 7
        cur.execute("SELECT balance FROM users WHERE id = %s", (uid,))
        bal_before = float(cur.fetchone()["balance"])
        claim_res = claim_tutorial_reward(cur, uid)
        db_conn.commit()

        assert claim_res["claimed_step"] == 7
        assert claim_res["next_step"] == 8
        assert claim_res["is_finished"] is False

        # Verify reward
        cur.execute("SELECT balance FROM users WHERE id = %s", (uid,))
        assert float(cur.fetchone()["balance"]) == round(bal_before + 200.0, 2)
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'iron'", (uid,))
        assert float(cur.fetchone()["amount"]) >= 40.0


def test_phase10_http_views_and_actions(db_conn):
    """
    Verifies HTTP views and actions:
    - POST /guilds/bank/deposit deposits funds into guild treasury
    - POST /guilds/auctions/{id}/bid places an auction bid
    - GET /guilds renders Kontor-Auktionen section
    - GET /handbuch renders Section 9
    """
    ts = int(time.time() * 1000)
    user = create_test_merchant(db_conn, f"http10_{ts}", "DANZ", balance=2000.0)
    uid = user["id"]

    with db_conn.cursor() as cur:
        guild = create_guild(cur, uid, f"HttpGilde_{ts}", f"H{uuid.uuid4().hex[:4].upper()}")
        gid = guild["guild_id"]
        ensure_kontor_auctions(cur)
        db_conn.commit()

        cur.execute("SELECT id FROM kontor_auctions WHERE status = 'ACTIVE' LIMIT 1")
        auc_id = cur.fetchone()["id"]
        cur.execute("UPDATE kontor_auctions SET current_highest_bid = 0.0, highest_bidder_guild_id = NULL WHERE id = %s", (auc_id,))
        db_conn.commit()

    with TestClient(app) as client:
        token = create_session_token(uid, user["username"])
        client.cookies.set(settings.COOKIE_NAME, token)
        client.get("/auth/register")
        csrf_token = client.cookies.get("imperium_csrf")
        headers = {"X-CSRF-Token": csrf_token} if csrf_token else {}

        # 1. Test bank deposit
        resp_dep = client.post("/guilds/bank/deposit", data={"amount": 300.0}, headers=headers)
        assert resp_dep.status_code == 200
        assert "erfolgreich in die Gildenkasse eingezahlt" in resp_dep.text

        # 2. Test auction bid
        resp_bid = client.post(f"/guilds/auctions/{auc_id}/bid", data={"bid_amount": 150.0}, headers=headers)
        assert resp_bid.status_code == 200
        assert "erfolgreich abgegeben" in resp_bid.text

        # 3. GET /guilds contains Kontor-Auktionen
        resp_g = client.get("/guilds")
        assert resp_g.status_code == 200
        assert "Kontor-Auktionen" in resp_g.text

        # 4. GET /handbuch contains Section 9
        resp_h = client.get("/handbuch")
        assert resp_h.status_code == 200
        assert "Marktschutz" in resp_h.text
        assert "Preisspannen" in resp_h.text
