import pytest
import uuid
import re
from datetime import datetime, timezone, timedelta
from starlette.testclient import TestClient

from app.main import app
from app.auth import create_session_token
from app.config import REFERENCE_PRICES, settings
from app.engine.production import ensure_user_entities
from app.engine.matching import place_and_match_order, get_price_corridor
from app.engine.convoys import (
    simulate_npc_convoys,
    liquidate_npc_convoy,
    get_arbitrage_routes,
    get_active_npc_convoys,
    get_or_create_npc_user,
)
from app.engine.charts import generate_price_chart_svg
from app.engine.tutorial import (
    ensure_user_tutorial,
    is_step_eligible,
    claim_tutorial_reward,
    TUTORIAL_STEPS,
)


def create_test_merchant(db_conn, suffix: str, region_tag: str = "DANZ", balance: float = 2000.0):
    """Helper to create an isolated merchant in a specific region."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = %s", (region_tag,))
        reg = cur.fetchone()
        region_id = reg["id"]

        cur.execute(
            """
            INSERT INTO users (username, password_hash, balance, region_id)
            VALUES (%s, 'dummy_hash', %s, %s)
            RETURNING id, username, balance, region_id
            """,
            (f"p11_{suffix}_{uuid.uuid4().hex[:6]}", balance, region_id),
        )
        user = cur.fetchone()
        ensure_user_entities(cur, user["id"])
        db_conn.commit()
        return user


def test_npc_convoy_routes_and_spawn(db_conn):
    """
    Verifies that arbitrage routes correctly identify surplus (>1.0x) to deficit (0.0x)
    pairs, and that simulate_npc_convoys maintains at least 4 active in-transit convoys.
    """
    with db_conn.cursor() as cur:
        routes = get_arbitrage_routes(cur)
        assert len(routes) >= 4, f"Expected at least 4 Hanseatic arbitrage routes, got {len(routes)}"

        for r in routes:
            assert r["origin_multiplier"] > 1.0
            assert r["origin_id"] != r["dest_id"]
            assert r["duration_seconds"] > 0
            assert r["resource_type"] in ["wood", "stone", "iron", "grain", "cloth"]

        # Simulate convoys to ensure at least 4 active
        sim_res = simulate_npc_convoys(cur, min_convoys=4)
        db_conn.commit()

        cur.execute("SELECT COUNT(*) AS cnt FROM npc_convoys WHERE status = 'IN_TRANSIT'")
        active_count = cur.fetchone()["cnt"]
        assert active_count >= 4, f"Expected >= 4 active convoys, found {active_count}"

        active_list = get_active_npc_convoys(cur)
        assert len(active_list) >= 4
        for c in active_list:
            assert c["cargo_amount"] > 0
            assert c["status"] == "IN_TRANSIT"
            assert c["dest_name"] is not None


def test_npc_convoy_liquidation_and_resting_sell(db_conn):
    """
    Verifies deterministic convoy liquidation on arrival:
    1. Arrived convoy matches against open player BUY orders within price corridor.
    2. Any remaining unfilled cargo is placed as a resting SELL order at 1.10 * RefPrice.
    3. Convoy status transitions to LIQUIDATED.
    """
    buyer = create_test_merchant(db_conn, "buyer_liq", region_tag="KOEL", balance=3000.0)

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
        orig_id = cur.fetchone()["id"]
        cur.execute("SELECT id FROM regions WHERE tag = 'KOEL'")
        dest_id = cur.fetchone()["id"]

        # Wood base reference price is 4.00 Taler; floor=2.00, ceiling=8.00
        floor, ceiling, ref_p = get_price_corridor(cur, "wood")

        # Clean existing market orders for wood to isolate liquidation test
        cur.execute("DELETE FROM market_orders WHERE resource_type = 'wood'")
        db_conn.commit()

        # Buyer places a limit BUY order for 20 wood at ref_price (4.00 Taler)
        place_and_match_order(
            cur,
            user_id=buyer["id"],
            order_type="BUY",
            resource_type="wood",
            amount=20,
            limit_price=ref_p,
        )
        db_conn.commit()

        # Buyer should have 1 active BUY order for 20 wood
        cur.execute(
            """
            SELECT id, amount, filled_amount, status
            FROM market_orders
            WHERE user_id = %s AND resource_type = 'wood' AND order_type = 'BUY'
            """,
            (buyer["id"],),
        )
        buy_order = cur.fetchone()
        assert buy_order["status"] == "ACTIVE"
        assert float(buy_order["filled_amount"]) == 0.0

        # Create an arrived NPC convoy carrying 50.00 wood
        cur.execute(
            """
            INSERT INTO npc_convoys (
                convoy_name, origin_region_id, destination_region_id,
                resource_type, cargo_amount, departure_at, arrival_at, status
            )
            VALUES (
                'Kogge Test Roland', %s, %s, 'wood', 50.00,
                NOW() - INTERVAL '1 hour', NOW() - INTERVAL '5 minutes', 'IN_TRANSIT'
            )
            RETURNING id
            """,
            (orig_id, dest_id),
        )
        convoy_id = cur.fetchone()["id"]
        db_conn.commit()

        # Execute liquidation
        liq_res = liquidate_npc_convoy(cur, convoy_id)
        db_conn.commit()

        assert liq_res["status"] == "LIQUIDATED"
        assert liq_res["trades_executed"] >= 1
        assert liq_res["unfilled_cargo"] == 30.00  # 50 - 20 = 30 remaining
        assert liq_res["resting_order_id"] is not None

        # Verify buyer's order was FILLED
        cur.execute("SELECT filled_amount, status FROM market_orders WHERE id = %s", (buy_order["id"],))
        updated_buy = cur.fetchone()
        assert updated_buy["status"] == "FILLED"
        assert float(updated_buy["filled_amount"]) == 20.0

        # Verify buyer received 20 wood in inventory
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'wood'", (buyer["id"],))
        assert float(cur.fetchone()["amount"]) >= 20.0

        # Verify resting SELL order for remaining 30.00 wood at 1.10 * ref_price
        cur.execute("SELECT * FROM market_orders WHERE id = %s", (liq_res["resting_order_id"],))
        resting_sell = cur.fetchone()
        assert resting_sell["order_type"] == "SELL"
        assert float(resting_sell["amount"]) == 30.00
        assert float(resting_sell["limit_price"]) == round(1.10 * ref_p, 2)
        assert resting_sell["status"] == "ACTIVE"


def test_svg_price_chart_generation(db_conn):
    """
    Verifies that the SVG price chart engine:
    1. Returns a valid neutral baseline reference when trade history is empty.
    2. Computes and plots valid polyline and VWAP markers when trades exist.
    3. Handles coordinates and viewBox dimensions strictly within bounds.
    """
    with db_conn.cursor() as cur:
        # 1. Empty trade history test using an unseeded commodity
        svg_empty = generate_price_chart_svg(cur, "silk")
        assert '<svg viewBox="0 0 300 80"' in svg_empty
        assert 'stroke-dasharray="4,4"' in svg_empty
        assert "Basis-Referenz:" in svg_empty

        # 2. Add trades to test polyline generation
        u1 = create_test_merchant(db_conn, "chart_seller", balance=5000.0)
        u2 = create_test_merchant(db_conn, "chart_buyer", balance=5000.0)

        # Insert 3 executed trades over trailing hours
        cur.execute(
            """
            INSERT INTO trades (buyer_id, seller_id, resource_type, amount, price, fee, executed_at)
            VALUES 
                (%s, %s, 'iron', 10.0, 11.50, 0.23, NOW() - INTERVAL '3 hours'),
                (%s, %s, 'iron', 15.0, 12.80, 0.38, NOW() - INTERVAL '2 hours'),
                (%s, %s, 'iron', 20.0, 12.00, 0.48, NOW() - INTERVAL '1 hour')
            """,
            (u2["id"], u1["id"], u2["id"], u1["id"], u2["id"], u1["id"]),
        )
        db_conn.commit()

        svg_active = generate_price_chart_svg(cur, "iron")
        assert '<svg viewBox="0 0 300 80"' in svg_active
        assert "<polyline points=" in svg_active
        assert "<polygon points=" in svg_active
        assert "VWAP" in svg_active

        # Extract points and ensure valid numbers within viewBox (x in [0, 300], y in [0, 80])
        match = re.search(r'<polyline points="([^"]+)"', svg_active)
        assert match is not None
        pts_str = match.group(1)
        pairs = pts_str.split()
        assert len(pairs) >= 3

        for p in pairs:
            x_str, y_str = p.split(",")
            x, y = float(x_str), float(y_str)
            assert 0.0 <= x <= 300.0, f"X coordinate {x} out of bounds"
            assert 0.0 <= y <= 80.0, f"Y coordinate {y} out of bounds"


def test_tutorial_step8_progression_and_claim(db_conn):
    """
    Verifies Tutorial Step 8 ("Marktanalyse & Flottenarbitrage"):
    - Requirement: Verifies at least 2 filled trades for the player.
    - Reward: 150.00 Taler and 30.00 Eisen.
    - Advances to completion (is_finished = True).
    """
    player = create_test_merchant(db_conn, "tut8_player", balance=500.0)
    counterparty = create_test_merchant(db_conn, "tut8_partner", balance=2000.0)

    with db_conn.cursor() as cur:
        tut = ensure_user_tutorial(cur, player["id"])

        # Manually advance player tutorial to step 8
        cur.execute(
            """
            UPDATE user_tutorials
            SET current_step = 8, completed_steps = '[1, 2, 3, 4, 5, 6, 7]'::jsonb, is_finished = FALSE
            WHERE user_id = %s
            """,
            (player["id"],),
        )
        db_conn.commit()

        # Step 8 requires >= 2 executed trades. Initially 0 trades:
        assert not is_step_eligible(cur, player["id"], 8)

        # Execute 1 trade:
        cur.execute(
            """
            INSERT INTO trades (buyer_id, seller_id, resource_type, amount, price, fee, executed_at)
            VALUES (%s, %s, 'wood', 10.0, 4.00, 0.80, NOW())
            """,
            (player["id"], counterparty["id"]),
        )
        db_conn.commit()
        assert not is_step_eligible(cur, player["id"], 8)

        # Execute 2nd trade:
        cur.execute(
            """
            INSERT INTO trades (buyer_id, seller_id, resource_type, amount, price, fee, executed_at)
            VALUES (%s, %s, 'stone', 5.0, 5.00, 0.50, NOW())
            """,
            (counterparty["id"], player["id"]),
        )
        db_conn.commit()
        assert is_step_eligible(cur, player["id"], 8)

        # Claim reward
        cur.execute("SELECT balance FROM users WHERE id = %s", (player["id"],))
        bal_before = float(cur.fetchone()["balance"])
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'iron'", (player["id"],))
        iron_before = float(cur.fetchone()["amount"])

        res = claim_tutorial_reward(cur, player["id"])
        db_conn.commit()

        assert res["claimed_step"] == 8
        assert res["is_finished"] is True

        cur.execute("SELECT balance FROM users WHERE id = %s", (player["id"],))
        bal_after = float(cur.fetchone()["balance"])
        assert bal_after == round(bal_before + 150.00, 2)

        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'iron'", (player["id"],))
        iron_after = float(cur.fetchone()["amount"])
        assert iron_after == round(iron_before + 30.00, 2)


def test_market_and_handbook_http_endpoints(db_conn):
    """
    Verifies that the market book endpoint renders the inline SVG price chart
    and the handbook endpoint renders Section 11.
    """
    user = create_test_merchant(db_conn, "http_p11")
    token = create_session_token(user["id"], user["username"])
    client = TestClient(app)
    client.cookies.set(settings.COOKIE_NAME, token)

    # 1. Market book endpoint renders SVG price chart
    resp = client.get("/market/book?resource=wood")
    assert resp.status_code == 200
    assert '<svg viewBox="0 0 300 80"' in resp.text
    assert "Preistrend &amp; 24h-VWAP" in resp.text or "Preistrend & 24h-VWAP" in resp.text

    # 2. Handbook endpoint renders Section 11
    hb_resp = client.get("/handbuch")
    assert hb_resp.status_code == 200
    assert "handbuch-flotten" in hb_resp.text
    assert "11. Autonome Hanseflotten &amp; Preischart-Analyse" in hb_resp.text or "11. Autonome Hanseflotten & Preischart-Analyse" in hb_resp.text
