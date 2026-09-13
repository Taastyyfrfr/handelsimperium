import pytest
import time
import json
from starlette.testclient import TestClient

from app.main import app
from app.config import settings, BUILDING_CONFIG, GUILD_PROJECT_CONFIG
from app.auth import create_session_token
from app.engine.production import ensure_user_entities, upgrade_building
from app.engine.matching import place_and_match_order
from app.engine.guilds import create_guild
from app.engine.tutorial import (
    ensure_user_tutorial,
    is_step_eligible,
    claim_tutorial_reward,
    get_tutorial_status,
    TUTORIAL_STEPS,
)

def test_tutorial_progression_and_rewards(db_conn):
    """
    Verifies full 5-step onboarding quest line ("Kaufmannslehre"):
    - Step 1 (Bestandsaufnahme): 25 Taler reward.
    - Step 2 (Expansion): Requires building level >= 2 -> 50 Taler reward.
    - Step 3 (Marktzugang): Requires active order -> 25 wood & 25 stone.
    - Step 4 (Fernhandel): Requires caravan or >= 2 orders -> 100 Taler.
    - Step 5 (Zunftbeitritt): Requires guild membership -> 150 Taler.
    - Post-completion: is_finished = True and further claims rejected.
    """
    ts = int(time.time() * 1000)
    username = f"lehrling_{ts}"

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 100.0) RETURNING id",
            (username,),
        )
        u_id = cur.fetchone()["id"]
        ensure_user_entities(cur, u_id)
        # Give materials for building upgrade
        cur.execute(
            "UPDATE inventories SET amount = 200.0 WHERE user_id = %s",
            (u_id,),
        )
        db_conn.commit()

        # 1. Initial tutorial state
        tut = ensure_user_tutorial(cur, u_id)
        db_conn.commit()
        assert tut["current_step"] == 1
        assert tut["is_finished"] is False

        # Step 1 is immediately eligible
        assert is_step_eligible(cur, u_id, 1) is True

        # Claim Step 1
        res1 = claim_tutorial_reward(cur, u_id)
        db_conn.commit()
        assert res1["claimed_step"] == 1
        assert res1["next_step"] == 2

        # Check balance: 100 + 25 = 125
        cur.execute("SELECT balance FROM users WHERE id = %s", (u_id,))
        assert float(cur.fetchone()["balance"]) == 125.0

        # 2. Step 2 is not yet eligible (all buildings at level 1)
        assert is_step_eligible(cur, u_id, 2) is False
        with pytest.raises(ValueError, match="Aufgabe noch nicht erfüllt"):
            claim_tutorial_reward(cur, u_id)
        db_conn.rollback()

        # Upgrade a building to level 2
        upgrade_building(cur, u_id, "lumberjack")
        db_conn.commit()
        assert is_step_eligible(cur, u_id, 2) is True

        # Claim Step 2
        res2 = claim_tutorial_reward(cur, u_id)
        db_conn.commit()
        assert res2["claimed_step"] == 2
        assert res2["next_step"] == 3

        # Check balance: was deducted for upgrade, then +50 Taler reward
        cur.execute("SELECT balance FROM users WHERE id = %s", (u_id,))
        bal_after_step2 = float(cur.fetchone()["balance"])
        assert bal_after_step2 > 0

        # 3. Step 3 is not yet eligible (no orders placed)
        assert is_step_eligible(cur, u_id, 3) is False
        with pytest.raises(ValueError, match="Aufgabe noch nicht erfüllt"):
            claim_tutorial_reward(cur, u_id)
        db_conn.rollback()

        # Place an active order
        place_and_match_order(cur, u_id, "SELL", "wood", 5.0, 5.00)
        db_conn.commit()
        assert is_step_eligible(cur, u_id, 3) is True

        # Claim Step 3 (Reward: 25 wood & 25 stone)
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'wood'", (u_id,))
        wood_before = float(cur.fetchone()["amount"])
        res3 = claim_tutorial_reward(cur, u_id)
        db_conn.commit()
        assert res3["claimed_step"] == 3
        assert res3["next_step"] == 4

        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'wood'", (u_id,))
        wood_after = float(cur.fetchone()["amount"])
        assert wood_after == wood_before + 25.0

        # 4. Step 4: Requires caravan or >= 2 orders. (User currently has 1 order)
        assert is_step_eligible(cur, u_id, 4) is False
        # Place second order
        place_and_match_order(cur, u_id, "SELL", "stone", 5.0, 5.00)
        db_conn.commit()
        assert is_step_eligible(cur, u_id, 4) is True

        # Claim Step 4 (Reward: 100 Taler)
        cur.execute("SELECT balance FROM users WHERE id = %s", (u_id,))
        bal_before_s4 = float(cur.fetchone()["balance"])
        res4 = claim_tutorial_reward(cur, u_id)
        db_conn.commit()
        assert res4["claimed_step"] == 4
        assert res4["next_step"] == 5

        cur.execute("SELECT balance FROM users WHERE id = %s", (u_id,))
        assert float(cur.fetchone()["balance"]) == round(bal_before_s4 + 100.0, 2)

        # 5. Step 5: Requires guild membership
        assert is_step_eligible(cur, u_id, 5) is False
        # Give enough Taler to found a guild
        cur.execute("UPDATE users SET balance = balance + 500.0 WHERE id = %s", (u_id,))
        db_conn.commit()
        create_guild(cur, u_id, f"TutorialGuild_{ts}", f"TG{str(ts)[-3:]}")
        db_conn.commit()
        assert is_step_eligible(cur, u_id, 5) is True

        # Claim Step 5 (Zunftbeitritt: 150 Taler)
        cur.execute("SELECT balance FROM users WHERE id = %s", (u_id,))
        bal_before_s5 = float(cur.fetchone()["balance"])
        res5 = claim_tutorial_reward(cur, u_id)
        db_conn.commit()
        assert res5["claimed_step"] == 5
        assert res5["next_step"] == 6

        cur.execute("SELECT balance FROM users WHERE id = %s", (u_id,))
        assert float(cur.fetchone()["balance"]) == round(bal_before_s5 + 150.0, 2)

        # Step 6 (Die erste Expedition)
        assert is_step_eligible(cur, u_id, 6) is False
        from app.engine.caravans import dispatch_caravan
        cur.execute("SELECT id FROM regions WHERE tag = 'VISB'")
        visb_reg = cur.fetchone()
        cur.execute("UPDATE inventories SET amount = 50.0 WHERE user_id = %s AND resource_type = 'wood'", (u_id,))
        dispatch_caravan(cur, u_id, visb_reg["id"], {"wood": 10.0})
        db_conn.commit()
        assert is_step_eligible(cur, u_id, 6) is True

        # Claim Step 6 (100 Taler & 30 Cloth)
        res6 = claim_tutorial_reward(cur, u_id)
        db_conn.commit()
        assert res6["claimed_step"] == 6
        assert res6["next_step"] == 7
        assert res6["is_finished"] is False

        # Step 7: Macht der Hanse (user is already leader of TutorialGuild_{ts})
        assert is_step_eligible(cur, u_id, 7) is True

        # Claim Step 7 (Final Step: 200 Taler & 40 Eisen)
        cur.execute("SELECT balance FROM users WHERE id = %s", (u_id,))
        bal_before_s7 = float(cur.fetchone()["balance"])
        res7 = claim_tutorial_reward(cur, u_id)
        db_conn.commit()
        assert res7["claimed_step"] == 7
        assert res7["is_finished"] is True

        cur.execute("SELECT balance FROM users WHERE id = %s", (u_id,))
        assert float(cur.fetchone()["balance"]) == round(bal_before_s7 + 200.0, 2)
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'iron'", (u_id,))
        assert float(cur.fetchone()["amount"]) >= 40.0

        # Further claims rejected
        with pytest.raises(ValueError, match="vollständig abgeschlossen"):
            claim_tutorial_reward(cur, u_id)
        db_conn.rollback()


def test_tutorial_duplicate_claim_prevention(db_conn):
    """
    Verifies that the same tutorial step cannot be claimed twice.
    """
    ts = int(time.time() * 1000)
    username = f"dup_user_{ts}"

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 100.0) RETURNING id",
            (username,),
        )
        u_id = cur.fetchone()["id"]
        ensure_user_entities(cur, u_id)
        db_conn.commit()

        # Step 1 claim
        claim_tutorial_reward(cur, u_id)
        db_conn.commit()

        # Immediate second claim on step 1 fails because current_step is now 2 and requirements not met
        with pytest.raises(ValueError):
            claim_tutorial_reward(cur, u_id)
        db_conn.rollback()


def test_handbuch_dynamic_rendering(db_conn):
    """
    Verifies that the living merchant handbook (/handbuch) renders correctly
    and directly reflects backend configuration values without hardcoded drift.
    """
    client = TestClient(app)
    ts = int(time.time() * 1000)
    username = f"handbuch_reader_{ts}"

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 200.0) RETURNING id",
            (username,),
        )
        u_id = cur.fetchone()["id"]
        db_conn.commit()

    token = create_session_token(u_id, username)
    client.cookies.set(settings.COOKIE_NAME, token)

    res = client.get("/handbuch")
    assert res.status_code == 200
    text = res.text

    # Verify dynamic values appear in HTML
    assert "Das Kontor-Handbuch" in text
    assert "2.0% auf ausgeführte Trades" in text or "2%" in text
    assert "1000" in text  # Base storage cap
    assert "1.5^(Stufe - 1)" in text or "1.5^" in text
    assert "Freihafen" in text
    assert "Speicherstadt" in text
    assert "Kaufmannslehre" in text


def test_visual_svg_and_template_integrity(db_conn):
    """
    Verifies that all views render cleanly with the new SVG icons,
    dual-column financial terminal, and tutorial widget.
    """
    client = TestClient(app)
    ts = int(time.time() * 1000)
    username = f"terminal_user_{ts}"

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 500.0) RETURNING id",
            (username,),
        )
        u_id = cur.fetchone()["id"]
        ensure_user_entities(cur, u_id)
        db_conn.commit()

    token = create_session_token(u_id, username)
    client.cookies.set(settings.COOKIE_NAME, token)

    # 1. Dashboard loads with tutorial widget container and SVG tabs
    r_dash = client.get("/")
    assert r_dash.status_code == 200
    assert "tutorial-widget-container" in r_dash.text
    assert "tab-handbook" in r_dash.text
    assert "<svg" in r_dash.text

    # 2. Tutorial widget HTMX endpoint
    r_tut = client.get("/tutorial/widget")
    assert r_tut.status_code == 200
    assert "Kaufmannslehre" in r_tut.text
    assert "Schritt 1 von" in r_tut.text

    # 3. Market dual-column terminal
    r_market = client.get("/market/book?resource=wood")
    assert r_market.status_code == 200
    assert "Aggregiertes Orderbuch" in r_market.text
    assert "Kauf (BUY)" in r_market.text
    assert "Verkauf (SELL)" in r_market.text
    assert "<svg" in r_market.text

    # 4. Resources overview with visual capacity bars
    r_res = client.get("/resources/overview")
    assert r_res.status_code == 200
    assert "Auslastung:" in r_res.text
    assert "Zentrallager Stufe" in r_res.text
    assert "<svg" in r_res.text
