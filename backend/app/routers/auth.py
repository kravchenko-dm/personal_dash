from fastapi import APIRouter, HTTPException, status

from app.auth import check_password, issue_token
from app.schemas import LoginIn, TokenOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn) -> TokenOut:
    if not check_password(body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong password")
    return TokenOut(token=issue_token())
