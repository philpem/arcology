# Codebase Review - Arcology

**Date:** 2026-03-07 (updated for commits 2192b35..6e2c091)
**Scope:** Full codebase review, updated for latest master
**Branch:** master @ 6e2c091

---

## Summary

Strong progress continues. Two security issues reported in the previous review (timing-safe comparison for WORKER_API_KEY and bcrypt hashing for API keys) have been fixed in commit 958a49f. A full admin user management UI was added (commit 6e2c091) with proper safety constraints. HFE analysis received two bug fixes for byte ordering and false-positive detection.

No new security issues were identified. The remaining open issues are code quality items and edge cases.

**Open issues: 10** (0 critical, 0 high, 5 medium, 3 low, 1 medium template, 1 arch observation)

### Issue Severity Key

- **CRITICAL** - Broken functionality or security vulnerability that needs immediate attention
- **HIGH** - Bug or issue that will cause problems in normal use
- **MEDIUM** - Code quality issue, potential edge case, or minor security concern
- **LOW** - Style, consistency, or minor improvement opportunity

---

## Issues Fixed Since Last Review

The following issues from previous reviews have been resolved:

| # | Issue | Resolution |
|---|-------|------------|
| 1.1 | `download_artefact` uses non-existent `file_path` | Fixed: now uses `get_artefact_path(artefact)` (`api.py:186`) |
| 1.2 | `add_artefact` uses non-existent `file_path` | Fixed: now uses `storage_path`, `original_filename`, `storage_directory` (`api.py:146-162`) |
| 1.3 | Missing blank line after `safe_original_filename` | Fixed: blank line now present at line 41 of `artefacts.py` |
| 1.4 | `install.py` SECRET_KEY check is dead code | Resolved: `install.py` deleted entirely, replaced by `flask create-admin` CLI command |
| 1.5 | `install.py` creates app twice | Resolved: `install.py` deleted |
| 1.8 | Log format strings never substitute username | Fixed: now uses f-strings throughout |
| 2.2 | Default admin password `password` in `install.py` | Fixed: `flask create-admin` requires interactive password entry or env vars, enforces 12-char minimum (`app.py:236-302`) |
| 3.1 | Duplicated `_delete_artefact_files` | Fixed: `api.py` now imports from `artefacts.py` (`api.py:19`) |
| 3.2 | `app.py` creates app at import time | Fixed: module-level `app = create_app()` removed; `.flaskenv` sets `FLASK_APP=myapp.app:create_app`; Gunicorn invocation uses `"myapp.app:create_app()"` |
| 4.2 | `create-admin` docstring contradicts behaviour | Fixed: docstring now matches the idempotency guard |
| 5.1 | No timeout for external tool execution | Fixed: `run_tool()` in `base.py` now has `timeout=3600` default; sfdisk=30s, file=10s |
| T2 | Nested `<form>` tags in `login.html` | Not an issue: `render_form()` renders fields only, not a wrapping `<form>` tag |
| 1.1b | `FLASK_ENV` deprecated in Flask 3.x | Fixed: `FLASK_ENV` check removed, simplified SECRET_KEY handling (commit 840ec49) |
| 1.2b | Analysis retry doesn't clear stale fields | Fixed: all result fields now cleared on retry (commit beff369) |
| 2.1b | Hard-coded database credentials in Docker Compose | Fixed: credentials moved to `.env` file (commit ca42e69) |
| 2.4 | Adminer exposed on port 8080 | Fixed: moved to separate `docker-compose.adminer.yml` (commit ca42e69) |
| 1.1c | `sqltap` double-collect discards statistics | Fixed: second `collect()` replaced with filtering `stats_all` (commit f951ecd, `app.py:98-99`) |
| 1.2c | Item deletion leaves orphaned files on disk | Fixed: `_delete_item_files(item)` called before cascade delete (commit 68bcf8e, `items.py:194`) |
| 1.3c | Shallow descendant exclusion allows taxonomy cycles | Fixed: `_collect_descendant_ids()` recursively collects all descendants (commit 1d05640, `taxonomy.py:123`) |
| 1.4c | `detect_fat_filesystem` duplicates `_is_fat_bpb` logic | Fixed: `_is_fat_bpb` removed; `detect_partitions_sfdisk` now calls `detect_fat_filesystem` directly (commit 2c3b27c) |
| 1.5c | `_is_fat_bpb` skips boot signature check | Resolved: function removed as part of consolidation (commit 2c3b27c) |
| 2.1 | No API authentication | Fixed: `@require_auth` decorator on all API routes (except `/health`), Bearer token + user API keys, permission hierarchy (commit 2192b35) |
| 2.3 | Health check leaks error details | Fixed: returns generic `'Database unavailable'` instead of `str(e)` (commit 2192b35) |
| 3.1 | Deprecated `datetime.utcnow` | Fixed: all calls replaced with `datetime.now(timezone.utc)` (commit 6fcec15) |
| T1 | Orphaned `artefacts/form.html` | Fixed: template deleted (commit 93e4f18) |
| T2b | Missing pagination in `analysis/index.html` | Fixed: pagination added with status filter preservation (commit 93e4f18) |
| T3 | Breadcrumb misdirection in taxonomy forms | Fixed: links to Taxonomy index (commit 93e4f18) |
| T4 | Cancel button fallback in taxonomy forms | Fixed: falls back to Taxonomy index (commit 93e4f18) |
| T5 | Missing null guard for `file_size` | Fixed: uses `{% if artefact.file_size is not none %}` (commit 93e4f18) |
| T6 | Items pagination loses filter params | Fixed: passes `q`, `platform_id`, `category_id` in links (commit 93e4f18) |
| T7 | Dashboard analyses not clickable | Fixed: links to analysis view page (commit 93e4f18) |
| 2.1d | WORKER_API_KEY uses plain string comparison | Fixed: replaced `==` with `hmac.compare_digest()` (commit 958a49f) |
| 2.2d | API keys hashed with SHA-256 instead of bcrypt | Fixed: switched to `bcrypt.hashpw()`/`bcrypt.checkpw()`, migration deactivates old keys (commit 958a49f) |

---

## 1. Bugs

### 1.1 [MEDIUM] `hints` field bypasses NUL byte validation

**File:** `myapp/blueprints/api.py:272`

```python
analysis = Analysis(
    artefact_id=artefact.id,
    analysis_type=analysis_type,
    status=AnalysisStatus.PENDING,
    tool_name=data.get('tool_name'),
    hints=data.get('hints')  # Store hints JSON
)
```

The `_check_nul_bytes()` function (line 85) validates string fields before database writes to prevent PostgreSQL TEXT column errors. However, in `request_analysis()` (line 258), the `hints` field is stored directly from user input without NUL byte validation. The `update_analysis()` endpoint (line 294) validates `tool_name`, `tool_version`, `output_url`, `output_path`, `summary`, `details`, and `error_message` — but not `hints`. A NUL byte in `hints` would cause an unhandled 500 error on the database write.

**Fix:** Add `'hints'` to the `_check_nul_bytes` field list, or add it to the `update_analysis` validation at line 294.

---

### 1.2 [MEDIUM] `get_output_file` discards subdirectory paths

**File:** `myapp/blueprints/api.py:369-386`

```python
@blueprint.route('/outputs/<path:filename>', methods=['GET'])
@require_auth('read_only')
def get_output_file(filename):
    ...
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(output_dir, safe_filename)
```

The route accepts `<path:filename>` (allowing slashes in the URL), but `os.path.basename()` at line 379 strips all directory components. If any analysis stores output files in subdirectories within the output folder, this endpoint would be unable to serve them — it would look for just the leaf filename in the root output directory.

Currently, output files (like flux visualisation PNGs) are stored flat, so this works. But the `<path:filename>` route pattern suggests subdirectory support was intended. Either the route should use `<string:filename>` to match the flat-only behaviour, or the path handling should be changed to allow subdirectories with proper path-traversal protection (e.g., `os.path.realpath` and prefix check).

---

### 1.3 [MEDIUM] Hash lookup triggers N+1 queries

**File:** `myapp/blueprints/api.py:690-695`

```python
extracted = query.all()
return jsonify({
    'known_file': known_file_to_dict(known) if known else None,
    'found_in': [{'artefact_id': f.partition.artefact_id, 'item_id': f.partition.artefact.item_id,
                  'item_name': f.partition.artefact.item.name, 'path': f.path} for f in extracted]
})
```

For each `ExtractedFile` result, `f.partition.artefact.item.name` triggers three lazy-loaded relationships: `partition` -> `artefact` -> `item`. With N matching files, this generates 3N additional queries. The query at line 685-689 should use `joinedload` to eagerly load the required relationships.

**Fix:** Add `.options(joinedload(ExtractedFile.partition).joinedload(Partition.artefact).joinedload(Artefact.item))` to the query.

---

### 1.4 [MEDIUM] Overly broad exception handler catches everything

**Files:** `myapp/blueprints/artefacts.py:352,736,856`

```python
except (json.JSONDecodeError, Exception) as e:
```

This pattern now appears at three locations (up from two in the last review). Since `Exception` is a superclass of `json.JSONDecodeError`, the `JSONDecodeError` branch is redundant. More importantly, this catches all exceptions including `MemoryError`, meaning non-JSON errors (database errors, OS errors) are silently swallowed with a warning log.

**Fix:** Catch only `(json.JSONDecodeError, KeyError, TypeError)` — the specific exceptions that malformed JSON details could raise.

---

### 1.5 [MEDIUM] Decompression subprocess has no timeout

**File:** `worker/arcworker/compression.py:52-56`

```python
result = subprocess.run(
    cmd + [str(compressed_copy)],
    capture_output=True,
    cwd=work_dir
)
```

The main analysis tool runner (`run_tool()` in `base.py`) enforces a default 3600-second timeout. However, the decompression step in `compression.py` runs `subprocess.run()` without any timeout. A maliciously crafted or corrupt compressed file (e.g., a gzip bomb) could cause the decompression process to run indefinitely, blocking the worker.

**Fix:** Add `timeout=3600` (or a configurable value) to the `subprocess.run()` call, consistent with `run_tool()`.

*Resolved: Added `timeout=3600` to `subprocess.run()` in `decompress_if_needed()`, with a `subprocess.TimeoutExpired` handler that raises `RuntimeError`. Consistent with `run_tool()` default.*

---

### 1.6 [LOW] `analysis_to_dict` assumes `artefact` is never None

**File:** `myapp/blueprints/api.py:731`

```python
'artefact_uuid': analysis.artefact.uuid,
```

Line 731 unconditionally accesses `analysis.artefact.uuid`. While `artefact_id` is effectively always set (analyses are always created with an artefact reference), the column in `database.py` is defined with `nullable=True` (it's a ForeignKey without explicit `nullable=False`). An orphaned analysis record would cause an `AttributeError`.

---

## 2. Security

### 2.1 [LOW] `safe_original_filename` only strips three characters

**File:** `myapp/blueprints/artefacts.py:24-40`

The function strips null bytes, forward slashes, and backslashes, which prevents path traversal. However, it does not handle `..` sequences. Since `original_filename` is only used for display and download headers (not for filesystem paths — storage uses UUID-based names), the actual risk is low.

**Note:** Acceptable given the architecture — `original_filename` is a display-only field.

*[Carried forward]*

---

## 3. Code Quality

### 3.1 [LOW] Mixed indentation style across files

The project uses tabs (documented in CLAUDE.md), but several files or sections use spaces:
- `myapp/blueprints/artefacts.py`: most route functions use spaces (4-space indent)
- `myapp/database.py`: uses spaces throughout
- `worker/arcworker/tools/extraction.py`: some functions use spaces while others use tabs

This is cosmetic but can cause issues with editors and diff tools.

*[Carried forward]*

---

### 3.2 [LOW] `sqltap` installed unconditionally in Dockerfile

**File:** `Dockerfile`

`sqltap` is a debug-only dependency but is installed in the production Docker image. It's small, but unnecessary in production.

*[Carried forward]*

---

## 4. Commits Review (68bcf8e..2192b35)

### 4.1 Replace deprecated `datetime.utcnow` (commit 6fcec15)

**Files:** `myapp/database.py`, `myapp/blueprints/api.py`

Clean fix. All `datetime.utcnow()` calls replaced with `datetime.now(timezone.utc)`. No remaining deprecated usage found.

---

### 4.2 Fix template issues T1-T7 (commit 93e4f18)

**Files:** Multiple templates

Comprehensive template fix batch:
- Deleted orphaned `artefacts/form.html`
- Added pagination to `analysis/index.html` with status filter preservation
- Fixed taxonomy breadcrumbs and cancel button fallbacks to point to Taxonomy index
- Added null guard for `file_size` in `artefacts/edit.html`
- Preserved filter parameters in items pagination links
- Made dashboard analyses clickable with links to analysis view

All fixes are clean and correct.

---

### 4.3 Add disc mastering and protection analysis types (commit c21b947)

**Files:** `myapp/database.py`, `worker/arcworker/types.py`, `myapp/blueprints/artefacts.py`, migration

Added `DISC_MASTERING_DETECT` and `DISC_PROTECTION_DETECT` to `AnalysisType` enum in both web and worker copies. Migration properly uses `ALTER TYPE ... ADD VALUE`. Enum values are in sync between `database.py` and `types.py`.

---

### 4.4 HFE byte order fix and mastering/protection analysis (commit 23018d0)

**Files:** `worker/arcworker/tools/hfe.py`, `worker/arcworker/analysis.py`

Large addition (1274 lines) providing a pure-Python HFE v3 parser and two new analysis handlers:

- `process_disc_mastering_detect()` — scans for mastering tool fingerprints in flux data
- `process_disc_protection_detect()` — detects copy protection schemes (weak bits, non-standard sectors, etc.)

The HFE parser includes fixes for LSB-first bit ordering and proper handling of v3 opcode-based encoding. Code is clean, well-structured, and uses no external subprocesses.

---

### 4.5 Add API key authentication and user permissions (commit 2192b35)

**Files:** `myapp/blueprints/api.py`, `myapp/database.py`, `myapp/blueprints/admin.py`, `myapp/blueprints/profile.py`, `myapp/permissions.py`, `worker/arcworker/config.py`, `worker/arcworker/api.py`, migration

Major feature addition implementing a complete API key authentication system:

**Architecture:**
- `@require_auth(permission)` decorator on all API routes (except `/health`)
- Two authentication paths: pre-shared `WORKER_API_KEY` (always read_write, compared with `hmac.compare_digest`) and user API keys (permission-scoped)
- Three permission levels: `READ_ONLY`, `READ_UPLOAD` (create items/artefacts), `READ_WRITE` (full CRUD)
- `ApiKey` model with bcrypt-hashed keys, `arc_` prefix, 8-char prefix for display/lookup
- `effective_permission()` caps key permission by owning user's permission level
- Web route permission enforcement via `@require_permission` decorator in `myapp/permissions.py`

**Worker integration:**
- Worker reads `WORKER_API_KEY` from environment, exits if not set
- API client sends key as `Authorization: Bearer` header
- Health check remains unauthenticated for container orchestration

**Admin panel:**
- User permission management (read_only / read_write)
- API access toggle per user
- Admin-only access enforced via `_require_admin()`

**Profile page:**
- Users can create/view/revoke their own API keys
- Raw key shown once via session, popped on display

**Review notes:**
- Permission hierarchy correctly enforced across all endpoints
- CSRF exemption on API blueprint is correct (API keys replace CSRF tokens)
- Web routes properly use `@login_required` + `@require_permission` for server-side enforcement
- Template guards (`user_can_write`) provide UI-level protection in addition to server-side checks
- No remaining security issues — timing-safe comparison and bcrypt hashing both addressed

---

## 5. Commits Review (2192b35..6e2c091)

### 5.1 Fix API key security (commit 958a49f)

**Files:** `myapp/blueprints/api.py`, `myapp/database.py`, migration `b2e4f6a8c0d1`

Addresses both security issues reported in the previous review:

1. **Timing-safe comparison:** `WORKER_API_KEY` comparison changed from `==` to `hmac.compare_digest()` — clean one-line fix.

2. **Bcrypt hashing:** API key storage switched from SHA-256 to bcrypt:
   - `create()` now uses `bcrypt.hashpw()` with `bcrypt.gensalt()`
   - `verify()` looks up candidates by 8-char key prefix, then calls `bcrypt.checkpw()` on each match
   - `key_hash` column widened from `String(64)` to `String(72)` to hold bcrypt hashes
   - Migration deactivates all existing SHA-256-hashed keys (users must regenerate)

**Review notes:**
- The prefix-based lookup is an efficient approach — avoids loading all active keys for bcrypt comparison
- `bcrypt.checkpw()` is wrapped in `try/except (ValueError, TypeError)` to handle any corrupted hashes gracefully
- The migration correctly deactivates old keys rather than attempting in-place conversion (SHA-256 hashes cannot be converted to bcrypt without the original key)

---

### 5.2 HFE v1/v2 byte reversal fix (commit 4cebf6e)

**File:** `worker/arcworker/tools/hfe.py`

Applied `_BYTE_REVERSE` table to v1/v2 HFE data in `get_track_bytes()`. Previously only v3 data was normalised to MSB-first bit order, causing the MFM/FM walkers to find no sync patterns and decode zero sectors on v1/v2 files. Clean fix, no new issues.

---

### 5.3 HFE uniform-fill sector skip (commit 0c63b56)

**File:** `worker/arcworker/tools/hfe.py`

Added a guard to skip sectors whose content is entirely a single repeated byte (e.g., 0xF6 format-fill) in `unknown_mastering` detection. Blank sectors on unused tracks were being falsely flagged because they collapsed to a single unique entry after deduplication. Clean fix, no new issues.

---

### 5.4 Admin user management UI (commit 6e2c091)

**Files:** `myapp/blueprints/admin.py`, `myapp/templates/admin/create_user.html`, `myapp/templates/admin/edit_user.html`, `myapp/templates/admin/index.html`

Full CRUD for user accounts via the admin panel:

- **Create:** Form with username, password (min 12 chars with confirmation), permission level, admin flag, and API key access toggle
- **Edit:** All fields editable; password optional (blank = no change); username uniqueness enforced
- **Delete:** Confirmation modal; cascade-deletes API keys via SQLAlchemy relationship

**Security review:**
- All routes protected by `@login_required` + `_require_admin()` — correct
- Self-deletion prevented (`user_id == current_user.id` check)
- Self-admin-removal prevented (`is_admin` field ignored when `editing_self`)
- Password validation: min 12 chars + confirmation match enforced server-side
- Username uniqueness checked before save (excludes self in edit)
- CSRF tokens on all forms (including delete modal)
- Flash messages include `form.username.data` but Jinja2 auto-escapes, so no XSS risk
- Delete modal reuses `perm_forms[user.id].hidden_tag()` for CSRF — works because Flask-WTF CSRF tokens are per-session, not per-form

**No new security issues identified.**

---

## 6. Architecture Observations

### 6.1 Gunicorn runs with a single worker by default

**File:** `Dentrypoint.sh:10`

```sh
gunicorn -b 0.0.0.0:8000 "myapp.app:create_app()" --timeout 300
```

Still defaults to 1 worker process. For a system handling file uploads up to 4GB with a 300-second timeout, a single worker means one large upload blocks all other requests.

**Recommendation:** Add `--workers` flag (e.g., `--workers 4` or `--workers $(nproc)`).

*[Carried forward]*

---

## 7. Template Issues

### Medium Priority

| # | Issue | Description |
|---|-------|-------------|
| T8 | Integer IDs exposed in analysis and taxonomy URLs | Inconsistent with UUID convention used elsewhere |

**Note:** Template issues T1-T7 were all fixed in commit 93e4f18. T8 remains as a consistency concern but has no functional impact.

---

## 8. Summary of Recommended Fixes

### Must Fix (Broken Functionality)

*No critical or high-severity issues remain — all have been fixed.*

### Security Status

All previously-reported security issues have been addressed:
- API authentication with permission hierarchy (commit 2192b35)
- Timing-safe WORKER_API_KEY comparison with `hmac.compare_digest` (commit 958a49f)
- Bcrypt hashing for API keys (commit 958a49f)
- Health check error sanitisation (commit 2192b35)
- Admin routes properly protected with `@login_required` + `_require_admin()` (commit 6e2c091)

One low-risk security observation remains (2.1 `safe_original_filename`), which is acceptable given the architecture.

### Consider Fixing (MEDIUM)

| # | Issue | File | Severity |
|---|-------|------|----------|
| 1.1 | `hints` bypasses NUL byte validation | `api.py:272` | MEDIUM |
| 1.2 | `get_output_file` discards subdirectory paths | `api.py:369-386` | MEDIUM |
| 1.3 | Hash lookup N+1 queries | `api.py:690-695` | MEDIUM |
| 1.4 | Overly broad exception handler (3 locations) | `artefacts.py:352,736,856` | MEDIUM |
| ~~1.5~~ | ~~Decompression subprocess has no timeout~~ | Resolved | ~~MEDIUM~~ |

### Low Priority

| # | Issue | File | Severity |
|---|-------|------|----------|
| 1.6 | `analysis_to_dict` assumes artefact not None | `api.py:731` | LOW |
| 2.1 | `safe_original_filename` only strips three chars | `artefacts.py:24-40` | LOW |
| 3.1 | Mixed indentation style | various | LOW |
| 3.2 | `sqltap` in production Docker image | `Dockerfile` | LOW |

### Template Issues

| # | Severity | Issue | Template |
|---|----------|-------|----------|
| T8 | MEDIUM | Integer IDs in analysis/taxonomy URLs | `analysis/`, `taxonomy/` |

### Architecture Observations

| # | Issue | File |
|---|-------|------|
| 6.1 | Single Gunicorn worker | `Dentrypoint.sh:10` |
