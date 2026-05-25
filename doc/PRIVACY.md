# Privacy and Ownership

Arcology supports private items and artefacts, visible only to their owner and
administrators. This document describes the data model, visibility rules,
and how to use privacy controls from both the web UI and the REST API.

---

## Concepts

### Ownership

Every `Item` and `Artefact` has an optional **owner** — a registered user.

- Items created via the web UI are owned by the logged-in user.
- Artefacts uploaded via the web UI are owned by the uploader.
- Artefacts uploaded via the API with a user API key are owned by that key's
  user. Worker-derived artefacts (produced by analysis jobs) inherit their
  parent artefact's owner.
- Items and artefacts created by the worker using the worker API key have no
  owner (`owner_id = NULL`).

### Privacy

Items and artefacts each have an `is_private` flag. An item also carries a
denormalised `private_effective` flag that reflects the combined effect of the
item's own `is_private` and all ancestors:

- An item is **effectively private** if `is_private` is true on itself *or* on
  any ancestor.
- An artefact is **effectively private** if `artefact.is_private` is true *or*
  if its parent item is effectively private.

`private_effective` is recomputed whenever an item is created, its `is_private`
flag changes, or it is moved to a different parent.

---

## Visibility Rules

| Viewer | Can see public content? | Can see private content? |
|---|---|---|
| Anonymous (not logged in) | Yes | No |
| Authenticated user | Yes | Only items/artefacts they own |
| Admin | Yes | Yes (all) |
| Worker (worker API key) | Yes | Yes (all) |
| User API key | Yes | Only items/artefacts owned by that key's user |

The helpers in `myapp/visibility.py` implement these rules:

| Function | Description |
|---|---|
| `can_view_item(item, user)` | True if user may view the item |
| `can_view_artefact(artefact, user)` | True if user may view the artefact |
| `item_visibility_clause(user)` | SQLAlchemy WHERE clause for filtering Item queries |
| `artefact_visibility_clause(user)` | SQLAlchemy WHERE clause for filtering Artefact queries (requires a JOIN to Item) |
| `can_manage_privacy(obj, user)` | True if user may toggle `is_private` on an item or artefact |
| `can_change_owner(obj, user)` | True if user may reassign the owner |
| `is_owner(obj, user)` | True if user is the recorded owner |

Pass `sees_all=True` to the first four functions to bypass all visibility
filtering (used internally for the worker API key).

---

## Privacy Management Rules

### Who may mark something private or public

- **Admins** may always change `is_private`.
- **The owner** may always change `is_private` on their own objects.
- **Any authenticated user** may privatize an *unowned* object (claim-on-privatize:
  the object is automatically assigned to them when they set `is_private = True`).
- No other user may change the privacy flag.

Attempting to change `is_private` without permission returns HTTP 403 from the
API or silently ignores the field from the web form.

### Who may reassign the owner

- **Admins** may reassign any object to any user (or clear the owner).
- **The owner** may transfer their own object to another user (or relinquish it
  by clearing the owner).
- No other user may change the owner.

Attempting to change `owner_id` without permission returns HTTP 403 from the
API, or the owner picker is hidden from the web form.

---

## Web UI

### Items

- **New item**: The privacy checkbox is shown on the creation form. The creating
  user becomes the owner automatically.
- **Edit item**: Shows the current owner read-only. An owner-picker dropdown is
  shown when the current user is the owner or an admin. The privacy checkbox is
  shown when the current user may manage privacy.
- **Item list**: Private items show a lock icon (🔒) next to their name.
- **Item detail**: A `Private` badge is shown in the heading when
  `private_effective` is true.

### Artefacts

- **Upload**: The privacy checkbox is shown on the upload form. The uploader
  becomes the owner.
- **Edit artefact**: Same owner-display and picker logic as items.
- **Artefact detail**: A `Private` badge distinguishes direct privacy
  (`is_private`) from inherited privacy (item is private).

---

## REST API

### Representation

`GET /api/items/<uuid>` and `GET /api/artefacts/<uuid>` include these fields:

```json
{
  "is_private": false,
  "private_effective": false,
  "owner": "username"
}
```

For artefacts, `effective_private` is returned instead of `private_effective`.

### Creating items (`POST /api/items`)

```json
{
  "name": "My Item",
  "is_private": true
}
```

The owner is set to the user associated with the API key making the request.
Worker keys create unowned items.

### Updating items (`PUT /api/items/<uuid>`)

```json
{
  "is_private": true,
  "owner_id": "user-uuid-hex"
}
```

- `is_private` requires `can_manage_privacy`; returns 403 otherwise.
- `owner_id` requires `can_change_owner`; returns 403 otherwise.
- Setting `owner_id` to `null` clears the owner.

### Uploading artefacts (`POST /api/items/<uuid>/artefacts`)

```json
{
  "filename": "image.scp",
  "is_private": true
}
```

### Listing items (`GET /api/items`)

Only items visible to the caller are returned. Workers see all items.

---

## Database Schema

```
items
  owner_id      INTEGER REFERENCES users(id) ON DELETE SET NULL
  is_private    BOOLEAN NOT NULL DEFAULT FALSE
  private_effective  BOOLEAN NOT NULL DEFAULT FALSE  -- indexed

artefacts
  owner_id      INTEGER REFERENCES users(id) ON DELETE SET NULL
  is_private    BOOLEAN NOT NULL DEFAULT FALSE
```

`private_effective` on `Item` is a denormalised cache. It is authoritative for
all queries. Never set it directly — always call `recompute_item_privacy(item)`
instead, then commit.

---

## Developer Notes

### Adding a new creation path

Whenever you add a route or API endpoint that creates an `Item` or `Artefact`:

1. Set `owner_id` from the authenticated user (web: `current_user.id`;
   API user key: `g.api_user.id`; worker key: leave `None`).
2. Set `is_private` from the request payload or form field.
3. For items, call `recompute_item_privacy(item)` before committing so that
   `private_effective` is set correctly from the start.

### Adding a new query that lists items or artefacts

Apply the visibility clause so private content is not accidentally leaked:

```python
from myapp.visibility import item_visibility_clause, artefact_visibility_clause

# Items:
items = Item.query.filter(item_visibility_clause(current_user)).all()

# Artefacts (requires a join to Item):
artefacts = (
    Artefact.query
    .join(Item, Artefact.item_id == Item.id)
    .filter(artefact_visibility_clause(current_user))
    .all()
)
```

For API endpoints, use `_api_viewer()` to get `(user, sees_all)` and pass
both through: `item_visibility_clause(user, sees_all=sees_all)`.

### Moving an item to a different parent

After changing `item.parent_id`, call `recompute_item_privacy(item)` to
cascade the new parent's effective privacy down to all descendants, then
commit.

```python
item.parent_id = new_parent.id
db.session.flush()
recompute_item_privacy(item)
db.session.commit()
```

### Future: Phase 2 — ACL sharing

The groundwork for ACL sharing (granting specific users read access to a
private item) is not yet implemented. When it is, the visibility helpers in
`myapp/visibility.py` are the only place that needs to change: extend
`can_view_item`, `can_view_artefact`, `item_visibility_clause`, and
`artefact_visibility_clause` to check an ACL table. The rest of the codebase
calls these helpers and will pick up the change automatically.
