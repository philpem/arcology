# Searching

The search bar accepts free text and a set of prefix keys for targeted lookups.  You can mix free text and prefix keys in a single query.

---

## Free Text

Bare words (without a `key:` prefix) match against item names and artefact labels.

```
elite bbc micro
```

Finds items or artefacts whose name contains "elite", "bbc", or "micro".

---

## Prefix Keys

A prefix key constrains the search to a specific field:

```
key:value
key:"value with spaces"
```

Multiple values for the same key are combined with **OR** — any match satisfies it:

```
filename:readme.txt filename:read.me
```

Finds files named `readme.txt` **or** `read.me`.

Different keys are combined with **AND** — all must match:

```
filename:*.bas tag:needs-review
```

Finds `.bas` files **and** only in artefacts tagged `needs-review`.

---

## Wildcards

Use `*` as a wildcard within a value.  It matches any sequence of characters.

```
filename:*.bas        → all files ending in .bas
filename:elite*       → files starting with "elite"
filename:*disc*       → files containing "disc"
```

**`filename:` uses exact match by default** — `filename:readme` finds only a file named exactly `readme`, not `readme.txt` or `readme.old`.  Use a wildcard to broaden the search: `filename:readme*` matches `readme`, `readme.txt`, and `readme.old`.

**`path:` uses substring match by default** — `path:games` finds any file whose full path contains `games`.

> **Note:** `*` is a simple wildcard, not a regular expression.  Use `filename:*foo*` if you need to match anywhere within a filename.

---

## File Search Keys

These search the files extracted from disc images and archives.

| Key | Aliases | Matches |
|-----|---------|---------|
| `filename:` | `file:` | Filename only (no path) |
| `path:` | | Full path including directories |
| `ext:` | | File extension, including the dot: `ext:.bas` |
| `md5:` | | MD5 hash (exact, lowercase hex) |
| `sha1:` | | SHA-1 hash (exact, lowercase hex) |
| `sha256:` | | SHA-256 hash (exact, lowercase hex) |
| `type:` | `filetype:` | RISC OS filetype — hex code or name (see below) |
| `ident:` | `gnu:`, `gnufile:` | File format from magic-byte detection |

### Path Separator

Paths are always stored with **`/` as the separator**, regardless of the source filesystem.  RISC OS uses `.` (dot) internally, but when files are extracted to Linux the directory structure uses `/`, and that is what is stored in the database and shown in breadcrumb trails.

```
path:!Boot/!Run         → file !Run inside directory !Boot
path:Games/Elite*       → files starting with "Elite" inside a Games directory
path:*fonts*            → any path containing "fonts"
```

Use `path:*dirname*` to find a directory by name without caring about its position in the hierarchy.

### RISC OS Filetype Search

The `type:` key accepts either a 3-digit hex code or a human-readable RISC OS filetype name:

```
type:ffd         → BASIC programs (filetype &FFD)
type:BASIC       → same as type:ffd
type:ff9         → Sprite files
type:Sprite      → same as type:ff9
type:fea         → Desktop files
```

Filetype names are case-insensitive.  Unknown names fall back to a literal hex match.

---

## Partition and Filesystem Keys

These search the disc partitions detected inside images.

| Key | Aliases | Matches |
|-----|---------|---------|
| `fs:` | `filesystem:` | Filesystem type (see values below) |
| `label:` | `disc:` | Disc/partition label |
| `ident:` | `gnu:` | GNU `file` output for the disc image itself |

### Filesystem Values for `fs:`

| Value | Filesystem |
|-------|-----------|
| `adfs` | Acorn ADFS |
| `dfs` | Acorn DFS |
| `fat12`, `fat16`, `fat32` | FAT variants |
| `ntfs` | NTFS |
| `hfs`, `hfs_plus` | Apple HFS / HFS+ |
| `iso9660` | ISO 9660 (CD-ROM) |
| `cdfs` | CD filesystem |
| `amiga_ofs`, `amiga_ffs` | Amiga OFS / FFS |
| `cpm` | CP/M |
| `archive` | Archive format (treated as a partition) |
| `unknown` | Unrecognised filesystem |

If `fs:` cannot match a known filesystem type, it falls back to matching the detailed format description string (e.g. `fs:"Acorn ADFS E"`).

---

## Protection and Mastering Keys

These search the results of copy-protection and mastering analysis.

| Key | Matches |
|-----|---------|
| `protection:` | Copy protection indicator type |
| `mastering:` | Mastering fingerprint type |

### Protection Values

```
protection:bad_crc
protection:weak_bits
protection:id_mismatch
protection:ddam
protection:no_flux
protection:short_track
protection:long_track
```

### Mastering Values

```
mastering:traceback
mastering:formaster
mastering:duplicator_fingerprint
```

> **Tip:** The values actually present in the database are shown in the expandable **Known types** section on the Search page.  If a value isn't listed there, no artefacts have that indicator recorded yet.

---

## RISC OS Module Keys

These search the results of **RISCOS\_MODULE\_PARSE** analysis — only populated for artefacts that contain RISC OS module files.

| Key | Matches |
|-----|---------|
| `module:` | Module title (e.g. `module:Desktop`) |
| `command:` | `*command` name (e.g. `command:Run`) |
| `swi:` | SWI name (e.g. `swi:Wimp_Poll`) |

---

## Tag Key

```
tag:needs-review
tag:commercial tag:bbc-micro
```

Matches items or artefacts with the given tag.  Multiple `tag:` values are OR'd — the second example finds anything tagged `commercial` **or** `bbc-micro`.

---

## Result Limits

Each result bucket (files, partitions, artefacts, items) is capped at **200 results**.  If a query hits the cap, a notice is shown.  Refine your query to narrow the results — add more specific terms or combine keys to reduce the match set.

---

## Examples

| Query | Finds |
|-------|-------|
| `elite` | Items and artefacts with "elite" in their name |
| `md5:d41d8cd98f00b204e9800998ecf8427e` | The specific file with that MD5 |
| `filename:*.bas type:ffd` | BASIC files (by extension AND RISC OS filetype) |
| `protection:bad_crc tag:original` | Protected originals |
| `fs:adfs filename:!boot` | `!Boot` files on ADFS volumes |
| `module:Desktop swi:Wimp_Poll` | Modules that provide `Wimp_Poll` and are called Desktop |
| `mastering:traceback` | Discs with a Traceback mastering fingerprint |
| `sha256:abc123` | File with that exact SHA-256 hash |
| `path:*Games* filename:loader` | Files named "loader" inside a directory named "Games" |
| `path:!Boot/!Run` | File `!Run` directly inside directory `!Boot` (RISC OS disc) |
