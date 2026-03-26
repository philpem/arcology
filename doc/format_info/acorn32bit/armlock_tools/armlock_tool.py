#!/usr/bin/env python3
"""
armlock_tool.py — Detect and remove ARMlock protection from Filecore disc images.

Parses the boot block disc record to compute where the zone map and root
directory live on disc, checks for the "ARMlock installed" signature in the
padding area of the stripped root directory, and optionally restores the
real root directory (stashed at disc address 0x400) to its proper location.

Can also extract the ARMlock module itself from the stripped root directory's
!Boot entry by walking the zone map to resolve its SIN.

Usage:
    python3 armlock_tool.py image.dat                  # detect only
    python3 armlock_tool.py image.dat --remove          # remove in place
    python3 armlock_tool.py image.dat --remove -o out.dat  # write to new file
    python3 armlock_tool.py image.dat --extract-module  # save ARMlock module
"""

import sys
import struct
import argparse


# ---------------------------------------------------------------------------
# Disc record parsing
# ---------------------------------------------------------------------------

def read_disc_record(image, offset=0xDC0):
    """Parse the 64-byte disc record from the boot block."""
    d = image[offset:offset + 0x40]
    if len(d) < 0x20:
        raise ValueError("Image too small to contain a disc record")

    rec = {
        "log2_sector_size": d[0x00],
        "sectors_per_track": d[0x01],
        "heads": d[0x02],
        "density": d[0x03],
        "idlen": d[0x04],
        "log2_bpmb": d[0x05],
        "skew": d[0x06],
        "boot_option": d[0x07],
        "low_sector": d[0x08],
        "nzones_lo": d[0x09],
        "zone_spare": struct.unpack_from("<H", d, 0x0A)[0],
        "root_dir": struct.unpack_from("<I", d, 0x0C)[0],
        "disc_size_lo": struct.unpack_from("<I", d, 0x10)[0],
    }

    # Extended fields (RISC OS 3.6+)
    if len(d) >= 0x30:
        rec["disc_id"] = struct.unpack_from("<H", d, 0x14)[0]
        rec["disc_name"] = d[0x16:0x20].rstrip(b"\x00").decode("ascii", errors="replace")
        rec["disc_size_hi"] = struct.unpack_from("<I", d, 0x24)[0]
        rec["share_size"] = d[0x28]
        rec["big_flag"] = d[0x29]
        rec["nzones_hi"] = d[0x2A]
        rec["format_version"] = struct.unpack_from("<I", d, 0x2C)[0]
    else:
        rec["disc_name"] = ""
        rec["disc_size_hi"] = 0
        rec["nzones_hi"] = 0
        rec["format_version"] = 0

    # Derived values
    rec["nzones"] = rec["nzones_lo"] | (rec["nzones_hi"] << 8)
    rec["disc_size"] = rec["disc_size_lo"] | (rec["disc_size_hi"] << 32)
    rec["sector_size"] = 1 << rec["log2_sector_size"]
    rec["bpmb"] = 1 << rec["log2_bpmb"]

    return rec


# ---------------------------------------------------------------------------
# Address computation
# ---------------------------------------------------------------------------

def compute_zone_disc_address(rec, zone):
    """Compute the disc byte address of the start of a given zone's data.

    Zone 0 covers (sector_size - 64) * 8 allocation units.
    Every other zone covers (sector_size - 4) * 8 allocation units.
    Each allocation unit is `bpmb` bytes on disc.
    """
    sector_size = rec["sector_size"]
    bpmb = rec["bpmb"]

    zone0_aus = (sector_size - 64) * 8
    zoneN_aus = (sector_size - 4) * 8

    if zone == 0:
        return 0

    return (zone0_aus + (zone - 1) * zoneN_aus) * bpmb


def compute_armlock_addresses(rec):
    """Derive the three key addresses ARMlock uses, plus the signature location."""
    nzones = rec["nzones"]
    sector_size = rec["sector_size"]

    half = nzones // 2
    map_start = compute_zone_disc_address(rec, half)
    stride = nzones * sector_size

    return {
        "map_primary": map_start,
        "map_backup": map_start + stride,
        "root_dir": map_start + 2 * stride,
        "signature": map_start + 2 * stride + 0x400,
        "stride": stride,
    }


# ---------------------------------------------------------------------------
# Directory parsing
# ---------------------------------------------------------------------------

FILETYPES = {
    0xFFF: "Text",    0xFFE: "Command", 0xFFD: "Data",     0xFFC: "Paged",
    0xFFB: "BASIC",   0xFFA: "Module",  0xFF9: "Sprite",   0xFF8: "Absolute",
    0xFF7: "BBC ROM",  0xFF6: "Font",   0xFF5: "PoScript",  0xFF2: "Config",
    0xFEB: "Obey",    0xFEA: "Desktop", 0xFE6: "UNIX Ex",  0xFAF: "HTML",
    0xAFF: "DrawFile", 0xDDC: "Archive",
}


def parse_directory(image, addr):
    """Parse a new-format Filecore directory (0x800 bytes) at the given address.

    Returns (valid, magic, seq, entries) where entries is a list of dicts.
    """
    if addr + 0x800 > len(image):
        return False, None, None, []

    seq = image[addr]
    magic = image[addr + 1:addr + 5]

    if magic not in (b"Hugo", b"Nick"):
        return False, None, None, []

    entries = []
    offset = addr + 5

    while offset + 26 <= addr + 0x800 - 0x29:
        if image[offset] == 0x00:
            break

        name_bytes = image[offset:offset + 10]
        name_end = next((i for i in range(10) if name_bytes[i] in (0x00, 0x0D)), 10)
        name = name_bytes[:name_end].decode("ascii", errors="replace")

        load = struct.unpack_from("<I", image, offset + 0x0A)[0]
        exec_addr = struct.unpack_from("<I", image, offset + 0x0E)[0]
        length = struct.unpack_from("<I", image, offset + 0x12)[0]
        sin = image[offset + 0x16] | (image[offset + 0x17] << 8) | (image[offset + 0x18] << 16)
        attr = image[offset + 0x19]

        is_dir = bool(attr & 0x08)

        filetype = None
        if (load >> 20) == 0xFFF:
            filetype = (load >> 8) & 0xFFF

        entries.append({
            "name": name,
            "load": load,
            "exec": exec_addr,
            "length": length,
            "sin": sin,
            "attr": attr,
            "is_dir": is_dir,
            "filetype": filetype,
        })
        offset += 26

    return True, magic.decode("ascii"), seq, entries


def format_entry(e):
    """Format a directory entry for display."""
    kind = "Dir " if e["is_dir"] else "File"
    ft = ""
    if e["filetype"] is not None:
        ft_name = FILETYPES.get(e["filetype"], "")
        ft = f"  type {e['filetype']:03X}" + (f" ({ft_name})" if ft_name else "")
    return f"  {kind}  {e['name']:<12s} {e['length']:>9d} bytes  SIN 0x{e['sin']:06X}{ft}"


# ---------------------------------------------------------------------------
# Zone map walking (for module extraction)
# ---------------------------------------------------------------------------

def walk_zone_map(image, rec, target_frag_id):
    """Walk the zone map and collect all fragments with the given ID.

    Returns a list of (disc_address, length_in_bytes) tuples, in zone order.
    """
    nzones = rec["nzones"]
    sector_size = rec["sector_size"]
    bpmb = rec["bpmb"]
    idlen = rec["idlen"]
    zone_spare = rec["zone_spare"]

    half = nzones // 2
    fragments = []

    map_start = compute_zone_disc_address(rec, half)

    for z in range(nzones):
        # Map block for zone z is stored at map_start + z * sector_size,
        # but it describes the disc region starting at zone z's disc address.
        zone_disc_addr = compute_zone_disc_address(rec, z)
        map_block_addr = map_start + z * sector_size

        if map_block_addr + sector_size > len(image):
            continue

        zone_data = image[map_block_addr:map_block_addr + sector_size]

        # Header size: 64 bytes for zone 0, 4 bytes for others
        header_bits = (64 if z == 0 else 4) * 8
        zone_end_bits = sector_size * 8
        bit_pos = header_bits
        alloc_unit = 0

        while bit_pos < zone_end_bits:
            # Handle zone_spare continuation bits at start of non-zero zones
            if z > 0 and alloc_unit < zone_spare:
                bit_pos += 1
                alloc_unit += 1
                continue

            if bit_pos + idlen > zone_end_bits:
                break

            # Read fragment ID (idlen bits, LSB first)
            frag_id = 0
            for b in range(idlen):
                byte_idx = bit_pos >> 3
                bit_idx = bit_pos & 7
                if zone_data[byte_idx] & (1 << bit_idx):
                    frag_id |= (1 << b)
                bit_pos += 1

            frag_start_au = alloc_unit

            # Count padding zeros + terminating 1
            while bit_pos < zone_end_bits:
                byte_idx = bit_pos >> 3
                bit_idx = bit_pos & 7
                bit_val = (zone_data[byte_idx] >> bit_idx) & 1
                bit_pos += 1
                alloc_unit += 1
                if bit_val == 1:
                    break

            frag_len_au = alloc_unit - frag_start_au

            if frag_id == target_frag_id:
                # Compute disc address of this fragment
                disc_addr = zone_disc_addr + frag_start_au * bpmb
                frag_bytes = frag_len_au * bpmb
                fragments.append((disc_addr, frag_bytes))

    return fragments


def extract_file_by_sin(image, rec, sin, file_length):
    """Resolve a SIN and extract file data.

    SIN encoding: top bits = fragment ID, low 8 bits = sharing offset.
    Sharing offset 0 means the object has its own fragment.
    Non-zero means byte offset = (sharing_offset - 1) * sharing_unit.
    """
    frag_id = sin >> 8
    sharing_offset = sin & 0xFF

    if frag_id < 2:
        return None

    fragments = walk_zone_map(image, rec, frag_id)
    if not fragments:
        return None

    # Concatenate all fragment data
    obj_data = bytearray()
    for disc_addr, frag_bytes in fragments:
        if disc_addr + frag_bytes > len(image):
            frag_bytes = len(image) - disc_addr
        obj_data.extend(image[disc_addr:disc_addr + frag_bytes])

    # Apply sharing offset
    start = 0
    if sharing_offset > 0:
        share_unit = rec["sector_size"]
        if "share_size" in rec and rec["share_size"] > 0:
            share_unit = rec["sector_size"] << rec["share_size"]
        start = (sharing_offset - 1) * share_unit

    if start + file_length > len(obj_data):
        # Take what we can
        return bytes(obj_data[start:])

    return bytes(obj_data[start:start + file_length])


# ---------------------------------------------------------------------------
# Zone map checksum (§A.1)
# ---------------------------------------------------------------------------

def compute_zone_check(sector_data):
    """Compute the ZoneCheck byte for a zone sector.

    Models the ARM ADCS instruction exactly: a 32-bit accumulator plus
    a separate 1-bit carry flag.  Sum all words end-to-start, subtract
    the existing check byte at offset 0, fold 32->16->8 via XOR.
    """
    assert len(sector_data) % 4 == 0
    acc = 0      # 32-bit accumulator (ARM register)
    carry = 0    # 1-bit carry flag (ARM C flag, cleared by initial ADDS)

    for i in range(len(sector_data) - 4, -1, -4):
        word = struct.unpack_from("<I", sector_data, i)[0]
        total = acc + word + carry          # ADCS: Rd = Rn + Op2 + C
        acc = total & 0xFFFFFFFF            # 32-bit result
        carry = 1 if total > 0xFFFFFFFF else 0  # new carry flag

    s = (acc - sector_data[0]) & 0xFFFFFFFF  # subtract existing check byte
    s = s ^ (s >> 16)
    s = s ^ (s >> 8)
    return s & 0xFF


def decode_armlock_boot_option(stored):
    """Recover the real boot_option from ARMlock's encoded value."""
    return stored >> 2


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Detect and remove ARMlock protection from Filecore disc images."
    )
    parser.add_argument("image", help="Path to disc image file")
    parser.add_argument(
        "--remove", action="store_true",
        help="Remove ARMlock: restore the real root directory to its proper location",
    )
    parser.add_argument(
        "-o", "--output",
        help="Output file for --remove (default: overwrite input)",
    )
    parser.add_argument(
        "--extract-module", metavar="FILE",
        help="Extract the ARMlock module from the stripped root's !Boot entry",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show detailed information",
    )
    args = parser.parse_args()

    with open(args.image, "rb") as f:
        image = bytearray(f.read())

    # ---- Parse disc record ----

    if len(image) < 0xE00:
        print("Error: image too small to contain a boot block.", file=sys.stderr)
        return 1

    rec = read_disc_record(image)

    if rec["idlen"] == 0:
        print("Old-map disc — ARMlock uses new-map discs only.")
        return 1

    print("Disc record:")
    print(f"  Sector size:     {rec['sector_size']} bytes")
    print(f"  Bytes per map bit: {rec['bpmb']}")
    print(f"  Zones:           {rec['nzones']}")
    print(f"  ID length:       {rec['idlen']} bits")
    print(f"  Disc size:       {rec['disc_size']:,} bytes ({rec['disc_size'] / (1024 * 1024):.1f} MB)")
    print(f"  Root dir SIN:    0x{rec['root_dir']:06X}")
    print(f"  Format version:  {rec['format_version']}")
    if rec["disc_name"]:
        print(f"  Disc name:       {rec['disc_name']}")

    # ---- Compute addresses ----

    addrs = compute_armlock_addresses(rec)

    if args.verbose:
        print(f"\nComputed addresses:")
        print(f"  Zone map primary copy:  0x{addrs['map_primary']:08X}")
        print(f"  Zone map backup copy:   0x{addrs['map_backup']:08X}")
        print(f"  Root directory:         0x{addrs['root_dir']:08X}")
        print(f"  Signature location:     0x{addrs['signature']:08X}")
        print(f"  Map stride:             0x{addrs['stride']:X} ({addrs['stride']} bytes)")

    # ---- Check bounds ----

    if addrs["signature"] + 17 > len(image):
        print(f"\nError: image too small for computed addresses.", file=sys.stderr)
        return 1

    # ---- Check signature ----

    sig = image[addrs["signature"]:addrs["signature"] + 17]
    has_armlock = sig == b"ARMlock installed"

    if has_armlock:
        print(f"\n*** ARMlock DETECTED at 0x{addrs['signature']:08X} ***")
    else:
        print(f"\nNo ARMlock signature found.")
        if args.verbose:
            print(f"  Bytes at expected location: {sig[:20].hex(' ')}")
        return 0

    # ---- Show zone map tampering ----

    sector_size = rec["sector_size"]
    for label, map_addr in [("Primary", addrs["map_primary"]),
                            ("Backup", addrs["map_backup"])]:
        stored_boot = image[map_addr + 0x0B]
        real_boot = decode_armlock_boot_option(stored_boot)
        check_byte = image[map_addr]

        # Verify checksum against current (tampered) data
        sector = bytes(image[map_addr:map_addr + sector_size])
        expected_check = compute_zone_check(sector)
        check_ok = (check_byte == expected_check)

        print(f"\n  {label} map (0x{map_addr:08X}):")
        print(f"    ZoneCheck: 0x{check_byte:02X} ({'valid' if check_ok else 'INVALID — expected 0x' + format(expected_check, '02X')})")
        print(f"    boot_option: 0x{stored_boot:02X} (real value: {real_boot})")

    # ---- Show both directories ----

    print(f"\nStripped root directory (at 0x{addrs['root_dir']:08X}):")
    valid_s, magic_s, seq_s, entries_s = parse_directory(image, addrs["root_dir"])
    if valid_s:
        print(f"  Seq=0x{seq_s:02X}  Magic={magic_s}  Entries: {len(entries_s)}")
        for e in entries_s:
            print(format_entry(e))
    else:
        print("  (not a valid directory)")

    print(f"\nReal root directory (stashed at 0x000400):")
    valid_r, magic_r, seq_r, entries_r = parse_directory(image, 0x400)
    if valid_r:
        print(f"  Seq=0x{seq_r:02X}  Magic={magic_r}  Entries: {len(entries_r)}")
        for e in entries_r:
            print(format_entry(e))
    else:
        print("  (not a valid directory at 0x400!)")
        return 1

    # ---- Extract module ----

    if args.extract_module:
        boot_entry = next((e for e in entries_s if e["name"] == "!Boot" and not e["is_dir"]), None)
        if boot_entry is None:
            print("\nNo !Boot file entry in stripped root — cannot extract module.")
        else:
            print(f"\nExtracting ARMlock module (SIN 0x{boot_entry['sin']:06X}, "
                  f"{boot_entry['length']} bytes)...")
            data = extract_file_by_sin(image, rec, boot_entry["sin"], boot_entry["length"])
            if data:
                with open(args.extract_module, "wb") as f:
                    f.write(data)
                print(f"  Written to {args.extract_module}")
                # Quick sanity check — module title
                title_off = struct.unpack_from("<I", data, 0x10)[0] if len(data) > 0x14 else 0
                if 0 < title_off < len(data):
                    end = data.index(0, title_off) if 0 in data[title_off:] else title_off + 32
                    title = data[title_off:end].decode("ascii", errors="replace")
                    print(f"  Module title: {title}")
            else:
                print("  Failed to resolve SIN — zone map walk found no matching fragments.")

    # ---- Remove ----

    if not args.remove:
        print(f"\nRun with --remove to restore the real root directory.")
        return 0

    print(f"\nRemoving ARMlock protection...")

    # 1. Fix zone map: recover boot_option and recompute ZoneCheck
    sector_size = rec["sector_size"]
    for label, map_addr in [("primary", addrs["map_primary"]),
                            ("backup", addrs["map_backup"])]:
        stored_boot = image[map_addr + 0x0B]
        real_boot = decode_armlock_boot_option(stored_boot)
        old_check = image[map_addr]

        print(f"  {label.capitalize()} map at 0x{map_addr:08X}:")
        print(f"    boot_option: 0x{stored_boot:02X} -> 0x{real_boot:02X} (decoded)")
        print(f"    ZoneCheck before: 0x{old_check:02X}")

        # Fix boot_option
        image[map_addr + 0x0B] = real_boot

        # Recompute ZoneCheck
        sector = bytearray(image[map_addr:map_addr + sector_size])
        sector[0x0B] = real_boot
        new_check = compute_zone_check(sector)
        image[map_addr] = new_check
        print(f"    ZoneCheck after:  0x{new_check:02X}")

    # 2. Do NOT modify the boot block disc record — it has its own checksum
    #    at +0x1FE that we'd also need to recompute, and ARMlock deliberately
    #    set boot_option=0 there during installation.  The zone map copies
    #    (which FileCore uses at runtime) now have the correct decoded value.
    bb_boot = image[0xDC0 + 0x07]
    map_boot = image[addrs["map_primary"] + 0x0B]
    if bb_boot != map_boot:
        print(f"  Note: boot block boot_option=0x{bb_boot:02X} differs from "
              f"zone map=0x{map_boot:02X} — leaving boot block untouched")

    # 3. Copy the real root directory over the stripped one
    real_root = bytes(image[0x400:0x400 + 0x800])
    image[addrs["root_dir"]:addrs["root_dir"] + 0x800] = real_root
    print(f"  Copied real root: 0x000400 -> 0x{addrs['root_dir']:08X}")

    # 4. Zero out the stashed copy and signature
    image[0x400:0x400 + 0x800] = b"\x00" * 0x800
    print(f"  Zeroed stashed copy at 0x000400")

    # Verify the result
    valid_v, magic_v, seq_v, entries_v = parse_directory(image, addrs["root_dir"])
    if valid_v:
        print(f"  Verified: restored root has {len(entries_v)} entries")
    else:
        print("  WARNING: restored root directory fails validation!", file=sys.stderr)

    out_path = args.output or args.image
    with open(out_path, "wb") as f:
        f.write(image)
    print(f"  Written to {out_path}")
    print(f"\nDone. Disc should now show full root directory without ARMlock.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
