# RISC OS Character Set and Filename Encoding

## Background

RISC OS uses its own 8-bit character set — "RISC OS Latin1" — for filenames and text. It is based on ISO 8859-1 (Latin-1) but diverges in the `0x80`–`0x9F` range: where ISO 8859-1 defines C1 control codes, RISC OS places printable characters. These include typographic symbols, Welsh-specific letters added by Acorn for UK language support, and a handful of RISC OS-specific GUI glyphs with no standard Unicode equivalent.

The `0xA0`–`0xFF` range is **identical** between RISC OS Latin1 and ISO 8859-1, which in turn maps byte-for-byte to the Unicode Latin-1 Supplement block (U+00A0–U+00FF). This covers all common accented Latin letters and punctuation (©, é, ñ, etc.).

## The Problem: Python Surrogate Escaping

On Linux, filenames are arbitrary byte sequences. Python's filesystem APIs decode filenames using the `surrogateescape` error handler (PEP 383): each byte `0xXX` that is not valid UTF-8 is mapped to the Unicode surrogate code point `U+DCXX`. For example:

- Byte `0xA0` (RISC OS hard space / non-breaking space) → Python string `\udca0`
- Byte `0xA9` (copyright sign `©`) → Python string `\udca9`
- Byte `0x81` (RISC OS Ŵ Welsh W with circumflex) → Python string `\udc81`

Surrogate code points (U+D800–U+DFFF) are not valid UTF-8, so they cannot be stored in a UTF-8 PostgreSQL database, and they cannot be passed as arguments to external tools (such as riscosarc, a Java program that expects UTF-8).

## How We Handle It: Normalise at Extraction Time

The correct fix is to rename extracted files from their raw-byte names to their correct Unicode equivalents **immediately after extraction**, before any downstream code sees the filenames. This eliminates all surrogate-escape issues in one place.

`normalize_extracted_filenames(root)` in `worker/arcworker/utils/text.py` performs this walk. It is called:

- Inside `extract_acorn_disc_image_manager()` after DiscImageManager finishes
- Inside `extract_riscosarc()` and `extract_tbafs()` after archive extraction

After normalisation:

- All filenames in the output directory are valid UTF-8
- The database stores the correct Unicode string
- `Path(name).exists()` returns True directly — no conversion required
- External tools that need a path (riscosarc, etc.) receive a valid UTF-8 string

The walk is bottom-up (`os.walk(topdown=False)`) so directory entries are only renamed after their contents, keeping all path operations consistent.

## The RISC OS Latin1 → Unicode Mapping

`decode_riscos_latin1(data: bytes) -> str` and `encode_riscos_latin1(text: str) -> bytes` in `utils/text.py` implement the full codec.

### Ranges that are straightforward

| Range | Treatment |
|-------|-----------|
| `0x00`–`0x7F` | ASCII — code point equals byte value |
| `0xA0`–`0xFF` | Identical to ISO 8859-1 / Unicode Latin-1 Supplement |

### The `0x80`–`0x9F` C1 range

| Byte | Unicode | Character | Notes |
|------|---------|-----------|-------|
| 0x80 | U+20AC | € | Euro Sign (added RISC OS 3.5+; undefined in earlier versions) |
| 0x81 | U+0174 | Ŵ | Latin Capital Letter W with Circumflex (Welsh) |
| 0x82 | U+0175 | ŵ | Latin Small Letter W with Circumflex (Welsh) |
| 0x83 | U+25F0 | ◰ | White Square with Upper Left Quadrant — window resize icon (nearest geometric approximation; [Wikipedia](https://en.wikipedia.org/wiki/RISC_OS_character_set)) |
| 0x84 | U+1FBC0 | 🯀 | White Heavy Saltire with Rounded Corners — window close icon (Unicode "Symbols for Legacy Computing" block, added Unicode 13.0) |
| 0x85 | U+0176 | Ŷ | Latin Capital Letter Y with Circumflex (Welsh) |
| 0x86 | U+0177 | ŷ | Latin Small Letter Y with Circumflex (Welsh) |
| 0x87 | U+E087 | (PUA) | RISC OS "87 glyph" (subscript-8 superscript-7 ligature, not proposed for Unicode); mapped to a Private Use Area code point |
| 0x88 | U+2190 | ← | Leftwards Arrow (left scroll bubble — best standard approximation) |
| 0x89 | U+2192 | → | Rightwards Arrow (right scroll bubble) |
| 0x8A | U+2191 | ↑ | Upwards Arrow (up scroll bubble) |
| 0x8B | U+2193 | ↓ | Downwards Arrow (down scroll bubble) |
| 0x8C | U+2026 | … | Horizontal Ellipsis |
| 0x8D | U+2122 | ™ | Trade Mark Sign |
| 0x8E | U+2030 | ‰ | Per Mille Sign |
| 0x8F | U+2022 | • | Bullet |
| 0x90 | U+2018 | ' | Left Single Quotation Mark |
| 0x91 | U+2019 | ' | Right Single Quotation Mark |
| 0x92 | U+2039 | ‹ | Single Left-Pointing Angle Quotation Mark |
| 0x93 | U+203A | › | Single Right-Pointing Angle Quotation Mark |
| 0x94 | U+201C | " | Left Double Quotation Mark |
| 0x95 | U+201D | " | Right Double Quotation Mark |
| 0x96 | U+201E | „ | Double Low-9 Quotation Mark |
| 0x97 | U+2013 | – | En Dash |
| 0x98 | U+2014 | — | Em Dash |
| 0x99 | U+2212 | − | Minus Sign (distinct from HYPHEN-MINUS U+002D) |
| 0x9A | U+0152 | Œ | Latin Capital Ligature OE |
| 0x9B | U+0153 | œ | Latin Small Ligature OE |
| 0x9C | U+2020 | † | Dagger |
| 0x9D | U+2021 | ‡ | Double Dagger |
| 0x9E | U+FB01 | ﬁ | Latin Small Ligature FI |
| 0x9F | U+FB02 | ﬂ | Latin Small Ligature FL |

### Bijectivity

The mapping is bijective: every target code point in the table is distinct, and none fall within either the ASCII range (U+0000–U+007F) or the Latin-1 Supplement (U+00A0–U+00FF). There is therefore no collision between the three ranges, and `encode_riscos_latin1()` can invert `decode_riscos_latin1()` losslessly for any string that originated from RISC OS bytes.

The only caveat is U+E087 (the PUA entry for 0x87), which has no standardised rendering in any font. It round-trips correctly but will display as a placeholder glyph in the web interface.

### Version variation

The `0x80`–`0x9F` mapping was extended across RISC OS releases:

- **RISC OS 2**: all of `0x80`–`0x9F` are undefined
- **RISC OS 3.0**: `0x80`–`0x8B` are undefined; the remaining positions are as in the table above
- **RISC OS 3.5+**: `0x80` = Euro sign (€) added

Discs imaged from RISC OS 2 systems may contain filenames where bytes in the `0x80`–`0x9F` range were used informally (e.g. hard-coded by specific applications). The codec will still decode them; the character displayed may not match what the original RISC OS application intended for those undefined positions.

## Adding Support for Other Filesystems

`normalize_extracted_filenames()` accepts an optional `decoder` argument:

```python
def normalize_extracted_filenames(
    root: Path,
    decoder: Callable[[bytes], str] = decode_riscos_latin1,
) -> None: ...
```

When adding support for DOS/Windows disc images with CP437 or CP850 filenames, implement a `decode_cp437(data: bytes) -> str` function and pass it as `decoder=decode_cp437` in the relevant extraction function. The rest of the pipeline — bottom-up walk, collision handling, OS-level rename — remains identical.

## `sanitize_filename()` and `make_latin1_fspath()`

`sanitize_filename()` is still used for non-path strings (e.g. command strings in log output) and as a fallback for any path that was not processed by `normalize_extracted_filenames()`. It now uses `decode_riscos_latin1` as its primary codec.

`make_latin1_fspath()` is retained for backward compatibility: analyses run before normalisation was introduced stored surrogate-decoded ISO 8859-1 Unicode paths in the database, and need the reverse conversion to locate files on disk. It is not needed for any analysis run after this fix was deployed.

## References

- [RISC OS character set — Wikipedia](https://en.wikipedia.org/wiki/RISC_OS_character_set)
- [RISC OS PRMs Volume 4, Chapter 97: Character sets](http://www.riscos.com/support/developers/prm/charsets.html)
- [RISC OS Open: Character Sets](https://www.riscosopen.org/wiki/documentation/pages/Character+Sets)
- [gerph/python-codecs-riscos — Python Codecs for RISC OS alphabets](https://github.com/gerph/python-codecs-riscos)
- [Unicode in RISC OS — riscos.info](https://www.riscos.info/index.php/Unicode_in_RISC_OS)
- [U+1FBC0 — Symbols for Legacy Computing](https://codepoints.net/U+1FBC0)
- PEP 383 — Non-decodable bytes in system character interfaces (Python `surrogateescape`)

# vim: ts=4 sw=4 noet
