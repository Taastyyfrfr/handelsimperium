from decimal import Decimal
from typing import Dict, Any, List, Optional
from app.config import settings
from app.engine.production import calculate_offline_production

def place_and_match_order(
    cur,
    user_id: int,
    order_type: str,
    resource_type: str,
    amount: float,
    limit_price: float,
) -> Dict[str, Any]:
    """
    Atomically places a limit order and executes matches using SELECT ... FOR UPDATE.
    Deducts a 2% market fee on executed trades.
    """
    order_type = order_type.upper()
    if order_type not in ("BUY", "SELL"):
        raise ValueError("Order-Typ muss BUY oder SELL sein.")
    
    amount = round(float(amount), 2)
    limit_price = round(float(limit_price), 2)
    
    if amount <= 0:
        raise ValueError("Menge muss größer als 0 sein.")
    if limit_price <= 0:
        raise ValueError("Limit-Preis muss größer als 0 sein.")

    # Always ensure user has fresh production calculated before placing order
    calculate_offline_production(cur, user_id)

    # 1. Escrow / Lock user funds or goods
    if order_type == "BUY":
        total_cost = round(amount * limit_price, 2)
        cur.execute("SELECT balance FROM users WHERE id = %s FOR UPDATE", (user_id,))
        user_row = cur.fetchone()
        if not user_row or float(user_row["balance"]) < total_cost:
            raise ValueError(
                f"Unzureichendes Guthaben! Erforderlich: {total_cost:.2f} Taler, Vorhanden: {float(user_row['balance'] if user_row else 0):.2f} Taler."
            )
        # Deduct escrow from buyer
        cur.execute("UPDATE users SET balance = balance - %s WHERE id = %s", (total_cost, user_id))

    else:  # SELL
        cur.execute(
            "SELECT amount FROM inventories WHERE user_id = %s AND resource_type = %s FOR UPDATE",
            (user_id, resource_type),
        )
        inv_row = cur.fetchone()
        if not inv_row or float(inv_row["amount"]) < amount:
            raise ValueError(
                f"Nicht genügend {resource_type}! Erforderlich: {amount:.2f}, Vorhanden: {float(inv_row['amount'] if inv_row else 0):.2f}."
            )
        # Deduct escrow goods from seller
        cur.execute(
            "UPDATE inventories SET amount = amount - %s WHERE user_id = %s AND resource_type = %s",
            (amount, user_id, resource_type),
        )

    # 2. Insert new market order
    cur.execute(
        """
        INSERT INTO market_orders (user_id, order_type, resource_type, amount, filled_amount, limit_price, status)
        VALUES (%s, %s, %s, %s, 0.00, %s, 'ACTIVE')
        RETURNING id, created_at
        """,
        (user_id, order_type, resource_type, amount, limit_price),
    )
    new_order = cur.fetchone()
    new_order_id = new_order["id"]

    # 3. Match against opposing active orders with FOR UPDATE lock
    remaining_amount = amount
    filled_amount = 0.0
    trades_executed = []

    if order_type == "BUY":
        # Match against SELL orders: lowest price first, then oldest
        cur.execute(
            """
            SELECT id, user_id, amount, filled_amount, limit_price
            FROM market_orders
            WHERE resource_type = %s
              AND status = 'ACTIVE'
              AND order_type = 'SELL'
              AND limit_price <= %s
              AND user_id != %s
            ORDER BY limit_price ASC, created_at ASC
            FOR UPDATE
            """,
            (resource_type, limit_price, user_id),
        )
        opposing_orders = cur.fetchall()

        for maker in opposing_orders:
            if remaining_amount <= 0:
                break

            maker_id = maker["id"]
            seller_id = maker["user_id"]
            maker_unfilled = float(maker["amount"]) - float(maker["filled_amount"])
            trade_qty = min(remaining_amount, maker_unfilled)
            if trade_qty <= 0:
                continue

            exec_price = float(maker["limit_price"])
            trade_value = round(trade_qty * exec_price, 2)
            fee = round(trade_value * settings.MARKET_FEE_RATE, 2)
            seller_payout = round(trade_value - fee, 2)

            # Refund price improvement to buyer if buyer's limit_price was higher
            price_delta = limit_price - exec_price
            if price_delta > 0:
                refund = round(trade_qty * price_delta, 2)
                cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (refund, user_id))

            # Prevent deadlocks by locking involved accounts in deterministic order (user_id ASC)
            first_user, second_user = sorted([user_id, seller_id])
            cur.execute("SELECT id FROM users WHERE id IN (%s, %s) ORDER BY id FOR UPDATE", (first_user, second_user))

            # Credit seller balance with net payout
            cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (seller_payout, seller_id))

            # Deliver resources to buyer
            cur.execute(
                """
                UPDATE inventories
                SET amount = amount + %s
                WHERE user_id = %s AND resource_type = %s
                """,
                (trade_qty, user_id, resource_type),
            )

            # Record trade in trades table
            cur.execute(
                """
                INSERT INTO trades (buyer_id, seller_id, resource_type, amount, price, fee)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, executed_at
                """,
                (user_id, seller_id, resource_type, trade_qty, exec_price, fee),
            )
            trade_rec = cur.fetchone()

            # Update maker order
            new_maker_filled = round(float(maker["filled_amount"]) + trade_qty, 2)
            maker_status = "FILLED" if new_maker_filled >= float(maker["amount"]) else "ACTIVE"
            cur.execute(
                "UPDATE market_orders SET filled_amount = %s, status = %s WHERE id = %s",
                (new_maker_filled, maker_status, maker_id),
            )

            remaining_amount = round(remaining_amount - trade_qty, 2)
            filled_amount = round(filled_amount + trade_qty, 2)

            trades_executed.append({
                "trade_id": trade_rec["id"],
                "seller_id": seller_id,
                "amount": trade_qty,
                "price": exec_price,
                "fee": fee,
            })

    else:  # SELL order matching against BUY orders
        # Match against BUY orders: highest price first, then oldest
        cur.execute(
            """
            SELECT id, user_id, amount, filled_amount, limit_price
            FROM market_orders
            WHERE resource_type = %s
              AND status = 'ACTIVE'
              AND order_type = 'BUY'
              AND limit_price >= %s
              AND user_id != %s
            ORDER BY limit_price DESC, created_at ASC
            FOR UPDATE
            """,
            (resource_type, limit_price, user_id),
        )
        opposing_orders = cur.fetchall()

        for maker in opposing_orders:
            if remaining_amount <= 0:
                break

            maker_id = maker["id"]
            buyer_id = maker["user_id"]
            maker_unfilled = float(maker["amount"]) - float(maker["filled_amount"])
            trade_qty = min(remaining_amount, maker_unfilled)
            if trade_qty <= 0:
                continue

            exec_price = float(maker["limit_price"])  # Maker price
            trade_value = round(trade_qty * exec_price, 2)
            fee = round(trade_value * settings.MARKET_FEE_RATE, 2)
            seller_payout = round(trade_value - fee, 2)

            # Prevent deadlocks by locking involved accounts in deterministic order (user_id ASC)
            first_user, second_user = sorted([user_id, buyer_id])
            cur.execute("SELECT id FROM users WHERE id IN (%s, %s) ORDER BY id FOR UPDATE", (first_user, second_user))

            # Credit seller balance with net payout
            cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (seller_payout, user_id))

            # Deliver resources to buyer
            cur.execute(
                """
                UPDATE inventories
                SET amount = amount + %s
                WHERE user_id = %s AND resource_type = %s
                """,
                (trade_qty, buyer_id, resource_type),
            )

            # Record trade
            cur.execute(
                """
                INSERT INTO trades (buyer_id, seller_id, resource_type, amount, price, fee)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, executed_at
                """,
                (buyer_id, user_id, resource_type, trade_qty, exec_price, fee),
            )
            trade_rec = cur.fetchone()

            # Update maker order
            new_maker_filled = round(float(maker["filled_amount"]) + trade_qty, 2)
            maker_status = "FILLED" if new_maker_filled >= float(maker["amount"]) else "ACTIVE"
            cur.execute(
                "UPDATE market_orders SET filled_amount = %s, status = %s WHERE id = %s",
                (new_maker_filled, maker_status, maker_id),
            )

            remaining_amount = round(remaining_amount - trade_qty, 2)
            filled_amount = round(filled_amount + trade_qty, 2)

            trades_executed.append({
                "trade_id": trade_rec["id"],
                "buyer_id": buyer_id,
                "amount": trade_qty,
                "price": exec_price,
                "fee": fee,
            })

    # 4. Final status for the newly placed order
    final_status = "FILLED" if filled_amount >= amount else "ACTIVE"
    cur.execute(
        "UPDATE market_orders SET filled_amount = %s, status = %s WHERE id = %s",
        (filled_amount, final_status, new_order_id),
    )

    return {
        "order_id": new_order_id,
        "order_type": order_type,
        "resource_type": resource_type,
        "initial_amount": amount,
        "filled_amount": filled_amount,
        "remaining_amount": remaining_amount,
        "limit_price": limit_price,
        "status": final_status,
        "trades": trades_executed,
    }

def cancel_order(cur, user_id: int, order_id: int) -> Dict[str, Any]:
    """
    Cancels an active limit order and refunds unfulfilled escrowed funds/goods atomically.
    """
    cur.execute(
        """
        SELECT id, user_id, order_type, resource_type, amount, filled_amount, limit_price, status
        FROM market_orders
        WHERE id = %s AND user_id = %s
        FOR UPDATE
        """,
        (order_id, user_id),
    )
    order = cur.fetchone()
    if not order:
        raise ValueError("Order nicht gefunden.")
    if order["status"] != "ACTIVE":
        raise ValueError(f"Order kann nicht storniert werden (Status: {order['status']}).")

    unfilled = round(float(order["amount"]) - float(order["filled_amount"]), 2)
    if unfilled <= 0:
        cur.execute("UPDATE market_orders SET status = 'FILLED' WHERE id = %s", (order_id,))
        return {"success": True, "message": "Order war bereits vollständig ausgeführt."}

    # Refund
    if order["order_type"] == "BUY":
        refund_funds = round(unfilled * float(order["limit_price"]), 2)
        cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (refund_funds, user_id))
    else:  # SELL
        cur.execute(
            """
            INSERT INTO inventories (user_id, resource_type, amount, last_calculated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id, resource_type)
            DO UPDATE SET amount = inventories.amount + EXCLUDED.amount
            """,
            (user_id, order["resource_type"], unfilled),
        )

    cur.execute("UPDATE market_orders SET status = 'CANCELLED' WHERE id = %s", (order_id,))
    return {"success": True, "order_id": order_id, "refunded_amount": unfilled}

def get_order_book(cur, resource_type: str, current_user_id: int) -> Dict[str, Any]:
    """Fetches active bids (BUY) and asks (SELL) for the order book."""
    # Top Bids: highest price first
    cur.execute(
        """
        SELECT id, user_id, amount, filled_amount, limit_price, created_at
        FROM market_orders
        WHERE resource_type = %s AND status = 'ACTIVE' AND order_type = 'BUY'
        ORDER BY limit_price DESC, created_at ASC
        LIMIT 25
        """,
        (resource_type,),
    )
    bids = []
    for r in cur.fetchall():
        bids.append({
            "id": r["id"],
            "amount": round(float(r["amount"]) - float(r["filled_amount"]), 2),
            "limit_price": float(r["limit_price"]),
            "is_own": r["user_id"] == current_user_id,
            "created_at": r["created_at"],
        })

    # Top Asks: lowest price first
    cur.execute(
        """
        SELECT id, user_id, amount, filled_amount, limit_price, created_at
        FROM market_orders
        WHERE resource_type = %s AND status = 'ACTIVE' AND order_type = 'SELL'
        ORDER BY limit_price ASC, created_at ASC
        LIMIT 25
        """,
        (resource_type,),
    )
    asks = []
    for r in cur.fetchall():
        asks.append({
            "id": r["id"],
            "amount": round(float(r["amount"]) - float(r["filled_amount"]), 2),
            "limit_price": float(r["limit_price"]),
            "is_own": r["user_id"] == current_user_id,
            "created_at": r["created_at"],
        })

    # User's active orders for this resource
    cur.execute(
        """
        SELECT id, order_type, amount, filled_amount, limit_price, created_at
        FROM market_orders
        WHERE user_id = %s AND status = 'ACTIVE'
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (current_user_id,),
    )
    my_orders = []
    for r in cur.fetchall():
        my_orders.append({
            "id": r["id"],
            "order_type": r["order_type"],
            "amount": float(r["amount"]),
            "filled_amount": float(r["filled_amount"]),
            "remaining": round(float(r["amount"]) - float(r["filled_amount"]), 2),
            "limit_price": float(r["limit_price"]),
            "created_at": r["created_at"],
        })

    return {
        "resource_type": resource_type,
        "bids": bids,
        "asks": asks,
        "my_orders": my_orders,
    }
