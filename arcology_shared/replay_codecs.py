"""
Acorn Replay / ARMovie codec-type name tables.

ARMovie headers identify their video and sound codecs by a numeric *format*
field (header lines 5 and 10).  The number on its own is opaque, so this module
maps the known format numbers to human-readable codec names for display.

The tables are intentionally simple ``dict[int, str]`` lookups so they can be
extended as more codecs are documented — just add the number and name.  Both
the worker (which parses the header) and the web app (which renders it) can
import this single source of truth; resolution is done at *display* time, so
adding a name here immediately labels every already-analysed movie without
re-running any analysis.

Sources: the scotch reference decoder (<https://github.com/philpem/scotch>) and
the ARMovie / Acorn Replay container documentation.  Where a name is derived
from the codec sources but not yet validated against real movies it is still
listed — an approximate name beats a bare number.
"""

# Video compression format (ARMovie header line 5).
#
# 0 means the movie carries sound only (no video).  Types 1-23 are Acorn's
# native codecs (decoded by scotch directly); the 6xx range is Warm Silence
# Software's MovieFS re-encapsulation of PC codecs, and the 9xx range is IMS's
# VideoFS.  Type 500 is Iota's "The Complete Animator".
VIDEO_CODEC_NAMES: dict[int, str] = {
    0: 'Sound only (no video)',

    # Acorn native codecs.
    1: 'Moving Lines',
    2: 'Uncompressed (16bpp RGB)',
    7: 'Moving Blocks',
    17: 'Moving Blocks HQ',
    19: 'Super Moving Blocks',
    20: 'Moving Blocks Beta',
    23: 'Uncompressed (YUV)',

    # Iota "The Complete Animator" (TCA/ACEF).
    500: 'The Complete Animator (TCA)',

    # MovieFS (Warm Silence Software) — re-encapsulated PC codecs (600-699).
    600: 'CRAM8 (Microsoft Video 1, 8-bit)',
    601: 'CRAM16 (Microsoft Video 1, 16-bit)',
    602: 'Cinepak',
    603: 'RPZA (Apple Video)',
    604: 'SMC (Apple Graphics)',
    605: 'Ultimotion',
    606: 'RGB8',
    607: 'RLE8',
    608: 'RGB24',
    609: 'RLE8',
    610: 'FLI/FLC',
    613: 'RLE4',
    614: 'QuickTime RLE (16-bit)',
    615: 'QuickTime RLE (24-bit)',
    622: 'DL',
    623: 'ANM',
    624: 'RGB8',
    626: 'RGB24',
    628: 'Indeo',
    629: 'Indeo',

    # IMS VideoFS (900s).
    901: 'Indeo (raw YVU9)',
    902: 'Indeo 3.2',
}

# Sound compression format (ARMovie header line 10).
#
# 0 means the movie is silent.  Note that some encoders write this field as a
# bits-per-sample number with a textual suffix (e.g. "8 LIN" for linear vs the
# bare "8" for the exponential VIDC decoder), so the number alone is not always
# a unique codec id; the values below cover the commonly-seen format numbers.
SOUND_CODEC_NAMES: dict[int, str] = {
    0: 'Silent (no sound)',
    1: 'VIDC (8-bit logarithmic)',
    2: 'ADPCM',
}


def video_codec_name(number: int | None) -> str | None:
    """Return the codec name for a video format *number*, or None if unknown."""
    if number is None:
        return None
    return VIDEO_CODEC_NAMES.get(number)


def sound_codec_name(number: int | None) -> str | None:
    """Return the codec name for a sound format *number*, or None if unknown."""
    if number is None:
        return None
    return SOUND_CODEC_NAMES.get(number)

# vim: ts=4 sw=4 et
