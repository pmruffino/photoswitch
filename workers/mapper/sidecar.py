import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class GoogleSidecar:
    def __init__(
        self,
        title: Optional[str],
        description: Optional[str],
        taken_at: Optional[datetime],
        latitude: Optional[float],
        longitude: Optional[float],
        altitude: Optional[float],
        people: list[str],
        albums: list[str],
    ) -> None:
        self.title = title
        self.description = description
        self.taken_at = taken_at
        self.latitude = latitude
        self.longitude = longitude
        self.altitude = altitude
        self.people = people
        self.albums = albums

    @classmethod
    def from_file(cls, path: str) -> Optional["GoogleSidecar"]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

        title = data.get("title")
        description = data.get("description") or None

        taken_at = _parse_timestamp(data.get("photoTakenTime")) or _parse_timestamp(data.get("creationTime"))

        lat, lon, alt = _parse_gps(data)

        people = [p["name"] for p in data.get("people", []) if isinstance(p, dict) and p.get("name")]
        albums = [a["title"] for a in data.get("albumData", []) if isinstance(a, dict) and a.get("title")]

        return cls(
            title=title,
            description=description,
            taken_at=taken_at,
            latitude=lat,
            longitude=lon,
            altitude=alt,
            people=people,
            albums=albums,
        )


def _parse_timestamp(obj: Optional[dict]) -> Optional[datetime]:
    if not obj or not isinstance(obj, dict):
        return None
    ts = obj.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (ValueError, OSError):
        return None


def _parse_gps(data: dict) -> tuple[Optional[float], Optional[float], Optional[float]]:
    for key in ("geoDataExif", "geoData"):
        geo = data.get(key)
        if not geo or not isinstance(geo, dict):
            continue
        lat = geo.get("latitude")
        lon = geo.get("longitude")
        alt = geo.get("altitude")
        if lat is not None and lon is not None and (lat != 0.0 or lon != 0.0):
            return float(lat), float(lon), float(alt) if alt is not None else None
    return None, None, None
