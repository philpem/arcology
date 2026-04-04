# arco — Arcology Command-Line Client

`arco` is a command-line client for Arcology. Use it to create and manage items,
upload artefacts, and query your collection from a terminal or script — without
opening a browser.

## Installation

```bash
pip install -e cli/
```

From a development checkout, the `arco` command is available immediately.
No shared libraries are required: `arco` communicates with Arcology entirely
over HTTP.

## Configuration

`arco` resolves configuration in three layers; higher layers override lower ones:

| Priority | Source | Notes |
|----------|--------|-------|
| 1 (highest) | CLI flags | `--server`, `--api-key` |
| 2 | Environment variables | `ARCOLOGY_URL`, `ARCOLOGY_API_KEY` |
| 3 (lowest) | Config file | `~/.config/arcology/config.ini` |

### First-time setup

```bash
arco configure
```

This prompts for a server URL and API key, then writes
`~/.config/arcology/config.ini` with permissions set to `0600`:

```ini
[default]
url = http://localhost:8000
api_key = arc_xxxxxxxxxxxxxxxxxxxx
```

### Multiple profiles

Store credentials for several servers in the same config file:

```ini
[default]
url = http://localhost:8000
api_key = arc_dev_key

[production]
url = https://arcology.example.com
api_key = arc_prod_key
```

Select a profile with `--profile`:

```bash
arco --profile production items list
```

### API keys

API keys are managed from the Arcology web UI under your account settings.
Keys start with `arc_`. Three permission levels are available:

- **read_only** — list and view items/artefacts; query taxonomy
- **read_upload** — read_only + upload artefacts
- **read_write** — read_upload + create/update/delete items

## Global flags

These flags can be placed before any command:

| Flag | Description |
|------|-------------|
| `--server URL` | Override server URL |
| `--api-key KEY` | Override API key |
| `--profile NAME` | Use a named config profile (default: `default`) |
| `--json` | Print raw JSON output (useful for scripting) |

## Commands

### `arco health`

Test connectivity to the server.

```
arco health
```

```
Server: http://localhost:8000
Status: ok
```

---

### `arco configure`

Interactively create or update the config file.

```
arco configure
arco --profile staging configure
```

---

### `arco items list`

List items in the catalogue.

```
arco items list
arco items list --search "BBC Micro"
arco items list --platform 3
arco items list --category 7 --per-page 50
arco items list --tag "acorn"
arco items list --page 2
arco items list --parent ITEM_UUID   # list children of a specific item
```

| Flag | Description |
|------|-------------|
| `--search TEXT`, `-s TEXT` | Filter by name or description |
| `--platform ID`, `-p ID` | Filter by platform ID |
| `--category ID`, `-c ID` | Filter by category ID |
| `--tag NAME`, `-t NAME` | Filter by tag name |
| `--parent UUID` | Show only direct children of this item |
| `--page N` | Page number (default: 1) |
| `--per-page N` | Results per page (default: 25) |

Use `arco platforms` and `arco categories` to find IDs.

---

### `arco items create`

Create a new item.

```
arco items create --name "BBC Micro DFS Disc"
arco items create --name "Amiga Workbench 2.1" --platform 5 --category 3
arco items create --name "Archimedes Games" --tags "acorn,risc-os,games"
arco items create --name "Side A" --parent PARENT_UUID   # create as child item
```

| Flag | Description |
|------|-------------|
| `--name TEXT`, `-n TEXT` | **Required.** Item name |
| `--description TEXT`, `-d TEXT` | Optional description |
| `--platform ID`, `-p ID` | Platform ID |
| `--category ID`, `-c ID` | Category ID |
| `--tags NAMES` | Comma-separated tag names |
| `--parent UUID` | Parent item UUID (makes this a child of that item) |

Prints the new item's UUID on success.

---

### `arco items view`

Show full details of an item including its artefacts.

```
arco items view a1b2c3d4e5f6...
```

---

### `arco items update`

Change fields on an existing item.

```
arco items update a1b2c3d4... --name "Corrected Title"
arco items update a1b2c3d4... --platform 3 --category 2
arco items update a1b2c3d4... --parent PARENT_UUID   # move under a parent
arco items update a1b2c3d4... --no-parent            # make a root item
```

| Flag | Description |
|------|-------------|
| `--name TEXT`, `-n TEXT` | New name |
| `--description TEXT`, `-d TEXT` | New description |
| `--platform ID`, `-p ID` | New platform ID |
| `--category ID`, `-c ID` | New category ID |
| `--parent UUID` | Move item under a new parent |
| `--no-parent` | Remove parent (make a root-level item) |

---

### `arco items delete`

Delete an item and all its artefacts. **This cannot be undone.**

```
arco items delete a1b2c3d4...
arco items delete a1b2c3d4... --yes   # skip confirmation
```

---

### `arco artefacts move`

Move a root artefact to a different item. Derived artefacts follow
automatically. The artefact's slug is re-checked for uniqueness in the target
item, with automatic collision resolution if needed.

```
arco artefacts move ARTEFACT_UUID --to TARGET_ITEM_UUID
```

| Argument/Flag | Description |
|---------------|-------------|
| `ARTEFACT_UUID` | UUID of the artefact to move (must be a root artefact, not derived) |
| `--to UUID` | **Required.** UUID of the target item |

---

### `arco upload`

Upload one or more files as artefacts on an item.

```
# Single file
arco upload ITEM_UUID disc1.scp

# Single file with explicit label and type override
arco upload ITEM_UUID image.img --label "Side A" --type RAW_SECTOR

# Multiple files (labels derived from filenames)
arco upload ITEM_UUID side_a.hfe side_b.hfe

# All files in a directory
arco upload ITEM_UUID --dir ./disc_images/

# Skip automatic analysis
arco upload ITEM_UUID archive.zip --no-analyse
```

| Argument/Flag | Description |
|---------------|-------------|
| `ITEM_UUID` | UUID of the item to attach artefacts to |
| `FILE...` | One or more files to upload |
| `--dir PATH` | Upload all files found recursively in PATH |
| `--label TEXT`, `-l TEXT` | Label for the artefact (single-file upload only; auto-derived from filename for multi-file) |
| `--type TYPE`, `-t TYPE` | Override server's auto-detected artefact type |
| `--no-analyse` | Do not queue automatic analysis after upload |

**Type detection:** The server identifies the artefact type from the file
extension and content. Use `--type` only when you need to override this —
for example, uploading a raw sector image with a `.img` extension that the
server would otherwise classify as a generic unknown format. Common type
values: `SCP`, `HFE`, `IMD`, `ADF`, `RAW_SECTOR`, `FLUX_GENERIC`,
`KRYOFLUX_STREAM`, `ZIP`, `TAR`, `UNKNOWN`.

**Directory uploads:** junk files (`.DS_Store`, `Thumbs.db`, `desktop.ini`,
`__MACOSX/`, etc.) are silently skipped.

**Integrity checking:** `arco` computes MD5 and SHA256 hashes of each file
before uploading and compares them against the hashes reported by the server.
A warning is printed if they do not match.

---

### `arco download`

Download an artefact file to disk.

```
arco download ARTEFACT_UUID
arco download ARTEFACT_UUID --output /tmp/copy.scp
arco download ARTEFACT_UUID --output ./downloads/ --force
```

| Flag | Description |
|------|-------------|
| `--output PATH`, `-o PATH` | Output path (file or directory; default: original filename in current directory) |
| `--force`, `-f` | Overwrite an existing file |

---

### `arco status`

Show analysis status for an artefact: type, filename, and any extracted
partitions or file listings.

```
arco status ARTEFACT_UUID
```

---

### `arco debug analysis`

Show full details for a single analysis job, including tool info, timing,
process logs (command, exit code, stdout/stderr), and exception traces.

```
arco debug analysis ANALYSIS_UUID
arco debug analysis ANALYSIS_UUID --json
```

---

### `arco debug errors`

Show all failed analyses for an artefact and all its derived descendants
in one view.

```
arco debug errors ARTEFACT_UUID
arco debug errors ARTEFACT_UUID --all    # include non-failures too
```

| Flag | Description |
|------|-------------|
| `--all` | Show all analyses, not just failures |

---

### `arco debug tree`

Show the artefact derivation tree: artefact → analyses → produced artefacts,
recursively.  Each analysis shows a status icon (`+` completed, `X` failed,
`~` pending/running) and truncated error message if failed.

```
arco debug tree ARTEFACT_UUID
```

Example output:

```
[scp] MyDisc.scp (a1b2c3d4)
  + flux_decode  completed  greaseweazle 1.2
    [raw_sector] MyDisc.img (e5f6g7h8)
      + file_extraction  completed  adfutils 0.3
      X metadata_extract  FAILED  "Unsupported ADFS variant"
```

---

### `arco debug processing-tree`

Show the full processing pipeline for an artefact as a tree: artefacts,
their analyses (flat and grouped by file path for archive/format-convert
operations), and all derived child artefacts, recursively. Always shows the
root artefact even if a derived artefact UUID is given.

```
arco debug processing-tree ARTEFACT_UUID
arco debug processing-tree ARTEFACT_UUID --json
```

---

### `arco debug failures`

Search failed analyses across the entire system with optional filters.

```
arco debug failures
arco debug failures --type file_extraction
arco debug failures --tool 7z --since 2025-01-01
arco debug failures --error "BadZipFile" --per-page 100
```

| Flag | Description |
|------|-------------|
| `--type TYPE` | Filter by analysis type (e.g. `file_extraction`, `flux_decode`) |
| `--tool NAME` | Filter by tool name (e.g. `7z`, `fluxfox`) |
| `--since DATE` | Only failures after this date (ISO format) |
| `--until DATE` | Only failures before this date (ISO format) |
| `--error TEXT` | Filter by error message substring |
| `--page N` | Page number (default: 1) |
| `--per-page N` | Results per page (default: 50) |

---

### `arco platforms`

List all platforms with their IDs.

```
arco platforms
arco platforms --json
```

---

### `arco categories`

List all categories with their IDs.

```
arco categories
arco categories --json
```

---

### `arco tags`

List all tags with their IDs.

```
arco tags
arco tags --json
```

---

## Scripting and JSON output

Every command accepts `--json` to print raw API JSON instead of the formatted
table. This is useful for piping into `jq`:

```bash
# Get the UUID of a newly created item
UUID=$(arco --json items create --name "Test Item" | jq -r '.uuid')

# Upload a file to it
arco upload "$UUID" myfile.scp

# List all items on platform 3 as JSON
arco --json items list --platform 3 | jq '.items[].name'
```

---

## Bulk import example

Create an item, upload a directory of disc images, and check analysis status:

```bash
# Create the item
UUID=$(arco --json items create \
    --name "Acorn BBC Micro Software Collection" \
    --platform 1 \
    --category 4 \
    --tags "bbc-micro,dfs" \
  | jq -r '.uuid')

echo "Created item: $UUID"

# Upload all images in the directory
arco upload "$UUID" --dir ./bbc_discs/

# Check the item in the web UI
echo "View at: http://localhost:8000/items/$UUID"
```

---

## Error messages

| Message | Meaning |
|---------|---------|
| `Authentication failed. Check your API key.` | API key missing or wrong |
| `Insufficient permissions.` | Key exists but lacks the required permission level |
| `Not found.` | UUID does not exist or is not visible with this key |
| `File too large for server.` | Upload exceeds the server's `MAX_CONTENT_LENGTH` (default 4 GB) |
| `Cannot connect to …` | Network error or server is not running |
| `No server URL configured.` | Config file missing and no `--server` / `ARCOLOGY_URL` set |

All errors are printed to stderr and produce a non-zero exit code.

---

## See also

- [CLAUDE.md](../CLAUDE.md) — development guide and API overview
- [CONTRIBUTING.md](../CONTRIBUTING.md) — architecture and contribution workflow
