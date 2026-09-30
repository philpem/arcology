# Hard Disc Companion 2.0–2.4 (early 2.x) — backup format

This document describes the **early Hard Disc Companion II** backup format,
represented by version 2.06 (ReadMe dated 22 October 1992, RISC Developments).
It is distinct from both the 1.x `!saveset` format
([Hard Disc Companion 1.05](backups_hard_disc_companion_v1.md)) and the later
2.5x format ([Hard Disc Companion II 2.55](backups_hard_disc_companion_v2.md)).

The description is taken from the 2.06 Backup/`!Retrieve` resources and
executables. Byte offsets and lengths are in bytes; multi-byte payload integers
are little-endian.

> **Scope.** The three on-disk generations are:
>
> | Generation | Example | Container | Restore tool | Medium identity |
> |---|---|---|---|---|
> | 1.x | 1.05 | `!saveset` tree | `!Restore` 0.90 | set name |
> | early 2.x | 2.06 | `$.data.Chunk_N` + `$.data.Ident` | `!Retrieve` | per-medium `Ident` file |
> | 2.5x | 2.55 | `$.<set>.Chunk_N` | `!Restore` | volume label + set stem in the catalogue |
>
> The 2.50 release notes (19 May 1994) state that 2.50 cannot restore backups
> made by earlier releases. The change is in the **identity and catalogue**
> structures, not the chunk container or compression: 2.06 and 2.55 use the
> same chunked payloads and the **same 12-bit LZW codec**. An extractor must
> identify the generation before choosing a catalogue parser.

## 1. Path templates (2.06)

These are the 2.06 `MessageTrans` strings. The filing-system/device prefix is
`adfs::<drive>`.

**Floppy device:**

```text
floproot : adfs::%s.$
flopsset : adfs::%s.$.data
flopidnt : adfs::%s.$.data.Ident
flopchnk : adfs::%s.$.data.Chunk_%d
floprstr : adfs::%s.$.!Restore
floprtve : adfs::%s.$.!Retrieve
floplogf : adfs::%s.$.!Retrieve.LogFile
```

**Other (directory) destination:**

```text
othrrstr : %s.!Restore
othrrtve : %s.!Retrieve
othrdata : %s.!Retrieve.data_%d
othrflpe : %s.!Retrieve.data_%d.name_%d
othrchnk : %s.!Retrieve.data_%d.name_%d.Chunk_%d
othrlogf : %s.!Retrieve.LogFile
```

There is no separate removable-media template in 2.06; the floppy and Other
forms cover the supported destinations.

## 2. Differences from 2.5x

| Aspect | early 2.x (2.06) | 2.5x (2.55) |
|---|---|---|
| Set directory | fixed `$.data` | `$.<setname>` (floppy volume name, default `data`) |
| Medium identity | per-medium **`$.data.Ident`** file | medium volume label plus the set stem in the catalogue |
| Restore tool | **`!Retrieve`** | **`!Restore`** |
| Tools copied | both `!Restore` and `!Retrieve` | `!Restore` only |
| Log file | `$.!Retrieve.LogFile` | `$.!Restore.LogFile` |
| Other destination | `!Retrieve.data_N.name_N.Chunk_N` | `<setname>.!Restore.data_N.name_N.Chunk_N` |
| Removable template | none | `remvsset`/`remvchnk`/`devrstr`/`devlogf` |
| Compression setting | single `keenness` value | `perfZERO`/`perfMEDIUM`/`perfHIGH` levels |
| Log/status routines | `write_status`, `invalidatelogfile`, `validatelogfile` | `write_status_to_log`, `write_log_entry`, `read_log_entry`, `claim_log_entry` |

The 2.06 `Ident` file identifies the medium; the 2.5x format replaces it with
the medium's volume label and a set stem stored in the catalogue. This is the
most likely reason 2.50 cannot restore older backups, though the precise
catalogue-entry change is not pinned here.

## 3. Shared with 2.5x

- Payloads are packed into numbered `Chunk_N` files (RISC OS filetype `0xffd`,
  Data) under a `data` directory.
- A binary `LogFile` catalogue records the tree, metadata and first-fragment
  locations.
- Compression is the **same internal 12-bit LZW codec** as 2.5x. The 2.06
  encoder is byte-for-byte the same routine as the 2.55 encoder, and the
  compress-or-raw wrapper is identical. The codec description and the reference
  decoder in [Hard Disc Companion II 2.55](backups_hard_disc_companion_v2.md)
  (Appendix A) apply to 2.06 payloads as well.
- The `keenness` value is the 2.06 equivalent of the 2.5x performance level;
  `keenness:55` matches the 2.5x Medium threshold.

## 4. Not yet established

- The exact 2.06 `LogFile` record and chunk-header layouts. The 2.06 code has
  the same routine family as 2.55 but different log/status routines, so the
  catalogue entry layout must be confirmed against a real 2.06 backup or by
  further analysis before it is treated as interchangeable with 2.55.
- The `Ident` file's byte format and how it is matched to a medium.
- The 2.06 removable/Other media transitions.

An extractor can read 2.06 **chunk payloads and compression** using the 2.55
rules. Catalogue parsing and medium identity should be treated as a separate,
version-specific case until the layouts above are confirmed.
