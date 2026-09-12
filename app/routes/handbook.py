import os
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_user_by_id
from app.database import get_db_connection
from app.config import (
    settings,
    BUILDING_CONFIG,
    REFERENCE_PRICES,
    CONTRACT_TEMPLATES,
    GUILD_PROJECT_CONFIG,
    GUILD_CREATION_FEE,
    STARTER_CONFIG,
    SUPPORTED_RESOURCES,
)
from app.engine.tutorial import TUTORIAL_STEPS

router = APIRouter(prefix="/handbuch", tags=["handbook"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))

@router.get("", response_class=HTMLResponse)
def get_handbook(
    request: Request,
    user: dict = Depends(get_current_user),
):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            fresh_user = get_user_by_id(cur, user["id"])
            cur.execute("SELECT id, name, tag, description, coord_x, coord_y, resource_multipliers FROM regions ORDER BY id ASC")
            regions = cur.fetchall()

    return templates.TemplateResponse(
        request=request,
        name="components/handbook.html",
        context={
            "user": fresh_user,
            "regions": regions,
            "market_fee_pct": round(settings.MARKET_FEE_RATE * 100, 1),
            "base_storage_cap": settings.BASE_STORAGE_CAP,
            "building_config": BUILDING_CONFIG,
            "reference_prices": REFERENCE_PRICES,
            "contract_templates": CONTRACT_TEMPLATES,
            "guild_projects": GUILD_PROJECT_CONFIG,
            "guild_creation_fee": GUILD_CREATION_FEE,
            "starter_config": STARTER_CONFIG,
            "tutorial_steps": TUTORIAL_STEPS,
            "supported_resources": SUPPORTED_RESOURCES,
        },
    )
