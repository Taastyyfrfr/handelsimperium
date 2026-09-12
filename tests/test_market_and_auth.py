import pytest
from datetime import datetime
from app.config import settings, BUILDING_CONFIG
from app.auth import hash_password, verify_password, create_session_token, decode_session_token
from app.engine.matching import place_and_match_order, cancel_order, get_order_book
from app.engine.production import ensure_user_entities, upgrade_building

def test_password_and_session_tokens():
    raw_pass = "medieval_secret_99"
    hashed = hash_password(raw_pass)
    assert verify_password(raw_pass, hashed)
    assert not verify_password("wrong_pass", hashed)

    token = create_session_token(42, "KaufmannAnton")
    decoded = decode_session_token(token)
    assert decoded is not None
    assert decoded["user_id"] == 42
    assert decoded["username"] == "KaufmannAnton"

def test_building_upgrade_mechanics(db_conn):
    ts = int(datetime.now().timestamp() * 1000)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 1000.00) RETURNING id",
            (f"upgrader_{ts}", hash_password("pw")),
        )
        user_id = cur.fetchone()["id"]
        ensure_user_entities(cur, user_id)

        # Upgrade lumberjack
        res = upgrade_building(cur, user_id, "lumberjack")
        assert res["success"] is True
        assert res["new_level"] == 2
        assert res["new_balance"] < 1000.00

        # Verify in DB
        cur.execute("SELECT level, production_rate FROM buildings WHERE user_id = %s AND building_type = 'lumberjack'", (user_id,))
        b = cur.fetchone()
        assert b["level"] == 2
        assert float(b["production_rate"]) > BUILDING_CONFIG["lumberjack"]["base_rate"]

def test_order_cancellation_and_escrow_refund(db_conn):
    ts = int(datetime.now().timestamp() * 1000)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, %s, 500.00) RETURNING id",
            (f"canceller_{ts}", hash_password("pw")),
        )
        user_id = cur.fetchone()["id"]
        ensure_user_entities(cur, user_id)

        # Place BUY order for 10 stone @ 5.00 = 50.00 escrow
        order_res = place_and_match_order(cur, user_id, "BUY", "stone", 10.0, 5.0)
        order_id = order_res["order_id"]

        cur.execute("SELECT balance FROM users WHERE id = %s", (user_id,))
        bal_after_order = float(cur.fetchone()["balance"])
        assert bal_after_order == pytest.approx(450.00, abs=0.01)

        # Cancel the order
        cancel_res = cancel_order(cur, user_id, order_id)
        assert cancel_res["success"] is True

        cur.execute("SELECT balance FROM users WHERE id = %s", (user_id,))
        bal_after_cancel = float(cur.fetchone()["balance"])
        assert bal_after_cancel == pytest.approx(500.00, abs=0.01)
