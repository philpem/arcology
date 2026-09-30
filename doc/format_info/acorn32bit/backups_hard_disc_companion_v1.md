# Hard Disc Companion 1.05 — backup on-disk format

The on-disk backup format produced by **Hard Disc Companion** version 1.05
(`!B'up_Hard`, Beebug Limited, © 1989/1990; Backup 0.90 / Restore 0.90).
Reverse-engineered from the Backup/`Restore` program source (recovered from a
backup of the application itself) and confirmed against a genuine 14-disc
real-world backup set.

> **Context.** This is the **1.05** format. A later generation, Hard Disc
> Companion II 2.55, uses a different Chunk-based format described in
> `backups_hard_disc_companion_v2.md`.
>
> Items marked **[?]** are open/unknown.

---

## 1. Overview

A single HDC backup of a whole hard disc is split across multiple physical
ADFS floppy disks (ADFS-D/S, 800 KB, 1024-byte sectors in the captured set; the
images are named `<set>-Backup-1ofN … NofN`).

- The backup is a **directory tree**, and a copy of that tree is present on
  **each** physical disk. Every disk carries its own `!saveset/` directory with
  its own `!Run`, `!Sprites`, and a *piece* of the backed-up data.
- Each disk is an independent container; HDC directs the user to insert the
  disks in order (1 → N).
- Totals across the captured 14-disk set: 1023 data files, of which 183 are
  `_Z` (compressed) and the rest are `_N` (raw).

> The disk images in these captures used 1024-byte-sector ADFS ("Nick"-labelled
> large-directory) format. See §References for re-extracting them with a
> generic ADFS reader.

---

## 2. Storage layout

### 2.1 Directory tree

```
!saveset/
  !Run               ; finder/boot script
  !Sprites
  emptydirs          ; OPTIONAL — only on disk 1; list of empty directories (§4)
  FILES/             ; first backing directory (§3)
    names            ; original path list for THIS directory (§2.2)
    _1               ; file #1 (raw)
    _2 … _75
    FILES/           ; nested backing dir — created when the parent FILES fills
      names
      _1 …           ; numbering restarts (local to this directory)
      FILES/         ; can nest deeper (recursive; see §2.3)
        names
        _1 …
```

### 2.2 The `_N` numbering

The `_N` numbering **restarts at 1 in every `FILES` directory** — it is local to
each directory, not a global running counter. Confirmed:

```
disk 1:  FILES/_1 … FILES/_75            (75 entries = directory full)
         FILES/FILES/_1 … FILES/FILES/_12 (nested dir, numbering reset)
disk 2:  FILES/_1 … FILES/_55            (numbering reset again)
```

Each `FILES` directory is a self-contained unit capped at **75** data entries,
and its own `names` file lists the **original file paths in order** for exactly
those entries. There is no global counter; the ordering of the backup is purely
a function of the `names` files and the disk/directory walk order. The `names`
file is authoritative for reconstructing paths; `_N` is only a local storage
label.

### 2.3 `FILES` nesting depth

`FILES` directories nest recursively, each capped at 75 data entries. There is
**no observed upper bound on depth** — it grows whenever a parent `FILES`
fills. The real set reaches depth **3**:

```
disk 7:  FILES/_1…_75
         FILES/FILES/_1…_75
         FILES/FILES/FILES/_1…_8   (3 levels deep)
```

Per-disk `FILES`-depth usage across the 14 disks:

```
disk 1:  FILES(75)  FILES/FILES(12)
disk 2:  FILES(55)
disk 3:  FILES(75)  FILES/FILES(11)
disk 4:  FILES(37)
disk 5:  FILES(48)
disk 6:  FILES(75)  FILES/FILES(6)
disk 7:  FILES(75)  FILES/FILES(75)  FILES/FILES/FILES(8)     <- depth 3
disk 8:  FILES(72)
disk 9:  FILES(69)
disk 10: FILES(75)  FILES/FILES(58)
disk 11: FILES(67)
disk 12: FILES(75)  FILES/FILES(54)
disk 13: (none)
disk 14: FILES(1)
```

No disk shows a `FILES` directory with more than 75 `_N` files; no limit on
overall depth has been reached (the 75-cap recursion can go as deep as needed).

### 2.4 Only `_N` and `_N_Z` are files

There is **no** "backed-up directory" object in the `FILES` tree. Across the
entire 14-disk set, the **only** `is_directory=true` entry is the top-level
`!saveset/FILES` itself; every `_N`/`_N_Z` is a regular file. Directories are
re-created from the `names` / `emptydirs` metadata and the ADFS directory
attributes, not stored as `_N` directory nodes.

The apparent "directory" on disk 8 is a false positive from archive expansion,
not part of HDC's format:

```
!saveset/FILES/_36                          is_archive=True, Spark Archive, type 0xddc
!saveset/FILES/_36/!MTVDEMO/!RunImage …     (contents shown by the extractor)
```

`_36` is a single **Spark archive file**; an analysis pipeline auto-extracted it
and listed the contents beneath `_N`. HDC stores any archive (zip/Spark/etc.) as
one `_N` file; whether it gets unpacked is the viewer's decision, not HDC's.

Therefore HDC's container is a **flat recursion of `_N`/`_N_Z` regular files**,
with `FILES/FILES/…` being the only nesting and the 75-entry cap the only
grouping rule.

---

## 3. The `_N` / `_N_Z` data files

- `_N` (no suffix) = **raw** file data, stored as-is.
- `_N_Z` = **compressed** file data. The `_Z` suffix is the only marker; nothing
  in the file content distinguishes it (see §5.1).
- The original RISC OS **filetype / load / exec** is not in the file content. It
  is carried in the file's own ADFS directory entry (§6).

---

## 4. The `names` and `emptydirs` files

### 4.1 `names`

Plain text, one original path per line, **LF** line-terminators (no CRs
observed), no trailing newline beyond the final line terminator.

- Lists the original file path/name for each `_N` entry in that directory, in
  order: line 1 → `_1`, line 17 → `_17`, etc.
- Sample `names` has exactly **75** lines (matches the per-directory cap).
- Example entries (disk 1): line 1 `!Boot`, line 17 `!CoCo.Resources.Sprites`,
  line 22 `!CoCo.RMStore.CoCoDriver`.

### 4.2 `emptydirs`

Plain text, one empty-directory path per line, LF line endings, terminating in
a special footer line of the form `### <number>`:

```
!Printers.ps.Printers
!Scrap.ScrapDirs.ScrapDir.ID1f91aa88
…
Ts_Data.SPRITE
### 1103
```

- Present only on disk 1 of the set.
- The `### <number>` footer is a **progress/total-entries marker**, not a count
  of the listed empty dirs. The Restore program reads it as
  `IF LEFT$(line$,4)="### " THEN filestodo%=VAL(MID$(line$,5))` — `<N>` is the
  **total number of files to restore across the whole backup set** (1103 here),
  used for the progress percentage. Only 15 lines *precede* it — those are the
  empty-directory paths; `### 1103` is the file-total tail.

---

## 5. Compression

### 5.1 The `_N_Z` stream format

The compressed data is the standard **Unix `compress` (LZW)** stream,
**13-bit** max code width. It is not a bespoke Acorn format.

```
offset  size  field
0x0000  4     uncompressed original size, LITTLE-ENDIAN
0x0004  …     Unix LZW / `compress` stream (begins 1F 9D <flags>)
```

- The 4-byte leading word is the original file size (LE), added by HDC. It is
  **not** part of the `.Z` stream; strip it before decoding. `_N`
  (uncompressed) files carry no such prefix.
- The `.Z` stream is byte-identical to what `compress`/`uncompress` produce:
  magic `1F 9D`, then a flags byte.
- **Flags-byte semantics (empirically verified):** the **low 4 bits of the
  flags byte encode maxbits** — `0x8c`→12, `0x8d`→13, `0x8b`→11, `0x90`→16.
  Bit 7 (0x80) is set on all HDC files; bit 6 is reserved. HDC uses `0x8d` →
  **13 bits**.
- Because the 4-byte prefix sits at byte 0, `gzip`/`uncompress` cannot
  auto-detect the stream (they look for magic at byte 0). Strip the prefix
  first, then feed the `.Z` stream to `gzip -cd`/`uncompress`.

### 5.2 Verified across the whole backup

All **183 `_Z` files** in the 14-disk set were downloaded and independently
decoded:

- Magic `1f 9d 8d`: **183 / 183**
- Decode cleanly: **183 / 183**, 0 failures
- Decompressed size equals the 4-byte LE prefix: **183 / 183**, 0 mismatches

The compression is a single, uniform 13-bit Unix-LZW stream in every file — no
mixed formats or per-file variation.

### 5.3 Algorithm summary

Standard POSIX `compress` LZW (as in `ncompress`/NetBSD `compress`):

- Codes start at 9 bits and grow by one as the table fills, up to `maxbits` (13).
- `CLEAR = 256`; first free entry `257`.
- Codes are packed **least-significant-bit first** across the stream.
- The "KwKwK" special case must be handled on decode.

### 5.4 This is Unix `compress`, not Squash/Squeeze/ARC

Three distinct things, do not conflate:

| Scheme | Applies to | Format | Used on `_N_Z`? |
|---|---|---|---|
| **Compress** (this) | HDC backup data | Unix LZW `.Z`, 13-bit | **yes** |
| **Squeeze** | executable AIF images (e.g. `!RunImage`) | 16-bit-word dictionary / nibble encoding | no |
| **Squash** (`!Squash`) | single files, Acorn | compressed as a filetype; **no filename suffix** | no |

- **Squeeze** is not used on backup payloads — it only appears on the module's
  own squeezed `!RunImage`.
- **Squash** is a per-file compressor signalled by the RISC OS filetype
  `Squash`, not by a filename suffix; it does not appear in `_N_Z` data.
- SEA ARC / LZH "crunch" share LZW ancestry but are not byte-compatible with
  Unix `.Z` (no `1F 9D` magic, different code packing).

A restore tool therefore only needs standard Unix-LZW `.Z` decompression for
the backup data.

---

## 6. Original file attributes

The filetype and load/exec of each backed-up file are not stored in the file
content nor in `names` — they are stored in the **RISC OS ADFS directory entry**
of the `_N`/`_N_Z` file itself. HDC writes the original file's load/exec address
as the `_N` file's own load address. Copying these files to a PC (losing RISC OS
attributes) destroys the filetype info.

In the real set (recovered via ADFS metadata), each `_N`/`_N_Z` file carries a
load address whose top bits encode the **original filetype**:

```
filetype = (load_address >> 8) & 0xFFF
```

Confirmed examples (disk 1):

```
FILES/_1         load=ffffea48  -> filetype 0xfea   (raw)
FILES/_17_Z      load=fffff943  -> filetype 0xff9   (compressed)
FILES/_22        load=fffffa44  -> filetype 0xffa   (raw)
FILES/_28_Z      load=ffffff43  -> filetype 0xfff   (compressed)
FILES/FILES/_3   load=fffff646  -> filetype 0xff6   (compressed)
```

The exec address and the ADFS `attributes` byte are likewise preserved. These
must be captured and rewritten on restore, or the original filetypes are lost.

---

## 7. The Compress modules

### 7.1 `!Run` (backup-disk finder script) — observed

```
IF "<Restore$Dir>" = "" THEN Error 0 Can't find !Restore
RMEnsure Compress 0.03 RMLoad <Restore$Dir>.Resources.Compress
RMEnsure Compress 0.03 Error 0 Compress Module too old
Wimpslot -min 128K -max 128K
SET SelectiveRestore$Dir <obey$dir>
IconSprites <SelectiveRestore$Dir>.!Sprites
Run <Restore$Dir>.Resources.Floppy.RunImage
```

- Loads `<Restore$Dir>.Resources.Compress` and requires version 0.03 or later.
- HDC's own `!Backup`/`!Restore`/`!Run` (the creator application) does not
  `RMEnsure Compress` (it only loads SharedCLibrary, confirmed for 2.06 and
  2.55). The `RMEnsure Compress 0.03` line is in the **backup disk's**
  `!saveset/!Run`. So the Compress module is expected on the target (or
  supplied by the loader); the `!RunImage` invokes whichever Compress SWI is
  installed at runtime.

### 7.2 Algorithms implemented by the archived modules

Both `compress003` (Computer Concepts) and `compress003beebug`
(Pilling/BEEBUG) implement the **same underlying algorithm — standard Unix
`compress` LZW** — but with **incompatible framing**; they are not
interchangeable.

**Computer Concepts `compress003` — "Compress 0.03 (20 Feb 1991)"**
- LZW; exposes `Compress`/`Decompress` SWIs (chunk `0x42700`).
- **Hard-coded 12-bit**, confirmed by disassembly:
  - Decompress entry requires the 3rd byte to be exactly `0x8c` (12-bit marker),
    else error;
  - string table capped at 4096 = 2^12.
  - It writes `1f 9d 8c` as individual byte stores.
- This build decompresses **only 12-bit** `.Z` files (`0x8c`).

**David Pilling / BEEBUG `compress003beebug` — "Compress 0.03 (23 Aug 1989)"**
- LZW core; exposed as a **stream/primitive API**, not a file codec: SWIs
  `CSETMEM`, `CSETBITS`, `CCOPY`, `UCCOPY`, `CSETFILEBUFF`. It does not emit or
  expect the `1f 9d` header — it reads a config byte and maintains the bitstream
  across calls. It also bundles a helper **CRC-32** routine.
- Can handle 13-bit input when told (via `CSETBITS`), but the header/framing is
  the caller's responsibility.

**`compress103` — "Compression 1.03 (10 Aug 1990)"**
- Exposes a **block/line API** (chunk `0x41ec0`): `CompressBlock`, `ExpandBlock`,
  `CompressLine`, `ExpandLine`, `AnalyseLine`, `SetFlags`, `Diagnostics`,
  `GetFastExpand`, `VerticalSquash`. Different API; not the file compressor.

### 7.3 Which module produced the HDC data

HDC's `_N_Z` files are **13-bit** (`0x8d`), verified across all 183 files. The
archived Computer Concepts `compress003` is a **12-bit** build that rejects
`0x8d`. Therefore **HDC shipped a different (13-bit) version** than the
`compress003` in the archive. The 13-bit width + `1f 9d 8d` header is the
definitive signature a restore tool must target.

Practical note: a standalone restore tool should implement (or call) 13-bit
Unix-LZW itself, since the archived `compress003` won't decode the files.
gzip/`uncompress`/`ncompress` all handle the `_N_Z` stream after the 4-byte
prefix is stripped.

---

## 8. RunImage internals

`!RunImage` is a compressed AIF absolute file; `uncompressAIF` decompresses it.
After decompression it is an ARM 32-bit (ARMv4T) image.

Key message-token strings (used to drive the restore UI/errors):

```
%%WIPE %s   %%CDIR %s   deadfile   Xwritdest   Xfindfl   Xopenfl   Xrddata
Xfindhdr   Xcdirfor   Xrestore   Xdelete   Xrrweird   Xlogbuf   xrdlog
filefnd   onefile   filecnt   FileInfo   %.8X   %.8X   file_%.3x
small_%.3x   File$Type_%.3x   &%.3x   %s.%s
```

The record-processing path reads a 0x18-byte header block and a 0x100-byte path
per record. The `_N` file content is confirmed raw (no embedded header), so any
0x18-byte header is a log/record artifact of the restore tool, not part of `_N`.

---

## 9. Creator identification

The creating application is included **inside the backup itself** (disks 7–8),
and is the `!B'up_Hard` suite = **Hard Disc Companion v1.05, by Beebug
Limited, (c) 1989, 1990**. It is not HDC 2.55's `!Backup`/`!Restore` (which
uses `Chunk_%d`/`data_%d`); it is an earlier Beebug HDC variant that uses the
`!saveset`/`FILES`/`names`/`_N` layout.

Evidence, all from within the backup itself (disks 7–8):

```
B'up_Hard.!Backup/!RunImage        ; the Backup (write) program
B'up_Hard.!Restore/!RunImage       ; the Restore (read) program
B'up_Hard.!Restore.Resources.Floppy.RunImage   ; the executable the !saveset/!Run runs
B'up_Hard.!Backup.Resources.Compress           ; Compress module
B'up_Hard.!Restore.Resources.Compress          ; (same module)
B'up_Hard.!FindFile / .!Spark / .readme / MTVDEMOARC
```

- The `!saveset/!Run` finder is byte-identical to
  `B'up_Hard.!Restore.Resources.Floppy.!Run`, and both reference
  `<Restore$Dir>.Resources.Floppy.RunImage` and `RMEnsure Compress 0.03`.
- The Restore `!RunImage` is BASIC source that calls the Compress module
  directly: `OS_CLI "CSETBITS 13"`, `"CSETMEM"`, `"CSETFILEBUFF"`, and uses
  `UCCOPY`/`CCOPY`; the copyright string "Beebug Limited" appears in its
  `Messages` resource. This is the **David Pilling / BEEBUG Compress 0.03**
  module.
- The Backup `!RunImage` (also BASIC source) spells out the entire write format
  and confirms every structure in this spec:
  - `.".!saveset"`, `"…!saveset.emptydirs"`, `"…!saveset.FILES"` (`.FILES` for
    nesting), `"…!saveset.BIGFILES"`;
  - per-file naming `._N`, compressed `._N_Z`, via `"CCOPY"`/compress and
    `"UCCOPY"`/decompress;
  - a `check_compress` routine: compresses only if the file is filetype in a
    configurable compressible list (`Resources.ftypes`), and not if length ≤ 1
    sector and not if unstamped; filetype/load/exec are carried in the file's
    ADFS attributes.
- `B'up_Hard.readme` begins: "This file contains additional information for
  **v1.05 of Hard Disc Companion**", with sections "Backup v. 0.90" and
  "Spark v. 1.05".

So the creator of the observed savesets is **Beebug's Hard Disc Companion v1.05
(the `!B'up_Hard` suite)**, and its source, resources and full application tree
were themselves backed up, which is how the identity was confirmed. This also
resolves the `_Z` suffix, the `!saveset`/`FILES`/`names` naming, and the
compress-vs-raw decision rule.

---

## 10. Open questions

1. The **0x18-byte per-record header** — whether it applies to `_N` records or
   only to a separate (floppy/log) record type. The `_N` content is raw, so any
   0x18-byte header is a separate log/record artifact.
2. **Disk-presentation order** — each disk carries a full `!saveset`; the user
   is prompted to insert disks in order. No separate marker identifies the
   order beyond the set name suffix (`…_1` … `…_N`).
3. `compress103` line-mode API is irrelevant to HDC 1.05; the creator uses the
   beebug module (`Compress`/`CCOPY`/`UCCOPY`/`CSETBITS`), not `compress103`.

---

## 11. Quick reference

- All multi-byte integer fields in file content are **little-endian**.
- Compressed stream `_N_Z`: `[4-byte LE original size][Unix compress (.Z) LZW stream]`.
- `_Z` suffix ⇒ compressed Unix-LZW; no suffix ⇒ raw data.
- `names` / `emptydirs`: LF-terminated text lines; `emptydirs` ends with
  `### <num>` (grand total of files to restore).
- Filetype restoration: `filetype = (load_address >> 8) & 0xFFF` from the `_N`
  file's own ADFS directory entry. `exec_address` likewise preserved.
- 75 data entries per `FILES` directory; numbering restarts in each directory;
  nesting is unbounded.
- Creator: Beebug Hard Disc Companion v1.05 (`!B'up_Hard` suite), using the
  beebug Compress 0.03 module at 13-bit.

---

## 12. Verification

The claims above were confirmed against a genuine 14-disk HDC backup set. Each
`_N`/`_N_Z` file was pulled from the ADFS metadata; filetype reconstruction
(`(load >> 8) & 0xFFF`) matched the recovered ADFS directory entries, and all
183 `_Z` files decoded cleanly at 13-bit with the decompressed size equal to
the 4-byte LE prefix. Because the payload uses standard Unix `compress`, no
bespoke decoder is needed — strip the 4-byte prefix and use
`gzip -cd`/`uncompress`.

---

## 13. References

- ADFS on-disk format reference for anyone re-extracting the images:
  `http://mdfs.net/Docs/Comp/Disk/Format/ADFS` — these are 1024-byte-sector,
  "Nick"-labelled, large-directory ADFS-D images.
