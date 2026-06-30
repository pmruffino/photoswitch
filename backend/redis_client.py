import os
import redis.asyncio as aioredis

_redis: aioredis.Redis | None = None


async def init_redis() -> aioredis.Redis:
    global _redis
    _redis = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    return _redis


async def get_redis() -> aioredis.Redis:
    if _redis is None:
        return await init_redis()
    return _redis
