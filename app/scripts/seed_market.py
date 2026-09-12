"""
Idempotent CLI Market Seeding Script for Handelsimperium.
Establishes liquidity for the initial player cohort by deploying standard reference
orders via a designated market-maker account ('gilde_haendler').

Usage:
    python -m app.scripts.seed_market
    python scripts/seed_market.py
"""
import sys
import os

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from app.database import get_db_connection, init_pool, close_pool
from app.auth import hash_password
from app.config import SUPPORTED_RESOURCES
from app.engine.production import ensure_user_entities
from app.engine.matching import place_and_match_order

# Standard baseline spreads (ref price, bid price, ask price, quantity)
REFERENCE_LIQUIDITY = {
    "wood": {"ref": 4.00, "bid": 3.80, "ask": 4.20, "qty": 20},
    "stone": {"ref": 5.00, "bid": 4.75, "ask": 5.25, "qty": 20},
    "iron": {"ref": 12.00, "bid": 11.40, "ask": 12.60, "qty": 15},
    "grain": {"ref": 3.00, "bid": 2.85, "ask": 3.15, "qty": 25},
    "cloth": {"ref": 8.00, "bid": 7.60, "ask": 8.40, "qty": 15},
}

def get_or_create_market_maker(cur, username: str = "gilde_haendler") -> int:
    """Ensures a market maker merchant exists with ample capital and commodities."""
    cur.execute("SELECT id FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    if row:
        mm_id = row["id"]
        # Ensure high balance and high inventory
        cur.execute("UPDATE users SET balance = 100000.00 WHERE id = %s", (mm_id,))
    else:
        pwd = hash_password("MarktMeister2026!")
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 100000.00) RETURNING id",
            (username, pwd),
        )
        mm_id = cur.fetchone()["id"]

    ensure_user_entities(cur, mm_id)
    for res in SUPPORTED_RESOURCES:
        cur.execute(
            """
            UPDATE inventories 
            SET amount = 5000.00 
            WHERE user_id = %s AND resource_type = %s
            """,
            (mm_id, res),
        )
    return mm_id

def seed_market(silent: bool = False, close_at_end: bool = False) -> dict:
    """Idempotently populates baseline buy/sell orders and initial trade history."""
    init_pool()
    orders_created = 0
    orders_skipped = 0
    trades_seeded = 0

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                mm_id = get_or_create_market_maker(cur, "gilde_haendler")
                partner_id = get_or_create_market_maker(cur, "hafenmeister")
                conn.commit()

                for res, cfg in REFERENCE_LIQUIDITY.items():
                    # 1. Seed initial trade if no trade history exists for this resource
                    cur.execute("SELECT COUNT(*) AS count FROM trades WHERE resource_type = %s", (res,))
                    t_count = cur.fetchone()["count"]
                    if t_count == 0:
                        cur.execute(
                            """
                            INSERT INTO trades (buyer_id, seller_id, resource_type, amount, price, fee, executed_at)
                            VALUES (%s, %s, %s, 10.00, %s, %s, NOW())
                            """,
                            (partner_id, mm_id, res, cfg["ref"], round(10.00 * cfg["ref"] * 0.02, 2)),
                        )
                        trades_seeded += 1
                        conn.commit()

                    # 2. Check and place baseline BUY order
                    cur.execute(
                        """
                        SELECT id FROM market_orders
                        WHERE user_id = %s AND resource_type = %s AND order_type = 'BUY' AND status = 'ACTIVE'
                        """,
                        (mm_id, res),
                    )
                    if cur.fetchone():
                        orders_skipped += 1
                    else:
                        place_and_match_order(
                            cur,
                            user_id=mm_id,
                            order_type="BUY",
                            resource_type=res,
                            amount=cfg["qty"],
                            limit_price=cfg["bid"],
                        )
                        orders_created += 1
                        conn.commit()

                    # 3. Check and place baseline SELL order
                    cur.execute(
                        """
                        SELECT id FROM market_orders
                        WHERE user_id = %s AND resource_type = %s AND order_type = 'SELL' AND status = 'ACTIVE'
                        """,
                        (mm_id, res),
                    )
                    if cur.fetchone():
                        orders_skipped += 1
                    else:
                        place_and_match_order(
                            cur,
                            user_id=mm_id,
                            order_type="SELL",
                            resource_type=res,
                            amount=cfg["qty"],
                            limit_price=cfg["ask"],
                        )
                        orders_created += 1
                        conn.commit()

        if not silent:
            print(f"✅ Market Seeding Complete: {orders_created} orders placed, {orders_skipped} skipped (already active), {trades_seeded} initial trades seeded.")

        return {
            "orders_created": orders_created,
            "orders_skipped": orders_skipped,
            "trades_seeded": trades_seeded,
        }
    finally:
        if close_at_end:
            close_pool()

if __name__ == "__main__":
    seed_market(silent=False, close_at_end=True)

