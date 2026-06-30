import asyncio
import logging
import os
import shutil
import tarfile
import zipfile
from typing import Callable

from base_worker import BaseWorker
from schemas import Job, Stage

logger = logging.getLogger(__name__)

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".tif", ".bmp",
    ".heic", ".heif", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp",
    ".wmv", ".flv", ".ts",
}


class UnpackerWorker(BaseWorker):
    stage = Stage.UNPACK

    async def handle(self, job: Job) -> None:
        archive = job.archive_path
        if not archive or not os.path.exists(archive):
            raise FileNotFoundError(f"Archive not found: {archive}")

        extracted_dir = os.path.join(job.staging_dir, "extracted")
        if os.path.isdir(extracted_dir):
            shutil.rmtree(extracted_dir)
        os.makedirs(extracted_dir)

        lower = archive.lower()
        if lower.endswith(".zip"):
            extract_fn: Callable = lambda: self._extract_zip(archive, extracted_dir, job)
        elif lower.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
            extract_fn = lambda: self._extract_tar(archive, extracted_dir, job)
        else:
            raise ValueError(f"Unsupported archive format: {archive}")

        await self._extract_with_progress(extract_fn, job)

        media_count = self._count_media(extracted_dir)
        job.extracted_dir = extracted_dir
        # Reset so MAP stage starts with clean numbers (0 / media_count)
        job.processed_items = 0
        job.total_items = media_count
        logger.info("Job %s extracted %d media files to %s", job.id, media_count, extracted_dir)

    async def _extract_with_progress(self, extract_fn: Callable, job: Job) -> None:
        """Run sync extraction in a thread pool, saving progress every 5 s."""
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, extract_fn)
        while True:
            done, _ = await asyncio.wait({future}, timeout=5.0)
            if done:
                await future  # propagate any exception from the thread
                return
            await self.save_job(job)

    def _extract_zip(self, archive: str, dest: str, job: Job) -> None:
        with zipfile.ZipFile(archive, "r") as zf:
            members = zf.infolist()
            job.total_items = len(members)
            job.processed_items = 0
            for i, member in enumerate(members):
                zf.extract(member, dest)
                job.processed_items = i + 1

    def _extract_tar(self, archive: str, dest: str, job: Job) -> None:
        with tarfile.open(archive, "r:*") as tf:
            members = tf.getmembers()
            job.total_items = len(members)
            job.processed_items = 0
            for i, member in enumerate(members):
                tf.extract(member, dest, set_attrs=False)
                job.processed_items = i + 1

    def _count_media(self, directory: str) -> int:
        count = 0
        for root, _, files in os.walk(directory):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in MEDIA_EXTENSIONS:
                    count += 1
        return count
