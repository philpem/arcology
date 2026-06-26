"""
Artefact-type registry shared by the web app, worker, and CLI.

  EXTENSION_MAP          — filename extension → ArtefactType (single source
                           of truth for upload-time type detection)
  COMPRESSOR_SUFFIXES    — compression suffixes recognised on raw-sector
                           images (``drive.dd.zst`` …)
  RAW_SECTOR_EXTENSIONS  — extensions that map to ArtefactType.RAW_SECTOR
  ARCHIVE_EXTENSIONS     — extensions that map to an archive ArtefactType
  detect_artefact_type() — extension-based type detection

Server-side analysis scheduling (ANALYSIS_MAP, queue_analyses_for_artefact)
stays in ``myapp/services/artefact_types.py`` — it depends on the database
layer, which the worker and CLI must not import.
"""

import os
from .enums import ArtefactType

# Extension to ArtefactType mapping
EXTENSION_MAP = {
    # Flux-level
    '.scp': ArtefactType.SCP,
    '.dfi': ArtefactType.DFI,
    '.a2r': ArtefactType.A2R,

    # Cooked sector-level floppy or hard disc
    '.imd': ArtefactType.IMD,   # needs conversion to sectors
    '.hfe': ArtefactType.HFE,   # needs conversion to sectors

    # Raw sector images
    '.adf': ArtefactType.RAW_SECTOR,
    '.img': ArtefactType.RAW_SECTOR,
    '.ima': ArtefactType.RAW_SECTOR,
    '.dsk': ArtefactType.RAW_SECTOR,

    # CD/DVD
    '.iso': ArtefactType.ISO,

    # Hard drive raw images
    '.dd': ArtefactType.RAW_SECTOR,
    '.hdf': ArtefactType.RAW_SECTOR,

    # Documents
    '.pdf': ArtefactType.PDF,

    # Archives
    '.zip': ArtefactType.ZIP,
    '.tar.gz': ArtefactType.TARGZ,
    '.tgz': ArtefactType.TARGZ,
    '.rar': ArtefactType.RAR,
    '.7z':  ArtefactType.SEVENZ,
    '.arc': ArtefactType.ARC,
    '.arcfs': ArtefactType.ARC,
    '.spk': ArtefactType.ARC,
    '.spark': ArtefactType.ARC,
    '.b21':   ArtefactType.TBAFS,
    '.tbafs': ArtefactType.TBAFS,
    '.b23':   ArtefactType.XFILES,

    # Acorn/RISC OS native viewable formats
    '.spr':  ArtefactType.ACORN_SPRITE,
    '.aff':  ArtefactType.ACORN_DRAW,
    '.draw': ArtefactType.ACORN_DRAW,
    '.txt':  ArtefactType.ACORN_TEXT,

    # Common raster images (browser-native pass-through or Pillow-converted)
    '.jpg':  ArtefactType.IMAGE,
    '.jpeg': ArtefactType.IMAGE,
    '.png':  ArtefactType.IMAGE,
    '.gif':  ArtefactType.IMAGE,
    '.webp': ArtefactType.IMAGE,
    '.bmp':  ArtefactType.IMAGE,
    '.tif':  ArtefactType.IMAGE,
    '.tiff': ArtefactType.IMAGE,
    '.pcx':  ArtefactType.IMAGE,
    '.tga':  ArtefactType.IMAGE,

    # Windows vector metafiles (converted to SVG)
    '.wmf':  ArtefactType.IMAGE,
    '.emf':  ArtefactType.IMAGE,
}

# --- Time-based media (audio / video) ---------------------------------------
#
# Recognised media container extensions, split into video vs audio so the
# player can pick <video>/<audio> and the artefact type (VIDEO/AUDIO) follows
# the container.  These are the single source of truth — EXTENSION_MAP is
# derived from them below, and they drive MEDIA_TRANSCODE's file discovery.
#
# Whether a file is *transcoded* is NOT decided by extension: browser playback
# depends on the container **and the codecs inside it** (a .mov with H.264/AAC
# plays natively; a .mov with MPEG-4/DivX does not).  MEDIA_TRANSCODE therefore
# probes every media file with ffprobe and calls media_is_browser_playable()
# below — re-encoding only what browsers genuinely cannot play, and passing the
# rest through untouched (their metadata is still recorded).
_VIDEO_EXTENSIONS = frozenset({
    '.mp4', '.m4v', '.webm', '.ogv',                  # commonly browser-native
    '.mov', '.qt',                                    # QuickTime (native iff H.264/...)
    '.avi', '.mkv', '.wmv', '.flv', '.asf',
    '.mpg', '.mpeg', '.mpe', '.m2v', '.mpv',          # MPEG-1 / MPEG-2 program/elementary
    '.ts', '.m2ts', '.mts', '.vob',                   # MPEG-2 transport / DVD-Video
    '.3gp', '.3g2', '.divx', '.ogm', '.rm', '.rmvb',
})
_AUDIO_EXTENSIONS = frozenset({
    '.mp3', '.m4a', '.aac', '.ogg', '.oga', '.wav', '.flac', '.opus',
    '.wma', '.ra', '.au', '.aiff', '.aif', '.ac3', '.mp2',
    # Tracker / module music — decoded by ffmpeg when compiled with libopenmpt.
    # Browsers cannot play these natively so they are always transcoded to M4A.
    '.mod', '.s3m', '.it', '.xm',   # the four mainstream PC tracker formats
    '.med',                          # MED/OctaMED (Amiga / RISC OS)
    '.stm', '.669', '.mtm',         # early PC formats (ScreamTracker 2, Composer 669, MultiTracker)
    '.ult', '.far', '.okt', '.ptm', # UltraTracker, Farandole, Oktalyzer, PolyTracker
    '.mdl',                          # DigiTrack
})

MEDIA_EXTENSIONS = _VIDEO_EXTENSIONS | _AUDIO_EXTENSIONS

# RISC OS filetypes that identify standard video/audio containers.
# These let MEDIA_TRANSCODE select files that lack a PC-style extension.
# &AE7 (ARMovie) is intentionally excluded — it has its own REPLAY_PROCESS pipeline.
# &071 is technically dual-use (MIDI and AVI); it is mapped to video so that AVI
# files stored under that type reach the transcode pipeline.
RISCOS_MEDIA_FILETYPES: dict[str, str] = {
    # Video
    '071': 'video',   # AVI (also registered for MIDI; video takes priority here)
    '0f8': 'video',   # MPEG
    'a63': 'video',   # MKV (Matroska)
    'a64': 'video',   # MP4
    'a8d': 'video',   # MPEG DVD VOB
    'b9f': 'video',   # FLI/FLC (Autodesk Animator)
    'bf8': 'video',   # MPEG
    'fb2': 'video',   # AVI
    # Audio
    '1a8': 'audio',   # Ogg Vorbis
    '1ad': 'audio',   # AMPEG (MP2/MP3)
    '1cf': 'audio',   # FLAC
    'a8f': 'audio',   # AC3
    'f88': 'audio',   # RealAudio
    'fb1': 'audio',   # WAV
    'fc2': 'audio',   # AIFF
    # Tracker / module music (decoded to audio by ffmpeg/libopenmpt)
    '501': 'audio',   # SoundTracker
    '701': 'audio',   # ProTracker
    '7b7': 'audio',   # Soundtracker
    'acf': 'audio',   # QTM
    'c02': 'audio',   # ScreamTracker
    'c04': 'audio',   # Impulse Tracker
    'c05': 'audio',   # UltraTracker
    'cb6': 'audio',   # SoundTracker
}

# PC-style extension that best describes each RISC OS media filetype.
# Passed as a synthetic filename hint to media_is_browser_playable() so that
# passthrough-capable formats (MP4/H.264, MP3, Ogg, FLAC, WAV) stored on RISC OS
# discs without a file extension are not needlessly re-encoded.
# Tracker formats are omitted: no standard extension helps with passthrough, and
# ffprobe auto-detects their format from file headers regardless.
RISCOS_FILETYPE_EXTENSION: dict[str, str] = {
    # Video (none are browser-passthrough, but the hint aids logging)
    '071': '.avi',
    '0f8': '.mpg',
    'a63': '.mkv',
    'a64': '.mp4',    # may be passthrough if H.264/AAC
    'a8d': '.vob',
    'b9f': '.fli',
    'bf8': '.mpg',
    'fb2': '.avi',
    # Audio
    '1a8': '.ogg',    # passthrough
    '1ad': '.mp3',    # passthrough
    '1cf': '.flac',   # passthrough
    'a8f': '.ac3',
    'f88': '.ra',
    'fb1': '.wav',    # passthrough
    'fc2': '.aiff',
}


def media_kind_for_riscos_filetype(filetype: str) -> str | None:
    """Return ``'video'`` / ``'audio'`` for a RISC OS filetype hex code, else ``None``."""
    return RISCOS_MEDIA_FILETYPES.get(filetype.lower())


# Acorn Replay / ARMovie detection.
#
# On RISC OS these files carry filetype &AE7 (ARMovie), which is how the
# REPLAY_PROCESS pipeline normally finds them.  When such a
# file is moved to a non-RISC OS filesystem (e.g. zipped on a PC without the
# Acorn extra field), that filetype is lost, so we also recognise the PC-style
# extensions people give Replay movies.
#
# Extension/filetype is only a *candidate* signal: discovery confirms each
# match by sniffing the ``ARMovie`` magic bytes (file_has_armovie_magic) before
# treating a file as Replay, so a non-ARMovie file that merely happens to carry
# one of these extensions is rejected rather than mis-processed.
REPLAY_RISCOS_FILETYPE = 'ae7'
REPLAY_EXTENSIONS = frozenset({'.rpl', '.rep', '.replay', '.armovie'})


def is_replay_file(filename: str, risc_os_filetype: str | None) -> bool:
    """Return True if a file *might* be an Acorn Replay / ARMovie movie.

    A cheap, metadata-only candidate filter: matches RISC OS filetype &AE7 or a
    PC-style Replay extension (``.rpl`` / ``.rep`` / ``.replay`` / ``.armovie``),
    case-insensitive.  Callers confirm a genuine match with the ARMovie magic
    bytes (``file_has_armovie_magic``) once the file is on disk.
    """
    if (risc_os_filetype or '').lower() == REPLAY_RISCOS_FILETYPE:
        return True
    _, ext = os.path.splitext((filename or '').lower())
    return ext in REPLAY_EXTENSIONS

# Container extensions whose bytes a modern browser *may* be able to play
# directly (subject to a codec check below).  Anything outside these sets is
# always transcoded.  MOV/QT are included because, with H.264, they play in
# evergreen browsers (the same ISO-BMFF container family as MP4).
_PASSTHROUGH_VIDEO_CONTAINERS = frozenset({
    '.mp4', '.m4v', '.webm', '.ogv', '.mov', '.qt',
})
_PASSTHROUGH_AUDIO_CONTAINERS = frozenset({
    '.mp3', '.m4a', '.aac', '.ogg', '.oga', '.wav', '.flac', '.opus',
})

# Codecs an evergreen browser can decode via HTML5 <video>/<audio>.  Codecs
# outside these (HEVC/H.265, MPEG-1/2 video, MPEG-4 Part 2 / DivX/Xvid, WMV,
# VC-1, RealVideo, …) are transcoded to H.264.  ffprobe codec_name values.
_PASSTHROUGH_VIDEO_CODECS = frozenset({
    'h264', 'avc1', 'vp8', 'vp9', 'av1', 'theora',
})
_PASSTHROUGH_AUDIO_CODECS = frozenset({
    'aac', 'mp3', 'opus', 'vorbis', 'flac',
    'pcm_s16le', 'pcm_u8', 'pcm_s24le', 'pcm_s32le', 'pcm_f32le',  # WAV PCM
})

# Fold the media extensions into the single detection map (video container →
# VIDEO, audio container → AUDIO).
for _ext in _VIDEO_EXTENSIONS:
    EXTENSION_MAP[_ext] = ArtefactType.VIDEO
for _ext in _AUDIO_EXTENSIONS:
    EXTENSION_MAP[_ext] = ArtefactType.AUDIO
del _ext


def media_kind_for_extension(ext: str) -> str | None:
    """Return ``'video'`` / ``'audio'`` for a media extension, else ``None``."""
    ext = ext.lower()
    if ext in _VIDEO_EXTENSIONS:
        return 'video'
    if ext in _AUDIO_EXTENSIONS:
        return 'audio'
    return None


def media_is_browser_playable(filename: str, *, has_video: bool,
                              video_codec: str | None,
                              audio_codec: str | None) -> bool:
    """Decide whether a media file can be streamed to a browser as-is.

    Combines a container check (by *filename* extension) with a codec check
    (from ffprobe).  Returns True only when the container is one browsers
    understand **and** every present track uses a browser-decodable codec — so
    a file is passed through untouched rather than needlessly re-encoded.
    Anything that fails either check must be transcoded to H.264/AAC MP4.
    """
    _, ext = os.path.splitext(filename.lower())
    vc = (video_codec or '').lower()
    ac = (audio_codec or '').lower()

    if has_video:
        if ext not in _PASSTHROUGH_VIDEO_CONTAINERS:
            return False
        if vc not in _PASSTHROUGH_VIDEO_CODECS:
            return False
        # An audio track, if present, must also be browser-decodable.
        if ac and ac not in _PASSTHROUGH_AUDIO_CODECS:
            return False
        return True

    # Audio-only.  Accept both audio containers and browser-native *video*
    # containers carrying only an audio stream (e.g. an AAC track in an .mp4 or
    # .webm) — those play fine in an HTML5 element and must not be re-encoded.
    if ext not in _PASSTHROUGH_AUDIO_CONTAINERS and ext not in _PASSTHROUGH_VIDEO_CONTAINERS:
        return False
    # Require a known-good audio codec: an absent codec means ffprobe found no
    # playable audio stream (corrupt/empty/misnamed file), which must NOT be
    # passed through as a broken player — fall through to a (failing) transcode.
    if not ac or ac not in _PASSTHROUGH_AUDIO_CODECS:
        return False
    return True


# Compressor suffixes recognised on top of a raw-sector extension, in order
# of preference when several compressed forms of the same image exist.
COMPRESSOR_SUFFIXES = ('.zst', '.gz', '.bz2')

# Archive container artefact types (extract via ARCHIVE_EXTRACT).
ARCHIVE_ARTEFACT_TYPES = frozenset({
    ArtefactType.ZIP, ArtefactType.TARGZ, ArtefactType.RAR,
    ArtefactType.SEVENZ, ArtefactType.ARC, ArtefactType.TBAFS,
    ArtefactType.XFILES,
})

# Derived extension sets, for callers that classify by category rather than
# exact type (e.g. the bulk-import duplicate-form ranking).
RAW_SECTOR_EXTENSIONS = frozenset(
    ext for ext, atype in EXTENSION_MAP.items()
    if atype is ArtefactType.RAW_SECTOR
)
ARCHIVE_EXTENSIONS = frozenset(
    ext for ext, atype in EXTENSION_MAP.items()
    if atype in ARCHIVE_ARTEFACT_TYPES
)


def detect_artefact_type(filename: str) -> ArtefactType:
    """Detect artefact type from filename extension."""
    filename_lower = filename.lower()

    # Check compound extensions first (order matters)
    if filename_lower.endswith('.dd.zst'):
        return ArtefactType.DD_ZST
    if filename_lower.endswith('.dd.gz'):
        return ArtefactType.DD_GZ
    if filename_lower.endswith('.dd.bz2'):
        return ArtefactType.DD_BZ2
    if filename_lower.endswith('.tar.gz'):
        return ArtefactType.TARGZ

    # Strip a trailing compression suffix and re-check, so e.g. .dfi.bz2 → .dfi
    stem = filename_lower
    for suffix in COMPRESSOR_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break

    _, ext = os.path.splitext(stem)
    return EXTENSION_MAP.get(ext, ArtefactType.UNKNOWN)

# vim: ts=4 sw=4 et
