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
) -> Dict[str, Any]:
    """
    Calculates 3-pillar net worth for an individual merchant:
    1. Liquid Taler balance
    2. Commodity inventory valuation (using 24h VWAP or reference prices)
    3. Sunk capital in production buildings & warehouse
    """
    liquid = round(float(balance), 2)
    
    commodity_val = 0.0
    inv_breakdown = {}
    for res in SUPPORTED_RESOURCES:
        amt = float(inventories.get(res, 0.0))
        p = price_map.get(res, REFERENCE_PRICES.get(res, 1.0))
        val = round(amt * p, 2)
        inv_breakdown[res] = {"amount": amt, "price": p, "value": val}
        commodity_val += val
    commodity_val = round(commodity_val, 2)
    
    building_cap = 0.0
    building_breakdown = {}
    for b_type in BUILDING_CONFIG.keys():
        lvl = int(buildings.get(b_type, 1))
        cap = calculate_building_sunk_capital(b_type, lvl, price_map)
        building_breakdown[b_type] = {"level": lvl, "sunk_capital": cap}
        building_cap += cap
    building_cap = round(building_cap, 2)
    
    total_net_worth = round(liquid + commodity_val + building_cap, 2)
    
    return {
        "user_id": user_id,
        "username": username,
        "liquid_balance": liquid,
        "commodity_value": commodity_val,
        "building_capital": building_cap,
        "total_net_worth": total_net_worth,
        "inventories": inv_breakdown,
        "buildings": building_breakdown,
    }

def compute_full_leaderboard(cur) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    """
    Loads all merchants, their inventories, and buildings from PostgreSQL
    and computes sorted rankings in an atomic, efficient pass.
    """
    price_map = get_commodity_prices(cur)
    
    # 1. Fetch all users
    cur.execute("SELECT id, username, balance FROM users ORDER BY id ASC")
    user_rows = cur.fetchall()
    
    # 2. Fetch all inventories
    cur.execute("SELECT user_id, resource_type, amount FROM inventories")
    inv_rows = cur.fetchall()
    inv_by_user = {}
    for r in inv_rows:
        u_id = r["user_id"]
        if u_id not in inv_by_user:
            inv_by_user[u_id] = {}
        inv_by_user[u_id][r["resource_type"]] = float(r["amount"])
        
    # 3. Fetch all buildings
    cur.execute("SELECT user_id, building_type, level FROM buildings")
    build_rows = cur.fetchall()
    build_by_user = {}
    for r in build_rows:
        u_id = r["user_id"]
        if u_id not in build_by_user:
            build_by_user[u_id] = {}
        build_by_user[u_id][r["building_type"]] = int(r["level"])
        
    # 4. Compute net worth for all users
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
        )
        entries.append(nw)
        
    # 5. Sort descending by total_net_worth, tie-break by username
    entries.sort(key=lambda x: (-x["total_net_worth"], x["username"].lower()))
    
    # 6. Assign ranks
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
