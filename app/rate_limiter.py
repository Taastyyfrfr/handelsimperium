import time
import random
import logging
from typing import Callable, Optional
from datetime import datetime, timezone, timedelta
from fastapi import Request, HTTPException, status
from fastapi.responses import HTMLResponse

from app.config import settings
from app.database import get_db_connection

logger = logging.getLogger(__name__)

class SlidingWindowRateLimiter:
    """
    PostgreSQL-backed shared sliding-window rate limiter.
    Synchronizes request limits across multiple Uvicorn worker processes.
    Identifies clients via session token or IP address.
    """
    def _get_client_key(self, request: Request, scope: str) -> str:
        session_token = request.cookies.get(settings.COOKIE_NAME)
        if session_token:
            key_id = f"session:{session_token[:24]}"
        else:
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                client_ip = forwarded.split(",")[0].strip()
            else:
                client_ip = request.client.host if request.client else "127.0.0.1"
            key_id = f"ip:{client_ip}"
        
        return f"{scope}:{key_id}"

    def check(self, request: Request, max_requests: int, window_seconds: int, scope: str) -> bool:
        if not settings.RATE_LIMIT_ENABLED:
            return True

        key = self._get_client_key(request, scope)

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(seconds=window_seconds)

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    # Serialize concurrent checks for this specific client key across all workers
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))

                    # Count existing requests in sliding window
                    cur.execute(
                        """
                        SELECT COUNT(*) AS count
                        FROM rate_limits
                        WHERE client_key = %s
                          AND created_at > %s
                        """,
                        (key, window_start),
                    )
                    row = cur.fetchone()
                    count = row["count"] if row else 0

                    if count >= max_requests:
                        return False

                    # Record current request
                    cur.execute(
                        "INSERT INTO rate_limits (client_key, created_at) VALUES (%s, %s)",
                        (key, now),
                    )

                    # Automated rolling maintenance to prevent unbounded table growth
                    cur.execute("DELETE FROM rate_limits WHERE created_at < NOW() - INTERVAL '1 hour'")

                    conn.commit()
                    return True
        except Exception as e:
            # Fail-open if rate limiter table is unavailable during bootstrap
            logger.warning("SlidingWindowRateLimiter error: %s", e)
            return True

    def prune_expired(self) -> int:
        """Explicitly deletes rate limit entries older than 1 hour across all workers."""
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM rate_limits WHERE created_at < NOW() - INTERVAL '1 hour'")
                    count = cur.rowcount
                    conn.commit()
                    return count
        except Exception as e:
            logger.warning("Failed to prune expired rate limits: %s", e)
            return 0

    def reset(self):
        """Clears all recorded rate limit history across all workers."""
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM rate_limits")
                    conn.commit()
        except Exception:
            pass

limiter = SlidingWindowRateLimiter()

def rate_limit(max_requests: int, window_seconds: int, scope: str = "default") -> Callable:
    """FastAPI Dependency for route-level sliding window rate limiting."""
    def dependency(request: Request):
        allowed = limiter.check(request, max_requests, window_seconds, scope)
        if not allowed:
            # If HTMX request, return partial alert with 429
            if request.headers.get("HX-Request"):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Zu viele Anfragen. Bitte warte einen Moment, bevor Du weitere Aktionen ausführst.",
                )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Zu viele Anfragen. Bitte warte einen Moment.",
            )
        return True
    return dependency
