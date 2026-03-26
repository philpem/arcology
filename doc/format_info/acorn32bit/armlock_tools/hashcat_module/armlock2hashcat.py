#!/usr/bin/env python3
"""
armlock2hashcat.py — Convert ARMlock Options file or raw hash bytes
to hashcat -m 99999 format.

Usage:
  python3 armlock2hashcat.py <Options_file>
  python3 armlock2hashcat.py --hex C5212B8E37347C17
  python3 armlock2hashcat.py --words 8E2B21C5 177C3437
"""

import sys
import struct
import argparse


def options_to_hashcat(data):
    """Read first 8 bytes of an ARMlock Options file as v3, v4 (little-endian)."""
    if len(data) < 8:
        raise ValueError(f"Options file too short ({len(data)} bytes, need 8)")
    v3 = struct.unpack_from("<I", data, 0)[0]
    v4 = struct.unpack_from("<I", data, 4)[0]
    return v3, v4


def main():
    parser = argparse.ArgumentParser(
        description="Convert ARMlock hash to hashcat $armlock$ format"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("file", nargs="?", help="ARMlock Options file")
    group.add_argument("--hex", help="Raw 16 hex chars (8 bytes LE): v3v4")
    group.add_argument("--words", nargs=2, metavar=("V3", "V4"),
                       help="Two 32-bit hex words")
    args = parser.parse_args()

    if args.words:
        v3 = int(args.words[0], 16)
        v4 = int(args.words[1], 16)
    elif args.hex:
        raw = bytes.fromhex(args.hex)
        v3, v4 = options_to_hashcat(raw)
    else:
        with open(args.file, "rb") as f:
            data = f.read()
        v3, v4 = options_to_hashcat(data)
        print(f"Options file: {args.file}", file=sys.stderr)
        print(f"  v3 = 0x{v3:08X}", file=sys.stderr)
        print(f"  v4 = 0x{v4:08X}", file=sys.stderr)

    hashcat_line = f"$armlock${v3:08x}${v4:08x}"
    print(hashcat_line)


if __name__ == "__main__":
    main()
