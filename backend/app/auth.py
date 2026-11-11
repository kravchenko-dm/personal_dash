import hmac
import time

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

bearer = HTTPBearer(auto_error=False)
TOKEN_TTL = 60 * 60 * 24 * 90  # 90 days: a personal PWA should not log out often
# tokens issued before this unix time are void ("log out everywhere"); kept in the settings
# table and mirrored here, see app.services.security
_token_epoch: int = 0


def set_token_epoch(epoch: int) -> None:
    global _token_epoch
    _token_epoch = int(epoch)


def check_password(password: str) -> bool:
    return hmac.compare_digest(password.encode(), get_settings().dash_password.encode())


def issue_token() -> str:
    now = int(time.time())
    payload = {"sub": "owner", "iat": now, "exp": now + TOKEN_TTL}
    return jwt.encode(payload, get_settings().app_secret, algorithm="HS256")


def verify_token(token: str) -> bool:
    try:
        payload = jwt.decode(token, get_settings().app_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return False
    return int(payload.get("iat") or 0) >= _token_epoch


async def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> None:
    token = creds.credentials if creds else request.query_params.get("token")
    if not token or not verify_token(token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized")
