# ARMlock Module: Annotated Function Map

## Source file conventions

- Module is compiled C (Norcroft/Acorn C) targeting SharedCLibrary
- APCS-R calling convention: a1-a4 (R0-R3), v1-v6 (R4-R9), sl (R10), fp (R11), ip (R12), sp (R13), lr (R14)
- SharedCLib static data accessed via `[sl, #-0x218]` (module workspace base)
- All C functions have standard prologue: `MOV ip,sp; STMFD sp!,{...,fp,ip,lr,pc}; SUB fp,ip,#4`
- Two static data base pointers used:
  - **state_a**: loaded from relocated address `[0x9AC]` → data+0x2A68
  - **state_b**: loaded from relocated address `[0x9B0]` → data+0x2AC8
  - These are 0x60 bytes apart; +0x48 on state_a ≠ +0x48 on state_b

---

## Module Header (0x000–0x0A7)

```
0x000  Start offset:          0 (no application entry)
0x004  Init offset:           0x0A8 → mod_init
0x008  Final offset:          0x0F0 → mod_final
0x00C  Service call offset:   0 (none)
0x010  Title offset:          0x02C → "ARMlock"
0x014  Help offset:           0x034 → "ARMlock\t\t1.04 (12 Jul 1994)"
0x018  Command table:         0 (no *Commands)
0x01C  SWI chunk:             &47880
0x020  SWI handler:           0x128 → mod_swi_handler
0x024  SWI decode table:      0x050
0x028  SWI decode code:       0

0x02C  "ARMlock\0"
0x034  "ARMlock\t\t1.04 (12 Jul 1994)\0"
0x050  SWI Table: "ARMlock\0"
       "ValidatePassword\0"     ; &47880 (SWI 0)
       "ReturnInfo\0"           ; &47881 (SWI 1)
       "EnterUserMode\0"        ; &47882 (SWI 2)
       "ChangePassword\0"       ; &47883 (SWI 3)
       "ReturnSerialNumber\0"   ; &47884 (SWI 4)
       "\0"
```

---

## SharedCLib Module Wrappers (0x0A8–0x174)

### mod_init (0x0A8)
RISC OS module initialisation entry point.
Saves CLib relocation, calls `_clib_initialisemodule` indirectly, then
calls `c_module_init` (0xAF4).
Returns V clear on success, V set on error.

### mod_final (0x0F0)
Module finalisation. Restores CLib state, calls `_clib_finalisemodule`.

### mod_swi_handler (0x128)
SWI dispatch wrapper. Receives SWI number in R11 (fp), register block
in R13 (sp). Sets up CLib environment, calls `swi_dispatch` (0x2078),
returns result with V flag indicating error.

---

## SVC Callback Dispatcher (0x178–0x1E4)

### svc_callback_entry (0x178)
Called via `ARMlock_EnterUserMode` SWI or callback mechanism.
Enters SVC mode (via TEQP), sets up CLib environment, dispatches
to handler via branch table:
- Offset 0 → 0x1CC (default/return)
- Offset 4 → service_handler (0x9B4)

---

## FileCore Hook Installation (0x1E8–0x258)

### install_hooks (0x1E8)
**The core setup function.** Called with:
- a1 = base disc address (primary zone map start)
- a2 = stride (nzones × sector_size)
- a3 = inner module private word

```c
void install_hooks(uint32_t base, uint32_t stride, void *priv_word) {
    primary_map_addr = base;           // [0x264]
    backup_map_addr  = base + stride;  // [0x268]
    root_dir_addr    = base + stride*2;// [0x260]
    inner_priv_word  = priv_word;      // [0x25C]

    // Look up FileCore module
    XOS_Module(18, "FileCore%Base", &code, &workspace);   // 0x208
    filecore_jumptable = workspace + workspace[0x20];      // [0x280]

    // Insert embedded inner module as a RISC OS module
    XOS_Module(10, inner_module_header);  // 0x224 — register ARMlock_cunning
    XOS_Module(3, priv_word);             // 0x234 — claim workspace
    XOS_Module(4, "ARMlock_cunning");     // 0x244 — start (patches FileCore)

    return 0;
}
```

### Variables (0x25C–0x280)
```
0x25C  inner_priv_word      ; saved private word for ARMlock_cunning
0x260  root_dir_addr        ; disc address of root directory (within fragment 2)
0x264  primary_map_addr     ; disc address of primary zone map copy
0x268  backup_map_addr      ; disc address of backup zone map copy
0x26C  filecore_workspace   ; pointer to FileCore's workspace
0x270  "FileCore%Base\0"    ; string for OS_Module lookup
0x280  filecore_jumptable   ; pointer to FileCore's internal dispatch table
```

---

## Enable/Disable Functions (0x284–0x29C)

### enable_remap (0x284)
Sets `remap_active` [0x29C] = 1.

### disable_remap (0x290)
Sets `remap_active` [0x29C] = 0.

---

## Embedded Inner Module (0x2A4–0x2DF)

### ARMlock_cunning module header (0x2A4)
A minimal RISC OS module header embedded inside the outer module:
```
0x2A4 + 0x00: 0          Start (none)
0x2A4 + 0x04: 0          Init (none)
0x2A4 + 0x08: 0          Final (none)
0x2A4 + 0x0C: 0          Service (none)
0x2A4 + 0x10: 0x2C       Title → "ARMlock_cunning" (at abs 0x2D0)
0x2A4 + 0x14: 0x2C       Help → same
0x2A4 + 0x1C: &40540     SWI chunk base
0x2A4 + 0x20: 0x3C       SWI handler → abs 0x2E0 (cunning_entry)
```

No init, no finalise, no service calls. Exists purely to register a
legitimate SWI dispatch point that FileCore's patched jump table can
call through, avoiding suspicion from pointing to raw memory addresses.

---

## FileCore Jump Table Patcher (0x2E0–0x358)

### cunning_entry (0x2E0)
SWI handler for ARMlock_cunning. Called by FileCore via the patched
jump table. First call (fp=1) patches the table; subsequent calls
pass through to the original handler.

```c
void *cunning_entry(void *handler_block, int32_t reloc_offset) {
    if (fp != 1) goto filecore_jumptable;  // not our init call

    // Save original FileCore handler parameters
    saved_handler[0] = handler_block[0];              // [0x338]
    saved_handler[1] = handler_block[4];              // [0x33C]
    saved_handler[2] = handler_block[8];              // [0x340]
    original_handler = handler_block[12] + reloc;     // [0x34C]
    hook_base_offset = ADR(0x35C) - reloc;            // [0x344]
    saved_size       = handler_block[16];             // [0x348]
    boot_option_cache = handler_block[3];             // [0x350]

    // Return replacement handler block pointing to our hooks
    return &our_handler_block;
}
```

### get_boot_option (0x354)
Returns cached `boot_option_cache` [0x350].

---

## Disc Operation Hook (0x35C–0x5D0)

### discop_hook (0x35C)
**Central interception point.** Called for every FileCore disc operation.
Checks disc address (R2/a3) against three stored ranges.

```c
int discop_hook(int reason, int flags, uint32_t disc_addr, ...) {
    if (!remap_active)  // [0x29C]
        goto original_handler;  // [0x34C]

    // === Zone map access? ===
    if (disc_addr == primary_map_addr)   // [0x264]
        goto handle_map_access(map_copy=1);
    if (disc_addr == backup_map_addr)    // [0x268]
        goto handle_map_access(map_copy=2);

    // === Root directory access? ===
    if (disc_addr < root_dir_addr)       // [0x260]
        goto original_handler;
    if (disc_addr >= root_dir_addr + 0x800)
        goto original_handler;

    // ROOT DIRECTORY REMAP
    clean_addr = root_dir_addr & ~0xE0000000;  // strip DiscOp reason bits
    disc_addr -= clean_addr;
    disc_addr += 0x400;           // redirect to real root at 0x400

    call_original();

    disc_addr -= 0x400;           // reverse remap for caller
    disc_addr += clean_addr;
    return;

handle_map_access:
    sub_reason = flags & 0xF;

    if (sub_reason == 1 && map_copy == 1)  goto primary_read;
    if (sub_reason == 1 && map_copy == 2)  goto backup_read;
    if (sub_reason == 2 && map_copy == 1)  goto primary_write;
    if (sub_reason == 2 && map_copy == 2)  goto backup_write;
    goto original_handler;

primary_write:              // 0x40C
    save_byte0  = buffer[0];
    save_byte0B = buffer[0x0B];
    buffer[0x0B] = ((buffer[0x0B] & 3) << 2) + 2;  // encode boot_option
    zone_check(buffer, sector_size);                  // recompute checksum
    buffer[0] ^= 0xFF;                                // invalidate primary
    call_original();
    buffer[0]    = save_byte0;
    buffer[0x0B] = save_byte0B;
    return;

backup_write:               // 0x490
    save_byte0  = buffer[0];
    save_byte0B = buffer[0x0B];
    buffer[0x0B] = ((buffer[0x0B] & 3) << 2) + 2;  // encode boot_option
    zone_check(buffer, sector_size);                  // valid checksum
    call_original();
    // Check if primary map addr changed (disc swap detection)
    if (primary_map_addr != saved_primary)
        call_original_for_primary_too();
    buffer[0]    = save_byte0;
    buffer[0x0B] = save_byte0B;
    return;

primary_read:               // 0x534
    if (flags & 0x20) buffer = *buffer;  // scatter list dereference
    call_original();
    buffer[0x0B] = buffer[0x0B] >> 2;   // decode boot_option
    zone_check(buffer, sector_size);      // recompute for decoded data
    return;

backup_read:                // similar to primary_read
    call_original();
    buffer[0x0B] = buffer[0x0B] >> 2;
    zone_check(buffer, sector_size);
    return;
}
```

### zone_check (0x584)
Computes FileCore ZoneCheck byte. Identical to FileCore's `NewCheck`.
**Critical implementation detail**: models ARM `ADCS` as a 32-bit accumulator
with a separate 1-bit carry flag, NOT a 33-bit integer.

```c
uint8_t zone_check(uint8_t *sector, uint32_t length) {
    uint32_t acc = 0;
    uint32_t carry = 0;

    // Sum 32-bit words end-to-start, 4 words per iteration
    for (ptr = sector + length; ptr != sector; ) {
        ptr -= 4;
        total = acc + *ptr + carry;     // ADCS
        acc = total & 0xFFFFFFFF;
        carry = total > 0xFFFFFFFF ? 1 : 0;
    }

    acc -= sector[0];              // subtract existing check byte
    acc ^= (acc >> 16);            // fold 32 → 16
    acc ^= (acc >> 8);             // fold 16 → 8
    return acc & 0xFF;
}
```

---

## Hook Function Tables (0x5E0–0x668)

```
0x5E0  Hook table: DiscOp       [relocated ptr to filecore_discop_hook]
0x5FC  Hook table: MiscOp       [relocated ptr to filecore_miscop_hook]
0x604  Hook table: SectorDiscOp [relocated ptr to filecore_sectorop_hook]
```

### set_hooks_enabled (0x650)
Sets `hooks_enabled` [0x668] = 1.

### set_hooks_disabled (0x65C)
Sets `hooks_enabled` [0x668] = 0.

---

## FileCore Hook Entry Points (0x66C–0x77C)

### filecore_discop_hook (0x66C)
Hook for FileCore_DiscOp. Checks `hooks_enabled`, enters C environment
via `enter_c_env`, calls access control handler.

### filecore_miscop_hook (0x6B4)
Hook for FileCore_MiscOp. For reasons 0/7/8 with first_access_flag set,
performs password validation sequence via DiscOp calls.

### filecore_sectorop_hook (0x754)
Hook for FileCore_SectorDiscOp. Same pattern.

### discop_readwrite_wrapper (0x728)
Wrapper that saves/restores all 8 DiscOp parameter registers around
a call to the original FileCore handler.

---

## Vector Handlers (0x780–0x87C)

### enter_c_env (0x780)
Saves RISC OS SVC environment, replaces with SharedCLib environment.

### restore_c_env (0x7B0)
Reverses `enter_c_env`.

### bytev_handler (0x7D4)
ByteV (vector 6). Claims OS_Byte 162 (read CMOS) when intercepting.

### claim_bytev (0x7E0) / release_bytev (0x7F8)
`XOS_Claim/Release(6, bytev_handler, 0)`

### fscv_handler (0x814)
FSCV (vector 15). Intercepts FSControl reason 4 (*Cat), sets flag,
issues filtered OS_FSControl 37 (canonicalise).

### claim_fscv (0x848) / release_fscv (0x860)
`XOS_Claim/Release(15, fscv_handler, 0)`

### store_pathbuf (0x878)
Stores path buffer pointer for FSCV handler.

---

## C-Level Init and Boot (0x880–0xBA8)

### svc_callback (0x880)
Callback handler. Sets up CLib module environment, calls `c_main_init`.

### c_main_init (0x8C0)
Sets up OS environment handlers:
- Releases existing OS_ChangeEnvironment handler (reason 0x20)
- Installs callback handler (reason 7) — saves old handler to state_b+0x40/44/48
- Installs OS_ChangeEnvironment for errors (reason 0x40)
- Issues XOS_FSControl 0x29 to select filing system
- Sets OS_SetCallBack
- Claims event vector (reason 0x1E)

### service_handler (0x9B4)
Handles service reason 7 (environment change). Saves incoming handler
parameters to state_b+0x40/44/48.

### c_module_init (0xAF4)
Boot sequence orchestrator:
```c
int c_module_init(void *pw, int podule_base, void *priv) {
    state_a->first_run = 0;            // +0x34
    store_private_word(priv);
    disable_remap();
    disc_setup();

    if (error && retry_possible) {
        open_messages();
        disable_rmclear();
        display_message("bootup");
        cmos_restore();
        alloc_pathbuf();
        claim_fscv();
        set_hooks_disabled();
        start_desktop_login();
    }

    if (!error) {
        register_atexit(atexit_handler);
        claim_bytev();
        c_main_init();
    }
    return error;
}
```

---

## Disc Setup (0xBBC–0xF6C)

### disc_setup (0xBBC)
Discovers the ARMlock-protected disc and computes geometry. See §3.3 of
the reference document for the complete algorithm. Key steps:
1. Read `FileSwitch$CurrentFilingSystem`
2. Set `ARMlock$Disc` system variable
3. Iterate drives 4–7 to find the protected disc
4. Read disc record, extract geometry fields
5. Compute `map_start` and `stride`
6. Call `install_hooks(map_start, stride, ...)`
7. Set `$.ARMlock` access to `"wr/r"`

### match_path (0x119C)
Case-insensitive pathname prefix match:
```c
int match_path(const char *path) {
    const char *prefix = armlock_disc_path;
    while (toupper(*path) == toupper(*prefix)) { path++; prefix++; }
    return (*path == '.' && *prefix == '\0') ? 1 : 0;
}
```

---

## CMOS Restore (0x1394–0x1604)

### cmos_restore (0x1394)
Reads BootUp.Options and optionally restores CMOS RAM:

```c
void cmos_restore(void) {
    FILE *f = fopen("<ARMlock$Disc>.$.ARMlock.BootUp.Options", "r");

    if (!f) {
        display_message("defopt");
        state_a->serial_number   = 0xFFFFFFFF;   // +0x48 — invalid/default
        state_a->config_flag     = 1;             // +0x38
        state_a->cmos_protection = 1;             // +0x3C — disabled
        return;
    }

    // Read 4 bytes: serial number (little-endian via fgetc)
    state_a->serial_number =                      // +0x48
        fgetc(f) | (fgetc(f)<<8) | (fgetc(f)<<16) | (fgetc(f)<<24);
    state_a->config_flag     = fgetc(f);          // +0x38 (byte 4)
    state_a->cmos_protection = fgetc(f);          // +0x3C (byte 5)
    fclose(f);

    if (state_a->cmos_protection != 0)            // non-zero = disabled
        return;                                    // skip CMOS restore

    // CMOS protection enabled — restore from backup
    display_message("resetcmos");
    f = fopen("<ARMlock$Disc>.$.ARMlock.BootUp.CMOS", "r");
    if (!f) { display_message("nocmos"); return; }

    hourglass_on();
    // Read 240 bytes (10 banks × 24), compare & write CMOS
    for (bank = 0; bank < 240; bank += 24) {
        for (i = 0; i < 24; i++) {
            stored = fgetc(f);
            if (bank+i == 0x80 || bank+i == 0x81) continue;  // protected
            OS_Byte(161, bank+i, &current);
            if (stored != current) OS_Byte(162, bank+i, stored);
        }
        hourglass_percentage(bank * 100 / 240);
    }
    hourglass_off();
    fclose(f);
}
```

---

## ByteV Claim Management (0x1270–0x12F0)

### claim_bytev_if_needed (0x1270)
```c
void claim_bytev_if_needed(void) {
    if (state_a->cmos_protection != 0) return;  // +0x3C: disabled → skip
    if (state_a->bytev_claimed != 0) return;    // +0x34: already claimed
    claim_bytev();
    state_a->bytev_claimed = 1;                 // +0x34
}
```

### atexit_release_bytev (0x12B8)
Registered via `atexit()`. Releases ByteV if claimed:
```c
void atexit_release_bytev(void) {
    if (state_a->bytev_claimed == 0) return;
    release_bytev();
    state_a->bytev_claimed = 0;
}
```

---

## Protection Functions (0x1780–0x188C)

### disable_hform (0x1780)
On ADFS, loads `$.ARMlock.BootUp.NoHForm` module, then issues ADFS_63
to current drive to disable formatting.

### disable_rmclear (0x1834)
`OS_SetVarVal("Alias$RMClear", "", 0, 0, 0)` — disables `*RMClear`.

---

## Error Generators (0x1890–0x1A88)

All follow the same pattern: load error number, load MessageTrans token,
call `MessageTrans_ErrorLookup`:

| Address | Token | Meaning |
|---------|-------|---------|
| 0x1918 | nodisc | Disc not found |
| 0x1938 | nofsblock | No FS block |
| 0x195C | accviol | Access violation |
| 0x1974 | unrecint | Unrecognised interrupt |
| 0x1998 | accviols | Access violation (sector) |
| 0x19B4 | accviold | Access violation (directory) |
| 0x19D0 | accviolr | Access violation (read) |
| 0x19EC | accviolnr | Access violation (no read) |
| 0x1A08 | accviole | Access violation (enumerate) |
| 0x1A24 | nomem | Out of memory |
| 0x1A44 | wrongpass | Wrong password |
| 0x1A68 | nomanfile | Management file missing |

---

## Access Control Handlers (0x1A90–0x2074)

### handle_discop_verify (0x1A90)
DiscOp reason 0. Validates disc path and checks first-access state.

### handle_discop_readwrite (0x1B24)
Intercepts read/write, validates path against allowed list.

### handle_sectorop (0x1C20)
Dispatches by reason code. Reasons 0/7/8 trigger password validation
via `password_validate` (0x1FC0). Reason 0x1F (format) is blocked.

### handle_miscop (0x1E18)
Mount/catalogue/format interception. Catalogue requires password.
Format blocked. Dispatches via jump table on reason code.

### password_validate (0x1FC0)
Extracts leaf filename from path (after last "."), issues a DiscOp
read via the original handler to verify the disc is accessible,
then checks the access flags.

---

## SWI Dispatch (0x2078–0x2104)

### swi_dispatch (0x2078)
C-level SWI handler. Dispatch table:

| SWI | Offset | Branch target | Handler |
|-----|--------|---------------|---------|
| 0 | 0x2098 | → 0x2108 | swi_validate_password |
| 1 | 0x209C | → 0x2290 | swi_return_info |
| 2 | 0x20A0 | → 0x2198 | swi_enter_usermode |
| 3 | 0x20A4 | → 0x2208 | swi_change_password |
| 4 | 0x20A8 | inline | swi_return_serial |

---

## SWI Handlers (0x2108–0x22E0)

### swi_validate_password (0x2108)
```c
int swi_validate_password(int *regs) {
    if (read_options_hash() != 0) return error;  // reads <ARMlock$Dir>.Options

    uint32_t computed[2];
    password_hash(regs[0], computed);

    if (stored_v3 != computed[0] || stored_v4 != computed[1])
        return error_wrongpass();

    state->locked = 0;
    claim_bytev_if_needed();
    set_hooks_disabled();
    claim_bytev_if_needed();
    return 0;
}
```

### swi_return_info (0x2290)
Returns four 32-bit words from installation-patched static data:
```c
int swi_return_info(int *regs) {
    regs[0] = info_word_0;  // from data+0x2A90
    regs[1] = info_word_1;  // from data+0x2A8C
    regs[2] = info_word_2;  // from data+0x2A94
    regs[3] = info_word_3;  // from data+0x2A98
    return 0;
}
```
These are zero in a clean binary — patched into the file during installation.

### swi_enter_usermode (0x2198)
Stores caller-provided addresses, enables hooks, marks info as returned:
```c
int swi_enter_usermode(int *regs) {
    if (state->info_returned) return error;  // one-shot
    store_caller_addresses(regs);
    set_hooks_disabled();
    claim_bytev_if_needed();
    state->info_returned = 1;
    return 0;
}
```

### swi_change_password (0x2208)
```c
int swi_change_password(int *regs) {
    if (read_options_hash() != 0) return error;

    uint32_t old_hash[2];
    password_hash(regs[0], old_hash);
    if (stored_v3 != old_hash[0] || stored_v4 != old_hash[1])
        return error_wrongpass();

    password_hash(regs[1], new_hash);
    write_options_hash(new_hash);
    return 0;
}
```

### swi_return_serial (0x20A8, inline in dispatch)
```c
int swi_return_serial(int *regs) {
    regs[0] = state_a->serial_number;  // +0x48, from data+0x2AB0
    return 0;                           // loaded from BootUp.Options at boot
}
```

---

## Password Hash (0x22F4–0x2358)

### password_hash (0x22F4)
```c
void password_hash(const char *password, uint32_t result[2]) {
    uint32_t v4 = 0x01234567;
    //  MOV R7, #0x67
    //  SUB R7, R7, #0xBB00
    //  ADD R7, R7, #0x01240000
    //  = 0x01234567

    uint32_t v3 = 0x89ABCDEF;
    //  MOV R6, #0x01A80000
    //  ADD R6, R6, #0xEF
    //  SUB R6, R6, #0x3300
    //  ADD R6, R6, #0x00040000
    //  ADD R6, R6, #0x88000000
    //  = 0x89ABCDEF

    while (1) {
        char c = toupper(*password++);
        if (c == 0) break;

        // ASR 13 | LSL 19 (arithmetic right shift, NOT logical)
        // When bit 31 set: mask 0xFFF80000 fills top 13 bits
        uint32_t rotated = ((int32_t)v3 >> 13) | (v3 << 19);
        v3 = rotated + c;
        v4 ^= v3;
    }

    result[0] = v3;
    result[1] = v4;
}
```

---

## Options File I/O (0x235C–0x2448)

### read_options_hash (0x235C)
Opens `<ARMlock$Dir>.Options`, reads first 8 bytes (password hash):
```c
int read_options_hash(void) {
    handle = _kernel_osfind(0x43, "<ARMlock$Dir>.Options");
    if (!handle) return error_nomanfile();
    for (i = 0; i < 8; i++)
        hash_buf[i] = _kernel_osbget(handle);  // into [0x218C] area
    _kernel_osfind(0, handle);
    return 0;
}
```

### write_options_hash (0x23E0)
Opens same file for output, writes 8 bytes:
```c
int write_options_hash(uint32_t new_hash[2]) {
    handle = _kernel_osfind(0xC3, "<ARMlock$Dir>.Options");
    if (!handle) return error_nomanfile();
    for (i = 0; i < 8; i++)
        _kernel_osbput(hash_buf[i], handle);
    _kernel_osfind(0, handle);
    return 0;
}
```

---

## State Structure (state_a, base = data+0x2A68)

| Offset | Size | Field | Set by | Read by |
|--------|------|-------|--------|---------|
| +0x000 | 4 | FileCore base name pointer | disc_setup | various |
| +0x024 | 4 | map_stride | disc_setup | disc operations |
| +0x028 | 4 | map_disc_addr (with drive bits) | disc_setup | disc operations |
| +0x02C | 4 | SWI number for DiscOp | disc_setup | access control |
| +0x030 | 4 | sector_size | disc_setup | disc operations |
| +0x034 | 4 | bytev_claimed flag | claim_bytev_if_needed | claim/release |
| +0x038 | 4 | config_flag (from BootUp.Options byte 4) | cmos_restore | **desktop app only** |
| +0x03C | 4 | cmos_protection (byte 5): 0=on, ≠0=off | cmos_restore | cmos_restore, claim_bytev |
| +0x040 | 4 | path buffer pointer | alloc_pathbuf | various |
| +0x044 | 4 | current_drive (4–7) | disc_setup | disc operations |
| +0x048 | 4 | serial_number (from BootUp.Options bytes 0–3) | cmos_restore | swi_return_serial |
| +0x05C | 4 | module private word | c_module_init | various |

## State Structure (state_b, base = data+0x2AC8)

| Offset | Size | Field | Set by | Read by |
|--------|------|-------|--------|---------|
| +0x040 | 4 | saved callback handler address | c_main_init, service_handler | c_main_init |
| +0x044 | 4 | saved callback R12 value | c_main_init, service_handler | c_main_init |
| +0x048 | 4 | saved callback buffer pointer | c_main_init, service_handler | c_main_init |
