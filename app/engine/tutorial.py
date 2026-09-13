import json
from typing import Dict, List, Any, Optional
from app.engine.notifications import create_notification

TUTORIAL_STEPS = {
    1: {
        "step_id": 1,
        "title": "1. Bestandsaufnahme im Kontor",
        "description": "Prüfe Deine aktuellen Rohstoffbestände und beachte die Spezialisierung Deiner Heimatregion: Bestimmte Rohstoffe florieren hier mit regionalen Boni, während fehlende Güter (Reine Importware) später über die Börse erworben werden müssen.",
        "requirement_text": "Öffne die Rohstoffübersicht im Kontor.",
        "reward_desc": "25.00 Taler Handgeld",
        "reward": {"balance": 25.0, "resources": {}},
    },
    2: {
        "step_id": 2,
        "title": "2. Wirtschaftliche Expansion",
        "description": "Erweitere Deine Produktionskapazitäten: Baue einen beliebigen Betrieb oder Dein Zentrallager auf Stufe 2 aus.",
        "requirement_text": "Mindestens 1 Gebäude auf Stufe 2 oder höher ausbauen.",
        "reward_desc": "50.00 Taler Baukostenzuschuss",
        "reward": {"balance": 50.0, "resources": {}},
    },
    3: {
        "step_id": 3,
        "title": "3. Zugang zur Hansebörse",
        "description": "Handel ist das Lebenselixier des Imperiums: Keine Region der Hanse ist autark! Verkaufe Deine regionalen Überschüsse an der Börse oder kaufe Mangelwaren ein, die in Deiner Heimat nicht gewonnen werden können.",
        "requirement_text": "Eine Kauf- oder Verkaufsorder im Orderbuch aufgeben.",
        "reward_desc": "25 Holz & 25 Stein",
        "reward": {"balance": 0.0, "resources": {"wood": 25.0, "stone": 25.0}},
    },
    4: {
        "step_id": 4,
        "title": "4. Kaiserlicher Fernhandel",
        "description": "Nutze Fernhandelsrouten: Beliefere eine kaiserliche Karawane oder platziere mindestens 2 Marktorders.",
        "requirement_text": "Einen Karawanenvertrag erfüllen oder 2 Marktorders einstellen.",
        "reward_desc": "100.00 Taler Handelsprämie",
        "reward": {"balance": 100.0, "resources": {}},
    },
    5: {
        "step_id": 5,
        "title": "5. Der Hanseatische Bund",
        "description": "Gemeinsam zum Großhandel: Schließe Dich einer Kaufmannsgilde an oder gründe Deine eigene Allianz.",
        "requirement_text": "Mitglied einer Kaufmannsgilde werden.",
        "reward_desc": "150.00 Taler Hanse-Segen",
        "reward": {"balance": 150.0, "resources": {}},
    },
    6: {
        "step_id": 6,
        "title": "6. Die erste Expedition",
        "description": "Erweitere Deinen Einflussbereich über die Meere: Rüste eine Handelskarawane aus und entsende Waren in eine andere Hansestadt.",
        "requirement_text": "Mindestens eine Karawane zu einer fremden Region entsenden.",
        "reward_desc": "100.00 Taler & 30 Tuch",
        "reward": {"balance": 100.0, "resources": {"cloth": 30.0}},
    },
    7: {
        "step_id": 7,
        "title": "7. Macht der Hanse",
        "description": "Engagiere Dich für das Gemeinwohl Deines Bündnisses: Zahle Taler in die Gildenkasse ein, spende Rohstoffe für ein Monument oder führe eine Gilde an.",
        "requirement_text": "Einen Beitrag zur Gildenkasse leisten, für ein Monument spenden oder Gildenmeister sein.",
        "reward_desc": "200.00 Taler & 40 Eisen",
        "reward": {"balance": 200.0, "resources": {"iron": 40.0}},
    },
}

def ensure_user_tutorial(cur, user_id: int) -> Dict[str, Any]:
    """
    Initializes or retrieves the user's onboarding tutorial state.
    """
    cur.execute(
        """
        INSERT INTO user_tutorials (user_id, current_step, completed_steps, is_finished)
        VALUES (%s, 1, '[]'::jsonb, FALSE)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id,),
    )
    cur.execute(
        """
        SELECT user_id, current_step, completed_steps, is_finished, created_at
        FROM user_tutorials
        WHERE user_id = %s
        """,
        (user_id,),
    )
    row = cur.fetchone()
    completed = row["completed_steps"]
    if isinstance(completed, str):
        completed = json.loads(completed)
    return {
        "user_id": row["user_id"],
        "current_step": int(row["current_step"]),
        "completed_steps": completed or [],
        "is_finished": bool(row["is_finished"]),
        "created_at": row["created_at"],
    }

def is_step_eligible(cur, user_id: int, step_id: int) -> bool:
    """
    Dynamically verifies whether the requirements for the given tutorial step
    have been satisfied by the player in PostgreSQL.
    """
    if step_id == 1:
        # Step 1: Visiting overview / initial inspection is always immediately claimable
        return True

    elif step_id == 2:
        # Step 2: Any building with level >= 2
        cur.execute(
            "SELECT 1 FROM buildings WHERE user_id = %s AND level >= 2 LIMIT 1",
            (user_id,),
        )
        return cur.fetchone() is not None

    elif step_id == 3:
        # Step 3: Placed at least 1 order or executed a trade
        cur.execute(
            "SELECT 1 FROM market_orders WHERE user_id = %s LIMIT 1",
            (user_id,),
        )
        if cur.fetchone() is not None:
            return True
        cur.execute(
            "SELECT 1 FROM trades WHERE buyer_id = %s OR seller_id = %s LIMIT 1",
            (user_id, user_id),
        )
        return cur.fetchone() is not None

    elif step_id == 4:
        # Step 4: Fulfilled at least 1 caravan contract OR placed >= 2 orders
        cur.execute(
            "SELECT 1 FROM export_contracts WHERE user_id = %s AND status = 'FULFILLED' LIMIT 1",
            (user_id,),
        )
        if cur.fetchone() is not None:
            return True
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM market_orders WHERE user_id = %s",
            (user_id,),
        )
        cnt_row = cur.fetchone()
        return (cnt_row["cnt"] if cnt_row else 0) >= 2

    elif step_id == 5:
        # Step 5: Member of a guild
        cur.execute(
            "SELECT 1 FROM guild_members WHERE user_id = %s LIMIT 1",
            (user_id,),
        )
        return cur.fetchone() is not None

    elif step_id == 6:
        # Step 6: Dispatched at least 1 caravan
        cur.execute(
            "SELECT 1 FROM caravans WHERE user_id = %s LIMIT 1",
            (user_id,),
        )
        return cur.fetchone() is not None

    elif step_id == 7:
        # Step 7: Contributed to guild bank, monument, auction bid, or is guild leader
        cur.execute(
            "SELECT 1 FROM guild_contributions WHERE user_id = %s LIMIT 1",
            (user_id,),
        )
        if cur.fetchone() is not None:
            return True
        cur.execute(
            "SELECT 1 FROM guilds WHERE leader_id = %s LIMIT 1",
            (user_id,),
        )
        return cur.fetchone() is not None

    return False

def claim_tutorial_reward(cur, user_id: int) -> Dict[str, Any]:
    """
    Atomically verifies eligibility, disburses reward, advances milestone,
    and records completion.
    """
    # 1. Lock tutorial row
    cur.execute(
        """
        SELECT user_id, current_step, completed_steps, is_finished
        FROM user_tutorials
        WHERE user_id = %s
        FOR UPDATE
        """,
        (user_id,),
    )
    tut_row = cur.fetchone()
    if not tut_row:
        ensure_user_tutorial(cur, user_id)
        cur.execute(
            "SELECT user_id, current_step, completed_steps, is_finished FROM user_tutorials WHERE user_id = %s FOR UPDATE",
            (user_id,),
        )
        tut_row = cur.fetchone()

    if tut_row["is_finished"]:
        raise ValueError("Die Kaufmannslehre wurde bereits vollständig abgeschlossen!")

    current_step = int(tut_row["current_step"])
    if current_step not in TUTORIAL_STEPS:
        raise ValueError("Keine weitere Tutorial-Aufgabe verfügbar.")

    # 2. Verify eligibility
    if not is_step_eligible(cur, user_id, current_step):
        step_meta = TUTORIAL_STEPS[current_step]
        raise ValueError(
            f"Aufgabe noch nicht erfüllt: {step_meta['requirement_text']}"
        )

    step_info = TUTORIAL_STEPS[current_step]
    reward = step_info["reward"]

    # 3. Disburse Taler balance
    taler_reward = float(reward.get("balance", 0.0))
    if taler_reward > 0:
        cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (taler_reward, user_id))

    # 4. Disburse commodity resources
    resources_reward = reward.get("resources", {})
    for res_name, res_amt in resources_reward.items():
        cur.execute(
            """
            UPDATE inventories
            SET amount = amount + %s
            WHERE user_id = %s AND resource_type = %s
            """,
            (res_amt, user_id, res_name),
        )

    # 5. Advance tutorial state
    completed = tut_row["completed_steps"]
    if isinstance(completed, str):
        completed = json.loads(completed)
    completed_list = list(completed or [])
    if current_step not in completed_list:
        completed_list.append(current_step)

    next_step = current_step + 1
    is_finished = (next_step > len(TUTORIAL_STEPS))

    cur.execute(
        """
        UPDATE user_tutorials
        SET current_step = %s, completed_steps = %s, is_finished = %s
        WHERE user_id = %s
        """,
        (next_step, json.dumps(completed_list), is_finished, user_id),
    )

    # 6. Dispatch notification
    create_notification(
        cur,
        user_id=user_id,
        event_type="TUTORIAL_REWARD_CLAIMED",
        payload={
            "step_id": current_step,
            "title": step_info["title"],
            "reward_desc": step_info["reward_desc"],
            "is_finished": is_finished,
        },
    )

    return {
        "claimed_step": current_step,
        "title": step_info["title"],
        "reward_desc": step_info["reward_desc"],
        "next_step": next_step,
        "is_finished": is_finished,
    }

def get_tutorial_status(cur, user_id: int) -> Dict[str, Any]:
    """
    Returns full state of player's tutorial progression for rendering.
    """
    tut = ensure_user_tutorial(cur, user_id)
    current_step = tut["current_step"]
    is_finished = tut["is_finished"]

    step_info = TUTORIAL_STEPS.get(current_step)
    is_eligible = is_step_eligible(cur, user_id, current_step) if (step_info and not is_finished) else False

    total_steps = len(TUTORIAL_STEPS)
    completed_count = len(tut["completed_steps"])
    progress_pct = 100.0 if is_finished else round((completed_count / total_steps) * 100.0, 1)

    return {
        "user_id": user_id,
        "current_step": current_step,
        "total_steps": total_steps,
        "completed_count": completed_count,
        "progress_pct": progress_pct,
        "is_finished": is_finished,
        "step_info": step_info,
        "is_eligible": is_eligible,
    }
