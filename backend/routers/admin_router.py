import json
import os
import shutil
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from dependencies import get_db, get_redis, require_admin
from models import User, JobRecord
from auth import hash_password
from schemas import (
    Stage,
    CLEANUP_HOUR_KEY,
    DEFAULT_CLEANUP_HOUR,
    DEFAULT_MAX_TAKEOUT_BYTES,
    DEFAULT_STAGING_RETENTION_DAYS,
    DEFAULT_WORKER_PCT,
    MAX_TAKEOUT_BYTES_KEY,
    STAGING_RETENTION_DAYS_KEY,
    WORKER_HEARTBEAT_TTL,
    calculate_semaphore_limit,
    semaphore_limit_key,
    worker_pct_key,
    worker_presence_key,
    job_key,
    user_jobs_key,
)

STAGING_ROOT = os.environ.get("STAGING_ROOT", "/staging")

router = APIRouter()


async def _count_admins(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count()).select_from(User).where(User.role == "admin")
    )
    return result.scalar() or 0


async def purge_user_data(user: User, db: AsyncSession, redis: aioredis.Redis) -> None:
    """Delete Redis job keys, staging dirs for all user jobs, then delete the user (DB cascade handles credentials/jobs)."""
    result = await db.execute(select(JobRecord).where(JobRecord.user_id == user.id))
    job_records = result.scalars().all()
    for record in job_records:
        raw = await redis.get(job_key(record.job_id))
        staging_dir = os.path.join(STAGING_ROOT, record.job_id)
        if raw:
            try:
                staging_dir = json.loads(raw).get("staging_dir") or staging_dir
            except Exception:
                pass
        await redis.delete(job_key(record.job_id))
        await redis.zrem(user_jobs_key(str(user.id)), record.job_id)
        if os.path.isdir(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)
    await db.delete(user)
    await db.commit()

SIGNUP_POLICY_KEY = "psw:config:signup_policy"


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


class UpdateUserRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    is_approved: bool | None = None
    email: str | None = None
    new_password: str | None = None


class CreateUserRequest(BaseModel):
    username: str
    password: str
    email: str | None = None
    role: str = "user"


class UpdateConfigRequest(BaseModel):
    signup_policy: str | None = None
    worker_pct: dict[str, int] | None = None
    max_takeout_gb: int | None = None
    staging_retention_days: int | None = None
    cleanup_hour: int | None = None


@router.get("/users")
async def list_users(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = await db.execute(select(User).order_by(User.created_at))
    return [_user_out(u) for u in result.scalars().all()]


@router.post("/users", status_code=201)
async def create_user(
    body: CreateUserRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already taken")

    user = User(
        username=body.username,
        email=body.email or None,
        password_hash=hash_password(body.password),
        role=body.role if body.role in ("admin", "user") else "user",
        is_active=True,
        is_approved=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _user_out(user)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    body: UpdateUserRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="User not found")

    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if body.role is not None:
        if body.role not in ("admin", "user"):
            raise HTTPException(status_code=400, detail="Invalid role")
        if body.role == "user" and user.role == "admin":
            count = await _count_admins(db)
            if count <= 1:
                raise HTTPException(status_code=400, detail="Cannot demote the only admin account")
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.is_approved is not None:
        user.is_approved = body.is_approved
    if body.email is not None:
        user.email = body.email or None
    if body.new_password is not None:
        if len(body.new_password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        user.password_hash = hash_password(body.new_password)

    await db.commit()
    await db.refresh(user)
    return _user_out(user)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
    redis: aioredis.Redis = Depends(get_redis),
):
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="User not found")

    if uid == admin.id:
        raise HTTPException(status_code=400, detail="Use Account settings to delete your own account")

    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.role == "admin":
        count = await _count_admins(db)
        if count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the only admin account")

    await purge_user_data(user, db, redis)


def _job_out(record: JobRecord, live: dict | None, username: str) -> dict:
    out = {
        "job_id": record.job_id,
        "username": username,
        "stage": record.stage,
        "status": record.status,
        "takeout_url": record.takeout_url,
        "total_items": record.total_items,
        "processed_items": record.processed_items,
        "error": record.error,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "date_filter": None,
        "auto_ingest": True,
    }
    if live:
        out["stage"] = live.get("stage", out["stage"])
        out["status"] = live.get("status", out["status"])
        if live.get("total_items") is not None:
            out["total_items"] = live["total_items"]
        out["processed_items"] = live.get("processed_items", out["processed_items"])
        out["error"] = live.get("error") or out["error"]
        out["date_filter"] = live.get("date_filter")
        out["auto_ingest"] = live.get("auto_ingest", True)
    return out


@router.get("/jobs")
async def list_all_jobs(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    result = await db.execute(
        select(JobRecord, User)
        .join(User, JobRecord.user_id == User.id)
        .order_by(JobRecord.created_at.desc())
    )
    out = []
    for record, user in result.all():
        raw = await redis.get(job_key(record.job_id))
        live = json.loads(raw) if raw else None
        out.append(_job_out(record, live, user.username))
    return out


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_any_job(
    job_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    result = await db.execute(select(JobRecord).where(JobRecord.job_id == job_id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    # Read staging path from the job object before removing the Redis key
    raw = await redis.get(job_key(job_id))
    staging_dir = os.path.join(STAGING_ROOT, job_id)
    if raw:
        try:
            staging_dir = json.loads(raw).get("staging_dir") or staging_dir
        except Exception:
            pass

    await redis.delete(job_key(job_id))
    await redis.zrem(user_jobs_key(str(record.user_id)), job_id)

    if os.path.isdir(staging_dir):
        shutil.rmtree(staging_dir, ignore_errors=True)

    await db.delete(record)
    await db.commit()


@router.get("/config")
async def get_config(
    admin: User = Depends(require_admin),
    redis: aioredis.Redis = Depends(get_redis),
):
    policy = await redis.get(SIGNUP_POLICY_KEY) or "open"

    now = time.time()
    pcts: dict[str, int] = {}
    counts: dict[str, int] = {}
    for stage in Stage:
        raw_pct = await redis.get(worker_pct_key(stage))
        pcts[stage.value] = int(raw_pct) if raw_pct else DEFAULT_WORKER_PCT
        count = await redis.zcount(worker_presence_key(stage), now - WORKER_HEARTBEAT_TTL, "+inf")
        counts[stage.value] = int(count)

    raw_max = await redis.get(MAX_TAKEOUT_BYTES_KEY)
    max_takeout_bytes = int(raw_max) if raw_max else DEFAULT_MAX_TAKEOUT_BYTES
    raw_ret = await redis.get(STAGING_RETENTION_DAYS_KEY)
    retention_days = int(raw_ret) if raw_ret else DEFAULT_STAGING_RETENTION_DAYS
    raw_hour = await redis.get(CLEANUP_HOUR_KEY)
    cleanup_hour = int(raw_hour) if raw_hour else DEFAULT_CLEANUP_HOUR

    return {
        "signup_policy": policy,
        "worker_pct": pcts,
        "worker_counts": counts,
        "max_takeout_gb": max_takeout_bytes // (1024 * 1024 * 1024),
        "staging_retention_days": retention_days,
        "cleanup_hour": cleanup_hour,
    }


@router.patch("/config")
async def update_config(
    body: UpdateConfigRequest,
    admin: User = Depends(require_admin),
    redis: aioredis.Redis = Depends(get_redis),
):
    if body.signup_policy is not None:
        if body.signup_policy not in ("open", "approval", "closed"):
            raise HTTPException(status_code=400, detail="signup_policy must be open, approval, or closed")
        await redis.set(SIGNUP_POLICY_KEY, body.signup_policy)

    if body.worker_pct:
        now = time.time()
        for stage_str, pct in body.worker_pct.items():
            try:
                stage = Stage(stage_str)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Unknown stage: {stage_str}")
            if not (1 <= pct <= 100):
                raise HTTPException(status_code=400, detail="Percentage must be between 1 and 100")
            await redis.set(worker_pct_key(stage), pct)
            count = int(await redis.zcount(worker_presence_key(stage), now - WORKER_HEARTBEAT_TTL, "+inf"))
            await redis.set(semaphore_limit_key(stage), calculate_semaphore_limit(count, pct))

    if body.max_takeout_gb is not None:
        if not (1 <= body.max_takeout_gb <= 200):
            raise HTTPException(status_code=400, detail="max_takeout_gb must be between 1 and 200")
        await redis.set(MAX_TAKEOUT_BYTES_KEY, body.max_takeout_gb * 1024 * 1024 * 1024)

    if body.staging_retention_days is not None:
        if not (1 <= body.staging_retention_days <= 365):
            raise HTTPException(status_code=400, detail="staging_retention_days must be between 1 and 365")
        await redis.set(STAGING_RETENTION_DAYS_KEY, body.staging_retention_days)

    if body.cleanup_hour is not None:
        if not (0 <= body.cleanup_hour <= 23):
            raise HTTPException(status_code=400, detail="cleanup_hour must be between 0 and 23")
        await redis.set(CLEANUP_HOUR_KEY, body.cleanup_hour)

    return {"status": "updated"}
