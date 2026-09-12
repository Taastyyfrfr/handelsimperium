from fastapi import APIRouter, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.auth import get_current_user, get_user_by_id
from app.database import get_db_connection
from app.engine.production import calculate_offline_production, upgrade_building, get_latest_catchup, dismiss_catchup
import os

router = APIRouter(prefix="/resources", tags=["resources"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))

@router.get("/catchup", response_class=HTMLResponse)
def get_catchup_modal(request: Request, user: dict = Depends(get_current_user)):
    """
    Evaluates whether an unacknowledged offline catch-up event exists.
    Returns the catch-up modal partial if available, otherwise returns empty response.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            catchup = get_latest_catchup(cur, user["id"])

    if not catchup:
        return HTMLResponse(content="", status_code=status.HTTP_200_OK)

    # Only show if there was noticeable offline time (>= 10s) or trades took place
    has_trades = catchup.get("trade_delta", {}).get("total_trade_count", 0) > 0
    has_time = catchup.get("offline_seconds", 0) >= 10.0

    if not (has_trades or has_time):
        return HTMLResponse(content="", status_code=status.HTTP_200_OK)

    return templates.TemplateResponse(
        request=request,
        name="components/catchup_modal.html",
        context={"user": user, "catchup": catchup},
    )

@router.post("/catchup/dismiss", response_class=HTMLResponse)
def dismiss_catchup_modal(user: dict = Depends(get_current_user)):
    """
    Dismisses the offline catch-up modal for the current session.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            dismiss_catchup(cur, user["id"])
            conn.commit()
    return HTMLResponse(content="", status_code=status.HTTP_200_OK)


@router.get("/overview", response_class=HTMLResponse)
def resource_overview(request: Request, user: dict = Depends(get_current_user)):
    """
    HTMX-driven Resource Overview.
    Evaluates offline generation dynamically on page load/poll and updates the DOM.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            production_data = calculate_offline_production(cur, user["id"])
            fresh_user = get_user_by_id(cur, user["id"])
            conn.commit()

    return templates.TemplateResponse(
        request=request,
        name="components/resources.html",
        context={
            "user": fresh_user,
            "inventories": production_data["inventories"],
            "buildings": production_data["buildings"],
            "storage_cap": production_data.get("storage_cap"),
            "warehouse_level": production_data.get("warehouse_level"),
            "message": None,
            "error": None,
        },
    )

@router.post("/upgrade", response_class=HTMLResponse)
def upgrade_building_action(
    request: Request,
    building_type: str = Form(...),
    user: dict = Depends(get_current_user),
):
    message = None
    error = None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                res = upgrade_building(cur, user["id"], building_type)
                conn.commit()
                if res["building_type"] == "warehouse":
                    message = f"{res['name']} erfolgreich auf Stufe {res['new_level']} ausgebaut! Neue Lagerkapazität: {res['new_storage_cap']:.0f} Einheiten je Ware."
                else:
                    message = f"{res['name']} erfolgreich auf Stufe {res['new_level']} ausgebaut! Erzeugungsrate: {(res['new_rate'] * 60):.1f} / Min."
            except ValueError as e:
                conn.rollback()
                error = str(e)
            
            # Recalculate production to display updated stats
            production_data = calculate_offline_production(cur, user["id"])
            fresh_user = get_user_by_id(cur, user["id"])
            conn.commit()

    return templates.TemplateResponse(
        request=request,
        name="components/resources.html",
        context={
            "user": fresh_user,
            "inventories": production_data["inventories"],
            "buildings": production_data["buildings"],
            "storage_cap": production_data.get("storage_cap"),
            "warehouse_level": production_data.get("warehouse_level"),
            "message": message,
            "error": error,
        },
    )
