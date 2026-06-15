# Arcology User Guide

Arcology is a digital artefact catalogue for retrocomputing collections. It
helps you organise, store, and automatically analyse disk images, flux dumps,
archives, and other digital media from historical computer systems.

This guide covers the web interface. For the command-line client, see
[CLI.md](CLI.md).

---

## Table of Contents

- [Getting Started](#getting-started)
- [Dashboard](#dashboard)
- [Items](#items)
- [Artefacts](#artefacts)
- [The Analysis Pipeline](#the-analysis-pipeline)
- [Search](#search)
- [Taxonomy](#taxonomy)
- [Your Account](#your-account)
- [Administration](#administration)

---

## Getting Started

### Logging In

Navigate to your Arcology instance (e.g. `http://localhost:8000`). All pages
except the health-check API require authentication.

Depending on how your instance is configured, you will see one or both of:

- **Sign in with \<Provider\>** — single sign-on (SSO) via your organisation's
  identity provider (Keycloak, Okta, Azure AD, etc.). Click the button and
  complete authentication in the provider's login page; you will be redirected
  back to Arcology automatically.
- **Local login form** — enter the username and password provided by your
  administrator.

### Permissions

Arcology has three permission levels:

| Level | Can do |
|-------|--------|
| **Read Only** | Browse items, artefacts, search, and download files |
| **Read + Upload** | Everything above, plus upload artefacts and queue analyses |
| **Read + Write** | Everything above, plus create/edit/delete items and taxonomy |

Administrators can also access the admin panel to manage users.

---

## Dashboard

The dashboard is the homepage (`/`). It shows four summary cards:

- **Items** -- total number of catalogue entries
- **Artefacts** -- total number of uploaded files
- **Pending** -- analyses waiting in the queue
- **Running** -- analyses currently being processed

Below the cards you will find the ten most recently updated items and the ten
most recent analyses, both linked for quick navigation.

The **New Item** button appears if you have write permission.

---

## Items

An **item** is a logical catalogue entry -- for example "WordStar 3.0 for CP/M"
or "BBC Micro Welcome Disc". Each item can hold multiple artefacts (the actual
files), be classified by platform and category, and tagged for flexible
filtering.

Items can be nested to form a hierarchy -- for example a "Sega" item could
contain a "Sonic the Hedgehog" child item which holds the actual disc image
artefacts. Platform and category are inherited from the nearest ancestor that
has them set.

### Browsing Items

Go to **Items** in the top navigation bar. The list page shows all items with
their platform, category, artefact count, and last-updated date. The **tree**
view shows the full parent/child hierarchy with indentation; the **flat** view
shows all items as a simple list.

Use the filters at the top of the page to narrow results:

- **Search** -- text search across item names and descriptions
- **Platform** -- dropdown filter by platform
- **Category** -- dropdown filter by category
- **Sort** -- Name (A–Z / Z–A) or Uploaded (oldest / newest)

An **A–Z letter bar** sits above the list. Clicking a letter jumps to the page
where items starting with that letter begin. **#** covers items that start with
a digit or punctuation. Letters with no matching items are shown as disabled.
The **All** button clears any active letter jump.

Use the **per-page selector** (25 / 50 / 100 / 250 / All) to control how many
items appear per page. Your choice is remembered across sessions.

Click an item name to view its detail page.

### Creating an Item

1. Click **New Item** (on the items list or dashboard).
2. Fill in the form:
   - **Name** (required) -- a short descriptive title
   - **Description** -- optional free-text notes
   - **Platform** -- the computer system this item relates to
   - **Category** -- the type of software or media
   - **Tags** -- comma-separated labels (e.g. `bbc-micro, games, dfs`)
   - **Parent Item** -- optionally nest this item under an existing item
3. Click **Save**.

To create a child of an existing item, click **New Child Item** on the parent's
detail page instead.

### Editing and Deleting Items

On the item detail page, click **Edit** to change any field (including moving
the item to a different parent), or **Delete** to remove the item and all its
artefacts and children permanently.

The item detail page shows an **A–Z letter bar** and **sort/per-page controls**
above the artefacts list, and a **Child Items** section listing any items nested
under this one.

### External References

Items can be linked to records in external cataloguing systems (Koillection,
Collective Access, etc.). On the item detail page, the **External References**
card shows existing links and lets you add new ones:

1. Click **Add** on the External References card.
2. Select the external system from the dropdown.
3. Enter the external ID (e.g. the item number in that system).
4. Optionally provide a direct URL and notes.
5. Click **Add**.

If the external system has a URL template configured, Arcology generates a
clickable link automatically.

---

## Artefacts

An **artefact** is a single digital file attached to an item -- typically a disk
image, flux dump, archive, or scan. Artefacts can also be *derived* from other
artefacts by analysis (e.g. a decoded `.img` produced from a `.scp` flux dump).

### Uploading

1. Navigate to the item you want to add files to.
2. Click **Upload**.
3. Select a file. Arcology auto-detects the artefact type from the file
   extension; you can override this with the **Artefact Type** dropdown.
4. If the filename follows the Arcarc/Tosec naming convention
   (`Publisher_Title__Platform-DiscType`), the **Label** field and
   **Platform hint** dropdown are prefilled automatically. You can edit
   these before uploading.
5. Optionally set a label (defaults to the original filename) and description.
6. The **Auto Analyse** checkbox (on by default) queues appropriate analyses
   immediately after upload.
7. To stay on the upload form after submitting (useful for bulk uploads),
   tick the **Upload more** checkbox before clicking Upload. The form will
   reload ready for the next file instead of redirecting to the artefact page.
8. Click **Upload**.

Supported artefact types and their recognised extensions:

| Type | Extensions |
|------|-----------|
| SCP (SuperCard Pro flux) | `.scp` |
| IMD (ImageDisk) | `.imd` |
| HFE (HxC Floppy Emulator) | `.hfe` |
| RAW_SECTOR | `.img`, `.bin`, `.raw`, `.dsk`, `.adf`, `.adl`, `.ssd`, `.dsd`, `.40t`, `.80t` |
| ISO (CD/DVD) | `.iso` |
| DD_ZST (compressed raw) | `.dd.zst` |
| DD_GZ (compressed raw) | `.dd.gz` |
| DD_BZ2 (compressed raw) | `.dd.bz2` |
| PDF | `.pdf` |
| ZIP | `.zip` |
| TAR.GZ | `.tar.gz` |
| RAR | `.rar` |
| ARC (RISC OS archive) | `.arc`, `.arcfs`, `.spk`, `.spark` |

Files that don't match any extension are classified as `UNKNOWN`.

### Viewing an Artefact

Click an artefact on the item detail page to see:

- **File details** -- original filename, size, MIME type, MD5 and SHA-256
  checksums (with copy buttons)
- **Type badge** -- the detected or manually set artefact type
- **Tags** -- artefact-level tags shown as badges
- **Analyses** -- a summary of completed analyses with status badges; the
  **Show All** link opens a dedicated paginated analyses page. The
  **Processing Tree** button opens a collapsible tree showing the full
  artefact derivation hierarchy: each artefact node lists its analyses with
  status icons, archive and format-convert operations grouped by file path,
  and child artefacts nested below. The tree auto-refreshes every 10 seconds
  while any job is pending or running. Flux visualisation plots are shown
  inline on the artefact page. The raw JSON details panel is open by default.
- **Partitions and file listings** -- if file extraction has run, the extracted
  directory tree is shown grouped by partition. When the listing is sorted by
  path, an A–Z letter bar appears for quick navigation through large directories.
  Each subdirectory row shows a file count badge; click it to browse into that
  directory. A **breadcrumb** at the top of the file card shows your current
  path with each segment clickable. Use the **Clear** button to reset filters.
  The file table includes a **Date** column (modification or creation time).
  RISC OS native files (Acorn Sprite, Draw, text) that have been converted
  display an eye icon; click it to view the file inline.
- **Derived artefacts** -- if analysis produced new files (e.g. decoded sector
  images from flux data), they appear as linked child artefacts
- **Copy protection indicators** -- if protection scanning detected anything
  (bad CRCs, weak bits, DDAM sectors, ID mismatches)
- **Mastering data** -- duplicator fingerprints found in trailing tracks
  (formaster timestamps, traceback data)
- **Known-file matches** -- if extracted files match entries in a hash database,
  the matched product is shown

### Downloading

Click the **Download** button on an artefact page. Administrators can apply
download restrictions to individual artefacts (for malware, PII, copyright, or
other reasons). Restrictions can also be applied at the individual file level
within an extracted listing. If a restriction applies:

- Users with a bypass for that restriction type see a
  **Download (Override)** button.
- Users without a bypass see a disabled **Restricted** button with the reason.

Explicit content restrictions additionally hide the file listing until the user
actively chooses to reveal it.

#### Granting download access

A bypass can be granted in two ways:

- **Global, per-type** -- gives a user override access to *every* artefact
  carrying a particular restriction type. Managed from the admin user-edit page.
- **Per-artefact** -- gives a user override access to *one specific* restricted
  artefact, independently of any global bypass. Use this for narrow exemptions
  (e.g. a researcher who needs a single restricted file).

To grant per-artefact access, open the restricted artefact as an administrator
and use the **Per-User Download Access** card in the sidebar. Choose the user,
the restriction type to bypass, and an optional reason, then click **Grant**.
Each grant has a revoke button, and existing grants for a user are also listed
(and revocable) on that user's admin edit page.

A grant covers restrictions of the chosen type **anywhere in the artefact and
its derived artefacts** -- the artefact-level restriction, any individual
extracted files carrying that restriction type, and the same on artefacts
derived from this one (e.g. a sector image decoded from a flux dump). So if a
single extracted file is restricted, you can grant a user access to it here; the
restriction type for that file appears in the **Type** dropdown even when the
parent artefact is otherwise unrestricted.

Bypasses apply wherever that user authenticates. Downloads through the REST API
and the `arco` CLI (using one of the user's API keys) honour exactly the same
grants as the website, so a bypass granted here also unblocks programmatic
downloads for that user. The internal analysis worker is not a user and remains
blocked from restricted downloads.

> **Where is the card?** The **Per-User Download Access** card only appears
> when **all** of these are true: you are an administrator, you have write
> access, and the artefact has **at least one download restriction** -- on the
> artefact itself, on one of its extracted files, or on a file in an artefact
> derived from it. If nothing is restricted there is nothing to bypass, so the
> card is hidden -- add a
> restriction first (using the **Download Restrictions** card directly above it,
> or the per-file restriction controls in the file browser), and the access card
> will appear (it auto-expands when grants already exist).

### Editing and Deleting

Click **Edit** to change the label, type override, description, or tags.
Click **Delete** to permanently remove the artefact and all its analyses.

### Moving an Artefact

Root artefacts (not derived ones) can be reassigned to a different item. On
the artefact detail page, use the **Move to item** form in the sidebar to
select the target item. All derived artefacts move with it automatically.

### Re-analysing

Click **Re-analyse** to clear all existing analysis results (including derived
artefacts and file listings) and queue fresh analyses. You can optionally
provide a platform hint and filesystem hint to help the analysis tools.

### Manual Actions

On the artefact detail page, several one-click actions are available:

- **Compute Hashes** -- compute MD5 and SHA-256 if not already present
- **Rescan Known Files** -- re-link extracted files against currently active
  hash databases without re-running full analysis
- **Re-run Product Recognition** -- re-match extracted files against
  known-product definitions

---

## The Analysis Pipeline

When you upload an artefact with **Auto Analyse** enabled, Arcology
automatically queues the analyses appropriate for that file type. The analysis
worker (a separate background process) picks up jobs, runs external tools, and
reports results back.

### How It Works

1. **Upload** -- the artefact is saved and analyses are queued based on its type.
2. **Worker claims job** -- a worker atomically claims the next pending job.
   Multiple workers can run in parallel without duplicating work.
3. **Processing** -- the worker runs the appropriate external tool and collects
   results.
4. **Results posted** -- extracted files, derived artefacts, and metadata are
   sent back to the web app.
5. **Chain continues** -- derived artefacts (e.g. a decoded `.img` from a
   `.scp`) automatically trigger their own analyses, creating a processing
   chain.

### Analysis Types

| Analysis | Applies to | What it does |
|----------|-----------|-------------|
| Flux Visualisation | SCP | Generates graphical plots of magnetic flux data |
| Flux Decode | SCP | Converts flux images to sector formats (IMD, HFE, IMG) |
| Partition Detect | RAW_SECTOR, DD_ZST, DD_GZ, DD_BZ2 | Identifies partitions and filesystem types on a disc image |
| File Extraction | ISO, RAW_SECTOR (after partition detect) | Extracts the directory listing from a disc image |
| Archive Detect | (after file extraction) | Scans extracted files for nested archives by filetype |
| Archive Extract | (after archive detect) | Extracts nested archives recursively (up to 10 levels deep) |
| Metadata Extract | SCP, IMD, ISO | Computes hashes and extracts format-specific metadata |
| Checksum Compute | (manual) | Computes MD5/SHA-1/SHA-256 for an artefact |
| Format Identify | IMD, HFE, RAW_SECTOR | Identifies the exact disc format variant |
| Format Convert | (after file extraction, RISC OS files) | Converts Acorn Sprite, Draw, and text files to PNG/SVG for inline viewing |
| RISC OS Module Parse | (after file extraction, filetype FFA) | Extracts module title, version, star commands, and SWIs for search |
| Disc Protection Detect | HFE | Scans for copy-protection indicators |
| Disc Mastering Detect | HFE | Scans trailing tracks for mastering fingerprints |
| Product Recognition | (after file extraction) | Matches extracted files against known-product hash databases |
| ARMlock Remove | (manual) | Removes ARMlock disc security from RISC OS disc images |

### Monitoring the Queue

Go to **Analysis** in the navigation bar. You can filter by status (Pending,
Running, Completed, Failed) and click through to individual analysis details.
Use the per-page selector to control how many rows are shown.

The **View Queue** link shows pending and running jobs in priority order with
cancel buttons. Jobs that have been running longer than the stale timeout
(default 1 hour) are highlighted with a warning badge. Click **Reset Stale
Jobs** to reset any stuck jobs back to pending so they can be claimed by a
worker again.

### Retrying Failed Analyses

If an analysis fails, its detail page shows the error message and a **Retry**
button that resets the job to pending.

---

## Search

The **Search** page (`/search/`) provides a powerful structured query syntax for
finding items, artefacts, and extracted files across your entire collection.

### Basic Usage

Type plain words to search item and artefact names and descriptions:

```
BBC Micro
```

### Prefix Queries

Use `key:value` prefixes to search specific fields. Multiple values for the
same key are OR'd together; different keys are AND'd.

| Key | Aliases | What it searches |
|-----|---------|-----------------|
| `filename:` | `file:` | Extracted file path |
| `path:` | | Extracted file path (same as `filename:`) |
| `ext:` | | File extension (e.g. `ext:bas`) |
| `type:` | `filetype:` | RISC OS filetype -- 3-digit hex code (e.g. `type:fea`) or name (e.g. `type:Desktop`) |
| `label:` | `disc:` | Partition / disc label |
| `ident:` | `gnu:`, `gnufile:` | GNU `file` type identification string |
| `fs:` | `filesystem:` | Filesystem type (e.g. `fs:adfs`, `fs:fat`) |
| `protection:` | `prot:` | Copy-protection indicator type (e.g. `protection:bad_crc`) |
| `mastering:` | | Mastering indicator type (e.g. `mastering:formaster`) |
| `tag:` | | Artefact tag |
| `md5:` | | MD5 hash |
| `sha1:` | | SHA-1 hash |
| `sha256:` | | SHA-256 hash |
| `module:` | | RISC OS module title (e.g. `module:WindowManager`) |
| `command:` | | RISC OS star command provided by a module (e.g. `command:Desktop`) |
| `swi:` | | RISC OS SWI name provided by a module (e.g. `swi:Wimp_Poll`) |

### Wildcards and Quoting

Use `*` as a wildcard in any value:

```
filename:*.bas
type:ff*
```

Quote multi-word values:

```
label:"Boot Disc"
```

### Negation

Prefix any keyed term with `!` to exclude matches:

```
type:Basic !type:Obey                 # BASIC files, but not Obey files
tag:bbc-micro !tag:demo               # Tagged "bbc-micro" but not "demo"
```

A negated term refines an existing search — it never produces results on its
own, so a query must contain at least one non-negated term.  (A bare word
beginning with `!`, such as the RISC OS filename `!Boot`, is treated as a
literal search term, not a negation.)

### Examples

```
filename:!RunImage                    # Find a specific RISC OS file
type:Squash                           # All files with RISC OS filetype "Squash"
type:fca                              # Same thing, using the hex code
protection:bad_crc fs:adfs            # Protected ADFS discs
tag:bbc-micro ext:bas                 # BASIC files tagged "bbc-micro"
tag:bbc-micro !tag:demo               # Tagged "bbc-micro" but not "demo"
md5:d41d8cd98f00b204e9800998ecf8427e  # Find a file by hash
mastering:formaster                   # Discs with Formaster mastering data
module:WindowManager                  # Find the WindowManager module
command:Desktop                       # Find modules providing the *Desktop command
swi:Wimp_Poll                         # Find modules providing the Wimp_Poll SWI
```

### Result Buckets

Search results are grouped into three sections:

- **Catalogue Items** -- items whose name or description matches bare-word terms
- **Artefacts** -- artefacts matching partition, protection, mastering, tag, or
  hash queries
- **Files** -- individual extracted files matching filename, path, type, or hash
  queries

Each bucket is capped at 200 results. A notice is shown if results were
truncated.

---

## Taxonomy

Taxonomy pages let you organise your collection. Go to **Taxonomy** in the
navigation bar to see the five taxonomy areas.

### Platforms

Platforms represent computer systems and hardware (e.g. "BBC Micro",
"Acorn Archimedes", "IBM PC"). Platforms are hierarchical -- you can nest
sub-platforms under a parent (e.g. "Acorn" > "BBC Micro" > "BBC Master").

Each item can be assigned to one platform.

### Categories

Categories describe the type of software or media (e.g. "Games",
"Programming Tools", "System Software"). Like platforms, categories are
hierarchical.

Each item can be assigned to one category.

### Tags

Tags are flexible, flat labels that can be applied to items (e.g. `dfs`,
`games`, `educational`, `risc-os`). Unlike platforms and categories, an item
can have any number of tags.

### External Systems

External systems represent other cataloguing tools your collection is tracked
in (Koillection, Collective Access, a museum database, etc.). Configuring an
external system lets you link Arcology items to records in those systems.

When adding an external system you can set:

- **Name** -- display name (e.g. "Koillection")
- **System Type** -- optional classification
- **Base URL** -- the root URL of the external system
- **URL Template** -- a pattern like `/items/{id}` used to generate clickable
  links from external reference IDs

### Hash Databases

Hash databases contain collections of known file hashes (MD5, SHA-1) grouped by
product. When analysis extracts files from a disc image, it compares each file's
hash against active hash databases to identify known software.

This is useful for identifying commercial software, system ROMs, or other
well-known files in your collection.

---

## Your Account

Click your username in the top-right corner to access your profile page.

### Changing Your Password

If your account uses local authentication, enter your current password and a
new password (minimum 12 characters), then confirm the new password and click
**Change Password**.

If your account is managed by an SSO provider (shown by the provider name
instead of a password form), passwords must be changed through your identity
provider's own interface — Arcology does not store or manage your password.

### API Keys

If your account has API access enabled, the profile page shows an **API Keys**
section where you can create and manage keys for the command-line client or
other integrations.

To create a key:

1. Enter a descriptive name (e.g. "laptop CLI").
2. Choose a permission level:
   - **Read Only** -- list and view items and artefacts
   - **Read + Upload** -- also upload artefacts and queue analyses
   - **Full Read/Write** -- also create, edit, and delete items
3. Click **Create Key**.
4. Copy the key immediately -- it is shown only once. Keys begin with `arc_`.

Your key's effective permission is capped by your account's permission level.
For example, if your account is Read + Upload, a "Full Read/Write" key will
only grant Read + Upload access.

To revoke a key, click the **Revoke** button next to it. This is immediate and
cannot be undone.

For CLI setup instructions, see [CLI.md](CLI.md).

---

## Administration

The admin panel is available to administrator accounts via the gear icon in the
top-right corner.

### Managing Users

The admin panel shows all users with their permission levels and API access
status. Administrators can:

- **Create users** -- set username, password, permission level, admin status,
  and API access
- **Edit users** -- change permission level, admin status, or API access
- **Delete users** -- permanently remove a user account (requires confirmation)
- **Toggle API access** -- enable or disable a user's ability to create API keys
- **Manage download bypasses** -- the edit-user page has checkboxes to set the
  user's global per-type restriction bypasses, and lists any per-artefact
  download grants with a revoke button for each. See
  [Granting download access](#granting-download-access).

Lowering a user's permission level automatically restricts any existing API keys
to the new level.

Users whose accounts are managed by an SSO provider are shown with an
**SSO** badge next to their username. Their permission level and API access
are synchronised from the provider's role assignments on each login, so manual
changes here may be overridden at next sign-in. Password fields are hidden for
SSO-managed accounts. To change an SSO user's permissions permanently, update
their role assignments in the identity provider.

See [SSO.md](SSO.md) for full configuration instructions.

### Worker API Key

The admin panel displays the configured `WORKER_API_KEY` used by analysis
workers to authenticate with the API. This is set as an environment variable
and shown here for reference (e.g. when configuring additional worker
instances).
