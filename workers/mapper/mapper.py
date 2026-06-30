import hashlib
import json
import logging
import os
import re
import time
from typing import Optional

from base_worker import BaseWorker
from mapper.exiftool import extract_date_from_filename, read_date, write_metadata
from mapper.sidecar import GoogleSidecar
from schemas import Job, MappedAsset, Stage

logger = logging.getLogger(__name__)

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".tif", ".bmp",
    ".heic", ".heif", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp",
    ".wmv", ".flv", ".ts",
}

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".wmv", ".flv", ".ts"}

# Google truncates filenames at this length (base without extension) in sidecar names
_GOOGLE_TRUNCATE_LEN = 51

# Google Takeout year-rollup folders are named "Photos from YYYY" (English).
# Anything that doesn't match this pattern is treated as an album folder.
_YEAR_FOLDER_RE = re.compile(r"^Photos\s+from\s+\d{4}", re.IGNORECASE)


class MapperWorker(BaseWorker):
    stage = Stage.MAP

    async def handle(self, job: Job) -> None:
        extracted_dir = job.extracted_dir
        if not extracted_dir or not os.path.isdir(extracted_dir):
            raise FileNotFoundError(f"Extracted dir not found: {extracted_dir}")

        all_files = _collect_files(extracted_dir)
        media_files = [p for p in all_files if os.path.splitext(p)[1].lower() in MEDIA_EXTENSIONS]

        # Identify motion video partners so we don't upload them as standalone assets
        live_video_set: set[str] = set()

        mapped: list[MappedAsset] = []
        job.total_items = len(media_files)
        job.processed_items = 0
        last_save = time.monotonic()

        for media_path in media_files:
            if media_path in live_video_set:
                job.processed_items += 1
                continue

            sidecar_path = _find_sidecar(media_path, all_files)
            sidecar = GoogleSidecar.from_file(sidecar_path) if sidecar_path else None

            # Detect Live Photo / Motion Photo pairing
            live_video_path: Optional[str] = None
            is_live = False
            ext = os.path.splitext(media_path)[1].lower()
            if ext in {".jpg", ".jpeg", ".heic", ".heif"}:
                video_partner = _find_live_partner(media_path, all_files)
                if video_partner:
                    live_video_path = video_partner
                    is_live = True
                    live_video_set.add(video_partner)

            # Write EXIF metadata
            taken_at = sidecar.taken_at if sidecar else None
            if taken_at is None:
                taken_at = read_date(media_path)
            if taken_at is None:
                taken_at = extract_date_from_filename(media_path)
            lat = sidecar.latitude if sidecar else None
            lon = sidecar.longitude if sidecar else None
            description = sidecar.description if sidecar else None

            try:
                write_metadata(media_path, taken_at, lat, lon, description)
                if live_video_path:
                    write_metadata(live_video_path, taken_at, lat, lon, None)
            except Exception as exc:
                logger.warning("exiftool failed on %s: %s", media_path, exc)

            checksum = _sha1(media_path)

            # Google per-photo sidecars don't carry albumData; album membership
            # is encoded in the directory name. Year-rollup folders ("Photos from YYYY")
            # are not albums — any other parent directory is.
            albums = sidecar.albums if sidecar else []
            if not albums:
                parent_name = os.path.basename(os.path.dirname(media_path))
                if parent_name and not _YEAR_FOLDER_RE.match(parent_name):
                    albums = [parent_name]

            asset = MappedAsset(
                file_path=media_path,
                checksum_sha1=checksum,
                taken_at=taken_at,
                latitude=lat,
                longitude=lon,
                description=description,
                albums=albums,
                people=sidecar.people if sidecar else [],
                is_live_photo=is_live,
                live_video_path=live_video_path,
            )
            mapped.append(asset)
            job.processed_items += 1

            now = time.monotonic()
            if now - last_save >= 5.0:
                await self.save_job(job)
                last_save = now

        output_path = os.path.join(job.staging_dir, "mapped_assets.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump([a.model_dump(mode="json") for a in mapped], f)

        logger.info("Job %s mapped %d assets, wrote %s", job.id, len(mapped), output_path)


def _collect_files(root: str) -> set[str]:
    files: set[str] = set()
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            files.add(os.path.join(dirpath, name))
    return files


def _find_sidecar(media_path: str, all_files: set[str]) -> Optional[str]:
    base, ext = os.path.splitext(media_path)
    dir_path = os.path.dirname(media_path)
    name_no_ext = os.path.basename(base)

    candidates = [
        # Most common: photo.jpg.json
        media_path + ".json",
        # Alternative: photo.json
        base + ".json",
        # Supplemental metadata
        media_path + ".supplemental-metadata.json",
    ]

    # Strip -edited suffix: photo-edited.jpg → try photo.jpg.json
    if name_no_ext.endswith("-edited"):
        orig_base = name_no_ext[: -len("-edited")]
        candidates.append(os.path.join(dir_path, orig_base + ext + ".json"))
        candidates.append(os.path.join(dir_path, orig_base + ".json"))

    # Truncated filename: Google truncates base at _GOOGLE_TRUNCATE_LEN chars
    if len(name_no_ext) > _GOOGLE_TRUNCATE_LEN:
        truncated = name_no_ext[:_GOOGLE_TRUNCATE_LEN]
        candidates.append(os.path.join(dir_path, truncated + ext + ".json"))
        candidates.append(os.path.join(dir_path, truncated + ".json"))

    # Numbered duplicates: photo(1).jpg → photo.jpg(1).json
    m = re.match(r"^(.*?)(\(\d+\))$", name_no_ext)
    if m:
        base_name, num = m.group(1), m.group(2)
        candidates.append(os.path.join(dir_path, base_name + ext + num + ".json"))
        candidates.append(os.path.join(dir_path, base_name + num + ext + ".json"))

    for candidate in candidates:
        if candidate in all_files:
            return candidate

    return None


def _find_live_partner(photo_path: str, all_files: set[str]) -> Optional[str]:
    base, _ = os.path.splitext(photo_path)
    for vid_ext in (".mp4", ".MP4", ".mov", ".MOV"):
        candidate = base + vid_ext
        if candidate in all_files:
            return candidate
    return None


def _sha1(file_path: str) -> str:
    h = hashlib.sha1()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
