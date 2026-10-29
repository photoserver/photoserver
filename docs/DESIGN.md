# Photo Server – Design Document

> Status: **Draft**
> Scope: server application, stage 1. The client application is out of scope here except for the API contract.

---

## 1. Purpose

A self-hosted server that maintains a central library of photos and videos for a small group of users.

- Users run a client application on one or more devices.
- A client checks which local files are already stored on the server and uploads the missing ones.
- The server organizes stored files into a folder structure based on the date the media was taken.
- The server analyzes photos (initially: face detection) and tags them. Tags are readable by clients.

The project is also a learning vehicle for modern Python. Performance is explicitly
**not** a priority; clarity, correctness and portability are.

## 2. Goals and non-goals

### Goals (stage 1)

- Check which files a user already has on the server (by content hash), for photos and videos.
- Resumable, chunked upload of missing files (videos can be several GB).
- Automatic organization into `<user>/<YYYY>/<MM>/` on a configured storage root.
- Background face detection for photos, producing a simple tag (`face` / `no_face`).
- Read-only access to tags for clients.
- Multiple users; each user may have multiple devices, each device with its own API key.
- Runs as a Windows service now, migratable to Linux without code changes.

### Non-goals (stage 1)

- Video analysis (videos are stored and organized, analysis is skipped).
- Editing tags from clients.
- Reacting to files deleted or changed directly on the storage (handled later by a reconciliation worker, see §13).
- Web UI, sharing between users, face recognition (identifying specific people).
- High throughput or horizontal scaling.

## 3. Key decisions (summary)

| # | Decision | Rationale |
|---|---|---|
| D1 | Modular monolith with layered structure (API → services → persistence/storage) | Simple to run and learn; layers can be split out later |
| D2 | File identity = SHA-256 of content | Independent of file names, paths and copy dates |
| D3 | Data ownership is per **user**; dedup key is `(user_id, sha256)` | A user with several devices sees one library |
| D4 | Devices authenticate with their own API keys bound to a user | Revoking one device does not affect others |
| D5 | Photos and videos share one model (`media_items` with `media_type`) | Check/upload/organize are identical; only analysis differs |
| D6 | Chunked, resumable upload protocol used for all media | Large videos, unreliable connections, one code path |
| D7 | Background work via a DB-backed job table + worker | No external broker (Redis/Celery) needed; Celery does not support Windows |
| D8 | Tags as a generic many-to-many model with `source` and `confidence` | New tag types without schema changes |
| D9 | Application is a plain console process; OS service wrapping is external | No Windows-specific code; WinSW/NSSM on Windows, systemd on Linux |
| D10 | Store relative paths only; use `pathlib` everywhere | Portability between Windows and Linux |
| D11 | Files deleted directly from storage have no effect on the app (for now) | Keeps stage 1 simple; reconciliation comes later |
| D12 | PostgreSQL from the start (Docker container in development); no SQLite | Avoids SQLite pitfalls (foreign keys off by default, no time zones, limited `ALTER TABLE`, file-level write locks with API + worker); same database as the Linux target |

## 4. Architecture overview

```
┌───────────────────┐   HTTPS / JSON (REST)   ┌──────────────────────────────────────────┐
│ Client app        │ ──────────────────────► │ API layer (FastAPI)                      │
│ (per device)      │                         │  auth: API key → device → user           │
│ - hashes files    │ ◄────────────────────── │  /media/check, /uploads/*, /media, /tags │
│ - uploads missing │                         ├──────────────────────────────────────────┤
│ - reads tags      │                         │ Service layer (domain logic)             │
└───────────────────┘                         │  ingest · organizer · metadata · tags    │
                                              │  users/devices · jobs                    │
┌───────────────────┐                         ├────────────────────┬─────────────────────┤
│ Admin CLI (Typer) │ ──── same services ───► │ Persistence        │ Storage             │
└───────────────────┘                         │ (SQLAlchemy, DB)   │ (filesystem root)   │
                                              └────────────────────┴─────────────────────┘
                                                        ▲
                                              ┌─────────┴──────────┐
                                              │ Worker             │
                                              │  job handlers:     │
                                              │  - detect_faces    │
                                              │  - (later) video   │
                                              │  - (later) reconcile│
                                              └────────────────────┘
```

**Rule:** the service layer knows nothing about HTTP, the CLI, or the operating system. API endpoints, CLI commands and worker handlers are thin adapters calling services.

## 5. Domain model

- **User** – a person; owns all media. Identified internally by a stable id and a stable `username`.
-
**Device** – a client installation (laptop, phone, …) belonging to one user. Holds one API key. Can be revoked independently.
- **Media item** – one stored file (photo or video) belonging to one user. Records which device uploaded it.
- **Upload session** – state of an in-progress chunked upload.
- **Tag** – a label (e.g. `face`, `no_face`) attached to media items with a source and confidence.
- **Job** – a unit of background work (e.g. detect faces for media item X).

## 6. File identity and deduplication

- The client computes **SHA-256** of each file's content (streamed, never loaded fully into memory).
- The server treats `(user_id, sha256)` as unique. The same file owned by two users is stored twice (physical copies; no shared blob store in stage 1).
- The server re-computes the hash after upload and rejects the upload if it does not match.
- Future: perceptual hashes (e.g. `imagehash`) to detect re-encoded/resized duplicates.

## 7. Upload protocol (chunked, resumable)

All media go through the same protocol.

```
POST /api/v1/uploads
     body: { sha256, size, original_name, media_type?, file_mtime? }
     → 201 { upload_id, chunk_size, received_bytes: 0 }
     → 409 if (user, sha256) already exists (returns existing media id)

PUT  /api/v1/uploads/{upload_id}?offset=N
     body: raw bytes (one chunk)
     → 200 { received_bytes }
     → 409 if offset != received_bytes (client must resync)

GET  /api/v1/uploads/{upload_id}
     → 200 { received_bytes, status }      # used to resume after interruption

POST /api/v1/uploads/{upload_id}/complete
     → 201 { media_id }                     # hash verified, file organized, jobs enqueued
     → 422 if size or hash mismatch

DELETE /api/v1/uploads/{upload_id}          # client aborts
```

Server-side rules:

- Chunks are appended to a temporary file in `<storage_root>/.inbox/<upload_id>.part`.
- The hash is computed incrementally while chunks arrive (or once at completion – performance is not critical).
- Only on successful completion is the file moved into the final location and the `media_items` row committed.
- Upload sessions expire (`expires_at`); a periodic cleanup removes stale `.part` files.
- A maximum file size is configurable.
- Content type is verified on the server (by inspecting the file, not trusting the extension).

## 8. Storage layout and organization

```
<storage_root>/
  .inbox/                         # temporary upload files
  <user_key>/
    <YYYY>/
      <MM>/
        <stored_file_name>
```

- `<user_key>` is derived from an immutable value (user id or the stable `username`), never from a display name.
- `<YYYY>/<MM>` comes from the media's `taken_at` (see §9).
- Stored file names are generated by the server (e.g. `YYYYMMDD_HHMMSS_<sha8>.<ext>`). Client file names are kept only in the DB as `original_name` and are never used directly as paths.
- The DB stores `rel_path` relative to `storage_root`. Absolute paths are never persisted.
- Moves within the same filesystem use atomic rename; across filesystems copy-then-delete.

## 9. Metadata extraction

| Media | Source of `taken_at` | Tool |
|---|---|---|
| Photo | EXIF `DateTimeOriginal` | Pillow (+ `pillow-heif` for HEIC) |
| Video | Container metadata (`creation_time` in MP4/MOV) | `ffprobe` (FFmpeg) via `subprocess`, JSON output |
| Fallback | File modification time sent by client (`file_mtime`), else upload time | – |

`taken_at_source` records which source was used (`exif`, `container`, `mtime`, `upload`) so uncertain dates can be found later.

## 10. Background processing

- Table `jobs` with `job_type`, optional `media_id`, `status` (`pending`, `running`, `done`, `failed`), `attempts`, `last_error`, `run_after`.
- A worker polls for pending jobs, claims one, runs the registered handler, and records the result. Failed jobs are retried with backoff up to a limit.
- Handlers are registered in a registry: `job_type → handler function`. Adding video analysis or reconciliation means adding a handler.
- Stage 1 job types: `detect_faces` (photos only). Videos get `analysis_status = skipped`.
- Periodic tasks (upload cleanup; later reconciliation) are scheduled with a simple loop or APScheduler.
- Initially the worker may run inside the API process as a background task; it can be split into a separate process/service without code changes to handlers.

## 11. Analysis: face detection

- Model: **OpenCV YuNet** (`cv2.FaceDetectorYN`), a small ONNX model, CPU-only, works on Windows and Linux.
- Output: number of detected faces with confidence. Stage 1 tags: `face` (≥1 face above threshold) or `no_face`.
- Threshold is configurable.
- Model files are versioned and their location configured; the model name/version is recorded as the tag `source` (e.g. `yunet-2023mar`).
- Future: InsightFace embeddings for recognizing specific people; video analysis by sampling frames with FFmpeg.

## 12. Tags

- `tags`: `id`, `name`, `category` (e.g. `faces`).
- `media_tags`: `media_id`, `tag_id`, `source` (`model:<name>` or later `user`), `confidence`, `created_at`.
- Re-running analysis replaces tags from the same source.
- Clients have read-only access in stage 1.

## 13. Storage consistency (future)

Files may be deleted or changed directly on the storage; in stage 1 the app does not react to this.

Consequence (accepted): if a file is removed from storage, `/media/check` still reports it as present, so clients will not re-upload it.

Planned reconciliation worker:

- Periodically walks the storage and compares with the DB.
- Cheap check: existence + size + mtime. Expensive check (less often): full hash.
- Writes findings to `integrity_issues` (`missing`, `modified`, `unknown_file`) and logs them.
- Later, `/media/check` may treat `missing` items as absent so clients re-upload them.

All data needed for this (`rel_path`, `size`, `sha256`, `file_mtime`) is stored from day one.

## 14. API overview (v1)

All endpoints are under `/api/v1`, require `Authorization: Bearer <api_key>`, and are scoped to the authenticated user.

| Method | Path | Purpose |
|---|---|---|
| POST | `/media/check` | Body: list of `{sha256, size}` (batch, e.g. ≤ 1000). Returns which are known (with media id and tags) and which are missing |
| POST/PUT/GET/DELETE | `/uploads…` | Chunked upload, see §7 |
| GET | `/media` | List user's media; filters: `tag`, `media_type`, date range; paginated |
| GET | `/media/{id}` | Media details incl. tags |
| GET | `/media/{id}/tags` | Tags of one item |
| GET | `/tags` | All known tags |
| GET | `/me` | Current user and device info |
| GET | `/health` | Liveness (no auth) |

The OpenAPI schema generated by FastAPI is the contract for the client application.

## 15. Security

- **API
  keys**: generated with `secrets.token_urlsafe(32)` with a recognizable prefix (e.g. `pk_`). Shown once at creation. The DB stores only `key_prefix` and `key_hash` (SHA-256; sufficient for high-entropy random tokens).
- Authentication dependency resolves key → device → user, rejects revoked devices and inactive users, updates `last_seen_at`.
- Every query is filtered by the authenticated user; no endpoint accepts a `user_id` from the client.
- Uploads: size limit, content verification, server-generated file names, paths never derived from client input.
- Transport: HTTPS required outside a trusted LAN, via a reverse proxy (e.g. Caddy) in front of the app.
- Administration (users, devices) via a server-side CLI only in stage 1.

## 16. Data model

```
users
  id PK, username UNIQUE, display_name, is_active, created_at

devices
  id PK, user_id FK→users, name, key_prefix, key_hash UNIQUE,
  created_at, last_seen_at, revoked_at

media_items
  id PK, user_id FK→users, uploaded_by_device_id FK→devices,
  media_type (photo|video), sha256, size, original_name, mime_type,
  taken_at, taken_at_source (exif|container|mtime|upload),
  rel_path, file_mtime, analysis_status (pending|done|skipped|failed),
  created_at
  UNIQUE (user_id, sha256)

upload_sessions
  id PK, user_id FK, device_id FK, sha256, expected_size, original_name,
  received_bytes, temp_path, status (open|completed|aborted|expired),
  created_at, expires_at

tags
  id PK, name UNIQUE, category

media_tags
  media_id FK, tag_id FK, source, confidence, created_at
  PK (media_id, tag_id, source)

jobs
  id PK, job_type, media_id FK NULL, status, attempts, last_error,
  run_after, created_at, updated_at

integrity_issues   (future)
  id PK, media_id FK NULL, rel_path, issue_type, details, detected_at, resolved_at
```

Schema changes are managed with Alembic migrations from the first table onward.

## 17. Administration CLI

Built with Typer, using the same service layer as the API.

```
photoserver user create <username> [--display-name ...]
photoserver user list
photoserver device add <username> "<device name>"   # prints the API key once
photoserver device list <username>
photoserver device revoke <device_id>
photoserver db upgrade                               # run migrations
photoserver serve                                    # start API (+ in-process worker)
photoserver worker                                   # start worker only
```

Future: device pairing with a one-time code.

## 18. Configuration and logging

- Configuration via
  **pydantic-settings**: environment variables and an optional `.env` file. No OS-specific config sources.
- Key settings (`STORAGE_ROOT` and `DATABASE_URL` are required, no defaults): `STORAGE_ROOT`, `DATABASE_URL`, `MAX_UPLOAD_SIZE`, `UPLOAD_CHUNK_SIZE`, `UPLOAD_SESSION_TTL`, `FACE_DETECTION_THRESHOLD`, `MODEL_DIR`, `FFPROBE_PATH`, `LOG_LEVEL`, `HOST`, `PORT`.
- `.env` is never committed; `.env.example` documents the keys. Secrets (database password) are never printed by the CLI or logged.
- Logging with the standard `logging` module to stdout (service wrappers and systemd capture it); optional file handler.

## 19. Deployment and portability

- The app is started as a normal process (`photoserver serve`). It contains no Windows-service or systemd-specific code.
- **Windows (stage 1):** wrapped as a service with WinSW or NSSM.
- **Linux (target):** systemd unit file; optionally a Docker image.
- Portability rules:
    - `pathlib` for all path handling; only relative paths in the DB.
    - Linux filesystems are case-sensitive; generated file names avoid case-only differences.
    - External tools (`ffprobe`) are located via configuration, not hard-coded paths.
- Database: PostgreSQL from stage 1. In development and on Windows it runs in a Docker container defined in `compose.yaml` (named volume for data, port bound to localhost). On Linux it may run natively or in the same container setup.
- Database schema is created only by Alembic migrations, never by `create_all()` in the application.

## 20. Technology stack

| Area | Choice | Notes |
|---|---|---|
| Language | Python 3.15 (3.14 fallback) | 3.15.0 released Oct 2026; verify binary wheels for OpenCV/onnxruntime before committing |
| Project & dependency management | uv | `pyproject.toml`, lock file, Python version management |
| Web framework | FastAPI + Uvicorn | Pin exact version (FastAPI is still 0.x) |
| Validation / settings | Pydantic v2, pydantic-settings | |
| ORM / migrations | SQLAlchemy 2.x, Alembic | |
| Database | PostgreSQL (Docker) | Driver: psycopg 3 (`psycopg[binary]`), URL scheme `postgresql+psycopg://` |
| Images | Pillow, pillow-heif | EXIF, HEIC |
| Video metadata | FFmpeg / ffprobe | External binary |
| Face detection | OpenCV (YuNet) | |
| Scheduling | APScheduler or simple loop | |
| CLI | Typer | |
| Quality | ruff, mypy, pytest, pre-commit | |
| Integration tests | testcontainers-python | Disposable PostgreSQL container per test run; requires Docker |
| IDE | IntelliJ IDEA Ultimate + Python plugin | FastAPI support, Database Tools, HTTP Client |
| Service wrapper | WinSW / NSSM (Windows), systemd (Linux) | |

## 21. Roadmap

1. Project skeleton: uv project, settings, logging, PostgreSQL in Docker, DB models, first Alembic migration, CLI for users/devices.
2. Authentication and `/media/check`.
3. Chunked upload, metadata extraction, organization.
4. Job table, worker, face detection, tags endpoints.
5. Packaging as a Windows service.
6. Later: reconciliation worker, video analysis, Linux migration, device pairing, face recognition.

## 22. Open questions

- Supported formats list (JPEG, PNG, HEIC, RAW?, MP4, MOV, …) and handling of unsupported types.
- Maximum file size and upload session TTL defaults.
- Should thumbnails/previews be generated and served to clients?
- Handling of media without any reliable date (separate `unknown/` folder vs. upload date).
- Retention of `jobs` and expired `upload_sessions` rows.
