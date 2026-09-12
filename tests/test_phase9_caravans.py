import pytest
import math
import time
from datetime import datetime, timezone, timedelta
from starlette.testclient import TestClient

from app.main import app
from app.config import CARAVAN_MAX_CARGO, TRANSIT_SPEED_FACTOR
from app.auth import create_session_token
from app.engine.production import ensure_user_entities
from app.engine.caravans import (
    calculate_distance,
    calculate_travel_duration,
    resolve_caravan_statuses,
    dispatch_caravan,
    unload_caravan,
    transfer_depot_to_kontor,
    get_expeditions_overview,
)
from app.engine.tutorial import (
    ensure_user_tutorial,
    is_step_eligible,
    claim_tutorial_reward,
    TUTORIAL_STEPS,
)


def create_test_merchant(db_conn, username_suffix: str, region_tag: str = "DANZ"):
    """Helper to create a merchant in the specified region with initialized entities."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = %s", (region_tag,))
        reg = cur.fetchone()
        region_id = reg["id"]

        cur.execute(
            """
            INSERT INTO users (username, password_hash, balance, region_id)
            VALUES (%s, 'hash', 1000.0, %s)
            RETURNING id, username, balance, region_id
            """,
            (f"merch_{username_suffix}", region_id),
        )
        user = cur.fetchone()
        ensure_user_entities(cur, user["id"])
        db_conn.commit()
    return user


def test_distance_and_travel_duration_calculation(db_conn):
    """
    Validates Euclidean coordinate distance and duration calculation formulas.
    Danzig: (250, 120), Visby: (280, 50).
    Distance = sqrt(30^2 + (-70)^2) = sqrt(5800) = 76.16 sm.
    Duration = round(76.16 * 12) = 914s.
    """
    d = calculate_distance(250, 120, 280, 50)
    assert d == 76.16
    duration = calculate_travel_duration(d)
    assert duration == int(round(76.16 * TRANSIT_SPEED_FACTOR))
    assert duration == 914

    # Distance to same point is 0
    assert calculate_distance(100, 100, 100, 100) == 0.0
    assert calculate_travel_duration(0.0) == 0


def test_caravan_dispatch_atomic_deduction_and_capacity_limit(db_conn):
    """
    Verifies that caravan dispatching:
    - Enforces 250 units max cargo capacity
    - Rejects negative or zero cargo
    - Rejects dispatch to the same region
    - Rejects when inventory is insufficient
    - Deducts inventory atomically
    - Inserts EN_ROUTE caravan record with valid arrival timestamp
    """
    ts = int(time.time() * 1000)
    user = create_test_merchant(db_conn, f"disp_{ts}", "DANZ")
    uid = user["id"]

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
        danz_id = cur.fetchone()["id"]
        cur.execute("SELECT id FROM regions WHERE tag = 'VISB'")
        visb_id = cur.fetchone()["id"]

        # 1. Reject same region
        with pytest.raises(ValueError, match="Zielregion muss sich von der Heimatregion unterscheiden"):
            dispatch_caravan(cur, uid, danz_id, {"wood": 10.0})

        # 2. Reject zero cargo
        with pytest.raises(ValueError, match="Mindestens eine Handelsware"):
            dispatch_caravan(cur, uid, visb_id, {"wood": 0.0})

        # 3. Reject negative cargo
        with pytest.raises(ValueError, match="Negative Werte sind unzulässig"):
            dispatch_caravan(cur, uid, visb_id, {"wood": -5.0})

        # 4. Reject capacity > 250
        with pytest.raises(ValueError, match="Ladekapazität überschritten"):
            dispatch_caravan(cur, uid, visb_id, {"wood": 200.0, "stone": 60.0})

        # 5. Reject insufficient inventory (starter wood is 50.0)
        with pytest.raises(ValueError, match="Nicht genügend wood im Kontor"):
            dispatch_caravan(cur, uid, visb_id, {"wood": 80.0})

        # 6. Valid dispatch (30 wood, 15 stone)
        res = dispatch_caravan(cur, uid, visb_id, {"wood": 30.0, "stone": 15.0})
        db_conn.commit()

        assert res["total_cargo"] == 45.0
        assert res["status"] == "EN_ROUTE"
        assert res["distance"] == 76.16
        assert res["duration_seconds"] == 914
        assert res["dest_tag"] == "VISB"

        # Check inventory deduction
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'wood'", (uid,))
        assert float(cur.fetchone()["amount"]) == 20.0  # 50 - 30

        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'stone'", (uid,))
        assert float(cur.fetchone()["amount"]) == 35.0  # 50 - 15


def test_caravan_arrival_and_depot_unloading(db_conn):
    """
    Verifies arrival status transition from EN_ROUTE to ARRIVED,
    preventing premature unloading, and unloading into foreign regional depots.
    """
    ts = int(time.time() * 1000)
    user = create_test_merchant(db_conn, f"arr_{ts}", "DANZ")
    uid = user["id"]

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = 'VISB'")
        visb_id = cur.fetchone()["id"]

        # 1. Dispatch caravan
        res = dispatch_caravan(cur, uid, visb_id, {"wood": 25.0, "cloth": 5.0})
        caravan_id = res["id"]
        db_conn.commit()

        # Try to unload while still en route
        with pytest.raises(ValueError, match="befindet sich noch auf der Reise"):
            unload_caravan(cur, uid, caravan_id)

        # 2. Simulate arrival by setting arrival_at in the past
        cur.execute(
            "UPDATE caravans SET arrival_at = NOW() - INTERVAL '5 seconds' WHERE id = %s",
            (caravan_id,),
        )
        db_conn.commit()

        # Resolve status
        resolved = resolve_caravan_statuses(cur, uid)
        db_conn.commit()
        assert resolved == 1

        cur.execute("SELECT status FROM caravans WHERE id = %s", (caravan_id,))
        assert cur.fetchone()["status"] == "ARRIVED"

        # 3. Unload caravan into depot
        unload_res = unload_caravan(cur, uid, caravan_id)
        db_conn.commit()
        assert unload_res["dest_tag"] == "VISB"

        cur.execute("SELECT status FROM caravans WHERE id = %s", (caravan_id,))
        assert cur.fetchone()["status"] == "UNLOADED"

        # Check foreign depot records
        cur.execute(
            "SELECT resource_type, amount FROM regional_depots WHERE user_id = %s AND region_id = %s",
            (uid, visb_id),
        )
        depots = {r["resource_type"]: float(r["amount"]) for r in cur.fetchall()}
        assert depots["wood"] == 25.0
        assert depots["cloth"] == 5.0

        # Cannot unload again
        with pytest.raises(ValueError, match="bereits vollständig entladen"):
            unload_caravan(cur, uid, caravan_id)


def test_depot_transfer_to_kontor(db_conn):
    """
    Verifies transferring goods from a foreign regional depot back into the Kontor,
    respecting the warehouse storage capacity limit.
    """
    ts = int(time.time() * 1000)
    user = create_test_merchant(db_conn, f"trf_{ts}", "DANZ")
    uid = user["id"]

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = 'BRUG'")
        brug_id = cur.fetchone()["id"]

        # Seed foreign depot with 100.0 cloth
        cur.execute(
            """
            INSERT INTO regional_depots (user_id, region_id, resource_type, amount)
            VALUES (%s, %s, 'cloth', 100.0)
            """,
            (uid, brug_id),
        )
        db_conn.commit()

        # Kontor starter cloth is 10.0
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'cloth'", (uid,))
        assert float(cur.fetchone()["amount"]) == 10.0

        # Transfer cloth to Kontor
        trf_res = transfer_depot_to_kontor(cur, uid, brug_id, "cloth")
        db_conn.commit()

        assert trf_res["transferred"]["cloth"] == 100.0
        assert trf_res["total_transferred"] == 100.0

        # Check updated Kontor inventory
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'cloth'", (uid,))
        assert float(cur.fetchone()["amount"]) == 110.0  # 10 + 100

        # Check updated depot amount (now 0.0)
        cur.execute(
            "SELECT amount FROM regional_depots WHERE user_id = %s AND region_id = %s AND resource_type = 'cloth'",
            (uid, brug_id),
        )
        assert float(cur.fetchone()["amount"]) == 0.0

        # Further transfer should fail
        with pytest.raises(ValueError, match="Keine Waren im ausgewählten Depot"):
            transfer_depot_to_kontor(cur, uid, brug_id, "cloth")


def test_tutorial_step6_expedition_completion(db_conn):
    """
    Verifies that dispatching a caravan qualifies for Tutorial Step 6 ("Die erste Expedition"),
    and claiming awards 100.00 Taler + 30.00 Tuch, successfully completing the 6-step curriculum.
    """
    ts = int(time.time() * 1000)
    user = create_test_merchant(db_conn, f"tut6_{ts}", "DANZ")
    uid = user["id"]

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM regions WHERE tag = 'KOEL'")
        koel_id = cur.fetchone()["id"]

        # Ensure tutorial exists and set current step to 6
        ensure_user_tutorial(cur, uid)
        cur.execute(
            "UPDATE user_tutorials SET current_step = 6, completed_steps = '[1,2,3,4,5]'::jsonb WHERE user_id = %s",
            (uid,),
        )
        db_conn.commit()

        # Step 6 not eligible before dispatch
        assert is_step_eligible(cur, uid, 6) is False

        # Dispatch caravan
        dispatch_caravan(cur, uid, koel_id, {"wood": 10.0})
        db_conn.commit()

        # Step 6 is now eligible
        assert is_step_eligible(cur, uid, 6) is True

        # Claim reward
        claim_res = claim_tutorial_reward(cur, uid)
        db_conn.commit()

        assert claim_res["claimed_step"] == 6
        assert claim_res["is_finished"] is True

        # Verify rewards disbursed (100 Taler, 30 Cloth)
        cur.execute("SELECT balance FROM users WHERE id = %s", (uid,))
        assert float(cur.fetchone()["balance"]) == 1100.0  # 1000 + 100

        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'cloth'", (uid,))
        assert float(cur.fetchone()["amount"]) == 40.0  # 10 starter + 30 reward


def test_expeditions_http_endpoints_and_handbook_rendering(db_conn):
    """
    Verifies HTTP rendering:
    - GET /expeditions returns 200 OK and contains UI elements
    - POST /caravans/dispatch via form returns 200 OK and dispatches caravan
    - GET /handbuch includes Section 8 with transit formula
    """
    with TestClient(app) as client:
        ts = int(time.time() * 1000)
        user = create_test_merchant(db_conn, f"http_{ts}", "DANZ")
        uid = user["id"]
        token = create_session_token(uid, user["username"])
        client.cookies.set("imperium_session", token)

        # 1. Test GET /expeditions
        res_exp = client.get("/expeditions")
        assert res_exp.status_code == 200
        assert "Hanseatische Expeditionen" in res_exp.text
        assert "Neue Expedition ausrüsten" in res_exp.text
        assert "Ausländische Depots" in res_exp.text

        # 2. Test POST /caravans/dispatch via HTTP
        with db_conn.cursor() as cur:
            cur.execute("SELECT id FROM regions WHERE tag = 'VISB'")
            visb_id = cur.fetchone()["id"]

        init_res = client.get("/auth/register")
        csrf_token = client.cookies.get("imperium_csrf")
        headers = {"X-CSRF-Token": csrf_token} if csrf_token else {}

        res_dispatch = client.post(
            "/caravans/dispatch",
            data={
                "destination_region_id": visb_id,
                "cargo_wood": 20.0,
                "cargo_stone": 10.0,
                "cargo_iron": 0.0,
                "cargo_grain": 0.0,
                "cargo_cloth": 0.0,
            },
            headers=headers,
        )
        assert res_dispatch.status_code == 200
        assert "Karawane #" in res_dispatch.text
        assert "erfolgreich nach [VISB]" in res_dispatch.text

        # 3. Test GET /handbuch has Section 8 with formulas
        res_hb = client.get("/handbuch")
        assert res_hb.status_code == 200
        assert "8. Logistik, Übersee-Expeditionen &amp; Regionaldepots" in res_hb.text or "Logistik, Übersee-Expeditionen" in res_hb.text
        assert "Reisedauer = round(Distanz × 12)" in res_hb.text
        assert "250 Gütereinheiten" in res_hb.text
