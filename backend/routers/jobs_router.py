import json
import os
import re
import shutil
import time
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from dependencies import get_db, get_redis, get_current_user
from destinations import resolve_destination
from models import User, ICloudConnection, JobRecord
from schemas import (
    DateFilter, Job, JobStatus, Source, Stage,
    queue_key, job_key, user_jobs_key, sync_lock_key,
    MAX_TAKEOUT_BYTES_KEY, DEFAULT_MAX_TAKEOUT_BYTES,
)


router = APIRouter()

STAGING_ROOT = os.environ.get("STAGING_ROOT", "/staging")


class CreateJobRequest(BaseModel):
    takeout_url: str
    credential_id: str
    destination_kind: str = "immich"
    auto_ingest: bool = True
    after_date: Optional[date] = None
    before_date: Optional[date] = None

    @model_validator(mode="after")
    def check_date_order(self) -> "CreateJobRequest":
        if self.after_date and self.before_date and self.after_date > self.before_date:
            raise ValueError("after_date must be on or before before_date")
        return self


def _merge_job(record: JobRecord, live: dict | None) -> dict:
    out = {
        "job_id": record.job_id,
        "stage": record.stage,
        "status": record.status,
        "takeout_url": record.takeout_url,
        "total_items": record.total_items,
        "processed_items": record.processed_items,
        "error": record.error,
        "warnings": None,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "date_filter": None,
        "auto_ingest": True,
        "source": Source.GOOGLE_TAKEOUT.value,
        "destination_kind": "immich",
        "is_sync_anchor": False,
    }
    if live:
        out["stage"] = live.get("stage", out["stage"])
        out["status"] = live.get("status", out["status"])
        if live.get("total_items") is not None:
            out["total_items"] = live["total_items"]
        out["processed_items"] = live.get("processed_items", out["processed_items"])
        out["error"] = live.get("error") or out["error"]
        out["warnings"] = live.get("warnings")
        out["date_filter"] = live.get("date_filter")
        out["auto_ingest"] = live.get("auto_ingest", True)
        out["source"] = live.get("source", out["source"])
        out["destination_kind"] = (live.get("target") or {}).get("kind", out["destination_kind"])
        out["is_sync_anchor"] = bool(live.get("is_sync_anchor", False))
    return out


@router.post("/", status_code=201)
async def create_job(
    body: CreateJobRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    dest = await resolve_destination(db, user, body.destination_kind, body.credential_id)

    date_filter: Optional[DateFilter] = None
    if body.after_date or body.before_date:
        date_filter = DateFilter(after_date=body.after_date, before_date=body.before_date)

    job = Job(
        user_id=str(user.id),
        takeout_url=body.takeout_url,
        target=dest,
        auto_ingest=body.auto_ingest,
        date_filter=date_filter,
    )
    job.staging_dir = os.path.join(STAGING_ROOT, job.id)

    record = JobRecord(
        job_id=job.id,
        user_id=user.id,
        stage=job.stage.value,
        status=job.status.value,
        takeout_url=job.takeout_url,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    await redis.set(job_key(job.id), job.model_dump_json())
    await redis.zadd(user_jobs_key(str(user.id)), {job.id: time.time()})
    await redis.lpush(queue_key(Stage.FETCH), job.model_dump_json())

    return _merge_job(record, None)


class CreateUploadSessionRequest(BaseModel):
    filename: str
    credential_id: str
    destination_kind: str = "immich"
    auto_ingest: bool = True
    after_date: Optional[str] = None
    before_date: Optional[str] = None
    # "google_takeout" (default) or "icloud_bundle" for an Apple Data & Privacy export.
    source: str = Source.GOOGLE_TAKEOUT.value


@router.post("/upload/session", status_code=201)
async def create_upload_session(
    body: CreateUploadSessionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    # Validates that the destination exists and is owned by the user.
    await resolve_destination(db, user, body.destination_kind, body.credential_id)

    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', body.filename)[:200] or "archive.zip"
    session_id = uuid.uuid4().hex
    job_id = uuid.uuid4().hex
    staging_dir = os.path.join(STAGING_ROOT, job_id)
    dest_path = os.path.join(staging_dir, safe_name)

    # Staging dir and file are created lazily on first chunk so that abandoned
    # sessions leave no disk footprint.

    session_data = {
        "user_id": str(user.id),
        "job_id": job_id,
        "credential_id": body.credential_id,
        "destination_kind": body.destination_kind,
        "auto_ingest": body.auto_ingest,
        "after_date": body.after_date,
        "before_date": body.before_date,
        "filename": safe_name,
        "staging_dir": staging_dir,
        "dest_path": dest_path,
        "bytes_received": 0,
        "source": body.source,
    }

    await redis.set(f"psw:upload_session:{session_id}", json.dumps(session_data), ex=86400)
    return {"session_id": session_id}


@router.put("/upload/session/{session_id}", status_code=204)
async def upload_chunk(
    session_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    redis: aioredis.Redis = Depends(get_redis),
):
    raw = await redis.get(f"psw:upload_session:{session_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Upload session not found or expired")
    session_data = json.loads(raw)
    if session_data["user_id"] != str(user.id):
        raise HTTPException(status_code=403, detail="Not your upload session")

    raw_limit = await redis.get(MAX_TAKEOUT_BYTES_KEY)
    max_bytes = int(raw_limit) if raw_limit else DEFAULT_MAX_TAKEOUT_BYTES

    dest_path = session_data["dest_path"]
    bytes_received = session_data["bytes_received"]

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    try:
        with open(dest_path, "ab") as f:
            async for chunk in request.stream():
                f.write(chunk)
                bytes_received += len(chunk)
                if bytes_received > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds the configured limit of {max_bytes / 1e9:.1f} GB",
                    )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Chunk write failed: {exc}")

    session_data["bytes_received"] = bytes_received
    await redis.set(f"psw:upload_session:{session_id}", json.dumps(session_data), ex=86400)


@router.post("/upload/session/{session_id}/complete", status_code=201)
async def complete_upload_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    raw = await redis.get(f"psw:upload_session:{session_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Upload session not found or expired")
    session_data = json.loads(raw)
    if session_data["user_id"] != str(user.id):
        raise HTTPException(status_code=403, detail="Not your upload session")

    job_id = session_data["job_id"]
    staging_dir = session_data["staging_dir"]
    dest_path = session_data["dest_path"]
    safe_name = session_data["filename"]
    auto_ingest = session_data.get("auto_ingest", True)

    date_filter: Optional[DateFilter] = None
    try:
        af = date.fromisoformat(session_data["after_date"]) if session_data.get("after_date") else None
        bf = date.fromisoformat(session_data["before_date"]) if session_data.get("before_date") else None
        if af or bf:
            date_filter = DateFilter(after_date=af, before_date=bf)
    except ValueError:
        pass

    dest = await resolve_destination(
        db, user, session_data.get("destination_kind", "immich"), session_data.get("credential_id", ""),
    )

    try:
        source = Source(session_data.get("source", Source.GOOGLE_TAKEOUT.value))
    except ValueError:
        source = Source.GOOGLE_TAKEOUT
    # Only archive-based sources can be uploaded; a direct iCloud pull isn't an upload.
    if source == Source.ICLOUD_DIRECT:
        source = Source.GOOGLE_TAKEOUT

    job = Job(
        id=job_id,
        user_id=session_data["user_id"],
        source=source,
        takeout_url=f"upload://{safe_name}",
        target=dest,
        auto_ingest=auto_ingest,
        date_filter=date_filter,
        staging_dir=staging_dir,
        archive_path=dest_path,
    )

    if auto_ingest:
        job.stage = Stage.UNPACK
        job.status = JobStatus.QUEUED
    else:
        job.stage = Stage.FETCH
        job.status = JobStatus.SUCCEEDED

    record = JobRecord(
        job_id=job.id,
        user_id=uuid.UUID(session_data["user_id"]),
        stage=job.stage.value,
        status=job.status.value,
        takeout_url=job.takeout_url,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    await redis.set(job_key(job.id), job.model_dump_json())
    await redis.zadd(user_jobs_key(session_data["user_id"]), {job.id: time.time()})
    if auto_ingest:
        await redis.lpush(queue_key(Stage.UNPACK), job.model_dump_json())

    await redis.delete(f"psw:upload_session:{session_id}")

    live = json.loads(job.model_dump_json())
    return _merge_job(record, live)


@router.get("/")
async def list_jobs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    result = await db.execute(
        select(JobRecord)
        .where(JobRecord.user_id == user.id)
        .order_by(JobRecord.created_at.desc())
    )
    records = result.scalars().all()

    out = []
    for record in records:
        raw = await redis.get(job_key(record.job_id))
        live = json.loads(raw) if raw else None
        out.append(_merge_job(record, live))
    return out


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    result = await db.execute(
        select(JobRecord).where(
            JobRecord.job_id == job_id,
            JobRecord.user_id == user.id,
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    raw = await redis.get(job_key(job_id))
    live = json.loads(raw) if raw else None
    return _merge_job(record, live)


@router.delete("/{job_id}", status_code=204)
async def delete_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    result = await db.execute(
        select(JobRecord).where(
            JobRecord.job_id == job_id,
            JobRecord.user_id == user.id,
        )
    )
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
    await redis.zrem(user_jobs_key(str(user.id)), job_id)

    if os.path.isdir(staging_dir):
        shutil.rmtree(staging_dir, ignore_errors=True)

    # If this job anchors a recurring iCloud sync, deleting it tears the sync down
    # cleanly: disable the schedule, drop the anchor pointer, and clear the lock so
    # the scheduler can't fire for it again.
    conn_result = await db.execute(
        select(ICloudConnection).where(
            ICloudConnection.anchor_job_id == job_id,
            ICloudConnection.user_id == user.id,
        )
    )
    anchored = conn_result.scalar_one_or_none()
    if anchored:
        anchored.sync_enabled = False
        anchored.anchor_job_id = None
        await redis.delete(sync_lock_key(str(anchored.id)))

    await db.delete(record)
    await db.commit()


@router.post("/{job_id}/resume")
async def resume_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    result = await db.execute(
        select(JobRecord).where(
            JobRecord.job_id == job_id,
            JobRecord.user_id == user.id,
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    raw = await redis.get(job_key(job_id))
    if not raw:
        raise HTTPException(status_code=409, detail="Job state is no longer cached; cannot resume")

    job = Job.model_validate_json(raw)

    if job.stage == Stage.ROLLBACK and job.status == JobStatus.FAILED:
        # Re-queue a failed rollback for another attempt
        job.status = JobStatus.QUEUED
        job.attempts = 0
        job.error = None
        job.processed_items = 0
        job.total_items = None
        job.touch()
        await redis.set(job_key(job.id), job.model_dump_json())
        await redis.lpush(queue_key(Stage.ROLLBACK), job.model_dump_json())
    elif job.status == JobStatus.SUCCEEDED and job.stage == Stage.FETCH:
        # Download complete — advance to unpack and enqueue.
        job.advance()
        job.attempts = 0
        job.touch()
        await redis.set(job_key(job.id), job.model_dump_json())
        await redis.lpush(queue_key(job.stage), job.model_dump_json())
    elif job.status == JobStatus.FAILED:
        is_upload = job.takeout_url.startswith("upload://")
        if is_upload:
            # Uploaded files can't be re-fetched. Restart from unpack if the
            # archive is still on disk; otherwise the user must re-upload.
            if not job.archive_path or not os.path.isfile(job.archive_path):
                raise HTTPException(
                    status_code=409,
                    detail="The uploaded archive is no longer in staging — please upload the file again.",
                )
            job.stage = Stage.UNPACK
            job.status = JobStatus.QUEUED
            job.attempts = 0
            job.error = None
            job.extracted_dir = None
            job.processed_items = 0
            job.total_items = None
            job.touch()
            await redis.set(job_key(job.id), job.model_dump_json())
            await redis.lpush(queue_key(Stage.UNPACK), job.model_dump_json())
        else:
            # URL-based job: wipe staging and restart from fetch.
            staging_dir = job.staging_dir or os.path.join(STAGING_ROOT, job.id)
            if os.path.isdir(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)

            job.stage = Stage.FETCH
            job.status = JobStatus.QUEUED
            job.attempts = 0
            job.error = None
            job.archive_path = None
            job.extracted_dir = None
            job.processed_items = 0
            job.total_items = None
            job.touch()
            await redis.set(job_key(job.id), job.model_dump_json())
            await redis.lpush(queue_key(Stage.FETCH), job.model_dump_json())
    else:
        raise HTTPException(
            status_code=409,
            detail=f"Job is {job.status.value}/{job.stage.value} — nothing to resume",
        )

    record.stage = job.stage.value
    record.status = job.status.value
    record.error = None
    await db.commit()
    await db.refresh(record)

    fresh_raw = await redis.get(job_key(job.id))
    live = json.loads(fresh_raw) if fresh_raw else None
    return _merge_job(record, live)


class RerunRequest(BaseModel):
    after_date: Optional[date] = None
    before_date: Optional[date] = None
    include_undated: bool = True

    @model_validator(mode="after")
    def check_date_order(self) -> "RerunRequest":
        if self.after_date and self.before_date and self.after_date > self.before_date:
            raise ValueError("after_date must be on or before before_date")
        return self


@router.post("/{job_id}/rerun")
async def rerun_job(
    job_id: str,
    body: RerunRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    result = await db.execute(
        select(JobRecord).where(
            JobRecord.job_id == job_id,
            JobRecord.user_id == user.id,
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    raw = await redis.get(job_key(job_id))
    if not raw:
        raise HTTPException(status_code=409, detail="Job state is no longer cached; cannot re-run")

    job = Job.model_validate_json(raw)

    if job.stage != Stage.LOAD or job.status != JobStatus.SUCCEEDED:
        raise HTTPException(
            status_code=409,
            detail=f"Re-run is only available for completed load jobs (job is {job.status.value}/{job.stage.value})",
        )

    if not job.date_filter:
        raise HTTPException(
            status_code=409,
            detail="This job has no date filter — only date-filtered load jobs can be re-run with a new filter; use Resume to retry a failed job",
        )

    staging_dir = job.staging_dir or os.path.join(STAGING_ROOT, job.id)
    if not os.path.isfile(os.path.join(staging_dir, "mapped_assets.json")):
        raise HTTPException(
            status_code=409,
            detail="Mapped assets for this job are no longer on disk — the job needs to restart from the beginning",
        )

    date_filter: Optional[DateFilter] = None
    if body.after_date or body.before_date:
        date_filter = DateFilter(
            after_date=body.after_date,
            before_date=body.before_date,
            include_undated=body.include_undated,
        )
    job.date_filter = date_filter

    job.stage = Stage.LOAD
    job.status = JobStatus.QUEUED
    job.attempts = 0
    job.error = None
    job.processed_items = 0
    job.total_items = None
    job.touch()

    await redis.set(job_key(job.id), job.model_dump_json())
    await redis.lpush(queue_key(Stage.LOAD), job.model_dump_json())

    record.stage = job.stage.value
    record.status = job.status.value
    record.error = None
    await db.commit()
    await db.refresh(record)

    fresh_raw = await redis.get(job_key(job.id))
    live = json.loads(fresh_raw) if fresh_raw else None
    return _merge_job(record, live)


class RollbackRequest(BaseModel):
    after_date: Optional[date] = None
    before_date: Optional[date] = None
    include_undated: bool = True

    @model_validator(mode="after")
    def check_date_order(self) -> "RollbackRequest":
        if self.after_date and self.before_date and self.after_date > self.before_date:
            raise ValueError("after_date must be on or before before_date")
        return self


@router.post("/{job_id}/rollback", status_code=201)
async def rollback_job(
    job_id: str,
    body: RollbackRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    result = await db.execute(
        select(JobRecord).where(
            JobRecord.job_id == job_id,
            JobRecord.user_id == user.id,
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    raw = await redis.get(job_key(job_id))
    if not raw:
        raise HTTPException(status_code=409, detail="Job state is no longer cached; cannot roll back")

    source_job = Job.model_validate_json(raw)
    if source_job.stage != Stage.LOAD or source_job.status != JobStatus.SUCCEEDED:
        raise HTTPException(
            status_code=409,
            detail="Rollback is only available for completed load jobs",
        )

    staging_dir = source_job.staging_dir or os.path.join(STAGING_ROOT, job_id)
    if not os.path.isfile(os.path.join(staging_dir, "mapped_assets.json")):
        raise HTTPException(
            status_code=409,
            detail=(
                "Staged mapping file for this job has been cleaned up. "
                "Rollback requires mapped_assets.json to be present on disk."
            ),
        )

    date_filter: Optional[DateFilter] = None
    if body.after_date or body.before_date:
        date_filter = DateFilter(after_date=body.after_date, before_date=body.before_date)

    rollback = Job(
        user_id=str(user.id),
        takeout_url=f"rollback://{job_id}",
        target=source_job.target,
        auto_ingest=True,
        stage=Stage.ROLLBACK,
        date_filter=date_filter,
        rollback_source_job_id=job_id,
        rollback_undated=body.include_undated,
    )

    rb_record = JobRecord(
        job_id=rollback.id,
        user_id=user.id,
        stage=rollback.stage.value,
        status=rollback.status.value,
        takeout_url=rollback.takeout_url,
    )
    db.add(rb_record)
    await db.commit()
    await db.refresh(rb_record)

    await redis.set(job_key(rollback.id), rollback.model_dump_json())
    await redis.zadd(user_jobs_key(str(user.id)), {rollback.id: time.time()})
    await redis.lpush(queue_key(Stage.ROLLBACK), rollback.model_dump_json())

    live = json.loads(rollback.model_dump_json())
    return _merge_job(rb_record, live)
