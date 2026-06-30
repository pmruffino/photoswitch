import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from dependencies import get_db, get_redis, get_current_user
from models import User
from auth import hash_password, verify_password, create_session, delete_session

router = APIRouter()

SESSION_TTL = int(os.environ.get("SESSION_TTL_SECONDS", 86400))
SIGNUP_POLICY_KEY = "psw:config:signup_policy"


class RegisterRequest(BaseModel):
    username: str
    password: str
    email: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


def _user_out(user: User) -> dict:
    return {
        "id": str(user.id),
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "is_approved": user.is_approved,
        "created_at": user.created_at.isoformat(),
    }


@router.post("/register", status_code=201)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    policy = await redis.get(SIGNUP_POLICY_KEY) or "open"
    if policy == "closed":
        raise HTTPException(status_code=403, detail="Registration is closed")

    if len(body.username) < 3 or len(body.username) > 64:
        raise HTTPException(status_code=400, detail="Username must be 3–64 characters")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already taken")

    if body.email:
        email_check = await db.execute(select(User).where(User.email == body.email))
        if email_check.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already in use")

    count_result = await db.execute(select(func.count()).select_from(User))
    is_first_user = count_result.scalar() == 0

    user = User(
        username=body.username,
        email=body.email or None,
        password_hash=hash_password(body.password),
        role="admin" if is_first_user else "user",
        is_approved=is_first_user or (policy == "open"),
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    result = _user_out(user)
    result["message"] = (
        "Registration successful"
        if user.is_approved
        else "Registration submitted — awaiting admin approval"
    )
    return result


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    if not user.is_approved:
        raise HTTPException(status_code=403, detail="Account pending approval")

    ttl = 30 * 24 * 3600 if body.remember_me else SESSION_TTL
    token = await create_session(redis, str(user.id), ttl)
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=ttl,
    )

    return _user_out(user)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    redis: aioredis.Redis = Depends(get_redis),
):
    token = request.cookies.get("session")
    if token:
        await delete_session(redis, token)
    response.delete_cookie("session", httponly=True, samesite="lax")


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {
        "id": str(user.id),
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "is_approved": user.is_approved,
        "created_at": user.created_at.isoformat(),
    }


@router.get("/signup-policy")
async def signup_policy(redis: aioredis.Redis = Depends(get_redis)):
    policy = await redis.get(SIGNUP_POLICY_KEY) or "open"
    return {"signup_policy": policy}
