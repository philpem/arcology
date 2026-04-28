"""
Tests for extract_xfiles() — pure-Python X-Files archive extractor.

Constructs minimal valid X-Files images in memory and exercises the extractor,
security checks, and metadata extraction without requiring any external tools or
a running database.
"""

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('WORKER_API_KEY', 'test')


# ---------------------------------------------------------------------------
# X-Files image builder helpers
# ---------------------------------------------------------------------------

_XFILES_MAGIC = b'XFIL'
_XFILES_DIR_SIG = b'Andy'
_XFILES_ATTR_ISDIR = 0x100
_XFILES_ATTR_OWNER_RW = 0x03   # owner read + write
_ALLOC_UNIT = 1024


def _pack_chunk(offset: int, size: int, usage: int = 0, alloc_size: int = 0) -> bytes:
    return struct.pack('<IIII', offset, size, usage, alloc_size)


def _round_up(n: int, align: int) -> int:
    return (n + align - 1) & ~(align - 1)


def _dir_entry(name: bytes, load: int, exec_: int, fsize: int, attr: int) -> bytes:
    """Build a packed xFiles_dirEntry (without padding to 4-byte boundary)."""
    name_len = len(name)
    entry = struct.pack('<IIIII', load, exec_, fsize, attr, name_len)
    entry += name + b'\x00'
    # Pad to 4-byte boundary
    pad = (-len(entry)) % 4
    entry += b'\x00' * pad
    return entry


def _dir_hash(name_start: bytes, entry_pos: int, node: int) -> bytes:
    """Build a packed xFiles_dirHash record."""
    ns = (name_start + b'\x00\x00\x00\x00')[:4]
    return ns + struct.pack('<II', entry_pos, node)


def _build_dir_chunk(
    parent: int,
    entries: list,   # list of (name_bytes, load, exec_, fsize, attr, node_chunk)
) -> bytes:
    """Build a directory chunk payload.

    Returns raw bytes ready to be placed in a chunk slot.
    """
    num = len(entries)
    # Hash table: num slots, each 12 bytes
    hash_size = num
    hash_used = num

    # Calculate entryPos for each entry.
    # Entries are packed immediately after the hash table.
    hash_end = 16 + hash_size * 12  # relative to start of chunk payload
    entry_bytes_list = []
    entry_positions = []
    pos = hash_end
    for (name_bytes, load, exec_, fsize, attr, _node) in entries:
        entry_positions.append(pos)
        eb = _dir_entry(name_bytes, load, exec_, fsize, attr)
        entry_bytes_list.append(eb)
        pos += len(eb)

    # Build directory header
    header = _XFILES_DIR_SIG + struct.pack('<III', parent, hash_size, hash_used)

    # Build hash table
    hash_table = b''
    for i, (name_bytes, _load, _exec, _fsize, _attr, node) in enumerate(entries):
        ns = (name_bytes + b'\x00\x00\x00\x00')[:4]
        hash_table += ns + struct.pack('<II', entry_positions[i], node)

    # Concatenate everything
    return header + hash_table + b''.join(entry_bytes_list)


def _build_xfiles_image(root_entries: list, file_data: dict) -> bytes:
    """Build a minimal X-Files image.

    Args:
        root_entries: list of (name_bytes, load, exec_, fsize, attr, node_chunk)
                      where node_chunk is assigned automatically.
        file_data:    mapping of node_chunk_number → file_bytes for file entries.
                      Directory entries are handled automatically.

    Returns raw image bytes.
    """
    # We lay out the image as:
    #   offset 0:           header (52 bytes)
    #   offset 1024:        chunk table (chunk 0)
    #   offset 2048:        root directory (chunk 1)
    #   offset 3072+:       file/sub-directory chunks

    alloc = _ALLOC_UNIT
    header_size = 52

    # Determine chunk layout.
    # Chunk 0: chunk table
    # Chunk 1: root directory
    # Remaining chunks: file data / subdirectory data (as provided in file_data)
    #
    # We first build all chunk payloads to know their sizes.

    # Build root directory payload
    root_dir_payload = _build_dir_chunk(0, root_entries)

    # Collect all chunk payloads in order: [chunk_table, root_dir, ...]
    # We need to compute offsets. We'll do two passes.

    # Pass 1: compute payload sizes
    payloads = {}  # chunk_num -> bytes
    payloads[1] = root_dir_payload
    for chunk_num, data in file_data.items():
        payloads[chunk_num] = data

    max_chunk = max(payloads.keys()) if payloads else 1
    num_chunks = max_chunk + 1  # chunk 0 is the chunk table itself

    # Pass 2: assign file offsets
    # Chunk 0 (chunk table) comes first at alloc offset.
    # Then chunk 1, then 2, etc.
    ct_size = num_chunks * 16  # 16 bytes per chunk record
    ct_alloc = _round_up(ct_size, alloc)

    offsets = {}
    current_offset = alloc  # chunk table starts at alloc (1024)
    offsets[0] = current_offset
    current_offset += ct_alloc

    for chunk_num in range(1, num_chunks):
        offsets[chunk_num] = current_offset
        payload = payloads.get(chunk_num, b'')
        alloc_size = _round_up(max(len(payload), 1), alloc)
        current_offset += alloc_size

    # Build chunk table bytes
    chunk_table_bytes = b''
    for i in range(num_chunks):
        payload = payloads.get(i, b'')
        if i == 0:
            # Chunk table entry for itself
            chunk_table_bytes += _pack_chunk(
                offsets[0], ct_size, 0, ct_alloc
            )
        else:
            payload_size = len(payload)
            payload_alloc = _round_up(max(payload_size, 1), alloc)
            chunk_table_bytes += _pack_chunk(
                offsets[i], payload_size, 0, payload_alloc
            )
    assert len(chunk_table_bytes) == ct_size

    # Build file header
    # chunkTable descriptor: offset=offsets[0], size=ct_size, usage=0, allocSize=ct_alloc
    # rootChunk = 1
    header = _XFILES_MAGIC
    header += struct.pack('<III', header_size, 1, 1)  # hdrSize, structVer, dirVer
    header += struct.pack('<IIII', offsets[0], ct_size, 0, ct_alloc)  # chunkTable
    header += struct.pack('<I', 1)    # rootChunk
    header += struct.pack('<I', alloc)  # allocationUnit
    header += struct.pack('<I', 0)    # freeChunk
    header += struct.pack('<I', 0)    # waste
    # The spec states hdrSize = 52; the listed fields account for 48 bytes —
    # the C struct in the original source has an additional uint32 at 0x30
    # (possibly padding or an undocumented field).  Pad to match.
    header += b'\x00\x00\x00\x00'
    assert len(header) == 52

    # Assemble image
    image = bytearray(current_offset)
    image[0:52] = header
    image[offsets[0]:offsets[0] + len(chunk_table_bytes)] = chunk_table_bytes
    for chunk_num in range(1, num_chunks):
        payload = payloads.get(chunk_num, b'')
        if payload:
            image[offsets[chunk_num]:offsets[chunk_num] + len(payload)] = payload

    return bytes(image)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestXFilesExtraction(unittest.TestCase):

    def _run_extract(self, image_bytes: bytes):
        """Write *image_bytes* to a temp file, run extract_xfiles, return result."""
        from worker.arcworker.tools.archives import extract_xfiles

        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / 'test.xfiles'
            out = Path(tmpdir) / 'output'
            src.write_bytes(image_bytes)
            result = extract_xfiles(src, out)
            # Collect extracted files before tmpdir is removed.
            files = {}
            if result.get('success') and out.exists():
                for p in out.rglob('*'):
                    if p.is_file():
                        files[str(p.relative_to(out))] = p.read_bytes()
            return result, files

    # ── Basic extraction ─────────────────────────────────────────────────────

    def test_empty_archive(self):
        """An archive with an empty root directory extracts successfully."""
        img = _build_xfiles_image([], {})
        result, files = self._run_extract(img)
        self.assertTrue(result['success'], result.get('error'))
        self.assertEqual(result['file_count'], 0)
        self.assertEqual(files, {})

    def test_single_file(self):
        """A root-level file is extracted with correct content."""
        content = b'Hello from X-Files!'
        # File at chunk 2, date-stamped filetype &FFF (text)
        # load = 0xFFFFF00 gives filetype 0xFF0 -- use a real RISC OS type instead
        # Filetype &FFF (Text): load = 0xFFFFFF00 (top 12 bits = 0xFFF, bits 19:8 = 0xFFF)
        # Actually: (load >> 20) == 0xFFF means top 12 bits = 0xFFF
        # load = 0xFFFFFF00: bits 31:20 = 0xFFF, bits 19:8 = 0xFF → filetype = 0xFF? No.
        # bits 19:8 of 0xFFFFFF00: (0xFFFFFF00 >> 8) & 0xFFF = 0xFFFF & 0xFFF = 0xFFF
        # So filetype = 0xFFF which is &FFF (Text file in RISC OS)
        load = 0xFFFFFF00
        exec_ = 0x00000000
        entries = [
            (b'MyFile', load, exec_, len(content), _XFILES_ATTR_OWNER_RW, 2),
        ]
        img = _build_xfiles_image(entries, {2: content})
        result, files = self._run_extract(img)
        self.assertTrue(result['success'], result.get('error'))
        self.assertEqual(result['file_count'], 1)
        self.assertIn('MyFile', files)
        self.assertEqual(files['MyFile'], content)

    def test_file_with_inf_metadata(self):
        """inf_metadata is returned with correct load/exec/filetype/attributes."""
        content = b'data'
        load = 0xFFFFFF00   # date-stamped, filetype &FFF
        exec_ = 0xAB12CD34
        attr = _XFILES_ATTR_OWNER_RW  # 0x03
        entries = [(b'ReadMe', load, exec_, len(content), attr, 2)]
        img = _build_xfiles_image(entries, {2: content})

        import sys
        repo_root = str(Path(__file__).parent.parent)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from worker.arcworker.tools.archives import extract_xfiles

        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / 'test.xfiles'
            out = Path(tmpdir) / 'output'
            src.write_bytes(img)
            result = extract_xfiles(src, out)

        self.assertTrue(result['success'], result.get('error'))
        meta = result.get('inf_metadata', {})
        self.assertIn('ReadMe', meta)
        entry = meta['ReadMe']
        self.assertEqual(entry['load_address'], f'{load:08x}')
        self.assertEqual(entry['exec_address'], f'{exec_:08x}')
        self.assertEqual(entry['risc_os_filetype'], 'fff')
        self.assertEqual(entry['attributes'], f'{attr & 0xFF:02x}')

    def test_subdirectory(self):
        """Files in subdirectories are extracted to the correct path."""
        content = b'nested file content'
        # Sub-directory at chunk 2 (dir), file at chunk 3 (data)
        subdir_entries = [
            (b'nested.txt', 0x00000000, 0x00000000, len(content), _XFILES_ATTR_OWNER_RW, 3),
        ]
        subdir_payload = _build_dir_chunk(1, subdir_entries)
        root_entries = [
            (b'SubDir', 0, 0, 0, _XFILES_ATTR_ISDIR | _XFILES_ATTR_OWNER_RW, 2),
        ]
        img = _build_xfiles_image(root_entries, {2: subdir_payload, 3: content})
        result, files = self._run_extract(img)
        self.assertTrue(result['success'], result.get('error'))
        self.assertEqual(result['file_count'], 1)
        extracted_path = 'SubDir/nested.txt'
        self.assertIn(extracted_path, files)
        self.assertEqual(files[extracted_path], content)

    def test_multiple_files(self):
        """Multiple root-level files are all extracted."""
        files_data = {
            b'Alpha': b'aaa',
            b'Beta': b'bbb',
            b'Gamma': b'ccc',
        }
        entries = []
        payloads = {}
        for i, (name, data) in enumerate(files_data.items(), start=2):
            entries.append((name, 0, 0, len(data), _XFILES_ATTR_OWNER_RW, i))
            payloads[i] = data
        img = _build_xfiles_image(entries, payloads)
        result, files = self._run_extract(img)
        self.assertTrue(result['success'], result.get('error'))
        self.assertEqual(result['file_count'], 3)
        for name, data in files_data.items():
            self.assertEqual(files[name.decode()], data)

    # ── Security / error handling ─────────────────────────────────────────────

    def test_bad_magic_rejected(self):
        """An archive with wrong magic bytes fails cleanly."""
        img = b'BADM' + b'\x00' * 200
        result, _files = self._run_extract(img)
        self.assertFalse(result['success'])
        self.assertIn('magic', result['error'].lower())

    def test_truncated_header_rejected(self):
        """A file shorter than 52 bytes is rejected."""
        result, _files = self._run_extract(b'XFIL\x00\x00')
        self.assertFalse(result['success'])

    def test_path_traversal_dotdot_rejected(self):
        """A filename of '..' is rejected."""
        content = b'evil'
        entries = [(b'..', 0, 0, len(content), _XFILES_ATTR_OWNER_RW, 2)]
        img = _build_xfiles_image(entries, {2: content})
        result, _files = self._run_extract(img)
        self.assertFalse(result['success'])

    def test_path_traversal_slash_rejected(self):
        """A filename containing '/' is rejected."""
        content = b'evil'
        entries = [(b'sub/evil', 0, 0, len(content), _XFILES_ATTR_OWNER_RW, 2)]
        img = _build_xfiles_image(entries, {2: content})
        result, _files = self._run_extract(img)
        self.assertFalse(result['success'])

    def test_dot_filename_rejected(self):
        """A filename of '.' is rejected."""
        content = b'evil'
        entries = [(b'.', 0, 0, len(content), _XFILES_ATTR_OWNER_RW, 2)]
        img = _build_xfiles_image(entries, {2: content})
        result, _files = self._run_extract(img)
        self.assertFalse(result['success'])

    def test_unsupported_version_rejected(self):
        """A structure version other than 1 is rejected."""
        img = bytearray(_build_xfiles_image([], {}))
        # Patch structureVersion at offset 8 to 2
        struct.pack_into('<I', img, 8, 2)
        result, _files = self._run_extract(bytes(img))
        self.assertFalse(result['success'])
        self.assertIn('version', result['error'].lower())

    def test_out_of_range_chunk_rejected(self):
        """A directory entry pointing to a non-existent chunk is rejected."""
        content = b'data'
        # node = 999 — far outside the chunk table
        entries = [(b'file.txt', 0, 0, len(content), _XFILES_ATTR_OWNER_RW, 999)]
        img = _build_xfiles_image(entries, {})
        result, _files = self._run_extract(img)
        self.assertFalse(result['success'])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
