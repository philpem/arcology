# Chunked uploads, async finalise, and deploy resilience

This document describes how Arcology handles large artefact uploads: the
resumable chunked-upload protocol, the asynchronous server-side *finalise*
step, and how an in-progress upload survives a web-container redeploy without
forcing the user to restart from the beginning.

It is the design-of-record and is kept in sync with the implementation. File
and function references point at the live code.

## Why chunked uploads exist

A single multipart `POST` is capped at `MAX_CONTENT_LENGTH` (4 GB) and is
bounded by the gunicorn worker timeout (`--timeout 300`, `Dentrypoint.sh`).
Retrocomputing hard-disk images routinely run to tens of gigabytes, so any
upload at or above `CHUNKED_UPLOAD_THRESHOLD` (default 100 MB) is split into
`CHUNKED_UPLOAD_CHUNK_SIZE` pieces (default 50 MB) and sent over the resumable
chunked protocol instead. Smaller files continue to use the plain multipart
path, which is synchronous and well under the timeout.

## The protocol

Both the API blueprint (API-key clients such as the `arco` CLI) and the web
blueprint (cookie-authenticated browser sessions) drive the same four-step
protocol. The filesystem mechanics live in `myapp/services/chunked_upload.py`
so the two blueprints stay thin.

```
init     POST .../chunked/init                       -> { upload_uuid }
chunk    POST .../chunked/<uuid>/chunk/<index>        -> { received, chunk }
status   GET  .../chunked/<uuid>/status              -> { received_chunks, total_chunks }
complete POST .../chunked/<uuid>/complete            -> 201 artefact  (sync)
                                                     or 202 { state, status_url }  (async)
fstatus  GET  .../chunked/<uuid>/complete/status     -> { state, artefact? | error? }
```

API routes are under `/api/uploads/chunked/...` (`myapp/blueprints/api.py`);
web routes under `/items/<item_id>/artefacts/chunked/...`
(`myapp/blueprints/artefacts.py`). They share `chunked_upload.py`.

### Where chunks live

Chunks are staged on the local filesystem under `<CHUNK_DIR>/<upload_uuid>/`
regardless of the active storage backend (local or S3). `CHUNK_DIR` (config
key / env var, default `<instance_path>/.chunks`) **must point at a persisted
volume** in any containerised deployment — see `chunks_base()` and the
docker-compose mount of `./data/chunks`. This is the substrate that makes
both resume (window A) and finalise re-drive (window B) possible across a
redeploy; without it a container recreate would discard everything.

The session directory holds the numbered chunk files plus a `meta.json`
describing the upload (filename, item, label, type, total_chunks, auto_analyse,
hints) and — once `/complete` is called in async mode — the finalise state
(below).

## Synchronous vs asynchronous finalise

`/complete` must assemble all chunks into one object, hash it (MD5 + SHA-256),
push it to the storage backend, and ingest it (artefact row + blob dedup +
analysis queue). For a 10 GB upload that is minutes of CPU/IO — far longer than
the 300 s request timeout — so doing it inline on the request thread produces a
504 *after* the bytes have all arrived (the original bug this subsystem fixes).

The fix is to run finalise **off the request thread** and have the client poll
for the result. To avoid breaking older `arco` installs that expect the
historical `201 + artefact` response, async finalise is **opt-in**:

- A client that sends `{"async": true}` in the `/complete` body gets an
  immediate `202 {upload_uuid, state, status_url}` and polls
  `/complete/status` until the artefact is ready or finalise fails.
- A client that omits the flag gets the **unchanged** synchronous behaviour
  (`201 + artefact`), bounded by the request timeout exactly as before.

The bundled CLI and web uploader both opt in. The synchronous path is retained
for backward compatibility and may be deprecated once old clients have aged
out.

### Finalise state machine

Finalise state is stored in the session's `meta.json` on the persisted
`CHUNK_DIR` volume — no database table, no migration; the chunk directory is
already the durable record (if the volume is lost, the chunks are gone too, so
a separate store would buy nothing). Updates are written via a temp-file +
`os.replace()` so they are atomic and crash-safe.

```
            +-----------+
            |  pending  |   (set when async /complete claims the session)
            +-----+-----+
                  | claim_finalize(): atomic pending -> assembling
                  v
            +-------------+   heartbeat_at bumped every FINALIZE_HEARTBEAT_SECONDS
            | assembling  |   while assemble+ingest run
            +--+-------+--+
       success |       | failure
               v       v
         +--------+  +--------+
         |  done  |  | failed |
         +--------+  +--------+
         artefact_uuid   error, error_code
```

Meta fields: `finalize_state`, `artefact_uuid`, `error`, `error_code`
(`too_large` | `storage` | `internal`), `attempts`, `heartbeat_at`.

### Server components (`chunked_upload.py`)

- `claim_finalize(upload_uuid) -> bool` — atomic compare-and-set. Succeeds only
  if state is `pending`, or `assembling` with `heartbeat_at` older than
  `FINALIZE_STALE_SECONDS` (the re-drive case). Guarded by a per-process lock
  keyed on `upload_uuid` plus the atomic meta rewrite.
- `run_finalize(upload_uuid, finalize_fn)` — the worker body: mark
  `assembling`, start a heartbeat ticker, call `assemble_to_storage()` then the
  blueprint-supplied `finalize_fn` (the ingest closure), then write
  `done`+`artefact_uuid` or `failed`+`error`/`error_code`. The heartbeat is
  always stopped in a `finally`.
- A lazily-created, module-level `ThreadPoolExecutor(max_workers=
  FINALIZE_CONCURRENCY)` (default 2) runs finalises off the request threads and
  bounds how many multi-GB assemblies run at once. Submitted jobs push their
  own Flask app context.

The blueprints construct `finalize_fn` because it captures request-derived
context (resolved `item`, artefact type, auth/queue mode) and the existing
`ingest_uploaded_artefact()` call; the runner itself stays blueprint-agnostic.

### Chunk-directory lifecycle

On **successful** finalise the numbered chunk files are deleted but the session
directory + `meta.json` (now `done`+`artefact_uuid`) are retained so
`/complete/status` can still answer. Completed/failed sessions are reaped by
`purge_stale_chunks()` once older than `FINALIZE_RESULT_TTL_SECONDS` (default
1 h), with the existing 24 h sweep as a fallback.

### Correctness: no double artefact

`ingest_uploaded_artefact()` always inserts a **new** Artefact row (blob dedup
dedups the *file*, not the row), so finalise must never run twice concurrently.
This is prevented by:

1. a single atomic `pending -> assembling` transition — only one runner wins;
2. the heartbeat — a live runner keeps the entry non-stale, so a concurrent
   poller's re-drive check will not fire;
3. re-drive only when `heartbeat_at` is older than `FINALIZE_STALE_SECONDS`
   (set well above the heartbeat interval), i.e. the previous runner is
   genuinely dead;
4. `done` short-circuits — a re-drive that finds `done` returns the recorded
   `artefact_uuid` instead of re-ingesting.

## Deploy resilience

A web-container **recreate** (deploy) destroys the container's writable layer
and kills every in-process thread, but the `CHUNK_DIR` volume persists. An
upload in progress can be in one of two windows.

### Window A — interrupted while uploading chunks

The chunks received so far are safe on the volume and `meta.json` is intact.
Resume = "ask the server which chunks it already has and send only the rest":

- **Web UI** resumes via `localStorage` (`sessionKey()` / `resumeGet/Set/Del`)
  so a resume survives even a browser restart, calls `/status`
  (`fetchReceived()`), and skips received chunks (`runChunkedUpload()`). The
  per-chunk retry loop (`sendChunk()`) retries network/5xx errors with capped
  backoff (7 attempts), riding out a short outage.
- **CLI** resumes via a `~/.config/arcology/resume.json` sidecar keyed by file
  identity (server + target item + path + size + mtime + chunk count). On
  `arco upload`, if a saved session exists the client inspects the server
  (`_resume_session()`): a finished session returns the artefact, an in-flight
  one skips chunks the server already holds, and a missing/failed one starts
  fresh. Per-chunk retry is patient (6 attempts, capped backoff) to outlast a
  redeploy gap.

Server guard: once finalise has started (`finalize_state` != `pending`), the
chunk-write route rejects further writes for that session; `/status` keeps
working throughout `pending` for resume.

### Window B — interrupted during finalise

The finalise background thread dies with the old container. The chunks and
`meta.json` (`assembling`, now-stale `heartbeat_at`) persist. Recovery is
**lazy re-drive on poll**: when the client's next `/complete/status` lands on
the new container and finds a stale `assembling` entry, the handler
`claim_finalize()`s it and resubmits finalise on a fresh pool thread. The user
never re-uploads. This is race-free across gunicorn workers (atomic claim +
stale-heartbeat check) and needs no leader election: a status poll reads the
same `meta.json` regardless of which worker answers, and a *live* finalise
keeps its heartbeat fresh so only a genuinely orphaned one is re-driven.

For this to work the client poll loops (CLI and web) must treat connection
errors during the outage as "keep waiting", not "fail".

### Graceful shutdown

On `SIGTERM` gunicorn drains in-flight HTTP requests for `--graceful-timeout`
(30 s). In-flight *chunk* POSTs (50 MB) drain comfortably. Finalise pool
threads are not HTTP requests, so gunicorn does not wait for them — a finalise
in progress at shutdown is simply orphaned and recovered by window-B re-drive.
We deliberately do not try to finish a multi-GB finalise within the stop grace
period (it cannot complete in 45 s anyway); re-drive is the recovery path.

### Redeploy walkthrough (10 GB file, 200 × 50 MB chunks)

| Deploy moment | What persists | Recovery |
|---|---|---|
| During chunk 73 | chunks 0–72 + meta on volume | client retries chunk 73 across the outage; resumes via `/status`, continues 74…199 — **no re-upload** |
| During `/complete` assembly | all chunks + meta (`assembling`, stale heartbeat) | client keeps polling; new container re-drives finalise; next poll returns the artefact — **no re-upload** |
| After `done`, before client polled | meta (`done` + `artefact_uuid`) on volume | client's poll on the new container returns the artefact |

## Configuration

| Key | Default | Purpose |
|---|---|---|
| `CHUNKED_UPLOAD_THRESHOLD` | 100 MB | size at/above which uploads go chunked |
| `CHUNKED_UPLOAD_CHUNK_SIZE` | 50 MB | per-chunk size |
| `MAX_UPLOAD_SIZE` | 16 GiB | authoritative cap on assembled size |
| `CHUNK_DIR` | `<instance>/.chunks` | staging dir; **persisted volume in Docker** |
| `FINALIZE_CONCURRENCY` | 2 | max concurrent finalises per web process |
| `FINALIZE_HEARTBEAT_SECONDS` | 15 | heartbeat cadence during assembly |
| `FINALIZE_STALE_SECONDS` | 120 | age of `heartbeat_at` before re-drive |
| `FINALIZE_RESULT_TTL_SECONDS` | 3600 | how long a `done`/`failed` session is retained |

## Implementation phases

1. **Server core** — meta state machine, `claim_finalize`, `run_finalize`,
   bounded pool, heartbeat, chunk-dir lifecycle change, config keys.
   Unit-tested in isolation; no endpoint behaviour change yet.
2. **Endpoints** — async branch on both `/complete`, the two `/complete/status`
   routes, chunk-write guard after finalise starts. Sync path preserved.
3. **CLI** — async opt-in + status poll loop; resume (persisted `upload_uuid`,
   `/status` skip, patient retry).
4. **Web JS** — async opt-in + status poll loop + "assembling" UI; harden
   resume retry patience.
5. **Docs + test hardening** — keep this document in sync; round out tests.

## Tests

- `ci/test_chunked_finalize.py` — the finalise core (state transitions;
  `claim_finalize` atomicity and stale re-drive; `run_finalize`
  success/failure meta writes; **a double-claim yields exactly one artefact** —
  the key correctness test; success retains meta but drops chunk files; pool
  execution) plus the HTTP async `/complete` + `/complete/status` routes
  (complete → poll → done, late-chunk 409, unknown-session 404).
- `ci/test_chunked_upload.py` — the existing synchronous path, which still
  returns `201 + artefact` (web: redirect): the old-client regression guard.
- `ci/test_cli_chunked.py` — the `arco` client against an in-memory fake of the
  protocol: async happy path, transient-error retry, resume-skips-received, and
  resume-returns-existing-artefact.
