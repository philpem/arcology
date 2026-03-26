#!/usr/bin/env python3
"""
idefs_extract_hash.py — Extract password hashes from IDEFS disc images.

Reads the partition table from sector 0, then reads the boot block for
each partition and displays the protection level and password hash.

Usage:
    python3 idefs_extract_hash.py <disc_image_file>

The hash can be fed directly to the cracking tools:
    - Hashcat:    hashcat -m 99100 '<lo>*<hi>' wordlist.txt
    - Standalone: ./idefs_crack_mitm 0x<lo> 0x<hi>
"""

import sys
import struct

SECTOR_SIZE = 512

def read_u32(data, offset):
    return struct.unpack_from('<I', data, offset)[0]

def validate_partition_table(sector):
    checksum = 0x50617274  # "Part"
    for i in range(508):
        checksum = (checksum + sector[i]) & 0xFFFFFFFF
    stored = read_u32(sector, 508)
    return checksum == stored

def extract_hashes(filename):
    with open(filename, 'rb') as f:
        data = f.read()

    print(f"File: {filename} ({len(data)} bytes, {len(data) // SECTOR_SIZE} sectors)\n")

    # Read sector 0 — partition table
    if len(data) < SECTOR_SIZE:
        print("ERROR: File too small for partition table")
        return

    sector0 = data[0:SECTOR_SIZE]

    if not validate_partition_table(sector0):
        print("No valid partition table (checksum mismatch).")
        print("Treating entire disc as single partition.\n")
        partitions = [(0, len(data) // SECTOR_SIZE)]
    else:
        total_sectors = read_u32(sector0, 0x1F8)
        print(f"Partition table valid. Total capacity: {total_sectors} sectors "
              f"({total_sectors * SECTOR_SIZE / (1024*1024):.0f} MB)\n")

        partitions = []
        for i in range(0, 504, 8):
            start = read_u32(sector0, i)
            size = read_u32(sector0, i + 4)
            if size == 0:
                break  # end marker
            if size & 0x80000000:
                continue  # deleted/unused slot
            partitions.append((start, size))
            if len(partitions) >= 4:
                break

    protection_names = ["None", "Read/Write (password required)",
                       "Read-Only (password required)", "No access"]

    any_protected = False

    for idx, (start, size) in enumerate(partitions):
        boot_offset = (start + 6) * SECTOR_SIZE  # boot block at partition_start + 6 sectors

        print(f"Partition {idx}: start=sector {start} (0x{start:X}), "
              f"size={size} sectors ({size * SECTOR_SIZE / (1024*1024):.0f} MB)")

        if boot_offset + SECTOR_SIZE > len(data):
            print(f"  WARNING: Boot block at offset 0x{boot_offset:X} is beyond end of image\n")
            continue

        boot = data[boot_offset:boot_offset + SECTOR_SIZE]

        prot_flags = boot[0x1A7]
        prot_level = prot_flags & 0x03
        hash_lo = read_u32(boot, 0x1A8)
        hash_hi = read_u32(boot, 0x1AC)

        print(f"  Boot block at file offset: 0x{boot_offset:X}")
        print(f"  Protection: {prot_level} — {protection_names[prot_level]}")
        print(f"  Hash lo: 0x{hash_lo:08X}  hi: 0x{hash_hi:08X}")

        if prot_level == 0:
            print(f"  → No password set")
        elif hash_lo == 0 and hash_hi == 0:
            print(f"  → Hash is zero (password cleared or never set)")
        else:
            any_protected = True
            print(f"  → Hashcat format:    {hash_lo:08x}*{hash_hi:08x}")
            print(f"  → Standalone format: 0x{hash_lo:08X} 0x{hash_hi:08X}")
        print()

    if any_protected:
        print("To crack with Hashcat:")
        print("  hashcat -m 99100 hashes.txt rockyou.txt -r rules/best64.rule")
        print()
        print("To crack with standalone MITM tool:")
        print("  ./idefs_crack_mitm 0x<hash_lo> 0x<hash_hi> 10 12")

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <disc_image_file>")
        sys.exit(1)
    extract_hashes(sys.argv[1])
