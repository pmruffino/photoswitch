import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from typing import Optional

import asyncpg
import httpx

from base_worker import BaseWorker
from schemas import DateFilter, Job, MappedAsset, Stage, job_key

logger = logging.getLogger(__name__)

DEVICE_ID = "photoswitch"
_CHECK_BATCH = 100   # assets per bulk-upload-check call
_DELETE_BATCH = 100  # asset IDs per DELETE /api/assets call
_OWNERSHIP_SEM = 20  # max concurrent GET /api/assets/{id} calls


def _make_fernet():
    from cryptography.fernet import Fernet
    secret = os.environ["APP_SECRET_KEY"].encode()
    derived = hashlib.sha256(secret).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


class RollbackWorker(BaseWorker):
    stage = Stage.ROLLBACK
    max_attempts = 1  # rollback failures should be reviewed, not silently retried

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
        source_job_id = job.rollback_source_job_id
        if not source_job_id:
            raise ValueError("rollback_source_job_id not set on rollback job")

        # Locate the source job's mapped_assets.json (prefer Redis for the exact staging path)
        source_staging = os.path.join(self.staging_root, source_job_id)
        raw = await self.redis.get(job_key(source_job_id))
        if raw:
            try:
                source_staging = json.loads(raw).get("staging_dir") or source_staging
            except Exception:
                pass

        assets_file = os.path.join(source_staging, "mapped_assets.json")
        if not os.path.exists(assets_file):
            raise FileNotFoundError(
                f"Staging data for job {source_job_id} not found — "
                "the files may have been cleaned up. Rollback requires staged files to be present."
            )

        with open(assets_file, "r", encoding="utf-8") as f:
            all_assets = [MappedAsset.model_validate(a) for a in json.load(f)]

        # Apply date filter and undated flag to decide which assets are in scope
        date_filter: Optional[DateFilter] = job.date_filter
        include_undated: bool = job.rollback_undated
        in_scope: list[MappedAsset] = []
        for asset in all_assets:
            if asset.taken_at is None:
                if include_undated:
                    in_scope.append(asset)
            elif date_filter is None or date_filter.includes(asset.taken_at):
                in_scope.append(asset)

        logger.info(
            "Job %s rollback: %d / %d assets in scope (source job: %s)",
            job.id, len(in_scope), len(all_assets), source_job_id,
        )

        job.total_items = len(in_scope)
        job.processed_items = 0
        await self.save_job(job)

        if not in_scope:
            return

        server_url, api_key = await self._get_api_key(job.target.credential_ref)
        base_url = server_url.rstrip("/")
        headers = {"x-api-key": api_key, "Accept": "application/json"}

        to_delete: list[str] = []
        last_save = time.monotonic()
        ownership_sem = asyncio.Semaphore(_OWNERSHIP_SEM)

        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=60, verify=False, follow_redirects=True) as client:
            # --- Phase 1: find which scoped assets exist in Immich and are owned by this job ---
            for batch_start in range(0, len(in_scope), _CHECK_BATCH):
                batch = in_scope[batch_start : batch_start + _CHECK_BATCH]

                # Build the bulk-upload-check payload
                # checksum must be base64-encoded SHA-1; our stored value is hex
                check_payload = []
                for asset in batch:
                    if not asset.checksum_sha1:
                        continue
                    filename = os.path.basename(asset.file_path)
                    device_asset_id = f"{source_job_id}_{filename}"
                    sha1_b64 = base64.b64encode(bytes.fromhex(asset.checksum_sha1)).decode()
                    check_payload.append({"id": device_asset_id, "checksum": sha1_b64})

                if check_payload:
                    resp = await client.post(
                        "/api/assets/bulk-upload-check",
                        json={"assets": check_payload},
                    )
                    if resp.status_code == 200:
                        # "reject" results include the Immich UUID for existing assets
                        candidates = {
                            r["id"]: r["assetId"]
                            for r in resp.json().get("results", [])
                            if r.get("action") == "reject" and r.get("assetId")
                        }

                        # Verify ownership: only delete assets uploaded by this specific job
                        async def check_ownership(
                            device_asset_id: str, immich_id: str
                        ) -> Optional[str]:
                            async with ownership_sem:
                                try:
                                    r = await client.get(f"/api/assets/{immich_id}")
                                    if r.status_code == 200:
                                        data = r.json()
                                        if (
                                            data.get("deviceId") == DEVICE_ID
                                            and data.get("deviceAssetId") == device_asset_id
                                        ):
                                            return immich_id
                                except Exception:
                                    pass
                            return None

                        owned = await asyncio.gather(
                            *(check_ownership(did, iid) for did, iid in candidates.items())
                        )
                        to_delete.extend(iid for iid in owned if iid)
                    else:
                        logger.warning(
                            "Job %s: bulk-upload-check returned HTTP %d for batch at %d",
                            job.id, resp.status_code, batch_start,
                        )

                job.processed_items = min(batch_start + _CHECK_BATCH, len(in_scope))
                now = time.monotonic()
                if now - last_save >= 5.0:
                    await self.save_job(job)
                    last_save = now

            logger.info(
                "Job %s: found %d owned assets to remove (of %d checked)",
                job.id, len(to_delete), len(in_scope),
            )

            # --- Phase 2: bulk delete confirmed-owned assets ---
            deleted = 0
            for i in range(0, len(to_delete), _DELETE_BATCH):
                ids = to_delete[i : i + _DELETE_BATCH]
                resp = await client.request("DELETE", "/api/assets", json={"ids": ids, "force": True})
                if resp.status_code in (200, 204):
                    deleted += len(ids)
                else:
                    logger.warning(
                        "Job %s: delete batch failed HTTP %d: %s",
                        job.id, resp.status_code, resp.text[:200],
                    )

        logger.info(
            "Job %s rollback complete: removed %d assets from Immich",
            job.id, deleted,
        )
