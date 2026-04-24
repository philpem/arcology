"""
ISO 9660 Primary Volume Descriptor (PVD) parser.

Extracts standard filesystem-level metadata (volume name, publisher, data
preparer, timestamps, etc.) from the PVD at logical sector 16 of an ISO 9660
disc image.

No external dependencies — uses stdlib struct only.

Reference: ECMA-119 (ISO 9660) section 8.4, "Primary Volume Descriptor".
"""

import logging
import struct
from pathlib import Path

log = logging.getLogger(__name__)

# ISO 9660 sector size in bytes
_SECTOR = 2048

# The Primary Volume Descriptor sits at sector 16 (byte offset 32768)
_PVD_LBA = 16


def _decode_str(data: bytes) -> str | None:
    """
    Decode an ISO 9660 strA/strD/a-characters field.

    ISO 9660 string fields are space-padded ASCII.  Trailing NULs and spaces
    are stripped; an empty or whitespace-only field returns None so that the
    caller can omit it from the output dict.
    """
    try:
        text = data.decode('ascii', errors='replace').rstrip(' \x00')
    except Exception:
        return None
    return text or None


def _decode_pvd_datetime(data: bytes) -> str | None:
    """
    Decode a 17-byte PVD date-and-time field into an ISO 8601 string.

    Format (ECMA-119 8.4.26.1):
      Offset  Size  Meaning
        0      4    Year   (ASCII digits, "0001" .. "9999")
        4      2    Month  (ASCII digits, "01" .. "12")
        6      2    Day    (ASCII digits, "01" .. "31")
        8      2    Hour   (ASCII digits, "00" .. "23")
       10      2    Minute (ASCII digits, "00" .. "59")
       12      2    Second (ASCII digits, "00" .. "59")
       14      2    Hundredths of second (ASCII digits, "00" .. "99")
       16      1    Timezone offset from GMT, signed int8, in 15-minute
                    intervals.  Range -48 .. +52.

    An "unset" field is recorded as 16 ASCII '0' characters followed by a
    zero byte — this is returned as None.

    Returns ISO 8601 string like "2001-05-17T14:23:45+01:00", or None if the
    field is empty, malformed, or unset.
    """
    if len(data) < 17:
        return None
    digits = data[:16]
    # Unset-date conventions seen in the wild: all '0', all space, or all NUL.
    if digits in (b'0' * 16, b' ' * 16, b'\x00' * 16):
        return None
    try:
        year   = int(digits[0:4].decode('ascii'))
        month  = int(digits[4:6].decode('ascii'))
        day    = int(digits[6:8].decode('ascii'))
        hour   = int(digits[8:10].decode('ascii'))
        minute = int(digits[10:12].decode('ascii'))
        second = int(digits[12:14].decode('ascii'))
    except (UnicodeDecodeError, ValueError):
        return None

    # Reject obviously-invalid component ranges rather than silently emitting
    # a bogus-looking timestamp.  Zero year/month/day is a common "unset"
    # marker that doesn't use all-'0' digits.
    if year == 0 or month == 0 or day == 0:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 60):
        return None

    tz_offset = struct.unpack_from('<b', data, 16)[0]
    tz_mins = tz_offset * 15
    tz_sign = '+' if tz_mins >= 0 else '-'
    tz_abs = abs(tz_mins)
    tz_hh = tz_abs // 60
    tz_mm = tz_abs % 60

    return (
        f'{year:04d}-{month:02d}-{day:02d}'
        f'T{hour:02d}:{minute:02d}:{second:02d}'
        f'{tz_sign}{tz_hh:02d}:{tz_mm:02d}'
    )


def parse_iso9660_pvd(iso_path: Path) -> dict:
    """
    Read the Primary Volume Descriptor of an ISO 9660 image and return the
    human-readable metadata fields as a dict.

    Keys populated (only when non-empty):
      - system_identifier
      - volume_identifier
      - volume_set_identifier
      - publisher_identifier
      - data_preparer_identifier
      - application_identifier
      - copyright_file
      - abstract_file
      - bibliographic_file
      - volume_creation_date      (ISO 8601)
      - volume_modification_date  (ISO 8601)
      - volume_expiration_date    (ISO 8601)
      - volume_effective_date     (ISO 8601)

    Returns an empty dict if the file cannot be opened, the sector cannot be
    read, or the PVD signature is missing (i.e. not a valid ISO 9660 image).
    """
    meta: dict = {}
    try:
        with open(iso_path, 'rb') as f:
            f.seek(_PVD_LBA * _SECTOR)
            pvd = f.read(_SECTOR)
    except OSError as e:
        log.debug("Failed to read PVD from %s: %s", iso_path, e)
        return meta

    # A PVD must be at least 881 bytes to contain every field we read.
    if len(pvd) < 881:
        return meta

    # Validate descriptor type (0x01 = PVD) and standard identifier "CD001".
    if pvd[0] != 0x01 or pvd[1:6] != b'CD001':
        return meta

    # Field offsets per ECMA-119 section 8.4.  Each text field is ASCII and
    # space-padded; timestamp fields are 17 bytes as decoded above.
    fields = [
        ('system_identifier',        _decode_str(pvd[  8:  40])),
        ('volume_identifier',        _decode_str(pvd[ 40:  72])),
        ('volume_set_identifier',    _decode_str(pvd[190: 318])),
        ('publisher_identifier',     _decode_str(pvd[318: 446])),
        ('data_preparer_identifier', _decode_str(pvd[446: 574])),
        ('application_identifier',   _decode_str(pvd[574: 702])),
        ('copyright_file',           _decode_str(pvd[702: 739])),
        ('abstract_file',            _decode_str(pvd[739: 776])),
        ('bibliographic_file',       _decode_str(pvd[776: 813])),
        ('volume_creation_date',     _decode_pvd_datetime(pvd[813: 830])),
        ('volume_modification_date', _decode_pvd_datetime(pvd[830: 847])),
        ('volume_expiration_date',   _decode_pvd_datetime(pvd[847: 864])),
        ('volume_effective_date',    _decode_pvd_datetime(pvd[864: 881])),
    ]
    for key, value in fields:
        if value:
            meta[key] = value
    return meta

# vim: ts=4 sw=4 et
