from fastapi import APIRouter, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.auth import get_current_user
from app.database import get_db_connection
from app.engine.production import calculate_offline_production, upgrade_building
import os

router = APIRouter(prefix="/resources", tags=["resources"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))

@router.get("/overview", response_class=HTMLResponse)
def resource_overview(request: Request, user: dict = Depends(get_current_user)):
    """
    HTMX-driven Resource Overview.
    Evaluates offline generation dynamically on page load/poll and updates the DOM.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            production_data = calculate_offline_production(cur, user["id"])
            cur.execute("SELECT id, username, balance FROM users WHERE id = %s", (user["id"],))
            fresh_user = cur.fetchone()
            conn.commit()

    return templates.TemplateResponse(
        request=request,
        name="components/resources.html",
        context={
            "user": fresh_user,
            "inventories": production_data["inventories"],
            "buildings": production_data["buildings"],
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
                message = f"Gebäude '{building_type}' erfolgreich auf Stufe {res['new_level']} ausgebaut!"
            except ValueError as e:
                conn.rollback()
                error = str(e)
            
            # Recalculate production to display updated stats
            production_data = calculate_offline_production(cur, user["id"])
            cur.execute("SELECT id, username, balance FROM users WHERE id = %s", (user["id"],))
            fresh_user = cur.fetchone()
            conn.commit()

    return templates.TemplateResponse(
        request=request,
        name="components/resources.html",
        context={
            "user": fresh_user,
            "inventories": production_data["inventories"],
            "buildings": production_data["buildings"],
            "message": message,
            "error": error,
        },
    )
