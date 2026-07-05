# Photoswitch

Middleware that migrates photos **and their metadata** from **Google Photos** or **Apple iCloud** into one or more [Immich](https://immich.app) servers or **WebDAV** destinations (Nextcloud, ownCloud, PhotoPrism). Runs as a self-contained Docker Compose stack using prebuilt images from GHCR.

## Sources and destinations

**Sources** (where photos come from):
- **Google Photos** via [Google Takeout](https://takeout.google.com) — paste a public Drive share link or upload the archive directly.
- **Apple iCloud — direct connection** — sign in with your Apple ID + a one-time 2FA code; the trusted session is stored encrypted and reused, with support for incremental pulls and scheduled **periodic sync**.
- **Apple iCloud — export bundle** — upload an archive from [privacy.apple.com](https://privacy.apple.com) → *Get a copy of your data*.

**Destinations** (where photos go):
- **Immich** — uploads via the Immich API, dedupes by checksum, creates/joins albums.
- **WebDAV** — one integration covering **Nextcloud, ownCloud, and PhotoPrism** (or any WebDAV server). Photos upload into folders; metadata written into each file's EXIF is indexed automatically by Nextcloud Memories / PhotoPrism. Albums are folder-based, and a photo in several albums is uploaded once (extra album membership uses a server-side copy, not a re-upload).

## How it works

A user connects a source and a destination, then runs a job that flows through a pipeline of independent worker pools communicating over Redis queues:

1. **Fetch** — Downloads a Takeout archive from a public link, receives a direct upload, or (for iCloud direct) pulls new photos over the iCloud API into staging.
2. **Unpack** — Extracts the `.zip`/`.tgz` archive. (Skipped for an iCloud direct pull, which has no archive.)
3. **Map** — Writes correct EXIF/QuickTime timestamps, GPS, and descriptions using `exiftool`, detects Live Photos, and records album membership. Google metadata comes from JSON sidecars (album names from the Takeout folder structure — any folder that isn't a `Photos from YYYY` year-rollup is treated as an album); iCloud direct metadata comes from a manifest the Fetcher writes; export bundles rely on embedded EXIF.
4. **Load** — Uploads assets to the job's destination. For **Immich**, via its REST API with checksum dedup and album creation; for **WebDAV**, by uploading files into folders. Supports an optional date-range filter.
5. **Rollback** — Removes a job's previously uploaded assets from the destination. For Immich it verifies ownership so pre-existing duplicates are never affected; for WebDAV it deletes the uploaded files by path.

Which stages a job visits depends on its source, and the Loader/Rollback dispatch on the destination type. Recurrence for periodic iCloud sync is driven by a scheduler in the backend — there is no separate sync worker. The control plane (FastAPI + React) manages users, credentials, connections, and job dispatch; progress updates live in the Dashboard.

## Stack

| Service | Image |
|---|---|
| `photoswitch_frontend` | `ghcr.io/pmruffino/photoswitch-frontend` — React SPA + nginx |
| `photoswitch_backend` | `ghcr.io/pmruffino/photoswitch-backend` — FastAPI + SQLAlchemy |
| `photoswitch_postgres` | `postgres:16-alpine` |
| `photoswitch_redis` | `redis:7-alpine` |
| `photoswitch_worker_fetcher` | `ghcr.io/pmruffino/photoswitch-worker` (`WORKER_TYPE=fetcher`) |
| `photoswitch_worker_unpacker` | `ghcr.io/pmruffino/photoswitch-worker` (`WORKER_TYPE=unpacker`) |
| `photoswitch_worker_mapper` | `ghcr.io/pmruffino/photoswitch-worker` (`WORKER_TYPE=mapper`) |
| `photoswitch_worker_loader` | `ghcr.io/pmruffino/photoswitch-worker` (`WORKER_TYPE=loader`) |
| `photoswitch_worker_rollback` | `ghcr.io/pmruffino/photoswitch-worker` (`WORKER_TYPE=rollback`) |

Images are built and published to [GHCR](https://github.com/pmruffino/photoswitch/pkgs/container/photoswitch-backend) by the `.github/workflows/docker-publish.yml` workflow on every push to `main`. The Compose stack pulls these images directly — there is no local build step.

## Prerequisites

- Docker with Compose V2 (`docker compose` — note: no hyphen)
- A pre-existing external Docker network named `proxy` (used by the reverse proxy on your host). If you use Nginx Proxy Manager or Traefik, this network already exists.
- `exiftool` is included in the worker image; nothing needed on the host

## Quick start

### 1. Download the two required files

```bash
curl -O https://raw.githubusercontent.com/pmruffino/photoswitch/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/pmruffino/photoswitch/main/.env.example
```

Or download them manually from the repository. You do not need to clone the repo — the images are pulled from GHCR automatically.

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in all required values (see [Environment variables](#environment-variables) below). The stack will not start if required variables are missing.

### 3. Pull and start

```bash
docker compose up -d
```

The web UI will be available at `http://<host>:<WEB_PORT>` (default port 2273).

### 3. First login

The first account registered automatically becomes the admin. All subsequent registrations are governed by the **New User Policy** set in the Admin panel (`open` / `approval` / `closed`).

### 4. Add a destination

In the Dashboard, add at least one destination and use **Test** to verify it:

- **Immich** — Server URL (e.g. `https://immich.example.com`) + an API key generated in Immich under *Account Settings → API Keys*.
- **WebDAV** (Nextcloud / ownCloud / PhotoPrism) — WebDAV URL (Nextcloud/ownCloud: `…/remote.php/dav/files/<user>`), username, and password (prefer an app-password), plus an upload folder.

### 5. Run an import

- **Google Takeout** — click **Add bundle**, then paste a public download link or upload the archive. Pick a destination, optionally set a date range, and start.
  > Google Takeout public links expire — unshare the link in Google Drive after the import finishes.
- **Apple iCloud (direct)** — in the **Apple iCloud** section, click **Connect iCloud**, enter the 2FA code Apple sends to your device, then **Import now**. To sync on a schedule, mark the import as a *sync anchor* and open **Configure sync** (15 min up to 1 week); **Sync now** runs it on demand.
- **Apple export bundle** — upload the archive from privacy.apple.com in the Apple iCloud section.

Progress updates live in the **Imports** table, where you can also resume, adjust-and-rerun, or roll back a job.

---

## Environment variables

Copy `.env.example` to `.env` and fill in all values. **All required variables must be set** — the stack will fail immediately at startup if any are missing rather than silently using wrong defaults.

| Variable | Required | Default | Description |
|---|---|---|---|
| `POSTGRES_PASSWORD` | **Yes** | — | Password for the PostgreSQL user. Use a strong random string. |
| `APP_SECRET_KEY` | **Yes** | — | 32-byte hex key used to encrypt secrets at rest (Immich API keys, WebDAV passwords, and the iCloud password + trusted session). Generate with: `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `STAGING_PATH` | **Yes** | — | Absolute host path for the shared staging volume (downloads, extracts, mapped files). All worker and backend containers mount this same path. On Unraid, use an NVMe cache path, e.g. `/mnt/cache/appdata/photoswitch/staging`. |
| `POSTGRES_USER` | No | `psw` | PostgreSQL username. |
| `WEB_PORT` | No | `2273` | Host port the web UI is exposed on. |
| `SESSION_TTL_SECONDS` | No | `86400` | Login session lifetime in seconds (default 24 h). |
| `IMAGE_TAG` | No | `latest` | Tag of the `ghcr.io/pmruffino/photoswitch-*` images to pull (e.g. a version tag published by the publish workflow). |

---

## Networking

The backend, frontend, and all workers are connected to **two** Docker networks:

- **`photoswitch`** (default, internal) — used for all intra-stack communication (backend ↔ postgres, backend ↔ redis, workers ↔ redis, frontend ↔ backend).
- **`proxy`** (external) — allows the backend and workers to make outbound HTTPS calls to Immich or WebDAV destinations that are behind the reverse proxy on the same Docker host.

Postgres and Redis are on the internal network only and are not exposed to the proxy network.

If your destination server is behind a reverse proxy on the same host, you may also need an `extra_hosts` entry in a `docker-compose.override.yml` to resolve the domain name to the proxy container's IP:

```yaml
# docker-compose.override.yml  (do not commit — host-specific)
services:
  backend:
    extra_hosts:
      - "immich.example.com:host-gateway"
  worker-loader:
    extra_hosts:
      - "immich.example.com:host-gateway"
  worker-rollback:
    extra_hosts:
      - "immich.example.com:host-gateway"
```

---

## Adapting for non-Unraid hosts

The persistent volume mounts for Postgres and Redis in `docker-compose.yml` use Unraid-specific paths:

```yaml
- /mnt/cache/appdata/photoswitch/postgres:/var/lib/postgresql/data
- /mnt/cache/appdata/photoswitch/redis:/data
```

Replace the host-side path (left of the `:`) with any writable directory on your system:

```yaml
# Generic Linux / macOS / WSL2
- ./data/postgres:/var/lib/postgresql/data
- ./data/redis:/data
```

The staging volume is controlled by the `STAGING_PATH` env var and requires no changes to the compose file.

---

## Concurrency tuning

Each pipeline stage has a Redis semaphore controlling how many jobs run simultaneously. Adjust limits live in the **Admin → Settings** panel without restarting any containers. The defaults are:

| Stage | Default concurrent jobs |
|---|---|
| Fetch | 2 |
| Unpack | 2 |
| Map | 4 |
| Load | 4 |
| Rollback | 2 |

---

## Features

### Apple iCloud sync

Connect an Apple ID (with a one-time 2FA code) and import your iCloud library directly — no manual export needed. The trusted session is stored encrypted and reused until Apple expires it. An import can be designated the **anchor** for a recurring sync, scheduled at a fixed frequency (15 min · 1/2/4/8/12 h · 1/2/3 day · 1 week); each run pulls only photos added since the last one. **Sync now** triggers a run on demand. Deleting the anchor job cleanly stops the schedule without touching your connection or already-uploaded photos.

### WebDAV destinations

Upload to Nextcloud, ownCloud, or PhotoPrism (or any WebDAV server) instead of — or alongside — Immich. Metadata is carried in each file's EXIF, so it's indexed automatically. Albums map to folders; a photo in multiple albums is uploaded once and copied server-side into the other album folders.

### Date range filtering

When starting a job or re-running the load stage, you can specify an optional date range. Only assets whose timestamp falls within the range are uploaded. Optionally include or exclude assets that have no date metadata at all. Jobs with a date filter are excluded from automatic staging cleanup so you can re-run with a different filter without re-downloading.

### Job re-run

A completed load job with a date filter can be re-queued directly to the load stage with an adjusted date range — no re-download or re-map needed.

### Rollback

Any completed load job can be rolled back: the app identifies the assets that job uploaded and removes them from the destination. For **Immich** it matches by device ID + checksum and verifies ownership, so pre-existing duplicates are never touched; for **WebDAV** it deletes the uploaded files by path. Rollback and re-run are independent operations — you can roll back, then re-run (with a different date range if desired), and back again.

---

## Publishing images

`.github/workflows/docker-publish.yml` builds and pushes three images to `ghcr.io/pmruffino/`:

- `photoswitch-backend` (built from `backend/Dockerfile`, repo root context)
- `photoswitch-frontend` (built from `frontend/Dockerfile`, `frontend/` context)
- `photoswitch-worker` (built from `workers/Dockerfile`, repo root context — shared by all five worker types via `WORKER_TYPE`)

It runs on every push to `main` (tagging `latest`) and on `v*` tags (tagging the matching semver). It can also be triggered manually via `workflow_dispatch`. No secrets need to be configured — it authenticates with the repo's built-in `GITHUB_TOKEN`, which has package-write permission via the `packages: write` permission declared in the workflow.

---

## Development

### Backend

```bash
cd backend
pip install -r requirements.txt
cp ../schemas.py ../icloud_client.py .

DATABASE_URL=postgresql+asyncpg://psw:password@localhost:5432/photoswitch \
REDIS_URL=redis://localhost:6379/0 \
APP_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
STAGING_ROOT=/tmp/psw-staging \
uvicorn main:app --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev   # http://localhost:5173
```

API calls go to `/api/*`. Point them at your running backend by adding a proxy to `vite.config.ts`:

```ts
server: {
  proxy: {
    '/api': 'http://localhost:8000',
  },
},
```

### Workers

```bash
cd workers
pip install -r requirements.txt
cp ../schemas.py ../icloud_client.py .

WORKER_TYPE=mapper \
REDIS_URL=redis://localhost:6379/0 \
DATABASE_URL=postgresql://psw:password@localhost:5432/photoswitch \
APP_SECRET_KEY=<your-key> \
STAGING_ROOT=/tmp/psw-staging \
python run_worker.py
```

Valid `WORKER_TYPE` values: `fetcher`, `unpacker`, `mapper`, `loader`, `rollback`.

---

## Project structure

```
photoswitch/
├── schemas.py               # Shared contracts (Job, Source, Stage, Destination, Redis keys) — single source of truth
├── icloud_client.py         # Shared iCloud client (pyicloud): 2FA/session, incremental pull, manifest
├── docker-compose.yml
├── .env.example
├── .github/workflows/
│   └── docker-publish.yml   # Builds + pushes the three images to ghcr.io/pmruffino
├── backend/                 # FastAPI control plane
│   ├── main.py              # App setup, startup tasks, staging cleanup + iCloud sync scheduler
│   ├── models.py            # ORM: User, ImmichCredential, WebDavDestination, ICloudConnection, JobRecord
│   ├── auth.py              # Argon2 hashing, Redis session tokens
│   ├── crypto.py            # Fernet encryption for secrets at rest
│   ├── destinations.py      # Resolve (kind, id) → Immich/WebDAV destination
│   ├── routers/
│   │   ├── auth_router.py   # Login, logout, register, session
│   │   ├── user_router.py   # Profile, Immich + WebDAV destinations (add/test/remove)
│   │   ├── jobs_router.py   # Job create, list, resume, rerun, rollback, delete
│   │   ├── icloud_router.py # iCloud connect/2FA, direct import, periodic-sync config, sync-now
│   │   └── admin_router.py  # User management, concurrency limits, config
│   └── Dockerfile
├── workers/                 # All five workers share one Docker image; WORKER_TYPE selects which runs
│   ├── run_worker.py        # Entry point — reads WORKER_TYPE and starts the right class
│   ├── base_worker.py       # Redis semaphore, BRPOP loop, retry logic, DB sync
│   ├── fetcher.py           # Source-aware: Takeout download / upload / iCloud direct pull
│   ├── unpacker.py          # Extracts archive to staging directory
│   ├── loader.py            # Destination-aware upload (Immich API / WebDAV), dedup, albums, date filtering
│   ├── rollback.py          # Destination-aware asset removal (Immich ownership-verified / WebDAV by path)
│   ├── webdav.py            # WebDAV client: folder albums, single-upload + server-side COPY, delete
│   ├── mapper/
│   │   ├── mapper.py        # Core: sidecar/manifest pairing, Live Photo detection, exiftool writes, SHA-1
│   │   ├── sidecar.py       # Google Photos JSON sidecar parser
│   │   └── exiftool.py      # exiftool subprocess wrapper
│   └── Dockerfile
└── frontend/                # React 18 + TypeScript + Tailwind CSS
    ├── src/
    │   ├── api.ts           # Typed API client
    │   ├── contexts/auth.tsx
    │   ├── pages/           # Login, Register, Dashboard, Admin, Profile
    │   └── components/      # Layout, ICloudSection, WebDavSection
    ├── nginx.conf           # Serves SPA, proxies /api/ to backend with Docker DNS re-resolution
    └── Dockerfile
```

## License

MIT
