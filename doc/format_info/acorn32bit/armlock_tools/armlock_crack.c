/*
 * armlock_crack.c — Brute-force and dictionary cracker for ARMlock hashes.
 *
 * Hash algorithm (two 32-bit accumulators, fixed seeds):
 *   v3 = 0x89ABCDEF, v4 = 0x01234567
 *   for each char c (uppercased):
 *     rotated = (v3 ASR 13) | (v3 LSL 19)
 *     v3 = rotated + c
 *     v4 ^= v3
 *   hash = { v3, v4 }
 *
 * Key weakness: v4 never feeds back into v3, so the inner loop only
 * needs to track v3 (32 bits).  v4 is recomputed only when v3 matches.
 *
 * Modes:
 *   Brute-force:  armlock_crack <v3> <v4> [max_len]
 *   Dictionary:   armlock_crack <v3> <v4> -w wordlist.txt
 *   Dict+rules:   armlock_crack <v3> <v4> -w wordlist.txt -r
 *
 * Compile: gcc -O3 -o armlock_crack armlock_crack.c
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <ctype.h>
#include <time.h>

#define MAX_PW_LEN   11
#define SEED_V3      0x89ABCDEF
#define SEED_V4      0x01234567

/* ------------------------------------------------------------------ */
/* Hash primitives                                                     */
/* ------------------------------------------------------------------ */

static inline uint32_t hash_step_v3(uint32_t v3, uint32_t c)
{
    uint32_t rotated = ((uint32_t)((int32_t)v3 >> 13)) | (v3 << 19);
    return rotated + c;
}

/* Full hash — only called for verification */
static void hash_full(const char *pw, uint32_t *out_v3, uint32_t *out_v4)
{
    uint32_t v3 = SEED_V3, v4 = SEED_V4;
    for (const char *p = pw; *p; p++) {
        unsigned char c = *p;
        if (c >= 'a' && c <= 'z') c -= 32;
        v3 = hash_step_v3(v3, c);
        v4 ^= v3;
    }
    *out_v3 = v3;
    *out_v4 = v4;
}

static int verify(const char *pw, uint32_t target_v3, uint32_t target_v4)
{
    uint32_t v3, v4;
    hash_full(pw, &v3, &v4);
    return v3 == target_v3 && v4 == target_v4;
}

static void report_found(const char *pw, uint32_t target_v3, uint32_t target_v4)
{
    uint32_t v3, v4;
    hash_full(pw, &v3, &v4);
    printf("\n*** FOUND: \"%s\" ***\n", pw);
    printf("    Hash: v3=0x%08X v4=0x%08X\n", v3, v4);
}

/* ------------------------------------------------------------------ */
/* Brute-force: v3-only inner loop                                     */
/* ------------------------------------------------------------------ */

static char charset[128];
static int charset_len = 0;

static void build_charset(void)
{
    for (int c = 0x20; c <= 0x7E; c++) {
        if (c >= 'a' && c <= 'z') continue;
        charset[charset_len++] = (char)c;
    }
}

static uint32_t bf_target_v3, bf_target_v4;
static int bf_max_len;
static long long bf_attempts = 0;
static int bf_found = 0;

static void brute_recurse(char *buf, int pos, uint32_t v3)
{
    if (bf_found) return;

    if (pos > 0 && v3 == bf_target_v3) {
        buf[pos] = '\0';
        if (verify(buf, bf_target_v3, bf_target_v4)) {
            report_found(buf, bf_target_v3, bf_target_v4);
            bf_found = 1;
            return;
        }
    }

    if (pos >= bf_max_len) return;

    for (int i = 0; i < charset_len; i++) {
        buf[pos] = charset[i];
        bf_attempts++;
        brute_recurse(buf, pos + 1, hash_step_v3(v3, (unsigned char)charset[i]));
        if (bf_found) return;
    }
}

static int do_brute(uint32_t target_v3, uint32_t target_v4, int max_len)
{
    build_charset();
    bf_target_v3 = target_v3;
    bf_target_v4 = target_v4;
    bf_max_len = max_len;

    long long total = 0, n = 1;
    for (int i = 1; i <= max_len; i++) { n *= charset_len; total += n; }

    printf("Mode: brute-force (v3-only inner loop)\n");
    printf("Charset: %d chars (printable ASCII, no lowercase)\n", charset_len);
    printf("Max length: %d\n", max_len);
    printf("Search space: %lld candidates\n\n", total);

    char buf[MAX_PW_LEN + 1];
    memset(buf, 0, sizeof(buf));

    time_t start = time(NULL);
    brute_recurse(buf, 0, SEED_V3);
    time_t elapsed = time(NULL) - start;
    if (elapsed < 1) elapsed = 1;

    if (!bf_found) printf("\nNot found within %d characters.\n", max_len);
    printf("Tried %lld candidates in %ld seconds (%lld/sec)\n",
           bf_attempts, elapsed, bf_attempts / elapsed);
    return bf_found;
}

/* ------------------------------------------------------------------ */
/* Dictionary attack with optional rules                               */
/* ------------------------------------------------------------------ */

static long long dict_attempts = 0;
static int dict_found = 0;

static int try_candidate(const char *pw, uint32_t target_v3, uint32_t target_v4)
{
    dict_attempts++;
    uint32_t v3 = SEED_V3;
    for (const char *p = pw; *p; p++) {
        unsigned char c = *p;
        if (c >= 'a' && c <= 'z') c -= 32;
        v3 = hash_step_v3(v3, c);
    }
    if (v3 == target_v3 && verify(pw, target_v3, target_v4)) {
        report_found(pw, target_v3, target_v4);
        return 1;
    }
    return 0;
}

static int try_with_rules(const char *word, uint32_t tv3, uint32_t tv4)
{
    char buf[256];
    int len = strlen(word);
    if (len < 1 || len > MAX_PW_LEN) return 0;

    /* Rule 0: word as-is */
    if (try_candidate(word, tv3, tv4)) return 1;

    /* Rule 1: UPPERCASE */
    for (int i = 0; i < len; i++) buf[i] = toupper((unsigned char)word[i]);
    buf[len] = '\0';
    if (try_candidate(buf, tv3, tv4)) return 1;

    /* Rule 2: lowercase */
    for (int i = 0; i < len; i++) buf[i] = tolower((unsigned char)word[i]);
    buf[len] = '\0';
    if (try_candidate(buf, tv3, tv4)) return 1;

    /* Rule 3: Capitalise */
    buf[0] = toupper((unsigned char)word[0]);
    for (int i = 1; i < len; i++) buf[i] = tolower((unsigned char)word[i]);
    buf[len] = '\0';
    if (try_candidate(buf, tv3, tv4)) return 1;

    /* Rule 4: reversed */
    for (int i = 0; i < len; i++) buf[i] = word[len - 1 - i];
    buf[len] = '\0';
    if (try_candidate(buf, tv3, tv4)) return 1;

    /* Rules 5+: append digit 0-9 to each case variant */
    for (int d = 0; d <= 9; d++) {
        if (len + 1 > MAX_PW_LEN) continue;

        /* word + digit */
        strcpy(buf, word);
        buf[len] = '0' + d; buf[len + 1] = '\0';
        if (try_candidate(buf, tv3, tv4)) return 1;

        /* UPPER + digit */
        for (int i = 0; i < len; i++) buf[i] = toupper((unsigned char)word[i]);
        buf[len] = '0' + d; buf[len + 1] = '\0';
        if (try_candidate(buf, tv3, tv4)) return 1;

        /* Capital + digit */
        buf[0] = toupper((unsigned char)word[0]);
        for (int i = 1; i < len; i++) buf[i] = tolower((unsigned char)word[i]);
        buf[len] = '0' + d; buf[len + 1] = '\0';
        if (try_candidate(buf, tv3, tv4)) return 1;
    }

    /* Prepend digit */
    for (int d = 0; d <= 9; d++) {
        if (len + 1 > MAX_PW_LEN) continue;
        buf[0] = '0' + d;
        strcpy(buf + 1, word);
        if (try_candidate(buf, tv3, tv4)) return 1;
    }

    /* Doubled word */
    if (len * 2 <= MAX_PW_LEN) {
        strcpy(buf, word);
        strcpy(buf + len, word);
        if (try_candidate(buf, tv3, tv4)) return 1;
    }

    /* Common substitutions */
    const char *from = "AEIOST";
    const char *to   = "@3!0$7";
    for (int s = 0; from[s]; s++) {
        strcpy(buf, word);
        for (int i = 0; i < len; i++) {
            if (toupper((unsigned char)buf[i]) == from[s])
                buf[i] = to[s];
        }
        if (try_candidate(buf, tv3, tv4)) return 1;
    }

    /* Common suffixes */
    const char *suffixes[] = {"!", "?", "1!", "123", "69", "99", NULL};
    for (int s = 0; suffixes[s]; s++) {
        int slen = strlen(suffixes[s]);
        if (len + slen > MAX_PW_LEN) continue;
        strcpy(buf, word);
        strcpy(buf + len, suffixes[s]);
        if (try_candidate(buf, tv3, tv4)) return 1;
    }

    return 0;
}

static int do_dictionary(uint32_t target_v3, uint32_t target_v4,
                         const char *wordlist, int rules)
{
    FILE *f = fopen(wordlist, "r");
    if (!f) {
        fprintf(stderr, "Cannot open %s\n", wordlist);
        return 0;
    }

    printf("Mode: dictionary%s\n", rules ? " + rules" : "");
    printf("Wordlist: %s\n\n", wordlist);

    char line[256];
    time_t start = time(NULL);

    while (fgets(line, sizeof(line), f)) {
        int len = strlen(line);
        while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r'))
            line[--len] = '\0';
        if (len == 0 || len > MAX_PW_LEN) continue;

        if (rules) {
            if (try_with_rules(line, target_v3, target_v4)) {
                dict_found = 1;
                break;
            }
        } else {
            if (try_candidate(line, target_v3, target_v4)) {
                dict_found = 1;
                break;
            }
        }
    }
    fclose(f);

    time_t elapsed = time(NULL) - start;
    if (elapsed < 1) elapsed = 1;
    if (!dict_found) printf("\nNot found in wordlist.\n");
    printf("Tried %lld candidates in %ld seconds\n", dict_attempts, elapsed);
    return dict_found;
}

/* ------------------------------------------------------------------ */
/* Main                                                                */
/* ------------------------------------------------------------------ */

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr,
            "ARMlock password hash cracker\n\n"
            "Usage:\n"
            "  %s <v3_hex> <v4_hex> [max_len]         Brute-force\n"
            "  %s <v3_hex> <v4_hex> -w wordlist.txt    Dictionary\n"
            "  %s <v3_hex> <v4_hex> -w wordlist.txt -r Dictionary + rules\n"
            "\nExample: %s 8E2B21C5 177C3437 8\n",
            argv[0], argv[0], argv[0], argv[0]);
        return 1;
    }

    uint32_t target_v3 = (uint32_t)strtoul(argv[1], NULL, 16);
    uint32_t target_v4 = (uint32_t)strtoul(argv[2], NULL, 16);

    printf("ARMlock hash cracker\n");
    printf("Target: v3=0x%08X v4=0x%08X\n\n", target_v3, target_v4);

    const char *wordlist = NULL;
    int rules = 0;
    int max_len = 8;

    for (int i = 3; i < argc; i++) {
        if (strcmp(argv[i], "-w") == 0 && i + 1 < argc) {
            wordlist = argv[++i];
        } else if (strcmp(argv[i], "-r") == 0) {
            rules = 1;
        } else {
            max_len = atoi(argv[i]);
        }
    }

    if (max_len > MAX_PW_LEN) max_len = MAX_PW_LEN;

    if (wordlist) {
        return do_dictionary(target_v3, target_v4, wordlist, rules) ? 0 : 1;
    } else {
        return do_brute(target_v3, target_v4, max_len) ? 0 : 1;
    }
}
