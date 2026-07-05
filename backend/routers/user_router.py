import json
import os
import shutil
import uuid
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from dependencies import get_db, get_current_user, get_redis
from models import User, ImmichCredential, WebDavDestination, JobRecord
from auth import hash_password, verify_password, delete_session
from crypto import encrypt, decrypt
from schemas import job_key, user_jobs_key

STAGING_ROOT = os.environ.get("STAGING_ROOT", "/staging")

router = APIRouter()


class UpdateProfileRequest(BaseModel):
    email: str | None = None
    current_password: str | None = None
    new_password: str | None = None


class AddImmichRequest(BaseModel):
    server_url: str
    api_key: str
    label: str | None = None


class UpdateImmichRequest(BaseModel):
    server_url: str | None = None
    api_key: str | None = None
    label: str | None = None


def _normalise_server_url(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    # Strip explicit default ports (https:443, http:80) so stored URLs are canonical
    try:
        parsed = urlparse(url)
        default_port = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
        if default_port is not None and parsed.port == default_port:
            url = urlunparse(parsed._replace(netloc=parsed.hostname))
    except Exception:
        pass
    return url


def _cred_out(cred: ImmichCredential) -> dict:
    return {
        "id": str(cred.id),
        "server_url": cred.server_url,
        "label": cred.label,
        "created_at": cred.created_at.isoformat(),
    }


@router.get("/profile")
async def get_profile(user: User = Depends(get_current_user)):
    return {
        "id": str(user.id),
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "created_at": user.created_at.isoformat(),
    }


@router.patch("/profile")
async def update_profile(
    body: UpdateProfileRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.new_password:
        if not body.current_password:
            raise HTTPException(status_code=400, detail="current_password required to set a new password")
        if not verify_password(body.current_password, user.password_hash):
            raise HTTPException(status_code=403, detail="Incorrect current password")
        if len(body.new_password) < 8:
            raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
        user.password_hash = hash_password(body.new_password)

    if body.email is not None:
        user.email = body.email or None

    await db.commit()
    await db.refresh(user)
    return {"id": str(user.id), "username": user.username, "email": user.email}


@router.delete("/profile", status_code=204)
async def delete_account(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    if user.role == "admin":
        count_result = await db.execute(
            select(func.count()).select_from(User).where(User.role == "admin")
        )
        if (count_result.scalar() or 0) <= 1:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete the only admin account — promote another user to admin first",
            )

    # Invalidate the current session
    token = request.cookies.get("session")
    if token:
        await delete_session(redis, token)
    response.delete_cookie(
        "session",
        httponly=True,
        samesite="lax",
        secure=os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
    )

    # Clean up all job data (Redis keys + staging dirs) before removing the user
    job_result = await db.execute(select(JobRecord).where(JobRecord.user_id == user.id))
    for record in job_result.scalars().all():
        raw = await redis.get(job_key(record.job_id))
        staging_dir = os.path.join(STAGING_ROOT, record.job_id)
        if raw:
            try:
                staging_dir = json.loads(raw).get("staging_dir") or staging_dir
            except Exception:
                pass
        await redis.delete(job_key(record.job_id))
        await redis.zrem(user_jobs_key(str(user.id)), record.job_id)
        if os.path.isdir(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)

    await db.delete(user)
    await db.commit()


@router.get("/immich")
async def list_immich(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ImmichCredential)
        .where(ImmichCredential.user_id == user.id)
        .order_by(ImmichCredential.created_at)
    )
    return [_cred_out(c) for c in result.scalars().all()]


@router.post("/immich", status_code=201)
async def add_immich(
    body: AddImmichRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not body.server_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="server_url must start with http:// or https://")

    cred = ImmichCredential(
        user_id=user.id,
        server_url=_normalise_server_url(body.server_url),
        encrypted_api_key=encrypt(body.api_key),
        label=body.label or None,
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)
    return _cred_out(cred)


@router.delete("/immich/{cred_id}", status_code=204)
async def delete_immich(
    cred_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        cid = uuid.UUID(cred_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Credential not found")

    result = await db.execute(
        select(ImmichCredential).where(
            ImmichCredential.id == cid,
            ImmichCredential.user_id == user.id,
        )
    )
    cred = result.scalar_one_or_none()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    await db.delete(cred)
    await db.commit()


@router.patch("/immich/{cred_id}")
async def update_immich(
    cred_id: str,
    body: UpdateImmichRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        cid = uuid.UUID(cred_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Credential not found")

    result = await db.execute(
        select(ImmichCredential).where(
            ImmichCredential.id == cid,
            ImmichCredential.user_id == user.id,
        )
    )
    cred = result.scalar_one_or_none()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    if body.server_url is not None:
        if not body.server_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="server_url must start with http:// or https://")
        cred.server_url = _normalise_server_url(body.server_url)

    if body.api_key is not None:
        cred.encrypted_api_key = encrypt(body.api_key)

    if body.label is not None:
        cred.label = body.label or None

    await db.commit()
    await db.refresh(cred)
    return _cred_out(cred)


@router.post("/immich/{cred_id}/test")
async def test_immich(
    cred_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        cid = uuid.UUID(cred_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Credential not found")

    result = await db.execute(
        select(ImmichCredential).where(
            ImmichCredential.id == cid,
            ImmichCredential.user_id == user.id,
        )
    )
    cred = result.scalar_one_or_none()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    api_key = decrypt(cred.encrypted_api_key)
    base = cred.server_url
    client_kwargs = dict(timeout=10.0, verify=False, follow_redirects=True)
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            # Step 1: connectivity — ping requires no auth
            ping = await client.get(f"{base}/api/server/ping")
            if ping.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Server is reachable but /api/server/ping returned HTTP {ping.status_code} — is this URL an Immich instance?",
                )

            # Step 2: authentication — validate the API key
            me = await client.get(f"{base}/api/users/me", headers={"x-api-key": api_key})

        if me.status_code == 200:
            data = me.json()
            identity = data.get("email") or data.get("name") or "unknown"
            return {"ok": True, "user": identity}
        elif me.status_code in (401, 403):
            raise HTTPException(status_code=502, detail="Server reachable but API key was rejected — regenerate it in Immich under Account Settings → API Keys")
        elif me.status_code == 404:
            raise HTTPException(status_code=502, detail="Server reachable but /api/users/me returned 404 — the Immich version may be too old or the URL has an extra path segment")
        else:
            raise HTTPException(status_code=502, detail=f"Server reachable but /api/users/me returned HTTP {me.status_code}")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach server at {base}: {exc}")


# ---------------------------------------------------------------------------
# WebDAV destinations (Nextcloud / ownCloud / PhotoPrism / any WebDAV server)
# ---------------------------------------------------------------------------

class AddWebDavRequest(BaseModel):
    base_url: str
    username: str
    password: str
    base_path: str = "Photoswitch"
    label: str | None = None


class UpdateWebDavRequest(BaseModel):
    base_url: str | None = None
    username: str | None = None
    password: str | None = None
    base_path: str | None = None
    label: str | None = None


def _webdav_out(d: WebDavDestination) -> dict:
    return {
        "id": str(d.id),
        "base_url": d.base_url,
        "username": d.username,
        "base_path": d.base_path,
        "label": d.label,
        "created_at": d.created_at.isoformat(),
    }


async def _get_webdav(db: AsyncSession, user: User, dest_id: str) -> WebDavDestination:
    try:
        did = uuid.UUID(dest_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Destination not found")
    d = (await db.execute(
        select(WebDavDestination).where(
            WebDavDestination.id == did, WebDavDestination.user_id == user.id
        )
    )).scalar_one_or_none()
    if not d:
        raise HTTPException(status_code=404, detail="Destination not found")
    return d


@router.get("/webdav")
async def list_webdav(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WebDavDestination)
        .where(WebDavDestination.user_id == user.id)
        .order_by(WebDavDestination.created_at)
    )
    return [_webdav_out(d) for d in result.scalars().all()]


@router.post("/webdav", status_code=201)
async def add_webdav(
    body: AddWebDavRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not body.base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url must start with http:// or https://")
    dest = WebDavDestination(
        user_id=user.id,
        base_url=body.base_url.rstrip("/"),
        username=body.username,
        encrypted_password=encrypt(body.password),
        base_path=(body.base_path or "Photoswitch").strip("/") or "Photoswitch",
        label=body.label or None,
    )
    db.add(dest)
    await db.commit()
    await db.refresh(dest)
    return _webdav_out(dest)


@router.patch("/webdav/{dest_id}")
async def update_webdav(
    dest_id: str,
    body: UpdateWebDavRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    dest = await _get_webdav(db, user, dest_id)
    if body.base_url is not None:
        if not body.base_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="base_url must start with http:// or https://")
        dest.base_url = body.base_url.rstrip("/")
    if body.username is not None:
        dest.username = body.username
    if body.password:
        dest.encrypted_password = encrypt(body.password)
    if body.base_path is not None:
        dest.base_path = body.base_path.strip("/") or "Photoswitch"
    if body.label is not None:
        dest.label = body.label or None
    await db.commit()
    await db.refresh(dest)
    return _webdav_out(dest)


@router.delete("/webdav/{dest_id}", status_code=204)
async def delete_webdav(
    dest_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    dest = await _get_webdav(db, user, dest_id)
    await db.delete(dest)
    await db.commit()


@router.post("/webdav/{dest_id}/test")
async def test_webdav(
    dest_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    dest = await _get_webdav(db, user, dest_id)
    password = decrypt(dest.encrypted_password)
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False, follow_redirects=True,
                                     auth=(dest.username, password)) as client:
            # PROPFIND depth 0 on the root is the standard WebDAV liveness/auth probe.
            resp = await client.request(
                "PROPFIND", dest.base_url, headers={"Depth": "0"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach WebDAV server at {dest.base_url}: {exc}")

    if resp.status_code in (207, 200):
        return {"ok": True, "user": dest.username}
    if resp.status_code in (401, 403):
        raise HTTPException(status_code=502, detail="Server reachable but credentials were rejected — check the username/app-password")
    if resp.status_code == 405:
        raise HTTPException(status_code=502, detail="Server reachable but did not accept PROPFIND — is base_url the WebDAV endpoint?")
    raise HTTPException(status_code=502, detail=f"Server reachable but returned HTTP {resp.status_code}")
