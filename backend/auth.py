import json
import secrets
from typing import Optional

import argon2
from argon2 import PasswordHasher
import redis.asyncio as aioredis

from schemas import session_key

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except argon2.exceptions.VerifyMismatchError:
        return False
    except argon2.exceptions.VerificationError:
        return False


async def create_session(redis: aioredis.Redis, user_id: str, ttl: int) -> str:
    token = secrets.token_hex(32)
    await redis.set(session_key(token), json.dumps({"user_id": user_id}), ex=ttl)
    return token


async def get_session(redis: aioredis.Redis, token: str) -> Optional[dict]:
    raw = await redis.get(session_key(token))
    return json.loads(raw) if raw else None


async def delete_session(redis: aioredis.Redis, token: str) -> None:
    await redis.delete(session_key(token))
