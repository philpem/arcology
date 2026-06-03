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
subdirectory becomes a separate Item. Files directly in the archive-dir (not
in any subdirectory) are skipped.

**Flat mode** (`--flat`) treats the entire archive-dir as a single Item. All
files, including those in subdirectories, become Artefacts on that one Item.
The Item is named after the directory itself.

### Importable file types

Only files with recognised extensions are imported. Everything else (including
`desc.txt`, `README`, etc.) is silently skipped.

| Extensions | Type |
|------------|------|
| `.adf`, `.img`, `.ima`, `.dsk`, `.dd` | Sector images |
| `.scp` | Flux images |
| `.imd`, `.hfe` | Sector floppy images |
| `.iso` | CD/DVD images |
| `.zip`, `.rar` | PC archives |
| `.tar.gz`, `.tgz` | Compressed tarballs |
| `.dd.zst`, `.dd.gz`, `.dd.bz2` | Compressed sector images |
| `.pdf` | Documents |

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

Every imported Item is tagged with `--tag` plus the lowercase collection name
(e.g. `apps`, `games`). The tag is used for:

- **Resume mode** (`--resume`): matches existing Items by name + tag to avoid
  creating duplicates, then skips Artefacts whose filename already exists
- **Purge mode** (`--purge`): finds all Items with the tag for bulk deletion

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
└── top-level.zip                  ← SKIPPED (not in a subdirectory)
```

```bash
arco bulk-import --archive-dir ~/archive --tag myimport
```

| Item | Artefact label | Source path |
|------|---------------|-------------|
| `Apps` | `Editor/Editor 1.0.zip` | `Apps/Editor/Editor 1.0.zip` |
| `Apps` | `Viewer/Viewer 2.0.zip` | `Apps/Viewer/Viewer 2.0.zip` |
| `Games` | `Chess/Chess 1.5.adf` | `Games/Chess/Chess 1.5.adf` |

Two Items created. `top-level.zip` is skipped because it is not inside a
subdirectory. No Arcology category is assigned (no `--category-map`).

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
| `--tag TAG` | *(required)* | Tag applied to all imported Items |
| `--categories LIST` | *(all)* | Filter by top-level directory, comma-separated |
| `--skip-dirs LIST` | *(none)* | Skip directories by name at any level, comma-separated |
| `--skip-ext LIST` | *(none)* | Skip files by extension (e.g. `.pdf,.txt`) |
| `--platform NAME` | *(none)* | Platform to assign to all Items |
| `--name-prefix PREFIX` | *(none)* | Prefix for Item names (e.g. `--name-prefix Source` gives "Source: Apps") |
| `--parent UUID` | *(none)* | Nest all created Items under an existing parent Item |
| `--category-map K=V,...` | *(none)* | Map directory names to Arcology categories |
| `--flat` | off | Treat archive-dir as one collection (one Item, all files) |
| `--smart-labels` | off | Use smart label heuristic (strip letter groups, detect self-describing filenames) |
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
