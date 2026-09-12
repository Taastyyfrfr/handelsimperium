from fastapi import APIRouter, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.auth import get_current_user, get_user_by_id
from app.database import get_db_connection
from app.engine.production import calculate_offline_production, upgrade_building
import os

router = APIRouter(prefix="/buildings", tags=["buildings"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))

@router.post("/{building_id}/upgrade", response_class=HTMLResponse)
def upgrade_building_by_id(
    request: Request,
    building_id: str,
    user: dict = Depends(get_current_user),
):
    """
    Atomic building upgrade endpoint: POST /buildings/{id}/upgrade
    Verifies and deducts multi-resource costs with row-level locks,
    increments building level, scales production rate or warehouse capacity,
    and returns the updated HTMX partial.
    """
    message = None
    error = None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                res = upgrade_building(cur, user["id"], building_id)
                conn.commit()
                if res["building_type"] == "warehouse":
                    message = f"{res['name']} erfolgreich auf Stufe {res['new_level']} ausgebaut! Neue Lagerkapazität: {res['new_storage_cap']:.0f} Einheiten je Ware."
                else:
                    message = f"{res['name']} erfolgreich auf Stufe {res['new_level']} ausgebaut! Erzeugungsrate: {(res['new_rate'] * 60):.1f} / Min."
            except ValueError as e:
                conn.rollback()
                error = str(e)

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
            "storage_cap": production_data["storage_cap"],
            "warehouse_level": production_data["warehouse_level"],
            "message": message,
            "error": error,
        },
    )
