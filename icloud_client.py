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
import time
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
    # The initial 2FA challenge populates the service's auth data from Apple's HTML auth
    # shell, which is trusted-device oriented and usually omits phone numbers — so a bare
    # _request_sms_2fa_code() raises "no trusted number" even when the account has one.
    # Enrich the auth data with Apple's SMS-oriented (JSON) auth options first.
    _ensure_trusted_phone(service)

    sms = getattr(service, "_request_sms_2fa_code", None)
    try:
        if callable(sms):
            # Explicit SMS path; sets delivery state to 'sms' so validation routes right.
            sms()
        else:
            # Fallback: trigger whatever delivery route Apple has active.
            service.request_2fa_code()
    except Exception as exc:  # pyicloud raises PyiCloudNoTrustedNumberAvailable, etc.
        detail = str(exc) or exc.__class__.__name__
        raise ICloudAuthError(
            f"Apple would not send an SMS code ({detail}). The account may have no trusted "
            f"phone number available for this sign-in, or SMS delivery is unavailable."
        ) from exc
    return _masked_trusted_phone(service)


def _ensure_trusted_phone(service) -> None:
    """Populate `service._auth_data` with the account's trusted phone number(s).

    The 2FA challenge is bootstrapped from Apple's HTML auth shell (`Accept: text/html`),
    which is oriented at the trusted-device bridge and frequently omits phone numbers.
    Re-fetching the same auth endpoint with `Accept: application/json` returns Apple's
    SMS-oriented shape (a `trustedPhoneNumbers` list / `phoneNumberVerification` block);
    we merge those keys in so pyicloud's `_trusted_phone_number()` can find a number.
    Best-effort: on any failure we leave auth data as-is and let the SMS request surface
    a clear error.
    """
    # Already known? Nothing to do.
    try:
        finder = getattr(service, "_trusted_phone_number", None)
        if callable(finder) and finder() is not None:
            return
    except Exception:
        pass

    endpoint = getattr(service, "_auth_endpoint", None)
    get_headers = getattr(service, "_get_auth_headers", None)
    session = getattr(service, "session", None)
    auth_data = getattr(service, "_auth_data", None)
    if not (endpoint and callable(get_headers) and session is not None and isinstance(auth_data, dict)):
        return

    try:
        resp = session.get(endpoint, headers=get_headers({"Accept": "application/json"}))
        data = resp.json() if hasattr(resp, "json") else None
    except Exception:
        logger.debug("Could not fetch SMS auth options", exc_info=True)
        return
    if not isinstance(data, dict):
        return

    # Collect phone info from the top level and from a nested phoneNumberVerification.
    pv = auth_data.get("phoneNumberVerification")
    pv = dict(pv) if isinstance(pv, dict) else {}
    nested = data.get("phoneNumberVerification")
    sources = [data] + ([nested] if isinstance(nested, dict) else [])
    for src in sources:
        for key in ("trustedPhoneNumber", "trustedPhoneNumbers"):
            if src.get(key) is not None and pv.get(key) is None:
                pv[key] = src[key]
    if pv:
        auth_data["phoneNumberVerification"] = pv
    if data.get("trustedPhoneNumber") is not None and auth_data.get("trustedPhoneNumber") is None:
        auth_data["trustedPhoneNumber"] = data["trustedPhoneNumber"]
    if data.get("trustedPhoneNumbers") is not None and auth_data.get("trustedPhoneNumbers") is None:
        auth_data["trustedPhoneNumbers"] = data["trustedPhoneNumbers"]


def _masked_trusted_phone(service) -> Optional[str]:
    """Best-effort masked trusted-phone string pulled from the in-flight auth data."""
    def _mask(d) -> Optional[str]:
        if isinstance(d, dict):
            for key in ("numberWithDialCode", "obfuscatedNumber", "number", "lastTwoDigits"):
                val = d.get(key)
                if val:
                    return str(val)
        return None

    try:
        auth = getattr(service, "_auth_data", {}) or {}
        candidates = [auth.get("trustedPhoneNumber")]
        lst = auth.get("trustedPhoneNumbers")
        if isinstance(lst, list) and lst:
            candidates.append(lst[0])
        pv = auth.get("phoneNumberVerification")
        if isinstance(pv, dict):
            candidates.append(pv.get("trustedPhoneNumber"))
            plst = pv.get("trustedPhoneNumbers")
            if isinstance(plst, list) and plst:
                candidates.append(plst[0])
        for c in candidates:
            m = _mask(c)
            if m:
                return m
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
    # Assets that could not be downloaded after retries — "<filename>: <reason>". Kept so
    # the Fetcher can surface partial loss instead of silently dropping them.
    failures: list[str] = field(default_factory=list)


def _ms_from(val) -> Optional[int]:
    """Coerce a pyicloud date attribute (datetime | ms-int | None) to ms-since-epoch."""
    if isinstance(val, datetime):
        return int(val.timestamp() * 1000)
    if isinstance(val, (int, float)):
        return int(val)
    return None


def _asset_ms(photo) -> Optional[int]:
    """Capture ("taken") time in ms. Used only for EXIF `taken_at`, NOT the watermark."""
    for attr in ("asset_date", "created"):
        ms = _ms_from(getattr(photo, attr, None))
        if ms is not None:
            return ms
    return None


def _added_ms(photo) -> Optional[int]:
    """Time the asset was ADDED to the iCloud library, in ms — the incremental watermark
    key. This MUST match the iteration order (`_iter_added_desc_photos` walks the added
    index newest-first). Using capture time here instead would break the early-stop:
    an old photo added recently (screenshot, received image, import) sorts near the top
    but has an old capture time, prematurely ending the scan and skipping everything
    below it. pyicloud returns the Unix epoch (0) for a missing addedDate — treat that
    as unknown (None) so a stray record can't trigger the early break.
    """
    ms = _ms_from(getattr(photo, "added_date", None))
    if ms is None or ms <= 0:
        return None
    return ms


def _iter_photos_newest_first(api) -> Iterator[object]:
    """Yield PhotoAssets by ADDED date, newest-first, so the watermark scan can stop
    early at the first already-synced asset.

    Version 2.6.5 has `_iter_added_desc_photos` (walks Apple's added index descending)
    which we prefer. The fallback buffers and sorts by added-date descending so the
    order still matches the watermark comparison in `pull_new_photos`.
    """
    album = api.photos.all
    desc = getattr(album, "_iter_added_desc_photos", None)
    if callable(desc):
        try:
            yield from desc()
            return
        except Exception:
            logger.warning("added-descending iteration failed; falling back to sorted buffer")
    yield from sorted(list(album), key=lambda p: (_added_ms(p) or 0), reverse=True)


def pull_new_photos(
    api,
    dest_dir: str,
    since_ms: Optional[int] = None,
    max_items: Optional[int] = None,
    progress_cb=None,
) -> PullResult:
    """Download originals ADDED to iCloud after `since_ms` into `dest_dir`.

    `since_ms` and the returned watermark are keyed on each asset's *added-to-library*
    time (not capture time), matching the added-date iteration order so the early stop
    is valid. Iterates newest-added-first and stops at the first asset at/older than the
    watermark (icloudpd's --until-found idea). Live Photos download the still + its
    paired video as separate files. Immich checksum-dedup (and WebDAV path-dedup) on the
    Loader is the backstop if the watermark ever lets a duplicate through.
    """
    os.makedirs(dest_dir, exist_ok=True)
    assets: list[PulledAsset] = []
    failures: list[str] = []
    seen = 0
    success_max_ms: Optional[int] = None
    failed_min_ms: Optional[int] = None

    for photo in _iter_photos_newest_first(api):
        ms = _added_ms(photo)
        if since_ms is not None and ms is not None and ms <= since_ms:
            # Newest-added-first: everything past here was added on/before the last sync.
            break

        seen += 1
        pulled, reason = _download_with_retry(photo, dest_dir)
        if pulled is not None:
            assets.append(pulled)
            if ms is not None and (success_max_ms is None or ms > success_max_ms):
                success_max_ms = ms
            if progress_cb:
                progress_cb(len(assets))
        else:
            fname = _safe_name(getattr(photo, "filename", None) or str(getattr(photo, "id", "asset")))
            failures.append(f"{fname}: {reason}")
            logger.warning("Skipped iCloud asset %s: %s", fname, reason)
            # Keep the watermark below the oldest failure so it is retried next run
            # rather than being permanently skipped.
            if ms is not None and (failed_min_ms is None or ms < failed_min_ms):
                failed_min_ms = ms

        if max_items is not None and len(assets) >= max_items:
            break

    # Advance the watermark to the newest successfully-pulled asset, but never at/above a
    # failed asset — so a transient failure is re-attempted on the next sync (WebDAV
    # path-dedup / Immich checksum-dedup absorbs any re-pulled successes above it).
    new_watermark = since_ms
    if success_max_ms is not None:
        new_watermark = success_max_ms if new_watermark is None else max(new_watermark, success_max_ms)
    if failed_min_ms is not None:
        clamp = failed_min_ms - 1
        new_watermark = clamp if new_watermark is None else min(new_watermark, clamp)

    return PullResult(
        assets=assets, new_watermark_ms=new_watermark, total_seen=seen, failures=failures,
    )


def _download_with_retry(photo, dest_dir: str, attempts: int = 3):
    """Download one asset with retries. Returns (PulledAsset | None, reason).

    Transient errors (network resets, 5xx from the iCloud CDN) are retried with backoff.
    A missing downloadable resource is not retried (it won't fix itself) — it's reported
    as a reason so the Fetcher can surface it instead of silently dropping the asset.
    """
    last_reason = "unknown error"
    for attempt in range(1, attempts + 1):
        try:
            pulled = _download_asset(photo, dest_dir)
            if pulled is not None:
                return pulled, ""
            return None, "no downloadable full-resolution resource (asset may not be fully stored in iCloud)"
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 8))
    return None, last_reason


def _download_original_bytes(photo, dest_path: str) -> bool:
    """Write the asset's full-resolution bytes to `dest_path`.

    Prefers the true `original`; if that resource is absent, falls back to Apple's
    full-res `alternative` (e.g. the JPEG paired with a RAW, or a full-size edited
    version). We deliberately do NOT fall back to downscaled versions — a genuine miss
    is reported instead so the library's fidelity is honest. A transient error on the
    primary version propagates so the retry logic can see it.
    """
    if _write_download(photo.download("original"), dest_path):
        return True
    try:
        if _write_download(photo.download("alternative"), dest_path):
            logger.info("Asset %s had no 'original' resource; used 'alternative'",
                        getattr(photo, "filename", "?"))
            return True
    except Exception:
        pass
    return False


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


def _asset_token(photo) -> str:
    """Short, stable per-asset token for disambiguating a colliding filename.

    Derived from the iCloud asset id so the SAME asset always yields the SAME token —
    a re-pulled asset keeps its filename, so the downstream WebDAV upload stays
    idempotent (same target path → skipped, never duplicated).
    """
    import hashlib
    ident = str(getattr(photo, "id", None) or getattr(photo, "filename", "") or id(photo))
    return hashlib.sha1(ident.encode("utf-8", "ignore")).hexdigest()[:8]


def _unique_name(name: str, photo, dest_dir: str) -> str:
    """Avoid clobbering a *different* asset that already claimed this filename this pull.

    Two iCloud photos can share a filename (e.g. `IMG_0001.HEIC` from different devices
    or received images). Writing both to `dest_dir/name` would overwrite the first, and
    on a WebDAV destination (no checksum dedup) the second would then collide on the
    same remote path. If the name is already taken, insert a stable per-asset token.
    """
    if not os.path.exists(os.path.join(dest_dir, name)):
        return name
    stem, ext = os.path.splitext(name)
    return f"{stem}~{_asset_token(photo)}{ext}"


def _download_asset(photo, dest_dir: str) -> Optional[PulledAsset]:
    """Download the original of one PhotoAsset (+ Live Photo video) to `dest_dir`."""
    filename = _unique_name(
        _safe_name(getattr(photo, "filename", None) or f"{getattr(photo, 'id', 'asset')}.bin"),
        photo, dest_dir,
    )
    dest_path = os.path.join(dest_dir, filename)

    if not _download_original_bytes(photo, dest_path):
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
