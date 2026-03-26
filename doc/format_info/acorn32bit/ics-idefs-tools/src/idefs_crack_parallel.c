/*
 * idefs_crack_parallel.c — Multi-threaded IDEFS Password Hash Recovery
 * =====================================================================
 * Targets: ICS/Baildon Electronics IDEFS v3.15 ("Wizzo") for RISC OS
 *
 * Build:   gcc -O3 -pthread -o idefs_crack_parallel idefs_crack_parallel.c
 * Usage:   ./idefs_crack_parallel <hash_lo> <hash_hi> [max_length] [num_threads]
 *          ./idefs_crack_parallel --self-test
 *
 * Parallelisation strategy:
 *   For each password length, the 94 possible first-character values are
 *   distributed across N worker threads.  Each thread does a full depth-first
 *   search of its assigned subtrees.  A shared atomic flag provides early
 *   termination when any thread finds a match.
 *
 *   This gives near-linear speedup up to 94 threads (one per first char).
 *   Beyond that there is no benefit — but 94 threads already exceeds
 *   typical core counts.
 *
 * Timing (rough, single core @ ~100M hash/sec):
 *   Length 5: ~1 min   → with 8 threads: ~8 sec
 *   Length 6: ~2 hours → with 8 threads: ~15 min
 *   Length 7: ~7 days  → with 8 threads: ~21 hours
 *   Length 8: ~2 years → with 16 threads: ~45 days
 */

#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <time.h>
#include <pthread.h>
#include <stdatomic.h>

/* ── Constants ────────────────────────────────────────────────────── */

#define KEY        0x01810284u
#define MIN_CHAR   0x21
#define MAX_CHAR   0x7E
#define NUM_CHARS  (MAX_CHAR - MIN_CHAR + 1)  /* 94 */
#define MAX_PW_LEN 10

static inline uint32_t ror32(uint32_t v, unsigned n) {
    return (v >> n) | (v << (32 - n));
}

/* ── Forward hash ─────────────────────────────────────────────────── */

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

/* ── Shared state ─────────────────────────────────────────────────── */

static uint32_t       g_target_lo, g_target_hi;
static atomic_int     g_found;
static char           g_result[MAX_PW_LEN + 1];
static atomic_uint_fast64_t g_total_tested;

/* ── Per-thread search ────────────────────────────────────────────── */

typedef struct {
    int first_char_lo;   /* inclusive */
    int first_char_hi;   /* exclusive */
    int max_depth;
    uint64_t tested;
    char local_buf[MAX_PW_LEN + 1];
} thread_work_t;

static void search(thread_work_t *w, int depth, int max_depth,
                   uint32_t lo, uint32_t hi)
{
    if (atomic_load_explicit(&g_found, memory_order_relaxed))
        return;

    if (depth == max_depth) {
        w->tested++;
        if (lo == g_target_lo && hi == g_target_hi) {
            /* Race: first writer wins, but any match is valid */
            int expected = 0;
            if (atomic_compare_exchange_strong(&g_found, &expected, 1)) {
                memcpy(g_result, w->local_buf, max_depth);
                g_result[max_depth] = '\0';
            }
        }
        return;
    }

    for (int c = MIN_CHAR; c <= MAX_CHAR; c++) {
        if (atomic_load_explicit(&g_found, memory_order_relaxed))
            return;

        uint32_t ch   = ((uint32_t)c - 0x2A) & 0xFF;
        uint32_t top6 = lo & 0xFC000000u;
        uint32_t nhi  = top6 ^ ror32(hi, 26) ^ KEY;
        uint32_t nlo  = ch ^ (lo << 6) ^ KEY;

        w->local_buf[depth] = (char)c;
        search(w, depth + 1, max_depth, nlo, nhi);
    }
}

static void *thread_func(void *arg) {
    thread_work_t *w = (thread_work_t *)arg;
    w->tested = 0;

    if (w->max_depth == 0) return NULL;  /* shouldn't happen */

    /* Each thread handles a subset of first characters */
    for (int c = w->first_char_lo; c < w->first_char_hi; c++) {
        if (atomic_load_explicit(&g_found, memory_order_relaxed))
            break;

        uint32_t ch   = ((uint32_t)c - 0x2A) & 0xFF;
        uint32_t top6 = 0 & 0xFC000000u;  /* lo starts at 0 */
        uint32_t nhi  = top6 ^ ror32((uint32_t)0, 26) ^ KEY;
        uint32_t nlo  = ch ^ (0 << 6) ^ KEY;

        w->local_buf[0] = (char)c;

        if (w->max_depth == 1) {
            /* Leaf: check directly */
            w->tested++;
            if (nlo == g_target_lo && nhi == g_target_hi) {
                int expected = 0;
                if (atomic_compare_exchange_strong(&g_found, &expected, 1)) {
                    g_result[0] = (char)c;
                    g_result[1] = '\0';
                }
            }
        } else {
            search(w, 1, w->max_depth, nlo, nhi);
        }
    }

    atomic_fetch_add(&g_total_tested, w->tested);
    return NULL;
}

/* ── Distribute first-chars across threads ────────────────────────── */

static int run_parallel(int max_depth, int num_threads) {
    if (num_threads > NUM_CHARS)
        num_threads = NUM_CHARS;

    pthread_t       *threads = malloc(num_threads * sizeof(pthread_t));
    thread_work_t   *works   = calloc(num_threads, sizeof(thread_work_t));

    /* Distribute NUM_CHARS first-character values across threads */
    int chars_per = NUM_CHARS / num_threads;
    int remainder = NUM_CHARS % num_threads;
    int offset = MIN_CHAR;

    for (int t = 0; t < num_threads; t++) {
        works[t].first_char_lo = offset;
        int chunk = chars_per + (t < remainder ? 1 : 0);
        offset += chunk;
        works[t].first_char_hi = offset;
        works[t].max_depth = max_depth;
    }

    /* Launch */
    for (int t = 0; t < num_threads; t++)
        pthread_create(&threads[t], NULL, thread_func, &works[t]);

    /* Join */
    for (int t = 0; t < num_threads; t++)
        pthread_join(threads[t], NULL);

    free(threads);
    free(works);

    return atomic_load(&g_found);
}

/* ── Analytical single-char solve ─────────────────────────────────── */

static int solve_single_char(void) {
    if (g_target_hi != KEY) return 0;
    uint32_t ch = g_target_lo ^ KEY;
    if (ch > 0xFF) return 0;
    for (int offset = 0; offset <= 256; offset += 256) {
        int c = (int)(ch + 0x2A) + offset;
        if (c >= MIN_CHAR && c <= MAX_CHAR) {
            g_result[0] = (char)c;
            g_result[1] = '\0';
            atomic_store(&g_found, 1);
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

    printf("Hash self-test:\n");
    int pass = 1;
    for (int i = 0; tests[i].pw; i++) {
        uint32_t lo, hi;
        idefs_hash(tests[i].pw, &lo, &hi);
        int ok = (lo == tests[i].lo && hi == tests[i].hi);
        printf("  %-10s  lo=0x%08X hi=0x%08X  %s\n",
               tests[i].pw, lo, hi, ok ? "PASS" : "FAIL");
        if (!ok) pass = 0;
    }

    /* Recovery test with 4 threads */
    printf("\nParallel recovery test (4 threads):\n");
    struct { uint32_t lo, hi; int max_len; } cracks[] = {
        { 0x01810293, 0x01810284, 1 },
        { 0x7B5819DD, 0x0BF9E580, 4 },
        { 0x7AD2418E, 0x0BF9E580, 4 },
        { 0, 0, 0 }
    };

    for (int i = 0; cracks[i].max_len; i++) {
        g_target_lo = cracks[i].lo;
        g_target_hi = cracks[i].hi;
        atomic_store(&g_found, 0);
        atomic_store(&g_total_tested, 0);

        if (!solve_single_char()) {
            for (int len = 1; len <= cracks[i].max_len && !atomic_load(&g_found); len++)
                run_parallel(len, 4);
        }

        if (atomic_load(&g_found)) {
            uint32_t vlo, vhi;
            idefs_hash(g_result, &vlo, &vhi);
            int ok = (vlo == cracks[i].lo && vhi == cracks[i].hi);
            printf("  0x%08X:0x%08X -> \"%s\"  %s\n",
                   cracks[i].lo, cracks[i].hi, g_result, ok ? "PASS" : "FAIL");
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

static int get_num_cpus(void) {
#ifdef _SC_NPROCESSORS_ONLN
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    return (n > 0) ? (int)n : 4;
#else
    return 4;
#endif
}

int main(int argc, char **argv) {
    if (argc >= 2 && strcmp(argv[1], "--self-test") == 0)
        return self_test();

    if (argc < 3) {
        int ncpu = get_num_cpus();
        fprintf(stderr,
            "IDEFS Password Hash Recovery (Multi-threaded)\n"
            "==============================================\n\n"
            "Usage:\n"
            "  %s <hash_lo> <hash_hi> [max_length] [num_threads]\n"
            "  %s --self-test\n\n"
            "Arguments:\n"
            "  hash_lo      Low word from boot block offset &1A8 (hex)\n"
            "  hash_hi      High word from boot block offset &1AC (hex)\n"
            "  max_length   Max password length to try (default: 7, max: 10)\n"
            "  num_threads  Worker threads (default: %d = detected CPUs, max: 94)\n\n"
            "Timing estimates (%d threads, ~100M hash/sec per core):\n"
            "  1-4 chars:  instant\n"
            "  5 chars:    ~%.0f sec\n"
            "  6 chars:    ~%.0f min\n"
            "  7 chars:    ~%.1f hours\n"
            "  8 chars:    ~%.0f days\n\n"
            "Example:\n"
            "  %s 0x7AD2418E 0x0BF9E580 7 8\n",
            argv[0], argv[0], ncpu, ncpu,
            73.0 / ncpu,
            115.0 / ncpu,
            175.0 / ncpu,
            700.0 / ncpu,
            argv[0]);
        return 1;
    }

    g_target_lo = (uint32_t)strtoul(argv[1], NULL, 0);
    g_target_hi = (uint32_t)strtoul(argv[2], NULL, 0);
    int max_len     = (argc > 3) ? atoi(argv[3]) : 7;
    int num_threads = (argc > 4) ? atoi(argv[4]) : get_num_cpus();
    if (max_len < 1) max_len = 1;
    if (max_len > MAX_PW_LEN) max_len = MAX_PW_LEN;
    if (num_threads < 1) num_threads = 1;
    if (num_threads > NUM_CHARS) num_threads = NUM_CHARS;

    printf("IDEFS Password Hash Recovery (Multi-threaded)\n");
    printf("=============================================\n");
    printf("Target:   lo=0x%08X  hi=0x%08X\n", g_target_lo, g_target_hi);
    printf("Threads:  %d\n", num_threads);
    printf("Max len:  %d\n\n", max_len);

    /* Empty password */
    if (g_target_lo == 0 && g_target_hi == 0) {
        printf("Result: empty password (no password set)\n");
        return 0;
    }

    struct timespec ts_start, ts_now;
    clock_gettime(CLOCK_MONOTONIC, &ts_start);

    atomic_store(&g_found, 0);
    atomic_store(&g_total_tested, 0);

    /* Analytical 1-char */
    if (solve_single_char()) {
        printf("Found (analytical, 1-char): \"%s\"\n", g_result);
    } else {
        for (int len = 1; len <= max_len && !atomic_load(&g_found); len++) {
            struct timespec t0;
            clock_gettime(CLOCK_MONOTONIC, &t0);

            printf("Searching length %d (%d threads)...", len, num_threads);
            fflush(stdout);

            run_parallel(len, num_threads);

            struct timespec t1;
            clock_gettime(CLOCK_MONOTONIC, &t1);
            double elapsed = (t1.tv_sec - t0.tv_sec)
                           + (t1.tv_nsec - t0.tv_nsec) / 1e9;

            if (atomic_load(&g_found))
                printf(" FOUND in %.2fs\n", elapsed);
            else
                printf(" exhausted in %.2fs\n", elapsed);
        }
    }

    clock_gettime(CLOCK_MONOTONIC, &ts_now);
    double total = (ts_now.tv_sec - ts_start.tv_sec)
                 + (ts_now.tv_nsec - ts_start.tv_nsec) / 1e9;
    uint64_t tested = atomic_load(&g_total_tested);

    if (atomic_load(&g_found)) {
        uint32_t vlo, vhi;
        idefs_hash(g_result, &vlo, &vhi);
        printf("\nRecovered password: \"%s\"\n", g_result);
        printf("Verify:  lo=0x%08X  hi=0x%08X  %s\n",
               vlo, vhi,
               (vlo == g_target_lo && vhi == g_target_hi) ? "MATCH" : "ERROR");
        printf("Tested %llu candidates in %.2fs (%.1fM/sec)\n",
               (unsigned long long)tested, total,
               tested / total / 1e6);
        printf("\nNote: May be a collision rather than the original password.\n");
    } else {
        printf("\nNot found up to length %d.\n", max_len);
        printf("Tested %llu candidates in %.2fs (%.1fM/sec)\n",
               (unsigned long long)tested, total,
               tested / total / 1e6);
    }

    return atomic_load(&g_found) ? 0 : 1;
}
