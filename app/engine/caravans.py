import math
import json
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

from app.config import (
    settings,
    CARAVAN_MAX_CARGO,
    TRANSIT_SPEED_FACTOR,
    SUPPORTED_RESOURCES,
)
from app.engine.production import get_effective_storage_cap
from app.engine.notifications import create_notification
from app.engine.auctions import get_user_travel_speed_multiplier


def calculate_distance(x1: int, y1: int, x2: int, y2: int) -> float:
    """Calculates Euclidean distance between two coordinate pairs rounded to 2 decimal places."""
    return round(math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2), 2)


def calculate_travel_duration(distance: float) -> int:
    """Calculates caravan travel duration in seconds based on distance and speed factor."""
    return int(round(distance * TRANSIT_SPEED_FACTOR))


def resolve_caravan_statuses(cur, user_id: int) -> int:
    """
    Checks all EN_ROUTE caravans for the given user.
    If current time >= arrival_at, transitions their status to ARRIVED
    and emits a CARAVAN_ARRIVED notification.
    """
    cur.execute(
        """
        SELECT c.id, c.origin_region_id, c.destination_region_id, c.cargo, c.arrival_at,
               ro.name AS origin_name, ro.tag AS origin_tag,
               rd.name AS dest_name, rd.tag AS dest_tag
        FROM caravans c
        JOIN regions ro ON ro.id = c.origin_region_id
        JOIN regions rd ON rd.id = c.destination_region_id
        WHERE c.user_id = %s AND c.status = 'EN_ROUTE' AND c.arrival_at <= NOW()
        FOR UPDATE OF c
        """,
        (user_id,),
    )
    arrived = cur.fetchall()
    count = 0
    for row in arrived:
        cur.execute("UPDATE caravans SET status = 'ARRIVED' WHERE id = %s", (row["id"],))
        cargo_data = row["cargo"] if isinstance(row["cargo"], dict) else json.loads(row["cargo"])
        create_notification(
            cur,
            user_id=user_id,
            event_type="CARAVAN_ARRIVED",
            payload={
                "caravan_id": row["id"],
                "origin_tag": row["origin_tag"],
                "dest_tag": row["dest_tag"],
                "dest_name": row["dest_name"],
                "cargo": cargo_data,
            },
        )
        count += 1
    return count


def dispatch_caravan(
    cur,
    user_id: int,
    destination_region_id: int,
    cargo: Dict[str, float],
) -> Dict[str, Any]:
    """
    Dispatches a new caravan expedition from user's home region to destination region.
    Deducts cargo atomically from user's inventory with row locks.
    Validates capacity cap (250 units) and positive amounts.
    """
    # 1. Resolve user's home region
    cur.execute(
        """
        SELECT u.id, u.region_id, r.name, r.tag, r.coord_x, r.coord_y
        FROM users u
        LEFT JOIN regions r ON r.id = u.region_id
        WHERE u.id = %s
        """,
        (user_id,),
    )
    user_row = cur.fetchone()
    if not user_row:
        raise ValueError("Benutzer nicht gefunden.")

    origin_region_id = user_row["region_id"]
    if not origin_region_id or not user_row["coord_x"]:
        # Fallback to Danzig if region unset
        cur.execute("SELECT id, name, tag, coord_x, coord_y FROM regions WHERE tag = 'DANZ'")
        danz = cur.fetchone()
        origin_region_id = danz["id"]
        cur.execute("UPDATE users SET region_id = %s WHERE id = %s", (origin_region_id, user_id))
        origin_x, origin_y = danz["coord_x"], danz["coord_y"]
        origin_tag = danz["tag"]
        origin_name = danz["name"]
    else:
        origin_x, origin_y = user_row["coord_x"], user_row["coord_y"]
        origin_tag = user_row["tag"]
        origin_name = user_row["name"]

    # 2. Validate destination region
    if origin_region_id == destination_region_id:
        raise ValueError("Zielregion muss sich von der Heimatregion unterscheiden.")

    cur.execute(
        "SELECT id, name, tag, coord_x, coord_y FROM regions WHERE id = %s",
        (destination_region_id,),
    )
    dest_region = cur.fetchone()
    if not dest_region:
        raise ValueError("Ungültige Zielregion ausgewählt.")

    # 3. Clean and validate cargo
    clean_cargo: Dict[str, float] = {}
    total_cargo = 0.0

    for res, raw_val in cargo.items():
        if res not in SUPPORTED_RESOURCES:
            continue
        try:
            val = float(raw_val)
        except (ValueError, TypeError):
            continue
        if val < 0:
            raise ValueError(f"Ungültige Menge für {res}: Negative Werte sind unzulässig.")
        if val > 0:
            clean_cargo[res] = round(val, 2)
            total_cargo += clean_cargo[res]

    total_cargo = round(total_cargo, 2)
    if total_cargo <= 0:
        raise ValueError("Mindestens eine Handelsware muss in die Karawane verladen werden.")

    if total_cargo > CARAVAN_MAX_CARGO:
        raise ValueError(
            f"Ladekapazität überschritten: Eine Karawane kann maximal {CARAVAN_MAX_CARGO:.0f} Güter transportieren (gewählt: {total_cargo:.2f})."
        )

    # 4. Atomic inventory verification & deduction
    for res, amount in clean_cargo.items():
        cur.execute(
            "SELECT amount FROM inventories WHERE user_id = %s AND resource_type = %s FOR UPDATE",
            (user_id, res),
        )
        inv_row = cur.fetchone()
        current_amount = float(inv_row["amount"]) if inv_row else 0.0
        if current_amount < amount:
            raise ValueError(
                f"Nicht genügend {res} im Kontor vorhanden (benötigt: {amount:.2f}, vorhanden: {current_amount:.2f})."
            )

    for res, amount in clean_cargo.items():
        cur.execute(
            """
            UPDATE inventories
            SET amount = amount - %s
            WHERE user_id = %s AND resource_type = %s
            """,
            (Decimal(str(round(amount, 2))), user_id, res),
        )

    # 5. Calculate transit duration and arrival timestamp
    distance = calculate_distance(origin_x, origin_y, dest_region["coord_x"], dest_region["coord_y"])
    base_duration = calculate_travel_duration(distance)
    speed_mult = get_user_travel_speed_multiplier(cur, user_id, origin_region_id, destination_region_id)
    duration_seconds = max(1, int(round(base_duration * speed_mult)))
    departure_at = datetime.now(timezone.utc)
    arrival_at = departure_at + timedelta(seconds=duration_seconds)

    # 6. Insert caravan record
    cur.execute(
        """
        INSERT INTO caravans (user_id, origin_region_id, destination_region_id, cargo, departure_at, arrival_at, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'EN_ROUTE')
        RETURNING id, departure_at, arrival_at, status
        """,
        (
            user_id,
            origin_region_id,
            destination_region_id,
            json.dumps(clean_cargo),
            departure_at,
            arrival_at,
        ),
    )
    new_caravan = cur.fetchone()

    # 7. Create notification
    create_notification(
        cur,
        user_id=user_id,
        event_type="CARAVAN_DISPATCHED",
        payload={
            "caravan_id": new_caravan["id"],
            "origin_tag": origin_tag,
            "dest_tag": dest_region["tag"],
            "dest_name": dest_region["name"],
            "cargo": clean_cargo,
            "distance": distance,
            "duration_seconds": duration_seconds,
        },
    )

    return {
        "id": new_caravan["id"],
        "origin_tag": origin_tag,
        "origin_name": origin_name,
        "dest_tag": dest_region["tag"],
        "dest_name": dest_region["name"],
        "cargo": clean_cargo,
        "total_cargo": total_cargo,
        "distance": distance,
        "duration_seconds": duration_seconds,
        "departure_at": departure_at,
        "arrival_at": arrival_at,
        "status": new_caravan["status"],
    }


def unload_caravan(cur, user_id: int, caravan_id: int) -> Dict[str, Any]:
    """
    Unloads an ARRIVED caravan into the user's regional depot at destination region.
    Transitions status to UNLOADED.
    """
    # Resolve any pending arrivals first
    resolve_caravan_statuses(cur, user_id)

    cur.execute(
        """
        SELECT c.id, c.user_id, c.destination_region_id, c.cargo, c.status,
               rd.name AS dest_name, rd.tag AS dest_tag
        FROM caravans c
        JOIN regions rd ON rd.id = c.destination_region_id
        WHERE c.id = %s AND c.user_id = %s
        FOR UPDATE OF c
        """,
        (caravan_id, user_id),
    )
    caravan = cur.fetchone()
    if not caravan:
        raise ValueError("Karawane nicht gefunden.")

    if caravan["status"] == "EN_ROUTE":
        raise ValueError("Die Karawane befindet sich noch auf der Reise und kann noch nicht entladen werden!")
    elif caravan["status"] == "UNLOADED":
        raise ValueError("Diese Karawane wurde bereits vollständig entladen.")
    elif caravan["status"] != "ARRIVED":
        raise ValueError(f"Karawane im Zustand '{caravan['status']}' kann nicht entladen werden.")

    dest_region_id = caravan["destination_region_id"]
    cargo_data = caravan["cargo"] if isinstance(caravan["cargo"], dict) else json.loads(caravan["cargo"])

    # Enforce foreign regional depot capacity limit
    cur.execute(
        """
        SELECT resource_type, amount
        FROM regional_depots
        WHERE user_id = %s AND region_id = %s
        FOR UPDATE
        """,
        (user_id, dest_region_id),
    )
    existing_depot_rows = cur.fetchall()
    current_depot_total = sum(float(r["amount"]) for r in existing_depot_rows)
    cargo_total = sum(float(v) for v in cargo_data.values() if float(v) > 0)
    depot_cap = getattr(settings, "REGIONAL_DEPOT_CAP", 500.0)

    if current_depot_total + cargo_total > depot_cap:
        raise ValueError(
            f"Regionaldepot ist voll: Kapazitätsgrenze von {depot_cap:.0f} Einheiten würde überschritten "
            f"(Aktuell: {current_depot_total:.2f}, Ladung: {cargo_total:.2f})."
        )

    # Stockpile in regional_depots atomically
    for res, amt in cargo_data.items():
        if amt > 0:
            cur.execute(
                """
                INSERT INTO regional_depots (user_id, region_id, resource_type, amount, last_updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (user_id, region_id, resource_type)
                DO UPDATE SET
                    amount = regional_depots.amount + EXCLUDED.amount,
                    last_updated_at = NOW()
                """,
                (user_id, dest_region_id, res, Decimal(str(round(amt, 2)))),
            )

    # Mark as UNLOADED
    cur.execute("UPDATE caravans SET status = 'UNLOADED' WHERE id = %s", (caravan_id,))

    # Notification
    create_notification(
        cur,
        user_id=user_id,
        event_type="CARAVAN_UNLOADED",
        payload={
            "caravan_id": caravan_id,
            "dest_tag": caravan["dest_tag"],
            "dest_name": caravan["dest_name"],
            "cargo": cargo_data,
        },
    )

    return {
        "caravan_id": caravan_id,
        "dest_region_id": dest_region_id,
        "dest_tag": caravan["dest_tag"],
        "dest_name": caravan["dest_name"],
        "cargo": cargo_data,
    }


def transfer_depot_to_kontor(
    cur,
    user_id: int,
    region_id: int,
    resource_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Transfers stockpiled commodities from a foreign regional depot into the player's
    home Kontor warehouse. Respects warehouse storage capacity limits.
    """
    # 1. Determine warehouse storage capacity
    cur.execute("SELECT level FROM buildings WHERE user_id = %s AND building_type = 'warehouse'", (user_id,))
    wh_row = cur.fetchone()
    wh_level = int(wh_row["level"]) if wh_row else 1
    storage_cap = get_effective_storage_cap(cur, user_id, wh_level)

    # 2. Lock depot records
    if resource_type:
        cur.execute(
            """
            SELECT id, resource_type, amount
            FROM regional_depots
            WHERE user_id = %s AND region_id = %s AND resource_type = %s AND amount > 0
            FOR UPDATE
            """,
            (user_id, region_id, resource_type),
        )
    else:
        cur.execute(
            """
            SELECT id, resource_type, amount
            FROM regional_depots
            WHERE user_id = %s AND region_id = %s AND amount > 0
            FOR UPDATE
            """,
            (user_id, region_id),
        )
    depot_rows = cur.fetchall()
    if not depot_rows:
        raise ValueError("Keine Waren im ausgewählten Depot zum Transfer verfügbar.")

    transferred: Dict[str, float] = {}
    total_transferred = 0.0

    for d_row in depot_rows:
        res = d_row["resource_type"]
        d_amt = float(d_row["amount"])

        # Check current cumulative warehouse stock
        cur.execute("SELECT COALESCE(SUM(amount), 0) AS total_kontor FROM inventories WHERE user_id = %s", (user_id,))
        total_stored = float(cur.fetchone()["total_kontor"])

        space_left = max(0.0, storage_cap - total_stored)
        amt_to_move = min(d_amt, space_left)

        if amt_to_move > 0:
            # Add to Kontor inventory
            cur.execute(
                """
                UPDATE inventories
                SET amount = amount + %s
                WHERE user_id = %s AND resource_type = %s
                """,
                (Decimal(str(round(amt_to_move, 2))), user_id, res),
            )
            # Deduct from depot
            cur.execute(
                """
                UPDATE regional_depots
                SET amount = amount - %s, last_updated_at = NOW()
                WHERE id = %s
                """,
                (Decimal(str(round(amt_to_move, 2))), d_row["id"]),
            )
            transferred[res] = amt_to_move
            total_transferred += amt_to_move

    if total_transferred <= 0:
        raise ValueError("Das Zentrallager im Kontor ist voll! Kein freier Lagerplatz für den Transfer verfügbar.")

    # Get region info for message
    cur.execute("SELECT name, tag FROM regions WHERE id = %s", (region_id,))
    reg = cur.fetchone()

    create_notification(
        cur,
        user_id=user_id,
        event_type="DEPOT_TRANSFERRED",
        payload={
            "region_id": region_id,
            "region_tag": reg["tag"] if reg else "",
            "region_name": reg["name"] if reg else "",
            "transferred": transferred,
        },
    )

    return {
        "region_id": region_id,
        "region_tag": reg["tag"] if reg else "",
        "region_name": reg["name"] if reg else "",
        "transferred": transferred,
        "total_transferred": total_transferred,
    }


def get_expeditions_overview(cur, user_id: int) -> Dict[str, Any]:
    """
    Assembles comprehensive logistics terminal data:
    - User's home region and foreign trade destinations (with precalculated distances & travel times)
    - Active and past caravans (with live countdowns and progress percentages)
    - Foreign regional depots (stockpiled goods available for transfer)
    - Kontor inventory status and warehouse capacity
    """
    # Resolve pending arrivals first
    resolve_caravan_statuses(cur, user_id)

    # 1. Fetch home region
    cur.execute(
        """
        SELECT u.region_id, r.name, r.tag, r.description, r.coord_x, r.coord_y
        FROM users u
        LEFT JOIN regions r ON r.id = u.region_id
        WHERE u.id = %s
        """,
        (user_id,),
    )
    user_row = cur.fetchone()
    if not user_row or not user_row["region_id"]:
        cur.execute("SELECT id, name, tag, description, coord_x, coord_y FROM regions WHERE tag = 'DANZ'")
        home_region = cur.fetchone()
        cur.execute("UPDATE users SET region_id = %s WHERE id = %s", (home_region["id"], user_id))
    else:
        home_region = user_row

    home_id = home_region["region_id"] if "region_id" in home_region else home_region["id"]
    home_x = home_region["coord_x"]
    home_y = home_region["coord_y"]

    # 2. Fetch foreign destination regions
    cur.execute(
        """
        SELECT r.id, r.name, r.tag, r.description, r.coord_x, r.coord_y, r.resource_multipliers,
               cg.tag AS controller_guild_tag, cg.name AS controller_guild_name
        FROM regions r
        LEFT JOIN regional_controllers rc ON rc.region_id = r.id AND rc.valid_until > NOW()
        LEFT JOIN guilds cg ON cg.id = rc.guild_id
        WHERE r.id != %s
        ORDER BY r.id ASC
        """,
        (home_id,),
    )
    foreign_regions = []
    for row in cur.fetchall():
        dist = calculate_distance(home_x, home_y, row["coord_x"], row["coord_y"])
        base_dur = calculate_travel_duration(dist)
        speed_mult = get_user_travel_speed_multiplier(cur, user_id, home_id, row["id"])
        dur = max(1, int(round(base_dur * speed_mult)))
        mults = row["resource_multipliers"] if isinstance(row["resource_multipliers"], dict) else json.loads(row["resource_multipliers"])
        foreign_regions.append({
            "id": row["id"],
            "name": row["name"],
            "tag": row["tag"],
            "description": row["description"],
            "coord_x": row["coord_x"],
            "coord_y": row["coord_y"],
            "distance": dist,
            "duration_seconds": dur,
            "speed_mult": speed_mult,
            "has_speed_bonus": speed_mult < 1.0,
            "controller_guild_tag": row["controller_guild_tag"],
            "controller_guild_name": row["controller_guild_name"],
            "resource_multipliers": mults,
        })

    # 3. Fetch caravans (active and history)
    cur.execute(
        """
        SELECT c.id, c.origin_region_id, c.destination_region_id, c.cargo, c.departure_at, c.arrival_at, c.status, c.created_at,
               ro.name AS origin_name, ro.tag AS origin_tag,
               rd.name AS dest_name, rd.tag AS dest_tag
        FROM caravans c
        JOIN regions ro ON ro.id = c.origin_region_id
        JOIN regions rd ON rd.id = c.destination_region_id
        WHERE c.user_id = %s
        ORDER BY c.id DESC
        LIMIT 25
        """,
        (user_id,),
    )
    caravans = []
    now = datetime.now(timezone.utc)
    for row in cur.fetchall():
        cargo_dict = row["cargo"] if isinstance(row["cargo"], dict) else json.loads(row["cargo"])
        arr = row["arrival_at"]
        if arr.tzinfo is None:
            arr = arr.replace(tzinfo=timezone.utc)
        dep = row["departure_at"]
        if dep.tzinfo is None:
            dep = dep.replace(tzinfo=timezone.utc)

        rem_sec = max(0, int((arr - now).total_seconds()))
        total_sec = max(1, int((arr - dep).total_seconds()))
        elapsed_sec = total_sec - rem_sec
        progress_pct = min(100.0, max(0.0, round((elapsed_sec / total_sec) * 100.0, 1)))

        if row["status"] in ("ARRIVED", "UNLOADED"):
            progress_pct = 100.0
            rem_sec = 0

        rem_h = rem_sec // 3600
        rem_m = (rem_sec % 3600) // 60
        rem_s = rem_sec % 60
        if rem_h > 0:
            countdown_str = f"{rem_h:02d}:{rem_m:02d}:{rem_s:02d}"
        else:
            countdown_str = f"{rem_m:02d}:{rem_s:02d}"

        caravans.append({
            "id": row["id"],
            "origin_name": row["origin_name"],
            "origin_tag": row["origin_tag"],
            "dest_name": row["dest_name"],
            "dest_tag": row["dest_tag"],
            "cargo": cargo_dict,
            "total_cargo": round(sum(float(v) for v in cargo_dict.values()), 2),
            "departure_at": dep,
            "arrival_at": arr,
            "status": row["status"],
            "remaining_seconds": rem_sec,
            "countdown_str": countdown_str,
            "progress_pct": progress_pct,
        })

    # 4. Fetch foreign regional depots
    cur.execute(
        """
        SELECT rd.id, rd.region_id, rd.resource_type, rd.amount, rd.last_updated_at,
               r.name AS region_name, r.tag AS region_tag
        FROM regional_depots rd
        JOIN regions r ON r.id = rd.region_id
        WHERE rd.user_id = %s AND rd.amount > 0
        ORDER BY r.name ASC, rd.resource_type ASC
        """,
        (user_id,),
    )
    depots_by_region: Dict[int, Dict[str, Any]] = {}
    for row in cur.fetchall():
        rid = row["region_id"]
        if rid not in depots_by_region:
            depots_by_region[rid] = {
                "region_id": rid,
                "region_name": row["region_name"],
                "region_tag": row["region_tag"],
                "resources": {},
                "total_amount": 0.0,
            }
        amt = float(row["amount"])
        depots_by_region[rid]["resources"][row["resource_type"]] = amt
        depots_by_region[rid]["total_amount"] += amt

    for d in depots_by_region.values():
        d["total_amount"] = round(d["total_amount"], 2)

    # 5. Fetch Kontor inventories & storage capacity
    cur.execute("SELECT resource_type, amount FROM inventories WHERE user_id = %s", (user_id,))
    inventories = {r["resource_type"]: float(r["amount"]) for r in cur.fetchall()}

    cur.execute("SELECT level FROM buildings WHERE user_id = %s AND building_type = 'warehouse'", (user_id,))
    wh_row = cur.fetchone()
    wh_level = int(wh_row["level"]) if wh_row else 1
    storage_cap = get_effective_storage_cap(cur, user_id, wh_level)

    return {
        "home_region": home_region,
        "foreign_regions": foreign_regions,
        "caravans": caravans,
        "depots": list(depots_by_region.values()),
        "inventories": inventories,
        "storage_cap": storage_cap,
        "max_cargo": CARAVAN_MAX_CARGO,
    }
