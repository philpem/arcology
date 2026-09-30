# Hard Disc Companion 1.05 — backup format

This document describes Beebug Hard Disc Companion 1.05, including the
Backup/Restore 0.90 applications. The layout is established from their BASIC
programs and binary-format checks. Byte offsets and lengths are in bytes;
multi-byte payload integers are little-endian.

The later chunk-format family is introduced in
[Hard Disc Companion II](backups_hard_disc_companion_v2.md). Software version
2.06 already uses chunks and a binary catalogue; version 2.50 introduces a
further restore-compatibility break. Recognise the container layout before
choosing a parser.

## 1. Media layout

Each medium carries an independent `!saveset` application directory:

```text
!saveset/
  !Run
  !Sprites
  emptydirs              # first medium
  FILES/
    names
    _1
    _2_Z
    ...
    FILES/               # another group, as needed
      names
      _1
      ...
  BIGFILES/              # large-file phase, as needed
    names
    _1a
    ...

logfile                  # root of the final medium
```

The container uses ordinary filesystem directories and files. Keep their
RISC OS load address, execution address and access attributes when extracting
the media image. FileCore map/directory geometry belongs to the enclosing
filesystem; use a filesystem-aware reader to obtain the files and metadata.

Ordinary files are written first. The large-file phase follows, and may begin
on a medium that also contains ordinary files. Backup finally writes the
textual root `logfile`, allocating another medium if it needs room.

## 2. Ordinary files: `FILES`

Each `FILES` directory holds up to 75 data entries. Its `names` file contains
one relative source pathname per LF-terminated line. Line 1 identifies `_1`,
line 2 identifies `_2`, and so on. A compressed entry uses the corresponding
`_N_Z` filename.

When a group fills, Backup creates a child directory also named `FILES`, gives
it its own `names` file, and resets numbering to 1. Numbering also resets on a
new medium. Walk each medium's `FILES/FILES/...` chain in order. The 75-entry
limit applies to each group; no nesting limit was found in this path.

Every `_N` or `_N_Z` is a regular file. Recreate destination directories from
the pathnames and `emptydirs`. A Spark or other archive backed up as `_N`
remains one file; extraction of its contents is a separate operation.

### 2.1 Payloads

| Filename | Contents |
|---|---|
| `_N` | Original file bytes |
| `_N_Z` | Four-byte original length followed by a Unix `compress` stream |

The numbered filename associates a payload with its `names` line. Preserve
that association independently for each `FILES` directory.

### 2.2 Compression

```text
+0x00  uint32 LE    original file length
+0x04  bytes        Unix compress stream: 1f 9d 8d ...
```

The observed stream uses block-mode Unix LZW with a maximum width of 13 bits.
The low five bits of the flags byte encode maximum code width; bit 7 enables
block mode. Standard `.Z` readers handle the stream after the four-byte HDC
length prefix has been removed. Validate the decoded length against that
prefix. Standard `compress` code packing includes its width-change and CLEAR
alignment rules, so use a compatible implementation.

For example, this Python wrapper invokes an installed `.Z`-capable gzip:

```python
import struct
import subprocess

def decode_hdc1_file(payload):
    if len(payload) < 7 or payload[4:6] != b"\x1f\x9d":
        raise ValueError("invalid HDC compressed file")
    expected = struct.unpack_from("<I", payload)[0]
    decoded = subprocess.run(
        ["gzip", "-cd"], input=payload[4:], capture_output=True, check=True
    ).stdout
    if len(decoded) != expected:
        raise ValueError("decoded length mismatch")
    return decoded
```

Backup's `PROCcheck_compress` uses its `Resources.ftypes` list, skips files
of at most 1024 bytes, and requires stamped metadata. It configures the
David Pilling/BEEBUG Compress module using `CSETBITS 13`, `CSETMEM` and
`CSETFILEBUFF`, then uses its copy/compression commands. These requirements
describe the ordinary-file path.

## 3. Empty directories and metadata

The first medium's `!saveset.emptydirs` contains LF-terminated relative paths
for empty directories. Its final line is:

```text
### <total-files>
```

Restore uses the number for its progress display. It is the total file count
for the operation, independently of the number of empty-directory lines.

Each stored data file carries the original load/exec and access metadata in
its filesystem directory entry. For a stamped load address:

```python
if load_address & 0xfff00000 == 0xfff00000:
    filetype = (load_address >> 8) & 0xfff
```

Retain raw load/exec values for unstamped files. PC-side filetype suffixes
alone are insufficient to preserve every timestamp and execution address;
use metadata sidecars when exporting to an ordinary host filesystem.

## 4. Large files: `BIGFILES`

### 4.1 Selection and write order

In Backup 0.90, `MAXSIZE` is 745,472 bytes. `PROCcopy` places a source file
larger than that threshold in a deferred large-file list **before** calling
the ordinary-file compression test. `PROCbackup` finishes the ordinary-file
passes and then invokes `PROCcopy_big_files`.

The large-file writer reads directly from the source handle and writes raw
bytes using `OS_GBPB`. Its fragments contain consecutive slices of the source
file. This path adds no length prefix and does not invoke the compressor.
Reassembly is byte concatenation.

### 4.2 Fragment names and pathname mapping

```text
!saveset.BIGFILES.names
!saveset.BIGFILES._<file-number><part-character>
```

- File numbers start at **1 for the large-file phase** and continue across
  media. They are independent of the ordinary `FILES` counters.
- Each source file starts with part character **`a`**. The next fragment uses
  `CHR$(ASC(part$) + 1)`; `_1a`, `_1b`, `_1c` are consecutive fragments.
- The writer increments a single character. It does not implement `z` to
  `aa` rollover. An extractor should preserve this byte-wise rule rather than
  assume an alphabetical numbering convention beyond `z`.
- Each fragment adds one LF-terminated pathname to that medium's
  `BIGFILES.names`. A continuing file therefore repeats its pathname on the
  next medium. The line number is local to that names file; the numeric part
  of `_Na` remains global across the large-file phase.

Synthetic example:

```text
medium SET_4:
  BIGFILES.names:  Work.LargeData\n
  BIGFILES._1a

medium SET_5:
  BIGFILES.names:  Work.LargeData\nWork.OtherData\n
  BIGFILES._1b
  BIGFILES._2a

medium SET_6:
  BIGFILES.names:  Work.OtherData\n
  BIGFILES._2b
  $.logfile
```

Restore carries `count%`, `part$` and `previous_file$` across media. The same
pathname advances the part character; a different pathname increments the
global file number and resets the part to `a`. It opens part `a` with
`OPENOUT` (replace/create), and subsequent parts with `OPENUP`, seeking to the
output file's end before writing.

### 4.3 Fragment boundaries

The transfer buffer is 4096 bytes. Backup copies full buffers while the
medium's free space is greater than 4096 bytes, using a shorter final read
when the remaining source data fits. Before starting another fragment it
requests a new medium if free space is below `10000 + 4096` bytes or the
preceding copy requested a medium change.

Use the fragment files' actual lengths. Their boundaries depend on free space
and filesystem allocation. A file above `MAXSIZE` can still fit as a single
`a` fragment when enough space is available. The ordinary 75-entry nesting
rule is not implemented in the `BIGFILES` writer.

### 4.4 Attributes, restarts and completeness

After writing a fragment, `PROCtouch` copies the source file's load address,
execution address and access attributes to the fragment's directory entry.
Restore reapplies those values after appending each part. There is no embedded
fragment header, original-length field, checksum or explicit last-part flag.

The copy/retry logic can restart a source file with part `a`. Restore first
tries the expected fragment, for example `_1c`; if opening it fails, it tries
the same numeric identifier with part `a`, `_1a`. A successful fallback resets
the output rather than appending to it. An extractor should report such a
restart and retain provenance for the abandoned partial output.

A new pathname in the ordered names stream completes the preceding large
file. The final file is completed by reaching the final medium, identified by
the root `logfile`. Backup writes human-readable `<START>`, `<CONTINUED>` and
`<END>` annotations there. These are useful corroboration; part `a` is always
logged as `<START>`, even if it holds the whole file.

The format supplies no independent original size for a large file. A complete
fragment sequence and final-medium marker establish structural completeness,
but cannot detect truncation within an otherwise well-formed last fragment.
Missing fragments or an absent final-medium marker must remain visible in
the extraction report. The original Restore disables disc skipping during
the large-file phase.

## 5. Media identity and final-disc detection

Backup names media as:

```text
<user-chosen-backup-name>_<decimal-disc-number>
```

The source comments specify a seven-character backup-name maximum, leaving
room for the underscore and two-digit number within the ten-character disc
label. Restore accepts disc 1 initially, strips the trailing decimal digits
to retain the common prefix, and requests subsequent numbered media using
that prefix. Order by logical labels, numerically.

The root `logfile` is written after both data phases finish. If it cannot fit,
Backup requests an additional medium before copying it. Its presence is the
Restore application's final-disc test. It also contains a source/date heading
and per-disc headings; these are useful for separating captures with reused
user-chosen labels.

The ordinary-file restore loop ends when it encounters `BIGFILES` or the root
`logfile`. When it encounters `BIGFILES`, it starts the large-file loop on the
**same medium**. That loop then continues until the final-medium marker is
found. A log-only final medium must be allowed.

For unordered captures:

1. Identify `!saveset`, normalise filesystem label padding, and split each label
   at its final underscore followed by decimal digits.
2. Group candidate media by the retained prefix and order by disc number.
3. Correlate `logfile` source/date and per-disc headings where available.
   Labels are user-chosen and are not globally unique identifiers.
4. Preserve alternatives when equal labels contain different files. Exact
   duplicate captures can be deduplicated after content comparison.
5. Walk ordinary `FILES` groups, then process the ordered `BIGFILES.names`
   stream with one persistent large-file counter and part state.
6. Check expected fragment names and media gaps. A later fragment without its
   earlier parts is recoverable data, but it is not a complete original file.

## 6. Source anchors and limits

Line numbers refer to the tokenised BASIC programs shipped as Backup/Restore
0.90. They identify the code paths without depending on any private image.

| Program | Routine or lines | Evidence |
|---|---|---|
| Backup | 8470, 8580–8590 | Large-file and free-space thresholds |
| Backup | 9280–9300 | Global large-file number and initial part |
| Backup | `PROCcopy`, 16520–16660 | Large-file selection precedes compression test |
| Backup | `PROCcopy_big_files`, 17290–18240 | Deferred order, part progression, media changes |
| Backup | `PROCbigfile_copy`, 18280–18880 | Fragment filename, per-fragment names line and text-log annotations |
| Backup | `PROCtouch`, 18910–19020 | Copies load/exec and attributes |
| Backup | `PROCcopy_bytes`, 19050–19670 | Raw buffered byte transfer |
| Backup | 9540–9690 | Root `logfile` placement after the backup |
| Restore | `PROCrebuild`, 8850–9300 | Transition from ordinary files to large files |
| Restore | `PROCbig_rebuild`, 12980–13340 | Persistent file counter and final-medium test |
| Restore | `PROCdo_bignamefile`, 13370–13780 | Pathname-to-fragment mapping |
| Restore | `PROCcopy_bytes`, 13810–14530 | Append, restart-at-`a`, metadata restoration |

The large-file framing is source-confirmed. Independent round-trip tests can
exercise the naming and reassembly rules with synthetic fragments; validation
against original file contents is still required when assessing a particular
damaged or incomplete backup. The format itself has no large-file integrity
checksum or authoritative total byte count.
