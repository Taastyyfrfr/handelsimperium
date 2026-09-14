import pytest
import uuid
import re
from datetime import datetime, timezone, timedelta
from starlette.testclient import TestClient

from app.main import app
from app.auth import create_session_token, hash_password
from app.config import settings, REFERENCE_PRICES, BUILDING_CONFIG
from app.engine.production import ensure_user_entities
from app.engine.matching import get_price_corridor


def make_merchant(db_conn, prefix: str, region_tag: str = "DANZ", balance: float = 2000.0):
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = %s", (region_tag,))
        reg = cur.fetchone()
        region_id = reg["id"] if reg else 1
        username = f"{prefix}_{uuid.uuid4().hex[:6]}"
        cur.execute(
            """
            INSERT INTO users (username, password_hash, balance, region_id)
            VALUES (%s, %s, %s, %s)
            RETURNING id, username, balance, region_id
            """,
            (username, hash_password("secret123"), balance, region_id),
        )
        user = cur.fetchone()
        ensure_user_entities(cur, user["id"])
        db_conn.commit()
        return user


def get_client(user=None):
    client = TestClient(app)
    csrf_val = "test_csrf_token_secret_12345"
    client.cookies.set(settings.CSRF_COOKIE_NAME, csrf_val)
    if user:
        token = create_session_token(user["id"], user["username"])
        client.cookies.set(settings.COOKIE_NAME, token)
    return client, {"X-CSRF-Token": csrf_val}


# ----------------------------------------------------------------------
# 1. AUTHENTICATION & SESSION FEATURES
# ----------------------------------------------------------------------
def test_feature_auth_register_and_validation(db_conn):
    """Verifies Register page load, registration submit button, and input validations."""
    with TestClient(app) as client:
        # Load register page
        res = client.get("/auth/register")
        assert res.status_code == 200
        assert "Tor zur Handelsgilde" in res.text or "Konto" in res.text or "Kaufmannsname" in res.text

        csrf_val = client.cookies.get(settings.CSRF_COOKIE_NAME) or "csrf_reg_val"
        client.cookies.set(settings.CSRF_COOKIE_NAME, csrf_val)
        headers = {"X-CSRF-Token": csrf_val}

        # 1. Valid registration
        uname = f"reg_{uuid.uuid4().hex[:6]}"
        with db_conn.cursor() as cur:
            cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
            danz_id = cur.fetchone()["id"]

        res_ok = client.post(
            "/auth/register",
            data={
                "username": uname,
                "password": "password123",
                "confirm_password": "password123",
                "region_id": danz_id,
                "csrf_token": csrf_val,
            },
            headers=headers,
            follow_redirects=False,
        )
        assert res_ok.status_code == 303
        assert res_ok.headers["location"] == "/"
        assert settings.COOKIE_NAME in res_ok.cookies

        # 2. Duplicate username rejection
        res_dup = client.post(
            "/auth/register",
            data={
                "username": uname,
                "password": "password123",
                "confirm_password": "password123",
                "region_id": danz_id,
                "csrf_token": csrf_val,
            },
            headers=headers,
        )
        assert res_dup.status_code == 400
        assert "bereits vergeben" in res_dup.text

        # 3. Password mismatch rejection
        res_mismatch = client.post(
            "/auth/register",
            data={
                "username": f"mismatch_{uuid.uuid4().hex[:6]}",
                "password": "password123",
                "confirm_password": "password999",
                "region_id": danz_id,
                "csrf_token": csrf_val,
            },
            headers=headers,
        )
        assert res_mismatch.status_code == 400
        assert "stimmen nicht überein" in res_mismatch.text


def test_feature_auth_login_and_logout(db_conn):
    """Verifies Login submit button, authentication verification, and Logout link."""
    user = make_merchant(db_conn, "auth_usr")

    with TestClient(app) as client:
        # Load login page
        res_login_page = client.get("/auth/login")
        assert res_login_page.status_code == 200

        csrf_val = client.cookies.get(settings.CSRF_COOKIE_NAME) or "csrf_login_val"
        client.cookies.set(settings.CSRF_COOKIE_NAME, csrf_val)
        headers = {"X-CSRF-Token": csrf_val}

        # 1. Invalid login
        res_bad = client.post(
            "/auth/login",
            data={"username": user["username"], "password": "wrong_password", "csrf_token": csrf_val},
            headers=headers,
        )
        assert res_bad.status_code == 400
        assert "Ungültiger Benutzername" in res_bad.text

        # 2. Valid login
        res_good = client.post(
            "/auth/login",
            data={"username": user["username"], "password": "secret123", "csrf_token": csrf_val},
            headers=headers,
            follow_redirects=False,
        )
        assert res_good.status_code == 303
        assert res_good.headers["location"] == "/"
        assert settings.COOKIE_NAME in res_good.cookies

        # 3. Logout action
        client.cookies.set(settings.COOKIE_NAME, res_good.cookies[settings.COOKIE_NAME])
        res_logout = client.get("/auth/logout", follow_redirects=False)
        assert res_logout.status_code == 303
        assert res_logout.headers["location"] == "/auth/login"


# ----------------------------------------------------------------------
# 2. DASHBOARD & NAVIGATION TABS
# ----------------------------------------------------------------------
def test_feature_dashboard_and_all_tabs(db_conn):
    """Verifies the 8 primary navigation tabs in the dashboard render properly."""
    user = make_merchant(db_conn, "dash_usr")
    client, headers = get_client(user)

    # 1. Main Dashboard
    res_dash = client.get("/", headers=headers)
    assert res_dash.status_code == 200
    assert "main-panel" in res_dash.text

    # 2. Tab: Resources
    res_res = client.get("/resources/overview", headers=headers)
    assert res_res.status_code == 200
    assert "Lagerhaus & Produktion" in res_res.text

    # 3. Tab: Market Book
    res_mkt = client.get("/market/book?resource=wood", headers=headers)
    assert res_mkt.status_code == 200
    assert "Orderbuch" in res_mkt.text

    # 4. Tab: Trades Log
    res_trd = client.get("/trades/log", headers=headers)
    assert res_trd.status_code == 200
    assert "Handelsprotokoll" in res_trd.text or "Ausgeführte Trades" in res_trd.text

    # 5. Tab: Leaderboard Ranking
    res_rnk = client.get("/ranking", headers=headers)
    assert res_rnk.status_code == 200
    assert "Rangliste" in res_rnk.text

    # 6. Tab: Export Contracts
    res_cnt = client.get("/market/contracts", headers=headers)
    assert res_cnt.status_code == 200
    assert "Handelskarawanen" in res_cnt.text

    # 7. Tab: Logistics Expeditions
    res_exp = client.get("/expeditions", headers=headers)
    assert res_exp.status_code == 200
    assert "Expeditionen" in res_exp.text

    # 8. Tab: Guild Hall / Directory
    res_gld = client.get("/guilds", headers=headers)
    assert res_gld.status_code == 200
    assert "Gilde" in res_gld.text

    # 9. Tab: Merchant Handbook
    res_hnd = client.get("/handbuch", headers=headers)
    assert res_hnd.status_code == 200
    assert "Handbuch" in res_hnd.text


# ----------------------------------------------------------------------
# 3. PRODUCTION & BUILDING UPGRADE BUTTONS
# ----------------------------------------------------------------------
def test_feature_building_upgrades_and_feasibility(db_conn):
    """Verifies all building upgrade buttons: warehouse, yield buildings, and zero-yield guards."""
    # Create merchant in Danzig (Wood/Grain active, Iron/Cloth zero-yield)
    user = make_merchant(db_conn, "upg_usr", "DANZ", balance=5000.0)
    uid = user["id"]
    client, headers = get_client(user)

    with db_conn.cursor() as cur:
        # Give user abundant resources
        cur.execute("UPDATE inventories SET amount = 1000.0 WHERE user_id = %s", (uid,))
        db_conn.commit()

    # 1. Warehouse upgrade button
    res_wh = client.post("/buildings/warehouse/upgrade", headers=headers)
    assert res_wh.status_code == 200
    assert "Zentrallager erfolgreich auf Stufe" in res_wh.text

    # 2. Lumberjack upgrade button
    res_lj = client.post("/buildings/lumberjack/upgrade", headers=headers)
    assert res_lj.status_code == 200
    assert "Holzfällerhütte erfolgreich auf Stufe" in res_lj.text

    # 3. Quarry upgrade button
    res_qr = client.post("/buildings/quarry/upgrade", headers=headers)
    assert res_qr.status_code == 200
    assert "Steinbruch erfolgreich auf Stufe" in res_qr.text

    # 4. Mine (Iron) upgrade button in Danzig (multiplier 0.0) -> Rejection
    res_mine = client.post("/buildings/mine/upgrade", headers=headers)
    assert res_mine.status_code == 200
    assert "Dieser Rohstoff kann in Eurer Region nicht gewonnen werden" in res_mine.text

    # 5. Insufficient funds upgrade attempt
    with db_conn.cursor() as cur:
        cur.execute("UPDATE users SET balance = 0.0 WHERE id = %s", (uid,))
        cur.execute("UPDATE inventories SET amount = 0.0 WHERE user_id = %s", (uid,))
        db_conn.commit()

    res_fail = client.post("/buildings/warehouse/upgrade", headers=headers)
    assert res_fail.status_code == 200
    assert "Nicht genügend" in res_fail.text or "fehlen" in res_fail.text


# ----------------------------------------------------------------------
# 4. OFFLINE CATCH-UP MODAL & DISMISS BUTTON
# ----------------------------------------------------------------------
def test_feature_offline_catchup_modal_and_dismiss(db_conn):
    """Verifies catch-up modal loading and the 'Verstanden & Zum Kontor' dismiss button."""
    user = make_merchant(db_conn, "catch_usr")
    uid = user["id"]
    client, headers = get_client(user)

    with db_conn.cursor() as cur:
        # Insert a simulated unacknowledged catch-up
        cur.execute(
            """
            INSERT INTO user_catchups (user_id, offline_seconds, production_delta, trade_delta, dismissed)
            VALUES (%s, 3600.0, '{"wood": {"produced": 50.0, "lost": 0.0}}'::jsonb, '{"total_trade_count": 0}'::jsonb, FALSE)
            """,
            (uid,),
        )
        db_conn.commit()

    # 1. Fetch catchup modal
    res_modal = client.get("/resources/catchup", headers=headers)
    assert res_modal.status_code == 200
    assert "Während Deiner Abwesenheit" in res_modal.text
    assert "Verstanden & Zum Kontor" in res_modal.text

    # 2. Click dismiss button
    res_dismiss = client.post("/resources/catchup/dismiss", headers=headers)
    assert res_dismiss.status_code == 200
    assert res_dismiss.text == ""

    # 3. Subsequent fetch should return empty
    res_empty = client.get("/resources/catchup", headers=headers)
    assert res_empty.status_code == 200
    assert res_empty.text == ""


# ----------------------------------------------------------------------
# 5. TUTORIAL QUEST WIDGET & CLAIM BUTTON
# ----------------------------------------------------------------------
def test_feature_tutorial_widget_and_claim(db_conn):
    """Verifies onboarding quest drawer and the 'Belohnung einfordern!' button."""
    user = make_merchant(db_conn, "tut_usr", balance=500.0)
    uid = user["id"]
    client, headers = get_client(user)

    # 1. Load widget
    res_widget = client.get("/tutorial/widget", headers=headers)
    assert res_widget.status_code == 200
    assert "Kaufmannslehre" in res_widget.text

    # Step 1: Bestandsaufnahme is eligible upon initialization
    assert "Belohnung einfordern" in res_widget.text

    # 2. Click claim button for Step 1
    res_claim1 = client.post("/tutorial/claim", headers=headers)
    assert res_claim1.status_code == 200
    # Should advance to Step 2: Kontorausbau
    assert "Stufe 2" in res_claim1.text or "Kontorausbau" in res_claim1.text or "Expansion" in res_claim1.text


# ----------------------------------------------------------------------
# 6. MARKET ORDER ENTRY, COMMODITY TABS & CANCELLATION BUTTON
# ----------------------------------------------------------------------
def test_feature_market_tabs_orders_and_cancel_buttons(db_conn):
    """Verifies commodity selector tabs, order placement form, and order cancellation button."""
    user = make_merchant(db_conn, "mkt_usr", balance=5000.0)
    uid = user["id"]
    client, headers = get_client(user)

    with db_conn.cursor() as cur:
        cur.execute("UPDATE inventories SET amount = 500.0 WHERE user_id = %s", (uid,))
        cur.execute("DELETE FROM market_orders WHERE user_id = %s", (uid,))
        floor, ceiling, ref = get_price_corridor(cur, "wood")
        db_conn.commit()

    # 1. Commodity selector tabs
    for commodity in ["wood", "stone", "iron", "grain", "cloth"]:
        res_tab = client.get(f"/market/book?resource={commodity}", headers=headers)
        assert res_tab.status_code == 200
        assert f"value=\"{commodity}\"" in res_tab.text

    # 2. Place SELL limit order within corridor
    res_sell = client.post(
        "/market/orders",
        data={"order_type": "SELL", "resource_type": "wood", "amount": 10, "limit_price": ref},
        headers=headers,
    )
    assert res_sell.status_code == 200
    assert "erfolgreich im Orderbuch platziert" in res_sell.text or "ausgeführt" in res_sell.text

    # Find the order ID
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM market_orders WHERE user_id = %s AND status = 'ACTIVE' LIMIT 1", (uid,))
        order_row = cur.fetchone()
        assert order_row is not None
        order_id = order_row["id"]

    # 3. Order cancellation button
    res_cancel = client.post(
        f"/market/orders/{order_id}/cancel",
        data={"resource_type": "wood"},
        headers=headers,
    )
    assert res_cancel.status_code == 200
    assert "storniert" in res_cancel.text

    # 4. Out of bounds order rejection (below floor & above ceiling)
    res_low = client.post(
        "/market/orders",
        data={"order_type": "BUY", "resource_type": "wood", "amount": 10, "limit_price": floor - 1.0},
        headers=headers,
    )
    assert res_low.status_code == 422
    assert "Handelsspanne" in res_low.text

    res_high = client.post(
        "/market/orders",
        data={"order_type": "BUY", "resource_type": "wood", "amount": 10, "limit_price": ceiling + 2.0},
        headers=headers,
    )
    assert res_high.status_code == 422
    assert "Handelsspanne" in res_high.text


# ----------------------------------------------------------------------
# 7. EXPORT CONTRACTS & FULFILL BUTTON
# ----------------------------------------------------------------------
def test_feature_export_contracts_and_fulfill_button(db_conn):
    """Verifies listing export contracts and clicking the fulfillment delivery button."""
    user = make_merchant(db_conn, "cnt_usr", balance=2000.0)
    uid = user["id"]
    client, headers = get_client(user)

    # 1. List contracts
    res_list = client.get("/market/contracts", headers=headers)
    assert res_list.status_code == 200
    assert "Handelskarawanen" in res_list.text

    # Give user abundant resources to fulfill
    with db_conn.cursor() as cur:
        cur.execute("UPDATE inventories SET amount = 1000.0 WHERE user_id = %s", (uid,))
        cur.execute("SELECT id, resource_type, target_amount, reward_taler FROM export_contracts WHERE user_id = %s AND status = 'AVAILABLE' LIMIT 1", (uid,))
        contract = cur.fetchone()
        db_conn.commit()

    assert contract is not None
    cid = contract["id"]

    # 2. Click fulfill button
    res_fulfill = client.post(f"/market/contracts/{cid}/fulfill", headers=headers)
    assert res_fulfill.status_code == 200
    assert "erfolgreich beliefert" in res_fulfill.text


# ----------------------------------------------------------------------
# 8. EXPEDITIONS, CARAVAN DISPATCH & UNLOAD BUTTONS
# ----------------------------------------------------------------------
def test_feature_expeditions_dispatch_and_unload_buttons(db_conn):
    """Verifies outbound caravan dispatch form, arrival unloading button, and return expedition form."""
    user = make_merchant(db_conn, "exp_usr", "DANZ", balance=3000.0)
    uid = user["id"]
    client, headers = get_client(user)

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
        danz_id = cur.fetchone()["id"]
        cur.execute("SELECT id FROM regions WHERE tag = 'VISB'")
        visb_id = cur.fetchone()["id"]
        cur.execute("UPDATE inventories SET amount = 500.0 WHERE user_id = %s", (uid,))
        db_conn.commit()

    # 1. Dispatch outbound caravan to Visby
    res_dispatch = client.post(
        "/caravans/dispatch",
        data={
            "destination_region_id": visb_id,
            "cargo_wood": 30.0,
            "cargo_stone": 20.0,
        },
        headers=headers,
    )
    assert res_dispatch.status_code == 200
    assert "erfolgreich nach [VISB]" in res_dispatch.text

    # Find the caravan and fast-forward arrival
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM caravans WHERE user_id = %s AND status = 'EN_ROUTE' ORDER BY id DESC LIMIT 1", (uid,))
        caravan = cur.fetchone()
        assert caravan is not None
        cid = caravan["id"]
        cur.execute("UPDATE caravans SET status = 'ARRIVED', arrival_at = NOW() - INTERVAL '1 minute' WHERE id = %s", (cid,))
        db_conn.commit()

    # 2. Click unload button at destination depot
    res_unload = client.post(f"/caravans/{cid}/unload", headers=headers)
    assert res_unload.status_code == 200
    assert "erfolgreich im Kontor/Depot" in res_unload.text

    # 3. Dispatch return caravan from Visby back to Danzig
    res_return = client.post(
        "/caravans/dispatch",
        data={
            "origin_region_id": visb_id,
            "destination_region_id": danz_id,
            "cargo_wood": 20.0,
        },
        headers=headers,
    )
    assert res_return.status_code == 200
    assert "erfolgreich nach [DANZ]" in res_return.text

    # 4. Instant depot transfer prohibited (404)
    res_teleport = client.post(f"/caravans/depots/{visb_id}/transfer", headers=headers)
    assert res_teleport.status_code == 404


# ----------------------------------------------------------------------
# 9. GUILDS: CREATE, JOIN, WAR CHEST, MONUMENT, AUCTION, LEAVE
# ----------------------------------------------------------------------
def test_feature_guild_lifecycle_and_buttons(db_conn):
    """Verifies all guild buttons: founding, war chest donation, monuments, auctions, and leaving."""
    leader = make_merchant(db_conn, "gld_ldr", balance=5000.0)
    l_uid = leader["id"]
    l_client, l_headers = get_client(leader)

    member = make_merchant(db_conn, "gld_mbr", balance=2000.0)
    m_uid = member["id"]
    m_client, m_headers = get_client(member)

    # 1. Leader founds guild
    gtag = f"G{uuid.uuid4().hex[:4].upper()}"
    gname = f"Guild_{uuid.uuid4().hex[:6]}"
    res_create = l_client.post(
        "/guilds/create",
        data={"name": gname, "tag": gtag, "description": "Test Guild"},
        headers=l_headers,
    )
    assert res_create.status_code == 200
    assert "erfolgreich gegründet" in res_create.text

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM guilds WHERE tag = %s", (gtag,))
        gid = cur.fetchone()["id"]

    # 2. Member joins guild
    res_join = m_client.post(f"/guilds/{gid}/join", headers=m_headers)
    assert res_join.status_code == 200
    assert "erfolgreich beigetreten" in res_join.text

    # 3. War Chest deposit button
    res_deposit = l_client.post(
        "/guilds/bank/deposit",
        data={"amount": 500.0},
        headers=l_headers,
    )
    assert res_deposit.status_code == 200
    assert "Gildenkasse eingezahlt" in res_deposit.text

    # 4. Monument project contribute button
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM guild_projects WHERE guild_id = %s LIMIT 1", (gid,))
        pid = cur.fetchone()["id"]
        cur.execute("UPDATE inventories SET amount = 500.0 WHERE user_id = %s", (l_uid,))
        db_conn.commit()

    res_contrib = l_client.post(
        f"/guilds/projects/{pid}/contribute",
        data={"resource_type": "wood", "amount": 10.0},
        headers=l_headers,
    )
    assert res_contrib.status_code == 200
    assert "beigesteuert" in res_contrib.text or "vollendet" in res_contrib.text

    # 5. Kontor Auction bid button (from War Chest)
    with db_conn.cursor() as cur:
        cur.execute("SELECT id, current_highest_bid FROM kontor_auctions WHERE status = 'ACTIVE' LIMIT 1")
        auc = cur.fetchone()
        auc_id = auc["id"]
        cur_bid = float(auc["current_highest_bid"])
        next_bid = 100.0 if cur_bid == 0.0 else cur_bid + 50.0

    res_bid = l_client.post(
        f"/guilds/auctions/{auc_id}/bid",
        data={"bid_amount": next_bid},
        headers=l_headers,
    )
    assert res_bid.status_code == 200
    assert "erfolgreich abgegeben" in res_bid.text

    # 6. Member leaves guild
    res_leave_m = m_client.post("/guilds/leave", headers=m_headers)
    assert res_leave_m.status_code == 200
    assert "verlassen" in res_leave_m.text

    # 7. Leader leaves guild (triggers dissolution as sole remaining member)
    res_leave_l = l_client.post("/guilds/leave", headers=l_headers)
    assert res_leave_l.status_code == 200
    assert "aufgelöst" in res_leave_l.text


# ----------------------------------------------------------------------
# 10. NOTIFICATIONS INBOX, BADGE & MARK ALL READ BUTTON
# ----------------------------------------------------------------------
def test_feature_notifications_and_read_all(db_conn):
    """Verifies notification bell badge, dropdown list, and 'Alle gelesen' mark-all-read button."""
    user = make_merchant(db_conn, "notif_usr")
    uid = user["id"]
    client, headers = get_client(user)

    with db_conn.cursor() as cur:
        # Create an unread notification
        cur.execute(
            """
            INSERT INTO notifications (user_id, event_type, payload, is_read)
            VALUES (%s, 'STORAGE_OVERFLOW', '{"resource_type": "wood", "lost_amount": 10.0}'::jsonb, FALSE)
            """,
            (uid,),
        )
        db_conn.commit()

    # 1. Check badge count
    res_badge = client.get("/notifications/badge", headers=headers)
    assert res_badge.status_code == 200
    assert "unread-notification-badge" in res_badge.text

    # 2. Check notifications dropdown
    res_list = client.get("/notifications", headers=headers)
    assert res_list.status_code == 200
    assert "Handelsberichte" in res_list.text
    assert "Alle gelesen" in res_list.text

    # 3. Click 'Alle gelesen' button
    res_read = client.post("/notifications/read-all", headers=headers)
    assert res_read.status_code == 200
    # Badge should now be hidden or 0
    assert "unread_count" in res_read.text or "hidden" in res_read.text


# ----------------------------------------------------------------------
# 11. INLINE SVG CHARTS & ADMIN TELEMETRY
# ----------------------------------------------------------------------
def test_feature_inline_svg_charts_and_admin_telemetry(db_conn):
    """Verifies zero-dependency SVG chart generation and HTTP Basic Auth Admin Telemetry."""
    user = make_merchant(db_conn, "chart_usr")
    client, headers = get_client(user)

    # 1. Market book contains SVG chart
    res_mkt = client.get("/market/book?resource=cloth", headers=headers)
    assert res_mkt.status_code == 200
    assert "<svg" in res_mkt.text
    assert "viewBox=\"0 0 300 80\"" in res_mkt.text

    # 2. Admin Telemetry - Unauthorized without credentials
    res_unauth = client.get("/admin/economy")
    assert res_unauth.status_code == 401

    # 3. Admin Telemetry - Authorized with credentials
    res_admin = client.get(
        "/admin/economy",
        auth=(settings.ADMIN_USER, settings.ADMIN_PASS),
    )
    assert res_admin.status_code == 200
    data = res_admin.json()
    assert data["status"] == "success"
    assert "money_supply" in data
    assert "commodity_reserves" in data
    assert "market_activity_24h" in data


# ----------------------------------------------------------------------
# 12. HEALTH CHECKS
# ----------------------------------------------------------------------
def test_feature_health_checks():
    """Verifies system health endpoints."""
    with TestClient(app) as client:
        res = client.get("/health")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

        res_head = client.head("/health")
        assert res_head.status_code == 200
