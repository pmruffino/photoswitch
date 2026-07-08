# Photoswitch

Middleware that ingests photos + metadata from Google Photos (via Google Takeout) and
Apple iCloud (via Apple Data & Privacy export or a direct iCloud connection) into one
or more destination servers — Immich, or a WebDAV target (Nextcloud, ownCloud,
PhotoPrism). Built as a small, modular, multi-tenant stack so that worker stages can
scale out independently and per-user jobs run in parallel.

This file is read automatically by Claude Code each session. Keep it current — it is
the single source of truth for architecture and locked decisions.

**Status:** All pipeline stages are implemented. Sources: Google Takeout (link or
upload), Apple iCloud (direct connection + Apple Data & Privacy export bundle).
Destinations: Immich and WebDAV (Nextcloud / ownCloud / PhotoPrism). Periodic iCloud
sync is implemented via the backend scheduler. The dashboard is split into two
sections: **Image Destinations** (`frontend/src/components/DestinationsSection.tsx` —
a unified Immich + WebDAV list with an "Add destination" type picker: Immich,
Nextcloud, ownCloud, PhotoPrism, WebDAV-Other) and **Image Sources** (Apple iCloud via
`ICloudSection.tsx`, plus Google Takeout), followed by the **Imports** job table.

**Not yet verified against live third-party services:** the iCloud direct-connection
auth/2FA + photo pull (built against mainline `pyicloud` v2.6.5, API surface
confirmed by introspection but not exercised with a real Apple account) and the WebDAV
upload/COPY/rollback path (unit-tested against a mock transport, not yet against a live
Nextcloud/ownCloud/PhotoPrism). The Google Takeout → Immich path is the
battle-tested one.

---

## Goal

A user provides a photo library from one of two sources and the app imports it into
the user's chosen Immich server with correct timestamps, GPS, descriptions, and album
membership. A rollback stage can remove previously uploaded assets from Immich.

**Google Photos (implemented).** The user provides their Google Takeout export —
either as a public share link or by uploading the archive directly. The app
fetches/receives it, unpacks it, maps Google's JSON sidecar metadata onto the media
files (EXIF/QuickTime), and uploads the result.

**Apple iCloud (planned, `icloud` branch).** The user connects iCloud one of two ways:

1. **Direct cloud connection** — the app authenticates to iCloud (Apple ID sign-in
   with 2FA; there is no OAuth for iCloud Photos, so a session-based client is
   required) and pulls the photo library directly. Metadata (timestamps, GPS,
   descriptions, album membership) comes from the iCloud Photos API rather than JSON
   sidecars.
2. **Downloaded export bundle** — the user requests an export from the **Apple Data &
   Privacy** portal (privacy.apple.com → "Get a copy of your data"; Apple has no
   "Takeout"-style brand name for it), then uploads the resulting archive directly,
   the same way a Takeout archive is uploaded. The bundle carries metadata in Apple's
   own layout, which the mapper normalises before upload.

Both iCloud paths converge on the same Loader → Immich upload (and Rollback) as the
Google path. In addition, an iCloud **direct connection** import can be set up as a
recurring **periodic sync** so that new photos added to iCloud are imported into
Immich on a schedule — see Locked decisions.

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

1. **Fetcher** (`workers/fetcher.py`) — **source-aware**. For Google jobs it pulls
   the Takeout archive from the user-provided public link into a per-user staging path
   (stateless, idempotent; if `auto_ingest=False`, parks the job after download). For
   `ICLOUD_DIRECT` jobs it opens the connection's stored session (`icloud_client.py`),
   incrementally pulls photos newer than the connection's watermark straight into the
   extracted dir, writes `icloud_manifest.json`, and advances the watermark. Because a
   direct pull has no archive, its pipeline skips UNPACK (`next_stage()` in
   `schemas.py` routes `FETCH → MAP` for `ICLOUD_DIRECT`).
2. **Unpacker** (`workers/unpacker.py`) — stream-extracts the `.tgz`/`.zip` into
   media files + `.json` sidecars. Streams where possible to avoid double-inflating.
3. **Metadata Mapper** (`workers/mapper/`) — THE CORE. Pairs each media file with
   its Google JSON sidecar and writes correct metadata via exiftool: timestamp, GPS,
   description. Records album membership for the Loader. Handles edge cases: Live
   Photos / motion photos, `-edited` variants vs originals, truncated/duplicated
   filenames, `supplemental-metadata` naming. Outputs `mapped_assets.json`.
   Source-aware: `ICLOUD_DIRECT` jobs are mapped from the Fetcher's
   `icloud_manifest.json` (metadata came from the iCloud API, not sidecars);
   `ICLOUD_BUNDLE` (Apple Data & Privacy export) has no sidecars, so it flows through
   the same file-scan path as Google and relies on the existing embedded-EXIF fallback
   plus folder-name album detection.
4. **Loader** (`workers/loader.py`) — uploads assets to the job's destination.
   **Dispatches by `target.kind`** (see Destinations): for `immich` it uses the Immich
   API (`POST /api/assets`), dedupes by checksum, creates/joins albums; for `webdav`
   it uses `workers/webdav.py` to PUT files into folders. Respects the optional
   `date_filter` on the job to selectively upload by date range.
5. **Rollback** (`workers/rollback.py`) — removes previously loaded assets from the
   destination. **Dispatches by `target.kind`**: for `immich` it reads
   `mapped_assets.json`, uses `POST /api/assets/bulk-upload-check` to find assets by
   checksum, verifies ownership via `deviceId`/`deviceAssetId`, then batch-deletes with
   `force=true` (permanent, bypasses trash); for `webdav` it deletes the uploaded files
   by path. Does NOT follow the main pipeline sequence — it has no `next` stage and
   `max_attempts=1`.

### Control plane

- **FastAPI** backend (`backend/`) — async, single instance. Handles auth, job
  creation, state reads/writes, and job enqueueing. Does no heavy lifting.
  Built on Chainguard's hardened `cgr.dev/chainguard/python` images (two-stage:
  `-dev` variant to pip-install, distroless runtime variant to serve). Runs two
  background loops in its lifespan: the daily **staging cleanup** and the
  **periodic-sync scheduler** (`_sync_scheduler_loop` in `main.py`), which enqueues a
  fresh `ICLOUD_DIRECT` job whenever an iCloud connection's sync is enabled and due.
  Because the backend is single-instance, the in-progress iCloud 2FA service is held
  in a module dict between the connect and verify requests.
- **React + TypeScript SPA** (`frontend/`) — Tailwind CSS, full admin + user UI.
  Served by nginx which also reverse-proxies `/api/` to the backend. Built on
  Chainguard's `cgr.dev/chainguard/node` (build stage) and `cgr.dev/chainguard/nginx`
  (runtime) images. Chainguard's nginx runs as a non-root user, which can't bind
  privileged ports, so nginx listens on `2273` internally (not 80) — both
  `frontend/nginx.conf` and the `frontend` service's `ports:` mapping in
  `docker-compose.yml` map `${WEB_PORT:-2273}:2273`.

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
- **Source routing:** `Source` enum in `schemas.py` (`google_takeout`,
  `icloud_bundle`, `icloud_direct`) is carried on every `Job` and selects its stage
  pipeline via `PIPELINES` / `next_stage()`. Google and bundle use
  `FETCH→UNPACK→MAP→LOAD`; direct uses `FETCH→MAP→LOAD` (no archive to unpack).
  `Job.advance()` is source-aware; `Stage.next` is a back-compat shim for the Google
  order. Old serialized jobs with no `source` default to `google_takeout`.
- **iCloud input:** two modes, both feeding the same Loader as Google.
  - **Direct connection** — Apple ID sign-in with 2FA (no OAuth exists for iCloud
    Photos, so the mainline `pyicloud` library is used, borrowing icloudpd's session
    and incremental-pull patterns). Connect is a
    two-step flow: `POST /api/icloud/connections` starts auth; `POST
    /connections/{id}/verify` submits the 6-digit code. Apple pushes the code to
    trusted devices by default; if the user can't get it there, `POST
    /connections/{id}/send-sms` asks Apple to text it to the account's trusted phone
    number instead (`icloud_client.request_sms_code` → pyicloud `_request_sms_2fa_code`,
    which flips delivery state to `sms` so the same `/verify` → `validate_2fa_code`
    routes the texted code to the SMS verifier). Because the initial challenge is
    bootstrapped from Apple's HTML auth shell (trusted-device oriented, no phone
    numbers), `request_sms_code` first re-fetches the auth endpoint as JSON (Apple's
    SMS-oriented shape) and merges the `trustedPhoneNumbers` into the pending service's
    auth data — otherwise the SMS request fails with "no trusted number" even on
    accounts that have one. If a connect is interrupted before
    2FA finishes (or the single-instance backend restarts and loses the in-memory
    `_PENDING_AUTH` hold), the connection is left at `status=pending_2fa`; `POST
    /connections/{id}/restart` re-begins auth on that same row (re-storing the possibly
    edited Apple ID / password / label and sending a fresh code) so the user can resume
    without deleting and re-creating it. The same restart flow (UI label "Reconnect")
    also covers `status=needs_reauth` (Apple's ~2-month trust expiry) — there is no
    dead end that forces delete-and-recreate for any iCloud auth state. The resulting
    **trusted session** (a packed pyicloud cookie directory) and the Apple password are stored
    encrypted at rest on the `icloud_connections` row, reusing the same Fernet key as
    Immich credentials, so subsequent pulls skip 2FA. Apple expires trust ~every 2
    months; the connection is then flagged `needs_reauth`. Metadata comes from the
    iCloud API (not sidecars) and reaches the Mapper via `icloud_manifest.json`.
  - **Export bundle** — user requests an export from the Apple Data & Privacy portal
    (privacy.apple.com → "Get a copy of your data") and uploads the archive via the
    existing chunked-upload flow with `source=icloud_bundle`. Reuses unpack + map +
    load unchanged (Apple embeds metadata in-file, so no sidecar parsing is needed).
- **Periodic sync (iCloud direct connection only):** configured per connection via
  `PUT /api/icloud/connections/{id}/sync` (enabled, interval, Immich target). The
  backend scheduler (`_sync_scheduler_loop`, tick `SYNC_SCHEDULER_TICK_SECONDS` = 120 s)
  enqueues a normal `ICLOUD_DIRECT` fetch→map→load job whenever a connection is enabled
  and due; the Fetcher pulls only assets newer than the stored `watermark_ms` (Immich
  checksum-dedup on the Loader is the backstop). Interval is restricted to fixed
  presets `SYNC_INTERVAL_PRESETS` (15 min, 1/2/4/8/12 h, 1/2/3 day, 1 week; floor
  `SYNC_MIN_INTERVAL_MINUTES` = 15). A per-connection Redis lock plus DB
  `sync_last_run_at` prevent double-firing. There is **no dedicated sync worker** and
  no `SYNC` stage — recurrence is purely a control-plane scheduling concern; the
  Fetcher does the incremental pull and the Loader still uploads. `POST
  /connections/{id}/sync-now` runs the sync on demand (uses the configured sync
  target, updates `sync_last_run_at`). An import can be designated the sync **anchor**
  (`is_sync_anchor`, `POST /connections/{id}/import` with `as_sync_anchor=true`);
  anchor jobs are **exempt from staging cleanup** so the recurring sync's visible
  record survives the retention window. Export-bundle imports are one-shot and cannot
  be scheduled. The sync watermark lives on the connection row, so per-run staging
  cleanup never breaks an in-progress sync.
- **Incremental watermark is keyed on ADDED date, not capture date:** the Fetcher
  iterates iCloud's added-date index newest-first (`_iter_added_desc_photos`) and stops
  at the first asset added on/before `watermark_ms`; the watermark advances to the max
  *added-to-library* time seen. This MUST match the iteration key — using capture time
  would let an old photo added recently (a screenshot, a received/imported image) sort
  near the top with an old capture date and prematurely end the scan, skipping every
  newer-added photo below it. `_added_ms` (added time) drives the cutoff/watermark;
  `_asset_ms` (capture time) is used only for the EXIF `taken_at`. A missing/zero
  addedDate is treated as unknown so a stray record can't trigger the early break.
- **Sync teardown:** deleting a sync-anchor job (`DELETE /api/jobs/{id}`) cleanly
  removes the schedule — it disables `sync_enabled`, clears `anchor_job_id`, and drops
  the per-connection Redis lock on the owning `icloud_connections` row. Deleting the
  connection itself (`DELETE /api/icloud/connections/{id}`) also clears the lock. Both
  paths guarantee the scheduler cannot fire for a removed sync.
- **Build approach:** from scratch (not wrapping immich-go).
- **Run model:** manual trigger. Designed so an external scheduler can kick jobs.
- **Multi-tenant:** jobs are keyed by user. Per-user worker scoping is supported.
- **Auth:** local accounts only. Argon2 password hashing. Sessions in Redis.
  New-user policy: `open` / `approval` / `closed` (admin-configurable). Session cookie
  + Redis TTL are the same value (`SESSION_TTL_SECONDS`, default 24h, or 30 days with
  "remember me") so they expire together. **Session-expiry UX:** `AuthProvider` only
  checks `/auth/me` once at mount, so a session that expires while a tab stays open
  would otherwise fail silently and confusingly on whatever the user next clicks
  (surfacing the backend's generic `401 "Not authenticated"`, misread as a feature-
  specific error e.g. on an iCloud button). `frontend/src/api.ts`'s `req()` calls a
  registered `onSessionExpired` handler on any 401 (except `/auth/me` and
  `/auth/login`, which normally 401 for a logged-out visitor / bad password);
  `AuthProvider` clears `user` and sets `sessionExpired`, which routes to `/login` and
  shows "Your session expired. Please sign in again."
- **Bootstrap:** the FIRST successful registration becomes admin, guarded by an
  "is the users table empty?" check. Cannot be hijacked afterward.
- **Roles:** `admin` and `user`.
  - `user` — connect Takeout source + Immich server, trigger jobs, view own status.
  - `admin` — everything a user can do, plus: manage users, set concurrency limits,
    configure staging retention and max archive size.
- **Destinations:** a job uploads to a **destination**, identified by
  `Destination.kind` in `schemas.py` (`DestinationKind`: `immich` | `webdav`). The
  Loader and Rollback dispatch on `kind`; everything upstream (Fetcher/Unpacker/Mapper)
  is destination-agnostic and unchanged. Two kinds are supported:
  - **`immich`** — the original path (see Immich connection below).
  - **`webdav`** — covers **Nextcloud, ownCloud, and PhotoPrism** (and any WebDAV
    server) through one implementation. Credentials live in `webdav_destinations`
    (base URL, username, encrypted password/app-password, optional base folder — empty
    = the WebDAV root, and a
    `service` tag: `nextcloud` | `owncloud` | `photoprism` | `other`). The add form
    splits the **server** (`https://host`) from the **WebDAV path**; the path is
    auto-derived per service (`/remote.php/dav/files/<username>` for Nextcloud/ownCloud,
    `/originals` for PhotoPrism, editable free-text for `other`) and re-substitutes the
    username live for the fixed services. The Loader (`workers/webdav.py`) uploads the
    mapped files by `PUT` into folders; because the Mapper already bakes
    timestamp/GPS/description into each file's EXIF/QuickTime, that metadata travels
    with the upload and Nextcloud Memories/PhotoPrism index it — no destination-side
    metadata API needed.
    - **Albums are folder-based (v1)** and the layout is **uniform across services**,
      relative to the WebDAV URL's root (the user's files root for Nextcloud/ownCloud;
      the `originals` folder for PhotoPrism). The **base folder is optional and defaults
      to empty = the root**: album `A` → `{base_path}/A/` (or `A/` at the root when
      base_path is empty); un-albumed photos → `{base_path}/` (or the root itself).
    - **PhotoPrism specifics (verified against its docs):** WebDAV is mounted only at
      `/originals/` (and `/import/`), so the URL must contain `originals`. **PhotoPrism
      albums are virtual — there is no WebDAV path for them**, so what we write are
      plain *folders* under `originals` (PhotoPrism lists them under “Folders” and
      auto-indexes them); they are not true PhotoPrism albums. Native
      Nextcloud/PhotoPrism album *APIs* are out of scope for v1.
    - **A photo in multiple albums is uploaded once.** The bytes are `PUT` a single
      time to the first album's folder; membership in every other album is a
      **server-side WebDAV `COPY`** (no client re-upload). Folder albums do duplicate
      the file in server storage — unavoidable without native albums — but the
      network upload happens once. If a server rejects `COPY` (e.g. some PhotoPrism
      setups), the extra album membership is skipped with a warning rather than
      re-uploading. Re-runs are idempotent: existing target paths are skipped.
    - **No checksum dedup** (WebDAV has none); dedup is by target path/name plus the
      source-side watermark. **Rollback** deletes the uploaded files by path. Because
      dedup is by name, two *different* source photos that share a filename must not
      collide on one folder: the iCloud Fetcher gives each pulled file a collision-safe,
      per-asset-stable name (`icloud_client._unique_name` inserts a short token derived
      from the asset id when a name is already taken in the staging dir) so neither the
      staging write nor the WebDAV `PUT` overwrites/skips a distinct photo. (Known
      residual edge: two same-named distinct photos added in *different* sync windows can
      still land on the same clean remote name; if it bites, switch to always-unique
      names.)
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
    credentials (`immich_credentials`), encrypted WebDAV destinations
    (`webdav_destinations`: base URL, username, encrypted password, base upload
    folder), encrypted iCloud connections + sessions (`icloud_connections` table:
    Apple ID, encrypted password, encrypted trusted session, status, sync-watermark,
    sync schedule + target), job history (`job_records` table).
  - **Redis** — queues, semaphores, live config, sessions, live job state.
- **Schema patches (no Alembic):** the backend creates tables with
  `Base.metadata.create_all` on startup, which only creates *missing tables* — it never
  adds a column to a table that already exists. Because deployments keep a persistent
  Postgres volume, columns added to an existing table (e.g. `webdav_destinations.service`,
  added after v1.2.0-icloud) must be applied explicitly. `_apply_schema_patches` in
  `main.py` runs a short list of idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS`
  statements right after `create_all`; each is safe to run every startup and no-ops once
  applied. New non-nullable columns on existing tables must be added here (with a
  `DEFAULT`), not just on the model.
- **Partial-failure visibility (no silent drops):** stages that process many files
  never swallow per-item failures. The iCloud Fetcher retries transient downloads (with
  backoff) and falls back from `original` to Apple's full-res `alternative`; anything
  still undownloadable is collected in `PullResult.failures` and the watermark is held
  *below* the oldest failure so it retries next sync (rather than being skipped forever).
  The WebDAV client retries PUTs, checks their result, and `exists()` no longer follows
  redirects (an auth/misconfig redirect to a 200 login page must not masquerade as
  "already uploaded" and skip the PUT). Both stages summarise skipped/failed items into
  the job's non-fatal `warnings` field (`schemas.Job.warnings`), surfaced amber in the
  Imports table so partial data-loss is visible instead of silent. `warnings` lives in
  Redis live state only (not the durable `job_records` row). Two more silent-drop paths
  were closed the same way: the Mapper's iCloud-manifest path now reports (instead of
  only logging) any downloaded file that went missing from staging before mapping; and
  `WebDavClient.upload_asset` now returns per-asset failure notes distinguishing a
  **missing** asset (the primary PUT never landed — the file is absent entirely) from a
  **partial** one (the primary PUT succeeded but a COPY into an additional album
  failed) — the latter previously only hit the worker's log, so a photo could be fully
  uploaded yet silently absent from every album folder except its first, with zero
  visible warning.
- **Live job state:** canonical job state lives in Redis (`psw:job:{id}`). Postgres
  `job_records` is updated at stage transitions for durable history. The dashboard
  reads from Redis for live progress.
- **Staging cleanup:** a background task in the backend runs daily at a configurable
  hour (default 3 AM UTC). Removes terminal jobs older than the retention window
  (admin-selectable, default 7 days). Jobs with a date filter are excluded, as are
  **sync-anchor jobs** (`is_sync_anchor`) so a recurring iCloud sync keeps its visible
  record. Ordinary per-run sync jobs are cleaned normally — the sync watermark lives
  on the `icloud_connections` row, not in staging, so this never breaks a sync.
  Orphaned staging dirs (no DB record) are also pruned.
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
