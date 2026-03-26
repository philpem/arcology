# ARMlock: Complete Technical Reference

*Reverse-engineered from ARMlock version 1.04 (12 Jul 1994)*

## 1. Overview

ARMlock is a disc-level security system for Acorn RISC OS machines. It operates
by intercepting FileCore's low-level disc operations and redirecting reads/writes
to the root directory, making the real filesystem contents invisible unless the
ARMlock module is loaded and the correct password has been entered.

**Key facts:**
- RISC OS module, SWI chunk &47880
- Written in C (Norcroft/Acorn C) using SharedCLibrary
- Contains an embedded inner module: "ARMlock_cunning" (SWI chunk &40540)
- Targets new-map FileCore discs (E/F format) on hard drives
- Password-protected with a custom 64-bit hash
- Maximum password length: 11 characters, case-insensitive

## 2. Module Architecture

### 2.1 Module Header

| Offset | Value | Field |
|--------|-------|-------|
| +0x00 | 0 | Start offset (no application entry) |
| +0x04 | 0x0A8 | Initialisation code |
| +0x08 | 0x0F0 | Finalisation code |
| +0x0C | 0 | Service call handler (none — uses vector hooks instead) |
| +0x10 | 0x02C | Title: "ARMlock" |
| +0x14 | 0x034 | Help: "ARMlock\t\t1.04 (12 Jul 1994)" |
| +0x18 | 0 | No *Commands |
| +0x1C | &47880 | SWI chunk base |
| +0x20 | 0x128 | SWI handler |
| +0x24 | 0x050 | SWI decode table |

### 2.2 SWI Interface

| SWI | Number | Purpose |
|-----|--------|---------|
| ARMlock_ValidatePassword | &47880 | Check password against stored hash |
| ARMlock_ReturnInfo | &47881 | Return 4-word info block (installation-patched) |
| ARMlock_EnterUserMode | &47882 | Drop to user mode for callbacks |
| ARMlock_ChangePassword | &47883 | Change password (requires old password) |
| ARMlock_ReturnSerialNumber | &47884 | Return serial number (from BootUp.Options) |

### 2.3 Embedded Inner Module (ARMlock_cunning)

The inner module is embedded within the outer module's code at offset **0x2A4**.
It has a minimal RISC OS module header with no init, finalise, or service call
handlers — it exists purely to register a SWI dispatch point:

| Offset | Value | Field |
|--------|-------|-------|
| +0x00 | 0 | Start (none) |
| +0x04 | 0 | Init (none) |
| +0x08 | 0 | Final (none) |
| +0x0C | 0 | Service (none) |
| +0x10 | 0x02C | Title: "ARMlock_cunning" |
| +0x14 | 0x02C | Help: same |
| +0x1C | &40540 | SWI chunk base |
| +0x20 | 0x03C | SWI handler → absolute offset 0x2E0 in outer module |

The inner module's executable code lives entirely within the outer module's
address space. Its SWI handler at 0x2E0 (`cunning_entry`) is the function
that patches FileCore's internal jump table. By registering as a separate
RISC OS module, the patched jump table entries point to a legitimate SWI
dispatch address rather than a suspicious raw memory address.

The installation sequence (in `install_hooks`, 0x1E8) is:
1. `OS_Module 10` — insert the header block at 0x2A4 as a module from memory
2. `OS_Module 3` — claim workspace
3. `OS_Module 4, "ARMlock_cunning"` — start it (no-op since init=0)

### 2.4 Memory Layout

The module uses 0x3A98 bytes of code/data. Key regions:

| Range | Purpose |
|-------|---------|
| 0x000–0x0A7 | Module header, strings, SWI table |
| 0x0A8–0x174 | SharedCLib module init/final/SWI wrappers |
| 0x178–0x1E4 | SVC mode callback dispatcher |
| 0x1E8–0x258 | `install_hooks` — core FileCore patching |
| 0x25C–0x2D0 | Variables, module pointers, strings |
| 0x2A4–0x2DF | Embedded ARMlock_cunning module header |
| 0x2E0–0x358 | `cunning_entry` — FileCore jump table patcher |
| 0x35C–0x5D0 | `discop_hook` — disc operation interceptor + ZoneCheck |
| 0x5E0–0x87C | Hook tables, vector handlers, enable/disable functions |
| 0x880–0x9EC | C callback handler and service dispatch |
| 0xAF4–0xBA8 | `c_module_init` — boot sequence orchestrator |
| 0xBBC–0xF6C | `disc_setup` — disc discovery and geometry parsing |
| 0xF74–0x100C | Strings: filesystem names, paths |
| 0x1014–0x1198 | Disc swap, hook installation helpers |
| 0x119C–0x1268 | `match_path` — pathname comparison utility |
| 0x1270–0x12F0 | ByteV claim/release management |
| 0x12F4–0x1390 | Hourglass helpers |
| 0x1394–0x1604 | `cmos_restore` — reads BootUp.Options, restores CMOS RAM |
| 0x1608–0x1654 | `alloc_pathbuf` — allocates path buffer |
| 0x1658–0x1704 | `open_messages` — MessageTrans file init |
| 0x1708–0x177C | `display_message` — message lookup and print |
| 0x1780–0x1834 | `disable_hform` — prevents disc formatting |
| 0x1834–0x188C | `disable_rmclear` — prevents module killing |
| 0x1890–0x1A88 | Error generators (12 different error types) |
| 0x1A90–0x1C00 | DiscOp handlers (verify, read/write access control) |
| 0x1C20–0x2074 | SectorDiscOp and MiscOp handlers |
| 0x2078–0x2104 | SWI dispatch (C level) |
| 0x2108–0x2194 | `swi_validate_password` |
| 0x2198–0x2204 | `swi_enter_usermode` |
| 0x2208–0x228C | `swi_change_password` |
| 0x2290–0x22E0 | `swi_return_info` — returns 4 installation-patched words |
| 0x22F4–0x2358 | `password_hash` — the hash algorithm |
| 0x235C–0x2448 | `read_options_hash` / `write_options_hash` |
| 0x2450–0x3A94 | SharedCLib stubs, relocation table |

## 3. Boot Sequence

### 3.1 How ARMlock Loads

The stripped root directory at the normal FileCore root location contains a
single entry: `!Boot` — a RISC OS module (filetype &FFA). When the machine
boots with boot_option=2 (Run), FileCore loads and executes this file, which
is the ARMlock module itself.

### 3.2 Module Initialisation (0x0A8 → 0xAF4 → 0xBBC)

1. **SharedCLib init**: Standard module initialisation via `_clib_initialisemodule`
2. **Find FileCore** (0x208): `XOS_Module 18` to look up `"FileCore%Base"`, locates FileCore's module workspace and internal jump table
3. **Insert inner module** (0x224): `XOS_Module 10` to register the embedded ARMlock_cunning header as a module
4. **Claim workspace** (0x234): `XOS_Module 3`
5. **Start cunning** (0x244): `XOS_Module 4, "ARMlock_cunning"` — this causes its SWI handler to be callable, which `disc_setup` uses to patch FileCore

### 3.3 Disc Discovery (disc_setup, 0xBBC)

On first run, ARMlock discovers which disc it's installed on:

1. Reads `FileSwitch$CurrentFilingSystem` to determine the active FS
2. Uses `OS_FSControl 41` to canonicalise the boot device path
3. Sets system variable `ARMlock$Disc` = `"<fs>::<disc>"`
4. Iterates through ADFS drives 4–7, using `OS_FSControl 37` to find the physical disc
5. Calls `match_path` (0x119C) to match the path prefix against the ARMlock directory
6. Reads the **disc record** from the mounted disc and extracts geometry:
   - `log2_sector_size` (byte 0), `nzones` (byte 9), `sectors_per_track` (byte 1)
   - `heads` (byte 2), `zone_spare` (signed halfword at 0x0A), `log2_bpmb` (byte 5)
7. Computes the disc address of the zone map start (see §10)
8. Calls `install_hooks` (0x1E8) with `base = map_start` and `stride = nzones × sector_size`
9. Sets access permissions on `$.ARMlock` to `"wr/r"`

### 3.4 FileCore Hook Installation (install_hooks, 0x1E8)

This is the core of ARMlock. It computes three addresses and patches FileCore's
internal dispatch tables.

**Variables set:**
- `[0x264]` = `base` (primary zone map disc address)
- `[0x268]` = `base + stride` (backup zone map disc address)
- `[0x260]` = `base + 2 × stride` (root directory disc address)

**How FileCore is patched:**

`cunning_entry` (0x2E0), called via its SWI handler, receives a pointer to
FileCore's internal handler block. It saves the original handler addresses
and returns a replacement block pointing to ARMlock's hooks. Because the
replacement pointers are registered SWI addresses, FileCore doesn't detect
anything suspicious.

## 4. Disc Operation Interception

### 4.1 The Three Hooks

ARMlock intercepts three FileCore internal entry points:

| Hook | Address | Intercepts |
|------|---------|------------|
| `filecore_discop_hook` | 0x66C | FileCore_DiscOp (verify, read, write) |
| `filecore_miscop_hook` | 0x6B4 | FileCore_MiscOp (mount, catalogue) |
| `filecore_sectorop_hook` | 0x754 | FileCore_SectorDiscOp |

Each hook has a global enable flag at 0x668. When disabled (0), the hook
passes through to the original FileCore handler.

### 4.2 Root Directory Remapping (discop_hook, 0x35C)

This is the heart of ARMlock's concealment. Every FileCore disc operation
passes through this function, which checks the disc address (R2/a3) against
three stored ranges:

```c
void discop_hook(int reason, int flags, uint32_t disc_addr, ...) {
    if (!remap_active) goto original_handler;

    if (disc_addr == primary_map_addr)   goto handle_map_access(1);
    if (disc_addr == backup_map_addr)    goto handle_map_access(2);

    if (disc_addr >= root_dir_addr && disc_addr < root_dir_addr + 0x800) {
        // REMAP: redirect to disc address 0x400
        disc_addr -= (root_dir_addr & ~0xE0000000);  // strip reason flags
        disc_addr += 0x400;
        call_original_handler(disc_addr);
        disc_addr -= 0x400;
        disc_addr += (root_dir_addr & ~0xE0000000);
        return;
    }

    goto original_handler;
}
```

**Effect**: FileCore believes it is reading/writing the root directory at its
computed location within the system object (fragment 2), but the actual I/O
hits disc address 0x400 — where the real, complete root directory is stored.

### 4.3 Zone Map Tampering (handle_map_access)

When ARMlock intercepts access to zone 0's map sector (either copy), it
modifies two bytes on the fly:

**Byte 0x00 (ZoneCheck):**
- On **write** to the primary copy: XOR byte 0 with 0xFF (deliberate invalidation)
- On **read** from the primary copy: XOR byte 0 with 0xFF (reverse it)
- The backup copy retains a valid checksum for the tampered data

**Byte 0x0B (boot_option, disc record offset +0x07):**
- On **write**: `stored = ((boot_option & 3) << 2) + 2`
- On **read**: `recovered = stored >> 2`

This ensures that:
1. Without ARMlock, the primary map fails its checksum → "Bad free space map"
2. With ARMlock, both copies appear valid and boot_option reads correctly
3. The on-disc boot_option is mangled, preventing auto-boot without ARMlock

### 4.4 Access Control (DiscOp/SectorDiscOp/MiscOp handlers)

Beyond remapping, ARMlock implements access control. The SectorDiscOp handler
(0x1C20) dispatches by reason code and performs password validation on first
access. The MiscOp handler (0x1E18) intercepts mount, catalogue, and format
operations, requiring the password to have been entered for catalogue access
and blocking format entirely.

## 5. Password System

### 5.1 Hash Algorithm (password_hash, 0x22F4)

```c
void password_hash(const char *password, uint32_t result[2]) {
    uint32_t v3 = 0x89ABCDEF;   // seed 1
    uint32_t v4 = 0x01234567;   // seed 2

    for each byte c in password:
        c = toupper(c);
        uint32_t rotated = ((int32_t)v3 >> 13) | (v3 << 19);  // ASR 13 | LSL 19
        v3 = rotated + c;
        v4 ^= v3;

    result[0] = v3;
    result[1] = v4;
}
```

**Properties:**
- Case-insensitive (toupper before hashing)
- Fixed seeds, no salt
- 64-bit output but v4 never feeds back into v3
- Effective security: ~32 bits (brute-force v3 only, confirm with v4)
- ASR 13 (arithmetic, not logical) creates lossy collisions when bit 31 set
- ASR sign-extension mask: 0xFFF80000 (top 13 bits filled from sign)
- Max password length: 11 characters

### 5.2 File Storage

ARMlock uses two Options files with different contents:

**`<ARMlock$Dir>.Options`** (full options file, read by SWI handlers):

| Offset | Size | Content | Example value |
|--------|------|---------|---------------|
| 0x00 | 4 | Password hash v3 (LE) | 0x8E2B21C5 |
| 0x04 | 4 | Password hash v4 (LE) | 0x177C3437 |
| 0x08 | 4 | Serial number (LE) | 0x000002EF (751) |
| 0x0C | 1 | Config flag (desktop app only — unused by module) | 0x00 |
| 0x0D | 1 | CMOS protection: 0=enabled, non-zero=disabled | 0x01 |
| 0x0E | 1 | Management flag | 0x01 |
| 0x0F+ | var | NUL-terminated application path strings | (see below) |

The first 8 bytes (password hash) are read by `read_options_hash` (0x235C)
via a loop of 8 `_kernel_osbget` calls.

Bytes 0x08–0x0D are a copy of `BootUp.Options` (see below).

The application path strings list directories that the desktop management
application controls access to, e.g.:
- `Share::FILES.ARMlock.Pub`
- `$.Programs.!WP_DTP.Selection.!Publisher`
- `$.Programs.!WP_DTP.Selection.!WordWorks`

**`<ARMlock$Disc>.$.ARMlock.BootUp.Options`** (boot copy, read at startup):

| Offset | Size | Content | Example value |
|--------|------|---------|---------------|
| 0x00 | 4 | Serial number (LE) | 0x000002EF (751) |
| 0x04 | 1 | Config flag (state+0x38, unused by module) | 0x00 |
| 0x05 | 1 | CMOS protection flag (state+0x3C) | 0x01 |

**Note:** The password hash is NOT in BootUp.Options. BootUp.Options contains
only the serial number and two config bytes. The password hash is only in the
full `<ARMlock$Dir>.Options` file.

### 5.3 CMOS Protection Flag (BootUp.Options byte 5)

This flag at state+0x3C controls two things:

**When 0 (enabled):**
- `cmos_restore` (0x1394) reads `BootUp.CMOS` and writes CMOS RAM values back,
  restoring ARMlock's tampered boot configuration on every boot
- `claim_bytev_if_needed` (0x1270) claims ByteV to intercept OS_Byte 162
  (read CMOS), preventing the user from discovering the real boot_option

**When non-zero (disabled):**
- CMOS restore is skipped entirely
- ByteV is not claimed — CMOS reads pass through normally

On the examined disc this is 0x01 (disabled). When BootUp.Options is missing,
the default is also 1 (disabled — safe fallback).

### 5.4 Config Flag (BootUp.Options byte 4)

Stored at state+0x38. Written during boot but **never read by the module**.
This value is consumed only by the ARMlock desktop management application,
which reads it from byte 0x0C of the full `<ARMlock$Dir>.Options` file.
Its exact purpose cannot be determined from the boot module alone.

### 5.5 Serial Number

The serial number is a 32-bit value stored at:
- BootUp.Options bytes 0–3 (loaded at boot into state+0x48 = data+0x2AB0)
- `<ARMlock$Dir>.Options` bytes 8–11 (copy)

It is returned by `ARMlock_ReturnSerialNumber` (SWI &47884, handler at 0x20A8).
In a clean/template binary the location is zero — it is patched directly into
the module file on disc during ARMlock installation.

### 5.6 Password Validation (swi_validate_password, 0x2108)

```c
int swi_validate_password(const char *password) {
    if (read_options_hash() != 0) return error;

    uint32_t computed[2];
    password_hash(password, computed);

    if (stored_hash[0] == computed[0] && stored_hash[1] == computed[1]) {
        state->locked = 0;
        claim_bytev();
        disable_rmclear();
        enable_hooks();
        return 0;
    }
    return error_wrongpass();
}
```

### 5.7 Password Change (swi_change_password, 0x2208)

```c
int swi_change_password(const char *old_pass, const char *new_pass) {
    if (read_options_hash() != 0) return error;

    uint32_t old_hash[2];
    password_hash(old_pass, old_hash);

    if (stored_hash[0] != old_hash[0] || stored_hash[1] != old_hash[1])
        return error_wrongpass();

    password_hash(new_pass, new_hash);
    write_options_hash(new_hash);
    return 0;
}
```

## 6. Protection Mechanisms

### 6.1 Anti-Tampering

1. **NoHForm module**: Loaded from `$.ARMlock.BootUp.NoHForm` on ADFS, prevents formatting
2. **Alias$RMClear**: Set to empty, disabling `*RMClear`
3. **ByteV intercept**: Claims OS_Byte 162 to block CMOS reads (when CMOS protection enabled)
4. **FSCV intercept**: Filters `*Cat` through ARMlock's access control
5. **Zone map invalidation**: Primary ZoneCheck deliberately broken

### 6.2 CMOS Backup/Restore (cmos_restore, 0x1394)

When CMOS protection is enabled (flag = 0), ARMlock reads
`$.ARMlock.BootUp.CMOS` and restores CMOS RAM settings. The file contains
240 bytes (10 banks × 24 bytes), read via `fgetc`. Each byte is compared
against the current CMOS value (OS_Byte 161) and only written (OS_Byte 162)
if different. Locations 0x80 and 0x81 are skipped (protected).

## 7. On-Disc Layout

### 7.1 Disc Structure When ARMlock Is Installed

```
Disc address 0x000:
  [Unmodified — whatever was here before]

Disc address 0x400:
  [REAL root directory — 0x800 bytes]
  Contains the full directory listing

Disc address 0xC00:
  [Boot block — disc record at +0x1C0]
  boot_option = 0x00 (deliberately zeroed by ARMlock installer)

Disc address [map_start]:
  [Primary zone map — nzones × sector_size bytes]
  Zone 0, byte 0x00: ZoneCheck XORed with 0xFF (invalid)
  Zone 0, byte 0x0B: boot_option encoded as ((real & 3) << 2) + 2

Disc address [map_start + stride]:
  [Backup zone map]
  Zone 0, byte 0x00: Valid ZoneCheck (for tampered data)
  Zone 0, byte 0x0B: Same encoded boot_option

Disc address [map_start + 2 × stride]:
  [STRIPPED root directory — 0x800 bytes]
  Contains only !Boot (the ARMlock module) plus ghost data
  At +0x400 within this directory: "ARMlock installed\0" signature
```

### 7.2 Files on Disc

```
$                           (real root, at 0x400)
├── !Boot                   (ARMlock module — the decoy at [root_dir])
├── !Firmware
├── Apps
├── ARMlock
│   ├── BootUp
│   │   ├── CMOS            (240 bytes: CMOS RAM backup)
│   │   ├── Messages        (MessageTrans error strings)
│   │   ├── NoHForm         (module to prevent formatting)
│   │   └── Options         (6 bytes: serial + config flags)
│   └── Options             (8+ bytes: password hash + serial + config + app paths)
├── PC
├── Printing
├── Programs
├── Public
├── Utilities
└── VP
```

## 8. Detection

### 8.1 Detecting ARMlock on a Disc Image

```python
def detect_armlock(image):
    # 1. Parse disc record from boot block
    rec = read_disc_record(image, offset=0xDC0)

    # 2. Reject old-map discs (ARMlock only targets new-map)
    if rec.idlen == 0:
        return False

    # 3. Compute addresses
    sector_size = 1 << rec.log2_sector_size
    bpmb = 1 << rec.log2_bpmb
    zone0_aus = (sector_size - 64) * 8
    zoneN_aus = (sector_size - 4) * 8
    half = rec.nzones // 2

    if half == 0:
        map_start = 0
    else:
        map_start = (zone0_aus + (half - 1) * zoneN_aus) * bpmb

    stride = rec.nzones * sector_size
    signature_addr = map_start + 2 * stride + 0x400

    # 4. Check for signature
    if signature_addr + 17 > len(image):
        return False

    return image[signature_addr:signature_addr+17] == b"ARMlock installed"
```

### 8.2 Additional Indicators

- Boot block boot_option (0xDC7) is 0x00 despite the disc being bootable
- Primary zone 0 checksum fails but backup is valid
- Root directory at normal location has only one entry (!Boot, filetype &FFA)
- Zone map byte 0x0B has encoded value (e.g. 0x0A for real boot_option 2)
- A valid "Hugo"/"Nick" directory exists at disc address 0x400
- !Boot module title string is "ARMlock"

## 9. Removal

### 9.1 Complete Removal Procedure

```python
def remove_armlock(image):
    rec = read_disc_record(image, offset=0xDC0)
    addrs = compute_addresses(rec)  # §10

    # Step 1: Fix zone map (both copies)
    for map_addr in [addrs.primary, addrs.backup]:
        # Decode boot_option
        image[map_addr + 0x0B] = image[map_addr + 0x0B] >> 2

        # Recompute ZoneCheck
        image[map_addr] = compute_zone_check(image[map_addr:map_addr+sector_size])

    # Step 2: Restore root directory
    image[addrs.root_dir:addrs.root_dir + 0x800] = image[0x400:0x400 + 0x800]

    # Step 3: Clean up stashed copy
    image[0x400:0x400 + 0x800] = b'\x00' * 0x800

    # Step 4: Do NOT modify the boot block — it has its own 16-bit checksum
    # at +0x1FE. The boot_option=0 there won't cause runtime problems;
    # FileCore uses the zone map disc record copy at runtime.

    return image
```

### 9.2 ZoneCheck Algorithm

Models the ARM `ADCS` instruction: 32-bit accumulator with a separate 1-bit
carry flag. **Not** a 33-bit integer — the carry must be tracked separately.

```python
def compute_zone_check(sector_data):
    acc = 0      # 32-bit accumulator
    carry = 0    # 1-bit carry flag

    for i in range(len(sector_data) - 4, -1, -4):
        word = struct.unpack_from("<I", sector_data, i)[0]
        total = acc + word + carry          # ADCS
        acc = total & 0xFFFFFFFF
        carry = 1 if total > 0xFFFFFFFF else 0

    s = (acc - sector_data[0]) & 0xFFFFFFFF  # subtract existing check byte
    s = s ^ (s >> 16)                        # fold 32 → 16
    s = s ^ (s >> 8)                         # fold 16 → 8
    return s & 0xFF
```

### 9.3 Post-Removal Notes

After removal:
- FileCore reads the root directory directly from its normal location
- Both zone map checksums are valid
- All files are visible without the ARMlock module
- The `$.ARMlock` directory and its contents remain on disc but are inert
- Boot block boot_option is 0 (no auto-boot) — correcting this requires
  recomputing the boot block's 16-bit checksum at offset +0x1FE

## 10. Address Computation Quick Reference

All addresses are derived from four disc record fields:

```
sector_size  = 1 << log2_sector_size        (typically 512)
bpmb         = 1 << log2_bpmb               (typically 512)
nzones       = nzones_lo | (nzones_hi << 8) (e.g. 103)

half         = nzones // 2

zone0_bits   = (sector_size - 64) * 8
zoneN_bits   = (sector_size - 4) * 8

map_start    = (zone0_bits + (half - 1) * zoneN_bits) * bpmb
stride       = nzones * sector_size

primary_map  = map_start
backup_map   = map_start + stride
root_dir     = map_start + 2 * stride
signature    = root_dir + 0x400
```

**Important**: `map_start` is the disc address of zone `nzones/2`, which is
where the zone map is physically stored (near the middle of the disc for seek
optimisation). Fragment 2 (the system object) starts at this address, not at
disc address 0. The root directory's sharing offset within fragment 2 is
`2 × stride` bytes — past two complete copies of the zone map.

## 11. Password Hash Quick Reference

```
Format:   $armlock$<v3_hex>$<v4_hex>
Seeds:    v3 = 0x89ABCDEF, v4 = 0x01234567
Per-char: rotated = (v3 ASR 13) | (v3 LSL 19)
          v3 = rotated + toupper(c)
          v4 ^= v3
ASR mask: 0xFFF80000 when bit 31 set (top 13 bits sign-extended)
```

Storage in Options file: first 8 bytes, LE: `[v3_b0..v3_b3] [v4_b0..v4_b3]`

To crack: brute-force v3 only (32-bit search per candidate); recompute v4
only on v3 match. Dictionary + rules covers realistic passwords instantly.
