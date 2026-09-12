import pytest
import time
from starlette.testclient import TestClient

from app.main import app
from app.config import settings, BUILDING_CONFIG
from app.auth import create_session_token
from app.engine.production import ensure_user_entities, upgrade_building, calculate_offline_production

def test_registration_validation_for_regions(db_conn):
    """
    Verifies that registration validates the Hanseatic region_id:
    - Invalid region_id (non-existent) -> 400 Bad Request
    - Valid region_id (Visby) -> 303 Redirect and persists region_id
    - Missing region_id -> defaults to Danzig fallback
    """
    with TestClient(app) as client:
        ts = int(time.time() * 1000)

        # Acquire CSRF token
        init_res = client.get("/auth/register")
        assert init_res.status_code == 200
        csrf_token = client.cookies.get("imperium_csrf")
        headers = {"X-CSRF-Token": csrf_token} if csrf_token else {}

        # 1. Non-existent region_id
        res_invalid = client.post(
            "/auth/register",
            data={
                "username": f"reg_fail2_{ts}",
                "password": "secretpassword",
                "confirm_password": "secretpassword",
                "region_id": 99999,
            },
            headers=headers,
            follow_redirects=False,
        )
        assert res_invalid.status_code == 400
        assert "Ungültige Heimatregion gewählt" in res_invalid.text

        # 2. Valid region (Visby)
        with db_conn.cursor() as cur:
            cur.execute("SELECT id FROM regions WHERE tag = 'VISB'")
            visb_id = cur.fetchone()["id"]

        valid_username = f"kaufmann_visby_{ts}"
        res_valid = client.post(
            "/auth/register",
            data={
                "username": valid_username,
                "password": "secretpassword",
                "confirm_password": "secretpassword",
                "region_id": visb_id,
            },
            headers=headers,
            follow_redirects=False,
        )
        assert res_valid.status_code == 303

        with db_conn.cursor() as cur:
            cur.execute("SELECT id, region_id FROM users WHERE username = %s", (valid_username,))
            u = cur.fetchone()
            assert u is not None
            assert u["region_id"] == visb_id

        # 3. Default fallback region (Danzig) when region_id omitted
        default_username = f"kaufmann_danz_{ts}"
        res_default = client.post(
            "/auth/register",
            data={
                "username": default_username,
                "password": "secretpassword",
                "confirm_password": "secretpassword",
            },
            headers=headers,
            follow_redirects=False,
        )
        assert res_default.status_code == 303

        with db_conn.cursor() as cur:
            cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
            danz_id = cur.fetchone()["id"]
            cur.execute("SELECT id, region_id FROM users WHERE username = %s", (default_username,))
            u_def = cur.fetchone()
            assert u_def is not None
            assert u_def["region_id"] == danz_id

def test_initial_production_rates_reflect_regional_multipliers(db_conn):
    """
    Verifies that initial production rates for commodity buildings reflect regional multipliers:
    Danzig: Lumberjack 1.5x (0.25*1.5=0.375), Farm 1.5x (0.30*1.5=0.45), Quarry 0.8x (0.20*0.8=0.16), Mine 0.0x (0.0), Weaver 0.0x (0.0).
    Visby: Mine 1.6x (0.10*1.6=0.16), Quarry 1.4x (0.20*1.4=0.28), Lumberjack 1.0x (0.25*1.0=0.25), Farm 0.0x (0.0), Weaver 0.0x (0.0).
    """
    ts = int(time.time() * 1000)

    with db_conn.cursor() as cur:
        # Fetch region IDs
        cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
        danz_id = cur.fetchone()["id"]
        cur.execute("SELECT id FROM regions WHERE tag = 'VISB'")
        visb_id = cur.fetchone()["id"]

        # Create Danzig user
        cur.execute(
            "INSERT INTO users (username, password_hash, balance, region_id) VALUES (%s, 'hash', 1000.0, %s) RETURNING id",
            (f"danz_user_{ts}", danz_id),
        )
        danz_user_id = cur.fetchone()["id"]
        ensure_user_entities(cur, danz_user_id)

        # Create Visby user
        cur.execute(
            "INSERT INTO users (username, password_hash, balance, region_id) VALUES (%s, 'hash', 1000.0, %s) RETURNING id",
            (f"visb_user_{ts}", visb_id),
        )
        visb_user_id = cur.fetchone()["id"]
        ensure_user_entities(cur, visb_user_id)

        db_conn.commit()

        # Check Danzig building rates
        cur.execute("SELECT building_type, production_rate FROM buildings WHERE user_id = %s", (danz_user_id,))
        danz_rates = {r["building_type"]: float(r["production_rate"]) for r in cur.fetchall()}

        assert danz_rates["lumberjack"] == 0.375    # 0.25 * 1.5
        assert danz_rates["farm"] == 0.45          # 0.30 * 1.5
        assert danz_rates["quarry"] == 0.16        # 0.20 * 0.8
        assert danz_rates["mine"] == 0.0           # 0.10 * 0.0
        assert danz_rates["weaver"] == 0.0         # 0.08 * 0.0
        assert danz_rates["warehouse"] == 0.0

        # Check Visby building rates
        cur.execute("SELECT building_type, production_rate FROM buildings WHERE user_id = %s", (visb_user_id,))
        visb_rates = {r["building_type"]: float(r["production_rate"]) for r in cur.fetchall()}

        assert visb_rates["mine"] == 0.16          # 0.10 * 1.6
        assert visb_rates["quarry"] == 0.28        # 0.20 * 1.4
        assert visb_rates["lumberjack"] == 0.25    # 0.25 * 1.0
        assert visb_rates["farm"] == 0.0           # 0.30 * 0.0
        assert visb_rates["weaver"] == 0.0         # 0.08 * 0.0
        assert visb_rates["warehouse"] == 0.0

def test_blocked_upgrade_for_zero_yield_commodity(db_conn):
    """
    Verifies that upgrading a building for a resource with 0.0 multiplier is blocked
    with: "Dieser Rohstoff kann in Eurer Region nicht gewonnen werden."
    """
    ts = int(time.time() * 1000)

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
        danz_id = cur.fetchone()["id"]

        cur.execute(
            "INSERT INTO users (username, password_hash, balance, region_id) VALUES (%s, 'hash', 10000.0, %s) RETURNING id",
            (f"danz_block_{ts}", danz_id),
        )
        u_id = cur.fetchone()["id"]
        ensure_user_entities(cur, u_id)
        # Give ample resources
        cur.execute("UPDATE inventories SET amount = 1000.0 WHERE user_id = %s", (u_id,))
        db_conn.commit()

        # Try to upgrade mine (iron - 0.0 in Danzig)
        with pytest.raises(ValueError, match="Dieser Rohstoff kann in Eurer Region nicht gewonnen werden."):
            upgrade_building(cur, u_id, "mine")

        # Try to upgrade weaver (cloth - 0.0 in Danzig)
        with pytest.raises(ValueError, match="Dieser Rohstoff kann in Eurer Region nicht gewonnen werden."):
            upgrade_building(cur, u_id, "weaver")

def test_warehouse_upgrade_allowed_regardless_of_region(db_conn):
    """
    Verifies that warehouse (Zentrallager) has resource=None and is unaffected by regional restrictions.
    """
    ts = int(time.time() * 1000)

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
        danz_id = cur.fetchone()["id"]

        cur.execute(
            "INSERT INTO users (username, password_hash, balance, region_id) VALUES (%s, 'hash', 10000.0, %s) RETURNING id",
            (f"danz_wh_{ts}", danz_id),
        )
        u_id = cur.fetchone()["id"]
        ensure_user_entities(cur, u_id)
        cur.execute("UPDATE inventories SET amount = 1000.0 WHERE user_id = %s", (u_id,))
        db_conn.commit()

        res = upgrade_building(cur, u_id, "warehouse")
        db_conn.commit()

        assert res["success"] is True
        assert res["new_level"] == 2
        assert res["new_storage_cap"] > settings.BASE_STORAGE_CAP

def test_upgrade_scaling_with_regional_multiplier(db_conn):
    """
    Verifies that upgrading a valid building correctly scales using:
    effective_rate = round(base_rate * 1.25^(level-1) * region_multiplier, 4)
    Danzig Lumberjack (base_rate = 0.25, multiplier = 1.5):
    Level 1: 0.25 * 1.5 = 0.375
    Level 2: round(0.25 * 1.25^1 * 1.5, 4) = 0.4688
    """
    ts = int(time.time() * 1000)

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
        danz_id = cur.fetchone()["id"]

        cur.execute(
            "INSERT INTO users (username, password_hash, balance, region_id) VALUES (%s, 'hash', 10000.0, %s) RETURNING id",
            (f"danz_scale_{ts}", danz_id),
        )
        u_id = cur.fetchone()["id"]
        ensure_user_entities(cur, u_id)
        cur.execute("UPDATE inventories SET amount = 1000.0 WHERE user_id = %s", (u_id,))
        db_conn.commit()

        res = upgrade_building(cur, u_id, "lumberjack")
        db_conn.commit()

        assert res["success"] is True
        assert res["new_level"] == 2
        assert res["new_rate"] == 0.4688

def test_handbook_and_dashboard_render_regions(db_conn):
    """
    Verifies that the /handbuch endpoint returns the Hanseatic Regional Matrix,
    the dashboard shell displays the merchant's home region badge,
    and /resources/overview displays regional modifiers and import-only indicators.
    """
    ts = int(time.time() * 1000)
    with TestClient(app) as client:
        with db_conn.cursor() as cur:
            cur.execute("SELECT id FROM regions WHERE tag = 'KOEL'")
            koel_id = cur.fetchone()["id"]

            username = f"koeln_merchant_{ts}"
            cur.execute(
                "INSERT INTO users (username, password_hash, balance, region_id) VALUES (%s, 'hash', 1000.0, %s) RETURNING id",
                (username, koel_id),
            )
            u_id = cur.fetchone()["id"]
            ensure_user_entities(cur, u_id)
            db_conn.commit()

        token = create_session_token(u_id, username)
        client.cookies.set(settings.COOKIE_NAME, token)

        # 1. Handbook test
        res_hb = client.get("/handbuch")
        assert res_hb.status_code == 200
        assert "Hanseatische Regionalmatrix" in res_hb.text
        assert "DANZ" in res_hb.text
        assert "VISB" in res_hb.text
        assert "BRUG" in res_hb.text
        assert "KOEL" in res_hb.text

        # 2. Dashboard shell test
        res_dash = client.get("/")
        assert res_dash.status_code == 200
        assert "KOEL" in res_dash.text
        assert "Rheinland (Köln)" in res_dash.text

        # 3. Resources overview partial test
        res_overview = client.get("/resources/overview")
        assert res_overview.status_code == 200
        assert "Reine Importware" in res_overview.text
        assert "Nicht förderbar" in res_overview.text
