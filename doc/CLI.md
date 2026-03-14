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
```

| Flag | Description |
|------|-------------|
| `--search TEXT`, `-s TEXT` | Filter by name or description |
| `--platform ID`, `-p ID` | Filter by platform ID |
| `--category ID`, `-c ID` | Filter by category ID |
| `--tag NAME`, `-t NAME` | Filter by tag name |
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
```

| Flag | Description |
|------|-------------|
| `--name TEXT`, `-n TEXT` | **Required.** Item name |
| `--description TEXT`, `-d TEXT` | Optional description |
| `--platform ID`, `-p ID` | Platform ID |
| `--category ID`, `-c ID` | Category ID |
| `--tags NAMES` | Comma-separated tag names |

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
```

| Flag | Description |
|------|-------------|
| `--name TEXT`, `-n TEXT` | New name |
| `--description TEXT`, `-d TEXT` | New description |
| `--platform ID`, `-p ID` | New platform ID |
| `--category ID`, `-c ID` | New category ID |

---

### `arco items delete`

Delete an item and all its artefacts. **This cannot be undone.**

```
arco items delete a1b2c3d4...
arco items delete a1b2c3d4... --yes   # skip confirmation
```

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
