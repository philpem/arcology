"""
IMD (ImageDisk) file parser and filesystem geometry detector.

Parses IMD track 0 headers and sector data, then probes the boot sector
for known filesystem structures to infer the authoritative disk geometry.

Used by the flux decode pipeline to select an appropriate Greaseweazle
format string, avoiding the generic 'ibm.scan' which picks up extra
copy-protection sectors and interleaves them into the output image.
"""

import struct
from pathlib import Path

from .partition import (
    _is_valid_filecore_disc_record,
    _validate_filecore_boot_block_checksum,
    FILECORE_BB_DISC_RECORD_OFFSET,
    detect_fat_filesystem,
)
from ..config import log


# IMD sector size code → bytes per sector
_IMD_SECTOR_SIZES = {0: 128, 1: 256, 2: 512, 3: 1024, 4: 2048, 5: 4096, 6: 8192}


def parse_imd_track0(imd_path: Path) -> dict | None:
    """
    Parse track 0 header and sector data from an IMD file.

    Reads ALL track headers to determine cylinder/head geometry extents
    (copy-protection sectors don't add cylinders or sides, so these counts
    are reliable even on protected discs).  Sector data is stored only for
    cylinder 0, head 0 — just enough for the filesystem boot-structure
    probes in detect_geometry_from_boot_data().

    Returns:
        dict with keys:
            encoding        'FM' or 'MFM' (from first track's mode byte)
            sector_size     bytes per sector (from first track's size code)
            cylinders       max cylinder seen + 1
            heads           max head seen + 1
            sectors         dict[sector_id → bytes] for cylinder 0, head 0
        or None on any parse error.
    """
    try:
        with open(imd_path, 'rb') as f:
            data = f.read()
    except OSError:
        return None

    if not data.startswith(b'IMD '):
        return None

    # Skip the ASCII header comment terminated by 0x1A
    sentinel = data.find(b'\x1A')
    if sentinel < 0:
        return None

    pos = sentinel + 1
    max_cylinder = 0
    max_head = 0
    first_encoding = None
    first_sector_size = None
    track0_sectors: dict[int, bytes] = {}

    while pos < len(data):
        if pos + 5 > len(data):
            break

        mode      = data[pos]
        cylinder  = data[pos + 1]
        head_raw  = data[pos + 2]
        nsec      = data[pos + 3]
        size_code = data[pos + 4]
        pos += 5

        # bit 0 of head_raw is the actual head number;
        # bits 6 and 7 are flags (head-map / cylinder-map present)
        head = head_raw & 0x01
        cyl_map_present  = bool(head_raw & 0x80)
        head_map_present = bool(head_raw & 0x40)

        sector_size = _IMD_SECTOR_SIZES.get(size_code, 0)
        encoding    = 'FM' if mode <= 2 else 'MFM'

        if first_encoding is None:
            first_encoding    = encoding
            first_sector_size = sector_size

        if cylinder > max_cylinder:
            max_cylinder = cylinder
        if head > max_head:
            max_head = head

        # Sector ID map (N bytes)
        if pos + nsec > len(data):
            break
        sector_ids = list(data[pos:pos + nsec])
        pos += nsec

        # Optional cylinder map (N bytes)
        if cyl_map_present:
            if pos + nsec > len(data):
                break
            pos += nsec

        # Optional head map (N bytes)
        if head_map_present:
            if pos + nsec > len(data):
                break
            pos += nsec

        is_track0 = (cylinder == 0 and head == 0)

        for i in range(nsec):
            if pos >= len(data):
                break
            stype = data[pos]
            pos += 1

            if stype == 0:
                # Sector not present — no data bytes follow
                pass
            elif stype % 2 == 1:
                # Odd type (1, 3, 5, 7): raw sector data follows
                if pos + sector_size > len(data):
                    break
                if is_track0 and i < len(sector_ids) and sector_ids[i] not in track0_sectors:
                    track0_sectors[sector_ids[i]] = bytes(data[pos:pos + sector_size])
                pos += sector_size
            else:
                # Even type (2, 4, 6, 8): compressed — 1 fill byte follows
                if pos >= len(data):
                    break
                if is_track0 and i < len(sector_ids) and sector_ids[i] not in track0_sectors:
                    fill = data[pos]
                    track0_sectors[sector_ids[i]] = bytes([fill] * sector_size)
                pos += 1

    if first_encoding is None or first_sector_size is None:
        return None

    return {
        'encoding':    first_encoding,
        'sector_size': first_sector_size,
        'cylinders':   max_cylinder + 1,
        'heads':       max_head + 1,
        'sectors':     track0_sectors,
    }


def detect_geometry_from_boot_data(track0: dict) -> dict | None:
    """
    Probe track 0 sector data for known filesystem boot structures.

    Returns a geometry dict or None if no supported filesystem is identified.

    Return dict keys: filesystem, encoding, cylinders, heads,
                      sectors_per_track, sector_size, probe.
    The 'probe' key is a single letter identifying which probe matched.
    Callers should not pass 'probe' to _geometry_to_gw_format().

    Probe order:
      A — FM encoding → DFS (invariant: SPT=10, sector_size=256)
      D — MFM 1024B, sector 0: ADFS floppy new-map (D/E/F)
      E — MFM 1024B, sector 3: ADFS hard-disc new-map (F/F+)
      B — MFM 256B, Hugo magic: old-map ADFS (S/M/L)  [SPT=16 invariant]
      C — MFM any, FAT BPB in sorted-sector buffer
    """
    encoding    = track0['encoding']
    sector_size = track0['sector_size']
    cylinders   = track0['cylinders']
    heads       = track0['heads']
    sectors     = track0['sectors']

    base = {
        'encoding':    encoding,
        'cylinders':   cylinders,
        'heads':       heads,
        'sector_size': sector_size,
    }

    # ── Probe A: FM encoding → DFS ─────────────────────────────────────────
    # DFS always uses FM (single-density), 256B sectors, 10 SPT.
    # No boot-structure check needed: FM encoding is a sufficient discriminator.
    if encoding == 'FM':
        return {**base, 'filesystem': 'dfs', 'sectors_per_track': 10,
                'sector_size': 256, 'probe': 'A'}

    # ── Probe D: ADFS floppy new-map (D/E/F) ──────────────────────────────
    # Zone-0 boot block is at sector 0 (1024B); disc record starts at byte 4.
    # The first 512 bytes are the Filecore floppy boot block (mod-256 sum = 0).
    # Covers ADFS-D (800K, Hugo dirs), ADFS-E (800K, new-format dirs), and
    # ADFS-F (1600K floppy).  All use the floppy boot block convention.
    #
    # No directory-type probes (Hugo/SBPr/Nick) are needed here:
    #   • The geometry (SPT × sector_size) alone distinguishes every ADFS
    #     variant — (5 × 1024) = 800K, (10 × 1024) = 1600K, (16 × 256) = S/M/L
    #     — so the D/E/F/old sub-type label carries no information for gw format
    #     selection and is omitted.
    #   • SBPr is a RISC OS 4+ directory marker; pre-RO4 ADFS-E discs lack it.
    #   • 'Nick' (directory tail magic for new-format dirs) sits at offset 0x7FC
    #     within a 2048-byte directory block.  Sector 0 holds only the 512-byte
    #     boot block and 512 bytes of zone-0 map — the root directory is not in
    #     sector 0, so Nick is never accessible from track-0 data alone.
    if sector_size == 1024 and 0 in sectors:
        sec0 = sectors[0]
        if len(sec0) >= 512 and sum(sec0[0:512]) & 0xFF == 0:
            disc_record = sec0[4:]
            if _is_valid_filecore_disc_record(disc_record):
                log2ss   = disc_record[0]
                spt      = disc_record[1]
                dr_heads = disc_record[2]
                if spt > 0 and dr_heads > 0:
                    log.debug(f"IMD probe D: ADFS floppy new-map, "
                              f"spt={spt} heads={dr_heads} log2ss={log2ss}")
                    return {**base, 'filesystem': 'adfs',
                            'sectors_per_track': spt,
                            'sector_size': 1 << log2ss,
                            'heads': dr_heads,
                            'probe': 'D'}

    # ── Probe E: ADFS hard-disc new-map (F/F+) ────────────────────────────
    # Filecore hard-disc boot block is at disc address 0xC00.
    # For 1024B sectors (0-based IDs), that is sector 3 (3 × 1024 = 3072 = 0xC00).
    # The boot block uses carry-propagation checksum (stronger than mod-256 sum).
    if sector_size == 1024 and 3 in sectors:
        sec3 = sectors[3]
        if len(sec3) >= 512 and _validate_filecore_boot_block_checksum(sec3[:512]):
            disc_record = sec3[FILECORE_BB_DISC_RECORD_OFFSET:]
            if _is_valid_filecore_disc_record(disc_record):
                log2ss   = disc_record[0]
                spt      = disc_record[1]
                dr_heads = disc_record[2]
                if spt > 0 and dr_heads > 0:
                    log.debug(f"IMD probe E: ADFS hard-disc new-map, "
                              f"spt={spt} heads={dr_heads} log2ss={log2ss}")
                    return {**base, 'filesystem': 'adfs',
                            'sectors_per_track': spt,
                            'sector_size': 1 << log2ss,
                            'heads': dr_heads,
                            'probe': 'E'}

    # ── Probe B: Old-map ADFS (S/M/L) ─────────────────────────────────────
    # Old-map ADFS uses MFM, 256B sectors, 16 SPT (invariant).
    # The root directory starts at disc address 0x200 (sector 2, 0-based).
    # Its header begins with a master sequence number then the 4-byte "Hugo" magic.
    # We scan all track-0 sectors by content to be robust against non-standard IDs.
    # SPT is hard-coded to 16 — do NOT use the IMD value, which may be inflated
    # by copy-protection sectors on the track.
    if sector_size == 256:
        for sec_data in sectors.values():
            if len(sec_data) >= 5 and sec_data[1:5] == b'Hugo':
                log.debug("IMD probe B: old-map ADFS (Hugo signature)")
                return {**base, 'filesystem': 'adfs', 'sectors_per_track': 16,
                        'probe': 'B'}

    # ── Probe C: FAT BPB ──────────────────────────────────────────────────
    # Sort sectors by ID and concatenate.  For IBM-format discs the smallest
    # sector ID is 1 (sectors run 1…N), so the first entry is the FAT boot
    # sector.  detect_fat_filesystem() already validates all BPB fields; the
    # BPB's own SPT and NumHeads fields are authoritative geometry.
    if sectors:
        assembled = b''.join(v for _, v in sorted(sectors.items()))
        fat_type  = detect_fat_filesystem(assembled[:512])
        if fat_type is not None:
            bpb       = assembled[:512]
            bpb_spt   = struct.unpack_from('<H', bpb, 24)[0]
            bpb_heads = struct.unpack_from('<H', bpb, 26)[0]
            bpb_bps   = struct.unpack_from('<H', bpb, 11)[0]
            tot16     = struct.unpack_from('<H', bpb, 19)[0]
            tot32     = struct.unpack_from('<I', bpb, 32)[0]
            total_sec = tot16 if tot16 != 0 else tot32
            if bpb_spt > 0 and bpb_heads > 0 and total_sec > 0:
                cyl = total_sec // (bpb_spt * bpb_heads)
                log.debug(f"IMD probe C: FAT ({fat_type}), "
                          f"spt={bpb_spt} heads={bpb_heads} cyl={cyl} bps={bpb_bps}")
                return {**base, 'filesystem': 'fat',
                        'sectors_per_track': bpb_spt,
                        'heads': bpb_heads,
                        'cylinders': cyl,
                        'sector_size': bpb_bps,
                        'probe': 'C'}

    return None

# vim: ts=4 sw=4 et
