# Design Backlog

Items in this file need design discussion before implementation begins.
They are too large or have open design questions that make them unsuitable
for a straightforward coding session.

Tick off items and move them to a PR description once a design is agreed.

---

## Open Items

### DB-1 — Audit log

**From:** Product review P3 #11

Every write action (create/edit/delete item or artefact, upload, ownership
change, permission change, restriction add/remove) should be recorded with
actor, timestamp, action type, target, and a before/after snapshot.

**Questions to resolve before implementing:**

- What granularity?  Full JSON diff of changed fields, or just "user X changed
  item Y at time T"?
- Where to surface it?  Per-item/artefact tab, admin-wide feed, or both?
- Retention policy — keep forever, or expire old events?
- Should it be append-only at the DB level (no UPDATE/DELETE on audit rows)?
- PostgreSQL `jsonb` for the diff column, or a relational before/after table?

---

### DB-2 — Hash rescan as a proper background job

**From:** Product review P3 #14

The current "Rescan Hashes" button starts a Python `threading.Thread` on the
web worker.  No progress is visible, and the thread is lost if Gunicorn is
restarted mid-scan.

**Questions to resolve:**

- Use Celery + Redis, RQ, or a dedicated Flask-Executor task?
- Does Arcology want a broker dependency at all, or a simpler approach
  (e.g. queue a special Analysis job that the existing worker picks up)?
- How to surface progress?  Polling endpoint returning row counts, or
  a dedicated "background tasks" status page?

---

### DB-3 — Batch operations on items and artefacts

**From:** Product review P3 #15

Users with large collections need to bulk-retag, bulk-move, or bulk-delete
items and artefacts without acting on each one individually.

**Questions to resolve:**

- Which operations first?  (Suggested priority: bulk tag → bulk move → bulk delete)
- UI pattern: checkbox-select in the list view, or a separate "Manage" mode?
- Confirmation flow for bulk delete (confirm count, type "DELETE" to confirm)?
- Should bulk operations respect sharing/ownership restrictions (i.e. only items
  the user has `curator` or `owner` access to)?

---

### DB-4 — Slug regeneration policy on item rename

**From:** Product review P2 #9

Currently item slugs are immutable — renaming an item leaves the slug (and thus
the URL) unchanged.

**Questions to resolve:**

- Should rename always regenerate the slug, or only when the user explicitly
  requests it (a "Lock slug" checkbox)?
- If regenerated, should the old slug 301-redirect to the new URL?  (Requires
  storing old slugs.)
- What about artefact slugs — same policy?

---

### DB-5 — Per-item restriction bypasses

**From:** Product review P3 #16

Current `UserRestrictionBypass` is all-or-nothing per restriction type:
granting a user MALWARE bypass lets them download every malware-restricted
artefact.

**Questions to resolve:**

- New model: `UserItemRestrictionBypass(user_id, artefact_id, restriction_type)`
  — does this replace or augment the existing coarse table?
- UI: where does an admin grant per-artefact bypass?  On the artefact page, or
  in the user admin panel?
- Should bypasses expire (time-limited access)?

---

### DB-6 — Full live analysis status via Server-Sent Events

**From:** Product review P1 #2 (auto-refresh is the current stopgap)

The current implementation polls every 6 s and reloads the full page when
analyses complete.  This is functional but causes a jarring full reload and
doesn't update in-place.

**Questions to resolve:**

- Use Flask-SSE (requires Redis pub/sub) or a simpler generator-based SSE
  endpoint that long-polls the database?
- Which parts of the page update live?  The analyses card only, or also the file
  listing and analysis result sections?
- Should the "Analysing…" spinner and status badges update without a reload?

---

## Review Together

### RV-1 — Navbar active-class coverage

**From:** User note, 2026-05-31

The Help link in the navbar has an `active` class applied when on a help page
(via `request.endpoint and 'myapp_blueprints_help' in request.endpoint`).

Other nav links use the registered menu-item `endpoint == request.endpoint`
check, which only matches exact endpoints — so "Items" is only active when on
`items.index`, not on `items.view`, `items.new`, etc.

**Options to discuss:**

- Match on endpoint prefix (`startswith('myapp_blueprints_items')`)
- Match on URL prefix (`request.path.startswith('/items')`)
- Leave as-is (current behaviour pre-dates this review)

---

## Resolved Items

### DB-4 — Slug regeneration on item rename

**Resolution:** Already implemented. The item edit route (`items.py` line 522)
runs `item.slug = ensure_unique_slug(generate_slug(item.name), Item, existing_id=item.id)`
on every save.  `lookup_by_identifier` resolves old UUID-prefix URLs and the item
view route 301-redirects to the current canonical URL.  No code changes needed.
The backlog entry was based on an incorrect reading of the codebase.

### RV-1 — Navbar active-class coverage

**Decision (2026-06-01):** Blueprint-prefix match.  Extract blueprint name from
`menuitem.endpoint` with `.rsplit('.', 1)[0]` and test whether it appears in
`request.endpoint`.  Implemented in PR #418 (`fix/navbar-active-class`).

---

*Last updated: 2026-05-31*
