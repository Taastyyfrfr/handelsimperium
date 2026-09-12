import hashlib
from datetime import datetime, timezone, timedelta, time as dtime
from typing import Dict, List, Any, Optional
from decimal import Decimal

from app.config import settings, SUPPORTED_RESOURCES, CONTRACT_TEMPLATES
from app.engine.notifications import create_notification

def get_today_utc():
    return datetime.now(timezone.utc).date()

def ensure_daily_contracts(cur, user_id: int) -> List[Dict[str, Any]]:
    """
    Retrieves or deterministically generates exactly 3 daily export contracts
    ("Handelskarawanen") for the given merchant, expiring at midnight (23:59:59 UTC).
    """
    today = get_today_utc()
    now = datetime.now(timezone.utc)

    # 1. Mark expired contracts from previous days
    cur.execute(
        """
        UPDATE export_contracts
        SET status = 'EXPIRED'
        WHERE user_id = %s AND expires_at < %s AND status = 'AVAILABLE'
        """,
        (user_id, now),
    )

    # 2. Fetch existing contracts for today
    cur.execute(
        """
        SELECT id, user_id, contract_date, resource_type, title,
               target_amount, reward_taler, status, expires_at, fulfilled_at
        FROM export_contracts
        WHERE user_id = %s AND contract_date = %s
        ORDER BY id ASC
        """,
        (user_id, today),
    )
    existing = cur.fetchall()
    if existing and len(existing) >= 3:
        return [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "contract_date": r["contract_date"],
                "resource_type": r["resource_type"],
                "title": r["title"],
                "target_amount": float(r["target_amount"]),
                "reward_taler": float(r["reward_taler"]),
                "status": r["status"],
                "expires_at": r["expires_at"],
                "fulfilled_at": r["fulfilled_at"],
            }
            for r in existing
        ]

    # 3. Generate 3 distinct contracts deterministically for today
    seed_str = f"{user_id}_{today.isoformat()}"
    h = int(hashlib.sha256(seed_str.encode()).hexdigest(), 16)
    
    all_res = list(SUPPORTED_RESOURCES)
    selected_res = []
    # Use modulo arithmetic on hash to pick 3 distinct resources
    for shift in (0, 5, 10, 15, 20, 25, 30):
        res = all_res[(h >> shift) % len(all_res)]
        if res not in selected_res:
            selected_res.append(res)
        if len(selected_res) == 3:
            break

    if len(selected_res) < 3:
        for res in all_res:
            if res not in selected_res:
                selected_res.append(res)
            if len(selected_res) == 3:
                break

    # Expiration is 23:59:59 UTC of today
    midnight_utc = datetime.combine(today, dtime(23, 59, 59), tzinfo=timezone.utc)

    for res in selected_res:
        tpl = CONTRACT_TEMPLATES.get(res, {"amount": 20.0, "reward": 100.0, "title": f"Lieferung {res}"})
        cur.execute(
            """
            INSERT INTO export_contracts (
                user_id, contract_date, resource_type, title,
                target_amount, reward_taler, status, expires_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'AVAILABLE', %s)
            ON CONFLICT (user_id, contract_date, resource_type) DO NOTHING
            """,
            (user_id, today, res, tpl["title"], tpl["amount"], tpl["reward"], midnight_utc),
        )

    # Re-fetch generated contracts
    cur.execute(
        """
        SELECT id, user_id, contract_date, resource_type, title,
               target_amount, reward_taler, status, expires_at, fulfilled_at
        FROM export_contracts
        WHERE user_id = %s AND contract_date = %s
        ORDER BY id ASC
        """,
        (user_id, today),
    )
    rows = cur.fetchall()
    return [
        {
            "id": r["id"],
            "user_id": r["user_id"],
            "contract_date": r["contract_date"],
            "resource_type": r["resource_type"],
            "title": r["title"],
            "target_amount": float(r["target_amount"]),
            "reward_taler": float(r["reward_taler"]),
            "status": r["status"],
            "expires_at": r["expires_at"],
            "fulfilled_at": r["fulfilled_at"],
        }
        for r in rows
    ]

def fulfill_export_contract(cur, user_id: int, contract_id: int) -> Dict[str, Any]:
    """
    Atomically fulfills an export contract:
    - Verifies availability and expiration.
    - Locks and verifies required merchant inventory.
    - Permanently deletes commodities from circulation.
    - Credits guaranteed Taler reward to merchant balance.
    - Dispatches CONTRACT_FULFILLED notification.
    """
    # 1. Lock contract row
    cur.execute(
        """
        SELECT id, user_id, resource_type, target_amount, reward_taler, status, expires_at
        FROM export_contracts
        WHERE id = %s AND user_id = %s
        FOR UPDATE
        """,
        (contract_id, user_id),
    )
    contract = cur.fetchone()
    if not contract:
        raise ValueError("Exportvertrag nicht gefunden.")

    if contract["status"] == "FULFILLED":
        raise ValueError("Dieser Exportvertrag wurde bereits erfolgreich erfüllt.")

    now = datetime.now(timezone.utc)
    if contract["status"] != "AVAILABLE" or now > contract["expires_at"]:
        cur.execute("UPDATE export_contracts SET status = 'EXPIRED' WHERE id = %s", (contract_id,))
        raise ValueError("Dieser Exportvertrag ist abgelaufen.")

    res_type = contract["resource_type"]
    req_amount = float(contract["target_amount"])
    reward = float(contract["reward_taler"])

    # 2. Lock and verify merchant inventory
    cur.execute(
        """
        SELECT amount
        FROM inventories
        WHERE user_id = %s AND resource_type = %s
        FOR UPDATE
        """,
        (user_id, res_type),
    )
    inv_row = cur.fetchone()
    avail_amount = float(inv_row["amount"]) if inv_row else 0.0

    if avail_amount < req_amount:
        raise ValueError(
            f"Nicht genügend {res_type} im Lager! Erforderlich: {req_amount:.0f}, Vorhanden: {avail_amount:.2f}"
        )

    # 3. Deduct inventory (permanently burned from circulation)
    cur.execute(
        """
        UPDATE inventories
        SET amount = amount - %s
        WHERE user_id = %s AND resource_type = %s
        """,
        (req_amount, user_id, res_type),
    )

    # 4. Lock and credit merchant balance
    cur.execute(
        """
        UPDATE users
        SET balance = balance + %s
        WHERE id = %s
        RETURNING balance
        """,
        (reward, user_id),
    )
    new_balance = float(cur.fetchone()["balance"])

    # 5. Mark contract as fulfilled
    cur.execute(
        """
        UPDATE export_contracts
        SET status = 'FULFILLED', fulfilled_at = NOW()
        WHERE id = %s
        """,
        (contract_id,),
    )

    # 6. Dispatch notification
    create_notification(
        cur,
        user_id=user_id,
        event_type="CONTRACT_FULFILLED",
        payload={
            "contract_id": contract_id,
            "resource_type": res_type,
            "amount": req_amount,
            "reward": reward,
        },
    )

    return {
        "contract_id": contract_id,
        "resource_type": res_type,
        "amount": req_amount,
        "reward": reward,
        "new_balance": new_balance,
    }
