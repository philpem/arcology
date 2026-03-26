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

Navigate to your Arcology instance (e.g. `http://localhost:8000`) and log in
with the username and password provided by your administrator. All pages
except the health-check API require authentication.

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

### Browsing Items

Go to **Items** in the top navigation bar. The list page shows every item with
its platform, category, artefact count, and last-updated date.

Use the filters at the top of the page to narrow results:

- **Search** -- text search across item names and descriptions
- **Platform** -- dropdown filter by platform
- **Category** -- dropdown filter by category

Click an item name to view its detail page.

### Creating an Item

1. Click **New Item** (on the items list or dashboard).
2. Fill in the form:
   - **Name** (required) -- a short descriptive title
   - **Description** -- optional free-text notes
   - **Platform** -- the computer system this item relates to
   - **Category** -- the type of software or media
   - **Tags** -- comma-separated labels (e.g. `bbc-micro, games, dfs`)
3. Click **Save**.

### Editing and Deleting Items

On the item detail page, click **Edit** to change any field, or **Delete** to
remove the item and all its artefacts permanently.

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
4. Optionally set a label (defaults to the original filename) and description.
5. The **Auto Analyse** checkbox (on by default) queues appropriate analyses
   immediately after upload.
6. Click **Upload**.

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
- **Analyses** -- status and results of each analysis run on this artefact
- **Partitions and file listings** -- if file extraction has run, the extracted
  directory tree is shown grouped by partition
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
other reasons). If a restriction applies:

- Users with a bypass for that restriction type see a
  **Download (Override)** button.
- Users without a bypass see a disabled **Restricted** button with the reason.

### Editing and Deleting

Click **Edit** to change the label, type override, description, or tags.
Click **Delete** to permanently remove the artefact and all its analyses.

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
| Disc Protection Detect | HFE | Scans for copy-protection indicators |
| Disc Mastering Detect | HFE | Scans trailing tracks for mastering fingerprints |
| Product Recognition | (after file extraction) | Matches extracted files against known-product hash databases |
| ARMlock Remove | (manual) | Removes ARMlock disc security from RISC OS disc images |

### Monitoring the Queue

Go to **Analysis** in the navigation bar. You can filter by status (Pending,
Running, Completed, Failed) and click through to individual analysis details.

The **View Queue** link shows pending and running jobs in priority order with
cancel buttons.

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

### Examples

```
filename:!RunImage                    # Find a specific RISC OS file
type:Squash                           # All files with RISC OS filetype "Squash"
type:fca                              # Same thing, using the hex code
protection:bad_crc fs:adfs            # Protected ADFS discs
tag:bbc-micro ext:bas                 # BASIC files tagged "bbc-micro"
md5:d41d8cd98f00b204e9800998ecf8427e  # Find a file by hash
mastering:formaster                   # Discs with Formaster mastering data
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

Enter your current password and a new password (minimum 12 characters), then
confirm the new password and click **Change Password**.

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

Lowering a user's permission level automatically restricts any existing API keys
to the new level.

### Worker API Key

The admin panel displays the configured `WORKER_API_KEY` used by analysis
workers to authenticate with the API. This is set as an environment variable
and shown here for reference (e.g. when configuring additional worker
instances).
