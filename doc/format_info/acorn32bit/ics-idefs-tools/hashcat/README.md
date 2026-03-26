# IDEFS Hashcat Plugin (Hash Mode 99100)

GPU-accelerated password recovery for ICS/Baildon IDEFS disc protection.

## Hash Format

```
<lo_hex>*<hi_hex>
```

Example: `7ad2418e*0bf9e580`

The `*` separator avoids collision with hashcat's `:` potfile delimiter.

## Test Hashes

| Hash | Password | Length |
|------|----------|--------|
| `37262280*3b99a325` | dragon | 6 |
| `78d14d0c*07f3f003` | computer | 8 |
| `f3e9733e*66c76bf4` | helloworld | 10 |

## Installation (hashcat 7.x)

```bash
cd /path/to/hashcat-7.x.x

# Install plugin files
cp module_99100.c     src/modules/
cp m99100_a0-pure.cl  OpenCL/
cp m99100_a3-pure.cl  OpenCL/

# Clean any cached kernels from previous attempts
rm -rf kernels/
rm -f hashcat.potfile

# Build
make clean && make

# Verify
./hashcat -m 99100 --hash-info
```

You should see:

```
Hash.Name........: IDEFS (ICS/Baildon) Disc Protection
Password.Min.Len.: 1
Password.Max.Len.: 10
```

### Hashcat 7.1.2 Note

If you get `module_bridge_name` errors, add these two lines to the
`module_init` function in `module_99100.c`:

```c
module_ctx->module_bridge_name = MODULE_DEFAULT;
module_ctx->module_bridge_type = MODULE_DEFAULT;
```

## Usage

```bash
# Dictionary attack (recommended first step)
./hashcat -m 99100 hashes.txt rockyou.txt

# Dictionary with rules (covers mutations like capitalisation, suffixes)
./hashcat -m 99100 hashes.txt rockyou.txt -r rules/best64.rule

# Brute-force: all printable ASCII up to 6 characters
./hashcat -m 99100 hashes.txt -a 3 '?a?a?a?a?a?a' --increment

# Brute-force: all 10-char lowercase
./hashcat -m 99100 hashes.txt -a 3 '?l?l?l?l?l?l?l?l?l?l' --increment

# Hybrid: dictionary + 2-digit suffix
./hashcat -m 99100 hashes.txt -a 6 rockyou.txt '?d?d'
```

## Performance

On an NVIDIA RTX 4060: ~25 GH/s (mask attack, single hash).

At that speed:

| Attack | Keyspace | Time |
|--------|----------|------|
| rockyou.txt (14M words) | 14M | instant |
| rockyou + best64 rules | ~1B | instant |
| All 6-char printable ASCII | 690G | ~27 sec |
| All 7-char printable ASCII | 64.8T | ~42 min |

## Python Bridge (Alternative)

If you don't want to compile a native plugin, the Python bridge works
with hashcat 7.x out of the box. It's CPU-only (~1-10 MH/s) but
gives you all of hashcat's attack modes for free.

1. Copy `generic_hash_sp.py` to your hashcat directory
2. Format hashes as `<lo_hex>:<hi_hex>` (colon separator for the bridge)
3. Run: `hashcat -m 72000 hashes.txt wordlist.txt`

## Architecture

The plugin uses `ATTACK_EXEC_INSIDE_KERNEL` (fast mode) with separate
kernel files per attack mode:

- `m99100_a0-pure.cl` — wordlist/rules (`-a 0`), uses `KERN_ATTR_RULES`
- `m99100_a3-pure.cl` — mask/brute-force (`-a 3`), uses `KERN_ATTR_VECTOR`

Both kernels use inline digest comparison (direct register compare for
single-hash, loop over `digests_buf` for multi-hash) rather than
hashcat's `COMPARE_S`/`COMPARE_M` macros, for cross-version compatibility.
