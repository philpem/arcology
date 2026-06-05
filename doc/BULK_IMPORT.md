# Bulk Import Tool

`arco bulk-import` walks a local directory tree and imports files into
Arcology as Items and Artefacts. The Arcology worker pipeline then handles
extraction, hashing, archive detection, file listing, and product recognition
automatically.

## Prerequisites

- A running Arcology instance with the REST API accessible
- An API key with read/write permissions (`WORKER_API_KEY` or a dedicated key)
- The `arco` CLI installed (`pip install -e cli/` or `pipx install git+https://github.com/philpem/arcology.git#subdirectory=cli`)
- A local copy of the archive to import

## Quick start

```bash
# Preview what would be imported:
arco bulk-import \
    --archive-dir ~/my-archive --tag myimport \
    --dry-run -v

# Import everything:
arco bulk-import \
    --archive-dir ~/my-archive --tag myimport \
    --api-key YOUR_KEY

# Import a flat directory of disc images as a single Item:
arco bulk-import \
    --archive-dir ~/my-discs --tag discs --flat \
    --api-key YOUR_KEY

# Import hard-drive images, each zipped with its ddrescue map / readme / logs:
arco bulk-import \
    --archive-dir ~/hdd-images --tag hdd-archive --bundle-sidecars \
    --api-key YOUR_KEY

# Import the arcarc.nl archive (preset handles tag, prefix, categories):
arco bulk-import \
    --archive-dir ~/arcarc/archive --arcarc \
    --api-key YOUR_KEY

# Resume after interruption:
arco bulk-import \
    --archive-dir ~/my-archive --tag myimport \
    --api-key YOUR_KEY --resume
```

## How it works

### Import modes

The tool has two import modes:

**Default (categorised) mode** groups files by top-level subdirectory. Each
subdirectory becomes a separate Item. Files sitting directly in the archive-dir
(one level deep, not in any subdirectory) are grouped into a single Item named
after the archive directory itself.

**Flat mode** (`--flat`) treats the entire archive-dir as a single Item. All
files, including those in subdirectories, become Artefacts on that one Item.
The Item is named after the directory itself.

### Importable file types

Only files with recognised extensions are imported. Everything else (including
`desc.txt`, `README`, etc.) is silently skipped.

| Extensions | Type |
|------------|------|
| `.adf`, `.img`, `.ima`, `.dsk`, `.dd`, `.raw`, `.bin`, `.hdd`, `.hdf`, `.image` | Raw-sector / disk images |
| `.scp` | Flux images |
| `.imd`, `.hfe` | Sector floppy images |
| `.iso` | CD/DVD images |
| `.zip`, `.7z`, `.rar` | Archives |
| `.tar.gz`, `.tgz` | Compressed tarballs |
| *(any raw-sector ext)*`.zst` / `.gz` / `.bz2` | Compressed disk images (e.g. `.dd.zst`, `.img.gz`) |
| `.pdf` | Documents |

Any raw-sector / disk-image extension may carry a trailing `.zst`, `.gz` or
`.bz2` compressor suffix — convention is to compress the original image
immediately after imaging the drive.

### Compressed-duplicate filtering

When the same image appears in several forms in one directory — for example
`drive.dd`, `drive.dd.zst` and `drive.zip` — only the **best** form is uploaded
by default, in the order **archive > compressed > raw** (`.zip`/`.7z` beats
`.dd.zst`/`.gz`/`.bz2`, which beats a bare `.dd`). Redundant compressions of the
same image (e.g. both `.dd.zst` and `.dd.gz`) are collapsed to one (`.zst`
preferred). Genuinely different images that share a base name are all kept:
different raw types (`drive.dd` vs `drive.img`), and non-image artefacts of the
same name (`drive.pdf`, `drive.iso`) are never dropped. Matching is scoped to a
single directory, so two different drives that share a name in separate folders
are never collapsed together.

Pass `--keep-compressed-duplicates` to disable this and upload every recognised
form. Dropped forms are reported during the scan (use `-v` for per-file detail).

### Sidecar bundling (`--bundle-sidecars`)

Imaging a drive often leaves loose companion files next to the image — a
ddrescue `.map`, a readme, a `.log`, checksum files. With `--bundle-sidecars`,
each disk image is zipped together with its sidecars and that single zip is
uploaded in place of the bare image. The bundle is marked so Arcology
recognises it: rather than extracting it as a generic archive, it stores the
**image itself as a single disk-image artefact** (one copy, no decompressed or
duplicated copies) and attaches the sidecars as small **companion (SIDECAR)
artefacts** of the image. The image is then analysed like a directly-uploaded
compressed image, and the `.map`/readme remain downloadable alongside it
(inline ddrescue `.map` viewing is planned).

Sidecars are matched **within the image's own directory** and comprise:

- any non-image file sharing the image's base name — e.g. for `drive.dd.zst`:
  `drive.map`, `drive.log`, `drive.txt`, `drive.dd.txt`;
- any `README*` / `CHANGELOG*` / `CHECKSUM*` file and any `.md5` / `.sha1` /
  `.sha256` / `.sha512` checksum file in that directory, even if named
  differently.

Compressed-duplicate filtering runs **first**, so when both `drive.dd` and
`drive.dd.zst` exist, the `.dd.zst` is the form placed in the zip. The bundle is
uploaded as `<base>.zip`.

**Compression inside the bundle.** The zip container itself only uses STORED or
DEFLATE, so the worker's `unzip` can always extract it. How the image is added
depends on whether it is already compressed:

- An **already-compressed** image (`.dd.zst` / `.gz` / `.bz2`) or archive is
  stored verbatim — no pointless recompression.
- A **raw, uncompressed** image (`.dd`, `.img`, `.raw`, …) is compressed with
  Zstandard (via the `zstandard` Python library — a CLI dependency — streamed
  straight into the zip, no temporary `.zst` file) into a standalone `.zst`
  member that is *stored* in the zip. The worker's `unzip` extracts the `.zst`,
  then its existing `.zst` handling decompresses it — so a never-compressed
  image still ends up compressed for transfer and storage. If `zstandard` is
  somehow unavailable it falls back to fast Deflate (zlib level 1, the
  `gzip --fast` equivalent).
- Text sidecars are always lightly deflated.

Notes:

- Only raw-sector / disk images are bundled. A file that is already a `.zip` /
  `.7z` archive, or a non-image type (`.iso`, `.scp`, `.pdf`), is uploaded as-is.
- An image with no sidecars is uploaded directly (no pointless single-file zip).
- In a folder holding several images, a *generic* readme/checksum (one not tied
  to a base name) is copied into each image's bundle. In the common
  one-folder-per-drive layout there is exactly one image, so no duplication.
- Bundling writes a temporary zip on the order of the (compressed) image size —
  for a raw image, the intermediate `.zst` plus the zip copy of it. Use
  `--bundle-tmpdir` to place this on a filesystem with enough free space
  (default: system temp).

### Skipping over-large files (`--max-size`)

`--max-size SIZE` skips any **source file** larger than the limit and logs that
it happened, rather than uploading it. Useful when an archive contains the
occasional multi-hundred-gigabyte raw image you do not want to transfer.

```bash
arco bulk-import --archive-dir ~/hdd-images --tag hdd --max-size 50G
```

`SIZE` accepts a plain byte count or a `K`/`M`/`G`/`T` suffix (1024-based; a
trailing `B` is allowed, e.g. `50GB`). The limit is measured on the source file
on disk, **before** any bundling or compression — so a raw `drive.dd` is judged
by its uncompressed size. Skipped files are listed in the run summary.

### Artefact labels

The artefact label is what appears in the Arcology UI to identify each file.

**Default labels** use the path below the top-level directory (categorised
mode) or the relative path from archive-dir (flat mode).

**Smart labels** (`--smart-labels` or `--arcarc`) apply two heuristics to
produce shorter labels:

1. **Single-character directory groupings** (A-Z, 0-9) are stripped. These
   are alphabetical index directories common in large archives.
2. **Self-describing filenames** are used alone. If the filename starts with
   its parent directory name, the parent path is redundant.

When neither heuristic applies (e.g. a file called `as.zip` inside
`gcc-2.7.2.1/`), the full context path is preserved to avoid ambiguity.

### Categories

Categories are **not required**. Without `--categories`, all top-level
directories are imported. The `--categories` flag is a filter that selects
which top-level directories to process (e.g. `--categories Apps` imports only
the `Apps/` subdirectory).

The `--category-map` controls which Arcology category is assigned to each
Item. Without it, directory names are used as-is — if no matching Arcology
category exists, the Item simply won't have a category assigned. The arcarc
preset includes a built-in map (Apps=Applications, PD=Public Domain, etc.).

### Tagging and deduplication

`--tag` is optional. When supplied, every imported Item is tagged with it plus
the lowercase collection name (e.g. `apps`, `games`); without it, Items get only
the lowercase collection-name tag. The tag is used for:

- **Resume mode** (`--resume`): matches existing Items by name + tag to avoid
  creating duplicates, then skips Artefacts whose filename already exists.
  Without `--tag`, matching falls back to name alone, which may match an
  unrelated Item of the same name — pass `--tag` when resuming to be precise.
- **Purge mode** (`--purge`): finds all Items with the tag for bulk deletion.
  `--tag` is **required** with `--purge` (otherwise it would match every Item).

## Examples

### 1. Default mode (categorised)

```
~/archive/
├── Apps/
│   ├── Editor/
│   │   └── Editor 1.0.zip
│   └── Viewer/
│       └── Viewer 2.0.zip
├── Games/
│   └── Chess/
│       └── Chess 1.5.adf
└── top-level.zip                  ← grouped into an "archive" Item
```

```bash
arco bulk-import --archive-dir ~/archive --tag myimport
```

| Item | Artefact label | Source path |
|------|---------------|-------------|
| `Apps` | `Editor/Editor 1.0.zip` | `Apps/Editor/Editor 1.0.zip` |
| `Apps` | `Viewer/Viewer 2.0.zip` | `Apps/Viewer/Viewer 2.0.zip` |
| `Games` | `Chess/Chess 1.5.adf` | `Games/Chess/Chess 1.5.adf` |
| `archive` | `top-level.zip` | `top-level.zip` |

Three Items created. `top-level.zip` sits directly in the archive root, so it is
grouped into an Item named after the archive directory (`archive`). No Arcology
category is assigned (no `--category-map`).

### 2. With name prefix and category map

Same directory as above:

```bash
arco bulk-import --archive-dir ~/archive --tag myimport \
    --name-prefix "Source" --category-map "Apps=Applications"
```

| Item | Arcology category | Artefact label |
|------|------------------|---------------|
| `Source: Apps` | Applications | `Editor/Editor 1.0.zip` |
| `Source: Games` | Games *(directory name used as-is — not in map)* | `Chess/Chess 1.5.adf` |

### 3. Flat mode

```
~/my-discs/
├── system-disc.adf
├── games-disc.adf
└── backups/
    ├── jan/
    │   └── backup-jan.img
    └── feb/
        └── backup-feb.img
```

```bash
arco bulk-import --archive-dir ~/my-discs --tag discs --flat
```

| Item | Artefact label | Source path |
|------|---------------|-------------|
| `my-discs` | `system-disc.adf` | `system-disc.adf` |
| `my-discs` | `games-disc.adf` | `games-disc.adf` |
| `my-discs` | `backups/jan/backup-jan.img` | `backups/jan/backup-jan.img` |
| `my-discs` | `backups/feb/backup-feb.img` | `backups/feb/backup-feb.img` |

One Item (`my-discs`), four Artefacts. Top-level files use just the filename;
nested files use their relative path. Note that in default (non-flat) mode,
`system-disc.adf` and `games-disc.adf` would be skipped because they are not
inside a subdirectory.

### 4. Nesting created Items under a parent

Use `--parent` to group every imported Item as a child of an existing Item
(for example, a collection or donor record). The parent must already exist;
its UUID is validated before the import begins.

```bash
arco bulk-import --archive-dir ~/archive --tag myimport \
    --parent 3f9c2a1b4d5e6f708192a3b4c5d6e7f8
```

Each created Item (`Apps`, `Games`, …) becomes a child of the given parent.
The parent is only applied when an Item is created; Items reused via
`--resume` (matched by name and tag) keep their existing parent.

### 5. Arcarc preset with deep nesting and smart labels

```
~/arcarc/archive/
├── Apps/
│   ├── A/
│   │   ├── ArcFS 2 (FR)/
│   │   │   ├── ArcFS 2 (FR) 0.62 (1993)(Mark Smith).zip
│   │   │   └── ArcFS 2 (FR) (1995)(VTI).zip
│   │   └── Advance/
│   │       └── Acorn Advance 1.01 (1993)(Acorn)(Disk 1).adf
│   └── G/
│       └── GCC (FR)/
│           ├── gcc-2.7.2.1/
│           │   ├── as.zip
│           │   └── cc1.zip
│           ├── gcc-4.7.4-release-5/
│           │   ├── system.zip
│           │   └── gcc.zip
│           └── varia/
│               └── GCCSDK.pdf
├── Games/
│   └── ...
└── !Compilations/
    └── Warm Silence Software Emulators/
        └── Emulators 2.00 (1996)(Warm Silence Software).zip
```

```bash
arco bulk-import --archive-dir ~/arcarc/archive --arcarc
```

| Item | Arcology category | Artefact label | Why |
|------|------------------|----------------|-----|
| `Arcarc: Apps` | Applications | `ArcFS 2 (FR) 0.62 (1993)(Mark Smith).zip` | Filename starts with `ArcFS 2 (FR)` (parent dir) — self-describing |
| `Arcarc: Apps` | Applications | `ArcFS 2 (FR) (1995)(VTI).zip` | Same |
| `Arcarc: Apps` | Applications | `Advance/Acorn Advance 1.01 (1993)(Acorn)(Disk 1).adf` | `Acorn Advance` doesn't start with `Advance` — context kept |
| `Arcarc: Apps` | Applications | `GCC (FR)/gcc-2.7.2.1/as.zip` | `as` doesn't start with `gcc-2.7.2.1` — full context from `GCC (FR)` down |
| `Arcarc: Apps` | Applications | `GCC (FR)/gcc-2.7.2.1/cc1.zip` | Same |
| `Arcarc: Apps` | Applications | `GCC (FR)/gcc-4.7.4-release-5/system.zip` | Version directory disambiguates from other `system.zip` files |
| `Arcarc: Apps` | Applications | `GCC (FR)/gcc-4.7.4-release-5/gcc.zip` | Same |
| `Arcarc: Apps` | Applications | `GCC (FR)/varia/GCCSDK.pdf` | `GCCSDK` doesn't start with `varia` |
| `Arcarc: !Compilations` | !Compilations | `Warm Silence.../Emulators 2.00...zip` | `!Compilations` is not a letter group — kept |
| `Arcarc: Games` | Games | ... | |

Smart label logic: `A/` and `G/` are stripped (single-char letter groups).
Deep nesting under `GCC (FR)/` preserves version directories so that
`system.zip` from different GCC versions remains distinguishable.
`!Compilations` is not a single character, so it is kept as context.

### 6. Using `--categories` as a filter

Same arcarc structure, but only import Apps:

```bash
arco bulk-import --archive-dir ~/arcarc/archive --arcarc --categories Apps
```

Only `Arcarc: Apps` is created. `Games`, `!Compilations`, and all other
top-level directories are skipped entirely.

Multiple categories can be selected:

```bash
arco bulk-import --archive-dir ~/arcarc/archive --arcarc --categories Apps,Games
```

## Options reference

| Option | Default | Description |
|--------|---------|-------------|
| `--archive-dir PATH` | *(required)* | Local directory to import |
| `--tag TAG` | *(none)* | Tag applied to all imported Items (required with `--purge`) |
| `--categories LIST` | *(all)* | Filter by top-level directory, comma-separated |
| `--skip-dirs LIST` | *(none)* | Skip directories by name at any level, comma-separated |
| `--skip-ext LIST` | *(none)* | Skip files by extension (e.g. `.pdf,.txt`) |
| `--platform NAME` | *(none)* | Platform to assign to all Items |
| `--name-prefix PREFIX` | *(none)* | Prefix for Item names (e.g. `--name-prefix Source` gives "Source: Apps") |
| `--parent UUID` | *(none)* | Nest all created Items under an existing parent Item |
| `--category-map K=V,...` | *(none)* | Map directory names to Arcology categories |
| `--flat` | off | Treat archive-dir as one collection (one Item, all files) |
| `--smart-labels` | off | Use smart label heuristic (strip letter groups, detect self-describing filenames) |
| `--keep-compressed-duplicates` | off | Upload every recognised image form instead of collapsing raw/compressed/archived duplicates to the best one |
| `--bundle-sidecars` | off | Zip each disk image with its loose sidecars (`.map`, readme, `.log`, checksums) and upload that instead of the bare image |
| `--bundle-tmpdir PATH` | *(system temp)* | Directory for temporary bundle zips; needs free space ≈ image size |
| `--no-auto-analyse` | off | Upload without triggering automatic analysis |
| `--arcarc` | off | Preset for arcarc.nl (see below) |
| `--resume` | off | Skip already-uploaded Artefacts |
| `--dry-run` | off | Scan and report, do not upload |
| `--verbose` / `-v` | off | Show individual file labels |
| `--purge` | off | Delete all Items with the given tag |
| `--yes` / `-y` | off | Skip confirmation prompt for `--purge` |

## The `--arcarc` preset

`--arcarc` is a convenience shorthand for importing the
[arcarc.nl](https://arcarc.nl/) RISC OS archive. It sets:

| Setting | Value |
|---------|-------|
| `--tag` | `arcarc` |
| `--name-prefix` | `Arcarc` |
| `--category-map` | Apps=Applications, Games=Games, PD=Public Domain, Demos=Demos, Education=Education, Utilities=Utilities |
| `--smart-labels` | on |

Any of these can be overridden by passing the flag explicitly. For example,
`--arcarc --tag custom` uses `custom` instead of `arcarc`.

## Cleanup

To delete all Items from a previous import:

```bash
# Shows what would be deleted, prompts for confirmation:
arco bulk-import --purge --tag myimport --api-key YOUR_KEY

# Delete without confirmation:
arco bulk-import --purge --tag myimport --api-key YOUR_KEY --yes
```

This deletes Items and all their Artefacts (including uploaded files and
analysis results) via the API's cascading delete.

## Workflow tips

- **Start with `--dry-run -v`** to preview labels and file counts before
  uploading anything.
- **Use `--categories`** to import one subdirectory at a time
  (e.g. `--categories Apps`) and verify results before doing the rest.
- **Use `--flat`** when your files are in a single directory without
  category subdirectories.
- **Use `--no-auto-analyse`** for large imports if you want to review uploads
  before queueing hundreds of analysis jobs. You can trigger analysis later
  from the web UI.
- **Use `--skip-ext .pdf`** if PDF documents aren't useful for your import.
- **Use `--resume`** to safely re-run after an interruption — it skips
  Artefacts that already exist on each Item.
- **Use `--smart-labels`** when your archive uses single-character alphabetical
  groupings (A/, B/, ...) or has filenames that repeat the parent directory name.
- **Use `--bundle-sidecars` / `--max-size`** for hard-drive imaging output —
  bundle each image with its ddrescue map/readme/logs, and skip the occasional
  image too large to upload.

### Progress during long uploads

Large images (over 100 MB) upload in chunks. When running in an interactive
terminal, a live percentage bar (`uploading <label>: 42.0% (n/m chunks)`) is
shown so a slow transfer doesn't look like a hang; when output is redirected to
a file the bar is suppressed. With `--bundle-sidecars`, the per-image line is
printed *before* compression begins (and shows the image size), so the slow
compress step is visible too.
