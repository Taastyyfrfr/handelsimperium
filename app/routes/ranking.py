from fastapi import APIRouter, Request, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timezone
import os

from app.auth import get_current_user_optional
from app.database import get_db_connection
from app.engine.ranking import ranking_cache

router = APIRouter(prefix="/ranking", tags=["ranking"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))

@router.get("", response_class=HTMLResponse)
def get_ranking_view(
    request: Request,
):
    user = get_current_user_optional(request)
    is_htmx = bool(request.headers.get("HX-Request"))

    if not user:
        if is_htmx:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Nicht authentifiziert. Bitte einloggen.")
        return RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    """
    Renders HTMX-partial for the merchant leaderboard.
    Displays the top 50 merchants plus personal rank breakdown,
    backed by a 5-minute in-memory cache.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            all_entries, price_map, cached_at = ranking_cache.get_leaderboard(cur)
            
            # Find authenticated merchant's ranking entry
            user_entry = next((e for e in all_entries if e["user_id"] == user["id"]), None)
            
            # If user somehow not found in cache (e.g. newly registered), force refresh
            if not user_entry:
                all_entries, price_map, cached_at = ranking_cache.get_leaderboard(cur, force_refresh=True)
                user_entry = next((e for e in all_entries if e["user_id"] == user["id"]), None)

    top_50 = all_entries[:50]
    total_merchants = len(all_entries)
    
    now = datetime.now(timezone.utc)
    cache_age_seconds = int((now - cached_at).total_seconds()) if cached_at else 0
    next_refresh_seconds = max(0, 300 - cache_age_seconds)

    return templates.TemplateResponse(
        request=request,
        name="components/ranking.html",
        context={
            "user": user,
            "top_50": top_50,
            "user_entry": user_entry,
            "total_merchants": total_merchants,
            "price_map": price_map,
            "cache_age_seconds": cache_age_seconds,
            "next_refresh_seconds": next_refresh_seconds,
        },
    )
