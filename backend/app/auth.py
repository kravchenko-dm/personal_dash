"""Dashboard sessions. A JWT whose `sub` is the user id (the legacy "owner" means user 1).

The owner logs in with DASH_PASSWORD; members with a one-time code the bot sends them (/login). A token
is void when issued before the global epoch ("log out everywhere" by the owner) or before the user's
own token_epoch (the member's "log out everywhere")."""

import hashlib
import hmac
import secrets
import time

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.models import OWNER_ID
from app.services import bot_access
from app.services.bot_access import Account

bearer = HTTPBearer(auto_error=False)
TOKEN_TTL = 60 * 60 * 24 * 90  # 90 days: a personal PWA should not log out often
CODE_TTL = 600  # one-time login codes from the bot
# tokens issued before this unix time are void for everyone; kept in the settings table and mirrored
# here, see app.services.security
_token_epoch: int = 0
_codes: dict[str, tuple[int, float]] = {}  # sha256(code) -> (user id, expires at)


def set_token_epoch(epoch: int) -> None:
    global _token_epoch
    _token_epoch = int(epoch)


def check_password(password: str) -> bool:
    return hmac.compare_digest(password.encode(), get_settings().dash_password.encode())


def issue_token(user_id: int = OWNER_ID) -> str:
    now = int(time.time())
    payload = {"sub": str(user_id), "iat": now, "exp": now + TOKEN_TTL}
    return jwt.encode(payload, get_settings().app_secret, algorithm="HS256")


def account_for_token(token: str) -> Account | None:
    try:
        payload = jwt.decode(token, get_settings().app_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    sub = str(payload.get("sub") or "")
    user_id = OWNER_ID if sub == "owner" else int(sub) if sub.isdigit() else None
    acc = bot_access.get(user_id) if user_id is not None else None
    iat = int(payload.get("iat") or 0)
    if acc is None or not acc.has_workspace or iat < _token_epoch or iat < acc.token_epoch:
        return None
    return acc


def verify_token(token: str) -> bool:
    return account_for_token(token) is not None


async def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Account:
    token = creds.credentials if creds else request.query_params.get("token")
    acc = account_for_token(token) if token else None
    if acc is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized")
    return acc


async def require_owner(user: Account = Depends(require_auth)) -> Account:
    if not user.is_owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Только для владельца")
    return user


# ---------- one-time login codes (sent by the bot) ----------


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def new_login_code(user_id: int) -> str:
    now = time.time()
    for key, (_, exp) in list(_codes.items()):
        if exp < now:
            _codes.pop(key, None)
    code = secrets.token_urlsafe(24)
    _codes[_hash(code)] = (user_id, now + CODE_TTL)
    return code


def redeem_login_code(code: str) -> int | None:
    """-> user id, once. Unknown or expired codes give None."""
    entry = _codes.pop(_hash(code or ""), None)
    if not entry or entry[1] < time.time():
        return None
    return entry[0]
