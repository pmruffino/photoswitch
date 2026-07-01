import asyncio
import json
import logging
import os
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


from db import engine, Base
from models import JobRecord
from redis_client import init_redis
from routers.auth_router import router as auth_router
from routers.admin_router import router as admin_router
from routers.user_router import router as user_router
from routers.jobs_router import router as jobs_router
from schemas import (
    Stage,
    CLEANUP_HOUR_KEY,
    DEFAULT_CLEANUP_HOUR,
    DEFAULT_MAX_TAKEOUT_BYTES,
    DEFAULT_STAGING_RETENTION_DAYS,
    DEFAULT_WORKER_PCT,
    MAX_TAKEOUT_BYTES_KEY,
    STAGING_RETENTION_DAYS_KEY,
    job_key,
    user_jobs_key,
    worker_pct_key,
)

logger = logging.getLogger(__name__)

SIGNUP_POLICY_KEY = "psw:config:signup_policy"
STAGING_ROOT = os.environ.get("STAGING_ROOT", "/staging")


async def _run_cleanup(redis, staging_root: str) -> None:
    raw = await redis.get(STAGING_RETENTION_DAYS_KEY)
    retention_days = int(raw) if raw else DEFAULT_STAGING_RETENTION_DAYS
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_ts = cutoff.timestamp()

    # --- Pass 1: DB-driven ---
    # Remove job records, Redis keys, and staging dirs for terminal jobs whose
    # last state change is older than the retention window.
    # Exception: jobs with a date filter are kept so the user can adjust and re-run.
    jobs_removed = 0
    dirs_removed = 0
    async with AsyncSession(engine) as session:
        result = await session.execute(
            select(JobRecord).where(
                JobRecord.status.in_(["succeeded", "failed", "cancelled"]),
                JobRecord.updated_at < cutoff,
            )
        )
        records = result.scalars().all()

        for record in records:
            raw_job = await redis.get(job_key(record.job_id))
            if raw_job:
                try:
                    if json.loads(raw_job).get("date_filter"):
                        continue
                except Exception:
                    pass

            await redis.delete(job_key(record.job_id))
            await redis.zrem(user_jobs_key(str(record.user_id)), record.job_id)

            staging_dir = os.path.join(staging_root, record.job_id)
            if os.path.isdir(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
                dirs_removed += 1

            await session.delete(record)
            jobs_removed += 1

        if jobs_removed:
            await session.commit()

    if jobs_removed:
        logger.info(
            "Cleanup pass 1: removed %d job records (%d staging dirs, retention=%d days)",
            jobs_removed, dirs_removed, retention_days,
        )

    # --- Pass 2: filesystem scan ---
    # Remove orphaned staging dirs (no corresponding DB record) whose mtime is
    # older than the retention window. Known job dirs are always excluded here —
    # date-filtered or otherwise active jobs must not be touched by this pass.
    async with AsyncSession(engine) as session:
        result = await session.execute(select(JobRecord.job_id))
        known_job_ids = {row[0] for row in result.all()}

    orphans_removed = 0
    if os.path.isdir(staging_root):
        with os.scandir(staging_root) as it:
            for entry in it:
                if entry.name in known_job_ids:
                    continue
                try:
                    if entry.stat().st_mtime < cutoff_ts:
                        if entry.is_dir(follow_symlinks=False):
                            shutil.rmtree(entry.path, ignore_errors=True)
                        else:
                            os.unlink(entry.path)
                        orphans_removed += 1
                except Exception:
                    logger.warning("Could not remove orphaned staging entry: %s", entry.path)

    if orphans_removed:
        logger.info(
            "Cleanup pass 2: removed %d orphaned staging entries (retention=%d days)",
            orphans_removed, retention_days,
        )


async def _cleanup_loop(redis, staging_root: str) -> None:
    while True:
        # Determine next occurrence of the configured cleanup hour (UTC).
        raw_hour = await redis.get(CLEANUP_HOUR_KEY)
        hour = int(raw_hour) if raw_hour else DEFAULT_CLEANUP_HOUR

        now = datetime.now(timezone.utc)
        next_run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)

        sleep_seconds = (next_run - now).total_seconds()
        logger.info(
            "Cleanup scheduled at %02d:00 UTC — sleeping %.0f s",
            hour, sleep_seconds,
        )
        await asyncio.sleep(sleep_seconds)

        try:
            await _run_cleanup(redis, staging_root)
        except Exception:
            logger.exception("Staging cleanup task error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    redis = await init_redis()
    for stage in Stage:
        await redis.set(worker_pct_key(stage), DEFAULT_WORKER_PCT, nx=True)
    await redis.set(SIGNUP_POLICY_KEY, "open", nx=True)
    await redis.set(MAX_TAKEOUT_BYTES_KEY, DEFAULT_MAX_TAKEOUT_BYTES, nx=True)
    await redis.set(STAGING_RETENTION_DAYS_KEY, DEFAULT_STAGING_RETENTION_DAYS, nx=True)
    await redis.set(CLEANUP_HOUR_KEY, DEFAULT_CLEANUP_HOUR, nx=True)

    cleanup_task = asyncio.create_task(_cleanup_loop(redis, STAGING_ROOT))

    yield

    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Photoswitch", version="0.1.0", lifespan=lifespan)

# The SPA is served same-origin (nginx proxies /api to the backend), so no CORS
# is needed by default. A wildcard origin combined with credentials is both a
# security risk and rejected by browsers, so cross-origin access is opt-in:
# set CORS_ALLOW_ORIGINS to a comma-separated list of exact origins to enable it.
_cors_origins = [
    o.strip()
    for o in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
    if o.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(admin_router, prefix="/api/admin", tags=["admin"])
app.include_router(user_router, prefix="/api/user", tags=["user"])
app.include_router(jobs_router, prefix="/api/jobs", tags=["jobs"])
