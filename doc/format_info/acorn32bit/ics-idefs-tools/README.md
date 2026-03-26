# IDEFS Password Recovery Tools

Tools for recovering passwords from ICS/Baildon Electronics IDEFS v3.15
("Wizzo") disc images, as used on Acorn A4 and A5000 computers running
RISC OS.

These tools are for **retrocomputing preservation** — recovering access to
old disc images whose owners have forgotten their passwords. The IDEFS hash
is a trivial 64-bit XOR/shift construction with no salt, designed for
casual protection on 1990s hobbyist hardware.

## Quick Start

**Most likely to work (dictionary attack with Hashcat):**

```bash
# Build the hashcat plugin (see hashcat/README.md)
# Then:
hashcat -m 99100 your_hashes.txt rockyou.txt -r rules/best64.rule
```

**Standalone (no GPU needed):**

```bash
make
./idefs_crack_parallel 0xAABBCCDD 0x11223344 7 8
```

## Extracting the Hash

The password hash is stored in the boot block of each partition, at byte
offset 0xC00 from the partition start (sector 6).

1. Read the boot block sector (512 bytes at partition_start + 0xC00)
2. Check offset 0x1A7 bits 1:0 — the protection level:
   - 0 = no protection (no password needed)
   - 1 = read/write access requires password
   - 2 = read-only access requires password
   - 3 = no access without password
3. Read the hash words (little-endian uint32):
   - **hash_lo**: 4 bytes at boot sector offset 0x1A8
   - **hash_hi**: 4 bytes at boot sector offset 0x1AC
4. If both words are zero, no password is set.

For the Hashcat plugin, format the hash as: `<lo_hex>*<hi_hex>`
For the standalone tools, pass them as: `0x<lo_hex> 0x<hi_hex>`

Example: if the boot block contains `8E 41 D2 7A` at offset 0x1A8 and
`80 E5 F9 0B` at offset 0x1AC, the hash is:

- Hashcat format: `7ad2418e*0bf9e580`
- Standalone: `0x7AD2418E 0x0BF9E580`

## Tools

### 1. Hashcat Plugin (Recommended for Dictionary Attacks)

**Best for:** Likely passwords — dictionary words, names, common patterns.
At 25 GH/s on an RTX 4060, rockyou.txt (14M entries) is tested in under
a millisecond. With rules, you cover billions of realistic mutations in
seconds.

See `hashcat/README.md` for installation.

```bash
# Dictionary
hashcat -m 99100 hashes.txt rockyou.txt

# Dictionary + rules
hashcat -m 99100 hashes.txt rockyou.txt -r rules/best64.rule

# Brute-force mask (all 6-char printable ASCII)
hashcat -m 99100 hashes.txt -a 3 '?a?a?a?a?a?a' --increment
```

### 2. Parallel Brute Force (`idefs_crack_parallel`)

**Best for:** Exhaustive search up to 7 characters, no GPU required.

```bash
make idefs_crack_parallel
./idefs_crack_parallel <hash_lo> <hash_hi> [max_length] [num_threads]
./idefs_crack_parallel 0x7AD2418E 0x0BF9E580 7 8
```

Performance: ~330 MH/s per core. Exhaustive 6-char: ~15 minutes with
8 threads. Exhaustive 7-char: ~21 hours.

### 3. Meet-in-the-Middle (`idefs_crack_mitm`)

**Best for:** Guaranteed recovery of *some* valid password (possibly a
collision) for any hash, up to 10 characters. The recovered password
may not be the original, but it will produce the same hash and unlock
the disc.

```bash
make idefs_crack_mitm
./idefs_crack_mitm <hash_lo> <hash_hi> [max_length] [num_threads]
./idefs_crack_mitm 0x7AD2418E 0x0BF9E580 10 12
```

Searches alphanumeric passwords (a-z A-Z 0-9) first, then falls back
to full printable ASCII. Auto-selects FWD_LEN=5 (~12 GB RAM) or
FWD_LEN=4 (~200 MB) based on available memory.

Performance with FWD=5, 12 threads: length 8 in seconds, length 9 in
~10 seconds, length 10 in ~5-7 minutes.

### 4. Simple Brute Force (`idefs_crack`)

**Best for:** Quick single-threaded recovery of short passwords.

```bash
make idefs_crack
./idefs_crack <hash_lo> <hash_hi> [max_length]
./idefs_crack 0x7AD2418E 0x0BF9E580 6
```

## Practical Advice

For a real IDEFS disc image from the 1990s:

1. **Try Hashcat first** with rockyou.txt + rules. Passwords like
   "bagpipes", "dragon", "Acorn", "a5000" will be found instantly.

2. **Try the parallel brute force** up to 7 characters if the dictionary
   misses. Covers the entire printable ASCII space for realistic lengths.

3. **Use the MITM cracker** as a last resort for guaranteed recovery.
   It will always find a valid password, though it may be a collision
   rather than the original.

4. **Or just zero the hash.** If you don't care about recovering the
   original password, set bytes at boot block offsets 0x1A7-0x1AF to
   zero. This disables protection entirely.

## Hash Properties

- **Algorithm:** Shift-XOR with constant key 0x01810284
- **Output:** 64 bits (two 32-bit words)
- **Max password:** 10 printable ASCII characters (0x21-0x7E)
- **Case:** Fully case-sensitive
- **Salt:** None — identical passwords always produce identical hashes
- **Collisions:** Abundant — ~2^(n-1) collisions per n-character password
  within the same length class. Any collision unlocks the disc.

## File Listing

```
idefs-tools/
├── Makefile                        Build standalone tools
├── README.md                       This file
├── doc/
│   └── partition_ics_idefs.md      IDEFS partition/boot block format spec
├── hashcat/
│   ├── README.md                   Hashcat plugin installation guide
│   ├── module_99100.c              Hashcat C module (hash mode 99100)
│   ├── m99100_a0-pure.cl           GPU kernel: wordlist attack (-a 0)
│   ├── m99100_a3-pure.cl           GPU kernel: mask attack (-a 3)
│   ├── generic_hash_sp.py          Python bridge plugin (alternative)
│   ├── hashes.txt                  Test hashes (dragon/computer/helloworld)
│   └── wordlist.txt                Test dictionary
└── src/
    ├── idefs_crack.c               Simple single-threaded brute force
    ├── idefs_crack_parallel.c      Multi-threaded brute force
    └── idefs_crack_mitm.c          Meet-in-the-middle (guaranteed recovery)
```

## Building

Requires GCC with pthreads support (any modern Linux).

```bash
make          # builds all three standalone tools
make test     # runs self-tests
```

The Hashcat plugin requires hashcat 7.x source — see `hashcat/README.md`.

## Licence

These tools are released for retrocomputing preservation purposes.
The IDEFS filing system is a product of Baildon Electronics / Ian
Copestake Software, circa 1996.
