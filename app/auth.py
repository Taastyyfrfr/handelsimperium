import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, HTTPException, status, Depends
from typing import Optional, Dict, Any
from app.config import settings
from app.database import get_db_cursor

serializer = URLSafeTimedSerializer(settings.SECRET_KEY)

def hash_password(plain_password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(plain_password.encode("utf-8"), salt).decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False

def create_session_token(user_id: int, username: str) -> str:
    return serializer.dumps({"user_id": user_id, "username": username})

def decode_session_token(token: str, max_age: int = 86400 * 7) -> Optional[Dict[str, Any]]:
    try:
        data = serializer.loads(token, max_age=max_age)
        return data
    except (BadSignature, SignatureExpired):
        return None

def get_current_user_optional(request: Request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get(settings.COOKIE_NAME)
    if not token:
        return None
    data = decode_session_token(token)
    if not data:
        return None
    user_id = data.get("user_id")
    with get_db_cursor() as cur:
        cur.execute("SELECT id, username, balance, created_at FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
        return user

def get_current_user(request: Request) -> Dict[str, Any]:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nicht authentifiziert. Bitte einloggen.",
            headers={"HX-Redirect": "/auth/login"},
        )
    return user
