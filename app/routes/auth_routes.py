from typing import Optional
from fastapi import APIRouter, Request, Response, Form, HTTPException, status, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.config import settings, STARTER_CONFIG
from app.database import get_db_connection
from app.auth import hash_password, verify_password, create_session_token, get_current_user_optional
from app.engine.production import ensure_user_entities
from app.rate_limiter import rate_limit
import os

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    user = get_current_user_optional(request)
    if user:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request=request, name="auth/login.html", context={"error": None})

@router.post("/login")
def login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    _limiter: bool = Depends(rate_limit(max_requests=5, window_seconds=60, scope="login")),
):

    username = username.strip()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, password_hash FROM users WHERE username = %s", (username,))
            user = cur.fetchone()
            if not user or not verify_password(password, user["password_hash"]):
                return templates.TemplateResponse(
                    request=request,
                    name="auth/login.html",
                    context={"error": "Ungültiger Benutzername oder Passwort."},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            
            token = create_session_token(user["id"], user["username"])
            redirect = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
            is_secure = (
                request.url.scheme == "https"
                or request.headers.get("x-forwarded-proto") == "https"
            )
            redirect.set_cookie(
                key=settings.COOKIE_NAME,
                value=token,
                httponly=True,
                max_age=86400 * 7,
                samesite="lax",
                secure=is_secure,
            )
            return redirect

@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    user = get_current_user_optional(request)
    if user:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, tag, description, coord_x, coord_y, resource_multipliers FROM regions ORDER BY id ASC")
            regions = cur.fetchall()
    return templates.TemplateResponse(request=request, name="auth/register.html", context={"error": None, "regions": regions})

@router.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    region_id: Optional[int] = Form(None),
):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, tag, description, coord_x, coord_y, resource_multipliers FROM regions ORDER BY id ASC")
            regions = cur.fetchall()

            username = username.strip()
            if region_id is not None:
                # Validate region exists
                cur.execute("SELECT id FROM regions WHERE id = %s", (region_id,))
                if not cur.fetchone():
                    return templates.TemplateResponse(
                        request=request,
                        name="auth/register.html",
                        context={"error": "Ungültige Heimatregion gewählt.", "regions": regions},
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
            else:
                # Default fallback region (Danzig) for legacy callers/fallback
                cur.execute("SELECT id FROM regions WHERE tag = 'DANZ'")
                reg_danz = cur.fetchone()
                region_id = reg_danz["id"] if reg_danz else 1

            if len(username) < 3 or len(username) > 32:
                return templates.TemplateResponse(
                    request=request,
                    name="auth/register.html",
                    context={"error": "Benutzername muss zwischen 3 und 32 Zeichen lang sein.", "regions": regions},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if len(password) < 6:
                return templates.TemplateResponse(
                    request=request,
                    name="auth/register.html",
                    context={"error": "Passwort muss mindestens 6 Zeichen lang sein.", "regions": regions},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if password != confirm_password:
                return templates.TemplateResponse(
                    request=request,
                    name="auth/register.html",
                    context={"error": "Passwörter stimmen nicht überein.", "regions": regions},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            pwd_hash = hash_password(password)
            cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                return templates.TemplateResponse(
                    request=request,
                    name="auth/register.html",
                    context={"error": "Dieser Benutzername ist bereits vergeben.", "regions": regions},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            
            cur.execute(
                "INSERT INTO users (username, password_hash, balance, region_id) VALUES (%s, %s, %s, %s) RETURNING id",
                (username, pwd_hash, STARTER_CONFIG["balance"], region_id),
            )

            new_user = cur.fetchone()
            user_id = new_user["id"]
            
            # Initialize starter buildings (with regional multipliers) and starter warehouse inventories
            ensure_user_entities(cur, user_id)
            conn.commit()

            token = create_session_token(user_id, username)
            redirect = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
            is_secure = (
                request.url.scheme == "https"
                or request.headers.get("x-forwarded-proto") == "https"
            )
            redirect.set_cookie(
                key=settings.COOKIE_NAME,
                value=token,
                httponly=True,
                max_age=86400 * 7,
                samesite="lax",
                secure=is_secure,
            )
            return redirect

@router.get("/logout")
@router.post("/logout")
def logout():
    redirect = RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie(key=settings.COOKIE_NAME)
    return redirect
