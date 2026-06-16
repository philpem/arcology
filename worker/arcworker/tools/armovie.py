"""
Acorn Replay / ARMovie (RISC OS filetype &AE7) header parser.

ARMovie files begin with a 21-line plain-text header followed by the payload
(video + sound chunks) and, located by byte offsets in the header, a text
catalogue, a poster sprite and an optional key-frame table.

This parser reads the text header and (best-effort) the chunk catalogue to
produce the human-readable metadata and a few derived statistics.  It does NOT
decode the video/sound bitstreams.

No external dependencies — stdlib only.

Reference: the ARMovie / AE7 container specification (21-line header; numeric
lines carry a leading token optionally followed by descriptive prose, so only
the leading token is read).
"""

import io
import logging
import os
import re

log = logging.getLogger(__name__)

# The header is exactly 21 newline-terminated lines.
_HEADER_LINES = 21

# Bounded reads so a multi-GB movie is never loaded whole: the 21-line text
# header lives at the very start, and only a capped window of the catalogue is
# scanned for the sound-track count.
_HEADER_READ_BYTES = 64 * 1024
_CATALOGUE_READ_BYTES = 4 * 1024 * 1024

# Line 1 must be this literal magic (exact match).
_MAGIC = 'ARMovie'

# Leading optional sign + digits (VAL / strtoull(…, 10) style: parse the number
# at the start and stop at the first non-digit, so e.g. "1K" → 1).
_LEADING_DIGITS_RE = re.compile(r'\s*([+-]?\d+)')

# Generic descriptor some writers append after the video-format number
# (e.g. "19 video format", analogous to "160 pixels").  It is boilerplate, not a
# codec name, so it is dropped from the captured label.
_VIDEO_FORMAT_DESCRIPTOR = 'video format'


class ArmovieParseError(Exception):
    """Raised when the data is not a valid ARMovie file."""


def _leading_int(line: str) -> int | None:
    """Return the integer at the start of *line*, ignoring any trailing text.

    Numeric header lines are written as ``<number> <descriptive prose>`` (e.g.
    ``160 pixels``) and some values carry an attached suffix (e.g. ``1K``).
    Parsing is VAL/``strtoull(…, 10)`` style: read the leading optional sign and
    digits and stop at the first non-digit.  Returns None when no leading number
    is present.
    """
    m = _LEADING_DIGITS_RE.match(line)
    if not m:
        return None
    return int(m.group(1))


def _parse_codec(line: str) -> tuple[int | None, str | None]:
    """Parse a codec line into its number and (optional) name/label.

    The video-format field is ``<number><label>`` where the number is parsed
    VAL-style (leading digits) and the remainder is the codec name as written:

    * ``1K``             → ``(1, "1K")``          (attached suffix → whole token)
    * ``1 Moving Lines`` → ``(1, "Moving Lines")``(space-separated → remainder)
    * ``19``             → ``(19, None)``         (bare number → no label)
    * ``19 video format``→ ``(19, None)``         (generic descriptor dropped)
    * ``0``              → ``(0, None)``

    Returns (number, label); either element may be None.
    """
    stripped = line.strip()
    m = _LEADING_DIGITS_RE.match(stripped)
    if not m:
        return None, None
    number = int(m.group(1))
    rest = stripped[m.end():]
    if not rest:
        label = None
    elif rest[:1].isspace():
        # Space-separated: the label is the remaining text (a codec name), unless
        # it is just the generic boilerplate descriptor.
        remainder = rest.strip()
        label = None if remainder.lower() == _VIDEO_FORMAT_DESCRIPTOR else (remainder or None)
    else:
        # Attached suffix (e.g. "1K"): keep the whole first token.
        label = stripped.split()[0]
    return number, label


def _leading_float(line: str) -> float | None:
    """Like _leading_int but parses the leading token as a float."""
    parts = line.split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def _text_or_none(line: str) -> str | None:
    """Return the stripped line, or None when empty (title/author/copyright)."""
    text = line.strip()
    return text or None


def _parse_catalogue(region: bytes, number_of_chunks: int) -> int | None:
    """Best-effort scan of the chunk catalogue to count sound tracks.

    *region* is the (bounded) slice of the file starting at the catalogue offset.
    Each catalogue line is ``file_offset,video_bytes[;sound_0[;sound_1...]]``.
    Returns the maximum number of ``;``-separated sound-track fields seen across
    the (up to *number_of_chunks*) lines, or None if the catalogue cannot be
    read.  Never raises — header fields are the authoritative payload.
    """
    if not region:
        return None
    try:
        text = region.decode('latin-1', errors='replace')
        max_tracks = 0
        lines_read = 0
        for raw_line in text.split('\n'):
            line = raw_line.strip()
            if not line:
                continue
            # video_bytes and sound tracks are separated from file_offset by a
            # comma; sound tracks are ';'-separated after the first ';'.
            comma = line.split(',', 1)
            if len(comma) < 2:
                continue
            after_offset = comma[1]
            semis = after_offset.split(';')
            # semis[0] is video_bytes; the rest are sound tracks.
            tracks = len(semis) - 1
            if tracks > max_tracks:
                max_tracks = tracks
            lines_read += 1
            if number_of_chunks and lines_read >= number_of_chunks:
                break
        return max_tracks if lines_read else None
    except Exception as e:  # noqa: BLE001 - best-effort, never fatal
        log.debug("ARMovie catalogue parse failed: %s", e)
        return None


def parse_armovie_header(source) -> dict:
    """Parse an ARMovie file's text header (and, if present, the catalogue).

    *source* may be a filesystem path, a seekable binary file object, or a
    bytes-like object (the last mainly for tests).  Only a bounded prefix is read
    for the 21-line header and a bounded window at the catalogue offset for the
    sound-track count, so a multi-GB movie is never loaded into RAM.

    Returns a flat dict of the populated fields (None values omitted).  Raises
    ArmovieParseError if the file cannot be read, the magic line is missing, or
    the header is truncated.
    """
    if isinstance(source, (str, os.PathLike)):
        try:
            with open(source, 'rb') as fh:
                return _parse_armovie_stream(fh)
        except OSError as e:
            raise ArmovieParseError(f"Could not read ARMovie file: {e}") from e
    if isinstance(source, (bytes, bytearray, memoryview)):
        return _parse_armovie_stream(io.BytesIO(bytes(source)))
    return _parse_armovie_stream(source)


def _parse_armovie_stream(fh) -> dict:
    """Parse an ARMovie header from a seekable binary file object via bounded reads."""
    # The header is ASCII/Latin-1 text in the first 21 newline-terminated lines;
    # read only a bounded prefix rather than the whole (possibly huge) payload.
    fh.seek(0)
    head = fh.read(_HEADER_READ_BYTES)
    try:
        text = head.decode('latin-1', errors='replace')
    except Exception as e:  # noqa: BLE001
        raise ArmovieParseError(f"Could not decode header: {e}") from e

    # Lines are newline-terminated; tolerate a trailing '\r' per line.
    lines = [ln.rstrip('\r') for ln in text.split('\n')]

    if len(lines) < _HEADER_LINES:
        raise ArmovieParseError(
            f"Truncated header: {len(lines)} lines, need at least {_HEADER_LINES}"
        )

    if lines[0].strip() != _MAGIC:
        raise ArmovieParseError(f"Bad magic: {lines[0].strip()!r} (expected {_MAGIC!r})")

    # Header lines are 1-based in the spec; index by (n-1).
    title           = _text_or_none(lines[1])
    copyright_      = _text_or_none(lines[2])
    author          = _text_or_none(lines[3])
    video_format, video_label = _parse_codec(lines[4])
    width           = _leading_int(lines[5])
    height          = _leading_int(lines[6])
    pixel_depth     = _leading_int(lines[7])
    frame_rate      = _leading_float(lines[8])
    sound_format    = _leading_int(lines[9])
    sound_rate      = _leading_int(lines[10])
    sound_channels  = _leading_int(lines[11])
    sound_precision = _leading_int(lines[12])
    frames_per_chunk = _leading_float(lines[13])
    chunks_highest_index = _leading_int(lines[14])
    even_chunk_size = _leading_int(lines[15])
    odd_chunk_size  = _leading_int(lines[16])
    catalogue_offset = _leading_int(lines[17])
    sprite_offset   = _leading_int(lines[18])
    sprite_size     = _leading_int(lines[19])
    keys_offset     = _leading_int(lines[20])

    # Derived statistics.
    number_of_chunks = None
    if chunks_highest_index is not None:
        number_of_chunks = chunks_highest_index + 1

    duration_seconds = None
    if (frames_per_chunk is not None and number_of_chunks is not None
            and frame_rate is not None and frame_rate > 0):
        duration_seconds = (frames_per_chunk * number_of_chunks) / frame_rate

    sound_only = (video_format == 0) if video_format is not None else None
    has_key_frames = (keys_offset != -1) if keys_offset is not None else None
    has_poster_sprite = (sprite_size > 0) if sprite_size is not None else None

    sound_track_count = None
    if catalogue_offset is not None and catalogue_offset > 0:
        # Seek to the catalogue and read only a bounded window of it.
        fh.seek(catalogue_offset)
        catalogue = fh.read(_CATALOGUE_READ_BYTES)
        sound_track_count = _parse_catalogue(catalogue, number_of_chunks or 0)

    result: dict = {
        'title': title,
        'copyright': copyright_,
        'author': author,
        'video_format': video_format,
        'video_label': video_label,
        'width': width,
        'height': height,
        'pixel_depth': pixel_depth,
        'frame_rate': frame_rate,
        'sound_format': sound_format,
        'sound_rate': sound_rate,
        'sound_channels': sound_channels,
        'sound_precision': sound_precision,
        'frames_per_chunk': frames_per_chunk,
        'chunks_highest_index': chunks_highest_index,
        'number_of_chunks': number_of_chunks,
        'even_chunk_size': even_chunk_size,
        'odd_chunk_size': odd_chunk_size,
        'catalogue_offset': catalogue_offset,
        'sprite_offset': sprite_offset,
        'sprite_size': sprite_size,
        'keys_offset': keys_offset,
        'duration_seconds': duration_seconds,
        'sound_only': sound_only,
        'has_key_frames': has_key_frames,
        'has_poster_sprite': has_poster_sprite,
        'sound_track_count': sound_track_count,
    }

    # Omit None values (mirrors iso9660.py so templates can use {% if %}).
    return {k: v for k, v in result.items() if v is not None}

# vim: ts=4 sw=4 et
