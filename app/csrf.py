import secrets
import hmac
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from app.config import settings

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

class CSRFMiddleware(BaseHTTPMiddleware):
    """
    CSRF Protection Middleware for FastAPI / HTMX.
    - Generates cryptographically secure CSRF token and sets double-submit cookie.
    - Attaches csrf_token to request.state for Jinja2 template rendering.
    - Validates X-CSRF-Token / HX-CSRF-Token header or csrf_token form field on state-changing requests (POST, PUT, DELETE, PATCH).
    - Can be toggled via settings.CSRF_ENABLED (useful for specific unit testing).
    """

    async def dispatch(self, request: Request, call_next):
        csrf_cookie = request.cookies.get(settings.CSRF_COOKIE_NAME)
        csrf_token = csrf_cookie
        new_cookie_needed = False

        if not csrf_token:
            csrf_token = secrets.token_hex(32)
            new_cookie_needed = True

        # Attach token to request state for access in templates
        request.state.csrf_token = csrf_token

        # Validate state-changing requests if CSRF is enabled
        if request.method not in SAFE_METHODS and settings.CSRF_ENABLED:
            submitted_token = (
                request.headers.get("x-csrf-token")
                or request.headers.get("hx-csrf-token")
            )

            if not submitted_token:
                content_type = request.headers.get("content-type", "")
                if (
                    "application/x-www-form-urlencoded" in content_type
                    or "multipart/form-data" in content_type
                ):
                    try:
                        body = await request.body()

                        async def receive_temp():
                            return {"type": "http.request", "body": body, "more_body": False}

                        temp_req = Request(request.scope, receive_temp)
                        form = await temp_req.form()
                        submitted_token = form.get("csrf_token")

                        # Re-wrap receive so downstream route handlers can read form fields
                        async def receive_downstream():
                            return {"type": "http.request", "body": body, "more_body": False}

                        request = Request(request.scope, receive_downstream)
                    except Exception:
                        pass

            if (
                not submitted_token
                or not csrf_cookie
                or not hmac.compare_digest(str(submitted_token), str(csrf_cookie))
            ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF-Token ungültig oder fehlt."},
                )

        response: Response = await call_next(request)

        # Set cookie if new token was generated or cookie was missing
        if new_cookie_needed:
            is_secure = (
                request.url.scheme == "https"
                or request.headers.get("x-forwarded-proto") == "https"
            )
            response.set_cookie(
                key=settings.CSRF_COOKIE_NAME,
                value=csrf_token,
                httponly=False,  # Must be readable by client JS / HTMX
                samesite="lax",
                secure=is_secure,
                path="/",
            )

        return response
