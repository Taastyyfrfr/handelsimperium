import json
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

from app.config import settings, GUILD_CREATION_FEE, GUILD_PROJECT_CONFIG
from app.engine.notifications import create_notification

def has_guild_perk(cur, user_id: int, perk: str) -> bool:
    """
    Fast, index-backed check verifying if the user belongs to a guild
    that has completed the specified monument/perk (e.g., 'FREIHAFEN', 'SPEICHERSTADT').
    """
    if not user_id:
        return False
    cur.execute(
        """
        SELECT 1
        FROM guild_members gm
        JOIN guild_projects gp ON gp.guild_id = gm.guild_id
        WHERE gm.user_id = %s AND gp.project_type = %s AND gp.is_completed = TRUE
        LIMIT 1
        """,
        (user_id, perk),
    )
    return cur.fetchone() is not None

def get_user_guild_membership(cur, user_id: int) -> Optional[Dict[str, Any]]:
    """
    Fetches the current guild membership for a user, or None if unaffiliated.
    """
    cur.execute(
        """
        SELECT gm.id, gm.guild_id, gm.role, gm.joined_at,
               g.name AS guild_name, g.tag AS guild_tag, g.leader_id
        FROM guild_members gm
        JOIN guilds g ON g.id = gm.guild_id
        WHERE gm.user_id = %s
        """,
        (user_id,),
    )
    return cur.fetchone()

def ensure_guild_projects(cur, guild_id: int):
    """
    Ensures all monuments defined in GUILD_PROJECT_CONFIG exist for the guild.
    """
    for project_type, cfg in GUILD_PROJECT_CONFIG.items():
        cur.execute(
            """
            INSERT INTO guild_projects (
                guild_id, project_type, stage, target_costs, invested_resources, is_completed
            )
            VALUES (%s, %s, 1, %s, '{}'::jsonb, FALSE)
            ON CONFLICT (guild_id, project_type) DO NOTHING
            """,
            (guild_id, project_type, json.dumps(cfg["target_costs"])),
        )

def create_guild(cur, user_id: int, name: str, tag: str, description: str = "") -> Dict[str, Any]:
    """
    Found a new merchant guild:
    - Verifies user is not already a member of a guild.
    - Validates name and tag.
    - Verifies user has at least 500 Taler and deducts the founding fee.
    - Creates guild, bank, projects, and assigns user as LEADER.
    """
    # 1. Check if already in guild
    existing_membership = get_user_guild_membership(cur, user_id)
    if existing_membership:
        raise ValueError("Ihr seid bereits Mitglied einer Gilde. Verlasst diese zuerst.")

    clean_name = name.strip()
    clean_tag = tag.strip().upper()
    clean_desc = (description or "").strip()

    if len(clean_name) < 3 or len(clean_name) > 64:
        raise ValueError("Der Gildenname muss zwischen 3 und 64 Zeichen lang sein.")
    if len(clean_tag) < 2 or len(clean_tag) > 6:
        raise ValueError("Das Gilden-Kürzel muss zwischen 2 und 6 Zeichen lang sein.")
    if not clean_tag.isalnum():
        raise ValueError("Das Gilden-Kürzel darf nur aus Buchstaben und Zahlen bestehen.")

    # 2. Check uniqueness
    cur.execute("SELECT id FROM guilds WHERE LOWER(name) = LOWER(%s)", (clean_name,))
    if cur.fetchone():
        raise ValueError(f"Eine Gilde mit dem Namen '{clean_name}' existiert bereits.")
    cur.execute("SELECT id FROM guilds WHERE UPPER(tag) = %s", (clean_tag,))
    if cur.fetchone():
        raise ValueError(f"Eine Gilde mit dem Kürzel '{clean_tag}' existiert bereits.")

    # 3. Check and deduct founding fee
    cur.execute("SELECT balance FROM users WHERE id = %s FOR UPDATE", (user_id,))
    user_row = cur.fetchone()
    if not user_row or float(user_row["balance"]) < GUILD_CREATION_FEE:
        avail_bal = float(user_row["balance"]) if user_row else 0.0
        raise ValueError(
            f"Unzureichende Taler zur Gründung einer Gilde! Erforderlich: {GUILD_CREATION_FEE:.2f} Taler, Vorhanden: {avail_bal:.2f} Taler."
        )

    cur.execute("UPDATE users SET balance = balance - %s WHERE id = %s", (GUILD_CREATION_FEE, user_id))

    # 4. Insert guild
    cur.execute(
        """
        INSERT INTO guilds (name, tag, description, leader_id)
        VALUES (%s, %s, %s, %s)
        RETURNING id, name, tag, description, leader_id, created_at
        """,
        (clean_name, clean_tag, clean_desc, user_id),
    )
    guild = cur.fetchone()
    guild_id = guild["id"]

    # 5. Insert member as LEADER
    cur.execute(
        """
        INSERT INTO guild_members (guild_id, user_id, role)
        VALUES (%s, %s, 'LEADER')
        """,
        (guild_id, user_id),
    )

    # 6. Initialize guild bank
    cur.execute(
        """
        INSERT INTO guild_bank (guild_id, balance)
        VALUES (%s, 0.0)
        ON CONFLICT (guild_id) DO NOTHING
        """,
        (guild_id,),
    )

    # 7. Initialize projects
    ensure_guild_projects(cur, guild_id)

    # 8. Dispatch notification
    create_notification(
        cur,
        user_id=user_id,
        event_type="GUILD_CREATED",
        payload={
            "guild_id": guild_id,
            "name": clean_name,
            "tag": clean_tag,
            "cost": GUILD_CREATION_FEE,
        },
    )

    return {
        "guild_id": guild_id,
        "name": clean_name,
        "tag": clean_tag,
        "description": clean_desc,
        "leader_id": user_id,
    }

def join_guild(cur, user_id: int, guild_id: int) -> Dict[str, Any]:
    """
    Join an existing guild. Merchants can only belong to one guild at a time.
    """
    existing_membership = get_user_guild_membership(cur, user_id)
    if existing_membership:
        raise ValueError("Ihr seid bereits Mitglied einer Gilde. Verlasst diese zuerst.")

    cur.execute("SELECT id, name, tag FROM guilds WHERE id = %s", (guild_id,))
    guild = cur.fetchone()
    if not guild:
        raise ValueError("Die angegebene Gilde existiert nicht.")

    cur.execute(
        """
        INSERT INTO guild_members (guild_id, user_id, role)
        VALUES (%s, %s, 'MEMBER')
        """,
        (guild_id, user_id),
    )

    create_notification(
        cur,
        user_id=user_id,
        event_type="GUILD_JOINED",
        payload={"guild_id": guild_id, "guild_name": guild["name"], "tag": guild["tag"]},
    )

    return {
        "guild_id": guild_id,
        "guild_name": guild["name"],
        "tag": guild["tag"],
        "role": "MEMBER",
    }

def leave_guild(cur, user_id: int) -> Dict[str, Any]:
    """
    Leave current guild:
    - If member: removed from guild_members.
    - If leader: passes leadership to oldest member. If sole member, guild is disbanded.
    """
    cur.execute(
        """
        SELECT gm.id, gm.guild_id, gm.role, g.name AS guild_name, g.tag AS guild_tag
        FROM guild_members gm
        JOIN guilds g ON g.id = gm.guild_id
        WHERE gm.user_id = %s
        """,
        (user_id,),
    )
    membership = cur.fetchone()
    if not membership:
        raise ValueError("Ihr seid in keiner Gilde Mitglied.")

    guild_id = membership["guild_id"]
    role = membership["role"]
    guild_name = membership["guild_name"]

    disbanded = False
    new_leader_id = None

    if role == "LEADER":
        # Find next oldest member
        cur.execute(
            """
            SELECT user_id
            FROM guild_members
            WHERE guild_id = %s AND user_id != %s
            ORDER BY joined_at ASC
            LIMIT 1
            """,
            (guild_id, user_id),
        )
        next_member = cur.fetchone()
        if next_member:
            new_leader_id = next_member["user_id"]
            cur.execute("UPDATE guilds SET leader_id = %s WHERE id = %s", (new_leader_id, guild_id))
            cur.execute("UPDATE guild_members SET role = 'LEADER' WHERE user_id = %s", (new_leader_id,))
            cur.execute("DELETE FROM guild_members WHERE user_id = %s", (user_id,))
        else:
            # Sole member leaving -> disband guild
            cur.execute("DELETE FROM guilds WHERE id = %s", (guild_id,))
            disbanded = True
    else:
        cur.execute("DELETE FROM guild_members WHERE user_id = %s", (user_id,))

    return {
        "guild_id": guild_id,
        "guild_name": guild_name,
        "disbanded": disbanded,
        "new_leader_id": new_leader_id,
    }

def contribute_to_project(cur, user_id: int, project_id: int, resource_type: str, amount: float) -> Dict[str, Any]:
    """
    Atomically contribute resources or Taler to a cooperative guild monument:
    - Verifies user's guild membership.
    - Locks project row and verifies completion state and requirements.
    - Locks user inventory or balance.
    - Capped at remaining required target.
    - Deducts contribution from user and increments invested_resources.
    - If all requirements are met, marks project as completed and activates buff!
    """
    if amount <= 0:
        raise ValueError("Der Beitragsbetrag muss größer als 0 sein.")

    membership = get_user_guild_membership(cur, user_id)
    if not membership:
        raise ValueError("Ihr müsst Mitglied einer Gilde sein, um an Monumenten mitzuwirken.")

    guild_id = membership["guild_id"]

    # 1. Lock project row
    cur.execute(
        """
        SELECT id, guild_id, project_type, stage, target_costs, invested_resources, is_completed
        FROM guild_projects
        WHERE id = %s
        FOR UPDATE
        """,
        (project_id,),
    )
    project = cur.fetchone()
    if not project or project["guild_id"] != guild_id:
        raise ValueError("Dieses Monument existiert nicht oder gehört nicht zu Eurer Gilde.")

    if project["is_completed"]:
        raise ValueError("Dieses Monument wurde bereits vollendet und sein Gilden-Buff ist aktiv!")

    target_costs: Dict[str, float] = project["target_costs"]
    invested: Dict[str, float] = dict(project["invested_resources"] or {})

    clean_res = resource_type.strip().lower()
    if clean_res not in target_costs:
        raise ValueError(f"Die Ressource '{clean_res}' wird für den Bau dieses Monuments nicht benötigt.")

    target_req = float(target_costs[clean_res])
    current_inv = float(invested.get(clean_res, 0.0))
    needed = max(0.0, target_req - current_inv)

    if needed <= 0:
        raise ValueError(f"Das Kontingent für '{clean_res}' ist bei diesem Monument bereits vollständig erbracht.")

    actual_contributed = min(amount, needed)

    # 2. Check and deduct user balance or inventory
    if clean_res == "balance":
        cur.execute("SELECT balance FROM users WHERE id = %s FOR UPDATE", (user_id,))
        user_row = cur.fetchone()
        user_balance = float(user_row["balance"]) if user_row else 0.0
        if user_balance < actual_contributed:
            raise ValueError(
                f"Unzureichende Taler! Erforderlich: {actual_contributed:.2f}, Verfügbar: {user_balance:.2f}"
            )
        cur.execute("UPDATE users SET balance = balance - %s WHERE id = %s", (actual_contributed, user_id))
    else:
        cur.execute(
            "SELECT amount FROM inventories WHERE user_id = %s AND resource_type = %s FOR UPDATE",
            (user_id, clean_res),
        )
        inv_row = cur.fetchone()
        user_inv = float(inv_row["amount"]) if inv_row else 0.0
        if user_inv < actual_contributed:
            raise ValueError(
                f"Nicht genügend {clean_res} im Lager! Erforderlich: {actual_contributed:.2f}, Im Lager: {user_inv:.2f}"
            )
        cur.execute(
            "UPDATE inventories SET amount = amount - %s WHERE user_id = %s AND resource_type = %s",
            (actual_contributed, user_id, clean_res),
        )

    # 3. Update project progress
    new_invested_val = round(current_inv + actual_contributed, 2)
    invested[clean_res] = new_invested_val

    # Check overall completion
    all_completed = True
    for res_k, req_v in target_costs.items():
        if float(invested.get(res_k, 0.0)) < float(req_v):
            all_completed = False
            break

    if all_completed:
        cur.execute(
            """
            UPDATE guild_projects
            SET invested_resources = %s, is_completed = TRUE, completed_at = NOW()
            WHERE id = %s
            """,
            (json.dumps(invested), project_id),
        )
        create_notification(
            cur,
            user_id=user_id,
            event_type="MONUMENT_COMPLETED",
            payload={
                "guild_id": guild_id,
                "project_id": project_id,
                "project_type": project["project_type"],
            },
        )
    else:
        cur.execute(
            """
            UPDATE guild_projects
            SET invested_resources = %s
            WHERE id = %s
            """,
            (json.dumps(invested), project_id),
        )

    return {
        "project_id": project_id,
        "project_type": project["project_type"],
        "resource_type": clean_res,
        "contributed_amount": actual_contributed,
        "is_completed": all_completed,
        "invested_resources": invested,
    }

def list_all_guilds(cur) -> List[Dict[str, Any]]:
    """
    List all guilds with member count, leader name, and completed perks.
    """
    cur.execute(
        """
        SELECT g.id, g.name, g.tag, g.description, g.leader_id, g.created_at,
               u.username AS leader_name,
               COUNT(DISTINCT gm.user_id) AS member_count,
               COALESCE(
                   json_agg(
                       json_build_object('project_type', gp.project_type, 'is_completed', gp.is_completed)
                   ) FILTER (WHERE gp.id IS NOT NULL AND gp.is_completed = TRUE),
                   '[]'::json
               ) AS completed_monuments
        FROM guilds g
        JOIN users u ON u.id = g.leader_id
        LEFT JOIN guild_members gm ON gm.guild_id = g.id
        LEFT JOIN guild_projects gp ON gp.guild_id = g.id AND gp.is_completed = TRUE
        GROUP BY g.id, u.username
        ORDER BY member_count DESC, g.created_at ASC
        """
    )
    rows = cur.fetchall()
    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "name": r["name"],
            "tag": r["tag"],
            "description": r["description"],
            "leader_id": r["leader_id"],
            "leader_name": r["leader_name"],
            "member_count": int(r["member_count"]),
            "created_at": r["created_at"],
            "completed_monuments": r["completed_monuments"] or [],
        })
    return results

def get_user_guild_details(cur, user_id: int) -> Optional[Dict[str, Any]]:
    """
    Fetch comprehensive guild hall data for a member:
    - Guild profile & treasury balance
    - Full roster with roles and join dates
    - Monument projects with progress bars and targets
    - Active perks
    """
    membership = get_user_guild_membership(cur, user_id)
    if not membership:
        return None

    guild_id = membership["guild_id"]
    user_role = membership["role"]

    # Ensure projects exist
    ensure_guild_projects(cur, guild_id)

    # 1. Guild info
    cur.execute(
        """
        SELECT g.id, g.name, g.tag, g.description, g.leader_id, g.created_at,
               u.username AS leader_name,
               COALESCE(gb.balance, 0.0) AS bank_balance
        FROM guilds g
        JOIN users u ON u.id = g.leader_id
        LEFT JOIN guild_bank gb ON gb.guild_id = g.id
        WHERE g.id = %s
        """,
        (guild_id,),
    )
    guild_row = cur.fetchone()
    if not guild_row:
        return None

    # 2. Members roster
    cur.execute(
        """
        SELECT gm.user_id, u.username, gm.role, gm.joined_at, u.balance
        FROM guild_members gm
        JOIN users u ON u.id = gm.user_id
        WHERE gm.guild_id = %s
        ORDER BY CASE WHEN gm.role = 'LEADER' THEN 1 WHEN gm.role = 'OFFICER' THEN 2 ELSE 3 END, gm.joined_at ASC
        """,
        (guild_id,),
    )
    members = cur.fetchall()

    # 3. Monuments
    cur.execute(
        """
        SELECT id, guild_id, project_type, stage, target_costs, invested_resources, is_completed, completed_at
        FROM guild_projects
        WHERE guild_id = %s
        ORDER BY id ASC
        """,
        (guild_id,),
    )
    projects_rows = cur.fetchall()

    monuments = []
    active_perks = []

    for pr in projects_rows:
        ptype = pr["project_type"]
        cfg = GUILD_PROJECT_CONFIG.get(ptype, {
            "name": ptype,
            "description": "Gildenmonument",
            "perk_type": ptype,
            "target_costs": pr["target_costs"],
        })

        target_costs = pr["target_costs"]
        invested = dict(pr["invested_resources"] or {})
        is_completed = bool(pr["is_completed"])

        if is_completed:
            active_perks.append(cfg.get("perk_type", ptype))

        # Calculate progress per resource
        res_progress = []
        total_pct_sum = 0.0
        res_count = max(1, len(target_costs))

        for r_name, r_target in target_costs.items():
            r_target_f = float(r_target)
            r_inv_f = float(invested.get(r_name, 0.0))
            pct = min(100.0, round((r_inv_f / r_target_f * 100.0) if r_target_f > 0 else 100.0, 1))
            total_pct_sum += pct
            res_progress.append({
                "resource": r_name,
                "target": r_target_f,
                "invested": r_inv_f,
                "remaining": max(0.0, round(r_target_f - r_inv_f, 2)),
                "pct": pct,
            })

        overall_pct = 100.0 if is_completed else min(100.0, round(total_pct_sum / res_count, 1))

        monuments.append({
            "id": pr["id"],
            "project_type": ptype,
            "name": cfg["name"],
            "description": cfg["description"],
            "perk_type": cfg.get("perk_type", ptype),
            "is_completed": is_completed,
            "completed_at": pr["completed_at"],
            "overall_pct": overall_pct,
            "resources": res_progress,
        })

    return {
        "guild": {
            "id": guild_row["id"],
            "name": guild_row["name"],
            "tag": guild_row["tag"],
            "description": guild_row["description"],
            "leader_id": guild_row["leader_id"],
            "leader_name": guild_row["leader_name"],
            "bank_balance": float(guild_row["bank_balance"]),
            "created_at": guild_row["created_at"],
        },
        "user_role": user_role,
        "is_leader": (user_role == "LEADER"),
        "members": [
            {
                "user_id": m["user_id"],
                "username": m["username"],
                "role": m["role"],
                "joined_at": m["joined_at"],
                "balance": float(m["balance"]),
            }
            for m in members
        ],
        "member_count": len(members),
        "monuments": monuments,
        "active_perks": active_perks,
    }
