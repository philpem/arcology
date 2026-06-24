"""
Short human-readable labels for file extensions.

Used to display non-RISC OS file types in the file listing and viewer.
Curated lookup takes priority; mimetypes module is used as fallback.
"""
import mimetypes as _mimetypes
from .riscos_filetypes import get_filetype_name

# Curated short labels for extensions common in retro/vintage computing collections.
_EXTENSION_LABELS: dict[str, str] = {
    # Windows graphics
    'wmf':  'Windows Metafile',
    'emf':  'Enhanced Metafile',
    'bmp':  'Bitmap',
    'dib':  'Bitmap',
    'ico':  'Icon',
    'cur':  'Cursor',
    # Common raster images
    'png':  'PNG Image',
    'gif':  'GIF Image',
    'jpg':  'JPEG Image',
    'jpeg': 'JPEG Image',
    'tif':  'TIFF Image',
    'tiff': 'TIFF Image',
    'pcx':  'PCX Image',
    'tga':  'TGA Image',
    'ppm':  'PPM Image',
    'pgm':  'PGM Image',
    'pbm':  'PBM Image',
    # Vector / structured graphics
    'svg':  'SVG Vector',
    'eps':  'EPS',
    'ai':   'Illustrator',
    # Audio
    'wav':  'WAV Audio',
    'mid':  'MIDI',
    'midi': 'MIDI',
    'mp3':  'MP3 Audio',
    'ogg':  'OGG Audio',
    'oga':  'OGG Audio',
    'opus': 'Opus Audio',
    'aif':  'AIFF Audio',
    'aiff': 'AIFF Audio',
    'aac':  'AAC Audio',
    'm4a':  'M4A Audio',
    'flac': 'FLAC Audio',
    'wma':  'WMA Audio',
    # Video
    'mp4':  'MP4 Video',
    'm4v':  'MP4 Video',
    'mkv':  'Matroska Video',
    'webm': 'WebM Video',
    'avi':  'AVI Video',
    'mov':  'QuickTime Video',
    'qt':   'QuickTime Video',
    'wmv':  'WMV Video',
    'flv':  'Flash Video',
    'mpg':  'MPEG Video',
    'mpeg': 'MPEG Video',
    'm2v':  'MPEG Video',
    'm2ts': 'MPEG-TS Video',
    'ts':   'MPEG-TS Video',
    '3gp':  '3GP Video',
    'ogv':  'OGG Video',
    # Tracker music
    'mod':  'MOD Module',
    's3m':  'S3M Module',
    'xm':   'XM Module',
    'it':   'IT Module',
    'mtm':  'MTM Module',
    '669':  '669 Module',
    # Documents / text
    'txt':  'Text',
    'doc':  'Word Document',
    'rtf':  'RTF Document',
    'xls':  'Spreadsheet',
    'pdf':  'PDF',
    'htm':  'HTML',
    'html': 'HTML',
    'xml':  'XML',
    'csv':  'CSV',
    # Archives
    'zip':  'ZIP Archive',
    'tar':  'Tar Archive',
    'gz':   'GZip',
    'bz2':  'BZip2',
    'lzh':  'LZH Archive',
    'lha':  'LHA Archive',
    'rar':  'RAR Archive',
    'arj':  'ARJ Archive',
    'arc':  'ARC Archive',
    'zoo':  'ZOO Archive',
    # Executables / code
    'exe':  'Executable',
    'dll':  'Library',
    'com':  'DOS Program',
    'sys':  'System File',
    'drv':  'Driver',
    'vxd':  'VxD Driver',
    'bat':  'Batch Script',
    'cmd':  'Command Script',
    'bas':  'BASIC',
    'inf':  'Setup Info',
    # Disk images
    'img':  'Disc Image',
    'iso':  'ISO Image',
    'ima':  'Floppy Image',
    'st':   'Atari ST Image',
    'adf':  'Amiga Disc',
    'd64':  'C64 Disc',
    # Fonts
    'ttf':  'TrueType Font',
    'fon':  'Bitmap Font',
    'fnt':  'Bitmap Font',
    # Misc
    'ini':  'Config',
    'cfg':  'Config',
    'log':  'Log File',
    'dat':  'Data File',
}


def extension_label(ext: str) -> str:
    """Return a short human-readable label for a file extension.

    Lookup order: curated table → mimetypes description → uppercase extension.
    """
    if not ext:
        return ''
    clean = ext.lower().lstrip('.')
    if clean in _EXTENSION_LABELS:
        return _EXTENSION_LABELS[clean]
    mime, _ = _mimetypes.guess_type(f'file.{clean}')
    if mime:
        # e.g. 'image/x-wmf' → 'WMF', 'text/html' → 'HTML'
        sub = mime.split('/')[-1].removeprefix('x-').upper()
        if len(sub) <= 8:
            return sub
    return clean.upper()


def unified_type_label(key: str) -> str:
    """Return a display label for a viewer filetype key.

    Keys starting with '.' are extension-based (e.g. '.wmf' → 'Windows Metafile').
    Other keys are RISC OS filetype hex codes (e.g. 'fff' → 'Text').
    """
    if key.startswith('.'):
        return extension_label(key[1:])
    return get_filetype_name(key) or key.upper()

# vim: ts=4 sw=4 et
