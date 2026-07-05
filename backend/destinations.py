"""
Destination resolution.

A job's upload target can be an Immich server (`immich_credentials`) or a WebDAV
server (`webdav_destinations`). Both the request handlers and the sync scheduler turn
a (kind, credential_id) pair into a schemas.Destination through here, so the
kind→table mapping lives in exactly one place.
"""

import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ImmichCredential, WebDavDestination
from schemas import Destination, DestinationKind


async def build_destination(
    db: AsyncSession, user_id, kind: str, credential_id: str | uuid.UUID
) -> Optional[Destination]:
    """Resolve a (kind, id) owned by user_id into a Destination, or None if missing.

    Raises ValueError only for an unknown `kind`.
    """
    dkind = DestinationKind(kind)  # ValueError on unknown kind
    try:
        cid = credential_id if isinstance(credential_id, uuid.UUID) else uuid.UUID(str(credential_id))
    except (ValueError, TypeError):
        return None

    if dkind == DestinationKind.WEBDAV:
        row = (await db.execute(
            select(WebDavDestination).where(
                WebDavDestination.id == cid, WebDavDestination.user_id == user_id
            )
        )).scalar_one_or_none()
        if not row:
            return None
        return Destination(kind=DestinationKind.WEBDAV, server_url=row.base_url, credential_ref=str(row.id))

    row = (await db.execute(
        select(ImmichCredential).where(
            ImmichCredential.id == cid, ImmichCredential.user_id == user_id
        )
    )).scalar_one_or_none()
    if not row:
        return None
    return Destination(kind=DestinationKind.IMMICH, server_url=row.server_url, credential_ref=str(row.id))


async def resolve_destination(db: AsyncSession, user, kind: str, credential_id: str) -> Destination:
    """Request-handler variant: 400 on unknown kind, 404 on missing/unowned destination."""
    try:
        dest = await build_destination(db, user.id, kind, credential_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown destination kind: {kind}")
    if dest is None:
        raise HTTPException(status_code=404, detail="Destination not found")
    return dest
