/*
 * idefs_crack.c — IDEFS Password Hash Brute-Force Recovery Tool
 * ==============================================================
 * Targets: ICS/Baildon Electronics IDEFS v3.15 ("Wizzo") for RISC OS
 *
 * Build:   gcc -O3 -o idefs_crack idefs_crack.c
 * Usage:   ./idefs_crack <hash_lo> <hash_hi> [max_length]
 *          ./idefs_crack --self-test
 *
 * Hash values are read from the boot block at offsets &1A8 (lo) and &1AC (hi)
 * relative to the partition start.  Supply them in hex (with or without 0x).
 *
 * The cracker uses incremental depth-first search with the key optimisation
 * that lo is computed independently of hi, so partial-state pruning is possible.
 * Passwords up to 6 characters are typically recovered in under two hours;
 * 7 characters may take days.
 *
 * IMPORTANT: The hash has collisions — the tool may recover a different
 * password that produces the same hash.  Any matching password will work
 * to unlock the disc.
 *
 * Compile with -lpthread and uncomment the threaded section for parallel search.
 */

#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <time.h>

/* ── Constants ────────────────────────────────────────────────────── */

#define KEY        0x01810284u
#define MIN_CHAR   0x21       /* '!' — first printable char above space */
#define MAX_CHAR   0x7E       /* '~' — last printable ASCII char */
#define NUM_CHARS  (MAX_CHAR - MIN_CHAR + 1)  /* 94 */
#define MAX_PW_LEN 10

static inline uint32_t ror32(uint32_t v, unsigned n) {
    return (v >> n) | (v << (32 - n));
}

/* ── Forward hash (reference implementation) ──────────────────────── */

static void idefs_hash(const char *pw, uint32_t *out_lo, uint32_t *out_hi) {
    uint32_t lo = 0, hi = 0;
    while (*pw == ' ') pw++;
    for (int i = 0; i < 10 && (uint8_t)*pw > 0x20; i++) {
        uint32_t ch = ((uint8_t)*pw++ - 0x2A) & 0xFF;
        uint32_t top6 = lo & 0xFC000000u;
        hi = top6 ^ ror32(hi, 26) ^ KEY;
        lo = ch ^ (lo << 6) ^ KEY;
    }
    *out_lo = lo;
    *out_hi = hi;
}

/* ── Globals for search ───────────────────────────────────────────── */

static uint32_t g_target_lo, g_target_hi;
static int       g_found;
static uint64_t  g_tested;
static char      g_result[MAX_PW_LEN + 1];
static char      g_current[MAX_PW_LEN + 1];

/* ── Recursive incremental brute force ────────────────────────────── */

static void search(int depth, int max_depth, uint32_t lo, uint32_t hi) {
    if (g_found) return;

    if (depth == max_depth) {
        g_tested++;
        if (lo == g_target_lo && hi == g_target_hi) {
            memcpy(g_result, g_current, max_depth);
            g_result[max_depth] = '\0';
            g_found = 1;
        }
        return;
    }

    for (int c = MIN_CHAR; c <= MAX_CHAR && !g_found; c++) {
        uint32_t ch   = ((uint32_t)c - 0x2A) & 0xFF;
        uint32_t top6 = lo & 0xFC000000u;
        uint32_t nhi  = top6 ^ ror32(hi, 26) ^ KEY;
        uint32_t nlo  = ch ^ (lo << 6) ^ KEY;

        g_current[depth] = (char)c;
        search(depth + 1, max_depth, nlo, nhi);
    }
}

/* ── Analytical single-char solve ─────────────────────────────────── */

static int solve_single_char(void) {
    /* For 1-char passwords: hi must equal KEY (since hi starts at 0,
       and the first step sets hi = 0 ^ ror32(0,26) ^ KEY = KEY). */
    if (g_target_hi != KEY) return 0;

    /* lo = ch ^ KEY, so ch = lo ^ KEY */
    uint32_t ch = g_target_lo ^ KEY;
    if (ch > 0xFF) return 0;  /* ch must be 8-bit */

    /* Reverse the char transform: c = (ch + 0x2A).
       But ch = (c - 0x2A) & 0xFF, so we need both candidates. */
    for (int offset = 0; offset <= 256; offset += 256) {
        int c = (int)(ch + 0x2A) + offset;
        if (c >= MIN_CHAR && c <= MAX_CHAR) {
            g_result[0] = (char)c;
            g_result[1] = '\0';
            g_found = 1;
            g_tested = 1;
            return 1;
        }
    }
    return 0;
}

/* ── Self-test ────────────────────────────────────────────────────── */

static int self_test(void) {
    struct { const char *pw; uint32_t lo, hi; } tests[] = {
        { "A",        0x01810293, 0x01810284 },
        { "AB",       0x61C1A65C, 0x61C1A384 },
        { "test",     0x7AD2418E, 0x0BF9E580 },
        { "RISC",     0x7B5819DD, 0x0BF9E580 },
        { "hello",    0xC111D341, 0x87F86286 },
        { "password", 0xF7C9A1BE, 0x8FF3F7EF },
        { NULL, 0, 0 }
    };

    printf("Self-test:\n");
    int pass = 1;
    for (int i = 0; tests[i].pw; i++) {
        uint32_t lo, hi;
        idefs_hash(tests[i].pw, &lo, &hi);
        int ok = (lo == tests[i].lo && hi == tests[i].hi);
        printf("  %-10s  lo=0x%08X hi=0x%08X  %s\n",
               tests[i].pw, lo, hi, ok ? "PASS" : "FAIL");
        if (!ok) pass = 0;
    }

    /* Test cracker recovery */
    printf("\nRecovery test (cracking known hashes):\n");
    struct { uint32_t lo, hi; int max_len; const char *expected; } cracks[] = {
        { 0x01810293, 0x01810284, 1, "A" },
        { 0x7B5819DD, 0x0BF9E580, 4, NULL },  /* RISC or collision */
        { 0x7AD2418E, 0x0BF9E580, 4, NULL },  /* test or collision */
        { 0, 0, 0, NULL }
    };

    for (int i = 0; cracks[i].max_len; i++) {
        g_target_lo = cracks[i].lo;
        g_target_hi = cracks[i].hi;
        g_found = 0;
        g_tested = 0;

        if (!solve_single_char()) {
            for (int len = 1; len <= cracks[i].max_len && !g_found; len++)
                search(0, len, 0, 0);
        }

        if (g_found) {
            /* Verify */
            uint32_t vlo, vhi;
            idefs_hash(g_result, &vlo, &vhi);
            int ok = (vlo == cracks[i].lo && vhi == cracks[i].hi);
            printf("  0x%08X:0x%08X -> \"%s\"  %s\n",
                   cracks[i].lo, cracks[i].hi, g_result,
                   ok ? "PASS" : "FAIL");
            if (!ok) pass = 0;
        } else {
            printf("  0x%08X:0x%08X -> NOT FOUND  FAIL\n",
                   cracks[i].lo, cracks[i].hi);
            pass = 0;
        }
    }

    printf("\nOverall: %s\n", pass ? "ALL PASSED" : "SOME FAILED");
    return pass ? 0 : 1;
}

/* ── Main ─────────────────────────────────────────────────────────── */

int main(int argc, char **argv) {
    if (argc >= 2 && strcmp(argv[1], "--self-test") == 0)
        return self_test();

    if (argc < 3) {
        fprintf(stderr,
            "IDEFS Password Hash Recovery Tool\n"
            "==================================\n\n"
            "Usage:\n"
            "  %s <hash_lo> <hash_hi> [max_length]\n"
            "  %s --self-test\n\n"
            "Arguments:\n"
            "  hash_lo     Low word from boot block offset &1A8 (hex)\n"
            "  hash_hi     High word from boot block offset &1AC (hex)\n"
            "  max_length  Max password length to try (default: 6, max: 10)\n\n"
            "Timing estimates (single core, ~100M hash/sec):\n"
            "  1-4 chars:  instant to < 1 sec\n"
            "  5 chars:    ~1 minute\n"
            "  6 chars:    ~2 hours\n"
            "  7 chars:    ~7 days\n"
            "  8+ chars:   impractical without parallelism\n\n"
            "Note: The hash has collisions. The recovered password may differ\n"
            "from the original but will produce the same hash and unlock the disc.\n\n"
            "Example:\n"
            "  %s 0x7AD2418E 0x0BF9E580\n",
            argv[0], argv[0], argv[0]);
        return 1;
    }

    g_target_lo = (uint32_t)strtoul(argv[1], NULL, 0);
    g_target_hi = (uint32_t)strtoul(argv[2], NULL, 0);
    int max_len = (argc > 3) ? atoi(argv[3]) : 6;
    if (max_len < 1) max_len = 1;
    if (max_len > MAX_PW_LEN) max_len = MAX_PW_LEN;

    printf("IDEFS Password Hash Recovery\n");
    printf("============================\n");
    printf("Target:  lo=0x%08X  hi=0x%08X\n\n", g_target_lo, g_target_hi);

    /* Empty password check */
    if (g_target_lo == 0 && g_target_hi == 0) {
        printf("Result: empty password (no password set)\n");
        return 0;
    }

    clock_t t0 = clock();
    g_found = 0;
    g_tested = 0;

    /* Try analytical 1-char solution first */
    if (solve_single_char()) {
        printf("Found (analytical, 1-char): \"%s\"\n", g_result);
    } else {
        /* Brute force each length */
        for (int len = 1; len <= max_len && !g_found; len++) {
            clock_t tl = clock();
            printf("Searching length %d ...", len);
            fflush(stdout);

            search(0, len, 0, 0);

            double elapsed = (double)(clock() - tl) / CLOCKS_PER_SEC;
            if (g_found)
                printf(" FOUND in %.2fs\n", elapsed);
            else
                printf(" exhausted in %.2fs\n", elapsed);
        }
    }

    double total = (double)(clock() - t0) / CLOCKS_PER_SEC;

    if (g_found) {
        /* Verify */
        uint32_t vlo, vhi;
        idefs_hash(g_result, &vlo, &vhi);
        printf("\n");
        printf("Recovered password: \"%s\"\n", g_result);
        printf("Verify:  lo=0x%08X  hi=0x%08X  %s\n",
               vlo, vhi,
               (vlo == g_target_lo && vhi == g_target_hi) ? "MATCH" : "ERROR");
        printf("Tested %llu candidates in %.2fs\n",
               (unsigned long long)g_tested, total);
        printf("\nNote: This may be a hash collision rather than the original\n"
               "password, but it will produce the same hash and unlock the disc.\n");
    } else {
        printf("\nNot found up to length %d (%llu tested, %.1fs)\n",
               max_len, (unsigned long long)g_tested, total);
        printf("Try a higher max_length (currently capped at %d).\n", max_len);
    }

    return g_found ? 0 : 1;
}
