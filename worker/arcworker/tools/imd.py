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
from ..config import log
from .base import read_file_capped
from .partition import (
    FILECORE_BB_DISC_RECORD_OFFSET,
    _is_valid_filecore_disc_record,
    _validate_filecore_boot_block_checksum,
    detect_fat_filesystem,
)

# IMD sector size code → bytes per sector
_IMD_SECTOR_SIZES = {0: 128, 1: 256, 2: 512, 3: 1024, 4: 2048, 5: 4096, 6: 8192}


def _read_imd_data(imd_path: Path) -> tuple[bytes, int] | None:
    """Read an IMD file and locate the start of the track records.

    Returns ``(data, pos)`` where ``pos`` is the byte after the 0x1A
    sentinel terminating the ASCII comment header, or None if the file
    cannot be read or is not an IMD.
    """
    try:
        data = read_file_capped(imd_path)
    except OSError:
        # Unreadable, or implausibly large for an IMD floppy image — the cap
        # stops an over-size file being slurped into RAM.
        return None
    if not data.startswith(b'IMD '):
        return None
    sentinel = data.find(b'\x1A')
    if sentinel < 0:
        return None
    return data, sentinel + 1


def _parse_track_header(data: bytes, pos: int) -> tuple[dict | None, int | None]:
    """Parse one IMD track header plus its sector-ID/cylinder/head maps.

    Returns ``(hdr, new_pos)`` on success.  ``hdr`` contains: mode,
    cylinder, head (bit 0 of the raw head byte), nsec, size_code,
    sector_size, encoding ('FM' for modes 0-2, 'MFM' for 3-5),
    sector_ids, and the per-sector sector_cyls / idam_heads lists
    (taken from the optional maps when present, otherwise filled with
    the track's cylinder/head).

    Returns ``(None, None)`` when fewer than 5 header bytes remain, and
    ``(hdr, None)`` when the header was read but a map is truncated —
    callers can still use the header fields (e.g. for geometry extents)
    but must stop walking the file.
    """
    if pos + 5 > len(data):
        return None, None

    mode      = data[pos]
    cylinder  = data[pos + 1]
    head_raw  = data[pos + 2]
    nsec      = data[pos + 3]
    size_code = data[pos + 4]
    pos += 5

    # bit 0 of head_raw is the actual head number;
    # bits 6 and 7 are flags (head-map / cylinder-map present)
    head             = head_raw & 0x01
    cyl_map_present  = bool(head_raw & 0x80)
    head_map_present = bool(head_raw & 0x40)

    hdr = {
        'mode':        mode,
        'cylinder':    cylinder,
        'head':        head,
        'nsec':        nsec,
        'size_code':   size_code,
        'sector_size': _IMD_SECTOR_SIZES.get(size_code, 0),
        'encoding':    'FM' if mode <= 2 else 'MFM',
        'sector_ids':  [],
        'sector_cyls': [cylinder] * nsec,
        'idam_heads':  [head] * nsec,
    }

    # Sector ID map (N bytes)
    if pos + nsec > len(data):
        return hdr, None
    hdr['sector_ids'] = list(data[pos:pos + nsec])
    pos += nsec

    # Optional cylinder map → per-sector IDAM cylinders
    if cyl_map_present:
        if pos + nsec > len(data):
            return hdr, None
        hdr['sector_cyls'] = list(data[pos:pos + nsec])
        pos += nsec

    # Optional head map → per-sector IDAM heads
    if head_map_present:
        if pos + nsec > len(data):
            return hdr, None
        hdr['idam_heads'] = list(data[pos:pos + nsec])
        pos += nsec

    return hdr, pos


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
    loaded = _read_imd_data(imd_path)
    if loaded is None:
        return None
    data, pos = loaded

    max_cylinder = 0
    max_head = 0
    first_encoding = None
    first_sector_size = None
    track0_sectors: dict[int, bytes] = {}

    while pos < len(data):
        hdr, new_pos = _parse_track_header(data, pos)
        if hdr is None:
            break

        if first_encoding is None:
            first_encoding    = hdr['encoding']
            first_sector_size = hdr['sector_size']

        max_cylinder = max(max_cylinder, hdr['cylinder'])
        max_head     = max(max_head, hdr['head'])

        if new_pos is None:
            break          # truncated map — header counted, stop walking
        pos = new_pos

        sector_size = hdr['sector_size']
        sector_ids  = hdr['sector_ids']
        is_track0   = (hdr['cylinder'] == 0 and hdr['head'] == 0)

        for i in range(hdr['nsec']):
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


def parse_imd_tracks(imd_path: Path) -> list[dict] | None:
    """
    Parse every track header in an IMD file and return track metadata.

    Unlike parse_imd_track0(), this reads the optional cylinder map for each
    track so that per-sector IDAM cylinder numbers are available.  Sector data
    bytes are read to determine is_uniform_fill but are not stored.

    Returns a list of track dicts (in file order), each containing:
        physical_index  int        0-based position in file (= physical track number)
        cylinder        int        cylinder byte from track header
        head            int        head number (0 or 1) — physical head
        encoding        str        'FM' or 'MFM'
        sector_size     int        bytes per sector
        sector_ids      list[int]  sector ID map
        sector_cyls     list[int]  per-sector IDAM cylinder — from cylinder map
                                   if present, otherwise [cylinder] * nsec
        idam_heads      list[int]  per-sector IDAM head — from head map if present,
                                   otherwise [head] * nsec (physical head repeated)
        has_data        bool       True if any sector type != 0
        is_uniform_fill bool       True if every present sector consists of a
                                   single repeated byte value (all compressed, or
                                   all raw bytes equal).  Empty tracks are True.
                                   False as soon as any sector has varied content
                                   or two sectors have different fill bytes.

    Returns None on parse error.
    """
    loaded = _read_imd_data(imd_path)
    if loaded is None:
        return None
    data, pos = loaded

    tracks = []
    physical_index = 0

    while pos < len(data):
        hdr, new_pos = _parse_track_header(data, pos)
        if hdr is None or new_pos is None:
            break          # end of file or truncated map — drop partial track
        pos = new_pos

        sector_size = hdr['sector_size']

        # Read sector data: determine has_data and is_uniform_fill.
        # is_uniform_fill tracks whether every present sector consists of a
        # single repeated byte value (same fill byte across all sectors).
        has_data        = False
        is_uniform_fill = True          # set False on first varied or mismatched byte
        track_fill: int | None = None   # fill byte established by first sector with data

        for _ in range(hdr['nsec']):
            if pos >= len(data):
                break
            stype = data[pos]
            pos += 1
            if stype == 0:
                pass                        # not present — no data follows
            elif stype % 2 == 1:
                # Raw sector data — read and check uniformity
                if pos + sector_size > len(data):
                    break
                sec = data[pos:pos + sector_size]
                pos += sector_size
                has_data = True
                if is_uniform_fill and sec:
                    fill = sec[0]
                    if all(b == fill for b in sec):
                        if track_fill is None:
                            track_fill = fill
                        elif track_fill != fill:
                            is_uniform_fill = False
                    else:
                        is_uniform_fill = False
            else:
                # Compressed — 1 fill byte (always uniform by definition)
                if pos >= len(data):
                    break
                fill = data[pos]
                pos += 1
                has_data = True
                if is_uniform_fill:
                    if track_fill is None:
                        track_fill = fill
                    elif track_fill != fill:
                        is_uniform_fill = False

        tracks.append({
            'physical_index':  physical_index,
            'cylinder':        hdr['cylinder'],
            'head':            hdr['head'],
            'encoding':        hdr['encoding'],
            'sector_size':     sector_size,
            'sector_ids':      hdr['sector_ids'],
            'sector_cyls':     hdr['sector_cyls'],
            'idam_heads':      hdr['idam_heads'],
            'has_data':        has_data,
            'is_uniform_fill': is_uniform_fill,
        })
        physical_index += 1

    return tracks if tracks else None


def detect_track_density_mismatch(tracks: list[dict]) -> dict:
    """
    Detect a 40-track disc captured in an 80-track drive (track density mismatch).

    The tell-tale pattern: on even tracks with data, sector IDAM cylinders equal
    half the track number (e.g. track 4 reports cylinder 2).

    Variants are distinguished by what the odd tracks contain:

    1. Simple double-step read: the 40-track disc was imaged directly.
       Odd tracks have no decodeable sectors (between-track reads).
       → odd_tracks_with_varied_data == 0, odd_tracks_with_uniform_data == 0

    2. Alignment / wide-head case: the same 40-track data is decoded on both
       half-steps, so odd tracks also report half the track number.
       → odd_tracks_with_duplicate_data > 0

    3. Reformat case: an 80-track disc was reformatted as 40-track, leaving
       the old 80-track data on the unwritten odd tracks.
       Odd tracks contain real sectors where IDAM cylinder == track number.
       → odd_tracks_with_varied_data > 0 (real leftover data)
         or odd_tracks_with_uniform_data > 0 (formatted-empty leftover tracks)

    The detection criterion (confidence on even tracks) is the same for both.
    The caller uses odd_tracks_with_varied_data to decide whether to warn.

    Returns:
        detected                    bool
        confidence                  float  matching / checked (0.0 if checked == 0)
        matching                    int    even tracks where all sector_cyls == cylinder // 2
        checked                     int    even tracks with has_data tested
        odd_tracks_with_duplicate_data int odd tracks where all sector_cyls == cylinder // 2
        odd_tracks_with_varied_data int    odd tracks with non-uniform sector data
        odd_tracks_with_uniform_data int   odd tracks with uniform-fill sector data
        data_heads                  list[int] heads with any decoded sectors
        blank_heads                 list[int] heads with no decoded sectors
    """
    matching = 0
    checked  = 0
    odd_duplicate = 0
    odd_varied  = 0
    odd_uniform = 0
    all_heads: set[int] = set()
    data_heads: set[int] = set()

    for t in tracks:
        all_heads.add(t['head'])
        if not t['has_data']:
            continue
        data_heads.add(t['head'])

        if t['cylinder'] % 2 == 0:
            checked += 1
            expected = t['cylinder'] // 2
            if t['sector_cyls'] and all(c == expected for c in t['sector_cyls']):
                matching += 1
        else:
            expected = t['cylinder'] // 2
            if t['sector_cyls'] and all(c == expected for c in t['sector_cyls']):
                odd_duplicate += 1
            elif t['is_uniform_fill']:
                odd_uniform += 1
            else:
                odd_varied += 1

    confidence = matching / checked if checked > 0 else 0.0
    detected   = checked >= 6 and confidence >= 0.70
    data_head_list = sorted(data_heads)
    blank_head_list = sorted(all_heads - data_heads)

    return {
        'detected':                   detected,
        'confidence':                 confidence,
        'matching':                   matching,
        'checked':                    checked,
        'odd_tracks_with_duplicate_data': odd_duplicate,
        'odd_tracks_with_varied_data':   odd_varied,
        'odd_tracks_with_uniform_data':  odd_uniform,
        'data_heads':                 data_head_list,
        'blank_heads':                blank_head_list,
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
    # The Filecore zone checksum covers the entire zone.  For many Acorn tools
    # the checksum covers only the first 512 bytes (the "floppy boot block").
    # Accept either scope.  If neither checksum passes (non-standard formatting
    # tools or flux→HFE→IMD conversion artefacts), fall back to disc_size field
    # alignment: disc_size must be a whole number of sectors.  Together with the
    # disc record validity check (log2ss ∈ {8,9,10,12}, disc_size > 0, spt > 0,
    # heads > 0) the false-positive rate without a checksum is ≈ 4/256 × 1/256
    # ≈ 0.006 %.
    # Covers ADFS-D (800K, Hugo dirs), ADFS-E (800K, new-format dirs), and
    # ADFS-F (1600K floppy).  All use the floppy boot block convention.
    #
    # Cylinder count is derived from the disc record's disc_size field rather
    # than from the IMD track count, which can be inflated by hxcfe writing
    # extra empty padding tracks beyond the actual disc capacity.
    if sector_size == 1024 and 0 in sectors:
        sec0 = sectors[0]
        if len(sec0) >= 512:
            disc_record = sec0[4:]
            log.debug(
                f"IMD probe D: sec0[0:24]={sec0[0:24].hex()} "
                f"chk512=0x{sum(sec0[0:512])&0xFF:02X} "
                f"chk1024=0x{sum(sec0)&0xFF:02X} "
                f"log2ss={sec0[4]} spt={sec0[5]} heads={sec0[6]} "
                f"disc_size=0x{struct.unpack_from('<I', sec0, 0x14)[0]:08X}"
            )
            if _is_valid_filecore_disc_record(disc_record):
                log2ss          = disc_record[0]
                spt             = disc_record[1]
                dr_heads        = disc_record[2]
                sector_size_dr  = 1 << log2ss
                disc_size_bytes = struct.unpack_from('<I', disc_record, 0x10)[0]
                checksum_ok  = (sum(sec0[0:512]) & 0xFF == 0 or sum(sec0) & 0xFF == 0)
                size_aligned = disc_size_bytes > 0 and disc_size_bytes % sector_size_dr == 0
                if spt > 0 and dr_heads > 0 and (checksum_ok or size_aligned):
                    auth_cylinders = (disc_size_bytes // (spt * sector_size_dr * dr_heads)
                                      if disc_size_bytes > 0 else cylinders)
                    log.debug(f"IMD probe D: ADFS floppy new-map, "
                              f"spt={spt} heads={dr_heads} log2ss={log2ss} "
                              f"disc_size={disc_size_bytes} → cylinders={auth_cylinders}")
                    return {**base, 'filesystem': 'adfs',
                            'cylinders':        auth_cylinders,
                            'sectors_per_track': spt,
                            'sector_size':       sector_size_dr,
                            'heads':             dr_heads,
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
                    sector_size_dr = 1 << log2ss
                    disc_size_bytes = struct.unpack_from('<I', disc_record, 0x10)[0]
                    if disc_size_bytes > 0 and sector_size_dr > 0:
                        auth_cylinders = disc_size_bytes // (spt * sector_size_dr * dr_heads)
                    else:
                        auth_cylinders = cylinders
                    log.debug(f"IMD probe E: ADFS hard-disc new-map, "
                              f"spt={spt} heads={dr_heads} log2ss={log2ss} "
                              f"disc_size={disc_size_bytes} → cylinders={auth_cylinders}")
                    return {**base, 'filesystem': 'adfs',
                            'cylinders':         auth_cylinders,
                            'sectors_per_track': spt,
                            'sector_size':       sector_size_dr,
                            'heads':             dr_heads,
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


def detect_independent_sides(tracks: list[dict]) -> dict:
    """
    Detect a double-sided disc whose two physical sides were captured as if
    they were two independent single-sided discs.

    This happens when a double-sided drive has its head-select wired to a
    drive-select pin on the controller — to the controller and filesystem,
    the second side looks like another single-sided drive (e.g. BBC Micro
    drives 0/2, RM 380Z/480Z).  Each side records IDAM head = 0 regardless
    of which physical head wrote it.

    The tell: every sector on physical head 1 has IDAM head = 0.

    Contrast with a normal double-sided disc (e.g. ADFS-L) where sectors on
    physical head 1 correctly record IDAM head = 1.

    Requirements for detection:
    - Both physical heads 0 and 1 have tracks with data.
    - Every sector on physical head 1 tracks has IDAM head = 0.
    - There is at least one track with data on physical head 1.

    Returns:
        detected        bool    True when all conditions met
        reason          str     Human-readable explanation
        h1_tracks       int     Number of physical head-1 tracks with data
        h1_idam_all_zero bool   All head-1 sectors had IDAM head = 0
    """
    h0_data = False
    h1_data = False
    h1_tracks_with_data = 0
    h1_idam_all_zero = True  # innocent until proven guilty

    for t in tracks:
        if not t['has_data']:
            continue
        if t['head'] == 0:
            h0_data = True
        elif t['head'] == 1:
            h1_data = True
            h1_tracks_with_data += 1
            # idam_heads is [head] * nsec when no head map present, meaning
            # IDAM head == physical head (1) — a normal double-sided disc.
            if not all(h == 0 for h in t['idam_heads']):
                h1_idam_all_zero = False

    detected = h0_data and h1_data and h1_idam_all_zero and h1_tracks_with_data > 0

    if detected:
        reason = (
            f"Physical head 1 has {h1_tracks_with_data} track(s) with data, "
            f"all recording IDAM head=0 — each side is an independent single-sided disc"
        )
    elif not h0_data:
        reason = "No data on physical head 0"
    elif not h1_data:
        reason = "No data on physical head 1 (single-sided disc)"
    else:
        reason = "Physical head 1 sectors record IDAM head=1 — double-sided disc"

    return {
        'detected':         detected,
        'reason':           reason,
        'h1_tracks':        h1_tracks_with_data,
        'h1_idam_all_zero': h1_idam_all_zero,
    }

# vim: ts=4 sw=4 et
