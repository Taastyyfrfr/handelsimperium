import time
from collections import defaultdict
from threading import Lock
from typing import Callable, Optional
from fastapi import Request, HTTPException, status
from fastapi.responses import HTMLResponse
from app.config import settings

class SlidingWindowRateLimiter:
    """
    Thread-safe in-memory sliding-window rate limiter.
    Identifies clients via session token or IP address.
    """
    def __init__(self):
        # Key -> list of float timestamps
        self._history = defaultdict(list)
        self._lock = Lock()

    def _get_client_key(self, request: Request, scope: str) -> str:
        # 1. Prefer authenticated session token if available
        session_token = request.cookies.get(settings.COOKIE_NAME)
        if session_token:
            # Use prefix of session token
            key_id = f"session:{session_token[:24]}"
        else:
            # Fall back to client IP from X-Forwarded-For or socket
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

        now = time.time()
        key = self._get_client_key(request, scope)
        window_start = now - window_seconds

        with self._lock:
            # Prune timestamps outside current sliding window
            timestamps = [t for t in self._history[key] if t > window_start]
            if len(timestamps) >= max_requests:
                self._history[key] = timestamps
                return False
            
            timestamps.append(now)
            self._history[key] = timestamps
            return True

    def reset(self):
        """Clears all recorded rate limit history (useful for test isolation)."""
        with self._lock:
            self._history.clear()

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
