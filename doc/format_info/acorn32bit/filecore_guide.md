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
| **E+** | New | Big | 1024 (floppy) or 512 (HD) | as E | 4 MB dir limit | 255 chars | RISC OS 4 |
| **F+** | New | Big | 512 | as F | 4 MB dir limit | 255 chars | RISC OS 4 |
| **G** | New | Big | 2048 or 4096 | — (HD only) | 4 MB dir limit | 255 chars | RISC OS 5 |

**S, M, and L** use an identical filesystem structure — the old map and old directory formats described in this document. They differ only in physical geometry: S is a 160 KB single-sided 40-track floppy, M is a 320 KB single-sided 80-track floppy, and L is a 640 KB double-sided 80-track floppy. Hard discs formatted with old-map ADFS also use the same structure with 256-byte sectors and geometry determined by the drive. (See [mdfs.net ADFS structure](https://mdfs.net/Docs/Comp/Disk/Format/ADFS) for a thorough treatment of the 8-bit formats.)

**E and F** (and their + variants) share the same on-disc structures — new map, new directories, and boot block. The format letter reflects the disc record parameters (sector size, sectors per track, number of zones) rather than any structural difference. F format was introduced in RISC OS 3.6 for 1.6 MB floppies with 10 sectors per track and 4 zones; E format uses 5 sectors per track and typically 1 zone. Hard discs use the same structures regardless of whether they are labelled E or F — the distinction is just the default parameter choices at format time. Similarly, E+ and F+ differ only in parameters; both use big directories and the new map. (Source: `Doc/Formats` in the FileCore source tree.)

**Track ordering** differs between old-map and new-map floppies. On S/M/L discs, tracks are **sequential**: all tracks on side 0 come first, then all tracks on side 1 (for L). The logical sector formula is `sector + track × 16 + side × (tracks × 16)`. On D/E/F discs, tracks are **interleaved**: track 0 side 0 is followed by track 0 side 1, then track 1 side 0, etc. The formula is `sector + (track × 2 + side) × sectors_per_track`. Strictly this is not implied by the map type: it is selected by `DiscRecord_SequenceSides_Flag`, bit 6 of the disc record's `low_sector` field (§2.1), which is set for sequential ordering and clear for interleaved. The correlation with map type reflects how each format is conventionally created rather than any structural requirement, and on a new-map disc you should read the flag rather than assume.

### 1.2 How to Identify the Map Type

Read the disc record (see §2). The `nzones` field distinguishes the two map types by implication — but the most reliable test is to check for the **old map signature**:

- **Old map (S, M, L, D)**: A flat free-space table occupying two 256-byte sectors at disc addresses `0x000` and `0x100`. Not a bit stream. See §2.5 for the full layout.

- **New map (E, F, E+, F+, G)**: The disc record has `nzones >= 1` and `idlen >= 1`. The allocation data is a packed bit stream in zone sectors. There is no flat free-space table.

A pragmatic identification approach for a disc image of unknown format (from [mdfs.net](https://mdfs.net/Docs/Comp/Disk/Format/ADFS)):

1. Read at offset `0x200` (512 bytes from the start). If bytes 1–4 are `"Hugo"` or `"Nick"`, this is the root directory of a 256-byte-sector old-map disc (S, M, or L).

2. If no match, read at offset `0x400` (1024 bytes). If bytes 1–4 are `"Hugo"` or `"Nick"`, this is an **old-map D-format disc** — `0x400` is D's fixed root-directory address (§2.3). E and F discs will *not* match here: their root is wherever `root_dir` points, and on a single-zone E floppy `0x400` holds the backup copy of the zone 0 map block. Fall through to steps 3–4 for those.

3. On a hard disc, read the boot block at disc address `0xC00`. The disc record is at offset `+0x1C0` within it. If `idlen` (disc record offset `+0x04`) is non-zero, the disc uses a new map. This also applies to multi-zone new-map floppies (F format) — see §2.2. If the boot block fields aren't plausible (e.g. all zero), the disc may be a single-zone new-map floppy (E format); fall through to step 4.

4. On a floppy with 1024-byte sectors, distinguish old from new map by examining disc address `0x000`:
   - If a valid disc record is present at offset `+0x04` (non-zero `idlen`), this is a new-map disc (E or F) — the zone 0 map block starts at `0x000`.
   - Otherwise the disc is old-map D format, where the free space map is at `0x000`–`0x1FF`.

### 1.3 How to Identify the Directory Type

Having identified the map type, determine the directory format by examining the root directory's header and tail bytes:

- **Old directories** (S, M, L): Exactly `0x500` bytes (5 × 256-byte sectors), up to 47 entries. The header and tail contain the validation string `"Hugo"` — 8-bit ADFS does not support `"Nick"`. Attributes are encoded in name bytes rather than a separate field (see §3.3). The check byte at the end of the directory is always zero on 8-bit ADFS; 32-bit ADFS computes it (see §A.2).

- **New directories on old-map media** (D): Same entry structure as old directories but `0x800` bytes (2048 bytes = 2 × 1024-byte sectors) and up to 77 entries. Attributes are stored in a separate byte (offset `+0x19` of the entry) rather than in the name bits. The validation string may be `"Hugo"` or `"Nick"`.

- **New directories** (E, F): Same structure as D, except that the parent field in the tail is a SIN rather than a sector address (§3.3). Always `0x800` bytes (2048), holding up to 77 entries — `NewDirSize` is a hard constant in `s/Defns`, not derived from the sector size or the LFAU. The validation string may be `"Nick"` **or** `"Hugo"` — do not test for `"Nick"` alone. RISC OS writes `"Nick"` for directories it creates, but formatting software often writes `"Hugo"` for the root: on both sample hard discs the root is `"Hugo"` while all 4,857 subdirectories are `"Nick"`. (PRM vol. 2, ch. 28, "Directories".)

- **Big directories** (E+, F+, G): Identified by the 4-byte signature `"SBPr"` at offset `+0x04` of the directory header and `"oven"` in the tail. Big directories are variable-length (always a multiple of 2048 bytes), can grow dynamically up to 4 MB, and support filenames up to 255 characters. The `format_version` field (disc record offset `+0x2C`) equals 1 when the disc uses big directories. The source (`s/BigDirCode`, `TestBigDir`) detects big directories by testing `DiscRecord_BigDir_DiscVersion == 1`.

### 1.4 Quick Identification Pseudocode

```
# Try the boot block first — present on hard discs AND multi-zone
# (F format) floppies. Only single-zone (E format) floppies lack one,
# in which case the boot block area reads as all zero; fall back to
# the zone 0 map block at disc address 0x000+0x04.
disc_record = read boot_block at 0xC00
if disc_record is all zero or implausible:
    disc_record = read disc_record at 0x04   # zone 0 map block, floppy
if disc_record.idlen == 0:
    format = old map (L or D)
    dir_type = old
else:
    format = new map
    if disc_record.format_version == 1:   # offset 0x2C
        dir_type = big                    # E+, F+, or G
    else:
        dir_type = new                    # E or F
    if disc_record.sectors_per_track >= 10 or disc_record.nzones >= 4:
        letter = F (or F+/G)      # §1.1: F uses 10 sectors/track, typically 4 zones
    else:
        letter = E (or E+)        # §1.1: E uses 5 sectors/track, typically 1 zone
    # For HARD DISCS the E/F letter is just a naming convention for whichever
    # default parameters were chosen at format time (§1.1) — there is no
    # structural test that recovers "the" letter for an arbitrary hard disc,
    # and the test above is only meaningful for floppies.
```

**Do not use `disc_size > 512MB` or `big_flag` to distinguish E from F** — that tests something else entirely (RISC OS 3.6's hard-disc "big disc" partition flag) and gives the wrong answer for floppies. Confirmed on `adfs1600F.adf`, a genuine 1.6 MB F-format floppy (10 sectors/track, 4 zones): its `disc_size` is 1,638,400 bytes (nowhere near 512 MB) and `big_flag` is 0, so that test would call it "E".

---

## 2. Finding the Key Structures

### 2.1 The Disc Record

The disc record is the single most important structure on a FileCore disc. Every other structure's location and interpretation depends on values in the disc record. (PRM vol. 2, ch. 28, "The disc record".)

**Location:**

- **Hard disc**: Disc address `0xC00` holds the boot block (two sectors). The disc record is at offset `+0x1C0` within the boot block, i.e. disc address `0xDC0`. Note: `0xC00` is a byte address, not a sector number, so this works regardless of whether sectors are 256, 512, or 1024 bytes.

- **Floppy (new map), single zone (E format, `nzones == 1`)**: Zone 0's map block starts at disc address `0x000`. The disc record is at `+0x04` within it (after the 4-byte zone header). This is disc address `0x04`.

- **Floppy (new map), multiple zones (F format, `nzones > 1`)**: The map is *not* at `0x000` — it is relocated toward the middle of the disc (§2.4), and `0x000` onward is unused/zero. As on hard discs, there is a boot-block copy of the disc record at disc address `0xC00` (record at `+0x1C0`, i.e. `0xDC0`) — see §2.2, which despite its name is not exclusive to hard discs. Reading the boot-block copy is the reliable way to get the disc record on this format; computing the zone-0 map's address from `nzones` and `disc_size` alone is not — see the caveat in §2.4.

- **Floppy (old map)**: The disc record concept doesn't exist as a single structure. Geometry is implicit in the format. S/M/L floppies have 256-byte sectors, 16 sectors per track. D floppies have 1024-byte sectors, 5 sectors per track, 2 heads, 80 tracks.

**The disc record structure (20 bytes minimum, extended to 60 bytes from RISC OS 3.6):**

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| +0x00 | 1 | `log2_sector_size` | Log₂ of sector size in bytes (8 = 256, 9 = 512, 10 = 1024). |
| +0x01 | 1 | `sectors_per_track` | Sectors per track (physical geometry). |
| +0x02 | 1 | `heads` | Number of disc surfaces — but *n−1* on old ADFS floppy formats (`hdr/FileCore`). |
| +0x03 | 1 | `density` | Encoding density (0 = hard disc, 1 = single, 2 = double, 3 = double+, 4 = quad, 8 = octal). |
| +0x04 | 1 | `idlen` | Fragment ID width in bits. 0 for old map. Max 15 (new map), 19 (big map, Ursula), or 21 (RISC OS 5). |
| +0x05 | 1 | `log2_bpmb` | Log₂ of bytes per map bit (allocation unit size). |
| +0x06 | 1 | `skew` | Track-to-track sector skew for head positioning. |
| +0x07 | 1 | `boot_option` | Boot action (0 = none, 1 = load, 2 = run, 3 = exec). |
| +0x08 | 1 | `low_sector` | Bits 0–5 (`DiscRecord_LowSector_Mask`): lowest sector number on a track. Bit 6 `DiscRecord_SequenceSides_Flag`: tracks are numbered 0..s−1 on side 0, then s..2s−1 on side 1 (see §1.1). Bit 7 `DiscRecord_DoubleStep_Flag`: double stepping. |
| +0x09 | 1 | `nzones` | Number of zones in the allocation map (low byte). |
| +0x0A | 2 | `zone_spare` | `hdr/FileCore` defines this as *"# bits in zones after 0 which are not map bits"* — i.e. the 32-bit zone header plus the trailing slack. That definition yields the extent formulas in §2.4 directly. The PRM instead describes it as the worst-case number of bits reservable for a fragment spanning in from the previous zone; that is a disc-wide upper bound, not a per-boundary constant (see §3.1). |
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
| +0x2C | 4 | `format_version` | Disc format version (`DiscRecord_BigDir_DiscVersion`). 0 = old/new directories, 1 = big directories. |
| +0x30 | 4 | `root_size` | Size of root directory in bytes (`DiscRecord_BigDir_RootDirSize`, big directories). |
| +0x34 | 8 | — | Reserved (`DiscRecord_BigDir_Reserved`). |

The record ends at `+0x3C`, i.e. it is **60 bytes** (`SzDiscRecSigSpace` in `hdr/FileCore`), not 64. Two other sizes appear in the source and are worth knowing: `SzDiscRecSig` = 32 (the pre-3.6 record, through `disc_name`) and `SzDiscRecSig2` = 52 (through `root_size`), the size published in the public header file. That 52-byte figure is a struct-definition size, not what's physically written to disc — the boot block itself stores the full 60-byte record (§2.2, §C.3). There is no "disc needs checking" flag at `+0x34`, and no on-disc "dirty" state anywhere in FileCore — `hdr/FileCore` and the Phase 2 Functional Specification (§7.5) both make `0x34`–`0x3B` reserved. (The only such flag in the source is `BufDirDirty`, an in-RAM directory-buffer writeback bit in `s/FileCore25`.)

### 2.2 The Boot Block (Hard Discs, and Multi-Zone Floppies)

On hard discs, and on new-map floppies with more than one zone (F format), the boot block occupies disc address `0xC00` to `0xDFF` (two 256-byte sectors, or one 512-byte sector). Single-zone floppies (E format, `nzones == 1`) have no boot block — their disc record lives only in the zone 0 map block at `0x000`+`0x04` (§2.1); the boot block area on such discs reads as all zero. On a multi-zone F-format floppy, by contrast, the boot block at `0xDC0` holds a valid disc record whose core fields match the copy embedded in the zone 0 map block.

Its layout is:

| Offset from 0xC00 | Size | Content |
|--------------------|------|---------|
| +0x000 → | — | Defect list, growing **upwards** (terminated by `0x200000xx`) |
| → +0x1BF | — | Hardware-dependent information, growing **downwards** (includes the 4-byte park position address `ParkDiscAdd` at `+0x1BC`) |
| +0x1C0 | 0x03C | Disc record (60 bytes, `0x1C0`–`0x1FB`) |
| +0x1FC | 1 | Non-ADFS partition format identifier and flags: bits 0–3 format id (1 ⇒ RISC iX), bits 4–7 flags (reserved, must be zero) |
| +0x1FD | 2 | Non-ADFS partition start cylinder (low byte, then high byte) |
| +0x1FF | 1 | Checksum |

The three bytes `+0x1FC`–`+0x1FE` are a **non-ADFS partition descriptor**, recording where a foreign partition (historically RISC iX) begins — not a FileCore flag byte plus padding.

Three authorities agree on this layout. The PRM (vol. 2, ch. 28, "The boot block") gives the table above. `hdr/FileCore` makes the disc record 60 bytes (`SzDiscRecSigSpace`), which lands exactly on `0x1C0`–`0x1FB`. And `s/Defns` defines `DefectListDiscAdd * &400+&800` (= `0xC00`), `SzDefectList * &200`, then `# SzDefectList-4-MaxStruc` of defect list, `ParkDiscAdd # 4`, and `DefectStruc # MaxStruc` with `MaxStruc * 64` — that 64-byte `DefectStruc` is a *space reservation* covering the record, the partition descriptor and the checksum together, not the size of the record itself. Its comment reads: *"The last 64 bytes describe the disc to FileCore. Any other bytes may be used as params for the low level drivers."*

**The boot block checksum is a single byte at `+0x1FF`** — the very last byte of the block — not a 16-bit value at `+0x1FE`. It is an ascending byte sum with carry rollover over the preceding bytes:

```c
uint8_t boot_block_check(const uint8_t *bb)   /* bb points at disc address 0xC00 */
{
    unsigned sum = 0;
    for (int i = 0; i < 0x1FF; i++) {
        sum += bb[i];
        if (sum > 255) sum = (sum + 1) & 255;   /* propagate carry */
    }
    return (uint8_t)(sum & 0xFF);
}
```

The PRM states it exactly: *"an 8 bit add with carry on each of the other bytes in the block, starting with value 0"* — so the sum runs over `0x000`–`0x1FE` inclusive and the result is stored at `0x1FF`. Verified against all three sample images that have a boot block: `adfs1600F.adf` (`0xBE`), `HDD_2025-10-25_riscos_a5000cfcard.dd` (`0x69`) and `HDD_CP30174E_AM7AX9W.dd` (`0xAD`). Note that FileCore itself contains no boot-block checksum code — the boot block is written by formatting software (ADFS/`HForm`) — so the PRM and the on-disc evidence, not the FileCore source, are the authorities here.

The defect list contains 32-bit disc addresses of bad sectors. Each entry gives the byte address of a defective sector. The list is terminated by a word of the form `0x200000xx` where `xx` is a checksum byte — the low byte of the sum of all preceding list bytes. (With no defects the list is just `0x20000000`.) For discs larger than 512 MB, a second defect list is appended, using sector addresses and terminated by `0x400000yy`. (PRM vol. 2, ch. 28, "The boot block"; PRM vol. 5a, ch. 110, "Defect lists".)

Confirmed on `HDD_CP30174E_AM7AX9W.dd`, the one sample image with a real defect: the list reads `0x00003000`, `0x20000030`, and the bytes of the single entry sum to `0x30`. That defect also appears in the zone map as a fragment ID 1 covering disc addresses `0x2000`–`0x3FFF`.

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

To resolve the indirect address, **apply the sharing offset within the ID-2 piece that contains the zone map** — the piece beginning at the map's own disc address (§2.4). Do *not* concatenate every ID-2 fragment on the disc and index into that.

The distinction matters because **fragment ID 2 is a reserved bookkeeping ID, not an ordinary object identifier**. Unlike a fragmented file's pieces, its occurrences are physically unrelated. On `adfs1600F.adf`, ID 2 appears once in zone 0's range (64 units covering the boot block at `0xC00`) and again, separately, in zone 2's range (160 units covering the double-copied map plus the root directory, `0xC6800`–`0xC8FFF`); concatenating both and indexing lands 4096 bytes short. Read ID 2 as "the reserved ID used for map, boot block and root-directory bookkeeping in whichever zone each happens to live". The general procedure in §3.2 applies to ordinary objects (ID ≥ 3).

### 2.4 The Zone Map (New Map Only)

The zone map is the allocation structure for new-map discs. It consists of `nzones` sectors, one per zone, stored consecutively starting at the disc address of zone `nzones / 2` (integer division). This places the map near the middle of the disc on multi-zone discs, reducing average seek distance. On a single-zone floppy, the map is at disc address `0x000`.

**Caveat — don't compute the map's disc address as `(nzones / 2) × (disc_size / nzones)`.** Zones are not equal-sized slices of `disc_size`. On a sample 4-zone, 1024-byte-sector, `zone_spare`=1600-bit F-format floppy (`adfs1600F.adf`), the naive formula gives `2 × (1638400/4) = 819200`, but the true map start (confirmed against `ZoneCheck`/`CrossCheck`, §A.1) is `813056` — six sectors earlier.

A zone's **real extent** — the number of allocation units it actually describes — follows directly from `hdr/FileCore`'s definition of `zone_spare` as the number of bits in a zone that are *not* map bits (§2.1):

```
zoneN_bits = sector_size × 8 − zone_spare
zone0_bits = sector_size × 8 − zone_spare − 480     # 480 = the 60-byte disc record copy
```

Multiply each by `bpmb` to get that zone's byte extent, then sum the extents of zones `0 .. nzones/2 − 1` to get the map's disc address.

Two traps are worth naming. The raw per-zone budgets quoted in §C.1 (`(sector_size − 64) × 8` for zone 0, `(sector_size − 4) × 8` for the rest) are the *maximum theoretical capacity* used to choose `bpmb` and `nzones` at format time, not the real extents of a formatted disc. And correcting them means subtracting `zone_spare − 32` from **both** — subtracting the full `zone_spare`, or applying the correction only to zones after 0, each gives a wrong answer.

These extents were verified by decoding the allocation bit stream of all four zones on the sample image: every zone's real content ends at the same bit offset, `sector_size × 8 − zone_spare + 32`, located by finding the terminating `1` bit of each zone's last fragment. For the sample disk (`sector_size`=1024, `zone_spare`=1600, `bpmb`=64): `zone0_bits`=6112 → 391168 bytes; `zoneN_bits`=6592 → 421888 bytes; map start = `391168 + 421888 = 813056` — exact match. (As a further consistency check, summing all four zones' extents this way gives 1656832 bytes, 18432 bytes more than `disc_size`=1638400; decoding zone 3's bit stream shows a fragment ID 1 — the defect list — of exactly 18432 bytes near the end of that zone, which accounts for the difference: the formatter pads the last zone with a defect-list reservation to burn off the excess between nominal zone capacity and the disc's true size.)

The same formula is confirmed on both sample hard discs. `HDD_2025-10-25_riscos_a5000cfcard.dd` (`sector_size`=512, `zone_spare`=48, `bpmb`=1024, `nzones`=124) gives `zone0_bits`=3568, `zoneN_bits`=4048 and a map start of `250496 × 1024 = 256507904` (`0xF4A0000`) — where a valid zone 0 map block is indeed found. `HDD_CP30174E_AM7AX9W.dd` (`sector_size`=512, `zone_spare`=32, `bpmb`=512, `nzones`=82) gives 3584/4064 and `0x5120000`, likewise correct.

Note that of the three multi-zone sample images, only `adfs1600F.adf` and the A5000 CF card actually *test* the `zone_spare − 32` correction. `HDD_CP30174E_AM7AX9W.dd` has `zone_spare` = 32, so the correction term is zero and that image cannot distinguish this formula from one that subtracts nothing at all.

The trailing `zone_spare − 32` bits of each zone's map sector are slack that describes no disc space. Formatters treat them differently and a decoder must tolerate both: `adfs1600F.adf` simply leaves them as zero bits, whereas `HDD_2025-10-25_riscos_a5000cfcard.dd` writes a terminated fragment ID 1 (defect) descriptor filling the slack in every one of its 124 zones.

For multi-zone floppies, prefer reading the disc record from the boot block (§2.2) over computing this address at all; if you must locate the map block directly, use the formula above and confirm the candidate sector with its `ZoneCheck`/`CrossCheck` (§A.1) before trusting it.

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
| +0xF6 | 1 | Level 3 fileserver partition sector, or zero (see caveat below) |
| +0xF7 | 5 | Zero, or the *even*-indexed characters (0, 2, 4, 6, 8) of the 10-character RISC OS disc name, interleaved with sector 1 |
| +0xFC | 3 | Total number of sectors on disc |
| +0xFF | 1 | Checksum of sector 0 |

**Sector 1 (free space lengths):**

| Offset | Size | Content |
|--------|------|---------|
| +0x00 | 82 × 3 | Length of each free extent (3 bytes each, in units of 256 bytes) |
| +0xF6 | 5 | Zero, or the *odd*-indexed characters (1, 3, 5, 7, 9) of the 10-character RISC OS disc name, interleaved with sector 0 |
| +0xFB | 2 | Disc identifier (random 16-bit value set at format time) |
| +0xFD | 1 | Boot option (set by `*OPT 4`) |
| +0xFE | 1 | Pointer to end of free space list: `3 × (number of free extents)` |
| +0xFF | 1 | Checksum of sector 1 |

The disc name is reconstructed as `name[0]=sector0[0]`, `name[1]=sector1[0]`, `name[2]=sector0[1]`, `name[3]=sector1[1]`, ... alternating through both 5-byte runs. Confirmed against `adfs640L.adl` and `adfs800D.adf`, whose free-space-map bytes reconstruct to `"00_05_Sun"` and `"00_06_Sun"` respectively (both padded with a trailing NUL rather than a space) — matching the same naming pattern found verbatim in `adfs800E.adf`'s extended disc-record `disc_name` field, `"00_07_Sun "`.

**Caveat on the Level 3 field:** FileCore does not know about it. `s/Defns` lays out sector 0 as `FreeStart # 82*3`, then `EndSpaceList` (a zero-size label), then `# 1 ;reserved`, then `OldName0 # 5` — so `+0xF6` is a **single reserved byte** and the name run begins at `+0xF7`, as tabulated above. (Both name fields are marked "RETRO DEFINITION", confirming the disc name was retrofitted into reserved space.)

The "Level 3 fileserver partition" reading comes from [mdfs.net](https://mdfs.net/Docs/Comp/Disk/Format/ADFS), which documents 8-bit ADFS rather than FileCore and gives the field as three bytes at `0F6`–`0F8` — overlapping the disc name run that the same document places at `0F7`–`0FB`. Real disc bytes rule that overlap out. Sector 1 has no room for the second L3 field some sources list either: its 5-byte name run, 2-byte disc identifier, boot option, end-of-list pointer and checksum fill `+0xF6`–`+0xFF` exactly (`5+2+1+1+1 = 10`). Treat `+0xF6` as one reserved byte; none of the sample images are Level 3 partitions, so all they confirm is that it reads zero on ordinary discs.

Files on old-map discs must be stored contiguously. If there is no single free extent large enough, RISC OS reports "Compaction required" or "Can't extend". The `*Compact` command defragments the disc.

Although the map's units are always 256 bytes, **space is allocated in whole sectors**, which on D format means 1024 bytes — four map units at a time. On `adfs800D.adf`, `hostfs/txt` (104 bytes) starts at map unit 12 and occupies units 12–15, with the next file starting at 16; on the 256-byte-sector `adfs640L.adl` the same file occupies a single unit. A tool that computes occupancy as `ceil(length / 256)` on a D disc will under-count and see phantom gaps.

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

When `idlen` > 15 (big map discs), the free chain link within each free fragment is still treated as a 15-bit value, even though the ID field is wider than 15 bits. Free fragments are never shorter than `idlen` + 1 bits. (Acorn FileCore Phase 1 Functional Specification, §3.2.)

This is confirmed by the RISC OS 5 source. `s/Defns` defines `MaxFreeLinkBits * 15`, and every routine that reads or writes a free-chain link (`s/FileCore33`, in around ten places) loads `DiscRecord_IdLen` and then clamps it:

```arm
        LDRB    R7, [R10,#ZoneHead+DiscRecord_IdLen]
        CMP     r7, #MaxFreeLinkBits
        MOVHI   r7, #MaxFreeLinkBits
```

so the link width is `min(idlen, 15)` regardless of how wide the ID field is. Fifteen bits addresses offsets up to 32,767, which still covers a 4096-byte sector (32,768 bits) with one bit to spare, so G format needs no change here.

**Zone spare bits, and where a zone's stream ends.** A descriptor's bit pattern can in principle straddle a zone boundary, continuing into the next zone's map sector, and `zone_spare` bounds how many bits that could take. It is a worst-case allowance, not a per-boundary constant: you cannot assume any given boundary has continuation bits, nor how many.

Two consequences for a decoder. First, **never unconditionally skip `zone_spare` bits at the start of a zone.** If the previous zone's last descriptor terminated normally, the next zone begins a brand-new descriptor at its very first allocation bit, and skipping sails straight past it — on `adfs1600F.adf` that loses the ID 2 fragment at the start of zone 2, which is the zone map and the root directory, and misplaces everything after it. Second, **a zone's real disc coverage ends at `zone_header_bits + zone_bits(zone)`** (§2.4), not at the end of the sector; the trailing `zone_spare − 32` bits are slack describing no disc space. Decoding blindly to `sector_size × 8` reads that slack as a descriptor and, because it has no terminating `1` bit, wrongly concludes that a fragment spanned.

Stopping dead at the extent is wrong too, because a zone's last real descriptor may legitimately overrun into the slack. On `HDD_2025-10-25_riscos_a5000cfcard.dd` the formatter fills the slack of all 124 zones with a terminated ID 1 descriptor, and in the final zone the trailing defect run starts before the extent and ends inside it.

> **The rule that decodes all four sample images correctly:** decode each zone to the end of its sector; if the final descriptor has no terminating `1` bit, discard it as slack and do **not** let it set `pending_span`. Descriptors at or beyond `zone_bits(zone)` describe no disc space.

No fragment actually spans a zone boundary in any sample image — across all 211 zones, no zone's stream runs out without a terminator. The `pending_span` path below therefore follows the specification rather than observed media, and should be treated as untested code.

**Pseudocode to decode all zones (state threads forward from one zone to the next):**

```
pending_span = false   # did the previous zone's last fragment run off the end?

for zone in 0 .. nzones - 1:
    header_bits = zone_header_size(zone) * 8   # 64*8 for zone 0, 4*8 for others
    bit_pos = header_bits
    zone_end = sector_size * 8
    # Real disc coverage stops here; bits beyond this are slack (2.4).
    extent_end = header_bits + zone_bits(zone)
    alloc_unit = 0                          # within this zone

    if pending_span:
        # Finish the previous zone's last fragment: these are real
        # continuation bits, but we don't know in advance how many —
        # scan until the terminating 1 bit turns up (bounded by
        # zone_spare bits; if none is found within that bound, the map
        # is corrupt). They belong to the PREVIOUS fragment, not a new one.
        span_start = bit_pos
        found = false
        while bit_pos < zone_end and (bit_pos - span_start) < zone_spare:
            b = read_bit(map_data, bit_pos)
            bit_pos += 1
            alloc_unit += 1
            if b == 1:
                found = true
                break
        extend_previous_fragment(alloc_unit)   # add these bits to the prior fragment's length
        pending_span = false
        # (if `found` is false here, the map is corrupt — no terminator
        # within the zone_spare allowance)

    while bit_pos < zone_end:
        # read fragment ID — a brand-new fragment starts here
        id = read_bits(map_data, bit_pos, idlen)
        frag_start = alloc_unit
        bit_pos += idlen
        alloc_unit += idlen

        # count the zero bits + terminating 1
        found_terminator = false
        while bit_pos < zone_end:
            b = read_bit(map_data, bit_pos)
            bit_pos += 1
            alloc_unit += 1
            if b == 1:
                found_terminator = true
                break

        frag_len = alloc_unit - frag_start

        if not found_terminator:
            # No terminator before the end of the SECTOR. On every sample
            # image this is slack padding at the tail of the map sector
            # (2.4) — discard it and start the next zone cleanly.
            if frag_start >= extent_end - header_bits:
                break                       # pure slack: not a real fragment
            # Otherwise the descriptor genuinely overran the sector, and its
            # remaining bits are at the start of the NEXT zone. Don't guess
            # how many; let the next zone's pass (above) find the real length.
            # NOTE: not exercised by any sample image - see above.
            record_fragment(zone, id, frag_start, frag_len)
            pending_span = (zone < nzones - 1)
            break

        record_fragment(zone, id, frag_start, frag_len)
```

### 3.2 Resolving an Indirect Disc Address

On new-map discs, files and directories are identified by a **System Internal Number (SIN)**, also called an indirect disc address. This is a 3-byte (24-bit) value:

- Bits 8–23: Fragment ID (the `idlen`-bit object identifier)
- Bits 0–7: Sharing offset (1–255, representing offsets 0–254 in sharing units; 0 means the object has its own fragment and is not shared)

The sharing unit is one sector (or `2^share_size` sectors on RISC OS 3.6+ discs).

**To resolve a SIN to a physical disc address:**

1. Extract the fragment ID and sharing offset from the SIN.
2. Walk all zones in order, and within each zone decode its *entire* bit stream from the start — collecting **every** descriptor that matches the ID, however many there are. A single zone can contain more than one matching fragment (separated by unrelated data); don't stop scanning a zone after the first match.

   **Exclude free fragments first.** Only the *last* free fragment in a zone's chain has an ID field of 0; every earlier one holds a link offset to the next (§3.1). Those link values occupy the same numeric range as real fragment IDs, so a free fragment whose link happens to equal the ID you are resolving will be silently collected as a bogus extent. Walk each zone's free chain from `FreeLink` first, mark those fragments as free, and skip them when matching. (No such collision occurs in the sample images — `HDD_CP30174E_AM7AX9W.dd` has 58 chained free fragments with 9 distinct link values and none collides — but nothing prevents one.)
3. Concatenate the fragments, in the order encountered — bit position ascending within a zone, then zone ascending — to form the disc object's physical extent(s).
4. If the sharing offset is non-zero, the object starts at byte `(sharing_offset - 1) * sharing_unit` within the disc object.

**To find the zone containing a fragment's first occurrence**, use `start_zone = fragment_id / ids_per_zone`, where `ids_per_zone = zone_bits / (idlen + 1)` — matching `IdsPerZone` in `s/FileCore33`, which computes `(sector_size × 8 − zone_spare) / (idlen + 1)`. This is more than a search hint: it inverts the allocation rule, since a new ID is taken from the range belonging to the zone the object is placed in (§4.1), and compaction never moves a fragment between zones (§B.5). It held exactly for all 12,015 objects across the sample images. Treat it as a reliable starting point rather than a validity test, and still scan for further pieces (see above).

**Fragment ID 0 in a directory entry means "no disc space", not "free space".** A zero-length file is recorded with a SIN whose fragment ID is 0 — `0x000001` in practice, sharing offset 1. Do not try to resolve it against the map, where ID 0 marks free space; just return zero bytes. There are 111 such entries on `HDD_2025-10-25_riscos_a5000cfcard.dd` and 5 on `HDD_CP30174E_AM7AX9W.dd`, all of length 0. The fragment may have additional pieces elsewhere — in the same zone (separated by unrelated data, the common case left behind by a file extension that couldn't be done in place; see §4.3 and Appendix B.2) as well as in subsequent zones. Fragment IDs are unique per disc object, not per fragment — see the note in §A.3 for what this means for repair tooling that walks the map.

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

**Directory tail layouts:**

`s/Defns` defines exactly two tail layouts for non-big directories — "Old Directory End" and "New Directory End". Both are read *backwards* from the end of the directory.

In **small-sector directories** (S, M, L — `0x500` bytes total), the tail starts at offset `0x4CB`:

- `0x00` end marker (1 byte)
- Directory name (10 bytes)
- Parent start sector (3 bytes)
- Directory title (19 bytes)
- Reserved (14 bytes, zero)
- End sequence number (1 byte, BCD)
- End validation `"Hugo"` (4 bytes)
- Check byte (1 byte — always zero on 8-bit ADFS, computed by 32-bit ADFS)

In **large-sector directories** (D, E and F — `0x800` bytes total), the tail starts at offset `0x7D7`:

- `0x00` end marker (1 byte)
- Reserved (2 bytes, zero)
- Parent disc address (3 bytes) — a **start sector** on D, an **indirect disc address (SIN)** on E and F
- Directory title (19 bytes)
- Directory name (10 bytes)
- End sequence number (1 byte)
- End validation `"Hugo"` or `"Nick"` (4 bytes — see §1.3)
- Check byte (1 byte)

D, E and F share one layout; only the interpretation of the parent field differs. (Source: Nick Reeves' E Format Design Document.)

The two layouts also differ in field *order*, not just size: the small-sector one places the name before the parent and title, whereas the large-sector one places the title before the name. That second point is a common misreading — `s/Defns` puts `NewDirTitle` before `NewDirName` for D, E and F alike. A directory whose name is exactly 10 characters makes the two readings distinguishable on disc; `$.!BootPSLCD` on `HDD_CP30174E_AM7AX9W.dd` holds, from tail offset `+0x06`:

```
'!BootPSLCD' 00 00 00 00 00 00 00 00 00 '!BootPSLCD'
```

Reading title(19) then name(10) gives a title of `"!BootPSLCD"` zero-padded to 19 bytes followed by the 10-byte name — consistent. Reading name(10) then title(19) would require a title field beginning with nine NUL bytes.

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

There is no fixed cap on the number of entries — the limit is the 4 MB maximum directory size. Each entry costs 28 bytes here, plus a 4-byte backup word and at least 4 bytes of name heap, so a big directory tops out near 116,000 entries with one-character names and proportionately fewer as names lengthen. (The often-quoted ~32,000 figure is `2^15 − 3`, the *disc-wide* object limit when `idlen` = 15, which is a different constraint.)

The **name heap** is a separate region within the directory where the variable-length name strings are packed. Each name is CR-terminated (`0x0D`) and padded with zero bytes to a 4-byte boundary.

**Backup directory entries** are stored between the name heap and the directory tail. Each backup entry is a single 4-byte word containing the indirect disc address of the corresponding object, to aid recovery of broken directories. `BigDirBackupMove` (`s/BigDirCode`) builds them by walking the entry table with a stride of `BigDirEntrySize` and copying each entry's `BigDirIndDiscAdd` word, and `BigDirFreeSpace` budgets 32 bytes per entry (28 for the entry plus 4 for its backup word).

There is a build-time alternative: under the `BigDirFullBackup` switch the backup area holds a complete duplicate of each 28-byte entry, costing 56 bytes per entry rather than 32. The 4-byte form described above is what the shipping RISC OS 5 source builds.

The header, entry and tail tables above are as defined in `s/Defns`, together with `BigDirMaxNameLen * 255`, `BigDirMinSize * &800` and `BigDirMaxSize * 4*1024*1024`.

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

6. **Read the file data**: The fragments, concatenated in the order found (bit position within a zone, then zone order — §3.2), contain the file data. Read `length` bytes starting from the sharing offset within the first fragment.

For **old-map discs**, step 5 is simpler: the directory entry contains a direct disc address (in 256-byte units for L/D format). The file is stored contiguously at that address.

---

## 4. Writing to the Filesystem

### 4.1 Creating a File

FileCore's allocation strategy is sophisticated, designed to minimise fragmentation. The general procedure for creating a file is:

1. **Choose a zone**: FileCore prefers the zone containing the parent directory, expanding outward. For whole-file saves (`SAVE`), it tries hard to find a single contiguous extent, compacting zones if necessary. For sequential writes (`OPENOUT`), it allocates a starting extent and may extend later.

2. **Allocate space in the zone map**: Find a free fragment large enough (or combine adjacent free fragments). Split the free fragment if it's larger than needed. Assign a new fragment ID — the smallest unused ID **within the chosen zone's own ID range**, which runs from `zone × ids_per_zone` upwards (`UnusedId` in `s/FileCore33`). IDs are therefore *not* allocated from one disc-wide pool: only zone 0's range starts at 0, and there IDs 0, 1 and 2 are reserved, so zone 0's first usable ID is 3.

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
2. **Allocate a new fragment**: If in-place extension is not possible, allocate a new fragment — in the same zone if there's room, otherwise a different one — with the same fragment ID. The file now has multiple fragments — it is fragmented.
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

- **4-byte SIN field**: The indirect disc address field is widened from 3 to 4 bytes, allowing `idlen` to go beyond the small map's 15-bit ceiling (`MaxIdLenSmlMap`) to as much as 21 bits, and so past the 32,765-object limit that ceiling implies.

- **Different magic numbers**: `"SBPr"` and `"oven"` replace `"Hugo"` and `"Nick"`.

### 5.2 The Name Heap

The heap's layout is given in §3.4. One behavioural point belongs here: it may contain gaps left by deleted entries. FileCore does not compact the heap on every deletion, though it may do so when the directory is rewritten — so `BigDirNamesSize` is an allocation figure, not a count of live name bytes.

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

Every directory (old, new, and big) has a check byte at the very end of the directory. **Old/new directories and big directories use different routines** — `TestDirCheckByte` and `TestBigDirCheckByte` respectively — which cover different regions. The algorithm described first below is the old/new one; see "Big directories" at the end of this section for the other. Both differ from the zone checksum: they use a rotate-and-XOR scheme, and a **crucial detail is that they operate on whole 32-bit words, not on individual bytes**, except for the non-word-aligned leftovers at each region boundary. A naive byte-at-a-time implementation over "everything except the check byte" will not reproduce the real on-disk value.

The real algorithm (`s/FileCore25`, `TestDirCheckByte`) processes the directory in four passes:

1. **Whole words from the start of the directory** (byte 0 — the header, including the master sequence number and `"Hugo"`/`"Nick"`/`"SBPr"` start name, *is* included) up to the last word-aligned address at or before the end of the real entries.
2. **Any remaining non-word-aligned bytes** up to the exact end of the real entries (the position of the terminating NUL byte after the last entry — everything past this and before the tail is unused padding and is *not* processed).
3. **The tail's leading end-marker byte (`0x00`) is skipped entirely** — processing resumes at the byte immediately after it, consuming any non-word-aligned bytes there first to reach word alignment.
4. **Whole words through the tail**, stopping *before* the final 32-bit word of the directory. This excludes not just the single check byte but the entire last word — e.g. on a small S/M/L directory (`0x500` bytes) that's the check byte *and* the last three bytes of the `"Hugo"` validation string (`ugo`).

```
; From Nick Reeves' E Format Design Document / s/FileCore25, TestDirCheckByte
; R5 = directory start; R0 = end of real entries (word-aligned boundary in R1);
; R7 = tail start + 1 (skips the end-marker byte); final word (last 4 bytes,
; containing the check byte) is excluded via R1 = dir_end - 4.
checksum = 0
for each 32-bit word, then each leftover byte, from dir_start up to end_of_entries:
    checksum = word_or_byte XOR (checksum rotated right by 13 bits within 32 bits)
# skip the tail's end-marker byte (offset 0 of the tail) entirely
for each leftover byte (to reach word alignment), then each 32-bit word,
from (tail_start + 1) up to (dir_end - 4):
    checksum = word_or_byte XOR (checksum rotated right by 13 bits within 32 bits)
# fold to 8 bits — these two steps are SEQUENTIAL; the second consumes the
# result of the first (source: EOR R2,R2,R2,LSR #16 / EOR R2,R2,R2,LSR #8)
checksum = checksum XOR (checksum >> 16)
checksum = checksum XOR (checksum >> 8)
checksum = checksum & 0xFF
```

**The fold must be done as two sequential steps.** Collapsing them into the single expression `(checksum XOR (checksum >> 16) XOR (checksum >> 8)) & 0xFF` is *not* equivalent — it silently drops the `>> 24` term, so bits 24–31 never reach the result and the algorithm reproduces essentially no real check bytes. Expanded, the correct fold is `checksum XOR (checksum >> 16) XOR (checksum >> 8) XOR (checksum >> 24)`.

The "end of real entries" (`R0` in the source) is `5 + 26 × n`, where `n` is the number of used entries — i.e. the offset of the terminating NUL name byte after the last entry.

Verified against the real check bytes of **every directory on all six sample images — 4,863 directories, no mismatches**: `adfs640L.adl` (small S/M/L directory), `adfs800D.adf` (large old-map directory), `adfs800E.adf` and `adfs1600F.adf` (new-map floppies), and the 3,504 and 1,355 directories of `HDD_2025-10-25_riscos_a5000cfcard.dd` and `HDD_CP30174E_AM7AX9W.dd`. A simple byte-wise pass over the whole buffer reproduces none of them.

**Big directories** use `TestBigDirCheckByte` (`s/BigDirCode`), which is simpler — two regions, not four passes, and it *includes* the header:

1. **One run of whole words from the very start of the directory**, covering
   `BigDirHeaderSize + ((BigDirNameLen + 4) AND NOT 3) + (28 × BigDirEntries) + BigDirNamesSize`
   bytes — that is, the header, the padded directory name, all entries, and the whole name heap. Everything is word-aligned by construction, so there are no leftover bytes.
2. **The tail**: one word at `BigDirEndName` (`"oven"`), then the three individual bytes `BigDirEndMasSeq` and the two reserved bytes, stopping before the check byte itself.

The accumulator step is the same `checksum = word_or_byte XOR (checksum ROR 13)`, and the fold is the same two sequential shifts. Note the consequence: the **backup entry area between the name heap and the tail is not covered by the check byte at all**, so corruption there will not be detected by this test.

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

- **Fragment ID uniqueness**: A fragment ID is unique *per disc object*, not per fragment. The same ID (≥ 3) may appear as several separate fragments, and **not only one per zone** — two fragments with the same ID can sit in the same zone separated by unrelated data, which is exactly what a file extension that couldn't be done in place leaves behind (§4.3), and what Appendix B.2 lists as a normal target for compaction. So a repair tool must not treat multiple same-ID fragments in one zone as suspicious, and must not use "at most one fragment per ID per zone" as a scanning shortcut: resolving a SIN correctly means decoding a zone's *entire* bit stream and collecting every match, in order.

  The `start_zone = fragment_id / ids_per_zone` relation from §3.2, by contrast, *is* structural rather than arbitrary: IDs come from the target zone's own range (§4.1), and it held exactly for all 12,015 objects across the sample images. A mismatch is worth logging as a strong hint of damage — but confirm it by scanning rather than acting on the arithmetic alone, and be certain you have excluded free-chain fragments first (below), since misreading one as an object is the easiest way to manufacture a false mismatch.

- **Total allocation unit count**: Do **not** test that the total number of allocation units across all zones equals `disc_size / bpmb`. It normally exceeds it, and a repair tool applying that test as an equality reports every healthy multi-zone disc as corrupt.

  The reason is structural rather than accidental: the map is a whole number of zones, each of fixed bit capacity, so the last zone's coverage generally **runs off the far edge of the disc** — the PRM describes the map as spilling past the end. That spill-over is real map bits describing space that does not exist, so the formatter reserves it as a **fragment ID 1 (defect) run beginning at exactly `disc_size`**, ensuring it can never be allocated. §2.4 describes the same mechanism from the map's side.

  The correct invariant is therefore: every allocation unit below `disc_size / bpmb` must be covered exactly once, and everything at or above it must be ID 1. Measured on the samples — `adfs1600F.adf` overshoots by 18,432 bytes, `HDD_CP30174E_AM7AX9W.dd` by 237,568, and `HDD_2025-10-25_riscos_a5000cfcard.dd` by 1,024,000, each with a matching ID 1 run starting precisely at `disc_size`. (On the last of these, allow also for the per-zone `zone_spare` slack descriptors of §2.4: its trailing ID 1 run is 1,040,384 bytes = 1,024,000 + 16,384 of slack.)

- **Cross-references with directories**: Every fragment ID ≥ 3 in the map should correspond to a SIN referenced by some directory entry. Fragments with no directory reference are "lost" objects — allocated space that is wasted, and which a repair tool may free.

  This one holds cleanly in practice: across the four new-map sample images there are **no** unreferenced objects at all (8,230 and 3,781 map objects on the two hard discs, every one reachable from a directory). That makes it a usable check — but only after two traps are avoided, both of which manufacture phantom orphans:

  - **Exclude free-chain fragments before comparing.** A free fragment's ID field holds a link, not an ID (§3.1), and those link values land in the same numeric range as real IDs. Failing to exclude them adds 8 phantom "objects" on `HDD_CP30174E_AM7AX9W.dd` alone — which, being unreferenced, a naive tool would then free.
  - **Fragment ID 0 is not an object.** Zero-length files reference ID 0 (§3.2); it is not a lost object and there is nothing to free.

### A.4 Boot Block Checks

The boot block has its own checksum in its last byte, at offset `+0x1FF` (§2.2). The defect list has a check byte embedded in its terminator word. If these fail, the disc record may be untrustworthy. A repair strategy is to use the copy of the disc record stored in zone 0's map block (at disc address `map_start + 0x04`) as a fallback, since the map block has its own independent `ZoneCheck`.

**The two copies are not interchangeable, and the zone 0 copy is often the better one.** On all three sample images with a boot block, the boot-block copy has the entire extended region (`+0x14`–`+0x3F`) zeroed, while the zone 0 copy carries the real `disc_id`, `disc_name` and `disc_type`. A tool that reads the disc name from the boot block gets nothing; it must read zone 0. The two can also disagree outright on core fields — on `HDD_CP30174E_AM7AX9W.dd`, `boot_option` is 0 in the boot block and 2 in the zone 0 copy, and both copies pass their own checksums, so integrity checks alone cannot say which is authoritative.

### A.5 Sequence Number Mismatch

Both old/new and big directories store the master sequence number in both the header and the tail. FileCore increments both atomically during writes. A mismatch indicates an interrupted write. The standard repair is to set both to the higher value and recompute the check byte.

For old/new directories, the sequence number is a single byte in BCD, and therefore wraps at 99 (`0x99`), not at 255. Across the 4,859 directories of the two sample hard discs, 70 and 78 distinct sequence values occur, with maxima of `0x91` and `0x99`, and not one value on either disc has a nibble greater than 9. For big directories, it is also a single byte (`StartMasSeq` at offset +0x00 and `BigDirEndMasSeq` in the tail).

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

FileCore's compaction operates one zone at a time — the routine is literally `CompactZone`, taking a zone number in R0 (`s/FileCore32`). It does not move data between zones (that would change the fragment's zone membership, which affects how the fragment is found during SIN resolution, and would break the ID-to-zone relation of §3.2). However, the `*Compact` command iterates over all zones, compacting each in turn.

A file that spans multiple zones has one fragment per zone. Compaction within each zone can improve the contiguity of that zone's fragment but cannot merge fragments across zone boundaries.

### B.6 Constraints

Several constraints limit what compaction can do:

- **Minimum fragment size**: A fragment must be at least `idlen + 1` bits wide in the map — the `idlen`-bit ID field plus a terminating `1` bit. Because a descriptor's bit width *equals* its allocation-unit count (§3.1), this corresponds to `idlen + 1` **allocation units**, not one: with the usual `idlen` = 15, the floor is 16 allocation units. (§C.4 works through the consequences for choosing `bpmb`.) Confirmed on the samples — across all four new-map images the smallest fragment of any kind is exactly 16 allocation units.

- **Granularity**: Data must be physically allocated in whole sectors. If the allocation unit (`bpmb`) is smaller than the sector size, the granularity is the sector size. Moves must be aligned to granularity boundaries.

- **Shared objects**: Fragments containing shared objects (a directory and its small sub-files) are more complex to move, as the sharing offsets in directory entries would need updating. FileCore avoids moving shared fragments during automatic compaction.

- **System object (ID 2)**: The boot block, zone map, and root directory live in fragment ID 2. This cannot be moved by normal compaction — `s/FileCore32` tests `id <= 2` (`RSBS LR, R8, #2 ; C=1 <=> id<=2`) and keeps the reserved IDs out of the move machinery.

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
| `density` | 2 (double) — 4 (quad) for a 1.6 MB F floppy | 0 (hard disc) |
| `idlen` | 15 | 15 |
| `log2_bpmb` | 7 (128 bytes) — **not** 10; see §C.4 | depends on disc size (10 and 9 on the sample hard discs) |
| `nzones` | 1 | depends on disc size (124 and 82 on the samples) |
| `zone_spare` | irrelevant (no zone boundary exists on a single-zone disc; real formatters may still write a nonzero value here — e.g. 1312 on the sample `adfs800E.adf` image) | small: 48 and 32 on the two sample hard discs |
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

**The `zone_spare` field** specifies the *maximum* number of bits at the start of a zone's allocation area that could be needed as cross-zone continuation bits, sized for the worst case at format time — it is not a fixed amount consumed at every boundary (see §3.1). In practice `zone_spare` is often set so that the zone boundaries don't fall awkwardly mid-fragment. (PRM vol. 2, ch. 28, "The zone".)

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

  (On hard discs, and multi-zone floppies such as F format:)
  Disc address 0xC00:
  +-- Boot block                                         --+
  |   [defect list][disc record][partition descriptor][checksum] |
  +--                                                    --+
```

For a single-zone floppy (nzones=1), the map block is at disc address `0x000`, and the root directory follows it immediately. The entire system object (ID 2) is one contiguous fragment covering the map + root directory. There is no boot block on a single-zone floppy (§2.2), so there is nothing else to account for.

For a multi-zone disc (hard disc, or a multi-zone floppy such as F format), the map blocks start at disc address `zone_disc_address(nzones/2)` and the root directory follows after the last map block — but this is **not** the same fragment as the boot block. The boot block at `0xC00` and the map+root-directory region are physically far apart (the map is deliberately relocated toward the middle of the disc for seek-time reasons — see §2.4), so they cannot be one contiguous extent. Both are still tagged with fragment ID 2 (it is a reserved ID, not a unique-per-object one — see the note in §2.3), but they show up as two independent pieces, each recorded in whichever zone's logical address range it physically falls into. Confirmed on `adfs1600F.adf`: the boot block sits in zone 0's logical range (a 64-unit ID-2 piece at disc address `0x000`–`0xFFF`), while the map and root directory sit entirely within zone 2's logical range (a separate 160-unit ID-2 piece at `0xC6800`–`0xC8FFF`), with zone 1 in between containing no ID-2 data at all.

### C.3 Step-by-Step: Initialising a New-Map Image

#### Step 1: Zero-fill the image

Create a file of `disc_size` bytes, filled with zeros.

#### Step 2: Write the boot block (hard discs, and multi-zone floppies)

At disc address `0xC00`:

1. Write an empty defect list: a single word `0x20000000 | checkbyte` at offset `+0x000`. With no defects, the checkbyte is computed over an empty list, which gives `0x20000000`.
2. Write the disc record at offset `+0x1C0` (60 bytes — that is all the boot block has room for; see §2.2).
3. Write the non-ADFS partition descriptor at `+0x1FC`–`+0x1FE` (§2.2) — zero for an image with no foreign partition.
4. Compute and write the boot block checksum — a single byte at `+0x1FF` (§2.2).

#### Step 3: Initialise the zone map

For each zone `z` from 0 to `nzones - 1`:

a. Compute the disc address of this zone's map block. For a single-zone disc this is `0x000`. For multi-zone discs, use the zone address formula from §2.4.

b. Write the 4-byte zone header:
   - `ZoneCheck`: will be computed last.
   - `FreeLink`: bit offset to the first free fragment (computed below).
   - `CrossCheck`: set so that all zones' CrossCheck bytes XOR to `0xFF`.

c. For zone 0 only: write the 60-byte disc record copy at offset `+0x04`, immediately after the 4-byte zone header — 64 bytes total before the allocation bit stream begins.

d. Write the allocation bit stream. For a single-zone disc, the stream contains exactly two fragments:
   - **Fragment ID 2 (system object)**: Covers the map sectors + root directory (+ boot block, if any — see below). Set the `idlen`-bit ID field to `2`, followed by enough `0` bits and a terminating `1` to cover the required number of allocation units.
   - **Fragment ID 0 (free space)**: Covers the rest of the zone. Set the `idlen`-bit ID field to `0` (end of free chain), followed by `0` bits and a terminating `1` covering the remaining allocation units.

   For multi-zone discs, the boot block and the map+root-directory region are physically far apart and cannot be one fragment (§C.2) — write **two separate** ID-2 descriptors, each in whichever zone's logical range its data physically falls into: one covering just the boot block (in zone 0's range, since the boot block sits at the very start of the disc), and one covering the map sectors + root directory (in the zone whose logical range contains the relocated map address, typically zone `nzones/2`). Every other zone contains a single free fragment covering the entire zone — but only after any bits genuinely left over from the *previous* zone's last fragment overrunning the boundary, which is not always `zone_spare` bits and is sometimes none at all (§3.1). Do not assume a fixed `zone_spare`-bit skip applies at every zone start.

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
- `log2_bpmb` = 7, bpmb = 128 (one allocation unit = 128 bytes = 1/8 sector) — matches the value actually found on a real 800K E-format disc (`adfs800E.adf`); see the note below on why `log2_bpmb` = 10 doesn't work here.
- `zone_spare` = 1312 (`0x520`), the value found on `adfs800E.adf`. It has no functional effect on a single-zone disc — with `nzones` = 1 there is no zone boundary for it to reserve bits across — but do not assume it is therefore zero.
- `root_dir` = `0x000203` (515 decimal: fragment ID 2, sharing offset 3)

Layout:
- Disc address `0x000`: Zone 0 map block, primary copy (1024 bytes = 1 sector). Header (4 bytes) + disc record (60 bytes) + allocation bit stream (960 bytes = 7680 bits).
- Disc address `0x400`: Zone 0 map block, backup copy (1024 bytes). The new map is double-copied (Nick Reeves, "New Disc File Structure for RISC OS").
- Disc addresses `0x800`–`0xFFF`: Root directory (2048 bytes = 2 sectors).
- Disc address `0x1000` onwards: Free space.

The system object (fragment ID 2) is 32 allocation units (4096 bytes: 2 map sectors + 2 root-directory sectors). It contains the primary map block, the backup map block, and the root directory.

**Why not `log2_bpmb` = 10 (bpmb = 1 sector), as the "typical" row in §C.1 suggests?** Because the fragment descriptor's *total bit width equals its allocation-unit count* (§3.1) — a descriptor can never be shorter than `idlen + 1` bits (15 + 1 = 16 here), since the `idlen`-bit ID field alone needs that much room before the terminating `1` bit. With `bpmb` = 1024 bytes, the system object's real content (4096 bytes) is only **4** allocation units — less than the 16-unit floor forced by `idlen`. The descriptor's bit width cannot be made to equal 4 without truncating the ID field, so a naïve "content bytes ÷ bpmb" calculation that ignores this floor is self-contradictory. `bpmb` = 128 avoids the problem entirely, since 4096/128 = 32 units comfortably clears the 16-unit floor; this is presumably why real formatting software doesn't use the §C.1 "typical" value for small floppies. (This is also the value actually found on `adfs800E.adf`; that disc already has files on it, so its real fragment layout differs from this fresh-format walkthrough, but its fragment ID 2 is independently confirmed to be exactly 32 allocation units — matching this worked example.)

Map bit stream (7680 bits):
- Fragment ID 2 (system object): 15-bit ID = `0x0002`, then padding and `1` bit. It covers 32 allocation units, so the descriptor is 32 bits total: `15 + 16 + 1 = 32` bits: `[15-bit id=2][16 zero bits][1]`.
- Fragment ID 0 (free, chain terminator): 15-bit ID = `0x0000`, then enough bits to cover the remaining 6368 allocation units (6400 total units on the disc, minus 32 for the system object). That's `15 + 6352 + 1 = 6368` bits: `[15-bit id=0][6352 zero bits][1]`.
- The rest of the 7680 bits are unused (past the end of the disc's allocation units: 32 + 6368 = 6400 = 819200/128).

`FreeLink` calculation:

- The allocation bits start after the 64-byte zone 0 header, at bit 512 (= 64 × 8).
- The system fragment is 32 bits long, so the free fragment starts at bit 512 + 32 = 544.
- `FreeLink` is measured from byte 1 of the sector (i.e. from bit 8), so: FreeLink = 544 − 8 = 536.
- Bit 15 is always set: FreeLink = 536 | 0x8000 = `0x8218`.

### C.5 Old Map Formatting (L, D)

For old-map discs, the initialisation is simpler:

1. **Write the free space map** at sectors 0 and 1. Sector 0 holds 82 three-byte start addresses; sector 1 holds the corresponding 82 three-byte lengths (both in 256-byte units). On a fresh disc, entry 0 describes the entire free area (everything except the map and root directory), and entries 1–81 are zero.

2. **Write the root directory** at its fixed disc address (e.g. `0x200` for L-format). Initialise with the header (`"Hugo"`), an empty entry list (NUL first byte), and the tail with check byte.

3. **Write the disc name** split across the two map sectors, exactly as described in §2.5: the *even*-indexed characters (0, 2, 4, 6, 8) go at sector 0 offset `+0xF7`, and the *odd*-indexed characters (1, 3, 5, 7, 9) at sector 1 offset `+0xF6`. Both runs are 5 bytes. Note that `+0xF9` is *not* the start of either run — on sector 0 it falls inside the total-sectors field.

Old-map floppies (S, M, L, D) have no boot block — the old map has no disc record concept at all (§2.1). Hard discs with old maps do have a boot block at `0xC00` with a disc record and defect list, initialised as in step 2 of §C.3. Note this is distinct from new-map floppies: multi-zone new-map floppies (F format) *do* have a boot block, per §2.2.

---

## Glossary

**Allocation unit** — The smallest unit of disc space managed by the zone map. One allocation unit = 2^log2_bpmb bytes (the "bytes per map bit" value from the disc record). Every fragment occupies a whole number of allocation units.

**Big directory** — Variable-length directory format used by E+, F+, and G. Identified by the `"SBPr"` / `"oven"` magic strings. Entry count is bounded by the 4 MB maximum directory size rather than by a fixed limit; filenames may be up to 255 characters, stored in a name heap.

**Boot block** — A 512-byte structure at disc address `0xC00` on hard discs, and on new-map floppies with more than one zone (F format). Contains the defect list, a 60-byte copy of the disc record (at offset `+0x1C0`), a non-ADFS partition descriptor at `+0x1FC`–`+0x1FE`, and a one-byte checksum at `+0x1FF`. Single-zone new-map floppies (E format) and old-map floppies (S, M, L, D) have no boot block.

**bpmb** (`log2_bpmb`) — Disc record field: log₂ of the number of bytes per map bit (i.e. per allocation unit). Determines the map granularity.

**Check byte** — A single-byte checksum protecting a directory. Stored in the directory tail. Computed by a rotate-and-XOR algorithm over the directory contents.

**CrossCheck** — Byte at offset `+0x03` in each zone header. The XOR of all zones' CrossCheck bytes must equal `0xFF`. Detects zone-level corruption or a zone belonging to a different disc.

**Defect list** — A list of known bad-sector addresses stored in the boot block (hard discs only). Terminated by a word with bits 29–31 set and a check byte in bits 0–7. Fragment ID 1 in the zone map marks defective regions.

**Disc address** — A byte offset from the start of the disc image. All FileCore addresses are byte offsets, not sector numbers (even on old-map discs where the free space map uses 256-byte units).

**Disc record** — A 60-byte structure (the extended form, RISC OS 3.6+) describing the disc's geometry and map parameters; the earlier 32- and 52-byte forms are prefixes of the same layout (§2.1), with the remaining extended fields reading as zero on pre-3.6 media. Found in the boot block (hard discs) or at the start of zone 0's map block (new-map discs). Key fields include `log2_sector_size`, `sectors_per_track`, `heads`, `idlen`, `log2_bpmb`, `nzones`, `root_dir`, and `disc_size`.

**Exec address** — The 32-bit execution address in a directory entry. For date-stamped files (top 12 bits of load address = `0xFFF`), the low 8 bits of the exec address hold the low byte of the 40-bit centisecond timestamp.

**Filetype** — A 12-bit value encoded in bits 19–8 of the load address when the file is date-stamped (top 12 bits = `0xFFF`). Identifies the file's type (e.g. `0xFFD` = Data, `0xFFF` = Text).

**Fragment** — A contiguous run of allocation units on disc sharing the same fragment ID. A file or directory is composed of one or more fragments. A fragment ID is unique per disc object, not per fragment, so an object's fragments may be scattered — separated by unrelated data within a single zone just as readily as split across different zones (fragmentation).

**Fragment ID** — An `idlen`-bit identifier in the zone map bit stream. ID 0 = free space, 1 = defect, 2 = system (map and boot area), ≥ 3 = a file or directory. The fragment ID plus a sharing offset forms a SIN.

**FreeLink** — 16-bit field at offset `+0x01` in each zone header. Points to the first free fragment in the zone (as a bit offset from byte 1 of the zone). Bit 15 is always set. A value of `0x8000` means no free space in this zone.

**Hugo** — The 4-byte magic string `"Hugo"` at the start of old-format directories (S, M, L, D, E, F). Named after Hugo Tyson. Repeated in the tail as a validation word.

**idlen** — Disc record field: the number of bits used for fragment IDs in the zone map. Determines the maximum number of objects on disc (2^idlen − 3 usable IDs). Typically 15 for floppies and old-format hard discs. The Acorn Phase 1 spec (Ursula) raised the limit to 19 for big map discs; RISC OS 5 (FileCore 3.75, 2017) raised it further to 21.

**LFAU** — Largest Fragment Allocation Unit. Defined as max(sector_size, bpmb). This is the minimum granularity at which disc space is actually allocated; files smaller than one LFAU share a fragment with their parent directory.

**Load address** — The 32-bit load address in a directory entry. When the top 12 bits are `0xFFF`, the entry is date-stamped: bits 19–8 hold the filetype and bits 7–0 hold the high byte of the 40-bit timestamp. Otherwise it is an actual memory load address (Acorn legacy).

**Name heap** — A region within a big directory containing CR-terminated (`0x0D`) filenames padded with zero bytes to 4-byte boundaries. Directory entries reference names by offset into this heap. May contain gaps left by deleted entries.

**New directory** — Directory format used by D, E, and F. 2048 bytes (`0x800`), up to 77 entries, identified by `"Nick"` (or `"Hugo"`) in the tail. D uses sector addresses for entry disc addresses; E and F use SINs.

**New map** — Zone-based allocation map used by E, F, E+, F+, and G. The disc is divided into `nzones` zones, each one sector long, containing a packed bit stream of fragment-ID/length pairs.

**Nick** — The 4-byte magic string `"Nick"` found in the header and tail of new-format directories (D, E, F). Named after Nick Reeves, designer of the E format. On D, E and F discs either `"Hugo"` or `"Nick"` may appear, so validation must accept both (§1.3). S/M/L directories on 8-bit systems use `"Hugo"` only. Big directories (E+, F+, G) use neither — they use `"SBPr"` and `"oven"`.

**nzones** — Disc record field: the number of zones in the new map. Each zone is one sector. Zone 0 also holds the disc record.

**Old directory** — Directory format used by S, M, and L. 1280 bytes (`0x500`), up to 47 entries, identified by `"Hugo"` only (no `"Nick"` in the tail on 8-bit systems). File attributes are encoded in bit 7 of each filename byte.

**Old map** — Flat free-space table used by S, M, L, and D. Two 256-byte sectors at disc addresses `0x000` and `0x100`, containing 82 three-byte (start, length) pairs in 256-byte units.

**SBPr / oven** — The 4-byte magic strings `"SBPr"` (header, offset +0x04) and `"oven"` (tail) in big directories (E+, F+, G), analogous to `"Hugo"` / `"Nick"` in older formats. Defined as the single 8-byte literal `"SBProven"` in the source (`s/BigDirCode` line 71), a reference to `sproven`, the Acorn login of Simon Proven, who designed and implemented big directory support.

**Sequence number** — A byte stored in both the header and tail of every directory. Incremented on each directory update. A mismatch between header and tail indicates the directory was not completely written (e.g. power loss).

**share_size** — Disc record field (RISC OS 3.6+): log₂ of the sharing unit in sectors. Files smaller than one LFAU share a fragment with their parent directory; the sharing offset within the SIN locates the file's data within the shared fragment.

**Sharing offset** — The byte offset within a shared fragment that locates a small file's data. Encoded in the low bits of the SIN (below the fragment ID). Only relevant for files smaller than one LFAU.

**SIN** — System Internal Number. A disc address used in new-map directory entries. The top `idlen` bits (after shifting) give the fragment ID; the remaining bits give the sharing offset. Resolved by walking the zone maps to find all fragments with that ID, then applying the offset.

**Zone** — One sector of the new map. Each zone manages a region of disc and contains a 4-byte header (`ZoneCheck`, `FreeLink`, `CrossCheck`) followed by a packed bit stream. Zone 0 additionally holds the 60-byte disc record immediately after its header — 64 bytes total before the allocation bit stream begins (§2.4).

**ZoneCheck** — Checksum byte at offset `+0x00` in each zone header. Computed by summing all 32-bit words in the sector with carry, subtracting the existing check byte, and folding to 8 bits via XOR (see §A.1).

**zone_spare** — Disc record field: per `hdr/FileCore`'s definition, the number of bits in each zone after zone 0 that are *not* map bits — the 4-byte zone header plus a **trailing** slack region at the *end* of the zone's allocation bit stream (§2.4's extent formulas follow directly from this reading). Do not read this as bits reserved at the *start* of a zone for a fragment spanning in from the previous one — that framing (found in the PRM) describes a disc-wide worst-case upper bound on cross-zone continuation, not a per-boundary constant, and unconditionally skipping bits at a zone's start on that basis loses real fragments. See §3.1.

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
