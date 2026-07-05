"""
iCloud connection + import endpoints.

Two-step connect (start → verify) because iCloud needs an interactive 2FA code:
  POST /connections            → create row, begin auth; may return status=2fa_required
  POST /connections/{id}/verify → submit the 6-digit code, persist a trusted session

Once a connection has a trusted session, the user can:
  POST /connections/{id}/import → run a direct pull now (optionally the sync anchor)
  PUT  /connections/{id}/sync   → enable/configure the periodic sync (scheduler fires it)

The blocking pyicloud calls run in a thread pool so they don't stall the event loop.
Because the backend is single-instance, the in-progress auth service is held in a
module dict between the start and verify requests.
"""

import asyncio
import os
import shutil
import time
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

import icloud_client as ic
from crypto import encrypt, decrypt
from dependencies import get_db, get_redis, get_current_user
from destinations import resolve_destination, build_destination
from models import User, ICloudConnection, JobRecord
from datetime import datetime, timezone

from schemas import (
    DateFilter, Destination, Job, JobStatus, Source, Stage,
    queue_key, job_key, user_jobs_key, sync_lock_key,
    SYNC_MIN_INTERVAL_MINUTES, DEFAULT_SYNC_INTERVAL_MINUTES, SYNC_INTERVAL_PRESETS,
)

router = APIRouter()

STAGING_ROOT = os.environ.get("STAGING_ROOT", "/staging")

# In-progress auth services awaiting a 2FA code, keyed by connection id.
# Single-instance backend, so an in-memory hold is sufficient.
_PENDING_AUTH: dict[str, object] = {}


def _auth_cookie_dir(connection_id: str) -> str:
    return os.path.join(STAGING_ROOT, ".icloud_auth", connection_id)


async def _get_connection(db: AsyncSession, user: User, connection_id: str) -> ICloudConnection:
    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Connection not found")
    result = await db.execute(
        select(ICloudConnection).where(
            ICloudConnection.id == cid,
            ICloudConnection.user_id == user.id,
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


def _conn_out(conn: ICloudConnection) -> dict:
    return {
        "id": str(conn.id),
        "apple_id": conn.apple_id,
        "label": conn.label,
        "status": conn.status,
        "sync_enabled": conn.sync_enabled,
        "sync_interval_minutes": conn.sync_interval_minutes,
        "sync_credential_id": str(conn.sync_credential_id) if conn.sync_credential_id else None,
        "sync_credential_kind": conn.sync_credential_kind,
        "sync_last_run_at": conn.sync_last_run_at.isoformat() if conn.sync_last_run_at else None,
        "anchor_job_id": conn.anchor_job_id,
        "has_watermark": conn.watermark_ms is not None,
        "created_at": conn.created_at.isoformat(),
    }


class CreateConnectionRequest(BaseModel):
    apple_id: str
    password: str
    label: Optional[str] = None


@router.post("/connections", status_code=201)
async def create_connection(
    body: CreateConnectionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conn = ICloudConnection(
        user_id=user.id,
        apple_id=body.apple_id,
        label=body.label,
        encrypted_password=encrypt(body.password),
        status="pending_2fa",
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)

    cookie_dir = _auth_cookie_dir(str(conn.id))
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, _start_auth_blocking, body.apple_id, body.password, cookie_dir
        )
    except ic.ICloudAuthError as exc:
        await db.delete(conn)
        await db.commit()
        raise HTTPException(status_code=400, detail=str(exc))

    if result == "2fa_required":
        return {**_conn_out(conn), "status": "2fa_required"}

    # No 2FA needed (rare): the session in cookie_dir is already trusted.
    await _persist_session(db, conn, cookie_dir)
    return _conn_out(conn)


def _start_auth_blocking(apple_id: str, password: str, cookie_dir: str) -> str:
    try:
        ic.start_authentication(apple_id, password, cookie_dir)
        return "authenticated"
    except ic.ICloud2FARequired as exc:
        _PENDING_AUTH[cookie_dir] = exc.service
        return "2fa_required"


class VerifyRequest(BaseModel):
    code: str


@router.post("/connections/{connection_id}/verify")
async def verify_connection(
    connection_id: str,
    body: VerifyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conn = await _get_connection(db, user, connection_id)
    cookie_dir = _auth_cookie_dir(connection_id)
    service = _PENDING_AUTH.get(cookie_dir)
    if service is None:
        raise HTTPException(
            status_code=409,
            detail="No pending authentication for this connection — start the connection again",
        )

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, ic.complete_2fa, service, body.code)
    except ic.ICloudAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        _PENDING_AUTH.pop(cookie_dir, None)

    await _persist_session(db, conn, cookie_dir)
    return _conn_out(conn)


async def _persist_session(db: AsyncSession, conn: ICloudConnection, cookie_dir: str) -> None:
    """Pack + encrypt the trusted cookie dir onto the connection row, mark active."""
    blob = ic.serialize_session(cookie_dir)
    conn.encrypted_session = encrypt_bytes(blob)
    conn.status = "active"
    await db.commit()
    await db.refresh(conn)
    shutil.rmtree(cookie_dir, ignore_errors=True)


@router.get("/connections")
async def list_connections(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ICloudConnection)
        .where(ICloudConnection.user_id == user.id)
        .order_by(ICloudConnection.created_at.desc())
    )
    return [_conn_out(c) for c in result.scalars().all()]


def _test_session_blocking(apple_id: str, password: str, session_blob: bytes, cookie_dir: str) -> None:
    """Restore the stored session and open it — raises ic.ICloudAuthError if Apple no
    longer trusts it. Runs in a thread (blocking pyicloud calls)."""
    ic.restore_session(session_blob, cookie_dir)
    ic.open_session(apple_id, password, cookie_dir)


@router.post("/connections/{connection_id}/test")
async def test_connection(
    connection_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify the stored iCloud session still authenticates. On success the connection
    is marked active; on an auth failure it is flipped to needs_reauth so the user is
    prompted to reconnect before a scheduled sync silently fails."""
    conn = await _get_connection(db, user, connection_id)
    if not conn.encrypted_session:
        raise HTTPException(
            status_code=400,
            detail="This connection has no trusted session yet — finish the 2FA connect flow first",
        )

    apple_id = conn.apple_id
    password = decrypt(conn.encrypted_password)
    session_blob = decrypt_bytes(conn.encrypted_session)
    cookie_dir = os.path.join(STAGING_ROOT, ".icloud_test", connection_id)

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None, _test_session_blocking, apple_id, password, session_blob, cookie_dir
        )
    except ic.ICloudAuthError:
        if conn.status != "needs_reauth":
            conn.status = "needs_reauth"
            await db.commit()
        raise HTTPException(
            status_code=502,
            detail="iCloud session is no longer trusted — reconnect to refresh it (a new 2FA code).",
        )
    finally:
        shutil.rmtree(cookie_dir, ignore_errors=True)

    if conn.status != "active":
        conn.status = "active"
        await db.commit()
    return {"ok": True, "user": apple_id}


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    conn = await _get_connection(db, user, connection_id)
    _PENDING_AUTH.pop(_auth_cookie_dir(connection_id), None)
    shutil.rmtree(_auth_cookie_dir(connection_id), ignore_errors=True)
    # Tear down any scheduled sync state so the scheduler can't fire for a gone row.
    await redis.delete(sync_lock_key(connection_id))
    await db.delete(conn)
    await db.commit()


class SyncConfigRequest(BaseModel):
    enabled: bool
    interval_minutes: int = DEFAULT_SYNC_INTERVAL_MINUTES
    credential_id: Optional[str] = None
    destination_kind: str = "immich"

    @model_validator(mode="after")
    def check_interval(self) -> "SyncConfigRequest":
        if self.interval_minutes < SYNC_MIN_INTERVAL_MINUTES:
            raise ValueError(f"interval_minutes must be at least {SYNC_MIN_INTERVAL_MINUTES}")
        if self.interval_minutes not in SYNC_INTERVAL_PRESETS:
            raise ValueError(f"interval_minutes must be one of {SYNC_INTERVAL_PRESETS}")
        return self


@router.put("/connections/{connection_id}/sync")
async def configure_sync(
    connection_id: str,
    body: SyncConfigRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conn = await _get_connection(db, user, connection_id)
    if body.enabled and conn.status != "active":
        raise HTTPException(
            status_code=409,
            detail="Connection must be authenticated before enabling sync",
        )

    credential_id = body.credential_id
    if body.enabled and not credential_id:
        raise HTTPException(status_code=400, detail="credential_id is required to enable sync")

    if credential_id:
        # Validates the destination exists and is owned by the user.
        await resolve_destination(db, user, body.destination_kind, credential_id)
        conn.sync_credential_id = uuid.UUID(credential_id)
        conn.sync_credential_kind = body.destination_kind

    conn.sync_enabled = body.enabled
    conn.sync_interval_minutes = body.interval_minutes
    await db.commit()
    await db.refresh(conn)
    return _conn_out(conn)


class ImportRequest(BaseModel):
    credential_id: str
    destination_kind: str = "immich"
    as_sync_anchor: bool = False
    after_date: Optional[date] = None
    before_date: Optional[date] = None

    @model_validator(mode="after")
    def check_date_order(self) -> "ImportRequest":
        if self.after_date and self.before_date and self.after_date > self.before_date:
            raise ValueError("after_date must be on or before before_date")
        return self


@router.post("/connections/{connection_id}/import", status_code=201)
async def import_now(
    connection_id: str,
    body: ImportRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    conn = await _get_connection(db, user, connection_id)
    if conn.status != "active":
        raise HTTPException(status_code=409, detail="Connection is not authenticated")

    dest = await resolve_destination(db, user, body.destination_kind, body.credential_id)

    date_filter: Optional[DateFilter] = None
    if body.after_date or body.before_date:
        date_filter = DateFilter(after_date=body.after_date, before_date=body.before_date)

    job = _new_icloud_job(
        user_id=str(user.id),
        connection_id=str(conn.id),
        dest=dest,
        date_filter=date_filter,
        is_sync_anchor=body.as_sync_anchor,
    )
    await _enqueue_job(db, redis, user.id, job)

    if body.as_sync_anchor:
        conn.anchor_job_id = job.id
        await db.commit()

    return {"job_id": job.id, "connection_id": str(conn.id)}


@router.post("/connections/{connection_id}/sync-now", status_code=201)
async def sync_now(
    connection_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Trigger the recurring sync immediately (incremental pull from the watermark),
    using the connection's configured sync target. Distinct from /import, which is
    the initial/one-off import that can set the anchor."""
    conn = await _get_connection(db, user, connection_id)
    if conn.status != "active":
        raise HTTPException(status_code=409, detail="Connection is not authenticated")
    if not conn.sync_credential_id:
        raise HTTPException(
            status_code=400,
            detail="No sync target set — choose a destination server under Configure sync first",
        )

    dest = await build_destination(db, user.id, conn.sync_credential_kind, conn.sync_credential_id)
    if dest is None:
        raise HTTPException(status_code=409, detail="The configured sync target no longer exists")
    job = _new_icloud_job(
        user_id=str(user.id),
        connection_id=str(conn.id),
        dest=dest,
        date_filter=None,
        is_sync_anchor=False,
    )
    await _enqueue_job(db, redis, user.id, job)

    # Count the manual run against the schedule so the next tick doesn't double-fire.
    conn.sync_last_run_at = datetime.now(timezone.utc)
    await db.commit()
    return {"job_id": job.id, "connection_id": str(conn.id)}


# --- shared helpers reused by the sync scheduler ---------------------------

def _new_icloud_job(
    user_id: str,
    connection_id: str,
    dest: Destination,
    date_filter: Optional[DateFilter],
    is_sync_anchor: bool,
) -> Job:
    return Job(
        user_id=user_id,
        source=Source.ICLOUD_DIRECT,
        stage=Stage.FETCH,
        status=JobStatus.QUEUED,
        takeout_url=f"icloud://{connection_id}",
        target=dest,
        icloud_connection_ref=connection_id,
        is_sync_anchor=is_sync_anchor,
        date_filter=date_filter,
    )


async def _enqueue_job(db: AsyncSession, redis: aioredis.Redis, user_id, job: Job) -> None:
    record = JobRecord(
        job_id=job.id,
        user_id=user_id,
        stage=job.stage.value,
        status=job.status.value,
        takeout_url=job.takeout_url,
    )
    db.add(record)
    await db.commit()

    await redis.set(job_key(job.id), job.model_dump_json())
    await redis.zadd(user_jobs_key(str(user_id)), {job.id: time.time()})
    await redis.lpush(queue_key(Stage.FETCH), job.model_dump_json())


def encrypt_bytes(blob: bytes) -> bytes:
    """Encrypt raw bytes with the app Fernet key (crypto.encrypt is str-only)."""
    from crypto import _make_fernet
    return _make_fernet().encrypt(blob)


def decrypt_bytes(blob: bytes) -> bytes:
    """Decrypt raw bytes (the session tarball) — crypto.decrypt returns str."""
    from crypto import _make_fernet
    return _make_fernet().decrypt(bytes(blob))
