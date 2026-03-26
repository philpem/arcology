# Hashcat module for ARMlock Password Hashes (mode 99999)

## Hash algorithm

ARMlock (1994, Acorn RISC OS) uses a custom 64-bit hash with two 32-bit
accumulators and fixed seeds:

```
v3 = 0x89ABCDEF
v4 = 0x01234567

for each character c (converted to uppercase):
    rotated = (v3 ASR 13) | (v3 << 19)   // arithmetic shift right
    v3 = rotated + toupper(c)
    v4 ^= v3

output = v3 || v4
```

Properties: case-insensitive, no salt, 64-bit output, max 11 characters.
v4 never feeds back into v3, so the effective security is ~32 bits.

## Hash format

```
$armlock$<v3_hex>$<v4_hex>
```

Example (password "COFFEE"):
```
$armlock$8e2b21c5$177c3437
```

## Installation

Copy files into your hashcat source tree:

```bash
cp module_99999.c     <hashcat>/src/modules/
cp m99999_a0-pure.cl  <hashcat>/OpenCL/
cp m99999_a1-pure.cl  <hashcat>/OpenCL/
cp m99999_a3-pure.cl  <hashcat>/OpenCL/
cd <hashcat> && make
```

## Converting from ARMlock Options file

The password hash is the first 8 bytes of the `<ARMlock$Dir>.Options` file,
stored as two little-endian 32-bit words:

```bash
python3 armlock2hashcat.py Options
# Output: $armlock$8e2b21c5$177c3437
```

Or from known hex values:

```bash
python3 armlock2hashcat.py --words 8E2B21C5 177C3437
```

## Usage examples

Dictionary attack:
```bash
echo '$armlock$8e2b21c5$177c3437' > hash.txt
hashcat -m 99999 hash.txt rockyou.txt
```

Dictionary + rules:
```bash
hashcat -m 99999 hash.txt rockyou.txt -r rules/best64.rule
```

Mask attack (6-char uppercase):
```bash
hashcat -m 99999 hash.txt -a 3 '?u?u?u?u?u?u'
```

Mask with increment (1-8 chars, printable ASCII):
```bash
hashcat -m 99999 hash.txt -a 3 --increment --increment-min 1 --increment-max 8 '?a?a?a?a?a?a?a?a'
```

Combination attack (two wordlists):
```bash
hashcat -m 99999 hash.txt -a 1 words1.txt words2.txt
```

## Kernel notes

- **a0** (straight): scalar kernel, supports all hashcat rules
- **a1** (combination): scalar kernel, concatenates two wordlist entries
- **a3** (mask): SIMD-vectorised, precomputes base password v3 state
  and extends only the mask suffix — the a3 kernel also exploits the
  v4 independence by computing v4 separately from v3 in the hot path

## Files

| File | Destination |
|------|-------------|
| `module_99999.c` | `src/modules/` |
| `m99999_a0-pure.cl` | `OpenCL/` |
| `m99999_a1-pure.cl` | `OpenCL/` |
| `m99999_a3-pure.cl` | `OpenCL/` |
| `armlock2hashcat.py` | anywhere (utility) |
