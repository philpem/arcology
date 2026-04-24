"""
Tests for ISO 9660 Primary Volume Descriptor (PVD) metadata extraction.

Exercises the pure-Python parser in worker/arcworker/tools/iso9660.py using
synthetic PVD sectors — no real ISO image is needed.
"""

import os
import struct
import sys
import tempfile
import unittest

# Add the repo root to sys.path so imports work without installation
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from worker.arcworker.tools.iso9660 import (
    _decode_str,
    _decode_pvd_datetime,
    parse_iso9660_pvd,
)


_SECTOR = 2048


def _pad(data: bytes, length: int, pad: bytes = b' ') -> bytes:
    """Right-pad a bytes value to ``length`` with ``pad``."""
    if len(data) > length:
        return data[:length]
    return data + pad * (length - len(data))


def _datetime_field(
    year: int = 2001, month: int = 5, day: int = 17,
    hour: int = 14, minute: int = 23, second: int = 45,
    hundredths: int = 0, tz_offset: int = 4,
) -> bytes:
    """
    Build a 17-byte PVD date-and-time field.

    ``tz_offset`` is the signed byte (number of 15-minute intervals east of GMT).
    """
    digits = (
        f'{year:04d}{month:02d}{day:02d}'
        f'{hour:02d}{minute:02d}{second:02d}{hundredths:02d}'
    ).encode('ascii')
    assert len(digits) == 16
    return digits + struct.pack('<b', tz_offset)


def _build_pvd(
    system_id: bytes = b'',
    volume_id: bytes = b'',
    volume_set_id: bytes = b'',
    publisher: bytes = b'',
    preparer: bytes = b'',
    application: bytes = b'',
    copyright_file: bytes = b'',
    abstract_file: bytes = b'',
    bibliographic_file: bytes = b'',
    created: bytes | None = None,
    modified: bytes | None = None,
    expiration: bytes | None = None,
    effective: bytes | None = None,
) -> bytes:
    """
    Build a minimal synthetic ISO 9660 Primary Volume Descriptor sector.

    Only the fields the parser reads are populated; all other bytes are left
    as space-padded/zero filler.  The result is exactly 2048 bytes.
    """
    unset_date = b'0' * 16 + b'\x00'

    pvd = bytearray(_SECTOR)
    # Descriptor header
    pvd[0]     = 0x01          # Volume Descriptor Type = PVD
    pvd[1:6]   = b'CD001'
    pvd[6]     = 0x01          # Version

    # Text fields (all space-padded)
    pvd[8:40]    = _pad(system_id,          32)
    pvd[40:72]   = _pad(volume_id,          32)
    pvd[190:318] = _pad(volume_set_id,     128)
    pvd[318:446] = _pad(publisher,         128)
    pvd[446:574] = _pad(preparer,          128)
    pvd[574:702] = _pad(application,       128)
    pvd[702:739] = _pad(copyright_file,     37)
    pvd[739:776] = _pad(abstract_file,      37)
    pvd[776:813] = _pad(bibliographic_file, 37)

    # Date fields (default to the "unset" convention if caller omits)
    pvd[813:830] = created    if created    is not None else unset_date
    pvd[830:847] = modified   if modified   is not None else unset_date
    pvd[847:864] = expiration if expiration is not None else unset_date
    pvd[864:881] = effective  if effective  is not None else unset_date

    return bytes(pvd)


def _write_iso(pvd: bytes) -> str:
    """
    Write a temporary file containing 16 empty sectors followed by the given
    PVD at sector 16.  Returns the file path; caller should unlink when done.
    """
    fd, path = tempfile.mkstemp(suffix='.iso')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(b'\x00' * (16 * _SECTOR))
            f.write(pvd)
    except Exception:
        os.unlink(path)
        raise
    return path


class TestDecodeStr(unittest.TestCase):
    """_decode_str strips trailing padding and returns None for empty fields."""

    def test_trailing_spaces_stripped(self):
        self.assertEqual(_decode_str(b'HELLO          '), 'HELLO')

    def test_trailing_nuls_stripped(self):
        self.assertEqual(_decode_str(b'HELLO\x00\x00\x00'), 'HELLO')

    def test_mixed_trailing_padding(self):
        self.assertEqual(_decode_str(b'HELLO \x00 \x00'), 'HELLO')

    def test_all_spaces_returns_none(self):
        self.assertIsNone(_decode_str(b'     '))

    def test_empty_returns_none(self):
        self.assertIsNone(_decode_str(b''))

    def test_internal_spaces_preserved(self):
        self.assertEqual(_decode_str(b'HELLO WORLD  '), 'HELLO WORLD')


class TestDecodePvdDatetime(unittest.TestCase):
    """_decode_pvd_datetime produces ISO 8601 strings or None for unset values."""

    def test_valid_datetime_positive_tz(self):
        # 2001-05-17 14:23:45, +01:00 (tz_offset = 4 quarter-hours)
        field = _datetime_field(2001, 5, 17, 14, 23, 45, tz_offset=4)
        self.assertEqual(_decode_pvd_datetime(field), '2001-05-17T14:23:45+01:00')

    def test_valid_datetime_negative_tz(self):
        # 1995-11-01 09:05:00, -05:00 (tz_offset = -20 quarter-hours)
        field = _datetime_field(1995, 11, 1, 9, 5, 0, tz_offset=-20)
        self.assertEqual(_decode_pvd_datetime(field), '1995-11-01T09:05:00-05:00')

    def test_gmt_zero_offset(self):
        field = _datetime_field(2000, 1, 1, 12, 0, 0, tz_offset=0)
        self.assertEqual(_decode_pvd_datetime(field), '2000-01-01T12:00:00+00:00')

    def test_all_zeros_returns_none(self):
        self.assertIsNone(_decode_pvd_datetime(b'0' * 16 + b'\x00'))

    def test_all_spaces_returns_none(self):
        self.assertIsNone(_decode_pvd_datetime(b' ' * 16 + b'\x00'))

    def test_all_nuls_returns_none(self):
        self.assertIsNone(_decode_pvd_datetime(b'\x00' * 17))

    def test_short_input_returns_none(self):
        self.assertIsNone(_decode_pvd_datetime(b'2001'))

    def test_invalid_month_returns_none(self):
        field = _datetime_field(month=13)
        self.assertIsNone(_decode_pvd_datetime(field))

    def test_invalid_day_returns_none(self):
        field = _datetime_field(day=32)
        self.assertIsNone(_decode_pvd_datetime(field))

    def test_zero_year_returns_none(self):
        # Not strictly "all zeros" (tz byte could differ), but year=0 means unset.
        field = _datetime_field(year=0)
        self.assertIsNone(_decode_pvd_datetime(field))

    def test_non_numeric_returns_none(self):
        bad = b'ABCD' + b'0' * 12 + b'\x00'
        self.assertIsNone(_decode_pvd_datetime(bad))

    def test_half_hour_timezone(self):
        # India: UTC+05:30 (tz_offset = 22 quarter-hours)
        field = _datetime_field(2010, 6, 15, 10, 30, 0, tz_offset=22)
        self.assertEqual(_decode_pvd_datetime(field), '2010-06-15T10:30:00+05:30')


class TestParseIso9660Pvd(unittest.TestCase):
    """End-to-end parsing of a synthetic PVD sector written to a temp file."""

    def test_full_metadata_roundtrip(self):
        pvd = _build_pvd(
            system_id=b'WIN32',
            volume_id=b'ARCOLOGY_DISC',
            volume_set_id=b'VOLUMESET',
            publisher=b'ACORN COMPUTERS LTD',
            preparer=b'RETRO PRESERVATION TEAM',
            application=b'CDRECORD 2.01',
            created=_datetime_field(2001, 5, 17, 14, 23, 45, tz_offset=4),
            modified=_datetime_field(2001, 6, 1, 9, 0, 0, tz_offset=4),
        )
        path = _write_iso(pvd)
        try:
            meta = parse_iso9660_pvd(path)
        finally:
            os.unlink(path)

        self.assertEqual(meta['system_identifier'],        'WIN32')
        self.assertEqual(meta['volume_identifier'],        'ARCOLOGY_DISC')
        self.assertEqual(meta['volume_set_identifier'],    'VOLUMESET')
        self.assertEqual(meta['publisher_identifier'],     'ACORN COMPUTERS LTD')
        self.assertEqual(meta['data_preparer_identifier'], 'RETRO PRESERVATION TEAM')
        self.assertEqual(meta['application_identifier'],   'CDRECORD 2.01')
        self.assertEqual(meta['volume_creation_date'],     '2001-05-17T14:23:45+01:00')
        self.assertEqual(meta['volume_modification_date'], '2001-06-01T09:00:00+01:00')
        # Unset dates should be omitted entirely
        self.assertNotIn('volume_expiration_date', meta)
        self.assertNotIn('volume_effective_date', meta)

    def test_unset_text_fields_omitted(self):
        pvd = _build_pvd(volume_id=b'ONLY_VOLUME_ID')
        path = _write_iso(pvd)
        try:
            meta = parse_iso9660_pvd(path)
        finally:
            os.unlink(path)

        self.assertEqual(meta, {'volume_identifier': 'ONLY_VOLUME_ID'})

    def test_invalid_signature_returns_empty(self):
        bad = bytearray(_SECTOR)
        bad[0] = 0x01
        bad[1:6] = b'XX001'  # bad magic
        path = _write_iso(bytes(bad))
        try:
            meta = parse_iso9660_pvd(path)
        finally:
            os.unlink(path)
        self.assertEqual(meta, {})

    def test_wrong_descriptor_type_returns_empty(self):
        # Type 0x02 is a Supplementary VD, not a Primary VD — reject it.
        bad = bytearray(_build_pvd(volume_id=b'TEST'))
        bad[0] = 0x02
        path = _write_iso(bytes(bad))
        try:
            meta = parse_iso9660_pvd(path)
        finally:
            os.unlink(path)
        self.assertEqual(meta, {})

    def test_short_file_returns_empty(self):
        # File shorter than sector 17 → read returns too few bytes.
        fd, path = tempfile.mkstemp(suffix='.iso')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(b'\x00' * 100)
            self.assertEqual(parse_iso9660_pvd(path), {})
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty(self):
        self.assertEqual(parse_iso9660_pvd('/nonexistent/path/that/does/not/exist.iso'), {})


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
