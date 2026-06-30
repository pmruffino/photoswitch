import logging
import os
import re
import time
from urllib.parse import urlparse, unquote

import httpx

from base_worker import BaseWorker
from schemas import Job, Stage, MAX_TAKEOUT_BYTES_KEY, DEFAULT_MAX_TAKEOUT_BYTES

logger = logging.getLogger(__name__)


class FetcherWorker(BaseWorker):
    stage = Stage.FETCH

    async def handle(self, job: Job) -> None:
        raw_limit = await self.redis.get(MAX_TAKEOUT_BYTES_KEY)
        max_bytes = int(raw_limit) if raw_limit else DEFAULT_MAX_TAKEOUT_BYTES

        staging_dir = os.path.join(self.staging_root, job.id)
        os.makedirs(staging_dir, exist_ok=True)
        job.staging_dir = staging_dir

        # Reset progress counters before (re-)attempting a download
        job.processed_items = 0
        job.total_items = None

        archive_path = await self._download(job.takeout_url, staging_dir, max_bytes, job)
        job.archive_path = archive_path
        logger.info("Job %s fetched archive: %s", job.id, archive_path)

    async def _download(self, url: str, dest_dir: str, max_bytes: int, job: Job) -> str:
        file_id = self._gdrive_file_id(url)

        # Browser UA so Google serves the file/page rather than a bot-detection block.
        # One client for both resolution and download so cookies are preserved.
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=3600,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
        ) as client:
            if file_id:
                url = await self._resolve_gdrive(file_id, client)
                logger.info("Resolved Google Drive URL → %s", url)

            async with client.stream("GET", url) as response:
                response.raise_for_status()

                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type:
                    raise ValueError(
                        "URL returned an HTML page instead of a file — the share link may "
                        "require sign-in or the file is no longer accessible."
                    )

                content_length = response.headers.get("content-length")
                if content_length:
                    total_bytes = int(content_length)
                    if total_bytes > max_bytes:
                        raise ValueError(
                            f"Archive is {total_bytes / 1e9:.1f} GB, which exceeds the "
                            f"configured limit of {max_bytes / 1e9:.1f} GB"
                        )
                    job.total_items = total_bytes
                else:
                    total_bytes = None

                filename = self._extract_filename(response, url)
                dest_path = os.path.join(dest_dir, filename)

                logger.info("Downloading → %s", dest_path)
                downloaded = 0
                last_report = time.monotonic()

                try:
                    with open(dest_path, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                raise ValueError(
                                    f"Download exceeded the configured limit of {max_bytes / 1e9:.1f} GB"
                                )
                            f.write(chunk)

                            now = time.monotonic()
                            if now - last_report >= 5.0:
                                job.processed_items = downloaded
                                await self.save_job(job)
                                last_report = now
                except Exception:
                    if os.path.exists(dest_path):
                        os.unlink(dest_path)
                    raise

        # Final progress update with exact byte count
        job.processed_items = downloaded
        await self.save_job(job)

        return dest_path

    def _gdrive_file_id(self, url: str) -> str | None:
        """Extract a Google Drive file ID from share/view/uc URL formats."""
        m = re.search(r'drive\.google\.com/file/d/([a-zA-Z0-9_-]+)', url)
        if m:
            return m.group(1)
        if 'drive.google.com' in url:
            m = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
            if m:
                return m.group(1)
        return None

    async def _resolve_gdrive(self, file_id: str, client: httpx.AsyncClient) -> str:
        """
        Resolve a Google Drive file ID to a streamable direct-download URL.

        Google migrated file delivery from drive.google.com/uc to
        drive.usercontent.google.com. We try the new endpoint first, then the
        legacy one. A streaming probe is used instead of HEAD because Google's
        CDN can return different content-types for HEAD vs GET across redirects.
        Using the caller's client preserves cookies if Google sets any during
        the confirmation flow.
        """
        candidates = [
            f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
            f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t",
        ]

        for candidate in candidates:
            try:
                logger.info("GDrive probe: %s", candidate)
                async with client.stream("GET", candidate, timeout=30) as resp:
                    ct = resp.headers.get("content-type", "")
                    logger.info("GDrive probe response: status=%s content-type=%r final-url=%s",
                                resp.status_code, ct, str(resp.url))

                    if resp.status_code < 400 and "text/html" not in ct:
                        logger.info("GDrive resolved directly: %s", candidate)
                        return candidate

                    if resp.status_code < 400 and "text/html" in ct:
                        html = (await resp.aread()).decode("utf-8", errors="replace")
                        logger.info("GDrive HTML (first 3000 chars):\n%s", html[:3000])

                        if "quota exceeded" in html.lower():
                            raise ValueError(
                                "Google Drive download quota exceeded — this file has been downloaded "
                                "too many times today. Try again in 24 hours, or ask the owner to "
                                "share a fresh copy of the file."
                            )

                        parsed = self._parse_gdrive_html(file_id, html)
                        if parsed:
                            logger.info("GDrive parsed download URL: %s", parsed)
                            return parsed
                        logger.warning("GDrive: no download link found in HTML, trying next candidate")
            except ValueError:
                raise
            except Exception as exc:
                logger.warning("GDrive probe failed for %s: %s", candidate, exc)
                continue

        raise ValueError(
            "Could not resolve Google Drive download link — the file may be "
            "private or the link may have expired."
        )

    def _parse_gdrive_html(self, file_id: str, html: str) -> str | None:
        """Extract the real download URL from a Google Drive confirmation page."""
        # Pattern 0: element with id="uc-download-link" — Google's canonical download-anyway
        # anchor. Attribute order can vary, so try both orderings.
        for pat in (
            r'id=["\']uc-download-link["\'][^>]*\shref=["\']([^"\']+)["\']',
            r'href=["\']([^"\']+)["\'][^>]*\sid=["\']uc-download-link["\']',
        ):
            m = re.search(pat, html)
            if m:
                url = m.group(1).replace("&amp;", "&")
                if url.startswith("/"):
                    url = "https://drive.google.com" + url
                return url

        # Pattern 1: usercontent href with full session tokens
        m = re.search(
            r'href=["\']?(https://drive\.usercontent\.google\.com/download[^"\'>\s]*)',
            html,
        )
        if m:
            return m.group(1).replace("&amp;", "&")

        # Pattern 2: form action pointing at usercontent (hidden inputs carry the params)
        m = re.search(
            r'action=["\']?(https://drive\.usercontent\.google\.com/download[^"\'>\s]*)',
            html,
        )
        if m:
            base = m.group(1).replace("&amp;", "&")
            # Pull hidden input values and append them as query params
            inputs = dict(re.findall(r'<input[^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']*)["\']', html))
            inputs.update(dict(re.findall(r'<input[^>]+value=["\']([^"\']*)["\'][^>]+name=["\']([^"\']+)["\']', html)))
            # inputs dict might have keys reversed from second pattern — swap back
            # just collect all name/value pairs properly
            params = re.findall(r'<input[^>]+type=["\']hidden["\'][^>]*>', html)
            qs_parts: list[str] = []
            for tag in params:
                nm = re.search(r'name=["\']([^"\']+)["\']', tag)
                vl = re.search(r'value=["\']([^"\']*)["\']', tag)
                if nm and vl:
                    qs_parts.append(f"{nm.group(1)}={vl.group(1)}")
            if qs_parts:
                sep = "&" if "?" in base else "?"
                return base + sep + "&".join(qs_parts)
            return base

        # Pattern 3: old-style /uc?export=download href on drive.google.com
        m = re.search(r'href=["\']?(/uc\?export=download[^"\'>\s]*)', html)
        if m:
            return f"https://drive.google.com{m.group(1).replace('&amp;', '&')}"

        # Pattern 4: bare confirm token anywhere on the page
        m = re.search(r'[?&]confirm=([^&"\'<>\s]+)', html)
        if m and m.group(1) not in ("t", ""):
            token = m.group(1)
            return (
                f"https://drive.usercontent.google.com/download"
                f"?id={file_id}&export=download&confirm={token}"
            )

        return None

    def _extract_filename(self, response: httpx.Response, url: str) -> str:
        content_disposition = response.headers.get("content-disposition", "")
        if content_disposition:
            match = re.search(
                r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)',
                content_disposition,
                re.IGNORECASE,
            )
            if match:
                name = unquote(match.group(1).strip())
                if name:
                    return self._sanitize_filename(name)

        parsed = urlparse(url)
        path_part = parsed.path.split("/")[-1]
        if path_part:
            name = unquote(path_part)
            if "." in name:
                return self._sanitize_filename(name)

        return "archive.zip"

    def _sanitize_filename(self, name: str) -> str:
        name = re.sub(r'[<>:"/\\|?*]', "_", name)
        return name[:200]
