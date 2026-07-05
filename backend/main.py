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
from destinations import build_destination
from models import JobRecord, ICloudConnection
from redis_client import init_redis
from routers.auth_router import router as auth_router
from routers.admin_router import router as admin_router
from routers.user_router import router as user_router
from routers.jobs_router import router as jobs_router
from routers.icloud_router import router as icloud_router
from schemas import (
    Job,
    JobStatus,
    Source,
    Stage,
    CLEANUP_HOUR_KEY,
    DEFAULT_CLEANUP_HOUR,
    DEFAULT_MAX_TAKEOUT_BYTES,
    DEFAULT_STAGING_RETENTION_DAYS,
    DEFAULT_WORKER_PCT,
    MAX_TAKEOUT_BYTES_KEY,
    STAGING_RETENTION_DAYS_KEY,
    SYNC_SCHEDULER_TICK_SECONDS,
    job_key,
    queue_key,
    sync_lock_key,
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
                    live = json.loads(raw_job)
                    # Date-filtered jobs (re-runnable) and the anchor of a periodic
                    # sync are preserved past the retention window.
                    if live.get("date_filter") or live.get("is_sync_anchor"):
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


async def _run_sync_scheduler(redis) -> None:
    """Enqueue an ICLOUD_DIRECT job for every connection whose sync is enabled and due.

    This is the whole 'periodic sync' mechanism: recurrence lives here in the control
    plane; the Fetcher does the incremental pull and the Loader still uploads. A
    per-connection Redis lock plus the DB `sync_last_run_at` guard against double-firing.
    """
    now = datetime.now(timezone.utc)
    async with AsyncSession(engine) as session:
        result = await session.execute(
            select(ICloudConnection).where(
                ICloudConnection.sync_enabled.is_(True),
                ICloudConnection.status == "active",
            )
        )
        connections = result.scalars().all()

        for conn in connections:
            interval = timedelta(minutes=conn.sync_interval_minutes)
            due = conn.sync_last_run_at is None or (now - conn.sync_last_run_at) >= interval
            if not due or not conn.sync_credential_id:
                continue

            # Reserve this connection for the tick window so we never double-enqueue.
            got_lock = await redis.set(
                sync_lock_key(str(conn.id)), "1", nx=True, ex=SYNC_SCHEDULER_TICK_SECONDS * 2
            )
            if not got_lock:
                continue

            dest = await build_destination(
                session, conn.user_id, conn.sync_credential_kind, conn.sync_credential_id
            )
            if dest is None:
                logger.warning("Sync for connection %s skipped: target destination missing", conn.id)
                continue

            job = Job(
                user_id=str(conn.user_id),
                source=Source.ICLOUD_DIRECT,
                stage=Stage.FETCH,
                status=JobStatus.QUEUED,
                takeout_url=f"icloud://{conn.id}",
                target=dest,
                icloud_connection_ref=str(conn.id),
            )
            record = JobRecord(
                job_id=job.id,
                user_id=conn.user_id,
                stage=job.stage.value,
                status=job.status.value,
                takeout_url=job.takeout_url,
            )
            session.add(record)
            conn.sync_last_run_at = now

            await redis.set(job_key(job.id), job.model_dump_json())
            await redis.zadd(user_jobs_key(str(conn.user_id)), {job.id: now.timestamp()})
            await redis.lpush(queue_key(Stage.FETCH), job.model_dump_json())
            logger.info("Periodic sync fired for connection %s → job %s", conn.id, job.id)

        await session.commit()


async def _sync_scheduler_loop(redis) -> None:
    while True:
        try:
            await _run_sync_scheduler(redis)
        except Exception:
            logger.exception("Sync scheduler tick error")
        await asyncio.sleep(SYNC_SCHEDULER_TICK_SECONDS)


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
    sync_task = asyncio.create_task(_sync_scheduler_loop(redis))

    yield

    for task in (cleanup_task, sync_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Photoswitch", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(admin_router, prefix="/api/admin", tags=["admin"])
app.include_router(user_router, prefix="/api/user", tags=["user"])
app.include_router(jobs_router, prefix="/api/jobs", tags=["jobs"])
app.include_router(icloud_router, prefix="/api/icloud", tags=["icloud"])
