import os
from typing import Optional, Dict
from fastapi import APIRouter, Request, Depends, Form, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user
from app.database import get_db_connection
from app.engine.production import calculate_offline_production
from app.engine.caravans import (
    dispatch_caravan,
    unload_caravan,
    transfer_depot_to_kontor,
    get_expeditions_overview,
)

router = APIRouter(tags=["caravans"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))


def render_expeditions_response(
    request: Request,
    user_id: int,
    message: Optional[str] = None,
    error: Optional[str] = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Renders the comprehensive Logistics Terminal HTMX partial."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            calculate_offline_production(cur, user_id)
            cur.execute("SELECT id, username, balance FROM users WHERE id = %s", (user_id,))
            user_row = cur.fetchone()
            overview = get_expeditions_overview(cur, user_id)
            conn.commit()

    return templates.TemplateResponse(
        request=request,
        name="components/expeditions.html",
        context={
            "user": user_row,
            "overview": overview,
            "message": message,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/expeditions", response_class=HTMLResponse)
@router.get("/caravans", response_class=HTMLResponse)
def get_expeditions_view(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Returns the Logistics Terminal partial with active caravans, dispatch console, and regional depots."""
    return render_expeditions_response(request, user["id"])


@router.post("/caravans/dispatch", response_class=HTMLResponse)
def handle_dispatch_caravan(
    request: Request,
    destination_region_id: int = Form(...),
    origin_region_id: Optional[int] = Form(None),
    cargo_wood: float = Form(0.0),
    cargo_stone: float = Form(0.0),
    cargo_iron: float = Form(0.0),
    cargo_grain: float = Form(0.0),
    cargo_cloth: float = Form(0.0),
    user: dict = Depends(get_current_user),
):
    """
    Validates cargo capacity and inventory / depot balance, deducts resources atomically,
    calculates transit duration and departure/arrival timestamps,
    and inserts a new EN_ROUTE caravan (outbound or return).
    """
    cargo = {
        "wood": cargo_wood,
        "stone": cargo_stone,
        "iron": cargo_iron,
        "grain": cargo_grain,
        "cloth": cargo_cloth,
    }

    message = None
    error = None

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                calculate_offline_production(cur, user["id"])
                res = dispatch_caravan(cur, user["id"], destination_region_id, cargo, origin_region_id=origin_region_id)
                conn.commit()
                message = (
                    f"Karawane #{res['id']} erfolgreich nach [{res['dest_tag']}] {res['dest_name']} entsandt! "
                    f"Ladung: {res['total_cargo']:.1f} Güter | Distanz: {res['distance']:.1f} sm | Reisedauer: {res['duration_seconds']}s."
                )
            except ValueError as e:
                conn.rollback()
                error = str(e)
            except Exception as e:
                conn.rollback()
                error = f"Fehler bei der Expedition: {str(e)}"

    return render_expeditions_response(request, user["id"], message=message, error=error)


@router.post("/caravans/{caravan_id}/unload", response_class=HTMLResponse)
def handle_unload_caravan(
    request: Request,
    caravan_id: int,
    user: dict = Depends(get_current_user),
):
    """
    Unloads an arrived caravan into the foreign regional depot or home Kontor warehouse.
    """
    message = None
    error = None
    status_code = 200

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                calculate_offline_production(cur, user["id"])
                res = unload_caravan(cur, user["id"], caravan_id)
                conn.commit()
                cargo_str = ", ".join(f"{v:.0f} {k}" for k, v in res["cargo"].items() if v > 0)
                message = f"Karawane #{caravan_id} erfolgreich im Kontor/Depot [{res['dest_tag']}] {res['dest_name']} entladen! ({cargo_str})"
            except ValueError as e:
                conn.rollback()
                error = str(e)
                err_low = error.lower()
                if "regionaldepot ist voll" in err_low or "kapazitätsgrenze" in err_low or "zentrallager ist voll" in err_low:
                    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
            except Exception as e:
                conn.rollback()
                error = f"Fehler beim Entladen: {str(e)}"

    return render_expeditions_response(request, user["id"], message=message, error=error, status_code=status_code)

