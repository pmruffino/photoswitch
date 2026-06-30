"""
Photoswitch — shared contracts.

This module is the SINGLE source of truth for the shapes that cross the boundary
between the control plane (FastAPI) and the workers, and for the Redis key/queue
naming convention. Both sides import from here. Change a contract in ONE place.

Nothing in this file should import FastAPI, SQLAlchemy, or any worker-only deps —
it must stay importable by the tiny worker images.

Requires: pydantic v2.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

class Stage(str, Enum):
    """The worker types, in pipeline order. Each has its own queue + semaphore."""
    FETCH = "fetch"
    UNPACK = "unpack"
    MAP = "map"
    LOAD = "load"
    ROLLBACK = "rollback"

    @property
    def next(self) -> Optional["Stage"]:
        order = [Stage.FETCH, Stage.UNPACK, Stage.MAP, Stage.LOAD]
        try:
            i = order.index(self)
        except ValueError:
            return None  # stages outside the main pipeline (e.g. ROLLBACK) have no next
        return order[i + 1] if i + 1 < len(order) else None


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"


class SignupPolicy(str, Enum):
    """Admin-configurable new-user policy."""
    OPEN = "open"          # anyone can register
    APPROVAL = "approval"  # register, but an admin must approve
    CLOSED = "closed"      # only admins create users


# ---------------------------------------------------------------------------
# Redis key / queue contract
# ---------------------------------------------------------------------------
#
# All Redis keys are namespaced under "psw:" (Photoswitch) so the instance can
# share a Redis with other apps if needed. Workers and backend MUST use these
# helpers rather than hand-building key strings.

REDIS_NS = "psw"


def queue_key(stage: Stage) -> str:
    """List used as the work queue for a stage (LPUSH by producer, BRPOP by worker)."""
    return f"{REDIS_NS}:queue:{stage.value}"


def semaphore_limit_key(stage: Stage) -> str:
    """Integer: admin-configured max simultaneous jobs for a stage (live-tunable)."""
    return f"{REDIS_NS}:sem:{stage.value}:limit"


def semaphore_count_key(stage: Stage) -> str:
    """Integer: current number of in-flight jobs holding a slot for a stage."""
    return f"{REDIS_NS}:sem:{stage.value}:count"


def job_key(job_id: str) -> str:
    """Hash holding the serialized Job record (live status, progress, error)."""
    return f"{REDIS_NS}:job:{job_id}"


def user_jobs_key(user_id: str) -> str:
    """Sorted set of a user's job ids, scored by created-at, for dashboard listing."""
    return f"{REDIS_NS}:user:{user_id}:jobs"


def session_key(token: str) -> str:
    return f"{REDIS_NS}:session:{token}"


WORKER_HEARTBEAT_INTERVAL = 15   # seconds between worker heartbeat refreshes
WORKER_HEARTBEAT_TTL = 45        # seconds before a worker is considered gone


def worker_presence_key(stage: Stage) -> str:
    """Sorted set: member=worker_id, score=last_heartbeat_timestamp."""
    return f"{REDIS_NS}:workers:{stage.value}"


def worker_pct_key(stage: Stage) -> str:
    """Integer 1–100: admin-configured percentage of available workers to allow per stage."""
    return f"{REDIS_NS}:config:worker_pct:{stage.value}"


DEFAULT_WORKER_PCT = 100


def calculate_semaphore_limit(worker_count: int, pct: int) -> int:
    """Translate worker count + percentage into a concrete semaphore limit (min 1)."""
    return max(1, round(worker_count * pct / 100))


# Admin-configurable storage / pipeline limits (stored in Redis, live-tunable).
MAX_TAKEOUT_BYTES_KEY = f"{REDIS_NS}:config:max_takeout_bytes"
DEFAULT_MAX_TAKEOUT_BYTES = 15 * 1024 * 1024 * 1024  # 15 GB

STAGING_RETENTION_DAYS_KEY = f"{REDIS_NS}:config:staging_retention_days"
DEFAULT_STAGING_RETENTION_DAYS = 7

CLEANUP_HOUR_KEY = f"{REDIS_NS}:config:cleanup_hour"
DEFAULT_CLEANUP_HOUR = 3  # 3 AM UTC


# ---------------------------------------------------------------------------
# Job payloads
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex


class ImmichTarget(BaseModel):
    """Where a job uploads to. The api_key is decrypted just-in-time by the Loader;
    it is NEVER stored in Redis in plaintext — only an opaque reference is enqueued."""
    server_url: str = Field(..., description="Base URL of the user's Immich server")
    credential_ref: str = Field(
        ..., description="Opaque id of the encrypted Immich API key row in Postgres"
    )


class DateFilter(BaseModel):
    """Optional date-range filter applied by the Loader before each upload.

    Both bounds are inclusive and compared against the asset's taken_at date.

    Typical use-case: you've already synced recent photos from your phone and
    only want the older Google Photos archive. Set before_date to the day before
    your phone sync started to avoid re-uploading duplicates.
    """
    after_date: Optional[date] = Field(
        default=None,
        description="Include only assets taken on or after this date (YYYY-MM-DD, inclusive)",
    )
    before_date: Optional[date] = Field(
        default=None,
        description="Include only assets taken on or before this date (YYYY-MM-DD, inclusive)",
    )
    include_undated: bool = Field(
        default=True,
        description="Whether to include assets with no date/time metadata",
    )

    def includes(self, taken_at: Optional[datetime]) -> bool:
        """Return True if the asset should be uploaded given this filter."""
        if self.after_date is None and self.before_date is None:
            return True
        if taken_at is None:
            return self.include_undated
        asset_date = taken_at.date()
        if self.after_date is not None and asset_date < self.after_date:
            return False
        if self.before_date is not None and asset_date > self.before_date:
            return False
        return True


class Job(BaseModel):
    """A unit of work flowing through the pipeline.

    The same job id travels across all stages; `stage` is updated as it advances.
    The control plane creates the Job, enqueues it on the FETCH queue, and each
    worker re-enqueues it to the next stage's queue on success.
    """
    id: str = Field(default_factory=_new_id)
    user_id: str

    stage: Stage = Stage.FETCH
    status: JobStatus = JobStatus.QUEUED

    # --- inputs ---
    takeout_url: str = Field(..., description="User-provided public link to Takeout")
    target: ImmichTarget
    auto_ingest: bool = Field(
        default=True,
        description="If False, the Fetcher parks the job after download instead of advancing to Unpack",
    )
    date_filter: Optional[DateFilter] = Field(
        default=None,
        description="If set, only assets whose taken_at falls within this range are uploaded/removed",
    )

    # --- rollback-specific ---
    rollback_source_job_id: Optional[str] = Field(
        default=None,
        description="For ROLLBACK jobs: the job_id whose assets should be removed from Immich",
    )
    rollback_undated: bool = Field(
        default=True,
        description="For ROLLBACK jobs: whether to also remove assets that had no date/time",
    )

    # --- staging / handoff between stages ---
    staging_dir: Optional[str] = Field(
        default=None, description="Per-job staging path on the shared volume"
    )
    archive_path: Optional[str] = Field(
        default=None, description="Downloaded archive path (set by Fetcher)"
    )
    extracted_dir: Optional[str] = Field(
        default=None, description="Where Unpacker extracted contents"
    )

    # --- progress / observability ---
    total_items: Optional[int] = None
    processed_items: int = 0
    error: Optional[str] = None
    attempts: int = 0

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    def touch(self) -> None:
        self.updated_at = _now()

    def advance(self) -> bool:
        """Move to the next stage. Returns False if this was the final stage."""
        nxt = self.stage.next
        if nxt is None:
            self.status = JobStatus.SUCCEEDED
            self.touch()
            return False
        self.stage = nxt
        self.status = JobStatus.QUEUED
        self.touch()
        return True

    def fail(self, message: str) -> None:
        self.status = JobStatus.FAILED
        self.error = message
        self.touch()


# ---------------------------------------------------------------------------
# Metadata mapping result (Mapper -> Loader handoff detail)
# ---------------------------------------------------------------------------

class MappedAsset(BaseModel):
    """One media file after the Mapper has reconciled it with its Google sidecar.

    The Mapper writes corrected EXIF/QuickTime tags onto the file itself, but album
    membership and people tags can't live in EXIF, so they travel here for the Loader.
    """
    file_path: str
    checksum_sha1: Optional[str] = Field(
        default=None, description="Used to dedupe against Immich before upload"
    )
    taken_at: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    description: Optional[str] = None
    albums: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    is_live_photo: bool = False
    live_video_path: Optional[str] = Field(
        default=None, description="Paired motion/Live Photo video, if any"
    )
