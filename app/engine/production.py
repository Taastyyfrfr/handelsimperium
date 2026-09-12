from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Any
from app.config import settings, BUILDING_CONFIG, SUPPORTED_RESOURCES

def ensure_user_entities(cur, user_id: int):
    """Ensures a user has baseline buildings and inventories initialized."""
    for b_type, b_info in BUILDING_CONFIG.items():
        cur.execute(
            """
            INSERT INTO buildings (user_id, building_type, level, production_rate)
            VALUES (%s, %s, 1, %s)
            ON CONFLICT (user_id, building_type) DO NOTHING
            """,
            (user_id, b_type, b_info["base_rate"]),
        )
    for res in SUPPORTED_RESOURCES:
        cur.execute(
            """
            INSERT INTO inventories (user_id, resource_type, amount, last_calculated_at)
            VALUES (%s, %s, 100.00, NOW())
            ON CONFLICT (user_id, resource_type) DO NOTHING
            """,
            (user_id, res),
        )

def get_storage_cap(building_level: int) -> float:
    return float(settings.BASE_STORAGE_CAP + (building_level * settings.STORAGE_CAP_PER_LEVEL))

def get_upgrade_cost(building_type: str, current_level: int) -> float:
    cfg = BUILDING_CONFIG.get(building_type, {})
    base = cfg.get("upgrade_cost_base", 50.0)
    return round(base * (1.6 ** (current_level - 1)), 2)

def calculate_offline_production(cur, user_id: int) -> Dict[str, Any]:
    """
    Calculates offline resource generation strictly on-demand using timestamp deltas.
    Updates inventories atomically in PostgreSQL with storage cap enforcement.
    """
    ensure_user_entities(cur, user_id)
    
    # Lock inventories for this user to ensure atomic delta calculation
    cur.execute(
        """
        SELECT resource_type, amount, last_calculated_at
        FROM inventories
        WHERE user_id = %s
        FOR UPDATE
        """,
        (user_id,),
    )
    inv_rows = {r["resource_type"]: r for r in cur.fetchall()}
    
    # Fetch user buildings
    cur.execute(
        """
        SELECT building_type, level, production_rate
        FROM buildings
        WHERE user_id = %s
        """,
        (user_id,),
    )
    building_rows = {r["building_type"]: r for r in cur.fetchall()}
    
    now = datetime.now(timezone.utc)
    updated_inventories = []
    
    # Map resource to its producing building
    res_to_building = {info["resource"]: b_type for b_type, info in BUILDING_CONFIG.items()}
    
    for res in SUPPORTED_RESOURCES:
        inv = inv_rows.get(res)
        if not inv:
            continue
        
        b_type = res_to_building.get(res)
        b_info = building_rows.get(b_type) if b_type else None
        
        level = b_info["level"] if b_info else 1
        rate = float(b_info["production_rate"]) if b_info else 0.1
        storage_cap = get_storage_cap(level)
        
        last_calc = inv["last_calculated_at"]
        if last_calc.tzinfo is None:
            last_calc = last_calc.replace(tzinfo=timezone.utc)
        
        delta_seconds = max(0.0, (now - last_calc).total_seconds())
        generated = delta_seconds * rate
        current_amount = float(inv["amount"])
        new_amount = min(storage_cap, current_amount + generated)
        
        # Write back updated amount and new timestamp
        cur.execute(
            """
            UPDATE inventories
            SET amount = %s, last_calculated_at = %s
            WHERE user_id = %s AND resource_type = %s
            """,
            (round(new_amount, 2), now, user_id, res),
        )
        
        updated_inventories.append({
            "resource_type": res,
            "amount": round(new_amount, 2),
            "storage_cap": storage_cap,
            "production_rate": rate,
            "delta_seconds": round(delta_seconds, 1),
            "generated": round(generated, 2),
            "last_calculated_at": now,
        })
    
    # Return formatted buildings
    buildings_list = []
    for b_type, b_meta in BUILDING_CONFIG.items():
        b_row = building_rows.get(b_type, {"level": 1, "production_rate": b_meta["base_rate"]})
        lvl = b_row["level"]
        buildings_list.append({
            "building_type": b_type,
            "name": b_meta["name"],
            "resource": b_meta["resource"],
            "level": lvl,
            "production_rate": round(float(b_row["production_rate"]), 3),
            "storage_cap": get_storage_cap(lvl),
            "upgrade_cost": get_upgrade_cost(b_type, lvl),
        })
    
    return {
        "inventories": updated_inventories,
        "buildings": buildings_list,
    }

def upgrade_building(cur, user_id: int, building_type: str) -> Dict[str, Any]:
    """Upgrades a building level if user has sufficient balance."""
    if building_type not in BUILDING_CONFIG:
        raise ValueError("Ungültiger Gebäudetyp")
    
    # Lock user balance
    cur.execute("SELECT balance FROM users WHERE id = %s FOR UPDATE", (user_id,))
    user = cur.fetchone()
    if not user:
        raise ValueError("Benutzer nicht gefunden")
    
    cur.execute(
        "SELECT level, production_rate FROM buildings WHERE user_id = %s AND building_type = %s FOR UPDATE",
        (user_id, building_type),
    )
    b = cur.fetchone()
    current_level = b["level"] if b else 1
    cost = get_upgrade_cost(building_type, current_level)
    
    if float(user["balance"]) < cost:
        raise ValueError(f"Nicht genug Taler! Benötigt: {cost:.2f}, Vorhanden: {float(user['balance']):.2f}")
    
    # Deduct balance
    new_balance = float(user["balance"]) - cost
    cur.execute("UPDATE users SET balance = %s WHERE id = %s", (new_balance, user_id))
    
    # First flush pending production before increasing rate
    calculate_offline_production(cur, user_id)
    
    new_level = current_level + 1
    new_rate = BUILDING_CONFIG[building_type]["base_rate"] * (1.25 ** (new_level - 1))
    
    cur.execute(
        """
        UPDATE buildings
        SET level = %s, production_rate = %s
        WHERE user_id = %s AND building_type = %s
        """,
        (new_level, round(new_rate, 4), user_id, building_type),
    )
    
    return {
        "success": True,
        "building_type": building_type,
        "new_level": new_level,
        "new_rate": round(new_rate, 4),
        "new_balance": round(new_balance, 2),
    }
