import asyncio
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod

import asyncpg
import redis.asyncio as aioredis

from schemas import (
    DEFAULT_WORKER_PCT,
    Job,
    JobStatus,
    Stage,
    WORKER_HEARTBEAT_INTERVAL,
    WORKER_HEARTBEAT_TTL,
    calculate_semaphore_limit,
    job_key,
    queue_key,
    semaphore_count_key,
    semaphore_limit_key,
    worker_presence_key,
    worker_pct_key,
)

logger = logging.getLogger(__name__)

# Lua script: atomically increment semaphore if count < limit
_ACQUIRE_SCRIPT = """
local limit = tonumber(redis.call('get', KEYS[1])) or tonumber(ARGV[1])
local count = tonumber(redis.call('get', KEYS[2])) or 0
if count < limit then
    redis.call('incr', KEYS[2])
    return 1
end
return 0
"""


class BaseWorker(ABC):
    stage: Stage
    max_attempts: int = 3

    def __init__(self) -> None:
        self.redis_url = os.environ["REDIS_URL"]
        self.staging_root = os.environ.get("STAGING_ROOT", "/staging")
        self.db_url = os.environ.get("DATABASE_URL")
        self.redis: aioredis.Redis | None = None
        self._db_pool: asyncpg.Pool | None = None
        self.worker_id = uuid.uuid4().hex

    async def connect(self) -> None:
        self.redis = aioredis.from_url(self.redis_url, decode_responses=True)
        if self.db_url:
            self._db_pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=3)
        await self._register()

    async def _recalculate_limit(self) -> None:
        """Recompute and store the semaphore limit based on live worker count × pct."""
        now = time.time()
        count = int(await self.redis.zcount(
            worker_presence_key(self.stage), now - WORKER_HEARTBEAT_TTL, "+inf"
        ))
        raw_pct = await self.redis.get(worker_pct_key(self.stage))
        pct = int(raw_pct) if raw_pct else DEFAULT_WORKER_PCT
        limit = calculate_semaphore_limit(count, pct)
        await self.redis.set(semaphore_limit_key(self.stage), limit)

    async def _register(self) -> None:
        await self.redis.zadd(worker_presence_key(self.stage), {self.worker_id: time.time()})
        await self._recalculate_limit()
        logger.info("Worker %s registered for stage %s", self.worker_id[:8], self.stage.value)

    async def _deregister(self) -> None:
        await self.redis.zrem(worker_presence_key(self.stage), self.worker_id)
        await self._recalculate_limit()
        logger.info("Worker %s deregistered for stage %s", self.worker_id[:8], self.stage.value)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(WORKER_HEARTBEAT_INTERVAL)
            try:
                await self.redis.zadd(worker_presence_key(self.stage), {self.worker_id: time.time()})
                await self._recalculate_limit()
            except Exception:
                logger.exception("Heartbeat error for worker %s", self.worker_id[:8])

    async def acquire_semaphore(self) -> bool:
        result = await self.redis.eval(
            _ACQUIRE_SCRIPT,
            2,
            semaphore_limit_key(self.stage),
            semaphore_count_key(self.stage),
            1,  # conservative fallback if limit key is missing
        )
        return result == 1

    async def release_semaphore(self) -> None:
        count_key = semaphore_count_key(self.stage)
        val = await self.redis.get(count_key)
        if val and int(val) > 0:
            await self.redis.decr(count_key)

    async def save_job(self, job: Job) -> None:
        await self.redis.set(job_key(job.id), job.model_dump_json())

    async def sync_job_to_db(self, job: Job) -> None:
        if not self._db_pool:
            return
        async with self._db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE job_records
                SET stage = $1, status = $2, total_items = $3,
                    processed_items = $4, error = $5, updated_at = NOW()
                WHERE job_id = $6
                """,
                job.stage.value,
                job.status.value,
                job.total_items,
                job.processed_items,
                job.error,
                job.id,
            )

    async def run(self) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        await self.connect()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        q_key = queue_key(self.stage)
        logger.info("Worker %s listening on %s", self.__class__.__name__, q_key)

        try:
            while True:
                try:
                    result = await self.redis.brpop(q_key, timeout=5)
                    if result is None:
                        continue

                    _, raw = result
                    job = Job.model_validate_json(raw)

                    got_slot = await self.acquire_semaphore()
                    if not got_slot:
                        await self.redis.lpush(q_key, raw)
                        await asyncio.sleep(2)
                        continue

                    job.status = JobStatus.RUNNING
                    job.attempts += 1
                    job.touch()
                    await self.save_job(job)

                    try:
                        await self.handle(job)
                        if self.stage == Stage.FETCH and not job.auto_ingest:
                            # Download complete — park until the user manually triggers ingest.
                            job.status = JobStatus.SUCCEEDED
                            job.touch()
                            await self.save_job(job)
                            await self.sync_job_to_db(job)
                            logger.info("Job %s download complete, awaiting manual ingest", job.id)
                        else:
                            advanced = job.advance()
                            await self.save_job(job)
                            await self.sync_job_to_db(job)
                            if advanced:
                                next_q = queue_key(job.stage)
                                await self.redis.lpush(next_q, job.model_dump_json())
                                logger.info("Job %s advanced to %s", job.id, job.stage.value)
                            else:
                                logger.info("Job %s completed successfully", job.id)
                    except Exception as exc:
                        logger.exception("Job %s failed at %s: %s", job.id, self.stage.value, exc)
                        if job.attempts < self.max_attempts:
                            job.status = JobStatus.QUEUED
                            job.touch()
                            await self.save_job(job)
                            await self.redis.lpush(q_key, job.model_dump_json())
                            logger.info("Job %s re-queued (attempt %d/%d)", job.id, job.attempts, self.max_attempts)
                        else:
                            job.fail(str(exc))
                            await self.save_job(job)
                            await self.sync_job_to_db(job)
                            logger.error("Job %s permanently failed after %d attempts", job.id, job.attempts)
                    finally:
                        await self.release_semaphore()

                except Exception as exc:
                    logger.exception("Worker loop error: %s", exc)
                    await asyncio.sleep(1)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            await self._deregister()

    @abstractmethod
    async def handle(self, job: Job) -> None:
        ...
