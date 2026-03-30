#!/usr/bin/env python3
"""
Patch render_drawfile.py to read Draw file coordinates as signed integers.

Draw file format stores all bounding-box and path coordinates as signed
32-bit little-endian integers (two's complement).  The original
bytes_to_uint() reads them as *unsigned*, so negative values (objects
left of / below the origin) appear as very large positive numbers.  This
inflates the computed canvas far beyond Cairo's limits, causing:

    cairocffi.CairoError: CAIRO_STATUS_INVALID_SIZE

Fix: add bytes_to_int() that sign-extends the unsigned result, then use it
everywhere a coordinate (bbox corner, path vertex) is read.

Applied to: /opt/drawfile_render/render_drawfile.py
Commit pinned in worker/Dockerfile: 0ef83cb7575aa9ddc4103507936f71cd39df4a32
"""

import sys
from pathlib import Path

TARGET = Path('/opt/drawfile_render/render_drawfile.py')

if not TARGET.exists():
    print(f'ERROR: {TARGET} not found', file=sys.stderr)
    sys.exit(1)

src = TARGET.read_text()

# --------------------------------------------------------------------------
# 1. Insert bytes_to_int() immediately after bytes_to_uint()
# --------------------------------------------------------------------------

BYTES_TO_INT_DEF = '''

def bytes_to_int(size: int, byte_array: bytes, position: int) -> int:
    """
    Convert an array of bytes into a signed integer of arbitrary byte width.

    Identical to bytes_to_uint but sign-extends the result so that values
    with the most-significant bit set are returned as negative numbers.

    :param size:
        The number of bytes in the integer.
    :param byte_array:
        The input array of bytes.
    :param position:
        The position of the start of the integer.
    :return:
        Signed integer value.
    """
    out = bytes_to_uint(size=size, byte_array=byte_array, position=position)
    sign_bit = 1 << (size * 8 - 1)
    if out & sign_bit:
        out -= (sign_bit << 1)
    return out

'''

MARKER = 'def colour_dict_from_int'
if MARKER not in src:
    print('ERROR: could not find insertion point (colour_dict_from_int)', file=sys.stderr)
    sys.exit(1)

if 'def bytes_to_int' not in src:
    src = src.replace(MARKER, BYTES_TO_INT_DEF + MARKER, 1)
    print('Inserted bytes_to_int()')
else:
    print('bytes_to_int() already present — skipping insertion')

# --------------------------------------------------------------------------
# 2. Path coordinates: 'x'/'y' dict keys in fetch_path
#    (MOVE, LINE, BEZIER elements)
# --------------------------------------------------------------------------

coord_keys = ['x', 'y', 'x0', 'y0', 'x1', 'y1', 'x2', 'y2']
for key in coord_keys:
    old = f"'{key}': bytes_to_uint("
    new = f"'{key}': bytes_to_int("
    count = src.count(old)
    if count:
        src = src.replace(old, new)
        print(f"  {key}: replaced {count} occurrence(s)")

# --------------------------------------------------------------------------
# 3. Object bounding box keyword args (x_min=, y_min=, x_max=, y_max=)
# --------------------------------------------------------------------------

for kw in ['x_min', 'y_min', 'x_max', 'y_max']:
    old = f'{kw}=bytes_to_uint('
    new = f'{kw}=bytes_to_int('
    count = src.count(old)
    if count:
        src = src.replace(old, new)
        print(f"  {kw}=: replaced {count} occurrence(s)")

# --------------------------------------------------------------------------
# 4. File header bounding box assignments
#    (self.x_min_as_read, self.y_min_as_read, etc.)
# --------------------------------------------------------------------------

for var in ['x_min_as_read', 'y_min_as_read', 'x_max_as_read', 'y_max_as_read']:
    old = f'self.{var} = bytes_to_uint('
    new = f'self.{var} = bytes_to_int('
    count = src.count(old)
    if count:
        src = src.replace(old, new)
        print(f"  self.{var}: replaced {count} occurrence(s)")

# --------------------------------------------------------------------------
# Write back
# --------------------------------------------------------------------------

TARGET.write_text(src)
print(f'\nPatched {TARGET} successfully.')
