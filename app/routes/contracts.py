from fastapi import APIRouter, Request, Depends, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timezone
import os

from app.auth import get_current_user
from app.database import get_db_connection
from app.engine.contracts import ensure_daily_contracts, fulfill_export_contract
from app.engine.production import calculate_offline_production

router = APIRouter(prefix="/market/contracts", tags=["contracts"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))

@router.get("", response_class=HTMLResponse)
def list_contracts(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """
    Renders the Handelskarawanen (Export Contracts) partial view.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            contracts = ensure_daily_contracts(cur, user["id"])
            conn.commit()
            
            # Fetch merchant current inventories
            cur.execute(
                "SELECT resource_type, amount FROM inventories WHERE user_id = %s",
                (user["id"],),
            )
            inv_map = {r["resource_type"]: float(r["amount"]) for r in cur.fetchall()}

            # Refresh user balance
            cur.execute("SELECT id, username, balance FROM users WHERE id = %s", (user["id"],))
            fresh_user = cur.fetchone()

    # Calculate remaining time until midnight UTC
    now = datetime.now(timezone.utc)
    if contracts and contracts[0].get("expires_at"):
        exp = contracts[0]["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        rem_seconds = max(0, int((exp - now).total_seconds()))
    else:
        rem_seconds = 0

    rem_hours = rem_seconds // 3600
    rem_minutes = (rem_seconds % 3600) // 60
    rem_sec = rem_seconds % 60
    countdown_str = f"{rem_hours:02d}:{rem_minutes:02d}:{rem_sec:02d}"

    # Annotate contracts with merchant availability
    for c in contracts:
        avail = inv_map.get(c["resource_type"], 0.0)
        c["available_inventory"] = avail
        c["can_fulfill"] = (c["status"] == "AVAILABLE" and avail >= c["target_amount"])
        c["progress_pct"] = min(100.0, round((avail / c["target_amount"]) * 100, 1)) if c["target_amount"] > 0 else 100.0

    return templates.TemplateResponse(
        request=request,
        name="components/contracts.html",
        context={
            "user": fresh_user,
            "contracts": contracts,
            "countdown_str": countdown_str,
            "message": None,
            "error": None,
        },
    )

@router.post("/{contract_id}/fulfill", response_class=HTMLResponse)
def fulfill_contract(
    request: Request,
    contract_id: int,
    user: dict = Depends(get_current_user),
):
    """
    Executes atomic contract fulfillment, inventory deduction, and Taler payout.
    """
    message = None
    error = None

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                res = fulfill_export_contract(cur, user["id"], contract_id)
                conn.commit()
                message = f"Karawane erfolgreich beliefert! {res['amount']:.0f} {res['resource_type']} übergeben & {res['reward']:.2f} Taler erhalten."
            except ValueError as e:
                conn.rollback()
                error = str(e)

            contracts = ensure_daily_contracts(cur, user["id"])
            conn.commit()

            cur.execute(
                "SELECT resource_type, amount FROM inventories WHERE user_id = %s",
                (user["id"],),
            )
            inv_map = {r["resource_type"]: float(r["amount"]) for r in cur.fetchall()}

            cur.execute("SELECT id, username, balance FROM users WHERE id = %s", (user["id"],))
            fresh_user = cur.fetchone()

    now = datetime.now(timezone.utc)
    if contracts and contracts[0].get("expires_at"):
        exp = contracts[0]["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        rem_seconds = max(0, int((exp - now).total_seconds()))
    else:
        rem_seconds = 0

    rem_hours = rem_seconds // 3600
    rem_minutes = (rem_seconds % 3600) // 60
    rem_sec = rem_seconds % 60
    countdown_str = f"{rem_hours:02d}:{rem_minutes:02d}:{rem_sec:02d}"

    for c in contracts:
        avail = inv_map.get(c["resource_type"], 0.0)
        c["available_inventory"] = avail
        c["can_fulfill"] = (c["status"] == "AVAILABLE" and avail >= c["target_amount"])
        c["progress_pct"] = min(100.0, round((avail / c["target_amount"]) * 100, 1)) if c["target_amount"] > 0 else 100.0

    return templates.TemplateResponse(
        request=request,
        name="components/contracts.html",
        context={
            "user": fresh_user,
            "contracts": contracts,
            "countdown_str": countdown_str,
            "message": message,
            "error": error,
        },
    )
