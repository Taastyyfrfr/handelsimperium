from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Any, Union
from app.config import settings, BUILDING_CONFIG, SUPPORTED_RESOURCES

def ensure_user_entities(cur, user_id: int):
    """Ensures a user has baseline buildings (including warehouse) and inventories initialized."""
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

def get_storage_cap(warehouse_level: int) -> float:
    """Calculates max storage capacity per resource determined by warehouse level."""
    return float(round(settings.BASE_STORAGE_CAP * (1.5 ** (max(1, warehouse_level) - 1)), 0))

def get_upgrade_costs(building_type: str, current_level: int) -> Dict[str, float]:
    """Calculates multi-resource upgrade costs scaling deterministically with cost = base_cost * 1.5^(level-1)."""
    cfg = BUILDING_CONFIG.get(building_type, {})
    base_costs = cfg.get("base_costs", {})
    scaling_factor = 1.5 ** (max(1, current_level) - 1)
    
    costs = {}
    for item, base_val in base_costs.items():
        costs[item] = round(base_val * scaling_factor, 2)
    return costs

def calculate_offline_production(cur, user_id: int) -> Dict[str, Any]:
    """
    Calculates offline resource generation strictly on-demand using timestamp deltas.
    Uses the user's warehouse level to determine global storage cap.
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
    
    # Fetch user buildings (including id and warehouse)
    cur.execute(
        """
        SELECT id, building_type, level, production_rate
        FROM buildings
        WHERE user_id = %s
        """,
        (user_id,),
    )
    building_rows = {r["building_type"]: r for r in cur.fetchall()}
    
    # Storage cap is dictated by the player's warehouse building level
    warehouse_info = building_rows.get("warehouse")
    warehouse_level = warehouse_info["level"] if warehouse_info else 1
    storage_cap = get_storage_cap(warehouse_level)
    
    now = datetime.now(timezone.utc)
    updated_inventories = []
    
    # Map resource to its producing building
    res_to_building = {info["resource"]: b_type for b_type, info in BUILDING_CONFIG.items() if info["resource"]}
    
    for res in SUPPORTED_RESOURCES:
        inv = inv_rows.get(res)
        if not inv:
            continue
        
        b_type = res_to_building.get(res)
        b_info = building_rows.get(b_type) if b_type else None
        
        rate = float(b_info["production_rate"]) if b_info else 0.1
        
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
    
    # Format buildings with multi-resource upgrade requirements
    buildings_list = []
    for b_type, b_meta in BUILDING_CONFIG.items():
        b_row = building_rows.get(b_type)
        b_id = b_row["id"] if b_row else None
        lvl = b_row["level"] if b_row else 1
        rate = float(b_row["production_rate"]) if b_row else b_meta["base_rate"]
        upgrade_costs = get_upgrade_costs(b_type, lvl)
        
        buildings_list.append({
            "id": b_id,
            "building_type": b_type,
            "name": b_meta["name"],
            "description": b_meta.get("description", ""),
            "resource": b_meta["resource"],
            "level": lvl,
            "production_rate": round(rate, 4),
            "is_warehouse": b_type == "warehouse",
            "storage_cap": get_storage_cap(lvl) if b_type == "warehouse" else storage_cap,
            "next_storage_cap": get_storage_cap(lvl + 1) if b_type == "warehouse" else None,
            "upgrade_costs": upgrade_costs,
        })
    
    return {
        "inventories": updated_inventories,
        "buildings": buildings_list,
        "storage_cap": storage_cap,
        "warehouse_level": warehouse_level,
    }

def upgrade_building(cur, user_id: int, building_id_or_type: Union[int, str]) -> Dict[str, Any]:
    """
    Atomically upgrades a building by verifying and deducting multi-resource costs.
    Executes with row-level locks within a PostgreSQL transaction.
    """
    # 1. Look up target building
    if str(building_id_or_type).isdigit():
        cur.execute(
            """
            SELECT id, user_id, building_type, level, production_rate
            FROM buildings
            WHERE id = %s AND user_id = %s
            FOR UPDATE
            """,
            (int(building_id_or_type), user_id),
        )
    else:
        cur.execute(
            """
            SELECT id, user_id, building_type, level, production_rate
            FROM buildings
            WHERE building_type = %s AND user_id = %s
            FOR UPDATE
            """,
            (str(building_id_or_type), user_id),
        )
    
    building = cur.fetchone()
    if not building:
        raise ValueError("Gebäude nicht gefunden.")
    
    b_id = building["id"]
    b_type = building["building_type"]
    current_level = building["level"]
    
    if b_type not in BUILDING_CONFIG:
        raise ValueError(f"Unbekannter Gebäudetyp: {b_type}")
    
    # 2. Flush pending offline production before applying upgrade
    calculate_offline_production(cur, user_id)
    
    # 3. Calculate multi-resource upgrade costs
    required_costs = get_upgrade_costs(b_type, current_level)
    
    # 4. Lock user balance and inventory rows
    cur.execute("SELECT balance FROM users WHERE id = %s FOR UPDATE", (user_id,))
    user_row = cur.fetchone()
    current_balance = float(user_row["balance"]) if user_row else 0.0
    
    res_keys = [k for k in required_costs.keys() if k != "balance"]
    if res_keys:
        cur.execute(
            """
            SELECT resource_type, amount
            FROM inventories
            WHERE user_id = %s AND resource_type = ANY(%s)
            FOR UPDATE
            """,
            (user_id, res_keys),
        )
        inv_map = {r["resource_type"]: float(r["amount"]) for r in cur.fetchall()}
    else:
        inv_map = {}
    
    # 5. Check sufficiency for each required cost
    deficits = []
    if "balance" in required_costs:
        req_bal = required_costs["balance"]
        if current_balance < req_bal:
            deficits.append(f"{req_bal:.2f} Taler (Vorhanden: {current_balance:.2f})")
            
    for res_name in res_keys:
        req_amt = required_costs[res_name]
        avail_amt = inv_map.get(res_name, 0.0)
        if avail_amt < req_amt:
            deficits.append(f"{req_amt:.2f} {res_name} (Vorhanden: {avail_amt:.2f})")
            
    if deficits:
        deficit_str = ", ".join(deficits)
        raise ValueError(f"Nicht genügend Ressourcen für Ausbau! Fehlend: {deficit_str}")
    
    # 6. Deduct costs atomically
    if "balance" in required_costs:
        new_balance = round(current_balance - required_costs["balance"], 2)
        cur.execute("UPDATE users SET balance = %s WHERE id = %s", (new_balance, user_id))
        
    for res_name in res_keys:
        deduct_amt = required_costs[res_name]
        cur.execute(
            "UPDATE inventories SET amount = amount - %s WHERE user_id = %s AND resource_type = %s",
            (deduct_amt, user_id, res_name),
        )
    
    # 7. Increment level and recalculate production rate / warehouse capacity
    new_level = current_level + 1
    if b_type == "warehouse":
        new_rate = 0.0
        new_cap = get_storage_cap(new_level)
    else:
        base_rate = BUILDING_CONFIG[b_type]["base_rate"]
        new_rate = round(base_rate * (1.25 ** (new_level - 1)), 4)
        new_cap = None
        
    cur.execute(
        """
        UPDATE buildings
        SET level = %s, production_rate = %s
        WHERE id = %s
        """,
        (new_level, new_rate, b_id),
    )
    
    return {
        "success": True,
        "building_id": b_id,
        "building_type": b_type,
        "name": BUILDING_CONFIG[b_type]["name"],
        "new_level": new_level,
        "new_rate": new_rate,
        "new_storage_cap": new_cap,
        "new_balance": new_balance if "balance" in required_costs else current_balance,
        "costs_deducted": required_costs,
    }
