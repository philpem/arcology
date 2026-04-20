# Flux Decode Pipeline

Documents the `FLUX_DECODE` analysis job: how it converts flux/sector disc images
to a flat raw sector image (RAW_SECTOR / `.img`), and how it selects the correct
Greaseweazle format string.

---

## Pipeline overview

Each format represents a step in the decode chain:

```
SCP  (raw flux — closest to original)
  ↓  hxcfe
HFE  (normalised MFM flux — for use with hardware emulators)
  ↓  hxcfe
IMD  (sector-decoded, per-track metadata)
  ↓  gw convert
RAW_SECTOR / .img  (flat sector image — for file extraction)
```

The pipeline always feeds Greaseweazle (`gw convert`) the **original source
artefact** — not an intermediate conversion.  This is called the
"closest-to-original" rule and avoids introducing extra encoding/decoding
artefacts.

### Source-type behaviour

| Source type | Siblings produced | gw input |
|-------------|-------------------|----------|
| SCP | HFE sibling + IMD sibling (both `skip_analyses=[FLUX_DECODE]`) | source SCP |
| HFE | IMD sibling (`skip_analyses=[FLUX_DECODE]`) | source HFE |
| IMD | none | source IMD |

The `skip_analyses=[FLUX_DECODE]` flag on sibling artefacts prevents them from
re-triggering the same decode job (ping-pong prevention).

---

## Format detection

Before calling `gw convert`, the pipeline reads the IMD sibling (or the source
IMD directly) and probes the track-0 sector data for known filesystem boot
structures.  The result is a `gw_format` string like `acorn.adfs.800` or
`ibm.720`.  If detection fails, `ibm.scan` is used as a generic fallback.

### Why this matters

`ibm.scan` is a permissive generic format that picks up any sector it finds,
including copy-protection sectors that are intentionally placed outside the
normal track layout.  These extra sectors get interleaved into the output image
and corrupt file extraction.  Named formats tell Greaseweazle exactly how many
cylinders, heads, sectors/track, and bytes/sector to expect, so it ignores
everything else.

### Detection results in the analysis record

The `FLUX_DECODE` analysis `details` field records three keys for the format
decision:

| Key | Meaning |
|-----|---------|
| `gw_format_used` | The format string passed to `gw convert` (e.g. `acorn.adfs.800`) |
| `gw_format_source` | How the format was chosen: `"hint"`, `"detected"`, or `"fallback"` |
| `gw_track0` | Raw IMD geometry as parsed (cylinders, heads, sector_size, encoding, sector_ids on track 0) |
| `gw_geometry` | Geometry returned by the filesystem probe (only present when `gw_format_source == "detected"`) |

---

## Why `gw_track0.cylinders` can differ from `gw_geometry.cylinders`

`gw_track0` reflects the raw track count in the IMD file.  `gw_geometry`
reflects the cylinder count calculated from the disc's own boot record.

### hxcfe padding tracks

When hxcfe converts a flux image to IMD it writes the full 80 formatted
cylinders **plus two empty padding cylinders** (80 and 81) at the end.  These
cylinders contain no sectors.  The raw IMD track count therefore reads 82, but
the disc only ever had 80 formatted cylinders.

If the format map were keyed on the raw IMD count (82), no match would be found
and `gw convert` would fall back to `ibm.scan`.

### Solution: disc-record authoritative cylinder count

For ADFS floppy discs (probe D and probe E), the `disc_size` field in the
Filecore disc record gives the total usable disc capacity in bytes.  The
cylinder count is derived as:

```
cylinders = disc_size ÷ (sectors_per_track × sector_size × heads)
```

For an ADFS-D 800 KB disc:

```
cylinders = 819200 ÷ (5 × 1024 × 2) = 80
```

`gw_geometry.cylinders` therefore reflects the physically formatted disc
geometry (80), not the IMD file's inflated track count (82).

---

## Filesystem probes

Five probes are attempted in order; the first match wins.

| Probe | Trigger | Filesystem | Notes |
|-------|---------|------------|-------|
| A | FM encoding | DFS | FM is sufficient — no boot-structure check needed |
| D | MFM, 1024 B/sector, sector 0 present | ADFS floppy new-map (D/E/F) | Disc record at sec0[4:]; cylinders from `disc_size` |
| E | MFM, 1024 B/sector, sector 3 present | ADFS hard-disc new-map (F/F+) | Boot block at disc address 0xC00 (sector 3 for 1024 B sectors) |
| B | MFM, 256 B/sector, Hugo magic in any track-0 sector | ADFS old-map (S/M/L) | SPT hard-coded to 16 (invariant for old-map) |
| C | MFM, any sector size, FAT BPB | FAT12/FAT16/FAT32 | Sectors sorted by ID; BPB fields are authoritative geometry |

### Probe D: ADFS floppy new-map

Sector 0 of track 0 head 0 holds the Filecore zone-0 header.  The disc record
starts at byte 4 of that sector.

Acceptance criteria (in order of decreasing strength):

1. **Zone checksum** — the mod-256 sum of the first 512 bytes (floppy boot
   block style), or the mod-256 sum of the full 1024-byte sector (full-zone
   style), must equal zero.
2. **Disc size alignment** (fallback) — if neither checksum passes (e.g. the
   disc was formatted by a non-standard tool, or the flux→HFE→IMD conversion
   pipeline introduced byte-level differences), the disc record is still
   accepted when `disc_size % sector_size == 0`.

Together with the disc-record field checks (`log2ss ∈ {8,9,10,12}`,
`disc_size > 0`, `spt > 0`, `heads > 0`) the false-positive rate for the
alignment-only fallback is ≈ 0.006 %.

---

## Format map

The full list of recognised geometries and their Greaseweazle format strings:

| Filesystem | Cylinders | Heads | SPT | Bytes/sector | gw format |
|------------|-----------|-------|-----|--------------|-----------|
| DFS | 40 | 1 | 10 | 256 | `acorn.dfs.ss` |
| DFS | 40 | 2 | 10 | 256 | `acorn.dfs.ds` |
| DFS | 80 | 1 | 10 | 256 | `acorn.dfs.ss80` |
| DFS | 80 | 2 | 10 | 256 | `acorn.dfs.ds80` |
| ADFS | 40 | 1 | 16 | 256 | `acorn.adfs.160` |
| ADFS | 80 | 1 | 16 | 256 | `acorn.adfs.320` |
| ADFS | 80 | 2 | 16 | 256 | `acorn.adfs.640` |
| ADFS | 80 | 2 | 5 | 1024 | `acorn.adfs.800` |
| ADFS | 80 | 2 | 10 | 1024 | `acorn.adfs.1600` |
| FAT | 40 | 1 | 8 | 512 | `ibm.160` |
| FAT | 40 | 1 | 9 | 512 | `ibm.180` |
| FAT | 40 | 2 | 8 | 512 | `ibm.320` |
| FAT | 40 | 2 | 9 | 512 | `ibm.360` |
| FAT | 80 | 2 | 9 | 512 | `ibm.720` |
| FAT | 80 | 2 | 15 | 512 | `ibm.1200` |
| FAT | 80 | 2 | 18 | 512 | `ibm.1440` |
| FAT | 80 | 2 | 21 | 512 | `ibm.1680` |
| FAT | 80 | 2 | 36 | 512 | `ibm.2880` |

If the detected geometry does not match any row, `ibm.scan` is used.

---

## Key source files

| File | Role |
|------|------|
| `worker/arcworker/tools/imd.py` | IMD parser (`parse_imd_track0`) and filesystem probes (`detect_geometry_from_boot_data`) |
| `worker/arcworker/tools/flux.py` | `_GW_FORMAT_MAP`, `_geometry_to_gw_format()`, `sector_image_to_raw_greaseweazle()` |
| `worker/arcworker/analysis.py` | `process_flux_decode` — orchestrates the full pipeline |
| `myapp/blueprints/artefacts.py` | `ANALYSIS_MAP` — `FLUX_DECODE` entry for SCP, HFE, and IMD artefact types |
| `ci/test_imd_geometry.py` | Unit tests for the IMD parser and all five filesystem probes |
| `ci/test_flux_decode.py` | Unit tests for the three-source-type pipeline |

# vim: ts=4 sw=4 et
