# Hard Disc Companion II 2.55 — backup format

This document describes the chunk and catalogue formats used by Hard Disc
Companion II 2.55, published under the RISC Developments name. Beebug and RISC
Developments were the same company. The description combines static analysis
of Backup and Restore with binary-format validation. All multi-byte integers
are little-endian; offsets and lengths are in bytes.

The earlier format is documented in
[Hard Disc Companion 1.05](backups_hard_disc_companion_v1.md).
The 2.55 release notes date version 2.50 to 19 May 1994 and explicitly require
an older Restore application for backups made by earlier releases. Treat the
layouts below as specific to 2.55 until compatibility with another version has
been checked. The 2.06 ReadMe identifies that release as 22 October 1992.
Its Backup resources already name `data.Chunk_N` and `!Retrieve.LogFile`,
and its Other-destination template uses
`!Retrieve.data_N.name_N.Chunk_N`. This establishes the early-V2 chunk family
independently of the 2.50 release note. The exact 2.06 record layouts still
require their own compatibility check.

## 1. Storage layout

File contents are packed into `Chunk_N` files. A binary catalogue, `LogFile`,
records the directory tree, original metadata and locations of the first
fragments. A backup may span several media and chunk files. Chunk files have
RISC OS filetype `0xffd` (Data).

For floppy destinations the principal paths are:

```text
$.data.Chunk_N
$.!Restore.LogFile
$.!Restore.!RunImage
```

The copied Restore application also includes its run scripts and resources.
Locate the appropriate catalogue by backup identity; a capture filename or its
position in a directory listing does not establish its logical disc number.

The Backup message resources define these destination templates:

| Token | Template |
|---|---|
| `flopvol` | `data` |
| `floproot` | `%s::%s.$` |
| `flopsset` | `%s::%s.$.%s` |
| `flopchnk` | `%s::%s.$.%s.Chunk_%d` |
| `floprstr` | `%s::%s.$.!Restore` |
| `floplogf` | `%s::%s.$.!Restore.LogFile` |
| `remvsset` | `%s::%s.$.%s.data_%d` |
| `remvchnk` | `%s::%s.$.%s.data_%d.Chunk_%d` |
| `devrstr` | `%s::%s.$.%s.!Restore` |
| `devlogf` | `%s::%s.$.%s.!Restore.LogFile` |
| `othrrstr` | `%s.%s.!Restore` |
| `othrdata` | `%s.%s.!Restore.data_%d` |
| `othrflpe` | `%s.%s.!Restore.data_%d.name_%d` |
| `othrchnk` | `%s.%s.!Restore.data_%d.name_%d.Chunk_%d` |
| `othrlogf` | `%s.%s.!Restore.LogFile` |

Default backup names include `Full`, `Diff` and `In%s`. Restore constructs
numbered floppy labels using the set name and underscore padding. For the
Other destination, the message-resource comments describe grouping a logical
disc number by division and remainder by ten.

## 2. Chunk records

Each record has the following layout:

```text
[24-byte header][absolute RISC OS pathname][LF][stored payload]
```

Records can start at any byte offset. Read the six header words with unaligned
little-endian loads. Pathnames terminate at LF (`0x0a`); they are not padded.
Preserve pathname bytes, including non-ASCII filename characters.

### 2.1 Header

| Offset | Size | Field |
|---|---|---|
| `0x00` | 4 | Offset of the next record in this chunk, or `0xffffffff` at the end of the chunk |
| `0x04` | 4 | Original RISC OS load address |
| `0x08` | 4 | Original RISC OS execution address |
| `0x0c` | 4 | Stored payload length for a file fragment |
| `0x10` | 4 | Original access attributes |
| `0x14` | 4 | Record flags and compression method |

The execution word is also the low 32 bits of the timestamp for a stamped
file. It retains its execution-address meaning for an unstamped file.

Directory records end immediately after the pathname. Their `0x0c` field may
retain an object length such as `0x800`; it does not indicate a directory
payload. Test the directory flag before consuming payload bytes.

### 2.2 Flags

| Bits | Meaning |
|---|---|
| 0 | Continuation fragment: append to an already-started file |
| 1 | Final fragment of this file |
| 2 | Final object in the backup traversal |
| 3 | Directory record |
| 4 | Unstamped load/exec metadata |
| 5–12 | Compression method: `0` = raw, `1` = fixed 12-bit LZW |
| 13–31 | Unassigned by the examined header writer |

Thus `0x02` commonly describes a complete raw file, and `0x22` a complete
LZW-compressed file. The method is extracted as `(flags >> 5) & 0xff`.
Restore sends a nonzero method to its LZW decoder; the examined Backup emits
only methods 0 and 1. A new reader should reject unsupported method values.

### 2.3 Boundaries and file splitting

For a regular file record:

```text
payload_start = pathname_LF_offset + 1
record_end    = payload_start + stored_length
```

A directory record ends at `payload_start`. A normal next-record link points
to `record_end`. The sentinel `0xffffffff` ends the current chunk; a file may
continue in another chunk or on another medium.

Backup reads source data in blocks of at most `0xc000` bytes (48 KiB), further
limited by available buffer space. Compression and dictionary initialization
occur separately for each block. Concatenate the decoded fragments of a file
in order: start a file when bit 0 is clear, append when it is set, and complete
the file when bit 1 is set. Compare the total decoded length with the original
length in the catalogue.

Use links, lengths and flags to parse chunks. Scanning payloads for pathname
strings can mistake source-file text for archive structure.

## 3. LogFile catalogue

### 3.1 File layout

The catalogue starts with fixed-size state and workspace regions, followed by
60-byte nodes:

| Offset | Length | Purpose |
|---|---|---|
| `0x000` | `0x6d4` | Persisted backup state |
| `0x6d4` | `0x190` | Traversal workspace: 100 32-bit slots |
| `0x864` | `0x3c` per node | Linked catalogue nodes |

The initial pathname buffers contain working state and may retain bytes after
their string terminators. Interpret each field using its defined size and
termination rule.

Some header fields used by Restore are:

| Offset | Meaning |
|---|---|
| `0x000`, `0x100`, `0x200` | Working pathname buffers, each 256 bytes |
| `0x300` | Working copy of a 60-byte catalogue node |
| `0x370` | Current media name buffer |
| `0x380` | Backup set name buffer |
| `0x390` | Destination path buffer |
| `0x490` | Source path buffer |
| `0x590` | Backup configuration pathname buffer |
| `0x6a0` | Destination drive number/state; `-1` selects pathname-based access |
| `0x6a4` | Filing system number used by Restore |
| `0x6b8`–`0x6bc` | Backup creation time: five meaningful timestamp bytes |
| `0x6c0`, `0x6c2`, `0x6ca` | Backup/destination mode bytes copied into Restore |
| `0x6c9` | Catalogue-built flag, set after discovery and before data copying |
| `0x6cc` | Traversal-stack depth |
| `0x6d0` | Offset of an optional media-name string list |

The complete policy meaning of every header-state field has not yet been
mapped. The optional media-name list is read as NUL-terminated strings and
should be handled separately from the node array when present.

### 3.2 Node layout

Offsets below are relative to the **node start**, including its two links.
The name begins eight bytes into the node.

| Offset | Size | Field |
|---|---|---|
| `0x00` | 4 | Next sibling node: absolute LogFile byte offset; zero ends the sibling list |
| `0x04` | 4 | First child node: absolute LogFile byte offset; zero means no children |
| `0x08` | 16 | NUL-terminated leaf name buffer |
| `0x18` | 2 | Logical disc number of the first fragment |
| `0x1a` | 2 | Padding/unused bytes; can contain stale values |
| `0x1c` | 4 | First-fragment chunk index used by Restore's path builder |
| `0x20` | 4 | First-fragment grouping index used by Restore's path builder |
| `0x24` | 4 | Absolute byte offset of the first record header within its chunk |
| `0x28` | 2 | Signed filetype: `0x000`–`0xfff`, `0x1000` for a directory, `-1` for unstamped metadata |
| `0x2a` | 2 | Padding/unused bytes |
| `0x2c` | 4 | Original object length; for files, total uncompressed length |
| `0x30` | 1 | Access attributes |
| `0x31` | 1 | Unassigned by the examined node writer |
| `0x32` | 1 | Directory/traversal flag |
| `0x33` | 1 | Image-file flag (source object type 3) |
| `0x34` | 4 | Timestamp low word, or original load address for an unstamped object |
| `0x38` | 4 | Timestamp high byte in its low byte, or original execution address for an unstamped object |

Only read the defined width of narrow fields. Padding and unused portions of
words can retain working-memory values. Treating the filetype or attributes
as 32-bit fields produces misleading values.

The location at `0x1c` is zero-based: Restore opens `Chunk_{index + 1}`.
For removable destinations, `0x20` supplies the `data_N` directory index.
For Other destinations, the logical disc number supplies the grouping:
`data_{disc // 10}.name_{disc % 10}.Chunk_{index + 1}`. Floppy destinations use
the logical disc number to select the medium and the chunk index to select
the file.

### 3.3 Tree traversal and metadata restoration

Start at the root node at `0x864`. Follow the child link to descend into a
directory and the sibling link to visit its next peer. Join the leaf names
with RISC OS path separators. Both links address positions in `LogFile`.
The linked tree supplies parentage explicitly.

For stamped files, reconstruct the original metadata as:

```python
load = 0xfff00000 | (filetype << 8) | timestamp_high_byte
exec_address = timestamp_low_word
timestamp = (timestamp_high_byte << 32) | timestamp_low_word
```

The timestamp is the RISC OS 40-bit centisecond count since 1 January 1900.
For filetype `-1`, use the raw load/exec words at `0x34` and `0x38`.
The chunk header also stores load, exec and access attributes directly.

Byte `0xa4` in a leaf name is a filename character. Preserve it through the
[RISC OS character mapping](risc_os_character_set.md); the evidence does not
establish it as an archive escape or path-compression token. Filename suffixes
such as `T` are ordinary name bytes.

## 4. Compression and performance settings

### 4.1 Compression-selection test

**Medium and High use the same codec with different admission thresholds.**
The encoder calls a content-repetition estimator before attempting LZW.
Filetype filtering in the Filetypes window controls backup inclusion/exclusion.
The compression estimator examines the bytes of the current source block.

The shipped `!Backup.Setup` defines:

```text
cmprssize:2
perfZERO:40
perfMEDIUM:55
perfHIGH:70
```

`cmprssize` is the minimum block length in KiB for attempting compression.
The code clamps it to 0–1000 KiB; zero disables compression attempts. The
shipped minimum is 2 KiB. The Setup comments describe this as a minimum file
size, while the call path applies it to the block passed to the encoder.

The selection test is:

1. Obtain `P` from `perfZERO`, `perfMEDIUM` or `perfHIGH`, according to the
   performance-setting byte at configuration offset `0x315`. Clamp `P` to
   40–100.
2. Reject a disabled or undersized block.
3. Scan complete, aligned 32-bit words of the block. Track each word's low
   16 bits in a 65,536-bit bitmap. Count each repeated value as one hit, `R`.
4. Attempt LZW only when **`R * P > 10 * N`**, where `N` is the block length
   in bytes. The comparison is strictly greater-than.

For a word-aligned block, Medium requires repeat hits for more than about
72.7% of its words; High lowers that threshold to about 57.1%. Zero's value
of 40 cannot pass the strict test. The Setup comments recommend 55 for data
expected to compress to roughly 70% of its size, and 70 when more compression
attempts are worth the extra processing time.

High therefore attempts compression on more blocks. Both levels retain the
raw bytes when an attempted compression fails to make a useful saving. The
encoder abandons an attempt when its output pointer reaches the input-length
minus eight-byte guard, and the caller interprets a returned input length as
the raw-copy result.

### 4.2 Fixed-width LZW

- Codes are fixed at 12 bits, packed least-significant-bit first.
- The initial dictionary contains the 256 single-byte strings.
- New entries start at code 256. There are no reserved CLEAR or end codes.
- The dictionary stops growing at 4096 entries. It is reset for every block.
- The first code is a literal. An encoder can leave four or eight unused bits
  after the last complete code; the record's stored length bounds the stream.
- The `code == next_dictionary_index` case emits the previous string followed
  by its first byte (the standard LZW special case).

For two codes `a` and `b`, the three packed bytes are:

```python
byte0 = a & 0xff
byte1 = ((a >> 8) & 0x0f) | ((b & 0x0f) << 4)
byte2 = b >> 4
```

The implementation uses a 12-byte decoder dictionary node with a prefix
pointer, length and trailing byte. Those nodes are an in-memory detail.
The backup stream is headerless fixed-width LZW, so `.Z` utilities require a
different input format. Appendix A supplies a decoder for one bounded payload.

## 5. Persisted state and Status messages

Backup's `readstatus_fromlog` and `write_status_to_log` routines read and write
the first `0x6d4` bytes of `LogFile`. The reader also reads the following
400-byte traversal workspace. Restore reads the same catalogue state.

The Restore messages contain `xfindstat`, `invstatus` and `xcrtstat`, referring
to a Status file. The examined Restore executable has no literal references
to those tokens or to the filename `Status`. This establishes persisted state
inside `LogFile`; it does not establish a required separate Status file for
2.55. A separate Status-file layout remains unsubstantiated and is not needed
by the chunk/catalogue reader described here.

## 6. Media identity and catalogue completeness

### 6.1 Set names and logical disc numbers

The persisted set stem is the NUL-terminated string at LogFile `0x380`.
`0x370` holds the current medium's name; it is working state rather than a
list of every member of the set. Each node's `uint16` at `+0x18` locates the
logical disc containing that object's first fragment.

For floppy destinations Restore constructs the expected label as:

```python
def hdc2_floppy_label(stem, disc):
    digits = str(disc)
    return stem + "_" * max(0, 3 - len(digits)) + digits

assert hdc2_floppy_label("Example", 1) == "Example__1"
assert hdc2_floppy_label("Example", 10) == "Example_10"
assert hdc2_floppy_label("Example", 100) == "Example100"
```

The automatic stem generator formats day/month with `%DY%M3` and appends two
characters encoding the minute of day in base 38:

```python
alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ+_"
minute_of_day = hour * 60 + minute
time_code = alphabet[minute_of_day // 38] + alphabet[minute_of_day % 38]
```

Those two characters encode time; their letters do not select the backup
type. The short label omits the year and seconds, so label reuse and collisions
are possible. Full/incremental/differential operation state is stored separately.

Restore obtains the mounted medium's name through the filing system's
`DescribeDisc` interface, trims trailing spaces from a FileCore name, and
compares it case-insensitively with the expected label. This check uses the
name rather than the FileCore numeric disc ID. For removable destinations,
the optional media-name list at LogFile `0x6d0` supplies the expected name by
one-based logical disc number. Backup appends NUL-terminated names and retains
an extra terminating NUL.

### 6.2 Matching an unordered collection

The native records have no globally unique backup identifier or content digest.
An extractor should use the following evidence together:

1. **Candidate catalogue identity:** set stem (`0x380`), the five-byte creation
   timestamp (`0x6b8`), source path (`0x490`), and destination mode. Compare
   defined fields; scratch-buffer tails and structure padding are not identity
   data.
2. **Logical media location:** match the expected floppy label or removable
   name-list entry, then the destination-specific chunk path. Use the native
   logical number independently of the host-side capture filename.
3. **Object cross-check:** at a node's first-fragment location, require a valid
   header and a pathname matching the linked catalogue tree. Check load/exec,
   access attributes and decoded whole-file length as applicable.
4. **Duplicate handling:** collapse byte-identical alternatives for the same
   logical object only after comparison. Preserve conflicting alternatives as
   separate candidates. Reused labels alone cannot decide which copy is right.

When there is no catalogue, the self-describing chunks still permit recovery
of paths, fragment bytes and header metadata. Set assignment and original
whole-file length then have weaker evidence. Keep ambiguous or incomplete
results labelled accordingly.

### 6.3 Catalogue placement and completion

Backup builds the source catalogue **before** copying payloads. During copying,
`updatelogentry` records each object's first-fragment location. A catalogue may
therefore contain the full tree while payload copying is still incomplete.
The catalogue-built byte at `0x6c9` is set during discovery; it is not a
completed-backup marker.

At the successful end of data copying, the driver updates and closes the log,
checks available destination space, and copies the Restore application and
LogFile to the destination. For floppy/removable output the free-space check
includes the log length plus a `0x19000`-byte allowance. It can request another
medium for these files. Consequently, a complete catalogue can be on a final
log-only medium. The code does not require a copy of `!Restore` on every data
medium or prove that the first medium always contains the catalogue.

Catalogue length, capture modification time and the highest observed disc
number are insufficient measures of completeness. Prefer a candidate whose
identity, linked tree and mapped payloads validate together. If two catalogues
have matching identity fields but differing locations or object metadata,
compare the affected payload records and retain unresolved conflicts.

For each required file, validate a starting fragment, every continuation and a
final fragment. Compare the total decoded length with node `+0x2c`. Chunk-end
sentinel `0xffffffff` changes chunks/media; it does not by itself complete a
file or the backup. Bit 2 marks the last object selected by the backup walk:
for a file, its final-fragment bit must also be reached. A missing last-object
record, unresolved catalogue location or missing continuation leaves the set
incomplete. Recoverable complete files can still be exported separately.

This format does not cryptographically authenticate media or payloads. If
labels, catalogue identity and object-level checks all collide, the correct
choice between conflicting captures requires external provenance.

## 7. Static-analysis anchors

Addresses refer to the decompressed executable AIFs loaded with their headers
at `0x8000`. The first post-header code is at `0x8080`.

| Program | Address | Role |
|---|---|---|
| Backup | `0x0000a90c` | Parses ZERO/MEDIUM/HIGH and stores the selected level |
| Backup | `0x0001090c` | Reads `perf%s` and `cmprssize`; applies repetition threshold |
| Backup | `0x00010ae8` | LZW encoder, including preflight test and output-size guard |
| Backup | `0x0000cb44` | Chooses compressed output or raw bytes |
| Backup | `0x0000c728` | Builds chunk metadata and control flags |
| Backup | `0x0000cbe8` | Writes record header, pathname and payload; patches link and length |
| Backup | `0x0000c9a4` | Reads source blocks, limited to `0xc000` bytes |
| Backup | `0x0000f6d0` | Updates a node's first-fragment location |
| Backup | `0x0000f7bc` | Walks sibling/child links during backup |
| Backup | `0x0000e908`, `0x0000e9dc` | Reads/writes persisted catalogue state |
| Backup | `0x0000e670` | Generates the day/month and base-38 time stem |
| Backup | `0x0000f478`, `0x0000f65c` | Builds the catalogue and sets its built flag |
| Backup | `0x000105f4` | Appends removable-media names to LogFile |
| Backup | `0x0000d378`, `0x0000d40c` | Checks space and copies the final log/Restore application |
| Backup | `0x00014664`–`0x0001477c` | End-of-copy driver, log update and optional extra medium |
| Restore | `0x0000c380` | Resolves path components through the linked catalogue |
| Restore | `0x0000c5c8` | Opens the catalogue; establishes root offset `0x864` |
| Restore | `0x000091d4` | Copies catalogue location fields into restore state |
| Restore | `0x00008590` | Constructs floppy, removable and Other chunk paths |
| Restore | `0x0000ebb8`, `0x0000c8b8` | Computes expected floppy label or reads removable name list |
| Restore | `0x00011a34`, `0x0000eb38`–`0x0000eb84` | Reads mounted media name and checks it against the expected name |
| Restore | `0x00008cb8` | Seeks to the header, reads 24 bytes, then reads the pathname |
| Restore | `0x00008b00` | Reads the stored payload, decompresses and writes/appends |
| Restore | `0x0000ca10`, `0x0000ca70` | Initializes the dictionary and decodes LZW |
| Restore | `0x00008540` | Restores load, exec and access metadata |

The layouts and decoder have been checked against linked catalogue traversal,
chunk boundaries, reconstructed metadata, complete-file lengths and files
assembled from multiple fragments. Archive payloads and their identifying
details are excluded from this document.

Remaining work is confined to unassigned persisted-state fields and broader
validation of less-used destination modes, plus a separate record-layout
analysis for early V2 builds. The 2.55 media-identity rules, catalogue placement,
record offsets, grouping rules, flags, metadata and LZW packing are resolved
to the extent represented by this build. Ambiguous provenance and missing data
cannot be repaired solely from the on-disc structures.

## Appendix A — bounded LZW decoder

Pass exactly the stored payload of one compressed file fragment. Start a fresh
decoder for every fragment. `max_output` provides a caller-selected output
limit; `expected_size` is optional for fragments whose decoded length is known.
For a split file, validate the concatenated length against the catalogue.

```python
def hdc_lzw_decompress(data, *, max_output, expected_size=None):
    """Decode one HDC II fixed-12-bit LZW payload."""
    if max_output < 0:
        raise ValueError("negative output limit")
    if not data:
        if expected_size not in (None, 0):
            raise ValueError("empty payload has unexpected length")
        return b""
    if len(data) < 2:
        raise ValueError("truncated first code")

    def codes():
        # Complete 12-bit codes only; trailing padding bits are unused.
        for bit in range(0, len(data) * 8 - 11, 12):
            pos = bit // 8
            word = int.from_bytes(data[pos:pos + 3], "little")
            yield (word >> (bit % 8)) & 0xfff

    stream = iter(codes())
    first = next(stream)
    if first >= 256:
        raise ValueError("first code must be a literal")
    table = [bytes([i]) for i in range(256)]
    previous = table[first]
    output = bytearray(previous)
    if len(output) > max_output:
        raise ValueError("output limit exceeded")

    for code in stream:
        if code < len(table):
            entry = table[code]
        elif code == len(table) and len(table) < 4096:
            entry = previous + previous[:1]
        else:
            raise ValueError("invalid LZW dictionary reference")
        if len(output) + len(entry) > max_output:
            raise ValueError("output limit exceeded")
        output.extend(entry)
        if len(table) < 4096:
            table.append(previous + entry[:1])
        previous = entry

    if expected_size is not None and len(output) != expected_size:
        raise ValueError("decoded length mismatch")
    return bytes(output)
```

Synthetic examples exercise the literal, dictionary and special-case paths:

```python
assert hdc_lzw_decompress(bytes.fromhex("4100"), max_output=1) == b"A"
assert hdc_lzw_decompress(bytes.fromhex("410010"), max_output=3) == b"AAA"
assert hdc_lzw_decompress(bytes.fromhex("4120040001"), max_output=4) == b"ABAB"
```
