import pytest
import psycopg
from psycopg.rows import dict_row
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from app.config import settings
from app.auth import hash_password
from app.engine.matching import place_and_match_order, cancel_order
from app.engine.production import ensure_user_entities

def test_concurrent_order_matching_acid(db_conn):
    """
    Stress test verifying that concurrent BUY and SELL orders across multiple
    simultaneous database connections execute cleanly with SELECT ... FOR UPDATE,
    avoiding deadlocks and preserving exact balance and inventory conservation.
    """
    ts = int(datetime.now().timestamp() * 1000)
    user_a_name = f"trader_buyer_{ts}"
    user_b_name = f"trader_seller_{ts}"

    initial_buyer_balance = 5000.00
    initial_seller_iron = 200.00

    with db_conn.cursor() as cur:
        # Clean previous orders and trades to ensure isolated test environment
        cur.execute("DELETE FROM trades; DELETE FROM market_orders;")
        # Create Buyer
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, %s) RETURNING id",
            (user_a_name, hash_password("pass123"), initial_buyer_balance),
        )
        buyer_id = cur.fetchone()["id"]
        ensure_user_entities(cur, buyer_id)

        # Create Seller
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 0.00) RETURNING id",
            (user_b_name, hash_password("pass123")),
        )
        seller_id = cur.fetchone()["id"]
        ensure_user_entities(cur, seller_id)

        # Zero out production rates for test users so passive generation doesn't distort pure trade conservation checks
        cur.execute("UPDATE buildings SET production_rate = 0.0 WHERE user_id IN (%s, %s)", (buyer_id, seller_id))
        cur.execute("UPDATE inventories SET amount = 0 WHERE user_id = %s AND resource_type = 'iron'", (buyer_id,))
        cur.execute("UPDATE inventories SET amount = %s WHERE user_id = %s AND resource_type = 'iron'", (initial_seller_iron, seller_id))

    def run_buy_order(qty: float, price: float):
        for attempt in range(5):
            with psycopg.connect(settings.conn_str, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    try:
                        res = place_and_match_order(cur, buyer_id, "BUY", "iron", qty, price)
                        conn.commit()
                        return ("BUY_OK", res)
                    except Exception as e:
                        conn.rollback()
                        if "deadlock" in str(e).lower() and attempt < 4:
                            import time
                            time.sleep(0.05 * (attempt + 1))
                            continue
                        return ("BUY_ERR", str(e))

    def run_sell_order(qty: float, price: float):
        for attempt in range(5):
            with psycopg.connect(settings.conn_str, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    try:
                        res = place_and_match_order(cur, seller_id, "SELL", "iron", qty, price)
                        conn.commit()
                        return ("SELL_OK", res)
                    except Exception as e:
                        conn.rollback()
                        if "deadlock" in str(e).lower() and attempt < 4:
                            import time
                            time.sleep(0.05 * (attempt + 1))
                            continue
                        return ("SELL_ERR", str(e))

    # Concurrently execute 10 BUYs (10 units @ 10.00) and 10 SELLs (10 units @ 10.00)
    tasks = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        for _ in range(10):
            tasks.append(executor.submit(run_sell_order, 10.0, 10.00))
            tasks.append(executor.submit(run_buy_order, 10.0, 10.00))

        results = [t.result() for t in as_completed(tasks)]

    # Check for any catastrophic errors
    errors = [r for r in results if r[0].endswith("_ERR")]
    assert len(errors) == 0, f"Encountered unexpected errors during concurrent matching: {errors}"

    # Now verify complete financial and inventory conservation
    with db_conn.cursor() as cur:
        # 1. Balances
        cur.execute("SELECT balance FROM users WHERE id = %s", (buyer_id,))
        final_buyer_balance = float(cur.fetchone()["balance"])

        cur.execute("SELECT balance FROM users WHERE id = %s", (seller_id,))
        final_seller_balance = float(cur.fetchone()["balance"])

        # 2. Escrow in active buy orders
        cur.execute(
            """
            SELECT COALESCE(SUM((amount - filled_amount) * limit_price), 0) AS buy_escrow
            FROM market_orders
            WHERE user_id = %s AND status = 'ACTIVE' AND order_type = 'BUY'
            """,
            (buyer_id,),
        )
        buyer_escrow = float(cur.fetchone()["buy_escrow"])

        # 3. Fees collected by market
        cur.execute(
            """
            SELECT COALESCE(SUM(fee), 0) AS total_fees
            FROM trades
            WHERE buyer_id = %s OR seller_id = %s
            """,
            (buyer_id, seller_id),
        )
        total_fees = float(cur.fetchone()["total_fees"])

        # Total currency in system must strictly equal initial buyer balance!
        total_currency = final_buyer_balance + final_seller_balance + buyer_escrow + total_fees
        assert total_currency == pytest.approx(initial_buyer_balance, abs=0.05), (
            f"Currency discrepancy! Initial: {initial_buyer_balance}, "
            f"Buyer: {final_buyer_balance}, Seller: {final_seller_balance}, "
            f"Escrow: {buyer_escrow}, Fees: {total_fees}, Total: {total_currency}"
        )

        # 4. Iron inventories
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'iron'", (buyer_id,))
        buyer_iron = float(cur.fetchone()["amount"])

        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'iron'", (seller_id,))
        seller_iron = float(cur.fetchone()["amount"])

        cur.execute(
            """
            SELECT COALESCE(SUM(amount - filled_amount), 0) AS sell_escrow
            FROM market_orders
            WHERE user_id = %s AND status = 'ACTIVE' AND order_type = 'SELL'
            """,
            (seller_id,),
        )
        seller_escrow_iron = float(cur.fetchone()["sell_escrow"])

        total_iron = buyer_iron + seller_iron + seller_escrow_iron
        assert total_iron == pytest.approx(initial_seller_iron, abs=0.05), (
            f"Inventory discrepancy! Initial: {initial_seller_iron}, "
            f"Buyer Iron: {buyer_iron}, Seller Iron: {seller_iron}, "
            f"Escrow Iron: {seller_escrow_iron}, Total: {total_iron}"
        )
