from decimal import Decimal
from typing import Dict, Any, List, Optional
from app.config import settings
from app.engine.production import calculate_offline_production
from app.engine.notifications import create_notification

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
    
    try:
        amt_float = float(amount)
        if not amt_float.is_integer() or amt_float < 1:
            raise ValueError("Menge muss eine positive ganze Zahl ab 1 sein.")
        amount = int(amt_float)
    except (ValueError, TypeError) as e:
        raise ValueError("Menge muss eine positive ganze Zahl ab 1 sein.")

    limit_price = round(float(limit_price), 2)
    if limit_price <= 0:
        raise ValueError("Limit-Preis muss größer als 0 sein.")
    if limit_price > 1_000_000.0:
        raise ValueError("Limit-Preis darf maximal 1.000.000,00 Taler betragen.")


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

            # Dispatch trade notifications
            create_notification(
                cur,
                user_id=user_id,
                event_type="TRADE_EXECUTED",
                payload={
                    "role": "BUYER",
                    "trade_id": trade_rec["id"],
                    "resource_type": resource_type,
                    "amount": trade_qty,
                    "price": exec_price,
                    "total_value": trade_value,
                    "fee": 0.0,
                    "counterparty_id": seller_id,
                },
            )
            create_notification(
                cur,
                user_id=seller_id,
                event_type="TRADE_EXECUTED",
                payload={
                    "role": "SELLER",
                    "trade_id": trade_rec["id"],
                    "resource_type": resource_type,
                    "amount": trade_qty,
                    "price": exec_price,
                    "total_value": trade_value,
                    "payout": seller_payout,
                    "fee": fee,
                    "counterparty_id": user_id,
                },
            )

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

            # Dispatch trade notifications
            create_notification(
                cur,
                user_id=buyer_id,
                event_type="TRADE_EXECUTED",
                payload={
                    "role": "BUYER",
                    "trade_id": trade_rec["id"],
                    "resource_type": resource_type,
                    "amount": trade_qty,
                    "price": exec_price,
                    "total_value": trade_value,
                    "fee": 0.0,
                    "counterparty_id": user_id,
                },
            )
            create_notification(
                cur,
                user_id=user_id,
                event_type="TRADE_EXECUTED",
                payload={
                    "role": "SELLER",
                    "trade_id": trade_rec["id"],
                    "resource_type": resource_type,
                    "amount": trade_qty,
                    "price": exec_price,
                    "total_value": trade_value,
                    "payout": seller_payout,
                    "fee": fee,
                    "counterparty_id": buyer_id,
                },
            )

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
    Guarantees zero fund leakage using row-level locks.
    """
    cur.execute(
        """
        SELECT id, user_id, order_type, resource_type, amount, filled_amount, limit_price, status
        FROM market_orders
        WHERE id = %s
        FOR UPDATE
        """,
        (order_id,),
    )
    order = cur.fetchone()
    if not order:
        raise ValueError("Order nicht gefunden.")
    if order["user_id"] != user_id:
        raise PermissionError("Keine Berechtigung, fremde Orders zu stornieren.")
    if order["status"] != "ACTIVE":
        raise ValueError(f"Order kann nicht storniert werden (Status: {order['status']}).")

    unfilled = round(float(order["amount"]) - float(order["filled_amount"]), 2)
    if unfilled <= 0:
        cur.execute("UPDATE market_orders SET status = 'FILLED' WHERE id = %s", (order_id,))
        return {
            "success": True,
            "order_id": order_id,
            "message": "Order war bereits vollständig ausgeführt.",
            "refunded_amount": 0.0,
            "refunded_funds": 0.0,
        }

    # Atomic Refund with row-level updates
    if order["order_type"] == "BUY":
        refund_funds = round(unfilled * float(order["limit_price"]), 2)
        cur.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
        cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (refund_funds, user_id))
        refunded_amount = unfilled
        refunded_gold = refund_funds
    else:  # SELL
        cur.execute(
            """
            UPDATE inventories
            SET amount = amount + %s
            WHERE user_id = %s AND resource_type = %s
            """,
            (unfilled, user_id, order["resource_type"]),
        )
        refunded_amount = unfilled
        refunded_gold = 0.0

    cur.execute("UPDATE market_orders SET status = 'CANCELLED' WHERE id = %s", (order_id,))
    return {
        "success": True,
        "order_id": order_id,
        "order_type": order["order_type"],
        "resource_type": order["resource_type"],
        "refunded_amount": refunded_amount,
        "refunded_funds": refunded_gold,
    }

def get_market_statistics(cur, resource_type: str) -> Dict[str, Any]:
    """
    Computes market metrics for price discovery:
    - Last traded price
    - 24-hour Volume-Weighted Average Price (VWAP)
    - 24-hour total traded volume
    - Recent 10 executed trades
    """
    # 1. Last traded execution price
    cur.execute(
        """
        SELECT price, executed_at
        FROM trades
        WHERE resource_type = %s
        ORDER BY executed_at DESC
        LIMIT 1
        """,
        (resource_type,),
    )
    last_trade = cur.fetchone()
    last_price = float(last_trade["price"]) if last_trade else None

    # 2. 24h VWAP and Volume
    cur.execute(
        """
        SELECT 
            COALESCE(SUM(price * amount) / NULLIF(SUM(amount), 0), 0) AS vwap,
            COALESCE(SUM(amount), 0) AS volume_24h,
            COUNT(*) AS trade_count_24h
        FROM trades
        WHERE resource_type = %s AND executed_at >= NOW() - INTERVAL '24 HOURS'
        """,
        (resource_type,),
    )
    stat_row = cur.fetchone()
    vwap_val = float(stat_row["vwap"]) if stat_row else 0.0
    vol_24h = float(stat_row["volume_24h"]) if stat_row else 0.0
    trade_count_24h = int(stat_row["trade_count_24h"]) if stat_row else 0

    # 3. Last 10 executed trades for this resource
    cur.execute(
        """
        SELECT id, amount, price, fee, executed_at, buyer_id, seller_id
        FROM trades
        WHERE resource_type = %s
        ORDER BY executed_at DESC
        LIMIT 10
        """,
        (resource_type,),
    )
    raw_recent = cur.fetchall()
    recent_trades = []
    for r in raw_recent:
        r_amt = float(r["amount"])
        r_price = float(r["price"])
        recent_trades.append({
            "id": r["id"],
            "amount": r_amt,
            "price": r_price,
            "fee": float(r["fee"]),
            "total_value": round(r_amt * r_price, 2),
            "executed_at": r["executed_at"].strftime("%H:%M:%S") if r["executed_at"] else "",
        })

    return {
        "last_price": last_price,
        "vwap_24h": round(vwap_val, 2) if vwap_val > 0 else None,
        "volume_24h": round(vol_24h, 2),
        "trade_count_24h": trade_count_24h,
        "recent_trades": recent_trades,
    }

def get_order_book(cur, resource_type: str, current_user_id: int) -> Dict[str, Any]:
    """
    Fetches aggregated order book depth ladder for bids and asks,
    combining multiple orders at the same limit price into total volume,
    plus 24h VWAP, Last Price, and recent trades.
    """
    # 1. Aggregated Bids (highest price first)
    cur.execute(
        """
        SELECT 
            limit_price, 
            SUM(amount - filled_amount) AS total_amount, 
            COUNT(*) AS order_count,
            BOOL_OR(user_id = %s) AS has_own
        FROM market_orders
        WHERE resource_type = %s AND status = 'ACTIVE' AND order_type = 'BUY'
        GROUP BY limit_price
        ORDER BY limit_price DESC
        LIMIT 25
        """,
        (current_user_id, resource_type),
    )
    raw_bids = cur.fetchall()
    bids = []
    cum_bid = 0.0
    for r in raw_bids:
        amt = round(float(r["total_amount"]), 2)
        cum_bid = round(cum_bid + amt, 2)
        bids.append({
            "limit_price": float(r["limit_price"]),
            "total_amount": amt,
            "cumulative_amount": cum_bid,
            "order_count": int(r["order_count"]),
            "has_own": bool(r["has_own"]),
        })

    # 2. Aggregated Asks (lowest price first)
    cur.execute(
        """
        SELECT 
            limit_price, 
            SUM(amount - filled_amount) AS total_amount, 
            COUNT(*) AS order_count,
            BOOL_OR(user_id = %s) AS has_own
        FROM market_orders
        WHERE resource_type = %s AND status = 'ACTIVE' AND order_type = 'SELL'
        GROUP BY limit_price
        ORDER BY limit_price ASC
        LIMIT 25
        """,
        (current_user_id, resource_type),
    )
    raw_asks = cur.fetchall()
    asks = []
    cum_ask = 0.0
    for r in raw_asks:
        amt = round(float(r["total_amount"]), 2)
        cum_ask = round(cum_ask + amt, 2)
        asks.append({
            "limit_price": float(r["limit_price"]),
            "total_amount": amt,
            "cumulative_amount": cum_ask,
            "order_count": int(r["order_count"]),
            "has_own": bool(r["has_own"]),
        })

    # 3. User's active orders across all resources
    cur.execute(
        """
        SELECT id, resource_type, order_type, amount, filled_amount, limit_price, created_at
        FROM market_orders
        WHERE user_id = %s AND status = 'ACTIVE'
        ORDER BY created_at DESC
        LIMIT 50
        """,
        (current_user_id,),
    )
    my_orders = []
    for r in cur.fetchall():
        rem = round(float(r["amount"]) - float(r["filled_amount"]), 2)
        val = round(rem * float(r["limit_price"]), 2)
        my_orders.append({
            "id": r["id"],
            "resource_type": r["resource_type"],
            "order_type": r["order_type"],
            "amount": float(r["amount"]),
            "filled_amount": float(r["filled_amount"]),
            "remaining": rem,
            "limit_price": float(r["limit_price"]),
            "total_value": val,
            "is_current_resource": r["resource_type"] == resource_type,
            "created_at": r["created_at"],
        })

    # 4. Market discovery stats (VWAP, Last Price, Recent Trades)
    stats = get_market_statistics(cur, resource_type)

    return {
        "resource_type": resource_type,
        "bids": bids,
        "asks": asks,
        "my_orders": my_orders,
        "last_price": stats["last_price"],
        "vwap_24h": stats["vwap_24h"],
        "volume_24h": stats["volume_24h"],
        "trade_count_24h": stats["trade_count_24h"],
        "recent_trades": stats["recent_trades"],
    }

