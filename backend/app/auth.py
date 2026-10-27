import hmac
import time

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

bearer = HTTPBearer(auto_error=False)
TOKEN_TTL = 60 * 60 * 24 * 90  # 90 days: a personal PWA should not log out often


def check_password(password: str) -> bool:
    return hmac.compare_digest(password, get_settings().dash_password)


def issue_token() -> str:
    now = int(time.time())
    payload = {"sub": "owner", "iat": now, "exp": now + TOKEN_TTL}
    return jwt.encode(payload, get_settings().app_secret, algorithm="HS256")


def verify_token(token: str) -> bool:
    try:
        jwt.decode(token, get_settings().app_secret, algorithms=["HS256"])
        return True
    except jwt.PyJWTError:
        return False


async def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> None:
    token = creds.credentials if creds else request.query_params.get("token")
    if not token or not verify_token(token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized")
