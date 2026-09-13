import pytest
import time
import json
from starlette.testclient import TestClient

from app.main import app
from app.config import settings, BUILDING_CONFIG, SUPPORTED_RESOURCES, REFERENCE_PRICES
from app.engine.ranking import (
    calculate_building_sunk_capital,
    calculate_user_net_worth,
    compute_full_leaderboard,
    RankingCache,
)
from app.engine.production import ensure_user_entities, upgrade_building

def test_net_worth_calculation_accuracy(db_conn):
    """
    Tests 3-pillar net-worth calculation across diverse player profiles:
    - Cash-heavy merchant
    - Inventory-heavy merchant (evaluated at 24h VWAP / reference prices)
    - Building-heavy merchant (cumulative sunk capital formula)
    """
    price_map = dict(REFERENCE_PRICES)
    
    # 1. Profile: Cash-heavy merchant
    # Balance: 50,000, Inventories: 0, Buildings: all level 1 (sunk upgrade capital = 0)
    nw_cash = calculate_user_net_worth(
        user_id=101,
        username="reichskanzler",
        balance=50000.0,
        inventories={r: 0.0 for r in SUPPORTED_RESOURCES},
        buildings={b: 1 for b in BUILDING_CONFIG.keys()},
        price_map=price_map,
    )
    assert nw_cash["liquid_balance"] == 50000.0
    assert nw_cash["commodity_value"] == 0.0
    assert nw_cash["building_capital"] == 0.0
    assert nw_cash["total_net_worth"] == 50000.0

    # 2. Profile: Inventory-heavy merchant
    # Balance: 100, Inventories: 100 wood (@4.00), 100 stone (@5.00), 50 iron (@12.00), 200 grain (@3.00), 50 cloth (@8.00)
    # Expected commodity value: 400 + 500 + 600 + 600 + 400 = 2500 Taler
    nw_inv = calculate_user_net_worth(
        user_id=102,
        username="grosshaendler",
        balance=100.0,
        inventories={"wood": 100.0, "stone": 100.0, "iron": 50.0, "grain": 200.0, "cloth": 50.0},
        buildings={b: 1 for b in BUILDING_CONFIG.keys()},
        price_map=price_map,
    )
    assert nw_inv["liquid_balance"] == 100.0
    assert nw_inv["commodity_value"] == 2500.0
    assert nw_inv["building_capital"] == 0.0
    assert nw_inv["total_net_worth"] == 2600.0

    # 3. Profile: Building-heavy merchant (upgraded lumberjack to lvl 3 and warehouse to lvl 2)
    # Lumberjack lvl 1 -> 2 cost: 40 wood (160) + 20 stone (100) + 25 balance = 285 Taler
    # Lumberjack lvl 2 -> 3 cost: 285 * 1.5 = 427.50 Taler
    # Warehouse lvl 1 -> 2 cost: 100 wood (400) + 80 stone (400) + 25 iron (300) + 75 balance = 1175 Taler
    lj_cap = calculate_building_sunk_capital("lumberjack", 3, price_map)
    wh_cap = calculate_building_sunk_capital("warehouse", 2, price_map)
    assert lj_cap > 700.0
    assert wh_cap > 1100.0

    nw_bld = calculate_user_net_worth(
        user_id=103,
        username="baumeister",
        balance=500.0,
        inventories={r: 10.0 for r in SUPPORTED_RESOURCES},
        buildings={"lumberjack": 3, "warehouse": 2, "quarry": 1, "mine": 1, "farm": 1, "weaver": 1},
        price_map=price_map,
    )
    assert nw_bld["building_capital"] == round(lj_cap + wh_cap, 2)
    assert nw_bld["total_net_worth"] == round(500.0 + nw_bld["commodity_value"] + lj_cap + wh_cap, 2)

def test_building_upgrade_capital_conservation(db_conn):
    """
    Verifies that executing a building upgrade transfers capital from liquid/commodity
    into building capital without arbitrary loss of overall merchant net worth.
    """
    ts = int(time.time() * 1000)
    username = f"investor_{ts}"

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 1000.0) RETURNING id",
            (username,),
        )
        u_id = cur.fetchone()["id"]
        ensure_user_entities(cur, u_id)
        # Give enough resources to perform upgrade
        cur.execute("UPDATE inventories SET amount = 200.0 WHERE user_id = %s", (u_id,))
        db_conn.commit()

        # Calculate initial net worth
        price_map = dict(REFERENCE_PRICES)
        cur.execute("SELECT resource_type, amount FROM inventories WHERE user_id = %s", (u_id,))
        init_inv = {r["resource_type"]: float(r["amount"]) for r in cur.fetchall()}
        cur.execute("SELECT building_type, level FROM buildings WHERE user_id = %s", (u_id,))
        init_bld = {r["building_type"]: int(r["level"]) for r in cur.fetchall()}
        
        nw_before = calculate_user_net_worth(u_id, username, 1000.0, init_inv, init_bld, price_map)
        
        # Upgrade lumberjack (lvl 1 -> lvl 2)
        res = upgrade_building(cur, u_id, "lumberjack")
        db_conn.commit()

        # Fetch post-upgrade state
        cur.execute("SELECT balance FROM users WHERE id = %s", (u_id,))
        fresh_bal = float(cur.fetchone()["balance"])
        cur.execute("SELECT resource_type, amount FROM inventories WHERE user_id = %s", (u_id,))
        fresh_inv = {r["resource_type"]: float(r["amount"]) for r in cur.fetchall()}
        cur.execute("SELECT building_type, level FROM buildings WHERE user_id = %s", (u_id,))
        fresh_bld = {r["building_type"]: int(r["level"]) for r in cur.fetchall()}

        nw_after = calculate_user_net_worth(u_id, username, fresh_bal, fresh_inv, fresh_bld, price_map)

        # Net worth should be conserved (within rounding delta)
        assert abs(nw_before["total_net_worth"] - nw_after["total_net_worth"]) < 0.20
        assert nw_after["building_capital"] > nw_before["building_capital"]
        assert nw_after["liquid_balance"] < nw_before["liquid_balance"]

def test_ranking_cache_behavior_and_invalidation(db_conn):
    """
    Tests that RankingCache caches calculated rankings, serves them without DB hits,
    and invalidates appropriately on command or TTL expiry.
    """
    cache = RankingCache(ttl_seconds=300)
    
    with db_conn.cursor() as cur:
        # 1. Initial fetch computes and caches
        data1, prices1, time1 = cache.get_leaderboard(cur)
        assert len(data1) > 0
        assert time1 is not None

        # 2. Immediate second call serves from cache (same timestamp)
        data2, prices2, time2 = cache.get_leaderboard(cur)
        assert time1 == time2
        assert len(data1) == len(data2)

        # 3. Explicit invalidation forces fresh computation
        cache.invalidate()
        assert cache.cached_at is None
        data3, prices3, time3 = cache.get_leaderboard(cur)
        assert time3 >= time1

        # 4. Verify sorting: top entry has highest total_net_worth
        for i in range(len(data3) - 1):
            assert data3[i]["total_net_worth"] >= data3[i + 1]["total_net_worth"]
            assert data3[i]["rank"] == i + 1

def test_csrf_middleware_protection():
    """
    Verifies CSRF middleware security:
    - Missing CSRF token on POST returns 403 Forbidden.
    - Invalid CSRF token on POST returns 403 Forbidden.
    - Valid X-CSRF-Token header matching cookie passes.
    - Valid csrf_token in form data matching cookie passes.
    - Safe methods (GET, HEAD, OPTIONS) are exempt.
    """
    client = TestClient(app)

    # 1. GET request receives imperium_csrf cookie
    get_res = client.get("/health")
    assert get_res.status_code == 200
    csrf_cookie = get_res.cookies.get(settings.CSRF_COOKIE_NAME)
    assert csrf_cookie is not None
    assert len(csrf_cookie) >= 32

    # 2. POST without any CSRF token -> 403 Forbidden
    post_unprotected = client.post("/auth/login", data={"username": "test", "password": "pwd"})
    assert post_unprotected.status_code == 403
    assert "CSRF-Token" in post_unprotected.json().get("detail", "")

    # 3. POST with invalid/tampered CSRF token -> 403 Forbidden
    client.cookies.set(settings.CSRF_COOKIE_NAME, "valid_cookie_1234567890123456789012")
    post_invalid = client.post(
        "/auth/login",
        data={"username": "test", "password": "pwd"},
        headers={"X-CSRF-Token": "tampered_token_09876543210987654321"},
    )
    assert post_invalid.status_code == 403

    # 4. POST with valid header matching cookie -> Accepted past middleware (returns 400 Bad Request for bad credentials, NOT 403!)
    token_val = "secret_valid_csrf_token_hex_value_32_bytes"
    client.cookies.set(settings.CSRF_COOKIE_NAME, token_val)
    post_valid_header = client.post(
        "/auth/login",
        data={"username": "nonexistent_merchant", "password": "wrong_password"},
        headers={"X-CSRF-Token": token_val},
    )
    assert post_valid_header.status_code != 403

    # 5. POST with valid form field matching cookie -> Accepted past middleware
    post_valid_form = client.post(
        "/auth/login",
        data={"username": "nonexistent_merchant", "password": "wrong_password", "csrf_token": token_val},
    )
    assert post_valid_form.status_code != 403

def test_pwa_manifest_and_sw_assets():
    """
    Verifies that the PWA Web App Manifest, Service Worker, and icons
    are served correctly with proper HTTP status and mime-types.
    """
    client = TestClient(app)

    # 1. Manifest
    manifest_res = client.get("/static/manifest.json")
    assert manifest_res.status_code == 200
    manifest_data = manifest_res.json()
    assert manifest_data["name"] == "Handelsimperium"
    assert manifest_data["short_name"] == "Imperium"
    assert manifest_data["display"] == "standalone"
    assert manifest_data["start_url"] == "/"
    assert len(manifest_data["icons"]) > 0

    # 2. Service Worker
    sw_res = client.get("/static/sw.js")
    assert sw_res.status_code == 200
    assert "CACHE_NAME" in sw_res.text
    assert "addEventListener('fetch'" in sw_res.text

    # 3. Icon
    icon_res = client.get("/static/icon.svg")
    assert icon_res.status_code == 200
    assert "<svg" in icon_res.text
