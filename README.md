# Photoswitch

Middleware that ingests photos and metadata from a Google Takeout export into one or more [Immich](https://immich.app) servers. Runs as a self-contained Docker Compose stack using prebuilt images from GHCR.

## How it works

A user provides their Google Takeout archive — either as a public share link or by uploading the file directly — and selects which Immich server to upload to. The app processes the archive through a five-stage pipeline:

1. **Fetch** — Downloads the Takeout archive from the public link (or receives a direct upload)
2. **Unpack** — Extracts the `.zip` or `.tgz` archive
3. **Map** — Pairs each media file with its Google JSON sidecar, writes correct EXIF/QuickTime timestamps, GPS coordinates, and descriptions using `exiftool`, and detects Live Photos. Album membership is derived from the Takeout folder structure: Google exports album copies into named subdirectories alongside year-rollup folders (`Photos from YYYY`). Any folder that isn't a year-rollup folder is treated as an album name.
4. **Load** — Uploads assets to Immich via its REST API, deduplicates by checksum, and creates albums. Supports an optional date range filter to selectively upload assets by date.
5. **Rollback** — Removes previously uploaded assets from Immich. Verifies ownership before deleting so pre-existing duplicates in Immich are never affected.

Each stage runs as an independent pool of worker containers communicating through Redis queues. The control plane (FastAPI + React) manages users, credentials, and job dispatch. Progress updates automatically in the Dashboard while jobs are running.

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

### 4. Connect Immich

In the Dashboard, add an Immich connection:
- **Server URL** — e.g. `https://immich.example.com`
- **API Key** — generate one in Immich under *Account Settings → API Keys*

Use **Test** to verify the connection before starting a job.

### 5. Run an import

Click **Add bundle**, then either:
- Paste a Google Takeout public download link, or
- Upload the archive file directly

Select your Immich connection, optionally set a date range filter, and start. Progress updates live in the Dashboard.

> **Note:** Google Takeout public links expire. Unshare the link in Google Drive after the import finishes.

---

## Environment variables

Copy `.env.example` to `.env` and fill in all values. **All required variables must be set** — the stack will fail immediately at startup if any are missing rather than silently using wrong defaults.

| Variable | Required | Default | Description |
|---|---|---|---|
| `POSTGRES_PASSWORD` | **Yes** | — | Password for the PostgreSQL user. Use a strong random string. |
| `APP_SECRET_KEY` | **Yes** | — | 32-byte hex key used to encrypt Immich API keys at rest. Generate with: `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `STAGING_PATH` | **Yes** | — | Absolute host path for the shared staging volume (downloads, extracts, mapped files). All worker and backend containers mount this same path. On Unraid, use an NVMe cache path, e.g. `/mnt/cache/appdata/photoswitch/staging`. |
| `POSTGRES_USER` | No | `psw` | PostgreSQL username. |
| `WEB_PORT` | No | `2273` | Host port the web UI is exposed on. |
| `SESSION_TTL_SECONDS` | No | `86400` | Login session lifetime in seconds (default 24 h). |
| `IMAGE_TAG` | No | `latest` | Tag of the `ghcr.io/pmruffino/photoswitch-*` images to pull (e.g. a version tag published by the publish workflow). |

---

## Networking

The backend, frontend, and all workers are connected to **two** Docker networks:

- **`photoswitch`** (default, internal) — used for all intra-stack communication (backend ↔ postgres, backend ↔ redis, workers ↔ redis, frontend ↔ backend).
- **`proxy`** (external) — allows the backend and workers to make outbound HTTPS calls to Immich servers that are behind the reverse proxy on the same Docker host.

Postgres and Redis are on the internal network only and are not exposed to the proxy network.

If your Immich server is behind a reverse proxy on the same host, you may also need an `extra_hosts` entry in a `docker-compose.override.yml` to resolve the domain name to the proxy container's IP:

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

### Date range filtering

When starting a job or re-running the load stage, you can specify an optional date range. Only assets whose timestamp falls within the range are uploaded. Optionally include or exclude assets that have no date metadata at all. Jobs with a date filter are excluded from automatic staging cleanup so you can re-run with a different filter without re-downloading.

### Job re-run

A completed load job with a date filter can be re-queued directly to the load stage with an adjusted date range — no re-download or re-map needed.

### Rollback

Any completed load job can be rolled back: the app identifies assets uploaded by that specific job (by device ID and checksum), verifies ownership, and permanently removes them from Immich. Pre-existing duplicates that were in Immich before the job ran are never touched. Rollback and re-run are independent operations — you can roll back, then re-run (with a different date range if desired), and back again.

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
cp ../schemas.py .

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
cp ../schemas.py .

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
├── schemas.py               # Shared contracts (Job, Stage, Redis keys) — single source of truth
├── docker-compose.yml
├── .env.example
├── .github/workflows/
│   └── docker-publish.yml   # Builds + pushes the three images to ghcr.io/pmruffino
├── backend/                 # FastAPI control plane
│   ├── main.py              # App setup, startup tasks, scheduled cleanup
│   ├── models.py            # SQLAlchemy ORM: User, ImmichCredential, JobRecord
│   ├── auth.py              # Argon2 hashing, Redis session tokens
│   ├── crypto.py            # Fernet encryption for Immich API keys
│   ├── routers/
│   │   ├── auth_router.py   # Login, logout, register, session
│   │   ├── user_router.py   # Profile, Immich credentials (add/test/remove)
│   │   ├── jobs_router.py   # Job create, list, resume, rerun, rollback, delete
│   │   └── admin_router.py  # User management, concurrency limits, config
│   └── Dockerfile
├── workers/                 # All five workers share one Docker image; WORKER_TYPE selects which runs
│   ├── run_worker.py        # Entry point — reads WORKER_TYPE and starts the right class
│   ├── base_worker.py       # Redis semaphore, BRPOP loop, retry logic, DB sync
│   ├── fetcher.py           # Downloads Takeout archive from public link
│   ├── unpacker.py          # Extracts archive to staging directory
│   ├── loader.py            # Immich API upload, dedup, album creation, date filtering
│   ├── rollback.py          # Ownership-verified asset removal from Immich
│   ├── mapper/
│   │   ├── mapper.py        # Core: sidecar pairing, Live Photo detection, exiftool writes, SHA-1
│   │   ├── sidecar.py       # Google Photos JSON sidecar parser
│   │   └── exiftool.py      # exiftool subprocess wrapper
│   └── Dockerfile
└── frontend/                # React 18 + TypeScript + Tailwind CSS
    ├── src/
    │   ├── api.ts           # Typed API client
    │   ├── contexts/auth.tsx
    │   ├── pages/           # Login, Register, Dashboard, Admin, Profile
    │   └── components/      # Layout (nav bar)
    ├── nginx.conf           # Serves SPA, proxies /api/ to backend with Docker DNS re-resolution
    └── Dockerfile
```

## License

MIT
