"""
Photoswitch — shared iCloud client.

Like `schemas.py`, this module is copied into BOTH the backend and worker images
(see the two Dockerfiles). It is the single place that touches the unofficial
iCloud API, so the auth flow (backend) and the incremental pull (Fetcher worker)
speak to iCloud through one contract.

Design borrows from icloudpd (icloud-photos-downloader):
  * a persistent per-account cookie/session directory so a trusted session is
    reused across runs and 2FA is only needed on first connect / after expiry
    (Apple expires trust ~every 2 months);
  * originals downloaded per asset, with Live Photos yielding a separate video
    file (mapped onto MappedAsset.is_live_photo / live_video_path downstream);
  * incremental pull via a stored watermark (max asset timestamp seen), the
    analogue of icloudpd's --recent / --until-found.

The heavy dependency (`pyicloud_ipd`, icloudpd's maintained fork of pyicloud) is
imported lazily so this module stays importable — and unit-testable for the pure
helpers — in environments where it isn't installed.

NOTE: the exact PyiCloudService surface (attribute vs. method names, photo
iteration direction) varies across pyicloud_ipd releases. The version-sensitive
spots are marked `# VERIFY:` — they are the only things to reconcile against the
pinned version once a real Apple account is available to test with.
"""

from __future__ import annotations

import io
import logging
import os
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# Distroless / non-root safety: pyicloud depends on `keyring`, which by default
# probes for an OS secret store and can write under $HOME. We always pass the
# password explicitly and persist the session via an explicit cookie directory,
# so neutralise keyring (null backend) and give it a writable HOME fallback. This
# keeps the client working under the Chainguard backend image (distroless) and any
# future non-root worker image, where $HOME may be unset or read-only.
os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
if not os.environ.get("HOME"):
    os.environ["HOME"] = os.environ.get("STAGING_ROOT", "/tmp")


class ICloudAuthError(RuntimeError):
    """Raised when authentication fails or the stored session is no longer trusted."""


class ICloud2FARequired(Exception):
    """Signals that a 2FA code must be supplied to finish authentication.

    Carries the in-progress service so the caller (backend) can hold it across the
    two-step connect flow: start_authentication() → user enters code → complete_2fa().
    """

    def __init__(self, service: "object") -> None:
        super().__init__("Two-factor authentication code required")
        self.service = service


# ---------------------------------------------------------------------------
# Session (cookie directory) serialization
# ---------------------------------------------------------------------------
# pyicloud_ipd persists cookies + a session token as files inside a cookie
# directory, keyed by username. We tar that directory into an opaque blob so the
# backend can encrypt it at rest (Fernet, same key as Immich creds) and the worker
# can rehydrate it before a pull — no 2FA needed while the session stays trusted.


def serialize_session(cookie_dir: str) -> bytes:
    """Pack a cookie directory into a gzipped tar blob for encrypted storage."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in sorted(os.listdir(cookie_dir)):
            tar.add(os.path.join(cookie_dir, name), arcname=name)
    return buf.getvalue()


def restore_session(blob: bytes, cookie_dir: str) -> None:
    """Unpack a session blob (from serialize_session) back into `cookie_dir`."""
    os.makedirs(cookie_dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        # filter="data" confines extraction to cookie_dir (no traversal/symlinks).
        tar.extractall(cookie_dir, filter="data")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _service(apple_id: str, password: str, cookie_dir: str):
    """Construct a PyiCloudService bound to a specific cookie directory.

    Verified against pyicloud 2.6.5: `from pyicloud import PyiCloudService`, with a
    `cookie_directory` constructor param and the requires_2fa/validate_2fa_code/
    trust_session surface used below.
    """
    try:
        from pyicloud import PyiCloudService
    except Exception as exc:  # pragma: no cover - depends on runtime image
        raise ICloudAuthError(
            "pyicloud is not installed in this image — cannot talk to iCloud"
        ) from exc

    os.makedirs(cookie_dir, exist_ok=True)
    return PyiCloudService(apple_id, password, cookie_directory=cookie_dir)


def start_authentication(apple_id: str, password: str, cookie_dir: str):
    """Begin an interactive (backend) auth.

    Returns the authenticated service if the account needs no 2FA or a still-trusted
    session already exists in `cookie_dir`. Otherwise raises ICloud2FARequired,
    carrying the service so complete_2fa() can finish the handshake.
    """
    api = _service(apple_id, password, cookie_dir)
    if getattr(api, "requires_2fa", False) or getattr(api, "requires_2sa", False):
        raise ICloud2FARequired(api)
    return api


def complete_2fa(service, code: str) -> None:
    """Validate a 6-digit 2FA code and persist a trusted session into the cookie dir.

    Mutates `service` in place; on success the cookie directory holds a reusable
    trusted session. Raises ICloudAuthError on a bad code.
    """
    # VERIFY: 2FA uses validate_2fa_code; 2SA (older) uses validate_verification_code.
    if getattr(service, "requires_2fa", False):
        ok = service.validate_2fa_code(code)
    else:
        devices = service.trusted_devices
        device = devices[0] if devices else None
        ok = service.validate_verification_code(device, code)
    if not ok:
        raise ICloudAuthError("The 2FA code was not accepted by Apple")

    # Persist trust so future logins skip 2FA until Apple expires the session.
    if not getattr(service, "is_trusted_session", True):
        service.trust_session()


def request_sms_code(service) -> Optional[str]:
    """Ask Apple to send the 2FA code to the account's trusted phone number by SMS.

    This is the "text me a code instead" fallback for when the user can't retrieve the
    code from a trusted Apple device — the same "Didn't get a verification code?" route
    Apple offers on its web sign-in. pyicloud 2.6.5's `validate_2fa_code()` already
    routes to the SMS verifier once the delivery state is 'sms' (which the SMS request
    sets), so `complete_2fa()` needs no change to accept the texted code.

    Returns a masked phone-number hint (e.g. Apple's obfuscated "•••• 12") when Apple
    provides one, else None. Raises ICloudAuthError if no trusted phone number exists.
    """
    sms = getattr(service, "_request_sms_2fa_code", None)
    try:
        if callable(sms):
            # Explicit SMS path; sets delivery state to 'sms' so validation routes right.
            sms()
        else:
            # Fallback: trigger whatever delivery route Apple has active.
            service.request_2fa_code()
    except Exception as exc:  # pyicloud raises PyiCloudNoTrustedNumberAvailable, etc.
        raise ICloudAuthError(
            f"Apple would not send an SMS code — the account may have no trusted "
            f"phone number, or SMS delivery is unavailable ({exc})."
        ) from exc
    return _masked_trusted_phone(service)


def _masked_trusted_phone(service) -> Optional[str]:
    """Best-effort masked trusted-phone string pulled from the in-flight auth data."""
    try:
        raw = getattr(service, "_auth_data", {}) or {}
        tp = raw.get("trustedPhoneNumber")
        if isinstance(tp, dict):
            for key in ("numberWithDialCode", "obfuscatedNumber", "number", "lastTwoDigits"):
                val = tp.get(key)
                if val:
                    return str(val)
    except Exception:
        pass
    return None


def open_session(apple_id: str, password: str, cookie_dir: str):
    """Worker-side: open a service from an already-trusted cookie dir.

    Raises ICloudAuthError (not ICloud2FARequired) if the session has expired —
    the backend must re-run the interactive flow and store a fresh session.
    """
    api = _service(apple_id, password, cookie_dir)
    if getattr(api, "requires_2fa", False) or getattr(api, "requires_2sa", False):
        raise ICloudAuthError(
            "Stored iCloud session is no longer trusted — re-authentication required"
        )
    return api


# ---------------------------------------------------------------------------
# Incremental photo pull
# ---------------------------------------------------------------------------


@dataclass
class PulledAsset:
    """One downloaded asset + the metadata the Mapper needs to build a MappedAsset."""
    file_path: str
    filename: str
    taken_at: Optional[datetime]
    is_live_photo: bool = False
    live_video_path: Optional[str] = None
    albums: list[str] = field(default_factory=list)

    def taken_at_iso(self) -> Optional[str]:
        return self.taken_at.isoformat() if self.taken_at else None


@dataclass
class PullResult:
    assets: list[PulledAsset]
    # Max asset timestamp (ms since epoch) seen this run — the next sync's watermark.
    new_watermark_ms: Optional[int]
    total_seen: int


def _asset_ms(photo) -> Optional[int]:
    """Milliseconds-since-epoch for a PhotoAsset.

    pyicloud 2.6.5 exposes `asset_date` and `created` as datetimes; older forks used
    a millisecond int. Handle both.
    """
    for attr in ("asset_date", "created"):
        val = getattr(photo, attr, None)
        if isinstance(val, datetime):
            return int(val.timestamp() * 1000)
        if isinstance(val, (int, float)):
            return int(val)
    return None


def _iter_photos_newest_first(api) -> Iterator[object]:
    """Yield PhotoAssets newest-first so a watermark scan can stop early.

    pyicloud's `all` album iterates oldest-first. Version 2.6.5 has a private
    `_iter_added_desc_photos` (added-descending) we prefer for efficiency; otherwise
    we buffer and reverse (correct, but reads all metadata on a first full sync).
    """
    album = api.photos.all
    desc = getattr(album, "_iter_added_desc_photos", None)
    if callable(desc):
        try:
            yield from desc()
            return
        except Exception:
            logger.warning("added-descending iteration failed; falling back to reverse buffer")
    yield from reversed(list(album))


def pull_new_photos(
    api,
    dest_dir: str,
    since_ms: Optional[int] = None,
    max_items: Optional[int] = None,
    progress_cb=None,
) -> PullResult:
    """Download originals newer than `since_ms` into `dest_dir`.

    Iterates newest-first and stops at the first asset at/older than the watermark
    (icloudpd's --until-found idea). Live Photos download the still + its paired
    video as separate files. Immich checksum-dedup on the Loader is the backstop if
    the watermark ever lets a duplicate through.
    """
    os.makedirs(dest_dir, exist_ok=True)
    assets: list[PulledAsset] = []
    new_watermark = since_ms
    seen = 0

    for photo in _iter_photos_newest_first(api):
        ms = _asset_ms(photo)
        if since_ms is not None and ms is not None and ms <= since_ms:
            # Newest-first: everything past here is already synced.
            break

        seen += 1
        if new_watermark is None or (ms is not None and ms > new_watermark):
            new_watermark = ms

        try:
            pulled = _download_asset(photo, dest_dir)
        except Exception as exc:
            logger.warning("Failed to download iCloud asset %s: %s",
                           getattr(photo, "id", "?"), exc)
            continue
        if pulled:
            assets.append(pulled)

        if progress_cb:
            progress_cb(len(assets))
        if max_items is not None and len(assets) >= max_items:
            break

    return PullResult(assets=assets, new_watermark_ms=new_watermark, total_seen=seen)


def _write_download(result, dest_path: str) -> bool:
    """Persist a PhotoAsset.download() result to disk.

    pyicloud 2.6.5's `download()` returns `bytes | None`. Older/other versions return
    a streamed requests.Response — handle both so we're resilient to version drift.
    """
    if result is None:
        return False
    if isinstance(result, (bytes, bytearray)):
        with open(dest_path, "wb") as f:
            f.write(result)
        return True
    # requests.Response-like: stream it.
    raw_iter = getattr(result, "iter_content", None)
    if callable(raw_iter):
        with open(dest_path, "wb") as f:
            for chunk in result.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        return True
    return False


def _download_asset(photo, dest_dir: str) -> Optional[PulledAsset]:
    """Download the original of one PhotoAsset (+ Live Photo video) to `dest_dir`."""
    filename = _safe_name(getattr(photo, "filename", None) or f"{getattr(photo, 'id', 'asset')}.bin")
    dest_path = os.path.join(dest_dir, filename)

    if not _write_download(photo.download("original"), dest_path):
        return None

    ms = _asset_ms(photo)
    taken_at = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms is not None else None

    pulled = PulledAsset(
        file_path=dest_path,
        filename=filename,
        taken_at=taken_at,
        albums=list(getattr(photo, "albums", []) or []),
    )

    # Live Photo: pyicloud exposes a native `is_live_photo` flag. The paired video
    # component is a separate version download; version-key names vary across pyicloud
    # releases, so probe the asset's own version map for a video entry (best-effort).
    if getattr(photo, "is_live_photo", False):
        versions = getattr(photo, "versions", {}) or {}
        keys = versions.keys() if hasattr(versions, "keys") else versions
        live_key = next((k for k in keys if "vid" in str(k).lower()), None)
        if live_key:
            try:
                vid_path = os.path.join(dest_dir, _safe_name(os.path.splitext(filename)[0] + ".MOV"))
                if _write_download(photo.download(live_key), vid_path):
                    pulled.is_live_photo = True
                    pulled.live_video_path = vid_path
            except Exception as exc:
                logger.warning("Live Photo video download failed for %s: %s", filename, exc)

    return pulled


def _safe_name(name: str) -> str:
    import re
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:200] or "asset.bin"
