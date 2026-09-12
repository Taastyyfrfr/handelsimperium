import pytest
import time
import json
from starlette.testclient import TestClient

from app.main import app
from app.config import settings, SUPPORTED_RESOURCES
from app.auth import create_session_token
from app.engine.production import ensure_user_entities
from app.engine.matching import place_and_match_order
from app.engine.contracts import ensure_daily_contracts, fulfill_export_contract
from app.engine.notifications import (
    get_user_notifications,
    get_unread_notification_count,
    mark_all_notifications_read,
)

def test_export_contract_generation_and_fulfillment(db_conn):
    """
    Verifies daily export contracts ("Handelskarawanen"):
    - Generation of exactly 3 distinct daily contracts per merchant.
    - Idempotency on repeated retrieval.
    - Insufficient inventory rejection.
    - Atomic commodity burn and balance payout upon fulfillment.
    - Double-claim prevention.
    """
    ts = int(time.time() * 1000)
    username = f"karawane_{ts}"

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 100.0) RETURNING id",
            (username,),
        )
        u_id = cur.fetchone()["id"]
        ensure_user_entities(cur, u_id)
        db_conn.commit()

        # 1. Generate daily contracts
        contracts = ensure_daily_contracts(cur, u_id)
        db_conn.commit()
        assert len(contracts) == 3

        distinct_resources = {c["resource_type"] for c in contracts}
        assert len(distinct_resources) == 3
        for c in contracts:
            assert c["status"] == "AVAILABLE"
            assert c["target_amount"] > 0
            assert c["reward_taler"] > 0
            assert c["expires_at"] is not None

        # 2. Idempotent check
        contracts_again = ensure_daily_contracts(cur, u_id)
        assert [c["id"] for c in contracts] == [c["id"] for c in contracts_again]

        # 3. Fulfillment failure when insufficient inventory
        c0 = contracts[0]
        res0 = c0["resource_type"]
        cur.execute("UPDATE inventories SET amount = 0.0 WHERE user_id = %s AND resource_type = %s", (u_id, res0))
        db_conn.commit()

        with pytest.raises(ValueError, match="Nicht genügend"):
            fulfill_export_contract(cur, u_id, c0["id"])
        db_conn.rollback()

        # 4. Successful fulfillment when sufficient inventory
        req_amt = c0["target_amount"]
        reward = c0["reward_taler"]
        cur.execute("UPDATE inventories SET amount = %s WHERE user_id = %s AND resource_type = %s", (req_amt + 50.0, u_id, res0))
        db_conn.commit()

        ful_res = fulfill_export_contract(cur, u_id, c0["id"])
        db_conn.commit()

        assert ful_res["contract_id"] == c0["id"]
        assert ful_res["reward"] == reward
        assert ful_res["new_balance"] == round(100.0 + reward, 2)

        # Verify inventory was deducted (burned from circulation)
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = %s", (u_id, res0))
        rem_inv = float(cur.fetchone()["amount"])
        assert rem_inv == 50.0

        # Verify contract status is FULFILLED
        cur.execute("SELECT status, fulfilled_at FROM export_contracts WHERE id = %s", (c0["id"],))
        updated_c = cur.fetchone()
        assert updated_c["status"] == "FULFILLED"
        assert updated_c["fulfilled_at"] is not None

        # 5. Double-claim prevention
        with pytest.raises(ValueError, match="bereits"):
            fulfill_export_contract(cur, u_id, c0["id"])
        db_conn.rollback()

def test_trade_execution_notification_dispatch(db_conn):
    """
    Verifies that executing a matched trade automatically creates structured
    notification alerts for both buyer and seller.
    """
    ts = int(time.time() * 1000)
    user_buyer = f"buyer_notif_{ts}"
    user_seller = f"seller_notif_{ts}"

    with db_conn.cursor() as cur:
        # Create buyer & seller
        cur.execute("INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 1000.0) RETURNING id", (user_buyer,))
        b_id = cur.fetchone()["id"]
        ensure_user_entities(cur, b_id)

        cur.execute("INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 100.0) RETURNING id", (user_seller,))
        s_id = cur.fetchone()["id"]
        ensure_user_entities(cur, s_id)
        cur.execute("UPDATE inventories SET amount = 50.0 WHERE user_id = %s AND resource_type = 'wood'", (s_id,))
        cur.execute("DELETE FROM market_orders WHERE resource_type = 'wood'")
        db_conn.commit()

        # Seller places SELL order: 10 wood @ 5.00 Taler
        place_and_match_order(cur, s_id, "SELL", "wood", 10.0, 5.00)
        db_conn.commit()

        # Buyer places BUY order: 10 wood @ 5.00 Taler -> Match!
        res_buy = place_and_match_order(cur, b_id, "BUY", "wood", 10.0, 5.00)
        db_conn.commit()
        assert len(res_buy["trades"]) == 1

        # Check buyer notifications
        b_notifs = get_user_notifications(cur, b_id)
        assert len(b_notifs) >= 1
        b_trade_notif = next(n for n in b_notifs if n["event_type"] == "TRADE_EXECUTED")
        assert b_trade_notif["payload"]["role"] == "BUYER"
        assert b_trade_notif["payload"]["resource_type"] == "wood"
        assert b_trade_notif["payload"]["amount"] == 10.0
        assert b_trade_notif["payload"]["price"] == 5.00
        assert b_trade_notif["is_read"] is False

        # Check seller notifications
        s_notifs = get_user_notifications(cur, s_id)
        assert len(s_notifs) >= 1
        s_trade_notif = next(n for n in s_notifs if n["event_type"] == "TRADE_EXECUTED")
        assert s_trade_notif["payload"]["role"] == "SELLER"
        assert s_trade_notif["payload"]["resource_type"] == "wood"
        assert s_trade_notif["payload"]["amount"] == 10.0
        assert s_trade_notif["payload"]["price"] == 5.00
        assert s_trade_notif["payload"]["fee"] == 1.00  # 2% of 50.00
        assert s_trade_notif["payload"]["payout"] == 49.00
        assert s_trade_notif["is_read"] is False

        # Verify unread counts
        assert get_unread_notification_count(cur, b_id) >= 1
        assert get_unread_notification_count(cur, s_id) >= 1

        # Mark all read for buyer
        mark_all_notifications_read(cur, b_id)
        db_conn.commit()
        assert get_unread_notification_count(cur, b_id) == 0

def test_economic_telemetry_authentication_and_accuracy(db_conn):
    """
    Verifies the economic telemetry endpoint:
    - Rejection (401) on missing/invalid Basic Auth.
    - Acceptance (200) on valid admin credentials.
    - Consistency of aggregated macro sums.
    """
    client = TestClient(app)

    # 1. Unauthorized access
    r_no_auth = client.get("/admin/economy")
    assert r_no_auth.status_code == 401

    r_bad_auth = client.get("/admin/economy", auth=("wrong_admin", "bad_pass"))
    assert r_bad_auth.status_code == 401

    # 2. Authorized access
    r_auth = client.get("/admin/economy", auth=(settings.ADMIN_USER, settings.ADMIN_PASS))
    assert r_auth.status_code == 200
    telemetry = r_auth.json()
    assert telemetry["status"] == "success"

    # Verify money supply keys
    ms = telemetry["money_supply"]
    assert "circulating_taler" in ms
    assert "escrowed_buyer_taler" in ms
    assert "total_currency_supply" in ms
    assert ms["total_currency_supply"] == round(ms["circulating_taler"] + ms["escrowed_buyer_taler"], 2)

    # Verify commodity reserves
    cr = telemetry["commodity_reserves"]
    assert "breakdown" in cr
    assert "total_units_in_circulation" in cr
    for res in SUPPORTED_RESOURCES:
        assert res in cr["breakdown"]

    # Verify 24h market activity and sinks
    assert "market_activity_24h" in telemetry
    assert "system_sinks" in telemetry
    assert "lifetime_burned_market_fees" in telemetry["system_sinks"]
    assert "merchants" in telemetry

def test_http_contracts_and_notifications_flow(db_conn):
    """
    Tests HTMX endpoints for export contracts and notification inbox.
    """
    ts = int(time.time() * 1000)
    username = f"htmx_user_{ts}"

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 500.0) RETURNING id",
            (username,),
        )
        u_id = cur.fetchone()["id"]
        ensure_user_entities(cur, u_id)
        db_conn.commit()

    token = create_session_token(u_id, username)
    client = TestClient(app)
    client.cookies.set(settings.COOKIE_NAME, token)

    # Acquire CSRF cookie
    init_res = client.get("/health")
    csrf_token = init_res.cookies.get(settings.CSRF_COOKIE_NAME, "mock_csrf_token_value_32_bytes_long")
    client.cookies.set(settings.CSRF_COOKIE_NAME, csrf_token)

    # 1. View contracts partial
    c_res = client.get("/market/contracts")
    assert c_res.status_code == 200
    assert "Handelskarawanen" in c_res.text
    assert "Abreise der Karawane" in c_res.text

    # 2. View notifications partial
    n_res = client.get("/notifications")
    assert n_res.status_code == 200
    assert "Handelsberichte" in n_res.text

    # 3. View badge partial
    b_res = client.get("/notifications/badge")
    assert b_res.status_code == 200

    # 4. Mark all read
    read_res = client.post("/notifications/read-all", headers={"X-CSRF-Token": csrf_token})
    assert read_res.status_code == 200
    assert "Handelsberichte" in read_res.text
