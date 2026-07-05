import base64
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import httpx

from base_worker import BaseWorker
from schemas import DateFilter, DestinationKind, Job, MappedAsset, Stage
from webdav import WebDavClient

logger = logging.getLogger(__name__)

DEVICE_ID = "photoswitch"


def _make_fernet():
    from cryptography.fernet import Fernet
    secret = os.environ["APP_SECRET_KEY"].encode()
    derived = hashlib.sha256(secret).digest()
    fernet_key = base64.urlsafe_b64encode(derived)
    return Fernet(fernet_key)


class LoaderWorker(BaseWorker):
    stage = Stage.LOAD

    async def _get_api_key(self, credential_ref: str) -> tuple[str, str]:
        async with self._db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT server_url, encrypted_api_key FROM immich_credentials WHERE id = $1::uuid",
                credential_ref,
            )
        if not row:
            raise ValueError(f"Credential {credential_ref} not found")
        api_key = _make_fernet().decrypt(bytes(row["encrypted_api_key"])).decode()
        return row["server_url"], api_key

    async def handle(self, job: Job) -> None:
        assets_file = os.path.join(job.staging_dir, "mapped_assets.json")
        if not os.path.exists(assets_file):
            raise FileNotFoundError(f"mapped_assets.json not found in {job.staging_dir}")

        with open(assets_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
        assets = [MappedAsset.model_validate(a) for a in raw]

        job.total_items = len(assets)
        job.processed_items = 0

        # Dispatch by destination kind — everything above (the mapped_assets contract)
        # is identical across destinations.
        if job.target.kind == DestinationKind.WEBDAV:
            await self._load_webdav(job, assets)
        else:
            await self._load_immich(job, assets)

    async def _get_webdav_creds(self, credential_ref: str) -> tuple[str, str, str, str]:
        async with self._db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT base_url, username, encrypted_password, base_path "
                "FROM webdav_destinations WHERE id = $1::uuid",
                credential_ref,
            )
        if not row:
            raise ValueError(f"WebDAV destination {credential_ref} not found")
        password = _make_fernet().decrypt(bytes(row["encrypted_password"])).decode()
        return row["base_url"], row["username"], password, row["base_path"]

    async def _load_webdav(self, job: Job, assets: list[MappedAsset]) -> None:
        base_url, username, password, base_path = await self._get_webdav_creds(job.target.credential_ref)
        date_filter: Optional[DateFilter] = job.date_filter
        last_save = time.monotonic()

        async with WebDavClient(base_url, username, password, base_path) as client:
            for asset in assets:
                if date_filter and not date_filter.includes(asset.taken_at):
                    job.processed_items += 1
                else:
                    try:
                        await client.upload_asset(asset)
                    except Exception as exc:
                        logger.warning("WebDAV upload failed for %s: %s", asset.file_path, exc)
                    job.processed_items += 1

                now = time.monotonic()
                if now - last_save >= 5.0:
                    await self.save_job(job)
                    last_save = now

    async def _load_immich(self, job: Job, assets: list[MappedAsset]) -> None:
        server_url, api_key = await self._get_api_key(job.target.credential_ref)
        base_url = server_url.rstrip("/")
        headers = {"x-api-key": api_key, "Accept": "application/json"}

        date_filter: Optional[DateFilter] = job.date_filter
        if date_filter:
            logger.info(
                "Job %s applying date filter: after=%s before=%s",
                job.id, date_filter.after_date, date_filter.before_date,
            )

        job.total_items = len(assets)
        job.processed_items = 0
        last_save = time.monotonic()

        album_cache: dict[str, str] = {}

        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=300, verify=False, follow_redirects=True) as client:
            for asset in assets:
                if date_filter and not date_filter.includes(asset.taken_at):
                    logger.debug(
                        "Skipping %s (taken_at=%s filtered out)",
                        os.path.basename(asset.file_path), asset.taken_at,
                    )
                    job.processed_items += 1
                else:
                    try:
                        asset_id = await self._upload_asset(client, asset, job.id)
                        if asset_id and asset.is_live_photo and asset.live_video_path:
                            await self._upload_live_video(client, asset, asset_id, job.id)
                        if asset_id:
                            for album_name in asset.albums:
                                album_id = await self._ensure_album(client, album_name, album_cache)
                                await self._add_to_album(client, album_id, asset_id)
                    except Exception as exc:
                        logger.warning("Failed to load asset %s: %s", asset.file_path, exc)

                    job.processed_items += 1

                now = time.monotonic()
                if now - last_save >= 5.0:
                    await self.save_job(job)
                    last_save = now


    async def _upload_asset(self, client: httpx.AsyncClient, asset: MappedAsset, job_id: str) -> Optional[str]:
        if not os.path.exists(asset.file_path):
            logger.warning("Asset file missing: %s", asset.file_path)
            return None

        filename = os.path.basename(asset.file_path)
        taken_at_str = (
            asset.taken_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            if asset.taken_at
            else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        )

        with open(asset.file_path, "rb") as f:
            files = {"assetData": (filename, f, _mime_for(filename))}
            data = {
                "deviceAssetId": f"{job_id}_{filename}",
                "deviceId": DEVICE_ID,
                "fileCreatedAt": taken_at_str,
                "fileModifiedAt": taken_at_str,
                "isFavorite": "false",
            }
            response = await client.post("/api/assets", data=data, files=files)

        if response.status_code in (200, 201):
            body = response.json()
            status = body.get("status", "created")
            asset_id = body.get("id")
            if status == "duplicate":
                logger.debug("Asset already in Immich: %s", filename)
            return asset_id
        elif response.status_code == 409:
            logger.debug("Asset duplicate (409): %s", filename)
            return None
        else:
            logger.warning("Upload failed for %s: HTTP %d %s", filename, response.status_code, response.text[:200])
            return None

    async def _upload_live_video(
        self,
        client: httpx.AsyncClient,
        asset: MappedAsset,
        photo_asset_id: str,
        job_id: str,
    ) -> None:
        if not asset.live_video_path or not os.path.exists(asset.live_video_path):
            return

        filename = os.path.basename(asset.live_video_path)
        taken_at_str = (
            asset.taken_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            if asset.taken_at
            else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        )

        with open(asset.live_video_path, "rb") as f:
            files = {"assetData": (filename, f, "video/mp4")}
            data = {
                "deviceAssetId": f"{job_id}_{filename}",
                "deviceId": DEVICE_ID,
                "fileCreatedAt": taken_at_str,
                "fileModifiedAt": taken_at_str,
                "isFavorite": "false",
                "livePhotoVideoId": photo_asset_id,
            }
            response = await client.post("/api/assets", data=data, files=files)

        if response.status_code not in (200, 201, 409):
            logger.warning("Live video upload failed for %s: HTTP %d", filename, response.status_code)

    async def _ensure_album(
        self,
        client: httpx.AsyncClient,
        album_name: str,
        cache: dict[str, str],
    ) -> str:
        if album_name in cache:
            return cache[album_name]

        response = await client.get("/api/albums")
        if response.status_code == 200:
            for album in response.json():
                if album.get("albumName") == album_name:
                    cache[album_name] = album["id"]
                    return album["id"]

        response = await client.post("/api/albums", json={"albumName": album_name})
        if response.status_code in (200, 201):
            album_id = response.json()["id"]
            cache[album_name] = album_id
            return album_id

        raise RuntimeError(f"Could not create or find album: {album_name}")

    async def _add_to_album(self, client: httpx.AsyncClient, album_id: str, asset_id: str) -> None:
        response = await client.put(f"/api/albums/{album_id}/assets", json={"ids": [asset_id]})
        if response.status_code not in (200, 201):
            logger.warning("Failed to add asset %s to album %s: HTTP %d", asset_id, album_id, response.status_code)


def _mime_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp", ".tiff": "image/tiff", ".tif": "image/tiff",
        ".heic": "image/heic", ".heif": "image/heif",
        ".bmp": "image/bmp",
        ".mp4": "video/mp4", ".mov": "video/quicktime",
        ".m4v": "video/mp4", ".avi": "video/avi",
        ".mkv": "video/x-matroska", ".3gp": "video/3gpp",
    }.get(ext, "application/octet-stream")
