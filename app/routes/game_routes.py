from fastapi import APIRouter, Request, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.auth import get_current_user_optional
from app.database import get_db_connection
from app.engine.production import calculate_offline_production
import os

router = APIRouter(tags=["game"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))

@router.get("/", response_class=HTMLResponse)
def index_dashboard(request: Request):
    user = get_current_user_optional(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=status.HTTP_302_FOUND)
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            production_data = calculate_offline_production(cur, user["id"])
            cur.execute("SELECT id, username, balance FROM users WHERE id = %s", (user["id"],))
            fresh_user = cur.fetchone()
            conn.commit()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": fresh_user,
            "inventories": production_data["inventories"],
            "buildings": production_data["buildings"],
            "active_tab": "resources",
        },
    )
