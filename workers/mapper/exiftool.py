import os
import re
import subprocess
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".wmv", ".flv", ".ts"}


def write_metadata(
    file_path: str,
    taken_at: Optional[datetime],
    latitude: Optional[float],
    longitude: Optional[float],
    description: Optional[str],
) -> None:
    import os
    ext = os.path.splitext(file_path)[1].lower()
    is_video = ext in VIDEO_EXTENSIONS

    args = ["exiftool", "-overwrite_original", "-ignoreMinorErrors"]

    if taken_at:
        dt_str = taken_at.strftime("%Y:%m:%d %H:%M:%S")
        args += [
            f"-DateTimeOriginal={dt_str}",
            f"-CreateDate={dt_str}",
            f"-ModifyDate={dt_str}",
        ]
        if is_video:
            args += [
                f"-TrackCreateDate={dt_str}",
                f"-TrackModifyDate={dt_str}",
                f"-MediaCreateDate={dt_str}",
                f"-MediaModifyDate={dt_str}",
            ]

    if latitude is not None and longitude is not None:
        lat_ref = "N" if latitude >= 0 else "S"
        lon_ref = "E" if longitude >= 0 else "W"
        args += [
            f"-GPSLatitude={abs(latitude)}",
            f"-GPSLatitudeRef={lat_ref}",
            f"-GPSLongitude={abs(longitude)}",
            f"-GPSLongitudeRef={lon_ref}",
        ]

    if description:
        safe_desc = description.replace('"', '\\"')
        args += [
            f"-Description={safe_desc}",
            f"-ImageDescription={safe_desc}",
            f"-Comment={safe_desc}",
        ]

    args.append(file_path)

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.warning("exiftool warning on %s: %s", file_path, result.stderr.strip())
    except subprocess.TimeoutExpired:
        logger.error("exiftool timed out on %s", file_path)
    except FileNotFoundError:
        raise RuntimeError("exiftool not found — install it in the worker image")


def read_date(file_path: str) -> Optional[datetime]:
    """Read the best available date from a file's existing metadata.

    Tries DateTimeOriginal then CreateDate. Returns None if neither is present
    or parseable. Used as a fallback when no Google JSON sidecar is matched so
    that EXIF-stamped originals still get correct taken_at values for filtering.
    """
    args = ["exiftool", "-DateTimeOriginal", "-CreateDate", "-s3", file_path]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("0000"):
                continue
            try:
                return datetime.strptime(line, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Filename-based date extraction (last-resort fallback)
# ---------------------------------------------------------------------------

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _CHICAGO = _ZoneInfo("America/Chicago")
except Exception:
    _CHICAGO = None  # tzdata not installed; _chicago_to_utc falls back to manual DST


def _chicago_to_utc(year: int, month: int, day: int,
                    hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    """Convert a naive America/Chicago local time to a UTC-aware datetime."""
    if _CHICAGO is not None:
        local = datetime(year, month, day, hour, minute, second, tzinfo=_CHICAGO)
        return local.astimezone(timezone.utc)
    # Manual DST fallback: CDT (UTC-5) from 2nd Sunday in March 02:00 to
    # 1st Sunday in November 02:00; CST (UTC-6) otherwise.
    naive = datetime(year, month, day, hour, minute, second)
    mar1 = datetime(year, 3, 1)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7) + timedelta(weeks=1, hours=2)
    nov1 = datetime(year, 11, 1)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=2)
    offset = timezone(timedelta(hours=-5 if dst_start <= naive < dst_end else -6))
    return naive.replace(tzinfo=offset).astimezone(timezone.utc)


# Patterns ordered most-specific (datetime + seconds) to least (date only).
_FN_PATTERNS: list[tuple[re.Pattern, bool]] = [
    # YYYYMMDD[_-]HHMMSS  e.g. Snapchat-20201020-142345
    (re.compile(r'(?<!\d)(\d{4})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])[_\-]([01]\d|2[0-3])([0-5]\d)([0-5]\d)(?!\d)'), True),
    # YYYYMMDDHHMMSS  (14 contiguous digits, no separator)
    (re.compile(r'(?<!\d)(\d{4})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])([01]\d|2[0-3])([0-5]\d)([0-5]\d)(?!\d)'), True),
    # YYYY-MM-DD[T_ ]HH[-:]MM[-:]SS
    (re.compile(r'(?<!\d)(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])[T_ ]([01]\d|2[0-3])[-:]([0-5]\d)[-:]([0-5]\d)(?!\d)'), True),
    # YYYY-MM-DD  (date only, with separators)
    (re.compile(r'(?<!\d)(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?!\d)'), False),
    # YYYYMMDD  (date only, no separator)
    (re.compile(r'(?<!\d)(\d{4})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)'), False),
]


def extract_date_from_filename(file_path: str) -> Optional[datetime]:
    """Parse a date from the filename as a last-resort fallback.

    Tries patterns from most specific (datetime with seconds) to least (date
    only). Interprets the result as America/Chicago local time (CST/CDT),
    applying DST rules automatically.
    """
    stem = os.path.splitext(os.path.basename(file_path))[0]
    for pattern, has_time in _FN_PATTERNS:
        m = pattern.search(stem)
        if not m:
            continue
        try:
            parts = tuple(int(x) for x in m.groups())
            year, month, day = parts[0], parts[1], parts[2]
            if not (1970 <= year <= 2100):
                continue
            if has_time:
                hour, minute, second = parts[3], parts[4], parts[5]
            else:
                hour = minute = second = 0
            return _chicago_to_utc(year, month, day, hour, minute, second)
        except (ValueError, OverflowError):
            continue
    return None
