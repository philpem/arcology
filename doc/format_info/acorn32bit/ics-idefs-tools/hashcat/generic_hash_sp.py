# generic_hash_sp.py — IDEFS password hash for Hashcat Python Bridge
# ====================================================================
# This implements the ICS/Baildon Electronics IDEFS v3.15 password hash
# for use with Hashcat v7+ Python bridge (mode -m 72000 or -m 73000).
#
# INSTALLATION:
#   1. Copy this file to your hashcat directory, replacing or renaming
#      the existing generic_hash_sp.py
#   2. Prepare a hash file in the format:   hash_lo:hash_hi
#      e.g.:  7ad2418e:0bf9e580
#   3. Run:  hashcat -m 72000 hashes.txt wordlist.txt
#      Or:   hashcat -m 72000 hashes.txt -a 3 '?a?a?a?a?a?a' --increment
#
# HASH FILE FORMAT:
#   Each line is:  <lo_hex_lowercase>:<hi_hex_lowercase>
#   Example:       7ad2418e:0bf9e580
#
# The self-test hash/password pair below uses "test" → 7ad2418e:0bf9e580
#
# NOTE: Hashcat's Python bridge runs on CPU only (no GPU acceleration).
#       For a niche hash like this, CPU speed is typically sufficient.
#       The bridge gives you all of Hashcat's attack modes (wordlists,
#       rules, masks, combinators, etc.) for free.

# ── Hashcat Python Bridge interface ───────────────────────────────

ST_HASH = "7ad2418e:0bf9e580"
ST_PASS = "test"

def calc_hash(password: bytes, _salt_buf: bytes) -> str:
    """
    Compute the IDEFS password hash.

    Called by Hashcat for each password candidate.
    Returns the hash as a string matching the format of ST_HASH.
    """
    KEY = 0x01810284
    MASK32 = 0xFFFFFFFF

    lo = 0
    hi = 0

    # Skip leading spaces
    idx = 0
    while idx < len(password) and password[idx] == 0x20:
        idx += 1

    # Process up to 10 characters, stop at space or control char
    count = 0
    while count < 10 and idx < len(password):
        c = password[idx]
        if c <= 0x20:
            break

        ch = (c - 0x2A) & 0xFF

        top6 = lo & 0xFC000000
        hi = (top6 ^ _ror32(hi, 26) ^ KEY) & MASK32
        lo = (ch ^ ((lo << 6) & MASK32) ^ KEY) & MASK32

        idx += 1
        count += 1

    return f"{lo:08x}:{hi:08x}"


def _ror32(val: int, n: int) -> int:
    """32-bit rotate right."""
    val &= 0xFFFFFFFF
    n &= 31
    return ((val >> n) | (val << (32 - n))) & 0xFFFFFFFF
