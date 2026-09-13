from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from app.config import settings, REFERENCE_PRICES
from app.engine.production import calculate_offline_production
from app.engine.notifications import create_notification
from app.engine.guilds import has_guild_perk
from app.engine.auctions import credit_regional_trade_tax


def get_price_corridor(cur, resource_type: str) -> Tuple[float, float, float]:
    """
    Computes dynamic price corridor (volatility circuit breaker) with 3-tier fallback hierarchy:
    1. 24-Hour VWAP (if 24-hour trade volume > 0)
    2. Price of most recently executed trade for this commodity
    3. Canonical base price from REFERENCE_PRICES (only if no trade ever occurred)

    Price Floor = round(0.50 * ReferencePrice, 2)
    Price Ceiling = round(2.00 * ReferencePrice, 2)
    Returns (floor, ceiling, reference_price).
    """
    cur.execute(
        """
        SELECT 
            COALESCE(SUM(price * amount) / NULLIF(SUM(amount), 0), 0) AS vwap,
            COALESCE(SUM(amount), 0) AS volume_24h
        FROM trades
        WHERE resource_type = %s AND executed_at >= NOW() - INTERVAL '24 HOURS'
        """,
        (resource_type,),
    )
    row = cur.fetchone()
    vwap_val = float(row["vwap"]) if row and row["vwap"] is not None else 0.0
    vol_val = float(row["volume_24h"]) if row and row["volume_24h"] is not None else 0.0

    if vol_val > 0 and vwap_val > 0:
        ref_price = round(vwap_val, 2)
    else:
        # Tier 2: Last executed trade for this resource
        cur.execute(
            """
            SELECT price
            FROM trades
            WHERE resource_type = %s
            ORDER BY executed_at DESC, id DESC
            LIMIT 1
            """,
            (resource_type,),
        )
        last_trade = cur.fetchone()
        if last_trade and last_trade["price"] is not None:
            ref_price = round(float(last_trade["price"]), 2)
        else:
            # Tier 3: Hardcoded canonical base price
            ref_price = REFERENCE_PRICES.get(resource_type, 5.00)

    floor = round(0.50 * ref_price, 2)
    ceiling = round(2.00 * ref_price, 2)
    return floor, ceiling, ref_price


def place_and_match_order(
    cur,
    user_id: int,
    order_type: str,
    resource_type: str,
    amount: float,
    limit_price: float,
) -> Dict[str, Any]:
    """
    Atomically places a limit order and executes matches using deterministic lock ordering.
    Prevents PostgreSQL deadlocks by acquiring locks on users, orders, and inventories
    in strictly ascending numerical order.
    Deducts market fee (2.0% standard, 1.5% with Freihafen) on executed trades.
    Enforces dynamic price corridor [0.50 * VWAP, 2.00 * VWAP].
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

    # Volatility circuit breaker: validate against dynamic price corridor
    floor, ceiling, ref_price = get_price_corridor(cur, resource_type)
    if limit_price < floor or limit_price > ceiling:
        raise ValueError(
            f"Limitpreis liegt außerhalb der zulässigen Handelsspanne ({floor:.2f} - {ceiling:.2f} Taler)."
        )

    # 1. Discover potential opposing candidate orders without locks to identify all involved users and orders
    if order_type == "BUY":
        cur.execute(
            """
            SELECT id, user_id, amount, filled_amount, limit_price, created_at
            FROM market_orders
            WHERE resource_type = %s
              AND status = 'ACTIVE'
              AND order_type = 'SELL'
              AND limit_price <= %s
              AND user_id != %s
            ORDER BY limit_price ASC, created_at ASC
            """,
            (resource_type, limit_price, user_id),
        )
    else:  # SELL
        cur.execute(
            """
            SELECT id, user_id, amount, filled_amount, limit_price, created_at
            FROM market_orders
            WHERE resource_type = %s
              AND status = 'ACTIVE'
              AND order_type = 'BUY'
              AND limit_price >= %s
              AND user_id != %s
            ORDER BY limit_price DESC, created_at ASC
            """,
            (resource_type, limit_price, user_id),
        )
    candidates = cur.fetchall()

    # 2. Collect and sort all involved user IDs and order IDs in strictly ascending numerical order
    all_user_ids = sorted(list(set([user_id] + [c["user_id"] for c in candidates])))
    all_order_ids = sorted([c["id"] for c in candidates])

    # 3. Lock all involved user rows in deterministic numerical order (id ASC)
    cur.execute(
        """
        SELECT id, balance
        FROM users
        WHERE id = ANY(%s)
        ORDER BY id ASC
        FOR UPDATE
        """,
        (all_user_ids,),
    )
    locked_users = {u["id"]: u for u in cur.fetchall()}

    # 4. Lock candidate market orders in deterministic numerical order (id ASC)
    locked_orders = {}
    if all_order_ids:
        cur.execute(
            """
            SELECT id, user_id, order_type, resource_type, amount, filled_amount, limit_price, status, created_at
            FROM market_orders
            WHERE id = ANY(%s)
            ORDER BY id ASC
            FOR UPDATE
            """,
            (all_order_ids,),
        )
        locked_orders = {o["id"]: o for o in cur.fetchall()}

    # 5. Lock inventories for all involved users in deterministic order (user_id ASC)
    cur.execute(
        """
        SELECT user_id, resource_type, amount
        FROM inventories
        WHERE user_id = ANY(%s) AND resource_type = %s
        ORDER BY user_id ASC
        FOR UPDATE
        """,
        (all_user_ids, resource_type),
    )
    locked_invs = {inv["user_id"]: inv for inv in cur.fetchall()}

    # 6. Ensure taker user has fresh offline production calculated
    calculate_offline_production(cur, user_id)

    # 7. Escrow funds / goods from taker
    if order_type == "BUY":
        total_cost = Decimal(str(round(amount * limit_price, 2)))
        cur.execute("SELECT balance FROM users WHERE id = %s", (user_id,))
        user_bal_row = cur.fetchone()
        user_bal = Decimal(str(user_bal_row["balance"])) if user_bal_row else Decimal("0.00")
        if user_bal < total_cost:
            raise ValueError(
                f"Unzureichendes Guthaben! Erforderlich: {float(total_cost):.2f} Taler, Vorhanden: {float(user_bal):.2f} Taler."
            )
        cur.execute("UPDATE users SET balance = balance - %s WHERE id = %s", (total_cost, user_id))
    else:  # SELL
        cur.execute(
            "SELECT amount FROM inventories WHERE user_id = %s AND resource_type = %s",
            (user_id, resource_type),
        )
        inv_row = cur.fetchone()
        user_inv_amt = Decimal(str(inv_row["amount"])) if inv_row else Decimal("0.00")
        if user_inv_amt < Decimal(str(amount)):
            raise ValueError(
                f"Nicht genügend {resource_type}! Erforderlich: {amount:.2f}, Vorhanden: {float(user_inv_amt):.2f}."
            )
        cur.execute(
            "UPDATE inventories SET amount = amount - %s WHERE user_id = %s AND resource_type = %s",
            (Decimal(str(amount)), user_id, resource_type),
        )

    # 8. Insert new market order
    cur.execute(
        """
        INSERT INTO market_orders (user_id, order_type, resource_type, amount, filled_amount, limit_price, status)
        VALUES (%s, %s, %s, %s, 0.00, %s, 'ACTIVE')
        RETURNING id, created_at
        """,
        (user_id, order_type, resource_type, Decimal(str(amount)), Decimal(str(limit_price))),
    )
    new_order = cur.fetchone()
    new_order_id = new_order["id"]

    # 9. Filter and sort active opposing orders according to market priority rules
    valid_opposing = []
    for cand in candidates:
        cid = cand["id"]
        live = locked_orders.get(cid)
        if live and live["status"] == "ACTIVE":
            unfilled = Decimal(str(live["amount"])) - Decimal(str(live["filled_amount"]))
            if unfilled > 0:
                valid_opposing.append(live)

    # Priority sort:
    # BUY matches lowest SELL price first, then oldest
    # SELL matches highest BUY price first, then oldest
    if order_type == "BUY":
        valid_opposing.sort(key=lambda o: (float(o["limit_price"]), o["created_at"]))
    else:
        valid_opposing.sort(key=lambda o: (-float(o["limit_price"]), o["created_at"]))

    # 10. Execute matching iterations
    remaining_amount = Decimal(str(amount))
    filled_amount = Decimal("0.00")
    trades_executed = []

    for maker in valid_opposing:
        if remaining_amount <= 0:
            break

        maker_id = maker["id"]
        maker_unfilled = Decimal(str(maker["amount"])) - Decimal(str(maker["filled_amount"]))
        trade_qty = min(remaining_amount, maker_unfilled)
        if trade_qty <= 0:
            continue

        exec_price = Decimal(str(round(float(maker["limit_price"]), 2)))
        trade_value = Decimal(str(round(float(trade_qty * exec_price), 2)))

        if order_type == "BUY":
            buyer_id = user_id
            seller_id = maker["user_id"]
        else:
            buyer_id = maker["user_id"]
            seller_id = user_id

        seller_fee_rate = Decimal("0.015") if has_guild_perk(cur, seller_id, "FREIHAFEN") else Decimal(str(settings.MARKET_FEE_RATE))
        fee = Decimal(str(round(float(trade_value * seller_fee_rate), 2)))
        seller_payout = trade_value - fee

        # Refund price improvement to buyer if buyer's limit_price was higher
        if order_type == "BUY":
            price_delta = Decimal(str(round(limit_price - float(exec_price), 2)))
            if price_delta > 0:
                refund = Decimal(str(round(float(trade_qty * price_delta), 2)))
                cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (refund, buyer_id))

        # Credit seller balance with net payout
        cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (seller_payout, seller_id))

        # Deliver resources to buyer
        cur.execute(
            """
            UPDATE inventories
            SET amount = amount + %s
            WHERE user_id = %s AND resource_type = %s
            """,
            (trade_qty, buyer_id, resource_type),
        )

        # Record trade in trades table
        cur.execute(
            """
            INSERT INTO trades (buyer_id, seller_id, resource_type, amount, price, fee)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, executed_at
            """,
            (buyer_id, seller_id, resource_type, trade_qty, exec_price, fee),
        )
        trade_rec = cur.fetchone()

        # Regional tax dividend for controlling guild (0.5% of trade value)
        try:
            credit_regional_trade_tax(cur, seller_id, float(trade_value))
        except Exception:
            pass  # Fail-safe: trade execution must never fail due to tax dividend error

        # Update maker order
        new_maker_filled = Decimal(str(round(float(maker["filled_amount"]) + float(trade_qty), 2)))
        maker_status = "FILLED" if new_maker_filled >= Decimal(str(maker["amount"])) else "ACTIVE"
        cur.execute(
            "UPDATE market_orders SET filled_amount = %s, status = %s WHERE id = %s",
            (new_maker_filled, maker_status, maker_id),
        )

        remaining_amount = remaining_amount - trade_qty
        filled_amount = filled_amount + trade_qty

        trades_executed.append({
            "trade_id": trade_rec["id"],
            "buyer_id": buyer_id,
            "seller_id": seller_id,
            "amount": float(trade_qty),
            "price": float(exec_price),
            "fee": float(fee),
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
                "amount": float(trade_qty),
                "price": float(exec_price),
                "total_value": float(trade_value),
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
                "amount": float(trade_qty),
                "price": float(exec_price),
                "total_value": float(trade_value),
                "payout": float(seller_payout),
                "fee": float(fee),
                "counterparty_id": buyer_id,
            },
        )

    # 11. Final status for the newly placed order
    final_status = "FILLED" if filled_amount >= Decimal(str(amount)) else "ACTIVE"
    cur.execute(
        "UPDATE market_orders SET filled_amount = %s, status = %s WHERE id = %s",
        (filled_amount, final_status, new_order_id),
    )

    return {
        "order_id": new_order_id,
        "order_type": order_type,
        "resource_type": resource_type,
        "initial_amount": amount,
        "filled_amount": float(filled_amount),
        "remaining_amount": float(remaining_amount),
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
        return {
            "success": False,
            "order_id": order_id,
            "message": f"Order #{order_id} kann nicht storniert werden (Status: {order['status']}).",
            "refunded_amount": 0.0,
            "refunded_funds": 0.0,
        }

    unfilled = Decimal(str(order["amount"])) - Decimal(str(order["filled_amount"]))
    if unfilled <= 0:
        cur.execute("UPDATE market_orders SET status = 'FILLED' WHERE id = %s", (order_id,))
        return {
            "success": False,
            "order_id": order_id,
            "message": f"Order #{order_id} war bereits vollständig ausgeführt.",
            "refunded_amount": 0.0,
            "refunded_funds": 0.0,
        }

    # Atomic Refund with row-level updates
    if order["order_type"] == "BUY":
        refund_funds = Decimal(str(round(float(unfilled) * float(order["limit_price"]), 2)))
        cur.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
        cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (refund_funds, user_id))
        refunded_amount = float(unfilled)
        refunded_gold = float(refund_funds)
    else:  # SELL
        refund_goods = unfilled
        cur.execute(
            "SELECT amount FROM inventories WHERE user_id = %s AND resource_type = %s FOR UPDATE",
            (user_id, order["resource_type"]),
        )
        cur.execute(
            """
            UPDATE inventories
            SET amount = amount + %s
            WHERE user_id = %s AND resource_type = %s
            """,
            (refund_goods, user_id, order["resource_type"]),
        )
        refunded_amount = float(refund_goods)
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

