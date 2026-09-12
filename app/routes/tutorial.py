import os
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user
from app.database import get_db_connection
from app.engine.tutorial import get_tutorial_status, claim_tutorial_reward

router = APIRouter(prefix="/tutorial", tags=["tutorial"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))

@router.get("/widget", response_class=HTMLResponse)
def get_widget(
    request: Request,
    user: dict = Depends(get_current_user),
):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            tut = get_tutorial_status(cur, user["id"])
            cur.execute("SELECT id, username, balance FROM users WHERE id = %s", (user["id"],))
            fresh_user = cur.fetchone()

    return templates.TemplateResponse(
        request=request,
        name="components/tutorial_widget.html",
        context={
            "user": fresh_user,
            "tut": tut,
        },
    )

@router.post("/claim", response_class=HTMLResponse)
def claim_reward(
    request: Request,
    user: dict = Depends(get_current_user),
):
    error = None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                claim_tutorial_reward(cur, user["id"])
                conn.commit()
            except ValueError as e:
                conn.rollback()
                error = str(e)
            except Exception as e:
                conn.rollback()
                error = f"Fehler: {str(e)}"

            tut = get_tutorial_status(cur, user["id"])
            cur.execute("SELECT id, username, balance FROM users WHERE id = %s", (user["id"],))
            fresh_user = cur.fetchone()

    return templates.TemplateResponse(
        request=request,
        name="components/tutorial_widget.html",
        context={
            "user": fresh_user,
            "tut": tut,
            "error": error,
        },
    )
