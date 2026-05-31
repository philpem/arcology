# Analysis Pipeline

When you upload an artefact, Arcology automatically queues a set of analysis jobs suited to the file type.  A worker process picks them up, runs the appropriate tools, and stores the results.  You do not need to trigger analyses manually in normal use — the system handles the sequencing.

---

## The Derivation Chain

Many artefact types produce *derived artefacts* as a side effect of analysis.  For example, decoding a SuperCard Pro flux image produces an HFE disc image.  Extracting an HFE image produces a raw sector dump.  Extracting the sector dump produces the files that were stored on the original disc.

Each derived artefact is tracked as a child of its parent, and analysis results form a complete provenance chain from the original physical image to individual files.

A typical chain for a BBC Micro disc image looks like this:

```
SCP flux image
  └─ HFE image  (from FLUX_DECODE)
       └─ Sector dump  (from FLUX_DECODE)
            └─ Files extracted from disc  (from FILE_EXTRACTION)
                 └─ Any nested archives extracted  (from ARCHIVE_EXTRACT)
```

The artefact sidebar on the detail page shows the full derivation tree.

---

## The SCP Density-Detection Step

> **Why doesn't flux analysis appear in the queue immediately after uploading an SCP?**

For SuperCard Pro images, a **track density detection** job runs before flux analysis begins.  This job checks whether a 40-track disc was imaged in an 80-track drive (a common situation where even tracks contain data but odd tracks are empty).  If it detects this, it records the corrected geometry before flux decode runs.

This means:

1. You upload an SCP file.
2. **DETECT\_TRACK\_DENSITY** appears in the queue first.
3. Once it completes, **FLUX\_VISUALISATION** and **FLUX\_DECODE** are queued.
4. Flux decode runs, producing an HFE image.
5. Further analyses continue from there.

If you upload an SCP and the queue looks empty, wait for track detection to finish — flux analysis will follow automatically.

---

## Analysis Types

### Flux images (SCP, DFI, HFE, A2R)

| Analysis | What it does |
|----------|-------------|
| **DETECT\_TRACK\_DENSITY** | Detects 40-track-in-80-track geometry; queues downstream analyses with the correct layout |
| **FLUX\_VISUALISATION** | Generates a graphical track-by-track visualisation of the flux data using Fluxfox |
| **FLUX\_DECODE** | Decodes flux data to a sector-level image; produces an HFE and/or raw sector dump as derived artefacts |
| **METADATA\_EXTRACT** | Reads disc metadata embedded in the flux image (disc title, format, geometry) |

### Disc and sector images (HFE, RAW\_SECTOR, ISO)

| Analysis | What it does |
|----------|-------------|
| **PARTITION\_DETECT** | Identifies partitions and filesystems on the disc |
| **FILE\_EXTRACTION** | Extracts all files from the disc image using DiscImageManager; registers the files for search |
| **CHECKSUM\_COMPUTE** | Computes MD5, SHA-1, and SHA-256 hashes for every extracted file |
| **FORMAT\_IDENTIFY** | Identifies the format of each extracted file using magic-byte detection |
| **DISC\_PROTECTION\_DETECT** | Detects copy protection indicators: weak bits, CRC errors, ID mismatches, deleted data address marks |
| **DISC\_MASTERING\_DETECT** | Detects disc mastering tool fingerprints (Traceback, Formaster, duplicator patterns) |
| **ARMLOCK\_REMOVE** | Detects and removes ARMlock RISC OS disc security, producing a clean derived image |

### Archives (ZIP, ARC, TAR, etc.)

| Analysis | What it does |
|----------|-------------|
| **ARCHIVE\_DETECT** | Identifies the archive format |
| **ARCHIVE\_EXTRACT** | Extracts files from the archive; extracted files are registered for search |

### File-level (all extracted files)

| Analysis | What it does |
|----------|-------------|
| **PRODUCT\_RECOGNITION** | Compares extracted file hashes against known-file databases; identifies which products are present |
| **FORMAT\_CONVERT** | Converts RISC OS Sprite and Draw files to PNG/SVG; converts Acorn text files to UTF-8.  Enables in-browser viewing. |
| **RISCOS\_MODULE\_PARSE** | Parses RISC OS module files to extract title, version, date, SWI table, and `*command` list.  All fields are indexed for search. |

---

## Copy Protection Indicators

**DISC\_PROTECTION\_DETECT** records the following indicator types, which can be searched with `protection:`:

| Value | Meaning |
|-------|---------|
| `bad_crc` | Sector with a CRC that does not match its data (deliberate bad sector) |
| `weak_bits` | Flux-level weak bits that read inconsistently (magnetisation intentionally degraded) |
| `id_mismatch` | Sector ID header does not match the data record ID |
| `ddam` | Deleted Data Address Mark — sector flagged as deleted at the hardware level |
| `no_flux` | Track with no flux transitions at all |
| `short_track` | Track shorter than the nominal length for this format |
| `long_track` | Track longer than the nominal length for this format |

---

## Mastering Fingerprints

**DISC\_MASTERING\_DETECT** records the following fingerprint types, searchable with `mastering:`:

| Value | Meaning |
|-------|---------|
| `traceback` | Traceback duplicator fingerprint |
| `formaster` | Formaster duplicator fingerprint |
| `duplicator_fingerprint` | Generic duplicator pattern not attributable to a specific tool |

---

## When Analysis Fails

If an analysis job fails, the artefact detail page will show a **Failed** section listing the affected analysis types.  Click the analysis link for the full error output from the tool.

Common causes:

- **Wrong artefact type** — if the type was mis-detected, use the **Edit** button to correct it, then click **Re-analyse**.
- **Bad flux image** — some SCP files are too damaged or incomplete for flux decode.  The error output from Fluxfox or HxCFE will describe the problem.
- **Unsupported filesystem** — FILE\_EXTRACTION supports a specific set of filesystem types.  If the disc uses an unusual format, extraction may fail or produce partial results.
- **Worker not running** — if analyses stay in PENDING state indefinitely, check that the worker process is running.

To re-run analyses, open the artefact and click **Re-analyse**.  You can choose to re-run all analyses or only failed ones.

---

## The Analysis Queue

**Analysis** → **Queue** shows analyses currently pending or running.  The **Analysis** list view shows all historical analyses with filter buttons for status (Pending, Running, Completed, Failed).

If a job gets stuck in RUNNING state (the worker crashed mid-job), it will automatically be reset to PENDING after the stale job timeout (default: 1 hour).
