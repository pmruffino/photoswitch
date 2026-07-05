import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Boolean, DateTime, LargeBinary, ForeignKey, Integer, BigInteger, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(256), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    credentials: Mapped[list["ImmichCredential"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    webdav_destinations: Mapped[list["WebDavDestination"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    icloud_connections: Mapped[list["ICloudConnection"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["JobRecord"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ImmichCredential(Base):
    __tablename__ = "immich_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    server_url: Mapped[str] = mapped_column(String(512), nullable=False)
    encrypted_api_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    user: Mapped[User] = relationship(back_populates="credentials")


class WebDavDestination(Base):
    """A WebDAV upload target — Nextcloud, ownCloud, PhotoPrism, or any WebDAV server.

    The password (ideally an app-password) is encrypted at rest with the same Fernet
    key as Immich credentials. `base_path` is the folder under the WebDAV files root
    that album folders (and un-albumed photos) are created beneath.
    """
    __tablename__ = "webdav_destinations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[str] = mapped_column(String(256), nullable=False)
    encrypted_password: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    base_path: Mapped[str] = mapped_column(String(512), default="Photoswitch", nullable=False)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    user: Mapped[User] = relationship(back_populates="webdav_destinations")


class ICloudConnection(Base):
    """A user's iCloud (Apple ID) connection for direct pulls + periodic sync.

    The Apple password and the trusted-session blob (a packed pyicloud cookie
    directory) are both encrypted at rest with the same Fernet key as Immich
    credentials. `status` tracks whether the session is usable; Apple expires
    trust roughly every two months, at which point the sync scheduler flags the
    row `needs_reauth` and the user must re-enter a 2FA code.
    """
    __tablename__ = "icloud_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    apple_id: Mapped[str] = mapped_column(String(256), nullable=False)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    encrypted_password: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Null until a 2FA handshake produces a trusted session.
    encrypted_session: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # pending_2fa → active → needs_reauth (when Apple expires trust).
    status: Mapped[str] = mapped_column(String(24), default="pending_2fa", nullable=False)

    # Incremental-pull cursor: max asset timestamp (ms since epoch) synced so far.
    watermark_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # --- periodic sync config (direct-connection only) ---
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=1440, nullable=False)
    # Destination target used for scheduled runs (the anchor import's target).
    sync_credential_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    sync_credential_kind: Mapped[str] = mapped_column(String(16), default="immich", nullable=False)
    sync_last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The initial import job that anchors the recurring sync (kept out of cleanup).
    anchor_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    user: Mapped[User] = relationship(back_populates="icloud_connections")


class JobRecord(Base):
    __tablename__ = "job_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    takeout_url: Mapped[str] = mapped_column(Text, nullable=False)
    total_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="jobs")
