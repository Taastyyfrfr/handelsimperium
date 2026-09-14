import json
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

from app.config import (
    settings,
    SUPPORTED_RESOURCES,
    TRANSIT_SPEED_FACTOR,
    REFERENCE_PRICES,
)
from app.auth import hash_password
from app.engine.caravans import calculate_distance, calculate_travel_duration
from app.engine.matching import get_price_corridor
from app.engine.production import ensure_user_entities
from app.engine.notifications import create_notification
from app.engine.auctions import credit_regional_trade_tax

HANSE_CONVOY_NAMES = [
    "Kogge Roland von Bremen",
    "Holk St. Peter",
    "Bunte Kuh von Hamburg",
    "Karavelle Adler von Lübeck",
    "Galiot Fortuna",
    "Schoner Greif von Stralsund",
    "Kogge Wappen von Wismar",
    "Schnau Hoffnung",
    "Kogge Meerweib",
    "Holk Maria Magdalena",
    "Kogge König David",
    "Fregatte Frieden von Danzig",
]


def get_or_create_npc_user(cur, username: str = "hanse_flotte") -> int:
    """
    Ensures a dedicated Hanseatic NPC merchant user exists with sufficient balance
    and initial inventory allocations to conduct market transactions.
    """
    cur.execute("SELECT id FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    if row:
        return row["id"]

    pwd_hash = hash_password("HanseFlotte2026!")
    cur.execute(
        """
        INSERT INTO users (username, password_hash, balance)
        VALUES (%s, %s, 1000000.00)
        RETURNING id
        """,
        (username, pwd_hash),
    )
    npc_id = cur.fetchone()["id"]
    ensure_user_entities(cur, npc_id)
    return npc_id


def get_arbitrage_routes(cur) -> List[Dict[str, Any]]:
    """
    Discovers all valid surplus-to-deficit trade routes across the Hanseatic network:
    Origin region must have multiplier > 1.0, destination region must have multiplier == 0.0.
    """
    cur.execute("SELECT id, name, tag, coord_x, coord_y, resource_multipliers FROM regions ORDER BY id")
    regions = cur.fetchall()

    routes = []
    for orig in regions:
        orig_mults = orig["resource_multipliers"]
        if isinstance(orig_mults, str):
            orig_mults = json.loads(orig_mults)

        for dest in regions:
            if orig["id"] == dest["id"]:
                continue

            dest_mults = dest["resource_multipliers"]
            if isinstance(dest_mults, str):
                dest_mults = json.loads(dest_mults)

            for res in SUPPORTED_RESOURCES:
                orig_m = float(orig_mults.get(res, 1.0))
                dest_m = float(dest_mults.get(res, 1.0))

                if orig_m > 1.0 and dest_m == 0.0:
                    dist = calculate_distance(
                        orig["coord_x"], orig["coord_y"], dest["coord_x"], dest["coord_y"]
                    )
                    duration = calculate_travel_duration(dist)
                    routes.append({
                        "origin_id": orig["id"],
                        "origin_name": orig["name"],
                        "origin_tag": orig["tag"],
                        "dest_id": dest["id"],
                        "dest_name": dest["name"],
                        "dest_tag": dest["tag"],
                        "resource_type": res,
                        "distance": dist,
                        "duration_seconds": duration,
                        "origin_multiplier": orig_m,
                    })

    return routes


def liquidate_npc_convoy(cur, convoy_id: int) -> Dict[str, Any]:
    """
    Executes deterministic liquidation of an arrived NPC convoy:
    1. Matches incoming cargo against open player BUY orders within the dynamic price corridor.
    2. Any remaining unfilled cargo is placed as a resting SELL order at 1.10 * ReferencePrice(r).
    3. Updates convoy status to 'LIQUIDATED'.
    """
    cur.execute(
        """
        SELECT id, convoy_name, origin_region_id, destination_region_id,
               resource_type, cargo_amount, status
        FROM npc_convoys
        WHERE id = %s AND status = 'IN_TRANSIT'
        FOR UPDATE
        """,
        (convoy_id,),
    )
    convoy = cur.fetchone()
    if not convoy:
        return {"convoy_id": convoy_id, "status": "SKIPPED"}

    npc_id = get_or_create_npc_user(cur)
    resource_type = convoy["resource_type"]
    remaining_cargo = float(convoy["cargo_amount"])
    floor, ceiling, ref_price = get_price_corridor(cur, resource_type)

    # 1. Discover player BUY orders within the admissible dynamic price corridor
    cur.execute(
        """
        SELECT id, user_id, amount, filled_amount, limit_price
        FROM market_orders
        WHERE resource_type = %s
          AND order_type = 'BUY'
          AND status = 'ACTIVE'
          AND limit_price >= %s
          AND limit_price <= %s
          AND user_id != %s
          AND (amount - filled_amount) > 0
        ORDER BY limit_price DESC, created_at ASC
        FOR UPDATE
        """,
        (resource_type, floor, ceiling, npc_id),
    )
    candidate_orders = cur.fetchall()

    trades_executed = []
    for order in candidate_orders:
        if remaining_cargo <= 0:
            break

        order_remaining = float(order["amount"]) - float(order["filled_amount"])
        match_qty = round(min(remaining_cargo, order_remaining), 2)
        if match_qty <= 0:
            continue

        trade_price = float(order["limit_price"])
        fee = round(match_qty * trade_price * settings.MARKET_FEE_RATE, 2)
        net_proceeds = round((match_qty * trade_price) - fee, 2)

        # Record trade execution
        cur.execute(
            """
            INSERT INTO trades (buyer_id, seller_id, resource_type, amount, price, fee, executed_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
            """,
            (order["user_id"], npc_id, resource_type, match_qty, trade_price, fee),
        )
        trade_id = cur.fetchone()["id"]

        # Update buyer's market order
        new_filled = float(order["filled_amount"]) + match_qty
        new_status = "FILLED" if new_filled >= float(order["amount"]) else "ACTIVE"
        cur.execute(
            "UPDATE market_orders SET filled_amount = %s, status = %s WHERE id = %s",
            (new_filled, new_status, order["id"]),
        )

        # Deliver cargo to buyer's warehouse inventory
        cur.execute(
            """
            UPDATE inventories
            SET amount = amount + %s
            WHERE user_id = %s AND resource_type = %s
            """,
            (match_qty, order["user_id"], resource_type),
        )

        # Credit NPC seller balance
        cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (net_proceeds, npc_id))

        # Notify buyer
        create_notification(
            cur,
            user_id=order["user_id"],
            event_type="TRADE_EXECUTED",
            payload={
                "trade_id": trade_id,
                "order_id": order["id"],
                "resource_type": resource_type,
                "amount": match_qty,
                "price": trade_price,
                "total_taler": round(match_qty * trade_price, 2),
                "seller": "Hanseflotte",
            },
        )

        # Credit regional dividend if destination region is controlled by a guild
        cur.execute("UPDATE users SET region_id = %s WHERE id = %s", (convoy["destination_region_id"], npc_id))
        credit_regional_trade_tax(
            cur,
            seller_id=npc_id,
            trade_value=round(match_qty * trade_price, 2),
        )

        remaining_cargo = round(remaining_cargo - match_qty, 2)
        trades_executed.append({
            "trade_id": trade_id,
            "order_id": order["id"],
            "amount": match_qty,
            "price": trade_price,
        })

    # 2. Place remaining unfilled cargo as a resting SELL order at 1.10 * ReferencePrice(r)
    resting_order_id = None
    if remaining_cargo > 0:
        ask_price = round(1.10 * ref_price, 2)
        # Ensure ask_price adheres to ceiling
        ask_price = min(ask_price, ceiling)

        # Credit NPC inventory with remaining cargo for escrow
        cur.execute(
            """
            INSERT INTO inventories (user_id, resource_type, amount)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, resource_type)
            DO UPDATE SET amount = inventories.amount + EXCLUDED.amount
            """,
            (npc_id, resource_type, remaining_cargo),
        )

        cur.execute(
            """
            INSERT INTO market_orders (user_id, order_type, resource_type, amount, filled_amount, limit_price, status)
            VALUES (%s, 'SELL', %s, %s, 0.00, %s, 'ACTIVE')
            RETURNING id
            """,
            (npc_id, resource_type, remaining_cargo, ask_price),
        )
        resting_order_id = cur.fetchone()["id"]

    # 3. Mark convoy as LIQUIDATED
    cur.execute("UPDATE npc_convoys SET status = 'LIQUIDATED' WHERE id = %s", (convoy_id,))

    return {
        "convoy_id": convoy_id,
        "status": "LIQUIDATED",
        "trades_executed": len(trades_executed),
        "unfilled_cargo": remaining_cargo,
        "resting_order_id": resting_order_id,
    }


def simulate_npc_convoys(cur, min_convoys: int = 4) -> Dict[str, Any]:
    """
    Deterministic simulation executed on request cycles:
    1. Liquidates any arrived NPC convoys (arrival_at <= NOW()).
    2. Maintains at least `min_convoys` (default 4) active convoys in transit carrying surplus
       goods from regions with >1.0x multipliers to deficit regions (0.0x multiplier).
    """
    # 1. Liquidate arrived convoys
    cur.execute(
        """
        SELECT id FROM npc_convoys
        WHERE status = 'IN_TRANSIT' AND arrival_at <= NOW()
        ORDER BY arrival_at ASC
        """
    )
    arrived = cur.fetchall()
    liquidated_count = 0
    for row in arrived:
        res = liquidate_npc_convoy(cur, row["id"])
        if res.get("status") == "LIQUIDATED":
            liquidated_count += 1

    # 2. Check active convoy count
    cur.execute("SELECT COUNT(*) AS cnt FROM npc_convoys WHERE status = 'IN_TRANSIT'")
    active_count = cur.fetchone()["cnt"]

    spawned_count = 0
    if active_count < min_convoys:
        routes = get_arbitrage_routes(cur)
        if routes:
            needed = min_convoys - active_count
            for i in range(needed):
                route = routes[(active_count + i) % len(routes)]
                convoy_name = HANSE_CONVOY_NAMES[(active_count + i) % len(HANSE_CONVOY_NAMES)]
                cargo_amt = 75.00  # Standard Hanseatic NPC cargo batch

                cur.execute(
                    """
                    INSERT INTO npc_convoys (
                        convoy_name, origin_region_id, destination_region_id,
                        resource_type, cargo_amount, departure_at, arrival_at, status
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, NOW(), NOW() + (%s || ' seconds')::INTERVAL, 'IN_TRANSIT'
                    )
                    RETURNING id
                    """,
                    (
                        convoy_name,
                        route["origin_id"],
                        route["dest_id"],
                        route["resource_type"],
                        cargo_amt,
                        int(route["duration_seconds"]),
                    ),
                )
                spawned_count += 1

    return {
        "liquidated_count": liquidated_count,
        "spawned_count": spawned_count,
        "active_convoys": active_count + spawned_count,
    }


def get_active_npc_convoys(cur) -> List[Dict[str, Any]]:
    """Returns currently active NPC convoys for display and inspection."""
    cur.execute(
        """
        SELECT c.id, c.convoy_name, c.resource_type, c.cargo_amount,
               c.departure_at, c.arrival_at, c.status,
               ro.name AS origin_name, ro.tag AS origin_tag,
               rd.name AS dest_name, rd.tag AS dest_tag,
               EXTRACT(EPOCH FROM (c.arrival_at - NOW())) AS seconds_remaining,
               EXTRACT(EPOCH FROM (c.arrival_at - c.departure_at)) AS total_duration
        FROM npc_convoys c
        JOIN regions ro ON ro.id = c.origin_region_id
        JOIN regions rd ON rd.id = c.destination_region_id
        WHERE c.status = 'IN_TRANSIT'
        ORDER BY c.arrival_at ASC
        """
    )
    rows = cur.fetchall()
    results = []
    for r in rows:
        total = float(r["total_duration"]) if r["total_duration"] else 1.0
        rem = max(0.0, float(r["seconds_remaining"])) if r["seconds_remaining"] is not None else 0.0
        pct = min(100.0, max(0.0, round(((total - rem) / total) * 100.0, 1))) if total > 0 else 0.0
        results.append({
            "id": r["id"],
            "convoy_name": r["convoy_name"],
            "resource_type": r["resource_type"],
            "cargo_amount": float(r["cargo_amount"]),
            "departure_at": r["departure_at"],
            "arrival_at": r["arrival_at"],
            "origin_name": r["origin_name"],
            "origin_tag": r["origin_tag"],
            "dest_name": r["dest_name"],
            "dest_tag": r["dest_tag"],
            "status": r["status"],
            "seconds_remaining": int(rem),
            "progress_pct": pct,
        })
    return results
