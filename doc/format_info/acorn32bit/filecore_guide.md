# The FileCore Filing System: A Technical Guide

*A practical reference for reading, writing, and repairing FileCore disc images.*

FileCore is the disc filing system at the heart of Acorn's RISC OS. Rather than being a monolithic driver, FileCore is a layered module: it implements the filesystem logic (directories, allocation maps, caching) while delegating hardware access to child modules like ADFS (floppy and IDE), SCSIFS, and SDFS. FileSwitch sits above FileCore and provides the high-level API. This architecture means that any disc accessed through any of these child modules uses the same on-disc format.

This guide covers every FileCore disc format from the earliest L-format floppies through to the modern G-format hard disc. It follows a logical progression: identify what you're looking at, find the key structures, read files, understand how writes work, and detect and repair corruption.

> **Primary references**: *RISC OS 3 Programmer's Reference Manual* (PRM) vol. 2, ch. 28 "FileCore"; PRM vol. 5a, ch. 110 "FileCore" (RISC OS 3.6 extensions). The RISC OS Open source code is at [`RiscOS/Sources/FileSys/FileCore/`](https://gitlab.riscosopen.org/RiscOS/Sources/FileSys/FileCore) — file and line references point there unless stated otherwise.

---

## 1. Identifying the Format

A FileCore disc can use one of two **map types** (old or new) and one of three **directory types** (old, new, or big). The combination determines the format letter. Every tool that works with FileCore images must begin by identifying which combination it is dealing with.

### 1.1 The Format Matrix

| Format | Map | Directories | Sector size | Geometry (floppy) | Max entries/dir | Filename length | Introduced |
|--------|-----|-------------|-------------|-------------------|-----------------|-----------------|------------|
| **S** | Old | Old | 256 | 1 side, 40 trk, 16 sec | 47 | 10 chars | BBC Master / Electron Plus 3 |
| **M** | Old | Old | 256 | 1 side, 80 trk, 16 sec | 47 | 10 chars | BBC Master / Electron Plus 3 |
| **L** | Old | Old | 256 | 2 sides, 80 trk, 16 sec | 47 | 10 chars | BBC Master / Electron Plus 3 |
| **D** | Old | New | 1024 | 2 sides, 80 trk, 5 sec | 77 | 10 chars | Arthur / RISC OS 2 |
| **E** | New | New | 1024 (floppy) or 512 (HD) | 2 sides, 80 trk, 5 sec | 77 | 10 chars | RISC OS 3.10 |
| **F** | New | New | 512 (HD) or 1024 (floppy) | 2 sides, 80 trk, 10 sec | 77 | 10 chars | RISC OS 3.6 (big discs) |
| **E+** | New | Big | 1024 (floppy) or 512 (HD) | as E | ~32,000+ | 255 chars | RISC OS 4 |
| **F+** | New | Big | 512 | as F | ~32,000+ | 255 chars | RISC OS 4 |
| **G** | New | Big | 2048 or 4096 | — (HD only) | ~32,000+ | 255 chars | RISC OS 5 |

**S, M, and L** use an identical filesystem structure — the old map and old directory formats described in this document. They differ only in physical geometry: S is a 160 KB single-sided 40-track floppy, M is a 320 KB single-sided 80-track floppy, and L is a 640 KB double-sided 80-track floppy. Hard discs formatted with old-map ADFS also use the same structure with 256-byte sectors and geometry determined by the drive. (See [mdfs.net ADFS structure](https://mdfs.net/Docs/Comp/Disk/Format/ADFS) for a thorough treatment of the 8-bit formats.)

**E and F** (and their + variants) share the same on-disc structures — new map, new directories, and boot block. The format letter reflects the disc record parameters (sector size, sectors per track, number of zones) rather than any structural difference. F format was introduced in RISC OS 3.6 for 1.6 MB floppies with 10 sectors per track and 4 zones; E format uses 5 sectors per track and typically 1 zone. Hard discs use the same structures regardless of whether they are labelled E or F — the distinction is just the default parameter choices at format time. Similarly, E+ and F+ differ only in parameters; both use big directories and the new map. (Source: `Doc/Formats` in the FileCore source tree.)

**Track ordering** differs between old-map and new-map floppies. On S/M/L discs, tracks are **sequential**: all tracks on side 0 come first, then all tracks on side 1 (for L). The logical sector formula is `sector + track × 16 + side × (tracks × 16)`. On D/E/F discs, tracks are **interleaved**: track 0 side 0 is followed by track 0 side 1, then track 1 side 0, etc. The formula is `sector + (track × 2 + side) × sectors_per_track`.

### 1.2 How to Identify the Map Type

Read the disc record (see §2). The `nzones` field distinguishes the two map types by implication — but the most reliable test is to check for the **old map signature**:

- **Old map (S, M, L, D)**: A flat free-space table occupying two 256-byte sectors at disc addresses `0x000` and `0x100`. Not a bit stream. See §2.5 for the full layout.

- **New map (E, F, E+, F+, G)**: The disc record has `nzones >= 1` and `idlen >= 1`. The allocation data is a packed bit stream in zone sectors. There is no flat free-space table.

A pragmatic identification approach for a disc image of unknown format (from [mdfs.net](https://mdfs.net/Docs/Comp/Disk/Format/ADFS)):

1. Read at offset `0x200` (512 bytes from the start). If bytes 1–4 are `"Hugo"` or `"Nick"`, this is the root directory of a 256-byte-sector old-map disc (S, M, or L).

2. If no match, read at offset `0x400` (1024 bytes). If bytes 1–4 are `"Hugo"` or `"Nick"`, this is a 1024-byte-sector disc (D, E, or F). Step 4 distinguishes old from new map.

3. On a hard disc, read the boot block at disc address `0xC00`. The disc record is at offset `+0x1C0` within it. If `idlen` (disc record offset `+0x04`) is non-zero, the disc uses a new map.

4. On a floppy with 1024-byte sectors, distinguish old from new map by examining disc address `0x000`:
   - If a valid disc record is present at offset `+0x04` (non-zero `idlen`), this is a new-map disc (E or F) — the zone 0 map block starts at `0x000`.
   - Otherwise the disc is old-map D format, where the free space map is at `0x000`–`0x1FF`.

### 1.3 How to Identify the Directory Type

Having identified the map type, determine the directory format by examining the root directory's header and tail bytes:

- **Old directories** (S, M, L): Exactly `0x500` bytes (5 × 256-byte sectors), up to 47 entries. The header and tail contain the validation string `"Hugo"` — 8-bit ADFS does not support `"Nick"`. Attributes are encoded in name bytes rather than a separate field (see §3.3). The check byte at the end of the directory is always zero on 8-bit ADFS; 32-bit ADFS computes it (see §A.2).

- **New directories on old-map media** (D): Same entry structure as old directories but `0x800` bytes (2048 bytes = 2 × 1024-byte sectors) and up to 77 entries. Attributes are stored in a separate byte (offset `+0x19` of the entry) rather than in the name bits. The validation string may be `"Hugo"` or `"Nick"`.

- **New directories** (E, F): Similar structure to old directories but with a different field layout in the tail. The validation string is `"Nick"`. New directories are one sector in size (typically 1024 or 2048 bytes on floppies, or a size related to the LFAU on hard discs). They hold up to 77 entries. (PRM vol. 2, ch. 28, "Directories".)

- **Big directories** (E+, F+, G): Identified by the 4-byte signature `"SBPr"` at offset `+0x04` of the directory header and `"oven"` in the tail. Big directories are variable-length (always a multiple of 2048 bytes), can grow dynamically up to 4 MB, and support filenames up to 255 characters. The `format_version` field (disc record offset `+0x2C`) equals 1 when the disc uses big directories. The source (`s/BigDirCode`, `TestBigDir`) detects big directories by testing `DiscRecord_BigDir_DiscVersion == 1`.

### 1.4 Quick Identification Pseudocode

```
read boot_block at 0xC00 (hard disc) or disc_record at 0x04 (floppy)
if disc_record.idlen == 0:
    format = old map (L or D)
    dir_type = old
else:
    format = new map
    if disc_record.format_version == 1:   # offset 0x2C
        dir_type = big                    # E+, F+, or G
    else:
        dir_type = new                    # E or F
    if disc_record.disc_size > 512MB or (disc_record.big_flag & 1):
        letter = F (or F+/G)
    else:
        letter = E (or E+)
```

---

## 2. Finding the Key Structures

### 2.1 The Disc Record

The disc record is the single most important structure on a FileCore disc. Every other structure's location and interpretation depends on values in the disc record. (PRM vol. 2, ch. 28, "The disc record".)

**Location:**

- **Hard disc**: Disc address `0xC00` holds the boot block (two sectors). The disc record is at offset `+0x1C0` within the boot block, i.e. disc address `0xDC0`. Note: `0xC00` is a byte address, not a sector number, so this works regardless of whether sectors are 256, 512, or 1024 bytes.

- **Floppy (new map)**: Zone 0's map block starts at disc address `0x000`. The disc record is at `+0x04` within it (after the 4-byte zone header). This is disc address `0x04`.

- **Floppy (old map)**: The disc record concept doesn't exist as a single structure. Geometry is implicit in the format. S/M/L floppies have 256-byte sectors, 16 sectors per track. D floppies have 1024-byte sectors, 5 sectors per track, 2 heads, 80 tracks.

**The disc record structure (20 bytes minimum, extended to 64 bytes from RISC OS 3.6):**

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| +0x00 | 1 | `log2_sector_size` | Log₂ of sector size in bytes (8 = 256, 9 = 512, 10 = 1024). |
| +0x01 | 1 | `sectors_per_track` | Sectors per track (physical geometry). |
| +0x02 | 1 | `heads` | Number of disc surfaces. |
| +0x03 | 1 | `density` | Encoding density (0 = hard disc, 1 = single, 2 = double, 3 = double+, 4 = quad, 8 = octal). |
| +0x04 | 1 | `idlen` | Fragment ID width in bits. 0 for old map. Max 15 (new map), 19 (big map, Ursula), or 21 (RISC OS 5). |
| +0x05 | 1 | `log2_bpmb` | Log₂ of bytes per map bit (allocation unit size). |
| +0x06 | 1 | `skew` | Track-to-track sector skew for head positioning. |
| +0x07 | 1 | `boot_option` | Boot action (0 = none, 1 = load, 2 = run, 3 = exec). |
| +0x08 | 1 | `low_sector` | Lowest sector ID on a track, plus flags in bits 6–7. |
| +0x09 | 1 | `nzones` | Number of zones in the allocation map (low byte). |
| +0x0A | 2 | `zone_spare` | Cross-zone continuation bits per zone boundary. |
| +0x0C | 4 | `root_dir` | Disc address of the root directory (see §2.3). |
| +0x10 | 4 | `disc_size` | Total disc size in bytes (low 32 bits). |

**Extended fields (RISC OS 3.6+, at offsets 0x14–0x3F):**

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| +0x14 | 2 | `disc_id` | Cycle ID, incremented on each write to disc structure. |
| +0x16 | 10 | `disc_name` | Padded disc name. |
| +0x20 | 4 | `disc_type` | Filing system number. |
| +0x24 | 4 | `disc_size_2` | High 32 bits of disc size (for discs > 4 GB). |
| +0x28 | 1 | `share_size` | Log₂ of sharing granularity in sectors. |
| +0x29 | 1 | `big_flag` | Bit 0: set if RISC OS partition >512 MB (`DiscRecord_BigMap_BigFlag`). Bits 1–7: reserved, must be 0. |
| +0x2A | 1 | `nzones_hi` | High byte of nzones (total nzones = `nzones | (nzones_hi << 8)`). |
| +0x2B | 1 | | Reserved, must be 0. |
| +0x2C | 4 | `format_version` | Disc format version. 0 = old/new directories, 1 = big directories. |
| +0x30 | 4 | `root_size` | Size of root directory in bytes (big directories). |
| +0x34 | 1 | `flags` | Bit 0 = disc needs checking. |

### 2.2 The Boot Block (Hard Discs Only)

On hard discs, the boot block occupies disc address `0xC00` to `0xDFF` (two 256-byte sectors, or one 512-byte sector). Its layout is:

| Offset from 0xC00 | Size | Content |
|--------------------|------|---------|
| +0x000 | 0x1C0 | Defect list (terminated by `0x200000xx`) |
| +0x1C0 | 0x040 | Disc record (64 bytes) |
| +0x1FC | 1 | Boot block flag byte |
| +0x1FD | 1 | Reserved |
| +0x1FE | 2 | Checksum |

The defect list contains 32-bit disc addresses of bad sectors. Each entry gives the byte address of a defective sector. The list is terminated by a word of the form `0x200000xx` where `xx` is a checksum byte. For discs larger than 512 MB, a second defect list is appended, using sector addresses and terminated by `0x400000yy`. (PRM vol. 2, ch. 28, "The boot block"; PRM vol. 5a, ch. 110, "Defect lists".)

FileCore discs do not use x86-style MBR partition tables.

### 2.3 The Root Directory

The root directory's location depends on the map and sector type:

- **Old map, 256-byte sectors (S, M, L, and old-map hard discs)**: The root directory is at a fixed disc address: `0x200` (sector 2). It occupies 5 sectors (`0x500` bytes) through to `0x6FF`. The parent of the root directory points back to itself.

- **Old map, 1024-byte sectors (D)**: The root directory is at disc address `0x400` (logical sector 1 in 1024-byte terms). It occupies 2 sectors (`0x800` bytes).

- **New map (E, F, E+, F+, G)**: The root directory's disc address is stored in the disc record's `root_dir` field (offset `+0x0C`). For standard E/F format discs, this is an **indirect disc address** (internal address) of the form `0x0002xx`, where `02` is the fragment ID of the system object (boot block + zone map + root directory) and `xx` is the sharing offset within that object. The root directory is stored immediately after the zone map sectors within fragment ID 2.

  The root directory becomes a **separate disc object** (its own fragment ID, no longer sharing ID 2) in two independent situations:

  - **Big directories** (`format_version` = 1): Variable-length directories need to be able to grow, which requires the root directory to have its own fragment that can be extended independently of the map. (Acorn FileCore Phase 2 Functional Specification, §3.13.)
  - **Big maps** (`idlen` > 15, `nzones` > 127): The old `0x0002xx` SIN format cannot represent a sharing offset large enough to reach past a map of more than 127 zones. (Acorn FileCore Phase 1 Functional Specification, §3.4.)

  In either case, the `root_dir` field contains a full SIN with a fragment ID ≥ 3 rather than the special form `0x0002xx`. Formatting software places the root directory as the first object after the map in the same zone, using the first available fragment ID for that zone.

To resolve the indirect address to a physical disc address, you must walk the zone map to find all fragments with ID 2, concatenate them in order, then index into the result at the sharing offset. See §3.2 for the full procedure.

### 2.4 The Zone Map (New Map Only)

The zone map is the allocation structure for new-map discs. It consists of `nzones` sectors, one per zone, stored consecutively starting at the disc address of zone `nzones / 2` (integer division). This places the map near the middle of the disc on multi-zone discs, reducing average seek distance. On a single-zone floppy, the map is at disc address `0x000`.

The map is **double-copied**: a second copy of all `nzones` sectors follows immediately after the first. This allows recovery if one copy is damaged. The total map area on disc is therefore `2 × nzones` sectors. (Nick Reeves, "New Disc File Structure for RISC OS": "A double copied map".)

Each zone's map block is one sector. Zone 0's block has a 4-byte zone header followed by a 60-byte copy of the disc record (64 bytes total before the allocation bits begin). All other zones have only the 4-byte zone header. The remainder of each sector is the allocation bit stream.

**Zone header (4 bytes, all zones):**

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 1 | `ZoneCheck` — checksum byte |
| +0x01 | 2 | `FreeLink` — 15-bit offset to first free fragment (bit offset from byte 1), top bit always set |
| +0x03 | 1 | `CrossCheck` — XOR byte; all zones' CrossCheck bytes should XOR to `0xFF` |

See §3.1 for how to decode the bit stream.

### 2.5 The Old Free Space Map

Old-map discs (S, M, L, D) have a simple free space table rather than a bit stream. The map occupies two 256-byte sectors at disc addresses `0x000` and `0x100`.

**Sector 0 (free space start addresses):**

| Offset | Size | Content |
|--------|------|---------|
| +0x00 | 82 × 3 | Start sector of each free extent (3 bytes each, in units of 256 bytes) |
| +0xF6 | 3 | Level 3 fileserver partition sector, or zero |
| +0xF9 | 3 | Zero, or odd characters of RISC OS disc name (interleaved with sector 1) |
| +0xFC | 3 | Total number of sectors on disc |
| +0xFF | 1 | Checksum of sector 0 |

**Sector 1 (free space lengths):**

| Offset | Size | Content |
|--------|------|---------|
| +0x00 | 82 × 3 | Length of each free extent (3 bytes each, in units of 256 bytes) |
| +0xF6 | 3 | Level 3 fileserver partition sector, or zero |
| +0xF9 | 2 | Zero, or even characters of RISC OS disc name |
| +0xFB | 2 | Disc identifier (random 16-bit value set at format time) |
| +0xFD | 1 | Boot option (set by `*OPT 4`) |
| +0xFE | 1 | Pointer to end of free space list: `3 × (number of free extents)` |
| +0xFF | 1 | Checksum of sector 1 |

Files on old-map discs must be stored contiguously. If there is no single free extent large enough, RISC OS reports "Compaction required" or "Can't extend". The `*Compact` command defragments the disc.

**Old map checksum:** Each 256-byte map sector has its own checksum at offset `+0xFF`. The algorithm starts with 255, then adds bytes 254 down to 0 (in descending address order), propagating carry after each addition. From [mdfs.net](https://mdfs.net/Docs/Comp/Disk/Format/ADFS):

```basic
DEF FNadfs_sum(mem%)
LOCAL sum% : sum% = 255
FOR A% = 254 TO 0 STEP -1
  IF sum% > 255 THEN sum% = (sum% + 1) AND 255
  sum% = sum% + mem%?A%
NEXT
= sum% AND 255
```

Note that this differs from the new-map zone checksum (§A.1) and the directory check byte (§A.2) — each structure has its own algorithm. A "Bad map" error is generated if either checksum is wrong, or if any free extent entry has bits 29–31 set in its start or length fields.

---

## 3. Reading the Filesystem

### 3.1 Decoding the New Map Bit Stream

The allocation area of each zone is a packed bit stream, read LSB-first. It encodes a sequence of **fragment descriptors**. Each descriptor consists of:

1. An `idlen`-bit **fragment ID** (LSB first)
2. Zero or more `0` bits (padding)
3. A terminating `1` bit

The total width of the descriptor in bits equals the number of allocation units that fragment occupies on disc. One allocation unit = `2^log2_bpmb` bytes. (PRM vol. 2, ch. 28, "The map" and "The zone".)

**Fragment IDs:**

| ID | Meaning |
|----|---------|
| 0 | Free space (end of free chain), or standalone gap if not on the free chain |
| 1 | Defect list (bad sectors) |
| 2 | System object (boot block + zone map + root directory) |
| ≥ 3 | Allocated file or directory object |

**The free chain:** Each zone has an independent free chain. `FreeLink` in the zone header gives the bit offset (from bit 8 of the sector, i.e. byte 1) to the first free fragment. That fragment's `idlen`-bit ID field then gives the bit offset (from the start of that fragment) to the next free fragment. An ID of 0 terminates the chain.

When `idlen` > 15 (big map discs), the free chain link within each free fragment is still treated as a 15-bit value, even though the ID field is wider than 15 bits. Free fragments are never shorter than `idlen` + 1 bits. (Acorn FileCore Phase 1 Functional Specification, §3.2.) Note: the Phase 1 spec assumed a maximum sector size of 1024 bytes (8192 bits per zone), for which 13 bits suffices for any intra-zone offset. RISC OS 5 supports 2048 and 4096 byte sectors (G format); the handling of free chain links with these larger zones has not been verified against the source.

**Zone spare bits:** The first `zone_spare` bits of each zone's allocation area are cross-zone continuation bits. They cannot start a new fragment — they always continue the last fragment of the previous zone. These bits do represent real allocation units and must be counted when computing disc addresses.

**Pseudocode to decode a zone:**

```
bit_pos = zone_header_size * 8    # 64*8 for zone 0, 4*8 for others
zone_end = sector_size * 8
alloc_unit = 0                    # within this zone

# Skip zone_spare continuation bits (these belong to the
# previous zone's last fragment, not a new fragment here).
if zone > 0:
    bit_pos += zone_spare
    alloc_unit += zone_spare

while bit_pos < zone_end:
    # read fragment ID
    id = read_bits(map_data, bit_pos, idlen)
    frag_start = alloc_unit
    bit_pos += idlen
    alloc_unit += idlen

    # count the zero bits + terminating 1
    while bit_pos < zone_end:
        b = read_bit(map_data, bit_pos)
        bit_pos += 1
        alloc_unit += 1
        if b == 1:
            break

    frag_len = alloc_unit - frag_start
    record_fragment(zone, id, frag_start, frag_len)
```

### 3.2 Resolving an Indirect Disc Address

On new-map discs, files and directories are identified by a **System Internal Number (SIN)**, also called an indirect disc address. This is a 3-byte (24-bit) value:

- Bits 8–23: Fragment ID (the `idlen`-bit object identifier)
- Bits 0–7: Sharing offset (1–255, representing offsets 0–254 in sharing units; 0 means the object has its own fragment and is not shared)

The sharing unit is one sector (or `2^share_size` sectors on RISC OS 3.6+ discs).

**To resolve a SIN to a physical disc address:**

1. Extract the fragment ID and sharing offset from the SIN.
2. Walk all zones, collecting every fragment with the matching ID, in zone order.
3. Concatenate the fragments to form the disc object's physical extent(s).
4. If the sharing offset is non-zero, the object starts at byte `(sharing_offset - 1) * sharing_unit` within the disc object.

**To find the zone containing a fragment's first occurrence**, the PRM gives a hint: `start_zone = fragment_id / ids_per_zone`, where `ids_per_zone = zone_bits / (idlen + 1)`. The fragment may have additional pieces in subsequent zones.

### 3.3 Directory Structure (Old and New Directories)

Old and new directories share a common structure with minor layout differences. Both have:

- A **header** at the start: 1-byte master sequence number + 4-byte start name (`"Hugo"` or `"Nick"`).
- A **body** of fixed-size 26-byte directory entries, sorted alphabetically by name.
- A **tail** at the end: matching end name, title, parent disc address, and a check byte.

**Directory entry (26 bytes):**

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 10 | Object name (NUL- or CR-terminated if shorter than 10 characters) |
| +0x0A | 4 | Load address |
| +0x0E | 4 | Execution address |
| +0x12 | 4 | Length in bytes |
| +0x16 | 3 | Indirect disc address (SIN) for new map, or start sector for old map |
| +0x19 | 1 | On large-sector directories (D and later): attributes byte. On small-sector old directories (S/M/L): per-entry sequence number. |

The entries are terminated by a NUL byte in the name field of the next (empty) slot.

**Attributes:** On large-sector directories (D, E, F, E+, F+, G), the byte at offset `+0x19` stores attributes. The on-disc representation matches FileCore's internal format:

- Bit 0: owner read (`ReadBit`)
- Bit 1: owner write (`WriteBit`)
- Bit 2: locked (`IntLockedBit`)
- Bit 3: directory (`DirBit`)
- Bit 4: public read (`PublicReadBit`; also treated as a second owner-read bit for compatibility with 6502 ADFS "E" files)
- Bit 5: public write (`PublicWriteBit`)
- Bits 6–7: reserved per the Acorn specification

There are no execute bits. The FileSwitch external API returns the locked bit in bit 3 and does not expose the directory bit; FileCore converts between these representations internally. (Source: `s/Defns`, lines 300–318.)

Although the specification says bits 6–7 must be zero, RISC OS 5 FileCore includes them in its attribute masks (`IntAttMask` = `0xFF`) and silently preserves them on read and write. They are only stripped when writing to old-format (S/M/L) directories. Some systems are known to set these bits; the reasons are unknown. Tools reading disc images should not reject entries with these bits set, but should write them clear when creating new entries.

On small-sector old directories (S, M, L), there is no separate attributes byte. Instead, attributes are encoded in **bit 7 of each character** of the first five bytes of the 10-byte name field:

- Byte 0 bit 7: owner read (R)
- Byte 1 bit 7: owner write (W)
- Byte 2 bit 7: locked (L)
- Byte 3 bit 7: directory (D)
- Byte 4 bit 7: execute-only/private (E)
- Bytes 5–9: pure name characters, no attribute significance

The actual character is in bits 0–6, making filenames effectively 7-bit ASCII on S/M/L discs. The attribute table in the ADFS 1.30 ROM is the literal string `"RWLDE"`, indexed by byte position. (Verified against the [ADFS 1.30 disassembly](https://acornaeology.uk/acorn-adfs/1.30.html): `set_rwl_attribute_bit` at &99C9, `print_entry_name_and_access` at &92DE.)

**Old vs new directory tail differences:**

There are three tail layouts depending on directory type. All are read "backwards" from the end of the directory:

In **small-sector old directories** (S, M, L — `0x500` bytes total), the tail starts at offset `0x4CB`:

- `0x00` end marker (1 byte)
- Directory name (10 bytes)
- Parent start sector (3 bytes)
- Directory title (19 bytes)
- Reserved (14 bytes, zero)
- End sequence number (1 byte, BCD)
- End validation `"Hugo"` (4 bytes)
- Check byte (1 byte — always zero on 8-bit ADFS, computed by 32-bit ADFS)

In **large-sector old directories** (D — `0x800` bytes total), the tail starts at offset `0x7D7`:

- `0x00` end marker (1 byte)
- Reserved (2 bytes, zero)
- Parent start sector (3 bytes)
- Directory title (19 bytes)
- Directory name (10 bytes)
- End sequence number (1 byte)
- End validation `"Hugo"` or `"Nick"` (4 bytes)
- Check byte (1 byte)

In **new directories** (E, F — `0x800` bytes or LFAU-dependent), the tail has the same fields as the large-sector old directory but with different ordering and addressing:

- `0x00` end marker (1 byte)
- Reserved (2 bytes, zero)
- Parent indirect disc address / SIN (3 bytes, not a raw sector address)
- Directory name (10 bytes)
- Directory title (19 bytes)
- End sequence number (1 byte)
- End validation `"Nick"` (4 bytes)
- Check byte (1 byte)

(Source: Nick Reeves' E Format Design Document.)

A directory is reported as **"Broken"** if the master sequence number and validation string at the start (bytes `0x000`–`0x004`) do not match those at the end (`0x4FA`–`0x4FE` for small directories, `0x7FA`–`0x7FE` for large/new directories).

**Load/execution address encoding:** If the top 12 bits of the load address are all set (`0xFFFxxxxx`), the file is date-stamped: bits 19–8 of the load address are the 12-bit filetype, and the remaining bits of load address and the execution address together form a 40-bit centisecond timestamp (epoch: 00:00:00 1 January 1900).

### 3.4 Big Directory Structure (E+, F+, G)

Big directories were introduced in RISC OS 4 to support long filenames (up to 255 characters) and more than 77 entries. They are variable-length, always a multiple of 2048 bytes, and can grow on demand up to a maximum of 4 MB. (Acorn FileCore Phase 2 Functional Specification, §3.1–3.6.)

**Big directory header (28 bytes + directory name):**

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 1 | `StartMasSeq` — sequence number |
| +0x01 | 3 | `BigDirVersion` — reserved, must be 0 |
| +0x04 | 4 | `BigDirStartName` — `"SBPr"` (`0x72504253`) |
| +0x08 | 4 | `BigDirNameLen` — length of directory name |
| +0x0C | 4 | `BigDirSize` — total directory size in bytes |
| +0x10 | 4 | `BigDirEntries` — number of entries in directory |
| +0x14 | 4 | `BigDirNamesSize` — bytes allocated for name heap |
| +0x18 | 4 | `BigDirParent` — indirect disc address of parent directory |
| +0x1C | var | `BigDirName` — directory name, CR-terminated, padded to word boundary |

Directory entries follow immediately after the header.

**Big directory entry (28 bytes):**

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 4 | `BigDirLoad` — load address |
| +0x04 | 4 | `BigDirExec` — execution address |
| +0x08 | 4 | `BigDirLen` — length in bytes |
| +0x0C | 4 | `BigDirIndDiscAdd` — indirect disc address (SIN, 4 bytes) |
| +0x10 | 4 | `BigDirAtts` — attributes |
| +0x14 | 4 | `BigDirObNameLen` — length of object name in bytes |
| +0x18 | 4 | `BigDirObNamePtr` — offset into name heap for this entry's name |

The entry list has no terminating zero byte; `BigDirEntries` in the header gives the count. Entries are always word-aligned.

The **name heap** is a separate region within the directory where the variable-length name strings are packed. Each name is CR-terminated (`0x0D`) and padded with zero bytes to a 4-byte boundary.

**Backup directory entries** are stored between the name heap and the directory tail. Each backup entry is a single 4-byte word containing the indirect disc address of the corresponding object, to aid recovery of broken directories.

**Big directory tail (8 bytes at the end of the directory):**

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 4 | `BigDirEndName` — `"oven"` |
| +0x04 | 1 | `BigDirEndMasSeq` — must match `StartMasSeq` |
| +0x05 | 2 | Reserved, must be 0 |
| +0x07 | 1 | `BigDirCheckByte` — directory check byte |

### 3.5 Walkthrough: Reading a File

Here is the complete procedure to read a file given its pathname on a new-map disc:

1. **Find the root directory**: Resolve `root_dir` from the disc record (§2.3). Read the directory data from the resulting disc address.

2. **Validate the directory**: Check the start/end names match (`"Nick"` or `"Hugo"`, or `"SBPr"`/`"oven"` for big dirs). Verify the check byte (§A.2). Verify the master sequence numbers match.

3. **Search for the filename**: Walk the directory entries. For old/new directories, compare the 10-character name field (case-insensitive). For big directories, look up each entry's name from the name heap.

4. **Check if it's a directory**: If the attributes byte has bit 3 set, the entry is a subdirectory. Resolve its SIN and recurse from step 2.

5. **Resolve the SIN to disc extents**: Walk the zone map collecting all fragments with the matching fragment ID (§3.2). Apply the sharing offset if non-zero.

6. **Read the file data**: The fragments, concatenated in zone order, contain the file data. Read `length` bytes starting from the sharing offset within the first fragment.

For **old-map discs**, step 5 is simpler: the directory entry contains a direct disc address (in 256-byte units for L/D format). The file is stored contiguously at that address.

---

## 4. Writing to the Filesystem

### 4.1 Creating a File

FileCore's allocation strategy is sophisticated, designed to minimise fragmentation. The general procedure for creating a file is:

1. **Choose a zone**: FileCore prefers the zone containing the parent directory, expanding outward. For whole-file saves (`SAVE`), it tries hard to find a single contiguous extent, compacting zones if necessary. For sequential writes (`OPENOUT`), it allocates a starting extent and may extend later.

2. **Allocate space in the zone map**: Find a free fragment large enough (or combine adjacent free fragments). Split the free fragment if it's larger than needed. Assign a new fragment ID — the smallest unused ID, starting from 3.

3. **Update the map bit stream**: Rewrite the fragment descriptor for the allocated region with the new ID. Update the free chain links in affected zones.

4. **Create a directory entry**: Add an entry to the parent directory (maintaining alphabetical sort order). Write the object name, load/execution address, length, SIN, and attributes.

5. **Write the data**: Write file contents to the allocated disc addresses.

6. **Update the directory tail**: Increment the master sequence number, recompute the check byte, and write the tail.

7. **Update the zone checksums**: Recompute `ZoneCheck` for any modified zone. Verify `CrossCheck` XOR consistency.

**Small file sharing:** On new-map discs, if a file is smaller than the LFAU (largest file allocation unit = the minimum fragment size), it can share a disc object with its parent directory or siblings. FileCore stores the file within the same fragment as the directory, at a sector-aligned offset. The SIN encodes this sharing offset.

### 4.2 Deleting a File

1. **Remove the directory entry**: Shift subsequent entries down to fill the gap (entries must remain sorted).
2. **Free the disc space**: Walk the zone map, find all fragments with the file's fragment ID, and convert them to free space by setting their IDs to point into the free chain (or to 0 for the chain terminator).
3. **Update directory tail**: Increment sequence number, recompute check byte.
4. **Update zone checksums**.

### 4.3 Extending a File

When a file needs to grow beyond its current allocation:

1. **Try to extend in place**: If the fragment immediately following the file's last fragment is free, absorb it by extending the current fragment (adjust the terminating `1` bit position in the map).
2. **Allocate a new fragment**: If in-place extension is not possible, allocate a new fragment (possibly in a different zone) with the same fragment ID. The file now has multiple fragments — it is fragmented.
3. **Update the directory entry's length**.

FileCore will opportunistically compact zones during extension to try to reunite fragments. The compaction logic is in `s/FileCore32` in the RISC OS Open source.

### 4.4 Truncating a File

1. **Free excess space**: Walk the file's fragments in reverse, freeing allocation units from the end until the file fits within its new length.
2. **If the file now fits in fewer fragments**, free the trailing fragments entirely.
3. **Update the directory entry's length**.

---

## 5. Long Filenames (E+ and F+ Formats)

The E+ and F+ formats use **big directories** (§3.4) to support filenames up to 255 characters. The transition from the 10-character limit was a significant architectural change.

### 5.1 On-Disc Changes

Big directories differ from old/new directories in several ways:

- **Variable-length names**: Names are stored in a separate **name heap** within the directory, rather than in fixed 10-byte fields. Each directory entry has a 4-byte name length and a 4-byte offset into the heap.

- **Variable directory size**: Big directories can grow. When a new entry is added and there isn't enough space, FileCore extends the directory by reallocating its disc object. The maximum size is 4 MB.

- **4-byte SIN field**: The indirect disc address field is widened from 3 to 4 bytes, allowing `idlen` to exceed 16 bits and supporting more than 32,765 objects per disc.

- **Different magic numbers**: `"SBPr"` and `"oven"` replace `"Hugo"` and `"Nick"`.

### 5.2 The Name Heap

The name heap is a contiguous block within the directory, located after the directory entries. Names are stored as CR-terminated (`0x0D`) strings, each padded with zero bytes to a 4-byte boundary. The heap may contain gaps from deleted entries; FileCore does not compact the heap on every deletion but may do so when the directory is rewritten.

### 5.3 Compatibility

A disc formatted as E+ or F+ cannot be read by versions of RISC OS earlier than 4.0. RISC OS 3.x will see the disc but will report "Broken directory" when trying to open any directory, because the `"SBPr"` signature doesn't match `"Hugo"` or `"Nick"`.

The `format_version` field in the disc record (§2.1) allows RISC OS to identify at mount time whether the disc uses big directories, so it can reject incompatible discs gracefully rather than attempting to parse them.

---

## Appendix A: Detecting and Repairing Corruption

FileCore provides multiple layers of integrity checking. Understanding what each check detects is essential for building repair tools.

### A.1 Zone Map Checksums

Each zone has two integrity fields:

**ZoneCheck (offset +0x00):** A checksum byte computed over the entire zone sector. The algorithm is from Nick Reeves' E Format Design Document (1990):

```
; entry: R0 -> start of sector, R1 = zone length
; exit: LR = check byte, Z=1 if matches existing byte (good)
NewCheck:
    MOV    LR, #0
    ADDS   R1, R1, R0         ; C=0
loop:
    LDR    R2, [R1, #-4]!
    ADCS   LR, LR, R2         ; add with carry
    TEQS   R1, R0             ; preserves C
    BNE    loop
    AND    R2, R2, #&FF       ; ignore old sum
    SUB    LR, LR, R2
    EOR    LR, LR, LR, LSR #16
    EOR    LR, LR, LR, LSR #8
    AND    LR, LR, #&FF
```

In pseudocode: sum all 32-bit words in the sector using add-with-carry (forming a 33-bit running sum), subtract the existing check byte, then fold the 32-bit result down to 8 bits via XOR. A mismatch indicates the zone sector has been corrupted.

In C:

```c
/* Compute ZoneCheck byte for a zone sector.
   sector points to the zone data, len is the sector size in bytes.
   Returns the correct check byte for offset +0x00. */
uint8_t zone_check(const uint8_t *sector, unsigned len)
{
    const uint32_t *p = (const uint32_t *)(sector + len);
    uint64_t sum = 0;                 /* 33-bit accumulator */

    while (p != (const uint32_t *)sector) {
        p--;
        sum += (uint32_t)*p + (sum >> 32);   /* ADCS: add with carry */
        sum &= 0x1FFFFFFFF;                  /* keep 33 bits */
    }

    uint32_t s = (uint32_t)sum;
    s -= sector[0];                   /* subtract existing check byte */
    s = s ^ (s >> 16);               /* fold 32 → 16 */
    s = s ^ (s >> 8);                /* fold 16 → 8 */
    return (uint8_t)(s & 0xFF);
}
```

**CrossCheck (offset +0x03):** The XOR of all zones' CrossCheck bytes should equal `0xFF`. A mismatch suggests one or more zone sectors have been corrupted or belong to a different disc.

**Repair:** If a single zone has a bad ZoneCheck, recompute it from the sector contents (the map data itself may or may not be valid — the checksum only detects the discrepancy). If CrossCheck fails, identify which zone is the outlier.

### A.2 Directory Check Byte

Every directory (old, new, and big) has a check byte at the very end of the directory. The algorithm differs from the zone checksum — it uses a rotate-and-XOR scheme:

```
; From Nick Reeves' E Format Design Document, TestDirCheckByte
; Process directory entries, then directory tail, skipping the check byte itself
checksum = 0
for each byte in (entries ++ tail, excluding the check byte):
    checksum = byte XOR (checksum rotated right by 13 bits within 32 bits)
fold checksum to 8 bits: checksum = (checksum XOR (checksum >> 16) XOR (checksum >> 8)) & 0xFF
```

A mismatch triggers the **"Broken directory"** error. This is the most commonly seen FileCore corruption error.

**Common causes of "Broken directory":**
- Power loss during a directory write (the sequence number and check byte are updated last, so partial writes leave inconsistency)
- Bad sectors in the directory's disc area
- Software bugs — notably, big directories at exactly 4 MB triggered a validation bug in FileCore versions before 3.72 (RISC OS Open bug #415)
- Attempting to read an E+/F+ disc on RISC OS 3.x

**Repair:** If the directory data is otherwise intact (entries are valid, names are sensible), simply recompute the check byte and write it back. If the master/end sequence numbers don't match, set them both to the higher value. Tools like DiscKnight automate this.

### A.3 Map Consistency Checks

Beyond checksums, several structural invariants should hold:

- **Free chain integrity**: Every free fragment must be reachable from its zone's `FreeLink`, and the chain must terminate with an ID of 0. Orphaned free fragments (ID 0 but not on the chain) waste space but are not fatal.

- **Fragment ID uniqueness**: Each fragment ID (≥ 3) should appear in a contiguous sequence of zones. If the same ID appears in non-adjacent zones with a gap, the file is fragmented — which is normal — but if a fragment with that ID appears in a zone before the expected start zone, the map may be corrupt.

- **Total allocation unit count**: The total number of allocation units across all zones should equal `disc_size / bpmb` (rounded as appropriate). More or fewer suggests zone data has been lost or duplicated.

- **Cross-references with directories**: Every fragment ID ≥ 3 that appears in the map should correspond to a SIN referenced by some directory entry somewhere on the disc. Fragments with no directory reference are "lost" objects — allocated space that is wasted. A repair tool can free these.

### A.4 Boot Block Checks

The boot block has its own checksum at offset `+0x1FE` (two bytes from the end). The defect list has a check byte embedded in its terminator word. If these fail, the disc record may be untrustworthy. A repair strategy is to use the copy of the disc record stored in zone 0's map block (at disc address `map_start + 0x04`) as a fallback, since the map block has its own independent `ZoneCheck`.

### A.5 Sequence Number Mismatch

Both old/new and big directories store the master sequence number in both the header and the tail. FileCore increments both atomically during writes. A mismatch indicates an interrupted write. The standard repair is to set both to the higher value and recompute the check byte.

For old/new directories, the sequence number is a single byte in BCD (wrapping at 255). For big directories, it is also a single byte (`StartMasSeq` at offset +0x00 and `BigDirEndMasSeq` in the tail).

### A.6 Practical Repair Strategy

A typical disc repair tool (such as DiscKnight or the shareware `fsck`) performs these steps:

1. **Read and validate the boot block** (hard disc) or zone 0 (floppy). Check the disc record is plausible.
2. **Validate all zone checksums.** Recompute any that are wrong.
3. **Walk the zone map** to build a complete allocation bitmap. Check free chain integrity. Report and optionally repair orphaned free fragments.
4. **Walk the directory tree** starting from the root. For each directory:
   - Validate the check byte. Repair if needed.
   - Validate sequence numbers. Repair if mismatched.
   - For each entry, verify the SIN references a valid fragment in the map.
5. **Cross-reference map and directories**: Report fragments with no directory reference ("lost objects"). Optionally free them or move them to a "found" directory.
6. **Recompute and rewrite zone checksums** for any modified zones.

---

## Appendix B: Zone Compaction

Zone compaction is one of the most distinctive features of FileCore's new map design. Because all allocation is indirected through the zone map (files are identified by fragment ID, not disc address), FileCore can silently relocate file data on disc without updating any directory entries. This is impossible in old-map formats or in most other filesystems, where directories store physical disc addresses.

The compaction logic lives primarily in `s/FileCore32` in the RISC OS Open source. Nick Reeves' E Format Design Document describes the design philosophy:

> It builds up lists of moves which it cancels, combines, joins, splits, collects together in groups to be done together, and sorts into an order that reduces head movement for the scatter read/write primitives of the device drivers.

### B.1 When Compaction Occurs

FileCore does not compact entire discs or zones in one pass. Instead, it performs small, targeted compactions in response to allocation pressure:

- **Whole-file save (`SAVE`)**: If no single free extent large enough exists in the preferred zone, FileCore compacts until either a suitable gap appears or it has moved data totalling twice the file's length without success. Only then does it fall back to multi-fragment allocation.

- **File open/close**: Compaction happens opportunistically if a particularly good opportunity is found (e.g. two fragments of the same file are adjacent once a small intervening fragment is moved).

- **`*Compact` command**: Explicitly requests a full compaction pass across the disc. This iterates over all zones.

The key principle is that compaction is **incremental and demand-driven**, not a batch operation. Users rarely notice it happening.

### B.2 What Compaction Achieves

Within a single zone, compaction can perform several optimisations:

1. **Coalesce free space**: Move an allocated fragment that sits between two free fragments, merging three map entries into one larger free extent.

2. **Reunite file fragments**: If two fragments of the same file (same fragment ID) are in the same zone separated by other data, move the intervening data to bring them together. The two fragments merge into one (the map entries join and the `1` terminator of the first is removed, extending it to cover both).

3. **Eliminate small fragments**: Move a small fragment into free space elsewhere in the zone so that its former location merges with adjacent free space, creating a larger usable extent.

### B.3 The Compaction Algorithm

The following describes a simplified but implementable version of FileCore's zone compaction, based on the source and design document:

```
function compact_zone(zone):
    # Phase 1: Survey the zone
    fragments = decode_zone_fragments(zone)   # list of (id, start_au, length_au)
    free_list = [f for f in fragments if is_free(f)]
    alloc_list = [f for f in fragments if not is_free(f)]

    # Phase 2: Identify beneficial moves
    moves = []
    for each allocated fragment A between two free fragments F1, F2:
        # Moving A elsewhere merges F1 + A_space + F2 into one large free extent
        cost = A.length      # allocation units to move
        benefit = 1           # one fewer free fragment, one larger extent
        if find_free_space(zone, A.length, excluding=[F1, F2]):
            moves.append(Move(src=A, dst=target_free_space))

    for each pair of fragments with the same ID that are separated:
        # Moving the intervening data reunites them
        gap = fragments_between(frag1, frag2)
        if total_size(gap) is moveable:
            moves.append(plan_to_clear_gap(gap))

    # Phase 3: Optimise the move list
    # Cancel moves that conflict (same source or destination)
    # Combine moves of adjacent fragments into single scatter operations
    # Sort by disc address to minimise head seeks
    moves = optimise_moves(moves)

    # Phase 4: Execute
    for each move in moves:
        read_data(move.src_disc_addr, move.length)
        write_data(move.dst_disc_addr, move.length)
        update_zone_map(zone, move.src, move.dst)

    recompute_zone_checksum(zone)
```

### B.4 Map Updates During Compaction

When a fragment is moved within a zone, the map bit stream must be rewritten. This involves:

1. **Clear the source**: Convert the moved fragment's map entry to free space. If the source is now adjacent to existing free space, merge them by adjusting bit positions (remove the fragment boundary — the old `idlen`+padding+`1` terminator — and extend the neighbouring free entry).

2. **Split the destination free space**: Insert a new fragment descriptor at the destination position. If the moved fragment is smaller than the destination free space, the free entry is split: the moved fragment occupies part of it, and the remainder stays free.

3. **Update the free chain**: Relink `FreeLink` and the inter-fragment free chain pointers so that all free fragments remain reachable.

4. **Recompute `ZoneCheck`**.

Because the fragment ID does not change during a move, no directory entries need updating. This is the central elegance of the indirected map design.

### B.5 Cross-Zone Considerations

FileCore's compaction operates one zone at a time. It does not move data between zones (that would change the fragment's zone membership, which affects how the fragment is found during SIN resolution). However, the `*Compact` command iterates over all zones, compacting each in turn.

A file that spans multiple zones has one fragment per zone. Compaction within each zone can improve the contiguity of that zone's fragment but cannot merge fragments across zone boundaries.

### B.6 Constraints

Several constraints limit what compaction can do:

- **Minimum fragment size**: A fragment must be at least `idlen + 1` bits wide in the map. This corresponds to one allocation unit. You cannot create a fragment smaller than this.

- **Granularity**: Data must be physically allocated in whole sectors. If the allocation unit (`bpmb`) is smaller than the sector size, the granularity is the sector size. Moves must be aligned to granularity boundaries.

- **Shared objects**: Fragments containing shared objects (a directory and its small sub-files) are more complex to move, as the sharing offsets in directory entries would need updating. FileCore avoids moving shared fragments during automatic compaction.

- **System object (ID 2)**: The boot block, zone map, and root directory live in fragment ID 2. This cannot be moved by normal compaction.

- **Data safety**: FileCore writes the data to the destination before updating the map. If power is lost between the write and the map update, the data exists in both locations but only the old map entry points to it — no data is lost. The zone checksum will detect the inconsistency on the next mount.

---

## Appendix C: Creating a New FileCore Filesystem (Formatting)

On RISC OS, formatting is a two-stage process: the hardware module (e.g. ADFS) performs the low-level track format via `FileCore_DiscOp` reason code 4, then `FileCore_LayoutStructure` writes the logical structures. When creating disc images offline (e.g. for emulators), only the logical layout matters — there is no physical track formatting.

This appendix describes how to initialise a blank new-map disc image from scratch. The procedure applies to E, F, E+, F+, and G format images. Old-map (L, D) formatting is simpler and is covered briefly at the end.

### C.1 Choose Disc Parameters

Before writing anything, decide the disc geometry. The key parameters are:

| Parameter | Typical E (800K floppy) | Typical F (hard disc) |
|-----------|------------------------|----------------------|
| `log2_sector_size` | 10 (1024 bytes) | 9 (512 bytes) |
| `sectors_per_track` | 5 | varies |
| `heads` | 2 | varies |
| `density` | 0 | 0 |
| `idlen` | 15 | 15 |
| `log2_bpmb` | 10 (1024 bytes) | depends on disc size |
| `nzones` | 1 | depends on disc size |
| `zone_spare` | 0 (usually) | 60 × 8 = 480 (or similar) |
| `disc_size` | 819200 | varies |
| `root_dir` | `0x000203` (515) | varies |

**Choosing `log2_bpmb` and `nzones`:** The allocation unit size (`bpmb = 2^log2_bpmb`) and the number of zones are interdependent. Each zone is one sector of map data. The total map capacity in allocation units is:

```
zone0_bits = (sector_size - 64) * 8        # zone 0 has 64-byte header
zoneN_bits = (sector_size - 4) * 8         # other zones have 4-byte header
total_map_bits = zone0_bits + (nzones - 1) * zoneN_bits
```

This must cover the entire disc: `total_map_bits * bpmb >= disc_size`. And each zone must have enough bits for at least one fragment: `zoneN_bits >= idlen + 1`.

The LFAU (largest file allocation unit, i.e. the minimum fragment size) is `max(bpmb, sector_size)` — but in practice it is dominated by `bpmb` since the map imposes a minimum of `idlen + 1` map bits per fragment.

**The `zone_spare` field** specifies how many bits at the start of each zone's allocation area are cross-zone continuation bits. For zone 0, these bits are part of the first fragment (the system object). In practice `zone_spare` is often set so that the zone boundaries don't fall awkwardly mid-fragment. (PRM vol. 2, ch. 28, "The zone".)

### C.2 Layout: What Goes Where

On a freshly formatted new-map disc, the logical layout is:

```
Disc address 0x000:
  +-- Zone nzones/2 map block (zone 0's map sector)     --+
  |   [zone header][disc record copy][allocation bits]     |  Fragment ID 2
  +-- Zone (nzones/2)+1 map block                        --+  (system object)
  |   ...                                                  |
  +-- Zone (nzones/2)+(nzones-1) map block               --+
  +-- Root directory                                     --+
  |   [header][empty entries][tail]                        |
  +-- (padding to end of system object fragment)         --+

  (On hard discs only:)
  Disc address 0xC00:
  +-- Boot block                                         --+
  |   [defect list][disc record][flag][checksum]            |
  +--                                                    --+
```

For a single-zone floppy (nzones=1), the map block is at disc address `0x000`, and the root directory follows it immediately. The entire system object (ID 2) is one contiguous fragment covering the map + root directory.

For a multi-zone hard disc, the map blocks start at disc address `zone_disc_address(nzones/2)` and the root directory follows after the last map block, all within the same fragment (ID 2). The boot block at `0xC00` is also part of the system object.

### C.3 Step-by-Step: Initialising a New-Map Image

#### Step 1: Zero-fill the image

Create a file of `disc_size + 0` bytes (or `disc_size` bytes if not adding an FCFS trailer), filled with zeros.

#### Step 2: Write the boot block (hard discs only)

At disc address `0xC00`:

1. Write an empty defect list: a single word `0x20000000 | checkbyte` at offset `+0x000`. With no defects, the checkbyte is computed over an empty list, which gives `0x20000000`.
2. Write the disc record at offset `+0x1C0` (64 bytes).
3. Write the boot block flag byte at `+0x1FC`.
4. Compute and write the boot block checksum at `+0x1FE`.

#### Step 3: Initialise the zone map

For each zone `z` from 0 to `nzones - 1`:

a. Compute the disc address of this zone's map block. For a single-zone disc this is `0x000`. For multi-zone discs, use the zone address formula from §2.4.

b. Write the 4-byte zone header:
   - `ZoneCheck`: will be computed last.
   - `FreeLink`: bit offset to the first free fragment (computed below).
   - `CrossCheck`: set so that all zones' CrossCheck bytes XOR to `0xFF`.

c. For zone 0 only: write the disc record copy at offset `+0x04` (60 bytes of the 64-byte disc record, as the first 4 bytes of zone 0 are the zone header).

d. Write the allocation bit stream. On a fresh disc, the stream contains exactly two fragments:
   - **Fragment ID 2 (system object)**: Covers the map sectors + root directory + boot block. Set the `idlen`-bit ID field to `2`, followed by enough `0` bits and a terminating `1` to cover the required number of allocation units.
   - **Fragment ID 0 (free space)**: Covers the rest of the zone. Set the `idlen`-bit ID field to `0` (end of free chain), followed by `0` bits and a terminating `1` covering the remaining allocation units.

   For multi-zone discs, only the zone(s) containing the system object will have fragment ID 2. All other zones will contain a single free fragment covering the entire zone (after the `zone_spare` continuation bits).

e. Set `FreeLink` to point to the free fragment's position in the bit stream.

f. Compute and write `ZoneCheck` using the algorithm from §A.1.

#### Step 4: Write the root directory

Immediately after the last map block (within fragment ID 2), write an empty root directory:

**For new directories (E, F):**
1. Write the header: master sequence number (e.g. `0`), start name `"Nick"`.
2. Write a NUL byte at the first entry position (indicating no entries).
3. Write the tail (reading backwards from the directory end): check byte (computed last), end name `"Nick"`, end sequence number (matching header), directory name `"$"` (padded to 10 bytes), title (empty, padded to 19 bytes), parent disc address (`0x000000` for root).
4. Compute and write the check byte using the algorithm from §A.2.

**For big directories (E+, F+, G):**
1. Write the header: sequence number, `BigDirVersion` = 0, start name `"SBPr"`, `BigDirNameLen` = 1, `BigDirSize` = directory size, `BigDirEntries` = 0, `BigDirNamesSize` = 4 (padded `"$"`), `BigDirParent` = 0.
2. Write `"$"` (CR-terminated, padded to 4 bytes) as the directory name in the header.
3. Write the empty name heap (no entries).
4. Write the tail: `"oven"`, end sequence number (matching header), reserved = 0, check byte.
5. Compute the check byte.

#### Step 5: Set the root_dir field

The disc record's `root_dir` field must be the indirect disc address (SIN) of the root directory. For a standard format where the root directory is part of fragment ID 2:

```
root_dir = (2 << 8) | sharing_offset
```

Where `sharing_offset` = `(byte_offset_of_root_dir_within_fragment / sharing_unit) + 1`. For an 800K E-format floppy with a double-copied single-zone map (2 sectors) followed by the root directory, the root directory starts at byte 0x800 within the fragment. With a sharing unit of 1 sector (1024 bytes): `sharing_offset = (0x800 / 0x400) + 1 = 3`, giving `root_dir = 0x000203` (515 decimal).

Write this value into both the boot block disc record (if hard disc) and the zone 0 map block disc record copy.

#### Step 6: Final checksum pass

Recompute `ZoneCheck` for all zones (the disc record copy was written after the initial checksum). Recompute the boot block checksum if applicable.

### C.4 Worked Example: 800K E-Format Floppy

Parameters:
- `disc_size` = 819200 (800 × 1024)
- `log2_sector_size` = 10, sector_size = 1024
- `nzones` = 1
- `idlen` = 15
- `log2_bpmb` = 10, bpmb = 1024 (one allocation unit = one sector)
- `zone_spare` = 0
- `root_dir` = `0x000203` (515 decimal: fragment ID 2, sharing offset 3)

Layout:
- Disc address `0x000`: Zone 0 map block, primary copy (1024 bytes = 1 sector). Header (4 bytes) + disc record (60 bytes) + allocation bit stream (960 bytes = 7680 bits).
- Disc address `0x400`: Zone 0 map block, backup copy (1024 bytes). The new map is double-copied (Nick Reeves, "New Disc File Structure for RISC OS").
- Disc addresses `0x800`–`0xFFF`: Root directory (2048 bytes = 2 sectors).
- Disc address `0x1000` onwards: Free space.

The system object (fragment ID 2) is 4 allocation units (4 sectors, 4096 bytes). It contains the primary map block, the backup map block, and the root directory.

Map bit stream (7680 bits):
- Fragment ID 2 (system object): 15-bit ID = `0x0002`, then padding and `1` bit. It covers 4 allocation units. That's `15 + 2 + 1 = 18` bits total: `[15-bit id=2][00][1]`.
- Fragment ID 0 (free, chain terminator): 15-bit ID = `0x0000`, then enough bits to cover the remaining ~796 allocation units. That's `15 + 795 + 1 = 811` bits: `[15-bit id=0][795 zero bits][1]`.
- The rest of the 7680 bits are unused (past the end of the disc's allocation units).

`FreeLink` calculation:

- The allocation bits start after the 64-byte zone 0 header, at bit 512 (= 64 × 8).
- The system fragment is 18 bits long, so the free fragment starts at bit 512 + 18 = 530.
- `FreeLink` is measured from byte 1 of the sector (i.e. from bit 8), so: FreeLink = 530 − 8 = 522.
- Bit 15 is always set: FreeLink = 522 | 0x8000 = `0x820A`.

### C.5 Old Map Formatting (L, D)

For old-map discs, the initialisation is simpler:

1. **Write the free space map** at sectors 0 and 1. Sector 0 holds 82 three-byte start addresses; sector 1 holds the corresponding 82 three-byte lengths (both in 256-byte units). On a fresh disc, entry 0 describes the entire free area (everything except the map and root directory), and entries 1–81 are zero.

2. **Write the root directory** at its fixed disc address (e.g. `0x200` for L-format). Initialise with the header (`"Hugo"`), an empty entry list (NUL first byte), and the tail with check byte.

3. **Write the disc name** split across the two map sectors (odd characters at sector 0 offset `+0xF9`, even characters at sector 1 offset `+0xF9`).

There is no boot block on floppy discs. Hard discs with old maps do have a boot block at `0xC00` with a disc record and defect list, initialised as in step 2 of §C.3.

---

## Glossary

**Allocation unit** — The smallest unit of disc space managed by the zone map. One allocation unit = 2^log2_bpmb bytes (the "bytes per map bit" value from the disc record). Every fragment occupies a whole number of allocation units.

**Big directory** — Variable-length directory format used by E+, F+, and G. Identified by the `"SBPr"` / `"oven"` magic strings. Supports up to ~32,000 entries with filenames up to 255 characters, stored in a name heap.

**Boot block** — A 512-byte structure at disc address `0xC00` on hard discs (only). Contains the defect list, a copy of the disc record (at offset `+0x1C0`), a flag byte, and a checksum. Floppy discs have no boot block.

**bpmb** (`log2_bpmb`) — Disc record field: log₂ of the number of bytes per map bit (i.e. per allocation unit). Determines the map granularity.

**Check byte** — A single-byte checksum protecting a directory. Stored in the directory tail. Computed by a rotate-and-XOR algorithm over the directory contents.

**CrossCheck** — Byte at offset `+0x03` in each zone header. The XOR of all zones' CrossCheck bytes must equal `0xFF`. Detects zone-level corruption or a zone belonging to a different disc.

**Defect list** — A list of known bad-sector addresses stored in the boot block (hard discs only). Terminated by a word with bits 29–31 set and a check byte in bits 0–7. Fragment ID 1 in the zone map marks defective regions.

**Disc address** — A byte offset from the start of the disc image. All FileCore addresses are byte offsets, not sector numbers (even on old-map discs where the free space map uses 256-byte units).

**Disc record** — A 20–64 byte structure describing the disc's geometry and map parameters. Found in the boot block (hard discs) or at the start of zone 0's map block (new-map discs). Key fields include `log2_sector_size`, `sectors_per_track`, `heads`, `idlen`, `log2_bpmb`, `nzones`, `root_dir`, and `disc_size`.

**Exec address** — The 32-bit execution address in a directory entry. For date-stamped files (top 12 bits of load address = `0xFFF`), the low 8 bits of the exec address hold the low byte of the 40-bit centisecond timestamp.

**Filetype** — A 12-bit value encoded in bits 19–8 of the load address when the file is date-stamped (top 12 bits = `0xFFF`). Identifies the file's type (e.g. `0xFFD` = Data, `0xFFF` = Text).

**Fragment** — A contiguous run of allocation units on disc sharing the same fragment ID. A file or directory is composed of one or more fragments. Fragments of the same object may be scattered across zones (fragmentation).

**Fragment ID** — An `idlen`-bit identifier in the zone map bit stream. ID 0 = free space, 1 = defect, 2 = system (map and boot area), ≥ 3 = a file or directory. The fragment ID plus a sharing offset forms a SIN.

**FreeLink** — 16-bit field at offset `+0x01` in each zone header. Points to the first free fragment in the zone (as a bit offset from byte 1 of the zone). Bit 15 is always set. A value of `0x8000` means no free space in this zone.

**Hugo** — The 4-byte magic string `"Hugo"` at the start of old-format directories (S, M, L, D, E, F). Named after Hugo Tyson. Repeated in the tail as a validation word.

**idlen** — Disc record field: the number of bits used for fragment IDs in the zone map. Determines the maximum number of objects on disc (2^idlen − 3 usable IDs). Typically 15 for floppies and old-format hard discs. The Acorn Phase 1 spec (Ursula) raised the limit to 19 for big map discs; RISC OS 5 (FileCore 3.75, 2017) raised it further to 21.

**LFAU** — Largest Fragment Allocation Unit. Defined as max(sector_size, bpmb). This is the minimum granularity at which disc space is actually allocated; files smaller than one LFAU share a fragment with their parent directory.

**Load address** — The 32-bit load address in a directory entry. When the top 12 bits are `0xFFF`, the entry is date-stamped: bits 19–8 hold the filetype and bits 7–0 hold the high byte of the 40-bit timestamp. Otherwise it is an actual memory load address (Acorn legacy).

**Name heap** — A region within a big directory containing CR-terminated (`0x0D`) filenames padded with zero bytes to 4-byte boundaries. Directory entries reference names by offset into this heap. May contain gaps left by deleted entries.

**New directory** — Directory format used by D, E, and F. 2048 bytes (`0x800`), up to 77 entries, identified by `"Nick"` (or `"Hugo"`) in the tail. D uses sector addresses for entry disc addresses; E and F use SINs.

**New map** — Zone-based allocation map used by E, F, E+, F+, and G. The disc is divided into `nzones` zones, each one sector long, containing a packed bit stream of fragment-ID/length pairs.

**Nick** — The 4-byte magic string `"Nick"` found in the header and tail of new-format directories (D, E, F) and big directories (E+, F+, G). Named after Nick Reeves, designer of the E format. On D-format discs, either `"Hugo"` or `"Nick"` may appear. S/M/L directories on 8-bit systems use `"Hugo"` only.

**nzones** — Disc record field: the number of zones in the new map. Each zone is one sector. Zone 0 also holds the disc record.

**Old directory** — Directory format used by S, M, and L. 1280 bytes (`0x500`), up to 47 entries, identified by `"Hugo"` only (no `"Nick"` in the tail on 8-bit systems). File attributes are encoded in bit 7 of each filename byte.

**Old map** — Flat free-space table used by S, M, L, and D. Two 256-byte sectors at disc addresses `0x000` and `0x100`, containing 82 three-byte (start, length) pairs in 256-byte units.

**SBPr / oven** — The 4-byte magic strings `"SBPr"` (header, offset +0x04) and `"oven"` (tail) in big directories (E+, F+, G), analogous to `"Hugo"` / `"Nick"` in older formats. Defined as the single 8-byte literal `"SBProven"` in the source (`s/BigDirCode` line 71), a reference to `sproven`, the Acorn login of Simon Proven, who designed and implemented big directory support.

**Sequence number** — A byte stored in both the header and tail of every directory. Incremented on each directory update. A mismatch between header and tail indicates the directory was not completely written (e.g. power loss).

**share_size** — Disc record field (RISC OS 3.6+): log₂ of the sharing unit in sectors. Files smaller than one LFAU share a fragment with their parent directory; the sharing offset within the SIN locates the file's data within the shared fragment.

**Sharing offset** — The byte offset within a shared fragment that locates a small file's data. Encoded in the low bits of the SIN (below the fragment ID). Only relevant for files smaller than one LFAU.

**SIN** — System Internal Number. A disc address used in new-map directory entries. The top `idlen` bits (after shifting) give the fragment ID; the remaining bits give the sharing offset. Resolved by walking the zone maps to find all fragments with that ID, then applying the offset.

**Zone** — One sector of the new map. Each zone manages a region of disc and contains a 4-byte header (`ZoneCheck`, `FreeLink`, `CrossCheck`) followed by a packed bit stream. Zone 0 additionally holds the disc record (64 bytes).

**ZoneCheck** — Checksum byte at offset `+0x00` in each zone header. Computed by summing all 32-bit words in the sector with carry, subtracting the existing check byte, and folding to 8 bits via XOR (see §A.1).

**zone_spare** — Disc record field: the number of bits at the start of each non-zero zone reserved for a fragment that spans the zone boundary. These bits belong to the last fragment of the preceding zone.

---

## References

- *RISC OS 3 Programmer's Reference Manual*, vol. 2, ch. 28 "FileCore" — disc record, maps, directories, boot block. [Online (riscos.com)](http://www.riscos.com/support/developers/prm/filecore.html)
- *RISC OS 3 Programmer's Reference Manual*, vol. 5a, ch. 110 "FileCore" — RISC OS 3.6 extensions: big discs, sector addressing, sharing. [Online (riscos.com)](http://www.riscos.com/support/developers/prm/filecorenew.html)
- Nick Reeves, "New Disc File Structure for RISC OS" (E Format Design Document, 1990) — original specification of the new map format. [Archived text](https://www.chiark.greenend.org.uk/~theom/riscos/docs/ultimate/a252efmt.txt)
- RISC OS Open wiki — FileCore documentation pages including disc record, directories, old and new map formats, boot block, and disc addresses. [FileCore index](https://www.riscosopen.org/wiki/documentation/show/FileCore)
- RISC OS Open FileCore source: `RiscOS/Sources/FileSys/FileCore/` — Apache 2.0 licence; key files include `s/FileCore05` (leaf routines), `s/FileCore15` (map bit operations), `s/FileCore25` (directory check byte, `TestDirCheckByte`), `s/FileCore31` (new map allocation), `s/FileCore32` (new map auto-compaction), `s/FileCore33` (new map small routines, `NewCheck`), `s/FileCore40` (filename and directory operations), `s/BigDirCode` (big directory support), `hdr/FileCore` (exported constants and disc record layout). [GitLab](https://gitlab.riscosopen.org/RiscOS/Sources/FileSys/FileCore)
- Acorn Computers Ltd, FileCore functional specifications. Originally distributed to registered developers. Where the two specifications conflict, the Phase 2 spec takes precedence; the Phase 1 spec covers only the intermediate stage of increasing `idlen`. The disc record and related structures are as specified in Phase 2.
  - "FileCore — Phase 1 Functional Specification" (Project: Ursula, Revision 0.05, 1997), Simon Proven — extending `idlen` beyond 15 bits to support larger discs. [Phase 1 spec](https://www.marutan.net/wikiref/Acorn%20Registered%20Developer%20REFERNC/RO4/API/HTML/FILECORE.HTM), [readme](https://www.marutan.net/wikiref/Acorn%20Registered%20Developer%20REFERNC/RO4/API/HTML/FILECORE.REA)
  - "FileCore — Phase 2 Functional Specification" (Document Ref: 1309,208/FS, Project: Ursula, Revision 0.05), Simon Proven — big directories, extended disc record, long filenames. [Phase 2 spec](https://www.marutan.net/wikiref/Acorn%20Registered%20Developer%20REFERNC/RO4/API/HTML/FILECORE.000)
- J.G. Harston, "Acorn 8-Bit ADFS Filesystem Structure" — definitive reference for S/M/L format structure, old map checksum, directory layout, and disc identification. [mdfs.net](https://mdfs.net/Docs/Comp/Disk/Format/ADFS)
