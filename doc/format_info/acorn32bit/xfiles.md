# X-Files Archive Format

X-Files is a chunk-based archive format for RISC OS, written by Andy Armstrong. It stores files with long filenames (up to 256 characters) and full RISC OS metadata (load/exec addresses, attributes). The format is essentially a mini-filesystem: a fixed header locates a chunk table, and directory and file data are stored as numbered chunks.

X-Files archives use RISC OS filetype `&B23` and commonly appear with the leaf name in the form `MyArchive,B23` when stored on non-RISC OS systems that encode the filetype as a comma-separated suffix. The extension `.b23` is also used on some systems.

All integers are 32-bit little-endian unless otherwise noted.

---

## File Header

The file header is exactly 52 bytes at offset 0.

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| `0x00` | 4 | `magic` | `58 46 49 4C` — ASCII `XFIL` |
| `0x04` | 4 | `hdrSize` | Always `52` (0x34) |
| `0x08` | 4 | `structureVersion` | Always `1` |
| `0x0C` | 4 | `directoryVersion` | Always `1` |
| `0x10` | 16 | `chunkTable` | Chunk descriptor for the chunk table itself (see below) |
| `0x20` | 4 | `rootChunk` | Chunk number of the root directory |
| `0x24` | 4 | `allocationUnit` | Granularity used when allocating chunk space (bytes) |
| `0x28` | 4 | `freeChunk` | Chunk number of the first free chunk (or 0 if none) |
| `0x2C` | 4 | `waste` | Bytes of wasted (fragmented free) space in the archive |
| `0x30` | 4 | *(reserved)* | Present in the original C struct; purpose unknown. Treat as padding. |

---

## Chunk Descriptor (`xFiles_chunk`)

Used in the file header at offset `0x10` to locate the chunk table, and repeated for every entry in the chunk table itself.

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| `+0x00` | 4 | `offset` | Absolute file offset of the chunk's data |
| `+0x04` | 4 | `size` | Used byte count within the allocated space |
| `+0x08` | 4 | `usage` | Identifies free chunks: `b"FREE"` (LE uint32 `0x45455246`) = free slot. Any other value means the chunk is in use. |
| `+0x0C` | 4 | `allocSize` | Total allocated byte count (≥ `size`) |

---

## Chunk Table

Located at the absolute file offset given by `header.chunkTable.offset`. Its byte length is `header.chunkTable.size`; the number of chunks is `size / 16`.

The chunk table is itself chunk 0 (the first entry in the table describes the table's own storage). Each entry is one 16-byte `xFiles_chunk` record in sequential order. Free slots have `usage == 0x45455246` (`b"FREE"`).

To read chunk *n*:
1. Look up entry *n* in the chunk table to get its `offset` and `size`.
2. Verify `usage != FREE`.
3. Seek to `offset` and read `size` bytes.

---

## Directory Chunk

A directory is stored as a single chunk. Its payload has the following structure:

### Directory Header (16 bytes)

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| `+0x00` | 4 | `sig` | `41 6E 64 79` — ASCII `Andy` |
| `+0x04` | 4 | `parent` | Chunk number of the parent directory (undefined for root) |
| `+0x08` | 4 | `size` | Hash table capacity (number of slots) |
| `+0x0C` | 4 | `used` | Number of active entries in the hash table |

### Hash Table

Immediately follows the directory header. Contains `size` entries of 12 bytes each; only the first `used` entries are active.

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| `+0x00` | 4 | `nameStart` | First 4 bytes of the filename, for fast hash comparison |
| `+0x04` | 4 | `entryPos` | Byte offset within the directory chunk where the `xFiles_dirEntry` record begins |
| `+0x08` | 4 | `node` | Chunk number of the file's data (or child directory's chunk) |

### Directory Entry (`xFiles_dirEntry`)

Each active hash table entry points to a variable-length directory entry within the same chunk payload. Entries are packed with no gaps except for 4-byte padding at the end of each filename.

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| `+0x00` | 4 | `load` | RISC OS load address |
| `+0x04` | 4 | `exec` | RISC OS exec address |
| `+0x08` | 4 | `size` | File size in bytes |
| `+0x0C` | 4 | `attr` | RISC OS attributes (see below) |
| `+0x10` | 4 | `nameLen` | Length of the filename in bytes (excluding the NUL terminator) |
| `+0x14` | nameLen+1 | `name` | Filename in RISC OS Latin-1, NUL-terminated, padded to the next 4-byte boundary |

---

## Attributes

The `attr` field is a 32-bit word. Bit 8 is the directory flag:

| Bit | Meaning |
|-----|---------|
| 8 | Set if the entry is a directory (the `node` field is a directory chunk, not a file chunk) |

The low byte contains the standard RISC OS access bits:

| Bit | Meaning |
|-----|---------|
| 0 | Owner read |
| 1 | Owner write |
| 3 | Locked |
| 4 | Public read |
| 5 | Public write |

---

## RISC OS Filetype and Timestamp

RISC OS uses a date-stamped load/exec encoding. When `(load >> 20) == 0xFFF`:

- **Filetype** = bits 19–8 of `load` = `(load >> 8) & 0xFFF` (12-bit hex value, e.g. `0xFAF` for `Sprite`)
- **Timestamp** is a 40-bit centisecond count from the RISC OS epoch (1 Jan 1900 00:00:00 UTC):
  - High 32 bits = `exec`
  - Low 8 bits = `load & 0xFF`
  - Combined: `timestamp_cs = (exec << 8) | (load & 0xFF)`

When `(load >> 20) != 0xFFF`, the load and exec values are raw addresses with no embedded timestamp or filetype.

---

## File Data Chunk

File data is stored as a raw byte sequence in a single chunk. The chunk's `size` field gives the exact byte length. There is no additional structure; read `size` bytes from the chunk's `offset` to obtain the file content.

---

## Notes

- **Recursion depth**: Directory structures can theoretically nest arbitrarily deep. Implementations should impose a sanity limit to guard against malformed or adversarial archives.
- **Chunk 0**: The chunk table is chunk 0. The root directory is typically chunk 1 and the first file chunk is chunk 2, but this is not guaranteed.
- **Free chunks**: The chunk table may contain free (deallocated) slots with `usage == FREE`. These must not be dereferenced as file or directory data.
- **Undocumented field at 0x30**: The original C struct has a uint32 at offset `0x30` not described in the format documentation. It appears to be padding or a reserved field. Parsers should read past it without interpreting it.

# vim: ts=4 sw=4 et
