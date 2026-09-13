import pytest
import time
import json
from starlette.testclient import TestClient

from app.main import app
from app.config import settings, GUILD_CREATION_FEE, GUILD_PROJECT_CONFIG
from app.auth import create_session_token
from app.engine.production import (
    ensure_user_entities,
    calculate_offline_production,
    get_effective_storage_cap,
)
from app.engine.matching import place_and_match_order
from app.engine.guilds import (
    create_guild,
    join_guild,
    leave_guild,
    contribute_to_project,
    has_guild_perk,
    get_user_guild_details,
    list_all_guilds,
)

def test_guild_founding_membership_and_succession(db_conn):
    """
    Verifies Phase 6 Guild Founding & Lifecycle:
    - 500 Taler fee deduction & LEADER assignment.
    - Tag / Name uniqueness & constraints.
    - Joining & single-membership enforcement.
    - Member departure and leadership succession.
    """
    ts = int(time.time() * 1000)
    user1_name = f"founder_{ts}"
    user2_name = f"member_{ts}"

    with db_conn.cursor() as cur:
        # Create user 1 (with 600 Taler) and user 2 (with 100 Taler)
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 600.0) RETURNING id",
            (user1_name,),
        )
        u1_id = cur.fetchone()["id"]
        ensure_user_entities(cur, u1_id)

        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 100.0) RETURNING id",
            (user2_name,),
        )
        u2_id = cur.fetchone()["id"]
        ensure_user_entities(cur, u2_id)
        db_conn.commit()

        # 1. User with insufficient funds (< 500) fails to create guild
        with pytest.raises(ValueError, match="Unzureichende Taler"):
            create_guild(cur, u2_id, f"PoorGuild_{ts}", "POOR")
        db_conn.rollback()

        # 2. User 1 successfully founds guild
        guild_name = f"Hansebund_{ts}"
        guild_tag = f"H{str(ts)[-4:]}"
        g_info = create_guild(cur, u1_id, guild_name, guild_tag, "Ehrenwerte Kaufmannsgilde")
        db_conn.commit()

        assert g_info["name"] == guild_name
        assert g_info["tag"] == guild_tag.upper()
        guild_id = g_info["guild_id"]

        # Check balance deducted 500 Taler (600 -> 100)
        cur.execute("SELECT balance FROM users WHERE id = %s", (u1_id,))
        assert float(cur.fetchone()["balance"]) == 100.0

        # Check user 1 role is LEADER
        details = get_user_guild_details(cur, u1_id)
        assert details is not None
        assert details["user_role"] == "LEADER"
        assert details["member_count"] == 1
        assert len(details["monuments"]) == len(GUILD_PROJECT_CONFIG)

        # 3. Duplicate guild name or tag rejection
        with pytest.raises(ValueError, match="existiert bereits"):
            create_guild(cur, u2_id, guild_name, "DIFF")
        db_conn.rollback()

        with pytest.raises(ValueError, match="existiert bereits"):
            create_guild(cur, u2_id, "DifferentName", guild_tag)
        db_conn.rollback()

        # 4. User 1 cannot join or create another guild while affiliated
        with pytest.raises(ValueError, match="bereits Mitglied"):
            create_guild(cur, u1_id, f"SecondGuild_{ts}", "SEC")
        db_conn.rollback()

        # 5. User 2 joins the guild
        join_res = join_guild(cur, u2_id, guild_id)
        db_conn.commit()
        assert join_res["role"] == "MEMBER"

        details_after_join = get_user_guild_details(cur, u1_id)
        assert details_after_join["member_count"] == 2

        # User 2 cannot join again
        with pytest.raises(ValueError, match="bereits Mitglied"):
            join_guild(cur, u2_id, guild_id)
        db_conn.rollback()

        # 6. Leadership succession: Leader leaves, User 2 becomes LEADER
        leave_res1 = leave_guild(cur, u1_id)
        db_conn.commit()
        assert leave_res1["disbanded"] is False
        assert leave_res1["new_leader_id"] == u2_id

        details_u2 = get_user_guild_details(cur, u2_id)
        assert details_u2 is not None
        assert details_u2["user_role"] == "LEADER"
        assert details_u2["member_count"] == 1

        # 7. Sole member leaves -> guild disbanded
        leave_res2 = leave_guild(cur, u2_id)
        db_conn.commit()
        assert leave_res2["disbanded"] is True

        assert get_user_guild_details(cur, u2_id) is None


def test_atomic_project_contribution(db_conn):
    """
    Verifies Cooperative Monuments & Atomic Contributions:
    - Target requirement verification.
    - Resource / Taler locking and deduction.
    - Over-contribution capping.
    - Completion trigger and perk activation.
    """
    ts = int(time.time() * 1000)
    username = f"builder_{ts}"

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 1000.0) RETURNING id",
            (username,),
        )
        u_id = cur.fetchone()["id"]
        ensure_user_entities(cur, u_id)
        # Give generous materials
        cur.execute(
            "UPDATE inventories SET amount = 500.0 WHERE user_id = %s",
            (u_id,),
        )
        db_conn.commit()

        # Create guild
        g_info = create_guild(cur, u_id, f"Builders_{ts}", f"B{str(ts)[-4:]}")
        db_conn.commit()
        guild_id = g_info["guild_id"]

        # Fetch projects
        cur.execute(
            "SELECT id, project_type, target_costs FROM guild_projects WHERE guild_id = %s AND project_type = 'FREIHAFEN'",
            (guild_id,),
        )
        p_row = cur.fetchone()
        project_id = p_row["id"]
        targets = p_row["target_costs"]  # e.g., wood: 150, stone: 100, cloth: 50, balance: 300

        # Non-member cannot contribute
        with pytest.raises(ValueError, match="Mitglied einer Gilde"):
            contribute_to_project(cur, 999999, project_id, "wood", 10.0)
        db_conn.rollback()

        # Invalid resource type
        with pytest.raises(ValueError, match="nicht benötigt"):
            contribute_to_project(cur, u_id, project_id, "gold_bars", 10.0)
        db_conn.rollback()

        # Partial contribution of wood (50 of 150)
        c1 = contribute_to_project(cur, u_id, project_id, "wood", 50.0)
        db_conn.commit()
        assert c1["contributed_amount"] == 50.0
        assert c1["is_completed"] is False
        assert c1["invested_resources"]["wood"] == 50.0

        # Check inventory was deducted
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'wood'", (u_id,))
        assert float(cur.fetchone()["amount"]) == 450.0

        # Over-contribution capping: request 200 wood when only 100 needed
        c2 = contribute_to_project(cur, u_id, project_id, "wood", 200.0)
        db_conn.commit()
        assert c2["contributed_amount"] == 100.0
        assert c2["invested_resources"]["wood"] == 150.0

        # Trying to contribute more wood when target is full raises ValueError
        with pytest.raises(ValueError, match="bereits vollständig"):
            contribute_to_project(cur, u_id, project_id, "wood", 10.0)
        db_conn.rollback()

        # Complete all remaining requirements
        for res_k, target_v in targets.items():
            current_inv = float(c2["invested_resources"].get(res_k, 0.0))
            diff = float(target_v) - current_inv
            if diff > 0:
                res = contribute_to_project(cur, u_id, project_id, res_k, diff)
                db_conn.commit()

        # Project is now completed!
        cur.execute("SELECT is_completed, completed_at FROM guild_projects WHERE id = %s", (project_id,))
        p_final = cur.fetchone()
        assert p_final["is_completed"] is True
        assert p_final["completed_at"] is not None

        # Perk FREIHAFEN is active for user!
        assert has_guild_perk(cur, u_id, "FREIHAFEN") is True


def test_freihafen_market_fee_perk(db_conn):
    """
    Verifies that the FREIHAFEN guild perk lowers the seller's market fee
    from the default 2.0% down to exactly 1.5%.
    """
    ts = int(time.time() * 1000)
    seller_guild_name = f"seller_g_{ts}"
    buyer_name = f"buyer_g_{ts}"
    seller_standard_name = f"seller_std_{ts}"

    with db_conn.cursor() as cur:
        # 1. Seller with guild & FREIHAFEN completed
        cur.execute("INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 1000.0) RETURNING id", (seller_guild_name,))
        seller_g_id = cur.fetchone()["id"]
        ensure_user_entities(cur, seller_g_id)
        cur.execute("UPDATE inventories SET amount = 100.0 WHERE user_id = %s AND resource_type = 'iron'", (seller_g_id,))

        g_info = create_guild(cur, seller_g_id, f"GuildFrei_{ts}", f"F{str(ts)[-4:]}")
        # Directly mark FREIHAFEN as completed
        cur.execute(
            "UPDATE guild_projects SET is_completed = TRUE, completed_at = NOW() WHERE guild_id = %s AND project_type = 'FREIHAFEN'",
            (g_info["guild_id"],),
        )

        # 2. Seller without guild
        cur.execute("INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 500.0) RETURNING id", (seller_standard_name,))
        seller_std_id = cur.fetchone()["id"]
        ensure_user_entities(cur, seller_std_id)
        cur.execute("UPDATE inventories SET amount = 100.0 WHERE user_id = %s AND resource_type = 'iron'", (seller_std_id,))

        # 3. Buyer
        cur.execute("INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 2000.0) RETURNING id", (buyer_name,))
        buyer_id = cur.fetchone()["id"]
        ensure_user_entities(cur, buyer_id)
        # Clean up any lingering open iron orders to ensure clean test matches
        cur.execute("DELETE FROM market_orders WHERE resource_type = 'iron'")
        db_conn.commit()

        # Trade 1: Standard seller sells 10 iron at 10.00 Taler = 100.00 Taler trade value.
        # Fee should be standard 2.0% = 2.00 Taler.
        place_and_match_order(cur, seller_std_id, "SELL", "iron", 10.0, 10.00)
        res_trade1 = place_and_match_order(cur, buyer_id, "BUY", "iron", 10.0, 10.00)
        db_conn.commit()

        assert len(res_trade1["trades"]) == 1
        t1 = res_trade1["trades"][0]
        assert t1["fee"] == 2.00  # 2% of 100.00

        # Trade 2: Guild seller with FREIHAFEN sells 10 iron at 10.00 Taler = 100.00 Taler trade value.
        # Fee should be reduced 1.5% = 1.50 Taler!
        place_and_match_order(cur, seller_g_id, "SELL", "iron", 10.0, 10.00)
        res_trade2 = place_and_match_order(cur, buyer_id, "BUY", "iron", 10.0, 10.00)
        db_conn.commit()

        assert len(res_trade2["trades"]) == 1
        t2 = res_trade2["trades"][0]
        assert t2["fee"] == 1.50  # 1.5% of 100.00! Exactly 1.5% perk verified!


def test_speicherstadt_storage_bonus_perk(db_conn):
    """
    Verifies that the SPEICHERSTADT guild perk grants a +10% flat bonus
    to warehouse storage capacity (Level 1: 1000 -> 1100).
    """
    ts = int(time.time() * 1000)
    member_name = f"speicher_{ts}"
    nonmember_name = f"nonspeicher_{ts}"

    with db_conn.cursor() as cur:
        # Non-member
        cur.execute("INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 100.0) RETURNING id", (nonmember_name,))
        non_id = cur.fetchone()["id"]
        ensure_user_entities(cur, non_id)

        # Guild member with SPEICHERSTADT completed
        cur.execute("INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 800.0) RETURNING id", (member_name,))
        mem_id = cur.fetchone()["id"]
        ensure_user_entities(cur, mem_id)

        g_info = create_guild(cur, mem_id, f"SpeicherG_{ts}", f"S{str(ts)[-4:]}")
        cur.execute(
            "UPDATE guild_projects SET is_completed = TRUE, completed_at = NOW() WHERE guild_id = %s AND project_type = 'SPEICHERSTADT'",
            (g_info["guild_id"],),
        )
        db_conn.commit()

        # Verify cap calculations
        std_cap = get_effective_storage_cap(cur, non_id, warehouse_level=1)
        perk_cap = get_effective_storage_cap(cur, mem_id, warehouse_level=1)

        assert std_cap == 1000.0
        assert perk_cap == 1100.0  # +10% bonus

        # Check offline production cap enforcement
        # Give both players 1200 wood in inventory
        cur.execute("UPDATE inventories SET amount = 1200.0 WHERE resource_type = 'wood' AND user_id IN (%s, %s)", (non_id, mem_id))
        db_conn.commit()

        prod_non = calculate_offline_production(cur, non_id, record_catchup=False)
        prod_mem = calculate_offline_production(cur, mem_id, record_catchup=False)
        db_conn.commit()

        # Non-member is capped at 1000.0
        assert prod_non["storage_cap"] == 1000.0
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'wood'", (non_id,))
        assert float(cur.fetchone()["amount"]) == 1000.0

        # Guild member with SPEICHERSTADT is capped at 1100.0
        assert prod_mem["storage_cap"] == 1100.0
        cur.execute("SELECT amount FROM inventories WHERE user_id = %s AND resource_type = 'wood'", (mem_id,))
        assert float(cur.fetchone()["amount"]) == 1100.0


def test_guild_http_htmx_flow(db_conn):
    """
    Verifies full HTTP / HTMX flow for guilds:
    - GET /guilds shows founding form & recruitment directory.
    - POST /guilds/create founds guild and returns Guild Hall.
    - POST /guilds/projects/{id}/contribute updates monument progress.
    - POST /guilds/leave exits guild and returns recruitment view.
    """
    client = TestClient(app)
    ts = int(time.time() * 1000)
    username = f"http_guild_{ts}"

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, balance) VALUES (%s, 'hash', 1000.0) RETURNING id",
            (username,),
        )
        u_id = cur.fetchone()["id"]
        ensure_user_entities(cur, u_id)
        cur.execute("UPDATE inventories SET amount = 200.0 WHERE user_id = %s", (u_id,))
        db_conn.commit()

    token = create_session_token(u_id, username)
    client.cookies.set(settings.COOKIE_NAME, token)

    # Acquire CSRF cookie
    init_res = client.get("/health")
    csrf_token = init_res.cookies.get(settings.CSRF_COOKIE_NAME, "mock_csrf_token_value_32_bytes_long")
    client.cookies.set(settings.CSRF_COOKIE_NAME, csrf_token)
    headers = {"X-CSRF-Token": csrf_token}

    # 1. GET /guilds as unaffiliated
    r1 = client.get("/guilds")
    assert r1.status_code == 200
    assert "Gilde gründen" in r1.text
    assert "Bestehende Kaufmannsgilden" in r1.text

    # 2. POST /guilds/create
    guild_tag = f"H{str(ts)[-3:]}"
    r2 = client.post(
        "/guilds/create",
        data={"name": f"Handelshaus {ts}", "tag": guild_tag, "description": "Erste Liga"},
        headers=headers,
    )
    assert r2.status_code == 200
    assert f"Handelshaus {ts}" in r2.text
    assert "Gemeinschaftliche Monumente" in r2.text
    assert "Freihafen" in r2.text
    assert "Speicherstadt" in r2.text

    # 3. Contribute to a monument via HTTP
    with db_conn.cursor() as cur:
        details = get_user_guild_details(cur, u_id)
        project_id = details["monuments"][0]["id"]

    r3 = client.post(
        f"/guilds/projects/{project_id}/contribute",
        data={"resource_type": "wood", "amount": "25"},
        headers=headers,
    )
    assert r3.status_code == 200
    assert "Erfolgreich" in r3.text or "Spende" in r3.text

    # 4. Leave guild
    r4 = client.post("/guilds/leave", headers=headers)
    assert r4.status_code == 200
    assert "verlassen" in r4.text or "aufgelöst" in r4.text
