/*
 * idefs_crack_mitm.c — Optimised Meet-in-the-Middle IDEFS Password Recovery
 * ===========================================================================
 * Build:   gcc -O3 -pthread -o idefs_crack_mitm idefs_crack_mitm.c -lm
 * Usage:   ./idefs_crack_mitm <hash_lo> <hash_hi> [max_length] [num_threads]
 *          ./idefs_crack_mitm --self-test
 *
 * Optimisations:
 *   1. Bucket-indexed forward table (no sorting, O(1) bucket access)
 *   2. Bloom filter on lo values (rejects ~99% of candidates cheaply)
 *   3. Restructured backward inversion: only 4 char checks instead of
 *      256 — the character depends only on 2 bits, not 8.  Inner loop
 *      of 64 iterations is completely branchless.  ~10x faster.
 *   4. Inlined leaf-level Bloom+lookup: skips hi computation for the
 *      ~99% of candidates that fail Bloom.
 *   5. Auto-selects FWD_LEN=5 (~12 GB) or FWD_LEN=4 (~200 MB) based
 *      on available RAM.
 *
 * IDEFS passwords are CASE-SENSITIVE, max 10 chars.
 */

#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <ctype.h>
#include <time.h>
#include <math.h>
#include <pthread.h>
#include <stdatomic.h>

/* ── Constants ────────────────────────────────────────────────────── */

#define KEY         0x01810284u
#define MIN_CHAR    0x21
#define MAX_CHAR    0x7E
#define NUM_CHARS   94
#define MAX_PW_LEN  10
#define NUM_ALNUM   62

#define BUCKET_BITS  20
#define NUM_BUCKETS  (1u << BUCKET_BITS)
#define BUCKET_SHIFT (32 - BUCKET_BITS)

static inline uint32_t ror32(uint32_t v, unsigned n) { return (v >> n) | (v << (32-n)); }

/* ── Charsets and lookup tables ────────────────────────────────────── */

static char    g_alnum[62];
static char    g_full[94];
static uint8_t g_valid_char[256];   /* 1 if printable (0x21-0x7E) */
static uint8_t g_alnum_char[256];   /* 1 if alphanumeric */

static void init_charsets(void) {
    int ai = 0, fi = 0;
    for (int c = MIN_CHAR; c <= MAX_CHAR; c++) {
        g_full[fi++] = (char)c;
        g_valid_char[c] = 1;
        if (isalnum(c)) { g_alnum[ai++] = (char)c; g_alnum_char[c] = 1; }
    }
}

/* ── Hash primitives ──────────────────────────────────────────────── */

static void idefs_hash(const char *pw, uint32_t *out_lo, uint32_t *out_hi) {
    uint32_t lo = 0, hi = 0;
    while (*pw == ' ') pw++;
    for (int i = 0; i < 10 && (uint8_t)*pw > 0x20; i++) {
        uint32_t ch = ((uint8_t)*pw++ - 0x2A) & 0xFF;
        uint32_t top6 = lo & 0xFC000000u;
        hi = top6 ^ ror32(hi, 26) ^ KEY;
        lo = ch ^ (lo << 6) ^ KEY;
    }
    *out_lo = lo; *out_hi = hi;
}

static inline void idefs_step(uint32_t *lo, uint32_t *hi, int c) {
    uint32_t ch = ((uint32_t)c - 0x2A) & 0xFF;
    uint32_t top6 = *lo & 0xFC000000u;
    *hi = top6 ^ ror32(*hi, 26) ^ KEY;
    *lo = ch ^ (*lo << 6) ^ KEY;
}

/* ── Bloom filter ─────────────────────────────────────────────────── */

static uint8_t *g_bloom;
static uint32_t g_bloom_mask;  /* byte-mask: (bloom_bytes - 1) */

static inline void bloom_set(uint32_t lo) {
    uint32_t h1 = lo, h2 = lo * 2654435761u, h3 = lo * 2246822519u;
    g_bloom[(h1 >> 3) & g_bloom_mask] |= (1 << (h1 & 7));
    g_bloom[(h2 >> 3) & g_bloom_mask] |= (1 << (h2 & 7));
    g_bloom[(h3 >> 3) & g_bloom_mask] |= (1 << (h3 & 7));
}

static inline int bloom_test(uint32_t lo) {
    uint32_t h1 = lo, h2 = lo * 2654435761u, h3 = lo * 2246822519u;
    if (!(g_bloom[(h1 >> 3) & g_bloom_mask] & (1 << (h1 & 7)))) return 0;
    if (!(g_bloom[(h2 >> 3) & g_bloom_mask] & (1 << (h2 & 7)))) return 0;
    if (!(g_bloom[(h3 >> 3) & g_bloom_mask] & (1 << (h3 & 7)))) return 0;
    return 1;
}

/* ── Bucket-indexed forward table ─────────────────────────────────── */

#pragma pack(push, 1)
typedef struct { uint32_t lo, hi, prefix_idx; } fwd_entry_t;
#pragma pack(pop)

static fwd_entry_t *g_fwd_arr;
static uint32_t     g_fwd_count;
static uint32_t    *g_bucket_start;

static uint32_t fwd_lookup(uint32_t lo, uint32_t hi) {
    uint32_t bkt = lo >> BUCKET_SHIFT;
    uint32_t start = g_bucket_start[bkt];
    uint32_t end   = g_bucket_start[bkt + 1];
    for (uint32_t i = start; i < end; i++) {
        if (g_fwd_arr[i].lo == lo && g_fwd_arr[i].hi == hi)
            return g_fwd_arr[i].prefix_idx;
    }
    return (uint32_t)-1;
}

/* ── Forward table build ──────────────────────────────────────────── */

static int         g_fwd_len;
static const char *g_fwd_charset;
static int         g_fwd_charset_len;

static void decode_prefix(uint32_t idx, char *out) {
    for (int i = g_fwd_len - 1; i >= 0; i--) {
        out[i] = g_fwd_charset[idx % g_fwd_charset_len];
        idx /= g_fwd_charset_len;
    }
}

/* Two-pass builder: count buckets, then place directly */
static uint32_t *g_bcounts;   /* temporary, freed after build */
static uint32_t  g_fwd_idx;
static int       g_build_pass;

static void build_recurse(int depth, uint32_t lo, uint32_t hi) {
    if (depth == g_fwd_len) {
        uint32_t bkt = lo >> BUCKET_SHIFT;
        if (g_build_pass == 1) {
            g_bcounts[bkt]++;
        } else {
            g_fwd_arr[g_bcounts[bkt]++] = (fwd_entry_t){ lo, hi, g_fwd_idx };
            bloom_set(lo);
        }
        g_fwd_idx++;
        return;
    }
    for (int i = 0; i < g_fwd_charset_len; i++) {
        uint32_t tlo = lo, thi = hi;
        idefs_step(&tlo, &thi, g_fwd_charset[i]);
        build_recurse(depth + 1, tlo, thi);
    }
}

static int build_forward_table(const char *charset, int charset_len,
                               int fwd_len, const char *label)
{
    g_fwd_charset = charset;
    g_fwd_charset_len = charset_len;
    g_fwd_len = fwd_len;
    g_fwd_count = (uint32_t)pow(charset_len, fwd_len);

    double ram_arr = (double)g_fwd_count * sizeof(fwd_entry_t);
    uint64_t bloom_bytes = 1;
    while (bloom_bytes * 8 < (uint64_t)g_fwd_count * 12) bloom_bytes <<= 1;
    printf("Building %s forward table (FWD=%d, %u entries, %.1f GB)...\n",
           label, fwd_len, g_fwd_count, (ram_arr + bloom_bytes) / 1e9);
    fflush(stdout);

    g_bloom_mask = (uint32_t)(bloom_bytes - 1);
    g_bloom = calloc(bloom_bytes, 1);
    g_bucket_start = calloc(NUM_BUCKETS + 1, sizeof(uint32_t));
    g_bcounts = calloc(NUM_BUCKETS, sizeof(uint32_t));
    if (!g_bloom || !g_bucket_start || !g_bcounts) {
        printf("  Allocation failed\n");
        free(g_bloom); free(g_bucket_start); free(g_bcounts);
        g_bloom = NULL; g_bucket_start = NULL; g_bcounts = NULL;
        return -1;
    }

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    /* Pass 1: count */
    printf("  Pass 1 (counting)..."); fflush(stdout);
    g_fwd_idx = 0; g_build_pass = 1;
    build_recurse(0, 0, 0);

    /* Prefix sums */
    g_bucket_start[0] = 0;
    for (uint32_t b = 0; b < NUM_BUCKETS; b++)
        g_bucket_start[b + 1] = g_bucket_start[b] + g_bcounts[b];

    struct timespec t1a;
    clock_gettime(CLOCK_MONOTONIC, &t1a);
    printf(" %.1fs\n", (t1a.tv_sec-t0.tv_sec) + (t1a.tv_nsec-t0.tv_nsec)/1e9);

    /* Allocate array */
    g_fwd_arr = malloc((size_t)g_fwd_count * sizeof(fwd_entry_t));
    if (!g_fwd_arr) {
        printf("  Array allocation failed (%.1f GB)\n", ram_arr / 1e9);
        free(g_bloom); free(g_bucket_start); free(g_bcounts);
        g_bloom = NULL; g_bucket_start = NULL; g_bcounts = NULL;
        return -1;
    }

    /* Pass 2: place */
    printf("  Pass 2 (placing)..."); fflush(stdout);
    memcpy(g_bcounts, g_bucket_start, NUM_BUCKETS * sizeof(uint32_t));
    memset(g_bloom, 0, bloom_bytes);
    g_fwd_idx = 0; g_build_pass = 2;
    build_recurse(0, 0, 0);

    clock_gettime(CLOCK_MONOTONIC, &t1);
    printf(" %.1fs total\n", (t1.tv_sec-t0.tv_sec) + (t1.tv_nsec-t0.tv_nsec)/1e9);

    free(g_bcounts); g_bcounts = NULL;
    return 0;
}

static void free_tables(void) {
    free(g_fwd_arr);      g_fwd_arr = NULL;
    free(g_bloom);        g_bloom = NULL;
    free(g_bucket_start); g_bucket_start = NULL;
}

/* ── Backward inversion (10x optimised) ───────────────────────────── */

typedef struct { uint32_t lo, hi; uint8_t c; } prev_t;

/*
 * ch depends ONLY on b01 (2 bits), not b26 (6 bits).
 * 4 char checks, then branchless 64-iteration inner loops.
 */
static int invert_step(uint32_t lo_t, uint32_t hi_t, prev_t *out, int alnum_only) {
    uint32_t X = lo_t ^ KEY;
    uint32_t lp_mid = (X >> 6) & 0x03FFFFFC;
    int count = 0;
    const uint8_t *filter = alnum_only ? g_alnum_char : g_valid_char;

    for (int b01 = 0; b01 < 4; b01++) {
        uint32_t cv = (((X ^ ((uint32_t)b01 << 6)) & 0xFF) + 0x2A) & 0xFF;
        if (!filter[cv]) continue;

        uint32_t lp_base = (b01 & 3) | lp_mid;
        for (int b26 = 0; b26 < 64; b26++) {
            uint32_t lp  = lp_base | ((uint32_t)b26 << 26);
            uint32_t rot = hi_t ^ ((uint32_t)b26 << 26) ^ KEY;
            out[count++] = (prev_t){ lp, (rot << 26) | (rot >> 6), (uint8_t)cv };
        }
    }
    return count;
}

/* ── Parallel backward search ─────────────────────────────────────── */

static uint32_t   g_target_lo, g_target_hi;
static atomic_int g_found;
static char       g_result[MAX_PW_LEN + 1];

typedef struct {
    prev_t  *first_cands;
    int      cand_start, cand_count;
    int      remaining_steps;
    int      alnum_only;
    uint64_t tested;
    char     result[MAX_PW_LEN + 1];
} worker_t;

/*
 * Inlined leaf level: Bloom-test lo inside the 64-iteration inner loop.
 * Only computes hi for the ~1% that pass Bloom.
 * suffix[] is in backward order: suffix[0]=outermost inverted char (=last pw char).
 */
static int leaf_probe(worker_t *w, uint32_t lo_t, uint32_t hi_t,
                      const char *suffix, int suffix_len)
{
    uint32_t X = lo_t ^ KEY;
    uint32_t lp_mid = (X >> 6) & 0x03FFFFFC;
    const uint8_t *filter = w->alnum_only ? g_alnum_char : g_valid_char;

    for (int b01 = 0; b01 < 4; b01++) {
        uint32_t cv = (((X ^ ((uint32_t)b01 << 6)) & 0xFF) + 0x2A) & 0xFF;
        if (!filter[cv]) continue;

        uint32_t lp_base = (b01 & 3) | lp_mid;
        for (int b26 = 0; b26 < 64; b26++) {
            uint32_t lp = lp_base | ((uint32_t)b26 << 26);
            if (!bloom_test(lp)) continue;

            /* Bloom hit — compute hi and lookup */
            uint32_t rot = hi_t ^ ((uint32_t)b26 << 26) ^ KEY;
            uint32_t hp  = (rot << 26) | (rot >> 6);

            w->tested++;
            uint32_t pidx = fwd_lookup(lp, hp);
            if (pidx != (uint32_t)-1) {
                char prefix[MAX_PW_LEN];
                decode_prefix(pidx, prefix);
                int pos = 0;
                memcpy(w->result + pos, prefix, g_fwd_len); pos += g_fwd_len;
                w->result[pos++] = (char)cv;  /* leaf char */
                /* Reverse suffix into result */
                for (int j = suffix_len - 1; j >= 0; j--)
                    w->result[pos++] = suffix[j];
                w->result[pos] = '\0';
                return 1;
            }
        }
    }
    return 0;
}

static void bw_recurse(worker_t *w, uint32_t lo, uint32_t hi,
                       char *suffix, int suffix_len, int steps_left)
{
    if (atomic_load_explicit(&g_found, memory_order_relaxed)) return;

    if (steps_left == 1) {
        if (leaf_probe(w, lo, hi, suffix, suffix_len)) {
            int exp = 0;
            atomic_compare_exchange_strong(&g_found, &exp, 1);
        }
        return;
    }

    prev_t cands[256];
    int n = invert_step(lo, hi, cands, w->alnum_only);
    for (int i = 0; i < n && !atomic_load_explicit(&g_found, memory_order_relaxed); i++) {
        suffix[suffix_len] = (char)cands[i].c;
        bw_recurse(w, cands[i].lo, cands[i].hi, suffix, suffix_len + 1, steps_left - 1);
    }
}

static void *thread_func(void *arg) {
    worker_t *w = arg;
    w->tested = 0;
    for (int i = w->cand_start; i < w->cand_start + w->cand_count; i++) {
        if (atomic_load_explicit(&g_found, memory_order_relaxed)) break;
        char suffix[MAX_PW_LEN];
        suffix[0] = (char)w->first_cands[i].c;
        bw_recurse(w, w->first_cands[i].lo, w->first_cands[i].hi,
                   suffix, 1, w->remaining_steps);
    }
    return NULL;
}

static uint64_t parallel_backward(int total_steps, int alnum_only, int num_threads) {
    prev_t first[256];
    int n_first = invert_step(g_target_lo, g_target_hi, first, alnum_only);

    if (total_steps == 1) {
        worker_t w = { .alnum_only = alnum_only };
        if (leaf_probe(&w, g_target_lo, g_target_hi, NULL, 0)) {
            memcpy(g_result, w.result, sizeof(g_result));
            atomic_store(&g_found, 1);
        }
        return w.tested;
    }

    if (num_threads > n_first) num_threads = n_first;
    pthread_t *threads = malloc(num_threads * sizeof(pthread_t));
    worker_t  *workers = calloc(num_threads, sizeof(worker_t));

    int per = n_first / num_threads, rem = n_first % num_threads, off = 0;
    for (int t = 0; t < num_threads; t++) {
        int chunk = per + (t < rem ? 1 : 0);
        workers[t] = (worker_t){
            .first_cands = first, .cand_start = off, .cand_count = chunk,
            .remaining_steps = total_steps - 1, .alnum_only = alnum_only
        };
        off += chunk;
    }

    for (int t = 0; t < num_threads; t++)
        pthread_create(&threads[t], NULL, thread_func, &workers[t]);
    for (int t = 0; t < num_threads; t++)
        pthread_join(threads[t], NULL);

    uint64_t total = 0;
    for (int t = 0; t < num_threads; t++) {
        total += workers[t].tested;
        if (workers[t].result[0] && !g_result[0])
            memcpy(g_result, workers[t].result, sizeof(g_result));
    }
    free(threads); free(workers);
    return total;
}

/* ── Brute force (short passwords) ────────────────────────────────── */

static void bf_search(int depth, int max_depth, uint32_t lo, uint32_t hi,
                      char *buf, const char *charset, int charset_len)
{
    if (atomic_load_explicit(&g_found, memory_order_relaxed)) return;
    if (depth == max_depth) {
        if (lo == g_target_lo && hi == g_target_hi) {
            memcpy(g_result, buf, max_depth);
            g_result[max_depth] = '\0';
            atomic_store(&g_found, 1);
        }
        return;
    }
    for (int ci = 0; ci < charset_len && !atomic_load_explicit(&g_found, memory_order_relaxed); ci++) {
        int c = charset[ci];
        buf[depth] = (char)c;
        uint32_t ch = ((uint32_t)c - 0x2A) & 0xFF;
        uint32_t top6 = lo & 0xFC000000u;
        bf_search(depth + 1, max_depth, ch ^ (lo << 6) ^ KEY,
                  top6 ^ ror32(hi, 26) ^ KEY, buf, charset, charset_len);
    }
}

/* ── Search tier ──────────────────────────────────────────────────── */

static void search_tier(const char *charset, int charset_len,
                        int fwd_len, int max_len, int num_threads,
                        int alnum_only, const char *label)
{
    printf("=== %s ===\n\n", label);

    char buf[MAX_PW_LEN + 1];
    for (int len = 1; len <= fwd_len && len <= max_len && !atomic_load(&g_found); len++) {
        printf("  Brute force length %d...", len); fflush(stdout);
        bf_search(0, len, 0, 0, buf, charset, charset_len);
        printf(" %s\n", atomic_load(&g_found) ? "FOUND" : "no match");
    }
    if (atomic_load(&g_found) || max_len <= fwd_len) return;

    if (build_forward_table(charset, charset_len, fwd_len, label) < 0) {
        printf("  Skipping (allocation failed)\n\n");
        return;
    }

    for (int len = fwd_len + 1; len <= max_len && !atomic_load(&g_found); len++) {
        int bk = len - fwd_len;
        struct timespec t0, t1;
        clock_gettime(CLOCK_MONOTONIC, &t0);
        printf("  MITM length %d (%d backward steps, %d threads)...", len, bk, num_threads);
        fflush(stdout);
        memset(g_result, 0, sizeof(g_result));
        uint64_t lookups = parallel_backward(bk, alnum_only, num_threads);
        clock_gettime(CLOCK_MONOTONIC, &t1);
        double el = (t1.tv_sec-t0.tv_sec) + (t1.tv_nsec-t0.tv_nsec)/1e9;
        if (atomic_load(&g_found))
            printf(" FOUND in %.2fs (%llu lookups)\n", el, (unsigned long long)lookups);
        else
            printf(" no match (%.2fs, %llu lookups)\n", el, (unsigned long long)lookups);
    }
    free_tables();
}

/* ── RAM detection ────────────────────────────────────────────────── */

static int can_alloc_gb(double gb) {
    FILE *f = fopen("/proc/meminfo", "r");
    if (!f) return (gb < 2.0);
    char line[256]; long avail_kb = 0;
    while (fgets(line, sizeof(line), f))
        if (sscanf(line, "MemAvailable: %ld", &avail_kb) == 1) break;
    fclose(f);
    return avail_kb > 0 ? (gb < avail_kb / 1e6 - 1.0) : (gb < 2.0);
}

static int get_num_cpus(void) {
#ifdef _SC_NPROCESSORS_ONLN
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    return (n > 0) ? (int)n : 4;
#else
    return 4;
#endif
}

/* ── Self-test ────────────────────────────────────────────────────── */

static int self_test(void) {
    init_charsets();
    printf("=== Self-test ===\n\n");

    struct { const char *pw; uint32_t lo, hi; } tests[] = {
        { "RISC",       0x7B5819DD, 0x0BF9E580 },
        { "dragon",     0x37262280, 0x3B99A325 },
        { "computer",   0x78D14D0C, 0x07F3F003 },
        { NULL, 0, 0 }
    };

    int pass = 1;
    for (int i = 0; tests[i].pw; i++) {
        g_target_lo = tests[i].lo;
        g_target_hi = tests[i].hi;
        atomic_store(&g_found, 0);
        memset(g_result, 0, sizeof(g_result));
        int orig_len = strlen(tests[i].pw);
        search_tier(g_alnum, NUM_ALNUM, 4, orig_len, 2, 1, "test");
        if (atomic_load(&g_found)) {
            uint32_t vlo, vhi;
            idefs_hash(g_result, &vlo, &vhi);
            int ok = (vlo == tests[i].lo && vhi == tests[i].hi);
            printf("  → \"%s\" recovered as \"%s\"  %s\n\n",
                   tests[i].pw, g_result, ok ? "PASS" : "FAIL");
            if (!ok) pass = 0;
        } else {
            printf("  → \"%s\" NOT FOUND  FAIL\n\n", tests[i].pw);
            pass = 0;
        }
    }
    printf("Overall: %s\n", pass ? "ALL PASSED" : "SOME FAILED");
    return pass ? 0 : 1;
}

/* ── Main ─────────────────────────────────────────────────────────── */

int main(int argc, char **argv) {
    init_charsets();
    int ncpu = get_num_cpus();

    if (argc >= 2 && strcmp(argv[1], "--self-test") == 0)
        return self_test();

    if (argc < 3) {
        fprintf(stderr,
            "IDEFS Password Recovery (Optimised MITM)\n"
            "=========================================\n\n"
            "Usage: %s <hash_lo> <hash_hi> [max_length] [num_threads]\n"
            "       %s --self-test\n\n"
            "Passwords are CASE-SENSITIVE. Requires 0.2-12 GB RAM.\n\n"
            "Example: %s 0x37262280 0x3B99A325\n",
            argv[0], argv[0], argv[0]);
        return 1;
    }

    g_target_lo = (uint32_t)strtoul(argv[1], NULL, 0);
    g_target_hi = (uint32_t)strtoul(argv[2], NULL, 0);
    int max_len     = (argc > 3) ? atoi(argv[3]) : 10;
    int num_threads = (argc > 4) ? atoi(argv[4]) : ncpu;
    if (max_len < 1) max_len = 1;
    if (max_len > MAX_PW_LEN) max_len = MAX_PW_LEN;
    if (num_threads < 1) num_threads = 1;

    printf("IDEFS Password Recovery (Optimised MITM)\n");
    printf("=========================================\n");
    printf("Target:   lo=0x%08X  hi=0x%08X\n", g_target_lo, g_target_hi);
    printf("Max len:  %d\n", max_len);
    printf("Threads:  %d\n", num_threads);
    printf("Case:     SENSITIVE\n\n");

    if (g_target_lo == 0 && g_target_hi == 0) {
        printf("Result: empty password (no password set)\n");
        return 0;
    }

    struct timespec t_start;
    clock_gettime(CLOCK_MONOTONIC, &t_start);
    atomic_store(&g_found, 0);
    memset(g_result, 0, sizeof(g_result));

    int alnum_fwd = can_alloc_gb(14.0) ? 5 : 4;
    printf("Forward table: FWD=%d\n\n", alnum_fwd);

    /* Tier 1: Alphanumeric */
    search_tier(g_alnum, NUM_ALNUM, alnum_fwd, max_len, num_threads,
                1, "Tier 1: Alphanumeric (a-z A-Z 0-9)");

    /* Tier 2: Full charset */
    if (!atomic_load(&g_found)) {
        printf("\n");
        search_tier(g_full, NUM_CHARS, 4, max_len, num_threads,
                    0, "Tier 2: Full charset (all printable ASCII)");
    }

    struct timespec t_end;
    clock_gettime(CLOCK_MONOTONIC, &t_end);
    double total = (t_end.tv_sec-t_start.tv_sec) + (t_end.tv_nsec-t_start.tv_nsec)/1e9;

    if (atomic_load(&g_found)) {
        uint32_t vlo, vhi;
        idefs_hash(g_result, &vlo, &vhi);
        int all_an = 1;
        for (int j = 0; g_result[j]; j++)
            if (!isalnum((unsigned char)g_result[j])) all_an = 0;
        printf("\nRecovered password: \"%s\" (length %zu, %s)\n",
               g_result, strlen(g_result), all_an ? "alphanumeric" : "has punctuation");
        printf("Verify:  lo=0x%08X  hi=0x%08X  %s\n",
               vlo, vhi, (vlo == g_target_lo && vhi == g_target_hi) ? "MATCH" : "ERROR");
        printf("Total time: %.2fs\n", total);
    } else {
        printf("\nNo preimage found up to length %d (%.1fs)\n", max_len, total);
    }
    return atomic_load(&g_found) ? 0 : 1;
}
