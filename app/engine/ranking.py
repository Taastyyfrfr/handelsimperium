import threading
from datetime import datetime, timezone
from typing import Dict, List, Any, Tuple, Optional
from app.config import settings, BUILDING_CONFIG, SUPPORTED_RESOURCES, REFERENCE_PRICES
from app.engine.production import get_upgrade_costs

def get_commodity_prices(cur) -> Dict[str, float]:
    """
    Determines market valuation for all supported commodities.
    Uses 24-hour Volume-Weighted Average Price (VWAP) if available;
    falls back to canonical reference prices.
    """
    cur.execute(
        """
        SELECT resource_type,
               SUM(amount * price) / NULLIF(SUM(amount), 0) AS vwap
        FROM trades
        WHERE executed_at >= NOW() - INTERVAL '24 hours'
        GROUP BY resource_type
        """
    )
    rows = cur.fetchall()
    vwap_map = {r["resource_type"]: float(r["vwap"]) for r in rows if r["vwap"] is not None}
    
    price_map = {}
    for res in SUPPORTED_RESOURCES:
        if res in vwap_map and vwap_map[res] > 0:
            price_map[res] = round(vwap_map[res], 2)
        else:
            price_map[res] = REFERENCE_PRICES.get(res, 5.00)
    return price_map

def calculate_building_sunk_capital(building_type: str, level: int, price_map: Dict[str, float]) -> float:
    """
    Computes total capital sunk into upgrading a building from level 1 up to current level
    using the cumulative cost scaling curve: cost = base_cost * 1.5^(level-1).
    Evaluates both Taler balance and resource costs at current market/reference prices.
    """
    if level <= 1:
        return 0.0
    
    total_capital = 0.0
    for lvl in range(1, level):
        costs = get_upgrade_costs(building_type, lvl)
        step_cost = float(costs.get("balance", 0.0))
        for res, amt in costs.items():
            if res != "balance":
                p = price_map.get(res, REFERENCE_PRICES.get(res, 1.0))
                step_cost += float(amt) * p
        total_capital += step_cost
        
    return round(total_capital, 2)

def calculate_user_net_worth(
    user_id: int,
    username: str,
    balance: float,
    inventories: Dict[str, float],
    buildings: Dict[str, int],
    price_map: Dict[str, float],
    escrow_taler: float = 0.0,
    escrow_commodities: Optional[Dict[str, float]] = None,
    transit_commodities: Optional[Dict[str, float]] = None,
    depot_commodities: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Calculates comprehensive net worth for an individual merchant across all assets:
    1. Liquid Taler balance
    2. Escrowed Taler in active BUY orders
    3. Warehouse inventory (valued at 24h-VWAP / reference prices)
    4. Escrowed commodities in active SELL orders
    5. In-transit cargo in caravans (EN_ROUTE or ARRIVED)
    6. Regional depot stockpiles
    7. Sunk capital in production buildings & warehouse
    """
    liquid = round(float(balance), 2)
    escrow_bal = round(float(escrow_taler), 2)
    
    # 1. Warehouse inventory
    commodity_val = 0.0
    inv_breakdown = {}
    for res in SUPPORTED_RESOURCES:
        amt = float(inventories.get(res, 0.0))
        p = price_map.get(res, REFERENCE_PRICES.get(res, 1.0))
        val = round(amt * p, 2)
        inv_breakdown[res] = {"amount": amt, "price": p, "value": val}
        commodity_val += val
    commodity_val = round(commodity_val, 2)

    # 2. Escrowed commodities (active SELL orders)
    escrow_comm_map = escrow_commodities or {}
    escrow_comm_val = 0.0
    escrow_comm_breakdown = {}
    for res, amt in escrow_comm_map.items():
        amt_f = float(amt)
        if amt_f > 0:
            p = price_map.get(res, REFERENCE_PRICES.get(res, 1.0))
            val = round(amt_f * p, 2)
            escrow_comm_breakdown[res] = {"amount": amt_f, "price": p, "value": val}
            escrow_comm_val += val
    escrow_comm_val = round(escrow_comm_val, 2)

    # 3. In-transit / arrived caravan cargo
    transit_map = transit_commodities or {}
    transit_val = 0.0
    transit_breakdown = {}
    for res, amt in transit_map.items():
        amt_f = float(amt)
        if amt_f > 0:
            p = price_map.get(res, REFERENCE_PRICES.get(res, 1.0))
            val = round(amt_f * p, 2)
            transit_breakdown[res] = {"amount": amt_f, "price": p, "value": val}
            transit_val += val
    transit_val = round(transit_val, 2)

    # 4. Regional depot stockpiles
    depot_map = depot_commodities or {}
    depot_val = 0.0
    depot_breakdown = {}
    for res, amt in depot_map.items():
        amt_f = float(amt)
        if amt_f > 0:
            p = price_map.get(res, REFERENCE_PRICES.get(res, 1.0))
            val = round(amt_f * p, 2)
            depot_breakdown[res] = {"amount": amt_f, "price": p, "value": val}
            depot_val += val
    depot_val = round(depot_val, 2)
    
    total_commodity_val = round(commodity_val + escrow_comm_val + transit_val + depot_val, 2)

    # 5. Building sunk capital
    building_cap = 0.0
    building_breakdown = {}
    for b_type in BUILDING_CONFIG.keys():
        lvl = int(buildings.get(b_type, 1))
        cap = calculate_building_sunk_capital(b_type, lvl, price_map)
        building_breakdown[b_type] = {"level": lvl, "sunk_capital": cap}
        building_cap += cap
    building_cap = round(building_cap, 2)
    
    total_net_worth = round(liquid + escrow_bal + total_commodity_val + building_cap, 2)
    
    return {
        "user_id": user_id,
        "username": username,
        "liquid_balance": liquid,
        "escrow_balance": escrow_bal,
        "escrow_taler": escrow_bal,
        "commodity_value": commodity_val,
        "escrow_commodity_value": escrow_comm_val,
        "transit_commodity_value": transit_val,
        "depot_commodity_value": depot_val,
        "total_commodity_value": total_commodity_val,
        "building_capital": building_cap,
        "total_net_worth": total_net_worth,
        "inventories": inv_breakdown,
        "buildings": building_breakdown,
        "escrow_commodities": escrow_comm_breakdown,
        "transit_commodities": transit_breakdown,
        "depot_commodities": depot_breakdown,
    }

def compute_full_leaderboard(cur) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    """
    Loads all merchants, liquid balances, escrowed cash/goods, inventories,
    caravans, regional depots, and buildings from PostgreSQL
    and computes sorted rankings in an atomic, efficient pass.
    """
    import json
    price_map = get_commodity_prices(cur)
    
    # 1. Fetch all users
    cur.execute("SELECT id, username, balance FROM users ORDER BY id ASC")
    user_rows = cur.fetchall()
    
    # 2. Fetch all warehouse inventories
    cur.execute("SELECT user_id, resource_type, amount FROM inventories")
    inv_rows = cur.fetchall()
    inv_by_user: Dict[int, Dict[str, float]] = {}
    for r in inv_rows:
        u_id = r["user_id"]
        if u_id not in inv_by_user:
            inv_by_user[u_id] = {}
        inv_by_user[u_id][r["resource_type"]] = float(r["amount"])
        
    # 3. Fetch all buildings
    cur.execute("SELECT user_id, building_type, level FROM buildings")
    build_rows = cur.fetchall()
    build_by_user: Dict[int, Dict[str, int]] = {}
    for r in build_rows:
        u_id = r["user_id"]
        if u_id not in build_by_user:
            build_by_user[u_id] = {}
        build_by_user[u_id][r["building_type"]] = int(r["level"])

    # 4. Fetch escrowed Taler from active BUY orders
    cur.execute(
        """
        SELECT user_id, SUM((amount - filled_amount) * limit_price) AS escrow_taler
        FROM market_orders
        WHERE status = 'ACTIVE' AND order_type = 'BUY' AND (amount - filled_amount) > 0
        GROUP BY user_id
        """
    )
    escrow_taler_by_user = {r["user_id"]: float(r["escrow_taler"]) for r in cur.fetchall()}

    # 5. Fetch escrowed commodities from active SELL orders
    cur.execute(
        """
        SELECT user_id, resource_type, SUM(amount - filled_amount) AS escrow_amount
        FROM market_orders
        WHERE status = 'ACTIVE' AND order_type = 'SELL' AND (amount - filled_amount) > 0
        GROUP BY user_id, resource_type
        """
    )
    escrow_comm_by_user: Dict[int, Dict[str, float]] = {}
    for r in cur.fetchall():
        u_id = r["user_id"]
        if u_id not in escrow_comm_by_user:
            escrow_comm_by_user[u_id] = {}
        escrow_comm_by_user[u_id][r["resource_type"]] = float(r["escrow_amount"])

    # 6. Fetch in-transit / arrived caravan cargo
    cur.execute(
        """
        SELECT user_id, cargo
        FROM caravans
        WHERE status IN ('EN_ROUTE', 'ARRIVED')
        """
    )
    transit_by_user: Dict[int, Dict[str, float]] = {}
    for r in cur.fetchall():
        u_id = r["user_id"]
        if u_id not in transit_by_user:
            transit_by_user[u_id] = {}
        cargo_dict = r["cargo"] if isinstance(r["cargo"], dict) else json.loads(r["cargo"])
        for res, amt in cargo_dict.items():
            transit_by_user[u_id][res] = transit_by_user[u_id].get(res, 0.0) + float(amt)

    # 7. Fetch foreign regional depot stockpiles
    cur.execute(
        """
        SELECT user_id, resource_type, SUM(amount) AS depot_amount
        FROM regional_depots
        WHERE amount > 0
        GROUP BY user_id, resource_type
        """
    )
    depot_by_user: Dict[int, Dict[str, float]] = {}
    for r in cur.fetchall():
        u_id = r["user_id"]
        if u_id not in depot_by_user:
            depot_by_user[u_id] = {}
        depot_by_user[u_id][r["resource_type"]] = float(r["depot_amount"])
        
    # 8. Compute comprehensive net worth for all users
    entries = []
    for u in user_rows:
        u_id = u["id"]
        nw = calculate_user_net_worth(
            user_id=u_id,
            username=u["username"],
            balance=float(u["balance"]),
            inventories=inv_by_user.get(u_id, {}),
            buildings=build_by_user.get(u_id, {}),
            price_map=price_map,
            escrow_taler=escrow_taler_by_user.get(u_id, 0.0),
            escrow_commodities=escrow_comm_by_user.get(u_id, {}),
            transit_commodities=transit_by_user.get(u_id, {}),
            depot_commodities=depot_by_user.get(u_id, {}),
        )
        entries.append(nw)
        
    # 9. Sort descending by total_net_worth, tie-break by username
    entries.sort(key=lambda x: (-x["total_net_worth"], x["username"].lower()))
    
    # 10. Assign ranks
    for idx, e in enumerate(entries, start=1):
        e["rank"] = idx
        
    return entries, price_map

class RankingCache:
    """
    Thread-safe in-memory cache for merchant rankings with configurable TTL (default: 300s / 5min).
    Prevents repeated expensive table scans and joins across all accounts.
    """
    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self.cached_at: Optional[datetime] = None
        self.leaderboard: List[Dict[str, Any]] = []
        self.price_map: Dict[str, float] = {}
        self._lock = threading.Lock()

    def get_leaderboard(self, cur, force_refresh: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, float], datetime]:
        with self._lock:
            now = datetime.now(timezone.utc)
            is_stale = (
                self.cached_at is None
                or (now - self.cached_at).total_seconds() >= self.ttl
            )
            if force_refresh or is_stale:
                self.leaderboard, self.price_map = compute_full_leaderboard(cur)
                self.cached_at = now
            return self.leaderboard, self.price_map, self.cached_at

    def invalidate(self):
        with self._lock:
            self.cached_at = None

ranking_cache = RankingCache(ttl_seconds=300)
