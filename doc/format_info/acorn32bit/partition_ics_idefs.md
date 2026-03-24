# IDEFS Partition Table and Boot Block Format Specification

> **Product:** Baildon Electronics / Ian Copestake Software (ICS) IDEFS v3.15,
> "Wizzo" variant for IOMD-based hardware (Acorn A4, A5000).
> Module date: 05 Mar 1996. SWI chunk &041FC0, filing system number &31.
>
> There are several unrelated "IDEFS" filing systems for RISC OS (e.g. the
> Simtec/Yellowstone variants, and the built-in IDE support in later RISC OS
> versions). This document describes **only** the ICS/Baildon Electronics
> version. The on-disc partition format is specific to this implementation
> and is not necessarily compatible with other IDEFS variants.

## Overview

IDEFS (IDE Filing System) is a RISC OS FileCore-based filing system for
Acorn A4 and A5000 computers. It interfaces with the 82C710 Super I/O
controller that maps PC-compatible IDE registers into ARM address space.
Each physical IDE drive contains a partition table at sector 0 and a
FileCore boot block at sector 6 (byte offset &C00). All multi-byte values
are little-endian.

---

## Partition Table (Sector 0, byte offset &000)

The partition table occupies the first 512 bytes of the physical drive.

### Layout

| Offset | Size | Description |
|--------|------|-------------|
| &000 | up to 504 bytes | Array of 8-byte partition entries |
| &1F8 | 4 bytes | Total disc capacity in sectors (little-endian uint32) |
| &1FC | 4 bytes | Checksum (little-endian uint32) |

### Partition Entry Format (8 bytes each)

Each entry consists of two little-endian 32-bit words:

| Offset | Size | Field |
|--------|------|-------|
| +0 | 4 | Start sector (absolute sector number on the physical drive) |
| +4 | 4 | Partition size in sectors |

Interpretation of the size word:
- **Positive**: valid partition entry
- **Zero**: end-of-table marker (stop parsing)
- **Negative** (bit 31 set): unused/deleted slot (skip and continue)

The maximum number of RISC OS partitions supported by IDEFS is 4.
Entries are parsed sequentially; the first 4 valid entries are used.

### Checksum Algorithm

The checksum is computed as follows:

1. Start with the seed value `0x50617274` (ASCII "Part", little-endian `74 72 61 50`)
2. Add each of the first 508 bytes (offsets &000 to &1FB) individually as unsigned 8-bit values
3. The resulting 32-bit sum (naturally truncated to 32 bits) must equal the uint32 stored at offset &1FC

```c
int validate_partition_table(const uint8_t *sector) {
    uint32_t sum = 0x50617274;  /* "Part" */
    for (int i = 0; i < 508; i++)
        sum += sector[i];
    uint32_t stored = sector[508] | (sector[509] << 8)
                    | (sector[510] << 16) | (sector[511] << 24);
    return sum == stored;
}
```

---

## FileCore Boot Block (Offset &C00 from partition start)

Each partition has its own boot block at byte offset &C00 (sector 6) from
the start of that partition. For partition 0 starting at physical sector 0
this happens to be physical sector 6, but for subsequent partitions the
boot block is at `partition_start_sector + 6`.

The boot block is a standard 512-byte sector containing the FileCore disc
record and IDEFS-specific protection information.

### Layout

| Offset in sector | Offset from partition start | Size | Description |
|------------------|---------------------------|------|-------------|
|------------------|-------------|------|-------------|
| &000 | &C00 | 416 | Reserved / boot code area |
| &1A7 | &DA7 | 1 | Protection flags byte |
| &1A8 | &DA8 | 4 | Password hash word 1 (lo) |
| &1AC | &DAC | 4 | Password hash word 2 (hi) |
| &1C0 | &DC0 | 48 | FileCore disc record |
| &1FF | &DFF | 1 | Boot block checksum |

### Protection Flags Byte (offset &1A7)

| Bits | Meaning |
|------|---------|
| 1:0 | Protection level: 0=none, 1=RW(password required), 2=RO(password required), 3=NO access |
| 2 | Temporary flag used during identify operations (internal) |
| 7:3 | Reserved |

### FileCore Disc Record (offset &1C0)

This is the standard FileCore disc record format:

| Offset | Size | Field |
|--------|------|-------|
| &1C0 | 1 | log2(sector size) — typically 9 for 512-byte sectors |
| &1C1 | 1 | Sectors per track |
| &1C2 | 1 | Heads (number of heads) |
| &1C3 | 1 | Density |
| &1C4 | 1 | ID field length / fragment ID length |
| &1C5 | 1 | log2(bytes per map bit) |
| &1C6 | 1 | Skew |
| &1C7 | 1 | Boot option |
| &1C8 | 1 | Low zones |
| &1C9 | 1 | Number of zones / total heads (used for size computation) |
| &1CA | 2 | Zone spare |
| &1CC | 4 | Root directory disc address |
| &1D0 | 4 | Disc size (bytes) |
| &1D6 | 10 | Disc name (padded with zeros) |

### Boot Block Checksum (offset &1FF)

An additive checksum over bytes &1FE down to &000 of the boot sector,
computed with 8-bit carry propagation:

```c
void compute_boot_checksum(uint8_t *sector) {
    uint32_t sum = 0;
    for (int i = 510; i >= 0; i--) {
        sum += sector[i];
        /* Propagate carry: keep only low 8 bits, carry the rest */
        sum = (sum & 0xFF) + (sum >> 8);
    }
    sector[511] = sum & 0xFF;
}
```

The code computes this by walking backwards from byte &1FE to &000 using
`LDRB R2,[R0,#-1]!` / `ADC R1,R1,R2` / shift-mask for carry folding.

---

## Password Hash Algorithm

Passwords are hashed into two 32-bit words using a shift-XOR algorithm
with the constant key `0x01810284`. Maximum password length is 10 characters.

### Algorithm

```c
#include <stdint.h>

static uint32_t ror32(uint32_t val, unsigned n) {
    n &= 31;
    return (val >> n) | (val << (32 - n));
}

void idefs_hash_password(const char *password, uint32_t *hash_lo, uint32_t *hash_hi) {
    const uint32_t key = 0x01810284;
    uint32_t lo = 0;
    uint32_t hi = 0;

    /* Skip leading spaces */
    while (*password == ' ')
        password++;

    /* Process up to 10 characters, stop at space or control char */
    for (int i = 0; i < 10 && (uint8_t)*password > 0x20; i++) {
        uint32_t ch = ((uint8_t)*password++ - 0x2A) & 0xFF;

        uint32_t top6 = lo & 0xFC000000;   /* extract top 6 bits of lo */
        hi = top6 ^ ror32(hi, 26);         /* rotate hi right 26, XOR in top6 */
        hi ^= key;
        lo = ch ^ (lo << 6);               /* shift lo left 6, XOR in char */
        lo ^= key;
    }

    *hash_lo = lo;
    *hash_hi = hi;
}
```

### Storage

The two hash words are stored at boot sector offsets &1A8 (lo) and &1AC (hi).
When protection is disabled (level 0), these bytes are typically zero or stale
from a previous password.

### Verification

To verify a password, hash the candidate and compare both words against the
stored values. There is no salt — identical passwords always produce identical
hashes.

---

## Example Partition Table (Hex Dump of Sector 0)

This is a 4-partition ~1.9GB drive:

```
00000000: 00 00 00 00  00 98 0F 00  00 98 0F 00  00 98 0F 00
00000010: 00 30 1F 00  00 98 0F 00  00 C8 2E 00  E0 47 0E 00
00000020: 00 00 00 00  00 00 00 00  00 00 00 00  00 00 00 00
   ... (all zeros from &020 to &1F7) ...
000001F0: 00 00 00 00  00 00 00 00  E0 0F 3D 00  B6 78 61 50
```

### Decoded Entries

| # | Offset | Start Sector | Size (sectors) | Size (bytes) | Start (bytes) |
|---|--------|-------------|----------------|--------------|---------------|
| 0 | &000 | 0x00000000 | 0x000F9800 | ~512 MB | 0 |
| 1 | &008 | 0x000F9800 | 0x000F9800 | ~512 MB | 512 MB |
| 2 | &010 | 0x001F3000 | 0x000F9800 | ~512 MB | 1024 MB |
| 3 | &018 | 0x002EC800 | 0x000E47E0 | ~464 MB | 1536 MB |
| 4 | &020 | 0x00000000 | 0x00000000 | (end marker) | — |

Total capacity at &1F8: 0x003D0FE0 sectors = 1,987,MB

### Checksum Verification

Seed: `0x50617274`

Non-zero bytes in &000–&1FB:
```
  Byte &004: 0x98  &005: 0x0F  &008: 0x98  &009: 0x0F
  &00C: 0x98  &00D: 0x0F  &010: 0x30  &011: 0x1F
  &014: 0x98  &015: 0x0F  &018: 0xC8  &019: 0x2E
  &01C: 0xE0  &01D: 0x47  &01E: 0x0E
  &1F8: 0xE0  &1F9: 0x0F  &1FA: 0x3D
```

Sum of non-zero bytes: 0x98+0x0F + 0x98+0x0F + 0x98+0x0F + 0x30+0x1F
  + 0x98+0x0F + 0xC8+0x2E + 0xE0+0x47+0x0E + 0xE0+0x0F+0x3D = 0x0642

Checksum: 0x50617274 + 0x0642 = **0x506178B6**

Stored at &1FC: `B6 78 61 50` (little-endian) = 0x506178B6 ✓

---

## Example Boot Block (Partition 0, sector 6)

From the same drive, partition 0 starts at sector 0 so its boot block is
at physical disc offset &C00 (sector 6). For partition 1 starting at sector
0xF9800, the boot block would be at physical byte offset 0xF9800×512 + &C00.

```
   ... (mostly zeros from &000 to &DA9) ...
   &DA7: 00                    (protection flags: none)
   &DA8: 00 00 00 00           (password hash lo: empty)
   &DAC: FF FF FF FF           (password hash hi: 0xFFFFFFFF)
   ...
   &DC0: 09                    log2(sector size) = 9 → 512 bytes
   &DC1: 3F                    sectors per track = 63
   &DC2: 10                    heads = 16
   &DC3: 00
   &DC4: 0F                    ID length
   &DC5: 0A                    log2(bytes per map bit) = 10
   ...
   &DC8: 01 7C 30 00           (map/zone info)
   &DCC: F9 02 00 00
   &DD0: 00 E0 8B 1E           disc size = 0x1E8BE000 = ~513 MB
   ...
   &DFF: 69                    boot block checksum
```

---

## Notes for Implementation

1. Sector size is always 512 bytes for IDEFS.
2. The partition table is always in sector 0 of the physical drive.
3. Each partition has its own boot block at byte offset &C00 (sector 6) from
   the start of that partition — i.e. at physical sector
   `partition_start_sector + 6`. For partition 0 at the start of the drive
   this is physical sector 6; for other partitions it will be elsewhere.
4. A drive with no partition table (invalid checksum) is treated as a
   single whole-drive partition.
5. The partition start sectors in the table are absolute physical sectors
   from the beginning of the drive, not relative to anything.
6. When decoding, you should display both the partition table and the
   boot block for each partition (at partition_start + 6 sectors).
