# Hard Disc Companion II (2.55) — on-disk backup format

The on-disk backup format produced by **Hard Disc Companion II** version 2.55
(RISC Developments Ltd, "HDC II"). Reverse-engineered from the 2.55 `!Backup`
and `!Restore` executables (decompressed AIF images loaded in Ghidra) and
confirmed against a genuine digital multi-disc backup.

> **Context.** A companion document, `backups_hard_disc_companion_v1.md`,
> describes the earlier HDC **1.05** (B'up_Hard, Beebug) format
> (`!saveset`/`FILES`/`names`/`_N`). The 2.xx series does **not** use that
> layout; it uses the different Chunk-based format described here.
>
> Items marked **[?]** are open/unknown.

---

## 1. Comparison with HDC 1.05

| | HDC 1.05 (B'up_Hard, Beebug) | HDC 2.55 (RISC Developments, "HDC II") |
|---|---|---|
| Container | `!saveset/` tree | `data_N`/`name_N`/`Chunk_N` + `LogFile`/`Status` |
| Data layout | flat `FILES` recursion of `_N`/`_N_Z` files | independent `Chunk_N` files |
| Path index | `names` (plain text) | `LogFile` (binary records) |
| Empty dirs | `emptydirs` text file | rebuilt by the restore from the log/status |
| Compression | Beebug/CC module, Unix-LZW 13-bit (`_Z`) | internal 12-bit LZW |
| Tools on disc | `!Backup`/`!Restore` in-tree | `!Restore`/`!Retrieve` copied onto the backup |
| Runtime | BASIC | compiled C, WIMP, SharedCLibrary + FPEmulator |

---

## 2. Path templates

These are HDC's own `MessageTrans` strings from the 2.55 resources, so they are
the authoritative on-disk naming scheme.

**Floppy device** (`device::volume`):

```
flopvol : data                                        ; default sub-volume name
floproot: %s::%s.$
flopsset: %s::%s.$.%s                                 ; device::volume.$.<setname>
flopchnk: %s::%s.$.%s.Chunk_%d                        ; device::volume.$.<setname>.Chunk_<N>
floprstr: %s::%s.$.!Restore                           ; Restore program at disc root
floplogf: %s::%s.$.!Restore.LogFile
```

**Removable device:**

```
remvsset: %s::%s.$.%s.data_%d
remvchnk: %s::%s.$.%s.data_%d.Chunk_%d
devrstr : %s::%s.$.%s.!Restore
devlogf : %s::%s.$.%s.!Restore.LogFile
```

**Other (directory) destination:**

```
othrrstr: %s.%s.!Restore
othrdata: %s.%s.!Restore.data_%d
othrflpe: %s.%s.!Restore.data_%d.name_%d
othrchnk: %s.%s.!Restore.data_%d.name_%d.Chunk_%d
othrlogf: %s.%s.!Restore.LogFile
```

Set/volume names come from `volfull:Full`, `voldiff:Diff`, `volinc:In%s`
(incremental substitutes the date at `%s`). The Restore side also references a
**Status** file (`xfindstat`/`invstatus`/`xcrtstat`) alongside `!Restore`
(`hdciirsr`) and `LogFile` (`hdciilog`).

Data is held in numbered `Chunk_N` files, possibly under `data_N`/`name_N`
directories. In the observed backup this was a single `data/Chunk_N` per disc.

---

## 3. Backup build process

The backup driver's message strings show the order of operations:

```
prepdev : Creating volume directory on device
doinglog: Making LogFile
cpyrstr : Copying !Restore program
doingflp: Writing to floppy %d %s
hstlgidx: Moving LogFile, copying !Restore
notfmtdo: Floppy appears to be unformatted: format?
renmqury: Prepare floppy %s as %s ?
```

The Backup writes `Chunk_N` files, records everything in a `LogFile`, and copies
the `!Restore` program onto the first disc and `!Retrieve` onto the last.

---

## 4. Compression

Compression uses HDC II's own internal **12-bit LZW** codec. The backup data
carries no `_Z` suffix and no `1f 9d` Unix-compress stream, and the tool loads
no external Compress module (its `!Run` loads only SharedCLibrary and
FPEmulator).

### 4.1 The codec

The Restore's `FUN_0000ca70` (and the Backup's matching encoder
`FUN_00010ae8`) is a fixed 12-bit LZW:

- Codes are 12 bits (0x000–0xFFF); the dictionary is seeded with single-byte
  literals 0–255 (`count = 0x100`) and capped at `0x1000` entries. It stops
  growing at the cap — there is no code-width widening.
- Dictionary entries are 12 bytes (0xc): `{prev_code, length, byte}`, where the
  byte field is a single byte; an output string is rebuilt backwards by
  following the `prev` chain `length` times.
- Codes are extracted by an alternating nibble-stuffed scheme
  (`bA | (bB & 0xF)<<8` one iteration, `bA>>4 | bB<<4` the next), interleaving
  the 12-bit codes through the byte stream rather than packing them
  byte-aligned.
- The first stream byte doubles as the first output byte and initial decoder
  state; the classic "KwKwK" back-reference is handled.

### 4.2 Verified behaviour

The decompressor was re-implemented from `FUN_0000ca70` and used to decode real
files back to their original content (e.g. the `!CoCo.Resources.Messages` file,
whose stored payload is 4,061 B, was recovered to its original
`# > Messages file for CoCo 1.10 …` source text), confirming the codec.
Compression is **per-file and applied only when it helps**: the rule is to
compress only if the LZW output would be smaller than the original. Correlating
sizes across the set confirms this; large compressible files (e.g. the `0xdf2`
font files, whose original size is `0x64ec` and stored size is ~`0x500`) are
compressed, while small or already-dense files (small Text/Help files, Obey
`!Boot`/`!Run`, BASIC `!RunImage`s) are stored raw. For this reason the LogFile
records the **original** size (0x24) separately from the record header's
**stored** size.

### 4.3 Compression levels (ZERO / MEDIUM / HIGH)

Static analysis shows the level has **no effect on the codec or the per-file
rule**. There is exactly one encoder (`FUN_00010ae8`) and one decoder
(`FUN_0000ca70`), both fixed 12-bit; structural similarity search on the decoder
returns only itself, and no level value is read anywhere in the compression
path. The sole caller of the encoder, `FUN_0000cb44`, decides raw-vs-compressed
purely by whether the compressed output is smaller than the input.

Consequently:

- **ZERO** — no compression (raw copy).
- **MEDIUM** and **HIGH** — equivalent: identical codec and identical per-file
  rule. A High backup's files decode with the same `FUN_0000ca70`.

The one thing static analysis cannot settle is the per-file **selection** gate,
because compression is not applied to every file. The `!Calendar.!RunImage`
(7.7 KB BASIC, `0xffb`) was stored raw even though the same 12-bit/capped LZW
would shrink it to about 71 %. So a rule (mirroring 1.05's `check_compress`,
i.e. a compressible-filetype list and/or size-and-stamp threshold) excludes
certain files before the codec. That rule appears independent of the level too,
so it behaves the same under Medium and High. **[?]** The exact rule (which
filetype list / size threshold) would need a High sample or the `ftypes`
config; it does not change the compressed format.

### 4.4 Signalling

Compression is signalled by a flag bit in the **chunk record header**, not in
the chunk content or the LogFile filetype. In the record header flags word
(`+0x14`), **bit 5** is set for a compressed record. Verified against the real
data: raw records carry `0x02` in that word, compressed records carry `0x22`.
The Restore tests this bit and routes the payload through `FUN_0000ca70` (LZW)
instead of a raw copy. The index itself is also compressed by the log-buffer
routines (`cmprssize`, `get_logbuffer`, etc.).

---

## 5. On-disk layout (real backup)

The observed backup ("30Sep" set, 14 discs, ADFS-E) has this per-disc
structure:

```
disc 1:   !Restore/       ; Restore program + resources + LogFile (initial)
          data/Chunk_1,ffd
disc 2-10:data/Chunk_1,ffd
disc 11:  !Restore/       ; complete final LogFile (81,948 B), no data chunk
disc 12-13:(blank formatted floppies)
disc 14:  data/Chunk_1,ffd
```

- Volume names follow `<date><type>__<disc>` (e.g. `30SepEI__1`, `30SepEL_10`).
  The captured discs interleave two sessions (disc 1 = `30SepEI`, discs 2–14 =
  `30SepEL`), so the naming is inconsistent across the set.
- `!Restore` (program + LogFile) appears on the **first** and **last** data
  disc. Disc 11 holds the complete final LogFile and no data; disc 1 held an
  initial LogFile plus a small chunk.
- Some discs in a set are blank formatted floppies (discs 12/13 had no data and
  carried the default ADFS volume name `09_08_Wed `).
- Every `Chunk_N` is filetype `0xffd` (Data), and each disc holds a single
  chunk blob. The `Chunk_N` number is local to the disc.
- The `LogFile` is the **master index for the whole set**, so a restore tool
  must read it (from disc 1, or the final disc) to obtain the complete list.

### 5.1 `Chunk_N` framing

A chunk is a concatenated sequence of records, each with the same form:

```
[0x18-byte record header][full original path string][0x0a LF][data]
```

- The **0x18-byte record header** precedes each path. Its fields (confirmed by
  correlating against the LogFile offsets and real data) are:
  - `+0x00` — **link**: the chunk offset of the *next* record (a forward chain);
  - `+0x04` — load address (e.g. `0xfffaff43` / `0xffffff43`);
  - `+0x08` — timestamp (identical to the LogFile timestamp field);
  - `+0x0c` — **stored** data size (the number of bytes of `data`);
  - `+0x10`, `+0x14` — flags (word 5 carries the compressed bit, per
    `FUN_0000cbe8`).
- The path is the full original path, LF-terminated, with no `_N` relabelling
  and no filetype suffix.
- Directories and files use the **same** record form. A directory record has no
  `data`; a file record's `data` is the raw content, or the LZW stream if the
  file was compressed.
- The chunk is **self-describing**: the header's stored-size (`+0x0c`) lets a
  reader delimit each record, and the link (`+0x00`) lets it walk the records in
  order. This is the function of the 0x18-byte records written by
  `FUN_0000cbe8`; it is **not** per-directory metadata as previously thought.
- Confirmed by correlating the LogFile `offset` (0x1c) with the chunk: each
  LogFile offset equals exactly the position of the record's 0x18-byte header,
  which is always 0x18 bytes before that record's path string.

### 5.2 File attributes

Filetype is stored as an **explicit value** in the LogFile (e.g. `0xfff` Text),
not encoded in a load address as in 1.05. Attributes are split between the two
places: the **LogFile** carries the origin-identifying data (filetype, original
size, timestamp, access/load-exec-ish fields), while the **chunk record header**
carries the load address, timestamp, and stored size needed to read the record
back. The timestamp appears in both and is identical.

### 5.3 LogFile records

The LogFile is a binary index (the 1.05 `names` was plain text). It is a
sequence of **fixed `0x3c` (60) byte records**, in DFS order, forming the master
index for the whole multi-disc set:

```
offset  size  field
0x00    ...   leaf filename (null-terminated; the full path is implied by the
              DFS order, since directories are serialized as their own records)
0x18    4     reserved (0)
0x1c    4     chunk offset of this record's 0x18-byte header (absolute)
0x20    4     filetype (explicit RISC OS value), or 0x1000 for a directory
0x24    4     ORIGINAL (uncompressed) file size, in bytes
0x28    4     flags (0x3 for files; larger values for dirs; see note)
0x2c    4     timestamp / date (identical to the chunk header timestamp)
0x30    4     access/exec-ish field (e.g. 0x43; or 0x04d545 dir date marker)
0x34    4     load / offset field
0x38    4     exec / offset field
```

- The `0x1c` offset is a real, absolute chunk-file offset — it points at the
  record's 0x18-byte header, and the path is always at `offset + 0x18`. It is
  **not** a running counter.
- The `0x24` size is the **original** size. The *stored* size (which is smaller
  when the file was compressed) is carried in the chunk record header, so a
  record whose stored size differs from its LogFile size is compressed.

Observed filetypes: `0xfeb` Obey (`!Boot`/`!Run`), `0xffa` Module (`MsgTrans`,
`IRQUtils`, CoCo*), `0xfff` Text (`Help`/`Messages`), `0xfec` Template, `0xff9`
Sprite, `0xffb`/`0xffc`/`0xff6`/`0xffd` Data, and `0x1000` for a directory. The
filetype is an explicit value — a different scheme from 1.05, which encoded it
in the load address.

The small records at the top of the LogFile are variable-length **preamble**
entries: a full absolute source path, a null-separated copy, and a footer naming
the volume and disc (`…_10`, `30SepEL`, `adfs::0.$`), identifying which set/disc
each payload belongs to.

### 5.4 Reconciliation with static RE

Static RE showed `0x3c`-byte records written/read at a `0x3c` stride — this is
the on-disc LogFile record size, confirmed on real media (the leaf names at
`0x8e4`, `0x920`, `0x95c`, … are each exactly `0x3c` apart). The `0x18`-byte
records written by `FUN_0000cbe8` are the **record headers inside the Chunk_N**
(each is exactly `0x18` bytes and precedes a path), not directory metadata and
not part of the LogFile. This reconciles the static RE with the observed
structures.

---

## 6. Version context

The `ReadMe` shipped with HDC 2.55 documents the lineage: 2.55 (16/06/95), 2.54,
2.53, 2.52 (image handling reworked; logfile built in memory or on disc), 2.51,
and 2.50 (19/05/94 — cannot restore backups from earlier versions, so the format
changed at or before 2.50). The `!Run` scripts brand this line as **"HDCII"**.

---

## 7. Confirmed vs open

**Confirmed:**
- Uses a Chunk/LogFile format, not `!saveset`/`_N`/`names`.
- Set layout: `data/Chunk_N` per disc, `!Restore/` (program + LogFile) on the
  first and last data disc, volume names `<date><type>__<disc>`.
- Chunk framing: each record is `[0x18-byte record header][path][0x0a][data]`;
  the header's first word is a link to the next record, and word 3 is the stored
  data size. Directories and files share the same record form (directories have
  no data). The chunk is self-describing via these headers.
- LogFile: fixed `0x3c`-byte records with a leaf filename and explicit metadata.
  The `0x1c` field is an **absolute chunk offset** of the record's header (not a
  running counter); `0x20` is the explicit filetype; `0x24` is the **original**
  (uncompressed) size; `0x2c` is the timestamp (matching the chunk header).
- Payload compression: internal 12-bit LZW applied per-file only when it shrinks
  the data (not a filetype whitelist). The LogFile records the original size; the
  chunk header records the stored size. It is HDC's own codec — neither Unix
  compress nor Squash — and imports no Compress module.
- Blank/spare formatted floppies can appear in a numbered set.
- Compression levels: Medium and High are equivalent (single fixed codec, no
  level parameter); ZERO disables compression.

**Open [?]**
- The precise meaning of the LogFile `0x30`, `0x34`, `0x38` fields and the
  directory-record `0x28`/`0x30` encoding (likely access/load-exec, but not
  isolated).
- The exact bit-packing of the 12-bit LZW code stream (the decoder reproduces
  the content, but the nibble-stuffed packing is described from disassembly and
  has not been byte-for-byte reproduced by an independent encoder).
- The `Status` file's contents (referenced by the restore messages but absent
  from this backup).

---

## Appendix A — LZW decompressor (reference implementation)

The 12-bit LZW codec is non-standard, so a restore tool cannot rely on
gzip/uncompress. The following Python is a faithful re-implementation of the
Restore's `FUN_0000ca70`, validated by decoding real backup files. It expects
the raw LZW stream (the bytes after the `[path][0x0a]` of a compressed chunk
entry) and returns the original file content.

```python
def hdc_lzw_decompress(data):
    """HDC II 12-bit LZW decoder. data = compressed payload bytes."""
    dict = []
    for i in range(0x1000):
        dict.append([0, 1, i & 0xff])      # [prev, length, byte]
    count = 0x100
    out = bytearray()
    out.append(data[0])                    # first stream byte = first output byte
    prev_code = data[0] | ((data[1] & 0xF) << 8)
    pb = 1
    alternate = True

    def emit(code):
        # reconstruct string by following prev chain 'length' times, backwards
        chars = []
        e = code
        for _ in range(dict[e][1]):
            chars.append(dict[e][2])
            e = dict[e][0]
        return bytes(reversed(chars))

    while True:
        pnext = pb + 1
        if pnext >= len(data):
            break
        alternate = not alternate
        if alternate:
            code = data[pb] | ((data[pnext] & 0xF) << 8)
            pb = pnext
        else:
            code = (data[pb] >> 4) | (data[pnext] << 4)
            pb += 2
        if code >= count:
            if count < 0x1000:
                dict.append([prev_code, dict[prev_code][1] + 1, out[0]])
                count += 1
            string = emit(code)
        else:
            string = emit(code)
        out += string
        if count < 0x1000:
            dict.append([prev_code, dict[prev_code][1] + 1, string[0]])
            count += 1
        prev_code = code
    return bytes(out)
```

Notes:
- The `prev` field uses `0` as the end-of-chain sentinel; string length is taken
  from the entry, so chains are walked exactly `length` times (mirroring the
  ARM code, which copies backwards rather than checking a sentinel).
- The dictionary is capped at `0x1000` entries and stops growing; the code width
  never widens.
