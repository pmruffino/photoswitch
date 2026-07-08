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
    """The worker types. Each has its own queue + semaphore.

    The order a job visits stages depends on its Source (see PIPELINES) — e.g. an
    iCloud *direct* pull has no archive, so it skips UNPACK. Use `next_stage()` /
    `Job.advance()` rather than assuming a single global order.
    """
    FETCH = "fetch"
    UNPACK = "unpack"
    MAP = "map"
    LOAD = "load"
    ROLLBACK = "rollback"

    @property
    def next(self) -> Optional["Stage"]:
        # Back-compat shim: the default (Google/bundle) pipeline order. Source-aware
        # code should call next_stage(stage, source) instead.
        return next_stage(self, Source.GOOGLE_TAKEOUT)


class Source(str, Enum):
    """Where a job's media originates. Drives the stage pipeline it flows through."""
    GOOGLE_TAKEOUT = "google_takeout"   # Takeout archive (link or upload) → unpack → map → load
    ICLOUD_BUNDLE = "icloud_bundle"     # Apple Data & Privacy export archive (upload) → unpack → map → load
    ICLOUD_DIRECT = "icloud_direct"     # Live iCloud pull via API → (no unpack) → map → load


# Per-source stage pipelines. The Fetcher for ICLOUD_DIRECT writes media straight
# into the extracted dir, so there is nothing to UNPACK — that stage is skipped.
PIPELINES: dict["Source", list[Stage]] = {
    Source.GOOGLE_TAKEOUT: [Stage.FETCH, Stage.UNPACK, Stage.MAP, Stage.LOAD],
    Source.ICLOUD_BUNDLE: [Stage.FETCH, Stage.UNPACK, Stage.MAP, Stage.LOAD],
    Source.ICLOUD_DIRECT: [Stage.FETCH, Stage.MAP, Stage.LOAD],
}


def next_stage(stage: Stage, source: "Source") -> Optional[Stage]:
    """Next stage for `source`'s pipeline, or None if `stage` is terminal/off-pipeline."""
    pipeline = PIPELINES.get(source, PIPELINES[Source.GOOGLE_TAKEOUT])
    try:
        i = pipeline.index(stage)
    except ValueError:
        return None  # stages outside the pipeline (e.g. ROLLBACK) have no next
    return pipeline[i + 1] if i + 1 < len(pipeline) else None


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


# --- Periodic iCloud sync ---------------------------------------------------
# The recurrence is driven by a scheduler loop in the backend control plane (next
# to the cleanup loop). It enqueues a normal ICLOUD_DIRECT job on the FETCH queue
# whenever an iCloud connection's sync is enabled and due. There is NO dedicated
# sync worker — the Fetcher does the incremental pull, the Loader still uploads.

# How often the scheduler wakes to look for due syncs (seconds). Must be <= the
# smallest allowed interval (15 min) so a 15-min sync fires close to on time.
SYNC_SCHEDULER_TICK_SECONDS = 120
# Floor on a user-configured sync interval, to protect Apple's servers and ours.
SYNC_MIN_INTERVAL_MINUTES = 15
DEFAULT_SYNC_INTERVAL_MINUTES = 1440  # daily

# Allowed sync-frequency presets (minutes) surfaced in the UI.
SYNC_INTERVAL_PRESETS = [15, 60, 120, 240, 480, 720, 1440, 2880, 4320, 10080]


def sync_lock_key(connection_id: str) -> str:
    """Short-lived lock so the scheduler never enqueues two runs for one connection."""
    return f"{REDIS_NS}:sync:lock:{connection_id}"


# ---------------------------------------------------------------------------
# Job payloads
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex


class DestinationKind(str, Enum):
    """Which kind of server a job uploads to; selects the Loader/Rollback path."""
    IMMICH = "immich"
    WEBDAV = "webdav"   # Nextcloud, ownCloud, PhotoPrism, or any WebDAV server


class Destination(BaseModel):
    """Where a job uploads to. Secrets are decrypted just-in-time by the Loader from
    the row referenced by `credential_ref`; only the opaque reference is enqueued —
    never a plaintext secret in Redis.

    `kind` selects both the Loader path and which Postgres table `credential_ref`
    points at (`immich_credentials` for immich, `webdav_destinations` for webdav).
    """
    kind: DestinationKind = Field(
        default=DestinationKind.IMMICH,
        description="Destination type; also picks the credentials table for credential_ref",
    )
    server_url: str = Field(..., description="Base URL of the destination server")
    credential_ref: str = Field(
        ..., description="Opaque id of the encrypted credentials row in Postgres"
    )


# Back-compat alias: earlier code and serialized jobs used the name ImmichTarget.
# Fields are unchanged (kind defaults to immich), so existing Redis payloads validate.
ImmichTarget = Destination


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

    source: Source = Field(
        default=Source.GOOGLE_TAKEOUT,
        description="Origin of the media; selects the stage pipeline (see PIPELINES)",
    )
    stage: Stage = Stage.FETCH
    status: JobStatus = JobStatus.QUEUED

    # --- inputs ---
    # For Google jobs this is the public Takeout link; for uploads / iCloud it holds
    # a scheme-tagged synthetic value (upload://, icloud://, rollback://).
    takeout_url: str = Field(..., description="Source locator for the job")
    target: ImmichTarget
    auto_ingest: bool = Field(
        default=True,
        description="If False, the Fetcher parks the job after download instead of advancing to Unpack",
    )
    date_filter: Optional[DateFilter] = Field(
        default=None,
        description="If set, only assets whose taken_at falls within this range are uploaded/removed",
    )

    # --- iCloud-specific ---
    icloud_connection_ref: Optional[str] = Field(
        default=None,
        description="For ICLOUD_DIRECT jobs: id of the encrypted iCloud connection row in Postgres",
    )
    is_sync_anchor: Optional[bool] = Field(
        default=False,
        description="True for the initial import that anchors a recurring sync; exempt from staging cleanup",
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
    # Non-fatal summary of items that were skipped/failed within a succeeded stage
    # (e.g. photos that would not download, or WebDAV PUTs the server rejected). Set so
    # partial data-loss is visible in the UI instead of being silently swallowed.
    warnings: Optional[str] = None
    attempts: int = 0

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    def touch(self) -> None:
        self.updated_at = _now()

    def advance(self) -> bool:
        """Move to the next stage in this job's source pipeline.

        Returns False if this was the final stage (job is then marked succeeded).
        """
        nxt = next_stage(self.stage, self.source)
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
