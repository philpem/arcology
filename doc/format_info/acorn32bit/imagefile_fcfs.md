# FCFS — FileCore Image Filing System

FCFS is a disc image format for Acorn's RISC OS, created by Nick Craig-Wood and Sergio Monesi (1995–97). The FCFS RISC OS module allowed whole FileCore discs (floppy or hard) to be copied to image files far faster than a file-by-file copy, and then accessed read-only through RISC OS's image filing system interface. It was distributed as shareware and required RISC OS 3.10 or later.

FCFS can only image E-format (800K) floppy discs and hard discs. Earlier floppy formats (D and L) use the FileCore old map format, which FCFS does not support.

FCFS image files use RISC OS filetype `FCD` and commonly appear with the leaf name in the form `CF-D1,FCD` when stored on non-RISC OS systems that encode the filetype as a comma-separated suffix.

Three image types were defined, each offering a different trade-off between file size and complexity.

## File Structure

Every FCFS image has the same overall layout:

```
+-----------------------------+
|  Disc data region           |  (from file offset 0)
|  (format depends on type)   |
+-----------------------------+
|  Block offset table         |  (type 2 only)
+-----------------------------+
|  256-byte trailer           |  (always at file_size − 0x100)
+-----------------------------+
```

## Trailer

The trailer is a 256-byte block at the very end of the file. Only the first 16 bytes are defined; the remainder appears to be unused (typically zero-filled).

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| +0x00 | 4 | Magic | `46 43 46 53` — ASCII "FCFS" (little-endian `0x53464346`) |
| +0x04 | 4 | Type | `0` = raw, `1` = compacted, `2` = block-compressed |
| +0x08 | 4 | Map offset | Offset of zone 0 map block within the image's logical data stream (the compacted data for type 1, the decompressed data for type 2, or the raw disc for type 0). On a hard disc this is the offset past the boot block area, e.g. `0x1000` for 512-byte sectors. On a floppy with `nzones=1` this is `0x0000` since zone 0 starts at the beginning. The FCFS module reads the disc record from `map_offset + 4` and the zone map data from `map_offset` when mounting the image. |
| +0x0C | 4 | Offset table size | Type 2: size in bytes of the block offset table that precedes the trailer. Types 0 and 1: the FCFS module ignores this field; observed values are non-zero on some images (e.g. `0x14` on an 800K floppy) but do not appear to affect operation. |

All multi-byte values are little-endian, as is standard for ARM/RISC OS.

## Type 0: Raw

A complete sector-by-sector copy of the disc. File offsets correspond directly to disc addresses. The disc data region runs from offset 0 to `file_size − 0x100`.

Recovery is trivial: strip the 256-byte trailer to obtain a flat sector image.

## Type 1: Compacted

Only allocated sectors are stored. Free space (as identified by the FileCore zone allocation map) is omitted, and the remaining allocated data is packed sequentially in disc address order. This produces significantly smaller images for partially-filled discs.

Recovery requires understanding the FileCore new-format zone map in order to reconstruct which allocation units are present in the file and where they belong on the disc. See the [recovery procedure for type 1](#recovery-procedure-for-type-1) below.

## Type 2: Block-Compressed

The compacted disc data (the same content as a type 1 image) is divided into 64 KB blocks. Each block is independently compressed and stored sequentially from file offset 0. A block offset table immediately before the trailer provides random access to each compressed block.

Decompression of a type 2 image therefore has two stages: first the blocks are decompressed to recover the type 1 compacted data, and then the compacted data is expanded to the full disc using the FileCore allocation map.

### Block Offset Table

Located at `file_size − 0x100 − offset_table_size`. It consists of `(N+1)` little-endian uint32 values, where `N` is the number of 64 KB blocks. The number of blocks is therefore `offset_table_size / 4 − 1`.

| Entry | Value |
|-------|-------|
| `table[0]` | File offset of compressed block 0 (always 0) |
| `table[i]` | File offset of compressed block *i* |
| `table[N]` | File offset of the byte past the last block (i.e. the start of the offset table itself) |

The compressed size of block *i* is `table[i+1] − table[i]`. If this difference is zero, the block contains no stored data and decompresses to 64 KB of zeros.

### Compressed Block Format

Each non-empty compressed block (`table[i+1] − table[i] > 0`) begins with a one-byte flag:

| Flag | Meaning |
|------|---------|
| `0x01` | LZ77-compressed data follows |
| `0xFF` | Stored (uncompressed) data follows; the remaining `table[i+1] − table[i] − 1` bytes are the literal block content |

### LZ77 Compression Format

The compression is an LZ77 variant confirmed by disassembly of the FCFS module (function at offset `0x2B0`). Back-references point directly into the already-written output buffer — there is no ring buffer.

- A **control byte** provides 8 flags (MSB first, i.e. bit 7 is processed first). Each flag selects literal or back-reference:
  - **Bit = 1**: copy one literal byte from the input to the output.
  - **Bit = 0**: read two bytes `b1, b2`. Offset = `b1 | ((b2 & 0x0F) << 8)` (12-bit backward displacement from the current output position). Length = `(b2 >> 4) + 3` (3–18 bytes). Copy *length* bytes from `(output_ptr − offset)` to the current output position.
- Back-references can overlap the region being written (i.e. the source may extend into the destination), which allows run-length encoding of repeated byte patterns. The copy is byte-at-a-time, not block-based.

The module's inner loop is unrolled 8× (one copy of the literal-or-backref logic per control bit). Two code paths exist: a fast path (address `0x2BC`) used when at least 17 bytes of source data remain (no per-bit bounds checking), and a slow path (`0x5C0`) that checks `src < src_end` before each operation.

FCFS provides six compression levels on a scale from "Fast" to "Medium" to "Slow". All levels use the same decompression algorithm; only the compressor's match search varies. Levels 5 and 6 ("Slow" end) tend to produce identical output because the search is already near-exhaustive.

## FileCore Disc Record

All three types require reading the FileCore disc record to determine disc geometry. The disc record can be found in two standard locations depending on disc type (see *RISC OS PRM* vol. 2, ch. 28 "FileCore", "The disc record"):

- **Hard disc**: The FileCore boot block occupies disc address `0xC00` (see PRM ch. 28, "The boot block"). The disc record is at offset `+0x1C0` within the boot block, i.e. disc address `0xDC0`. This address is in bytes, not sectors, so it works regardless of sector size (256, 512, or 1024 bytes).
- **Floppy disc** (`nzones=1`): Zone 0's map block starts at disc address `0x00`. The disc record is at `+0x04` (after the 4-byte zone header), i.e. disc address `0x04`.

FileCore discs do not use an x86-style MBR or partition table.

The disc record structure (relevant fields):

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 1 | `log2_sector_size` — log₂ of the sector size in bytes (typically 9 for 512-byte sectors) |
| +0x01 | 1 | `sectors_per_track` |
| +0x02 | 1 | `heads` — number of disc heads |
| +0x03 | 1 | `density` |
| +0x04 | 1 | `idlen` — length of fragment ID in bits (typically 15) |
| +0x05 | 1 | `log2_bpmb` — log₂ of bytes per map bit (the allocation unit size) |
| +0x06 | 1 | `skew` |
| +0x07 | 1 | `boot_option` |
| +0x08 | 1 | `low_sector` |
| +0x09 | 1 | `nzones` — number of zones in the allocation map |
| +0x0A | 2 | `zone_spare` — cross-zone continuation bits per zone |
| +0x0C | 4 | `root_dir` — disc address of the root directory |
| +0x10 | 4 | `disc_size` — total disc size in bytes (low 32 bits) |

## FileCore Zone Map (New Map Format)

Understanding the zone map is essential for type 1 recovery. This section summarises the new map format used by FileCore on RISC OS 3 and later (see PRM vol. 2, ch. 28 "FileCore", "The map" and "The zone").

### Layout

The disc is divided into `nzones` zones. The zone map occupies `nzones` consecutive sectors starting at the beginning of zone `nzones / 2` (integer division). Each sector is one "map block" and controls one zone.

The disc address of zone Z is:

```
zone_disc_address(0) = 0
zone_disc_address(Z) = zone0_bits × bpmb + (Z − 1) × zoneN_bits × bpmb   (for Z > 0)
```

Where:
- `bpmb = 1 << log2_bpmb` (bytes per map bit / allocation unit size)
- `zone0_bits = (sector_size − 64) × 8` (zone 0 has a 64-byte header including the disc record copy)
- `zoneN_bits = (sector_size − 4) × 8` (other zones have a 4-byte header only)

The total map capacity is `zone0_bits + (nzones − 1) × zoneN_bits` allocation units. The actual number of allocation units on the disc is `min(map_capacity, disc_size / bpmb)`.

### Map Block Structure

Each map block (one per zone) has this layout:

#### Map Block 0 (Zone 0)

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 1 | `ZoneCheck` — checksum byte |
| +0x01 | 2 | `FreeLink` — 15-bit offset to first free fragment (see below) |
| +0x03 | 1 | `CrossCheck` |
| +0x04 | 60 | Copy of the disc record |
| +0x40 | to end of sector | Allocation bit stream |

#### Map Block N (Zones 1+)

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 1 | `ZoneCheck` |
| +0x01 | 2 | `FreeLink` |
| +0x03 | 1 | `CrossCheck` |
| +0x04 | to end of sector | Allocation bit stream |

### Allocation Bit Stream

The allocation area of each map block is a packed bit stream read LSB-first. It encodes a sequence of **fragment blocks**. Each fragment block consists of:

1. An `idlen`-bit **fragment ID** (LSB first)
2. Zero or more `0` bits (padding / length encoding)
3. A terminating `1` bit

The total length of a fragment block in bits equals the number of allocation units occupied by that fragment on disc. One allocation unit = `bpmb` bytes of disc space.

### Zone Spare

The first `zone_spare` bits of each zone's allocation area are **cross-zone continuation bits**. They cannot start a new fragment — they always continue the last fragment from the previous zone (or, for zone 0, they hold the initial system fragment). Crucially, these bits **do** represent real allocation units on disc. They are not mere overhead. Without counting them, the map cannot cover the full disc address space.

### Fragment IDs

| ID | Meaning |
|----|---------|
| 0 | Free space (end of free chain) |
| 1 | Defect list (bad sectors) |
| 2 | System object (boot block + zone map + root directory) |
| ≥ 3 | Allocated file/directory objects |

### Free Space Chain

Free fragments are linked together in a chain. The `FreeLink` field in each map block header gives a 15-bit offset (in bits, measured from bit 8 of the sector, i.e. byte 1) to the first free fragment in that zone. Each free fragment's `idlen`-bit ID field then gives the offset (in bits, from the start of that fragment) to the next free fragment. An ID of 0 terminates the chain.

Any fragment that is reachable from the free chain is free space. Any fragment with ID 0 that is not part of the chain is also free (a standalone gap). All other fragments (IDs 1, 2, or ≥ 3) are allocated.

## Recovery Procedure for Type 1

The following procedure recovers a type 1 (compacted) FCFS image to a flat disc image:

1. **Read the trailer** at `file_size − 0x100`. Verify the magic bytes and confirm type = 1.
2. **Read the disc record** from file offset `0xDC0` (the boot block is allocated and always present at the start of the file, so file offset matches disc address for the initial portion).
3. **Locate the zone map** in the compacted file. The trailer's map\_offset field at +0x08 gives the file offset of map block 0 directly. Validate by checking that the disc record at offset +0x04 within that sector matches the boot block copy. On a floppy with `nzones=1` the map\_offset will be `0x0000` (zone 0 at the start); on a hard disc it will typically be `0x1000` or similar.
4. **Build an allocation bitmap** by walking all `nzones` map blocks:
   - Start with all allocation units marked as allocated.
   - For each zone, walk the fragment bit stream, decoding each fragment block.
   - Follow the free chain (`FreeLink` → fragment IDs) to identify free fragments.
   - Clear the bitmap bits for free fragments, including fragments with ID 0.
   - Remember that zone\_spare bits are real allocation units — do not skip them.
5. **Reconstruct the disc image**: Walk allocation units 0 through `total_alloc_units − 1` in order. For each unit:
   - If **allocated**: read `bpmb` bytes sequentially from the compacted file and write them at disc address `unit × bpmb` in the output.
   - If **free**: write `bpmb` zero bytes (or seek past for a sparse file).
6. **Set the output file size** to `disc_size` from the disc record.

The compacted file data starts at file offset 0 and runs to `file_size − 0x100`. There is no separate header — the first byte of file data is the first byte of the first allocated allocation unit (disc address 0).

## Recovery Procedure for Type 2

1. **Read the trailer** at `file_size − 0x100`. Verify magic and type = 2. Note the `offset_table_size`.
2. **Read the block offset table** at `file_size − 0x100 − offset_table_size`. This is an array of `(offset_table_size / 4)` 32-bit little-endian values.
3. **Determine the disc size**: decompress blocks 0 and 1, then search the decompressed data for a valid FileCore disc record at standard locations (`0xDC0` for a hard disc, `0x04` for a floppy). Note that for type 2 images the disc record cannot be read from raw file bytes — all data is compressed.
4. **Decompress each block** in sequence:
   - Compute compressed size: `offset_table[i+1] − offset_table[i]`.
   - If size = 0: output 64 KB of zeros.
   - If first byte = `0xFF`: the remaining bytes are stored uncompressed.
   - If first byte = `0x01`: LZ77-decompress the remaining bytes.
   - Write the decompressed 64 KB block at disc address `i × 0x10000`.
5. **Truncate the output** to `disc_size` if the last block extends beyond it.

## Tools

**fcfs2raw** is a C tool that handles all three FCFS types. It was developed by reverse-engineering the FCFS RISC OS module binary.

Usage:

```
fcfs2raw [-s] [-v] [-i] input.fcfs [output.raw]
  -s  Create sparse output (seek over free/zero blocks)
  -v  Verbose (summary statistics)
  -vv Very verbose (per-zone and per-fragment detail)
  -i  Info only (analyse and report, don't convert)
```

The output is a flat sector image suitable for use with emulators (Arculator, RPCEmu, ArcEm) or disc image tools such as Disk Image Manager.

## References

- FCFS 1.10 by Nick Craig-Wood & Sergio Monesi — original RISC OS module and documentation
- *RISC OS 3 Programmer's Reference Manual* vol. 2, ch. 28 "FileCore" — disc record, new map format, boot block
- RISC OS Open source — FileCore module source code (Apache 2.0 licence since 2018)
