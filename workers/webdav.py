"""
WebDAV destination client for the Loader/Rollback.

Covers Nextcloud, ownCloud, PhotoPrism, and any WebDAV server. Photos are stored as
plain files in folders; the Mapper has already written timestamp/GPS/description into
each file's EXIF/QuickTime, so that metadata travels with the upload — Nextcloud
Memories / PhotoPrism index it without any destination-side metadata API.

Albums are folder-based (v1): album `A` → folder `{base_path}/A/`, un-albumed photos
→ `{base_path}/`.

KEY BEHAVIOUR — a photo in multiple albums is uploaded (PUT) exactly once. Its bytes
land in the first album's folder; membership in every other album is a server-side
WebDAV `COPY` (no client re-upload). If a server rejects COPY, the extra membership is
skipped with a warning rather than re-uploading. Re-runs are idempotent: an existing
target path is left untouched.

All requests use verify=False, follow_redirects=True — the same convention as the
Immich calls, so self-signed / proxied certs work without configuration.
"""

import logging
import os
import re
from urllib.parse import quote

import httpx

from schemas import MappedAsset

logger = logging.getLogger(__name__)


def _safe_seg(name: str) -> str:
    """Sanitise one path segment: no separators or control chars."""
    name = re.sub(r"[\\/\x00-\x1f]", "_", name).strip()
    return name[:255] or "_"


class WebDavClient:
    def __init__(self, base_url: str, username: str, password: str, base_path: str = "Photoswitch"):
        self.base_url = base_url.rstrip("/")
        self.base_path = base_path.strip("/")
        self._client = httpx.AsyncClient(
            auth=(username, password), timeout=600, verify=False, follow_redirects=True,
        )
        self._ensured: set[str] = set()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "WebDavClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # -- URL helpers ----------------------------------------------------------

    def _url(self, segments: list[str]) -> str:
        parts = [self.base_url] + [quote(s, safe="") for s in segments if s != ""]
        return "/".join(parts)

    def _album_dir(self, album: str | None) -> list[str]:
        base = [self.base_path] if self.base_path else []
        return base + ([_safe_seg(album)] if album else [])

    # -- WebDAV primitives ----------------------------------------------------

    async def ensure_dir(self, segments: list[str]) -> None:
        """MKCOL each ancestor in turn (WebDAV can't create nested dirs in one call)."""
        for i in range(1, len(segments) + 1):
            prefix = segments[:i]
            key = "/".join(prefix)
            if key in self._ensured:
                continue
            resp = await self._client.request("MKCOL", self._url(prefix))
            # 201 created; 405 already exists; 301/302 redirect handled by client.
            if resp.status_code not in (201, 405, 200, 301, 302):
                logger.debug("MKCOL %s -> %s", key, resp.status_code)
            self._ensured.add(key)

    async def exists(self, segments: list[str]) -> bool:
        resp = await self._client.request("HEAD", self._url(segments))
        return resp.status_code < 400

    async def put_file(self, local_path: str, segments: list[str]) -> bool:
        # Async generator so httpx streams the body (an async client rejects a sync
        # iterable). Keeps large videos off the heap.
        async def _aiter():
            with open(local_path, "rb") as f:
                while True:
                    chunk = f.read(1 << 16)
                    if not chunk:
                        break
                    yield chunk
        resp = await self._client.put(self._url(segments), content=_aiter())
        if resp.status_code not in (200, 201, 204):
            logger.warning("WebDAV PUT %s -> HTTP %d", "/".join(segments), resp.status_code)
            return False
        return True

    async def copy(self, src: list[str], dest: list[str]) -> bool:
        """Server-side COPY src → dest. Overwrite: F, so an existing dest yields 412
        (treated as success — already present)."""
        resp = await self._client.request(
            "COPY", self._url(src),
            headers={"Destination": self._url(dest), "Overwrite": "F"},
        )
        return resp.status_code in (201, 204, 412)

    async def delete(self, segments: list[str]) -> None:
        resp = await self._client.request("DELETE", self._url(segments))
        if resp.status_code not in (200, 204, 404):
            logger.warning("WebDAV DELETE %s -> HTTP %d", "/".join(segments), resp.status_code)

    # -- High-level asset operations -----------------------------------------

    async def upload_asset(self, asset: MappedAsset) -> None:
        """Upload one asset once, then place it in any additional albums via COPY."""
        albums = list(asset.albums or [])
        primary_album = albums[0] if albums else None
        primary_dir = self._album_dir(primary_album)
        await self.ensure_dir(primary_dir)

        photo = os.path.basename(asset.file_path)
        primary_photo = primary_dir + [photo]
        if not await self.exists(primary_photo):
            await self.put_file(asset.file_path, primary_photo)

        has_video = bool(asset.is_live_photo and asset.live_video_path and os.path.exists(asset.live_video_path))
        primary_video = None
        if has_video:
            video = os.path.basename(asset.live_video_path)
            primary_video = primary_dir + [video]
            if not await self.exists(primary_video):
                await self.put_file(asset.live_video_path, primary_video)

        # Extra albums: server-side COPY only — never re-upload the bytes.
        for album in albums[1:]:
            extra_dir = self._album_dir(album)
            await self.ensure_dir(extra_dir)
            dest_photo = extra_dir + [photo]
            if not await self.exists(dest_photo):
                if not await self.copy(primary_photo, dest_photo):
                    logger.warning(
                        "WebDAV COPY not supported for %s → album %s; skipping extra album membership",
                        photo, album,
                    )
            if has_video:
                dest_video = extra_dir + [os.path.basename(asset.live_video_path)]
                if not await self.exists(dest_video):
                    await self.copy(primary_video, dest_video)

    async def delete_asset(self, asset: MappedAsset) -> None:
        """Delete every file this asset produced — the photo (+ live video) in each
        album folder it was placed in."""
        albums = list(asset.albums or [])
        dirs = [self._album_dir(a) for a in albums] or [self._album_dir(None)]
        photo = os.path.basename(asset.file_path)
        video = os.path.basename(asset.live_video_path) if asset.live_video_path else None
        for d in dirs:
            await self.delete(d + [photo])
            if video:
                await self.delete(d + [video])
