# RISC OS Character Set and Filename Encoding

## Background

RISC OS uses its own 8-bit character set — "RISC OS Latin1" — for filenames and text. It is based on ISO 8859-1 (Latin-1) but diverges significantly in the `0x80`–`0x9F` range: where ISO 8859-1 defines C1 control codes, RISC OS places printable characters, many of them typographic symbols similar to Windows CP1252, alongside a handful of RISC OS-specific GUI glyphs with no Unicode equivalent.

The `0xA0`–`0xFF` range is **identical** between RISC OS Latin1 and ISO 8859-1, which in turn maps byte-for-byte to the Unicode Latin-1 Supplement block (U+00A0–U+00FF). This covers all common accented Latin letters and punctuation (©, é, ñ, etc.).

## The Problem: Python Surrogate Escaping

On Linux, filenames are arbitrary byte sequences. Python's filesystem APIs (e.g. `os.listdir()`, `Path.rglob()`) decode filenames using the `surrogateescape` error handler (PEP 383): each byte `0xXX` that is not valid UTF-8 is mapped to the Unicode surrogate code point `U+DCXX`. For example:

- Byte `0xA0` (RISC OS hard space / non-breaking space) → Python string `\udca0`
- Byte `0xA9` (copyright sign `©`) → Python string `\udca9`
- Byte `0x80` (Euro sign `€` in RISC OS 3.5+) → Python string `\udc80`

Surrogate code points (U+D800–U+DFFF) are **not valid UTF-8**, so they cannot be stored in a UTF-8 PostgreSQL database. Attempting to do so raises:

```
UnicodeEncodeError: 'utf-8' codec can't encode character '\udca0': surrogates not allowed
```

## Current Handling

The function `sanitize_filename()` in `worker/arcworker/utils/text.py` resolves this:

1. Attempt to encode the filename as UTF-8. If it succeeds, return it unchanged (fast path for clean ASCII/UTF-8 names).
2. Otherwise, reverse the surrogate escaping: `filename.encode('utf-8', errors='surrogateescape')` recovers the original raw bytes.
3. Decode those bytes as ISO 8859-1. Since ISO 8859-1 maps every byte `0x00`–`0xFF` to the identical Unicode code point, this always succeeds and produces valid UTF-8 output.
4. Fallback chain to `cp1252` and then `errors='replace'` if step 3 somehow fails.

`sanitize_path()` wraps `sanitize_filename()`, splitting on the OS path separator and sanitising each component separately.

### What is handled correctly

Every byte in the **`0xA0`–`0xFF` range** is handled correctly, because ISO 8859-1 and RISC OS Latin1 are identical there. Examples:

| Byte | RISC OS character | Unicode result | UTF-8 bytes |
|------|-------------------|----------------|-------------|
| 0xA0 | Hard space (NBSP) | U+00A0 | C2 A0 |
| 0xA9 | © Copyright sign  | U+00A9 | C2 A9 |
| 0xC9 | É                 | U+00C9 | C3 89 |
| 0xE9 | é                 | U+00E9 | C3 A9 |

### What is handled incorrectly (but safely)

Bytes in the **`0x80`–`0x9F` range** are decoded as ISO 8859-1 C1 control codes (U+0080–U+009F), rather than their RISC OS-intended characters. This will not crash, and those code points are valid UTF-8, but the resulting characters will appear wrong in the web interface.

The correct RISC OS meanings for these bytes are:

| Byte | RISC OS meaning | Correct Unicode | Current output (ISO 8859-1) |
|------|-----------------|-----------------|-----------------------------|
| 0x80 | € Euro sign (RISC OS 3.5+) | U+20AC | U+0080 (C1 control) |
| 0x81 | Undefined (early RISC OS) | — | U+0081 |
| 0x82 | ‚ Single low-9 quotation mark | U+201A | U+0082 |
| 0x83 | Window resize icon | *(no Unicode equivalent)* | U+0083 |
| 0x84 | Window close icon | *(no Unicode equivalent)* | U+0084 |
| 0x85 | … Horizontal ellipsis | U+2026 | U+0085 |
| 0x86 | † Dagger | U+2020 | U+0086 |
| 0x87 | RISC OS-specific glyph ("87") | *(not proposed for Unicode)* | U+0087 |
| 0x88 | ← Left scroll bubble arrow | U+2190 (approx.) | U+0088 |
| 0x89 | → Right scroll bubble arrow | U+2192 (approx.) | U+0089 |
| 0x8A | ↑ Up scroll bubble arrow | U+2191 (approx.) | U+008A |
| 0x8B | ↓ Down scroll bubble arrow | U+2193 (approx.) | U+008B |
| 0x8C | Œ Latin capital ligature OE | U+0152 | U+008C |
| 0x8D | Undefined | — | U+008D |
| 0x8E | Ž Latin capital Z with caron | U+017D | U+008E |
| 0x8F | Undefined | — | U+008F |
| 0x91 | ' Left single quotation mark | U+2018 | U+0091 |
| 0x92 | ' Right single quotation mark | U+2019 | U+0092 |
| 0x93 | " Left double quotation mark | U+201C | U+0093 |
| 0x94 | " Right double quotation mark | U+201D | U+0094 |
| 0x95 | • Bullet | U+2022 | U+0095 |
| 0x96 | – En dash | U+2013 | U+0096 |
| 0x97 | — Em dash | U+2014 | U+0097 |
| 0x99 | ™ Trade mark sign | U+2122 | U+0099 |
| 0x9A | š Latin small s with caron | U+0161 | U+009A |
| 0x9B | › Single right angle quotation | U+203A | U+009B |
| 0x9C | œ Latin small ligature oe | U+0153 | U+009C |
| 0x9E | ž Latin small z with caron | U+017E | U+009E |
| 0x9F | Ÿ Latin capital Y with diaeresis | U+0178 | U+009F |

> **Note:** Bytes 0x83, 0x84, and 0x87 are RISC OS-specific glyphs (window decorations and a special glyph) with no standard Unicode equivalent. 0x87 is explicitly noted as not proposed for Unicode. Scroll arrows (0x88–0x8B) have approximate Unicode equivalents but the RISC OS glyphs are stylistically distinct "bubble arrows" specific to the RISC OS desktop.
>
> **Note on version variation:** The 0x80–0x9F mapping was extended over successive RISC OS releases. In RISC OS 2, all 0x80–0x9F bytes are undefined. The Euro sign at 0x80 was only added in RISC OS 3.5 (RiscPC era). The exact mapping for some positions may vary between RISC OS versions. The authoritative reference and a ready-to-use Python implementation are: [gerph/python-codecs-riscos](https://github.com/gerph/python-codecs-riscos).

### Practical impact of the current limitation

In practice, **these characters almost never appear in RISC OS disc image filenames**. The `0x80`–`0x9F` characters are:

- GUI widget symbols (0x83, 0x84, 0x87, 0x88–0x8B): meaningful only within the RISC OS desktop environment, never used as filename characters
- Typographic marks (0x82, 0x85–0x86, 0x91–0x97, etc.): could theoretically appear in filenames but are rare in practice

Accented letters used in Western European languages (French, German, Spanish, etc.) are all in the `0xA0`–`0xFF` range and are already handled correctly.

---

## Adding Proper RISC OS → Unicode Remapping

A correct remapping could be added with modest effort.

### What would change

`sanitize_filename()` in `worker/arcworker/utils/text.py` would need to try a RISC OS-specific decode **before** ISO 8859-1. The change is localised to that one function.

```python
# RISC OS Latin1: mapping for 0x80-0x9F (replaces C1 control code interpretation).
# 0xA0-0xFF are identical to ISO 8859-1 and need no special handling.
# None entries indicate bytes with no Unicode equivalent; they are replaced with U+FFFD.
_RISCOS_LATIN1_MAP = {
    0x80: '\u20ac',  # €
    0x82: '\u201a',  # ‚
    0x83: None,      # Window resize icon (no Unicode)
    0x84: None,      # Window close icon (no Unicode)
    0x85: '\u2026',  # …
    0x86: '\u2020',  # †
    0x87: None,      # RISC OS "87" glyph (not in Unicode)
    0x88: '\u2190',  # ← (best approximation for left scroll bubble)
    0x89: '\u2192',  # →
    0x8a: '\u2191',  # ↑
    0x8b: '\u2193',  # ↓
    0x8c: '\u0152',  # Œ
    0x8e: '\u017d',  # Ž
    0x91: '\u2018',  # '
    0x92: '\u2019',  # '
    0x93: '\u201c',  # "
    0x94: '\u201d',  # "
    0x95: '\u2022',  # •
    0x96: '\u2013',  # –
    0x97: '\u2014',  # —
    0x99: '\u2122',  # ™
    0x9a: '\u0161',  # š
    0x9b: '\u203a',  # ›
    0x9c: '\u0153',  # œ
    0x9e: '\u017e',  # ž
    0x9f: '\u0178',  # Ÿ
}

def _decode_riscos_latin1(byte_string: bytes) -> str:
    """Decode bytes using the RISC OS Latin1 character set."""
    chars = []
    for b in byte_string:
        if b < 0x80:
            chars.append(chr(b))
        elif b in _RISCOS_LATIN1_MAP:
            mapped = _RISCOS_LATIN1_MAP[b]
            chars.append(mapped if mapped is not None else '\ufffd')
        else:
            # 0xA0-0xFF: identical to ISO 8859-1 / Unicode
            chars.append(chr(b))
    return ''.join(chars)
```

`sanitize_filename()` would then call `_decode_riscos_latin1(byte_string)` instead of (or before) `byte_string.decode('iso-8859-1')`.

### Scope of the change

- **`worker/arcworker/utils/text.py`**: add the mapping table and update `sanitize_filename()`
- **`make_latin1_fspath()`**: no change needed — it works on Unicode characters in the U+0000–U+00FF range (the Latin-1 block). Characters produced by the RISC OS mapping that fall *outside* this range (e.g. U+20AC €, U+2026 …) would simply cause `make_latin1_fspath()` to return `None` (the `encode('latin-1')` step would raise `UnicodeEncodeError` for chars > U+00FF). This is correct behaviour: if the database stores `€` (U+20AC) for a file with byte `0x80` in its name, the corresponding filesystem file still has raw byte `0x80` in its name, and a surrogate-based lookup path would be needed. This edge case only affects **archive extraction lookups for files whose names contain `0x80`–`0x9F` bytes** — already an extremely rare scenario.

### Alternatively: use the existing Python codec

The third-party [gerph/python-codecs-riscos](https://github.com/gerph/python-codecs-riscos) library provides a fully-tested `riscos_latin1` codec that can be registered with Python's codec system. Using it would be as simple as:

```python
import rocodecs  # registers 'riscos-latin1' codec
decoded = byte_string.decode('riscos-latin1', errors='replace')
```

This avoids maintaining the table in-tree and benefits from any upstream corrections to the mapping, at the cost of an additional dependency.

### Priority assessment

The practical benefit is very low: characters in `0x80`–`0x9F` essentially never appear in Acorn disc image filenames. If this is ever implemented, the lookup-table approach is self-contained and carries no new dependencies.

---

## References

- [RISC OS character set — Wikipedia](https://en.wikipedia.org/wiki/RISC_OS_character_set)
- [RISC OS PRMs Volume 4, Chapter 97: Character sets](http://www.riscos.com/support/developers/prm/charsets.html)
- [RISC OS Open: Character Sets](https://www.riscosopen.org/wiki/documentation/pages/Character+Sets)
- [gerph/python-codecs-riscos — Python Codecs for RISC OS alphabets](https://github.com/gerph/python-codecs-riscos)
- [Unicode in RISC OS — riscos.info](https://www.riscos.info/index.php/Unicode_in_RISC_OS)
- PEP 383 — Non-decodable bytes in system character interfaces (Python surrogateescape)

# vim: ts=4 sw=4 noet
