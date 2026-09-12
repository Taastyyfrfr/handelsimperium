from fastapi import APIRouter, Request, Depends, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import os

from app.auth import get_current_user
from app.database import get_db_connection
from app.engine.notifications import (
    get_user_notifications,
    get_unread_notification_count,
    mark_all_notifications_read,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))

@router.get("", response_class=HTMLResponse)
def list_notifications(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """
    Renders HTMX-partial for the merchant notifications inbox / dropdown.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            notifs = get_user_notifications(cur, user["id"], limit=15)
            unread_count = get_unread_notification_count(cur, user["id"])

    return templates.TemplateResponse(
        request=request,
        name="components/notifications.html",
        context={
            "user": user,
            "notifications": notifs,
            "unread_count": unread_count,
        },
    )

@router.get("/badge", response_class=HTMLResponse)
def get_badge(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """
    Returns unread badge partial for the notification bell in header.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            unread_count = get_unread_notification_count(cur, user["id"])

    if unread_count > 0:
        return HTMLResponse(
            content=f'<span id="unread-notification-badge" class="absolute -top-1 -right-1 w-4 h-4 bg-red-600 text-white text-[10px] font-bold rounded-full flex items-center justify-center animate-pulse">{unread_count}</span>'
        )
    return HTMLResponse(
        content='<span id="unread-notification-badge" class="hidden"></span>'
    )

@router.post("/read-all", response_class=HTMLResponse)
def read_all(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """
    Marks all notifications as read and re-renders the notifications partial.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            mark_all_notifications_read(cur, user["id"])
            conn.commit()
            notifs = get_user_notifications(cur, user["id"], limit=15)

    return templates.TemplateResponse(
        request=request,
        name="components/notifications.html",
        context={
            "user": user,
            "notifications": notifs,
            "unread_count": 0,
        },
    )
