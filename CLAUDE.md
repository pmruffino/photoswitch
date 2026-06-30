# Photoswitch

Middleware that ingests photos + metadata from Google Photos (via Google Takeout)
into one or more Immich servers. Built as a small, modular, multi-tenant stack so
that worker stages can scale out independently and per-user jobs run in parallel.

This file is read automatically by Claude Code each session. Keep it current — it is
the single source of truth for architecture and locked decisions.

**Status:** Renamed/relaunched from the prior `immich-switch` codebase. All pipeline
stages are implemented; no backward compatibility with the old name was preserved
(internal naming — Redis namespace, Postgres DB, Immich `deviceId` — changed too,
since this rename happened before any production deployment under the new name).

---

## Goal

A user provides their Google Takeout export — either as a public share link or by
uploading the archive directly. The app fetches/receives it, unpacks it, maps Google's
JSON sidecar metadata onto the media files (EXIF/QuickTime), and uploads the result
into the user's chosen Immich server with correct timestamps, GPS, descriptions, and
album membership. A rollback stage can remove previously uploaded assets from Immich.

The whole thing runs as **one Docker Compose stack** on an Unraid server or other
Docker host.

---

## Architecture

A control plane (web app) plus a pipeline of lightweight worker pools that
communicate through Redis and a shared staging directory.

```
React SPA ──► FastAPI (control plane, 1 instance)
                 │            │
                 ▼            ▼
              Postgres      Redis ──► worker pools (N instances each)
            (durable)      (queues, semaphores, live config)
                                          │
                                          ▼
                                  /staging (shared volume)
```

### Pipeline stages (each an independent worker type)

1. **Fetcher** (`workers/fetcher.py`) — pulls the Takeout archive from the
   user-provided public link into a per-user staging path. Stateless, idempotent.
   If `auto_ingest=False`, parks the job after download instead of advancing.
2. **Unpacker** (`workers/unpacker.py`) — stream-extracts the `.tgz`/`.zip` into
   media files + `.json` sidecars. Streams where possible to avoid double-inflating.
3. **Metadata Mapper** (`workers/mapper/`) — THE CORE. Pairs each media file with
   its Google JSON sidecar and writes correct metadata via exiftool: timestamp, GPS,
   description. Records album membership for the Loader. Handles edge cases: Live
   Photos / motion photos, `-edited` variants vs originals, truncated/duplicated
   filenames, `supplemental-metadata` naming. Outputs `mapped_assets.json`.
4. **Loader** (`workers/loader.py`) — uploads assets via the Immich API
   (`POST /api/assets`), dedupes by checksum, creates/joins albums. Respects the
   optional `date_filter` on the job to selectively upload by date range.
5. **Rollback** (`workers/rollback.py`) — removes previously loaded assets from
   Immich. Reads `mapped_assets.json` from the source job's staging dir, uses
   `POST /api/assets/bulk-upload-check` to find existing assets by checksum, verifies
   ownership via `deviceId`/`deviceAssetId`, then batch-deletes with `force=true`
   (permanent delete, bypasses Immich's trash). Does NOT follow the main pipeline sequence — it has no
   `next` stage and `max_attempts=1`.

### Control plane

- **FastAPI** backend (`backend/`) — async, single instance. Handles auth, job
  creation, state reads/writes, and job enqueueing. Does no heavy lifting.
- **React + TypeScript SPA** (`frontend/`) — Tailwind CSS, full admin + user UI.
  Served by nginx which also reverse-proxies `/api/` to the backend.

### Workers

Plain Python, no web framework. Base loop in `workers/base_worker.py`:
acquire semaphore → BRPOP job → run `handle()` → advance to next stage or fail.
`workers/run_worker.py` selects the right class from `WORKER_TYPE` env var.
Worker image: `python:3.12-alpine` + exiftool.

---

## Docker Compose services

Images are prebuilt and published to GHCR by `.github/workflows/docker-publish.yml`
(triggered on push to `main` and on `v*` tags). `docker-compose.yml` pulls these
images directly — there is no `build:` context and no local Docker build at deploy
time. `IMAGE_TAG` (default `latest`) selects which published tag to pull.

| Service | Image | Networks |
|---|---|---|
| `postgres` | `postgres:16-alpine` | `photoswitch` only |
| `redis` | `redis:7-alpine` | `photoswitch` only |
| `backend` | `ghcr.io/pmruffino/photoswitch-backend` | `photoswitch` + `proxy` |
| `frontend` | `ghcr.io/pmruffino/photoswitch-frontend` | `photoswitch` + `proxy` |
| `worker-fetcher` | `ghcr.io/pmruffino/photoswitch-worker` | `photoswitch` + `proxy` |
| `worker-unpacker` | `ghcr.io/pmruffino/photoswitch-worker` | `photoswitch` + `proxy` |
| `worker-mapper` | `ghcr.io/pmruffino/photoswitch-worker` | `photoswitch` + `proxy` |
| `worker-loader` | `ghcr.io/pmruffino/photoswitch-worker` | `photoswitch` + `proxy` |
| `worker-rollback` | `ghcr.io/pmruffino/photoswitch-worker` | `photoswitch` + `proxy` |

All five worker services share one image (`photoswitch-worker`); `WORKER_TYPE` env
var picks the class at runtime — see `workers/run_worker.py`.

The `proxy` network is **external** (pre-existing on the Docker host, shared with
the reverse proxy). Backend and workers join it so they can make outbound HTTPS
calls to Immich servers that are behind the proxy on the same host. Postgres and
Redis are internal-only and must not join the proxy network.

Workers share a `x-worker-base` YAML anchor for DRY config.

---

## Locked decisions

- **Takeout input:** public share link (no Google OAuth). User pastes the link or
  uploads the archive directly via chunked multipart upload. Tradeoff accepted: user
  manages link lifecycle and should unshare after ingest.
- **Build approach:** from scratch (not wrapping immich-go).
- **Run model:** manual trigger. Designed so an external scheduler can kick jobs.
- **Multi-tenant:** jobs are keyed by user. Per-user worker scoping is supported.
- **Auth:** local accounts only. Argon2 password hashing. Sessions in Redis.
  New-user policy: `open` / `approval` / `closed` (admin-configurable).
- **Bootstrap:** the FIRST successful registration becomes admin, guarded by an
  "is the users table empty?" check. Cannot be hijacked afterward.
- **Roles:** `admin` and `user`.
  - `user` — connect Takeout source + Immich server, trigger jobs, view own status.
  - `admin` — everything a user can do, plus: manage users, set concurrency limits,
    configure staging retention and max archive size.
- **Immich connection:** user pastes their Immich server URL + API key generated in
  their own Immich account (Account Settings → API Keys). Stored encrypted in
  Postgres. All httpx calls to Immich use `verify=False, follow_redirects=True` so
  self-signed and proxied certs work without configuration.
- **Immich URL normalisation:** stored URLs strip trailing slashes, trailing `/api`,
  and default ports (`:443` for https, `:80` for http) so URLs are canonical.
- **Date filter:** jobs carry an optional `DateFilter` (after_date / before_date,
  inclusive). The Loader skips assets outside the range. Jobs with a date filter are
  exempt from staging cleanup so the user can adjust and re-run.
- **Job re-run:** a completed date-filtered Loader job can be re-queued directly to
  the LOAD stage with a new date filter, reusing the existing `mapped_assets.json`.
- **Rollback:** only available on jobs that reached `stage=load, status=succeeded`.
  Requires `mapped_assets.json` in the source job's staging dir. Uses
  `deviceId=photoswitch` and `deviceAssetId={job_id}_{filename}` to identify and
  verify ownership before deletion. `force=true` — assets are permanently deleted,
  bypassing Immich's trash. Creates a new rollback job each time; the source load job
  stays at `stage=load, status=succeeded` unchanged.
- **Multi-cycle load → rollback → rerun:** fully supported. After a rollback
  completes, call `/rerun` on the original load job to re-upload (the `deviceAssetId`
  is keyed to the original `job_id`, so rollback of a rerun still identifies assets
  correctly). Cycles can repeat indefinitely while `mapped_assets.json` is on disk.
- **Concurrent rollback + rerun limitation:** the state machine correctly blocks
  rollback while a rerun is in progress (source job status is `queued`/`running`, not
  `succeeded`). It does NOT block a rerun being triggered while a rollback is in
  progress, because the source load job never changes state during rollback. Avoid
  calling `/rerun` on a job whose rollback is still queued or running — the operations
  would race in Immich.
- **Concurrency control:** Redis semaphore per stage. Lua script atomically checks
  limit before incrementing. Admin page tunes limits live (no redeploy needed).
  Each worker registers itself in a Redis sorted set (`psw:workers:{stage}`) with a
  heartbeat timestamp; workers with no heartbeat in 45 s are considered gone. The
  semaphore limit is `max(1, round(live_worker_count × pct / 100))` where `pct` is
  the admin-configured percentage (1–100, default 100). The limit is recalculated
  whenever a worker registers, deregisters, or heartbeats, and immediately when the
  admin saves a new percentage. `calculate_semaphore_limit()` in `schemas.py` is the
  single formula used by both workers and the backend.
- **Secret storage:** Immich API keys encrypted at rest with Fernet derived from
  `APP_SECRET_KEY` env var. `crypto.py` in the backend; workers derive the same key
  using `hashlib.sha256`.
- **Datastores:**
  - **Postgres** — users, password hashes, roles, approval state, encrypted Immich
    credentials, job history (`job_records` table).
  - **Redis** — queues, semaphores, live config, sessions, live job state.
- **Live job state:** canonical job state lives in Redis (`psw:job:{id}`). Postgres
  `job_records` is updated at stage transitions for durable history. The dashboard
  reads from Redis for live progress.
- **Staging cleanup:** a background task in the backend runs daily at a configurable
  hour (default 3 AM UTC). Removes terminal jobs older than the retention window
  (default 7 days). Jobs with a date filter are excluded. Orphaned staging dirs
  (no DB record) are also pruned.
- **nginx DNS:** the frontend nginx uses `resolver 127.0.0.11 valid=10s` and a
  `set $backend_upstream` variable so it re-resolves the backend hostname after
  container restarts instead of caching the IP at startup.
- **Packaging:** one Docker Compose stack using prebuilt `ghcr.io/pmruffino/photoswitch-*`
  images (no local build context). `STAGING_PATH` is a **required** env var with no
  default — missing it fails fast at `docker compose up`. Copy `.env.example` to
  `.env` before first run.
- **Image publishing:** `.github/workflows/docker-publish.yml` builds and pushes
  `photoswitch-backend`, `photoswitch-frontend`, and `photoswitch-worker` to GHCR on
  every push to `main` (tag `latest`) and on `v*` tags (matching semver tag). Uses the
  repo's built-in `GITHUB_TOKEN` — no registry secrets to configure.

---

## Environment variables (required)

| Variable | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | Postgres password |
| `APP_SECRET_KEY` | Fernet key for encrypting Immich API keys |
| `STAGING_PATH` | Host path for the shared staging volume |

Optional: `POSTGRES_USER` (default `psw`), `WEB_PORT` (default `2273`),
`SESSION_TTL_SECONDS` (default `86400`), `IMAGE_TAG` (default `latest` — which
`ghcr.io/pmruffino/photoswitch-*` tag to pull).

---

## Deployment / environment notes (Unraid)

- Target host is Unraid (Docker host). Images are pulled from GHCR — `docker compose
  up -d` (or `--pull always`) is sufficient; no git-synced build step is required on
  the host itself. The repo can still be git-synced for the compose file and `.env`.
- Persistent container data at `/mnt/cache/appdata/photoswitch/<service>` to hit the
  NVMe cache pool directly and bypass FUSE/shfs. Postgres and Redis data dirs must
  stay on NVMe.
- The `proxy` external network is the reverse proxy network (e.g. Nginx Proxy
  Manager). Backend and workers must be on it to route outbound HTTPS calls to
  proxied Immich servers on the same host.
- If Immich is behind a proxy on the same host, add `extra_hosts` for the domain in
  a local `docker-compose.override.yml` — do not commit it. Static proxy network IPs
  in `extra_hosts` go stale on reboot; prefer `host-gateway` if NPM publishes 443 to
  the host, otherwise re-joining the proxy network (already done) is the correct fix.
- Dev laptop connects to stack services over the LAN.

---

## Conventions

- Secrets and connection strings come from env vars / `.env` (gitignored) — never
  hardcoded.
- `schemas.py` is the SINGLE source of truth for job shapes and Redis key contracts.
  Both backend and workers import it. Keep it free of FastAPI/SQLAlchemy deps.
- All outbound Immich HTTP calls (backend test endpoint + all workers) use
  `httpx.AsyncClient(verify=False, follow_redirects=True)`.
- Workers use `client.request("DELETE", ...)` not `client.delete(...)` when a DELETE
  needs a JSON body — httpx's `delete()` convenience method does not accept `json=`.
- Keep worker images minimal. The workers Dockerfile is shared across all five worker
  types; `WORKER_TYPE` env var selects the class in `run_worker.py`.
