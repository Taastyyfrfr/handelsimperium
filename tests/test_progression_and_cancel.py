import pytest
import psycopg
from psycopg.rows import dict_row
from datetime import datetime
from app.config import settings, BUILDING_CONFIG
from app.auth import hash_password
from app.engine.production import (
    ensure_user_entities,
    upgrade_building,
    get_upgrade_costs,
    get_storage_cap,
    calculate_offline_production,
)
from app.engine.matching import place_and_match_order, cancel_order, get_order_book

def test_building_upgrade_sufficient_resources(db_conn):
    """
    Verifies that upgrading a building with sufficient resources and currency
    deducts the exact amounts, increments the level, and scales the production rate.
    """
    ts = int(datetime.now().timestamp() * 1000)
    with db_conn.cursor() as cur:
        # Create test merchant with plentiful resources
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 1000.00) RETURNING id",
            (f"rich_builder_{ts}", hash_password("pass")),
        )
        user_id = cur.fetchone()["id"]
        ensure_user_entities(cur, user_id)
        
        # Give user 500 wood and 500 stone
        cur.execute("UPDATE inventories SET amount = 500.00 WHERE user_id = %s AND resource_type IN ('wood', 'stone')", (user_id,))
        
        # Calculate expected cost for Lumberjack Level 1 -> 2
        costs = get_upgrade_costs("lumberjack", 1)
        expected_wood_deduct = costs["wood"]
        expected_stone_deduct = costs["stone"]
        expected_gold_deduct = costs["balance"]

        # Perform atomic upgrade
        res = upgrade_building(cur, user_id, "lumberjack")
        assert res["success"] is True
        assert res["new_level"] == 2
        assert res["new_rate"] > BUILDING_CONFIG["lumberjack"]["base_rate"]

        # Verify DB state
        cur.execute("SELECT balance FROM users WHERE id = %s", (user_id,))
        new_balance = float(cur.fetchone()["balance"])
        assert new_balance == pytest.approx(1000.00 - expected_gold_deduct, abs=0.01)

        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'wood'", (user_id,))
        new_wood = float(cur.fetchone()["amount"])
        assert new_wood == pytest.approx(500.00 - expected_wood_deduct, abs=0.01)

        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'stone'", (user_id,))
        new_stone = float(cur.fetchone()["amount"])
        assert new_stone == pytest.approx(500.00 - expected_stone_deduct, abs=0.01)

def test_building_upgrade_insufficient_resources_rollback(db_conn):
    """
    Verifies that upgrading a building with insufficient resources aborts with ValueError
    and rolls back completely, leaving all inventories and balances unchanged.
    """
    ts = int(datetime.now().timestamp() * 1000)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 500.00) RETURNING id",
            (f"poor_builder_{ts}", hash_password("pass")),
        )
        user_id = cur.fetchone()["id"]
        ensure_user_entities(cur, user_id)
        
        # Set stone to 0 (insufficient) while wood has 50
        cur.execute("UPDATE inventories SET amount = 50.00 WHERE user_id = %s AND resource_type = 'wood'", (user_id,))
        cur.execute("UPDATE inventories SET amount = 0.00 WHERE user_id = %s AND resource_type = 'stone'", (user_id,))

    # Attempt upgrade in a new transaction
    with psycopg.connect(settings.conn_str, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            with pytest.raises(ValueError) as exc_info:
                upgrade_building(cur, user_id, "lumberjack")
            conn.rollback()
            assert "Nicht genügend Ressourcen" in str(exc_info.value)
            assert "stone" in str(exc_info.value)

    # Verify that balances and inventories remain 100% untouched
    with db_conn.cursor() as cur:
        cur.execute("SELECT balance FROM users WHERE id = %s", (user_id,))
        assert float(cur.fetchone()["balance"]) == pytest.approx(500.00, abs=0.01)

        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'wood'", (user_id,))
        assert float(cur.fetchone()["amount"]) == pytest.approx(50.00, abs=0.01)

        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'stone'", (user_id,))
        assert float(cur.fetchone()["amount"]) == pytest.approx(0.00, abs=0.01)

        cur.execute("SELECT level FROM buildings WHERE user_id = %s AND building_type = 'lumberjack'", (user_id,))
        assert cur.fetchone()["level"] == 1

def test_warehouse_upgrade_and_storage_expansion(db_conn):
    """
    Verifies that upgrading the warehouse expands global storage capacity
    for all trade goods according to the scaling curve.
    """
    ts = int(datetime.now().timestamp() * 1000)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 1000.00) RETURNING id",
            (f"warehouse_magnate_{ts}", hash_password("pass")),
        )
        user_id = cur.fetchone()["id"]
        ensure_user_entities(cur, user_id)

        # Baseline storage cap at Level 1 is 1000
        data_lvl1 = calculate_offline_production(cur, user_id)
        assert data_lvl1["storage_cap"] == 1000.0

        # Provide resources for warehouse upgrade
        cur.execute("UPDATE inventories SET amount = 500.00 WHERE user_id = %s", (user_id,))
        
        # Upgrade warehouse to Level 2
        res = upgrade_building(cur, user_id, "warehouse")
        assert res["success"] is True
        assert res["new_level"] == 2
        assert res["new_storage_cap"] == get_storage_cap(2)
        assert res["new_storage_cap"] == 1500.0

        # Verify offline production honors the new 1500 cap
        data_lvl2 = calculate_offline_production(cur, user_id)
        assert data_lvl2["storage_cap"] == 1500.0
        for inv in data_lvl2["inventories"]:
            assert inv["storage_cap"] == 1500.0

def test_order_cancellation_zero_leakage(db_conn):
    """
    Verifies that cancelling an active order releases escrowed funds or goods
    atomically with zero balance leakage and strict authorization enforcement.
    """
    ts = int(datetime.now().timestamp() * 1000)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 500.00) RETURNING id",
            (f"trader_cancel_a_{ts}", hash_password("pass")),
        )
        user_a = cur.fetchone()["id"]
        ensure_user_entities(cur, user_a)

        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 500.00) RETURNING id",
            (f"trader_cancel_b_{ts}", hash_password("pass")),
        )
        user_b = cur.fetchone()["id"]
        ensure_user_entities(cur, user_b)

        # 1. User A places BUY order: 20 cloth @ 10.00 Taler = 200.00 escrow
        buy_res = place_and_match_order(cur, user_a, "BUY", "cloth", 20.0, 10.0)
        buy_order_id = buy_res["order_id"]

        cur.execute("SELECT balance FROM users WHERE id = %s", (user_a,))
        assert float(cur.fetchone()["balance"]) == pytest.approx(300.00, abs=0.01)

        # User B attempts to cancel User A's order -> PermissionError
        with pytest.raises(PermissionError):
            cancel_order(cur, user_b, buy_order_id)

        # User A cancels their BUY order -> exact 200.00 refunded
        cancel_buy = cancel_order(cur, user_a, buy_order_id)
        assert cancel_buy["success"] is True
        assert cancel_buy["refunded_funds"] == 200.00

        cur.execute("SELECT balance FROM users WHERE id = %s", (user_a,))
        assert float(cur.fetchone()["balance"]) == pytest.approx(500.00, abs=0.01)

        # 2. User A places SELL order: 15 iron @ 20.00 Taler = 15 iron escrowed
        cur.execute("UPDATE inventories SET amount = 50.00 WHERE user_id = %s AND resource_type = 'iron'", (user_a,))
        sell_res = place_and_match_order(cur, user_a, "SELL", "iron", 15.0, 20.0)
        sell_order_id = sell_res["order_id"]

        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'iron'", (user_a,))
        assert float(cur.fetchone()["amount"]) == pytest.approx(35.00, abs=0.01)

        # Cancel SELL order -> exact 15 iron refunded
        cancel_sell = cancel_order(cur, user_a, sell_order_id)
        assert cancel_sell["success"] is True
        assert cancel_sell["refunded_amount"] == 15.00

        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'iron'", (user_a,))
        assert float(cur.fetchone()["amount"]) == pytest.approx(50.00, abs=0.01)

def test_aggregated_order_book_depth(db_conn):
    """
    Verifies that multiple orders placed at the exact same limit price
    combine into a single aggregated price level with accurate volume,
    cumulative depth, and order count.
    """
    ts = int(datetime.now().timestamp() * 1000)
    with db_conn.cursor() as cur:
        # Create 3 traders
        traders = []
        for i in range(3):
            cur.execute(
                "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 1000.00) RETURNING id",
                (f"agg_trader_{i}_{ts}", hash_password("pass")),
            )
            tid = cur.fetchone()["id"]
            ensure_user_entities(cur, tid)
            traders.append(tid)

        # Traders 0, 1, and 2 all place BUY orders for Grain at limit price 4.50
        # Quantities: 10, 25, 15 -> Total Volume = 50.00 at price 4.50
        place_and_match_order(cur, traders[0], "BUY", "grain", 10.0, 4.50)
        place_and_match_order(cur, traders[1], "BUY", "grain", 25.0, 4.50)
        place_and_match_order(cur, traders[2], "BUY", "grain", 15.0, 4.50)

        # Trader 0 also places a higher BUY order: 5.0 units @ 6.00 Taler
        place_and_match_order(cur, traders[0], "BUY", "grain", 5.0, 6.00)

        # Query aggregated order book from perspective of Trader 0
        book = get_order_book(cur, "grain", traders[0])
        bids = book["bids"]

        # Level 1: Price 6.00 (Volume 5.0, Count 1, Cumulative 5.0, has_own True)
        assert len(bids) >= 2
        lvl_top = next(b for b in bids if b["limit_price"] == 6.00)
        assert lvl_top["total_amount"] == 5.00
        assert lvl_top["order_count"] == 1
        assert lvl_top["has_own"] is True

        # Level 2: Price 4.50 (Aggregated Volume 50.0, Count 3, Cumulative 55.0, has_own True)
        lvl_agg = next(b for b in bids if b["limit_price"] == 4.50)
        assert lvl_agg["total_amount"] == 50.00
        assert lvl_agg["order_count"] == 3
        assert lvl_agg["cumulative_amount"] == 55.00
        assert lvl_agg["has_own"] is True
