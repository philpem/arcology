/*
 * fcfs2raw - Convert FCFS disc images to raw sector images
 *
 * Reads FCFS (FileCore Filing System) disc images (types 0, 1, and 2)
 * and converts them to raw flat sector images, optionally producing
 * sparse files where free/zero blocks are represented as holes.
 *
 * Based on reverse engineering of the FCFS RISC OS module by
 * Nick Craig-Wood & Sergio Monesi (1995-97).
 *
 * FCFS file format:
 *   - Disc data (raw, compacted, or block-compressed) from offset 0
 *   - Block offset table (type 2 only) before the trailer
 *   - 256-byte trailer at file_size - 0x100:
 *       +0x00: "FCFS" magic (0x53464346 LE)
 *       +0x04: type (0=raw, 1=compacted, 2=block-compressed)
 *       +0x08: map_offset (offset to zone 0 map block in compacted stream)
 *       +0x0C: offset_table_size (type 2 only)
 *
 * Image types:
 *   Type 0: Raw - complete sector-by-sector copy. file_offset == disc_address.
 *   Type 1: Compacted - only allocated sectors stored contiguously. Free
 *           sectors (identified by walking the FileCore zone allocation map)
 *           are omitted. To reconstruct the disc, walk allocation units in
 *           order: allocated units are read sequentially from the file, free
 *           units emit zeros.
 *   Type 2: Block-compressed - compacted data (as type 1) divided into
 *           64KB blocks, each independently LZ77-compressed. Offset table
 *           for random access. Decompresses to type 1 format.
 *
 * Usage: fcfs2raw [-s] [-v] [-i] input.fcfs [output.raw]
 *   -s  Create sparse output (seek over free/zero blocks)
 *   -v  Verbose output
 *   -i  Info only (don't convert, just show image details)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <errno.h>

#define FCFS_MAGIC      0x53464346  /* "FCFS" in little-endian */
#define TRAILER_SIZE    0x100       /* 256-byte trailer */
#define BLOCK_SIZE      0x10000     /* 64KB decompression blocks */
#define BLOCK_SHIFT     16

/* FCFS image types */
#define FCFS_TYPE_RAW           0
#define FCFS_TYPE_COMPACTED     1
#define FCFS_TYPE_COMPRESSED    2

/* Compression flags within a block */
#define COMP_FLAG_LZ77      0x01
#define COMP_FLAG_STORED    0xFF

static int verbose = 0;

/* ======================================================================
 * Structures
 * ====================================================================== */

/*
 * FCFS trailer — 256 bytes at the end of every FCFS image file.
 * Only the first 16 bytes are used; the rest are zero-padded.
 */
typedef struct {
    uint32_t magic;
    uint32_t type;
    uint32_t map_offset;         /* Disc address of zone 0's map block.
                                  * On a hard disc this equals the start of
                                  * the boot block area, e.g. 0x1000 for
                                  * 512-byte sectors. On a floppy (nzones=1)
                                  * it is 0.
                                  *
                                  * For type 0 this is a raw file offset
                                  * (file offsets equal disc addresses).
                                  * For types 1 and 2 it is an offset within
                                  * the compacted (or decompressed) data
                                  * stream — not a raw file offset. */
    uint32_t offset_table_size; /* Type 2 only: size of block offset table
                                 * in bytes. Not used for types 0 and 1. */
} fcfs_trailer_t;

/*
 * FileCore disc record — describes disc geometry and layout.
 * Found at +0x04 within zone 0's map block (floppy) or at +0x1C0 within
 * the boot block at disc address 0xC00 (hard disc).
 * See RISC OS PRM vol. 2, ch. 28 "FileCore", section "The disc record".
 */
typedef struct {
    uint8_t  log2_sector_size;   /* +0x00: log2 of sector size */
    uint8_t  sectors_per_track;  /* +0x01 */
    uint8_t  heads;              /* +0x02: number of disc heads */
    uint8_t  density;            /* +0x03: disc density */
    uint8_t  idlen;              /* +0x04: length of fragment id in bits */
    uint8_t  log2_bpmb;         /* +0x05: log2 bytes per map bit */
    uint8_t  skew;              /* +0x06: track-to-track sector skew */
    uint8_t  boot_option;       /* +0x07 */
    uint8_t  low_sector;        /* +0x08: lowest sector id on a track */
    uint8_t  nzones;            /* +0x09: low byte of number of zones in map.
                                 * The high byte is at offset +0x2A in the
                                 * extended disc record (RISC OS 4+), which
                                 * this 20-byte struct does not cover.  FCFS
                                 * images predate big maps, so nzones <= 255
                                 * is expected. */
    uint16_t zone_spare;        /* +0x0A: non-allocation bits between zones */
    uint32_t root_dir;          /* +0x0C: disc address of root directory */
    uint32_t disc_size_lo;      /* +0x10: disc size in bytes (low word) */
} __attribute__((packed)) filecore_disc_record_t;

/* This struct must match the on-disc byte layout exactly.  The packed
 * attribute ensures no padding on GCC/Clang; the static assert catches
 * any compiler that silently ignores it.  If porting to a compiler that
 * does not support __attribute__((packed)), replace the struct with
 * field-by-field byte reads from a raw buffer. */
_Static_assert(sizeof(filecore_disc_record_t) == 20,
               "filecore_disc_record_t must be 20 bytes (packed)");

/*
 * Decoded zone allocation map — built by parsing all nzones map blocks.
 * Contains disc geometry and a bitmap indicating which allocation units
 * are occupied vs free, enabling type 1/2 compacted images to be
 * expanded correctly.
 */
typedef struct {
    uint32_t disc_size;
    uint32_t bytes_per_map_bit;
    int      log2_bpmb;
    int      nzones;
    uint32_t sector_size;
    uint16_t zone_spare;
    uint32_t total_alloc_units;

    /* Bitmap: bit set = allocated, clear = free */
    uint8_t *alloc_bitmap;
} disc_map_t;

/*
 * Compacted-data reader abstraction.
 *
 * Both type 1 (compacted) and type 2 (block-compressed) images store the
 * same compacted byte stream — only allocated sectors, packed sequentially.
 * This reader provides uniform random access to that stream.
 *
 *   Type 1: reads directly from the file (file offsets = compacted offsets).
 *   Type 2: decompresses 64KB blocks on demand from the file, caching the
 *           most recently used block.
 *
 * Memory usage: O(sector_size * nzones) for build_disc_map (zone map copy),
 * plus one 64KB block buffer for type 2. Never allocates proportional to
 * disc size.
 */
typedef struct {
    FILE     *fp;             /* source file (for both types) */
    int       type;           /* FCFS_TYPE_COMPACTED or FCFS_TYPE_COMPRESSED */
    size_t    data_len;       /* total compacted data length */

    /* Type 2 only: */
    uint32_t *offset_table;   /* block offset table (malloced, NULL for type 1) */
    int       num_blocks;
    uint8_t  *block_buf;      /* current decompressed block (64KB, type 2 only) */
    int       cur_block;      /* which block is in block_buf (-1 = none) */
    int       cur_block_len;  /* decompressed size of current block */
} compacted_reader_t;

/* ======================================================================
 * LZ77 decompressor (matches FUN_000002b0 in FCFS module)
 *
 * Reverse-engineered from the module's ARM disassembly.  The format is
 * an LZ77 variant — NOT classic LZSS with a ring buffer:
 *
 *   Control byte: 8 flag bits (MSB first).
 *     bit=1 → literal: copy one byte from src to dst
 *     bit=0 → back-reference: two bytes encode:
 *       offset = b1 | ((b2 & 0x0F) << 8)   [12 bits, backward from dst]
 *       length = (b2 >> 4) + 3              [3..18]
 *       Copy `length` bytes from (dst − offset) to dst.
 *
 * The module's inner loop is unrolled 8× with 4-byte copy steps;
 * we reproduce the same logic without unrolling.  There is no ring
 * buffer — back-references point directly into previously-written
 * output via  SUB r5, dst, r5.
 *
 * The module has two code paths: a fast path (0x2BC) for when >= 17
 * bytes of source remain (no bounds checks per bit), and a slow path
 * (0x5C0) that checks src < srcend before each operation.
 * ====================================================================== */

static size_t lz_decompress(const uint8_t *src, size_t src_len,
                             uint8_t *dst, size_t dst_max)
{
    const uint8_t *src_end = src + src_len;
    uint8_t *dst_start = dst;
    uint8_t *dst_limit = dst + dst_max;

    while (src < src_end && dst < dst_limit) {
        uint8_t ctrl = *src++;

        for (int bit = 7; bit >= 0; bit--) {
            if (src >= src_end || dst >= dst_limit)
                goto done;

            if (ctrl & (1 << bit)) {
                /* Literal byte */
                *dst++ = *src++;
            } else {
                /* Back-reference into already-written output */
                if (src + 1 >= src_end)
                    goto done;

                uint8_t b1 = *src++;
                uint8_t b2 = *src++;

                uint32_t offset = b1 | ((b2 & 0x0F) << 8);
                int length = (b2 >> 4) + 3;

                uint8_t *ref = dst - offset;
                if (ref < dst_start)
                    ref = dst_start;  /* safety clamp */

                for (int i = 0; i < length && dst < dst_limit; i++)
                    *dst++ = ref[i];
            }
        }
    }

done:
    return (size_t)(dst - dst_start);
}

/* ======================================================================
 * Bit-level zone map access (matches FUN_00003470)
 *
 * The FileCore zone map is a packed bit array, read LSB-first.
 * Each bit in the allocation area corresponds to one allocation unit
 * (of 2^log2_bpmb bytes).  See the filecore_guide for full decoding.
 * ====================================================================== */

/*
 * Read nbits from the zone map at the given bit offset.
 * Handles spanning a 32-bit word boundary.
 *
 * Note: always reads words[word_idx + 1] even when not needed (bit_idx == 0).
 * Callers must ensure the map buffer has at least 4 bytes of padding beyond
 * the last valid word, or that bit_offset + nbits never falls in the last
 * word of the buffer.  In practice the map buffer is sector-aligned and the
 * last few bits are unused, so this is safe.
 */
static uint32_t read_map_bits(const uint8_t *map_data, uint32_t bit_offset,
                              int nbits)
{
    const uint32_t *words = (const uint32_t *)map_data;
    uint32_t word_idx = bit_offset >> 5;
    uint32_t bit_idx  = bit_offset & 0x1F;

    uint32_t w0 = words[word_idx];
    uint32_t w1 = words[word_idx + 1];

    uint32_t val = w0 >> bit_idx;
    if (bit_idx > 0)
        val |= w1 << (32 - bit_idx);

    if (nbits < 32)
        val &= (1u << nbits) - 1;

    return val;
}

/* ======================================================================
 * Build allocation bitmap for type 1 images
 *
 * Walks the FileCore zone maps and marks each allocation unit as
 * allocated (1) or free (0). The compacted image stores only the
 * allocated units, packed sequentially.
 *
 * Zone map layout within each sector:
 *   Zone 0: [4-byte zone header] [disc record to offset 0x40] [map bits]
 *   Zone N: [4-byte zone header] [map bits from offset 0x04]
 *
 * Within the map bit area, the first zone_spare bits of each zone continue
 * a fragment from the previous zone (they can't start a new fragment).
 * Fragment descriptors follow: idlen-bit ID + zero padding + terminating 1.
 * The total width of each descriptor in bits equals the number of
 * allocation units that fragment occupies.
 * ====================================================================== */

/*
 * Heuristic check: does this look like a plausible disc record?
 * log2_sector_size 8..12 covers 256 bytes (ADFS D/L format, ST506)
 * through 4096 bytes.  FCFS itself was tested on 512-byte and
 * 1024-byte sector discs; 256-byte support is untested but accepted.
 */
static int dr_looks_valid(const filecore_disc_record_t *dr)
{
    if (dr->log2_sector_size < 8 || dr->log2_sector_size > 12)
        return 0;
    if (dr->log2_bpmb < 3 || dr->log2_bpmb > 16)
        return 0;
    int nz = dr->nzones;
    if (nz == 0 || nz > 255)
        return 0;
    if (dr->disc_size_lo == 0 || dr->disc_size_lo > 0x80000000u)
        return 0;
    return 1;
}
static int reader_read(compacted_reader_t *r, size_t off, void *buf, size_t len);
static filecore_disc_record_t *find_disc_record(const uint8_t *buf,
                                                 size_t buflen,
                                                 const fcfs_trailer_t *trailer);

static disc_map_t *build_disc_map(compacted_reader_t *reader,
                                  const fcfs_trailer_t *trailer)
{
    /*
     * FileCore new map layout.
     * See RISC OS PRM vol. 2, ch. 28 "FileCore", sections "The map"
     * and "The zone".
     *
     * The disc is divided into nzones zones. The map is nzones sectors
     * long, located at the beginning of zone nzones/2 (rounded down).
     * Each map sector (map block) controls one zone.
     *
     * Map block 0 (controlling zone 0) has:
     *   +0x00: 4-byte header (ZoneCheck, FreeLink, CrossCheck)
     *   +0x04: Disc record (60 bytes, offsets 0-59)
     *   +0x40: Allocation bytes
     *
     * Map block N (N>0) has:
     *   +0x00: 4-byte header
     *   +0x04: Allocation bytes
     *
     * The allocation bytes are a bit stream of fragment blocks.
     * Each fragment block = idlen-bit fragment ID + zero padding + 1-bit terminator.
     * Total bits in a fragment block = number of allocation units in that fragment.
     *
     * Between zones, the first zone_spare bits continue the previous fragment.
     *
     * Fragment IDs:
     *   Part of free chain (reachable from FreeLink) = free space
     *   0 with terminating 1 = gap/padding
     *   1 = bad sectors
     *   2 = boot block + map + root dir
     *   >=3 = allocated disc objects
     *
     * For type 1 compaction: we need to identify which allocation units
     * are free (part of free chain) vs allocated (everything else).
     */

    /* Find the disc record. On a hard disc it's at
     * 0xC00+0x1C0 = 0xDC0 (within the boot block).
     * On a floppy it's at 0x04 (zone 0 map block + 4-byte header).
     * We only need the first 0x1000 bytes for this. */
    uint8_t head_buf[0x1000];
    size_t head_read = 0x1000;
    if (head_read > reader->data_len)
        head_read = reader->data_len;
    if (head_read < 8) {
        fprintf(stderr, "Error: data too small for disc record\n");
        return NULL;
    }
    int got = reader_read(reader, 0, head_buf, head_read);
    if (got < 8) {
        fprintf(stderr, "Error: couldn't read image header\n");
        return NULL;
    }
    head_read = (size_t)got;

    filecore_disc_record_t *dr = find_disc_record(head_buf, head_read, trailer);

    if (!dr) {
        fprintf(stderr, "Error: could not find valid disc record\n");
        return NULL;
    }

    uint32_t sector_size = 1u << dr->log2_sector_size;
    int nzones = dr->nzones;
    int idlen = dr->idlen;
    uint32_t zone_spare = dr->zone_spare;

    disc_map_t *dm = calloc(1, sizeof(disc_map_t));
    if (!dm) return NULL;

    dm->disc_size = dr->disc_size_lo;
    dm->log2_bpmb = dr->log2_bpmb;
    dm->bytes_per_map_bit = 1u << dr->log2_bpmb;
    dm->nzones = nzones;
    dm->sector_size = sector_size;
    dm->zone_spare = zone_spare;

    uint32_t zone0_bits = (sector_size - 64) * 8;
    uint32_t zoneN_bits = (sector_size - 4) * 8;
    uint32_t map_capacity = zone0_bits + (uint32_t)(nzones - 1) * zoneN_bits;
    uint32_t disc_units = dm->disc_size / dm->bytes_per_map_bit;
    
    dm->total_alloc_units = (map_capacity < disc_units) ? map_capacity : disc_units;
    
    dm->alloc_bitmap = calloc((dm->total_alloc_units + 7) / 8, 1);
    if (!dm->alloc_bitmap) { free(dm); return NULL; }

    if (verbose) {
        printf("FileCore disc record:\n");
        printf("  Sector: %u bytes, BPMB: %u, idlen: %u\n",
               sector_size, dm->bytes_per_map_bit, idlen);
        printf("  Zones: %d, zone_spare: %u bits\n", nzones, zone_spare);
        printf("  Disc size: %u bytes (%.1f MB)\n",
               dm->disc_size, dm->disc_size / (1024.0 * 1024.0));
        printf("  Disc units: %u, Map capacity: %u, Using: %u\n",
               disc_units, map_capacity, dm->total_alloc_units);
    }

    uint32_t map_block_0_bits = (sector_size - 64) * 8;
    uint32_t map_block_N_bits = (sector_size - 4) * 8;

    int map_zone = nzones / 2;
    uint64_t map_disc_addr;
    if (map_zone == 0) {
        map_disc_addr = 0;
    } else {
        map_disc_addr = (uint64_t)map_block_0_bits * dm->bytes_per_map_bit
                      + (uint64_t)(map_zone - 1) * map_block_N_bits * dm->bytes_per_map_bit;
    }

    if (verbose) {
        printf("  Map at zone %d, disc address 0x%llX (%.1f MB)\n",
               map_zone, (unsigned long long)map_disc_addr,
               map_disc_addr / (1024.0 * 1024.0));
    }

    /*
     * Read the zone map from the compacted data stream.
     *
     * The trailer's map_offset field gives the offset of zone 0 map block.
     * The map is nzones consecutive sectors starting there.
     * This is typically a few KB to a few hundred KB — never proportional
     * to disc size.
     */
    uint32_t map_file_offset = trailer->map_offset;
    uint32_t map_total_size = sector_size * nzones;
    uint8_t *map_data = malloc(map_total_size);
    if (!map_data) {
        free(dm->alloc_bitmap); free(dm);
        return NULL;
    }

    got = reader_read(reader, map_file_offset, map_data, map_total_size);
    if (got < (int)map_total_size) {
        fprintf(stderr, "Error: could not read zone map at offset 0x%X "
                "(got %d of %u bytes)\n", map_file_offset, got, map_total_size);
        free(map_data); free(dm->alloc_bitmap); free(dm);
        return NULL;
    }

    /* Validate: disc record at +0x04 in map block 0 should match */
    {
        uint8_t *dr_bytes = (uint8_t *)dr;
        uint8_t *c = map_data + 0x04;
        if (c[0] != dr_bytes[0] || c[4] != dr_bytes[4] ||
            c[5] != dr_bytes[5] || c[9] != dr_bytes[9] ||
            memcmp(c + 16, dr_bytes + 16, 4) != 0)
        {
            fprintf(stderr, "Error: zone map at offset 0x%X does not contain "
                    "expected disc record\n", map_file_offset);
            free(map_data); free(dm->alloc_bitmap); free(dm);
            return NULL;
        }
    }

    if (verbose) {
        uint16_t fl = map_data[1] | ((map_data[2] & 0x7F) << 8);  /* mask bit 15 (ZoneValid) */
        printf("  Map block 0 at offset 0x%X (FreeLink=%u)\n",
               map_file_offset, fl);
    }

    /*
     * Walk the map and determine which allocation units are free.
     *
     * For each zone's map block, we walk the allocation bits and decode
     * fragment blocks. A fragment block starts with idlen bits of fragment ID,
     * followed by zero-or-more 0 bits, terminated by a 1 bit.
     *
     * To determine if a fragment is free: we follow the free chain.
     * FreeLink (bytes 1-2 of the map block header, as a 15-bit offset)
     * gives the bit offset to the first free fragment. Each free fragment's
     * ID gives the offset to the next one (0 = end of chain).
     *
     * Strategy: first collect all free fragment bit positions from the chain,
     * then walk all fragments marking non-free ones as allocated.
     *
     * Actually simpler: walk all fragments. For each fragment, read its ID.
     * If the fragment is reachable from the free chain, it's free.
     * But we can simplify: mark everything, then unmark free chain fragments.
     *
     * Simplest approach: 
     * 1. Mark ALL allocation units as allocated
     * 2. Follow the free chain in each zone, unmarking those units as free
     */

    /* Start with all units allocated */
    memset(dm->alloc_bitmap, 0xFF, (dm->total_alloc_units + 7) / 8);
    /* Clear any excess bits beyond total_alloc_units */
    uint32_t excess = (dm->total_alloc_units % 8);
    if (excess)
        dm->alloc_bitmap[dm->total_alloc_units / 8] &= (1u << excess) - 1;

    uint32_t total_free = 0;
    uint32_t global_bit_offset = 0;  /* bit offset into concatenated allocation data */

    for (int z = 0; z < nzones; z++) {
        const uint8_t *block = map_data + (sector_size * z);

        /* Header */
        /* uint8_t zone_check = block[0]; */
        /* FreeLink is a 16-bit little-endian field at bytes 1-2 of the zone
         * header.  Bit 15 is always set (Nick Reeves, "New Disc File Structure
         * for RISC OS", 1990: "offset in bits to first free space in zone, or
         * 0 if none, with top bit always set").  The actual offset is the
         * lower 15 bits.  When the zone has no free space, FreeLink = 0x8000
         * — masking gives 0, which we treat as "no free fragments" below. */
        uint16_t free_link = block[1] | ((block[2] & 0x7F) << 8);
        /* uint8_t cross_check = block[3]; */

        /* Allocation bytes start after header (and disc record for block 0) */
        uint32_t alloc_start_byte = (z == 0) ? 0x40 : 0x04;
        uint32_t alloc_start_bit = alloc_start_byte * 8;

        /* How many allocation bits (units) in this zone.
         * zone_spare bits ARE allocation units — they map to disc space.
         * They just can't start a new fragment. */
        uint32_t zone_alloc_bits;
        if (nzones == 1) {
            /* Single zone disc: all allocation units in this zone */
            zone_alloc_bits = dm->total_alloc_units;
        } else if (z == nzones - 1) {
            /* Last zone: remainder */
            uint32_t used = zone0_bits + (uint32_t)(nzones - 2) * zoneN_bits;
            zone_alloc_bits = (dm->total_alloc_units > used) ?
                              dm->total_alloc_units - used : 0;
        } else if (z == 0) {
            zone_alloc_bits = zone0_bits;
        } else {
            zone_alloc_bits = zoneN_bits;
        }

        if (global_bit_offset + zone_alloc_bits > dm->total_alloc_units)
            zone_alloc_bits = dm->total_alloc_units - global_bit_offset;

        /*
         * Follow the free chain for this zone and walk all fragments.
         *
         * FreeLink (bytes 1-2 of the zone header, 15 bits) gives the
         * bit offset (from bit 8, i.e. byte 1) to the first free
         * fragment in this zone.  Each free fragment's idlen-bit ID
         * encodes the offset from that fragment's start to the next
         * free fragment; an ID of 0 terminates the chain.
         * See RISC OS PRM vol. 2, ch. 28 "FileCore", section
         * "The zone".
         *
         * We walk ALL fragments from the start of the allocation area,
         * marking each as allocated or free.  zone_spare bits at the
         * start of each zone continue a fragment from the previous zone
         * (or hold the system fragment in zone 0) but still represent
         * real allocation units on disc.
         */
        uint32_t free_bit_pos;
        
        if (free_link == 0) {
            free_bit_pos = 0;  /* no free fragments in this zone */
        } else {
            free_bit_pos = 8 + free_link;
        }

        uint32_t bit = alloc_start_bit;
        uint32_t bits_consumed = 0;  /* total bits consumed in this zone */
        uint32_t unit_in_zone = 0;   /* allocation units counted */
        int frag_count = 0;
        int free_frag_count = 0;
        uint32_t zone_total_alloc_bits = (sector_size * 8) - alloc_start_bit;

        while (bits_consumed < zone_total_alloc_bits && unit_in_zone < zone_alloc_bits) {
            /* Read idlen-bit fragment ID.
             *
             * For free fragments (ID encodes offset to next free fragment),
             * only the low 15 bits are meaningful when idlen > 15 — the
             * free chain link is always a 15-bit value even on big map discs
             * (Acorn FileCore Phase 1 spec, §3.2).  This doesn't affect us
             * since we walk all fragments sequentially rather than following
             * the free chain. */
            uint32_t frag_id = read_map_bits(block, bit, idlen);

            /* Find the end of this fragment: scan for the terminating 1 bit
             * after the idlen-bit ID */
            uint32_t frag_start = bit;
            uint32_t scan = bit + idlen;
            while (scan < (alloc_start_bit + zone_total_alloc_bits)) {
                uint32_t byte_idx = scan / 8;
                uint32_t bit_idx = scan % 8;
                if (block[byte_idx] & (1u << bit_idx))
                    break;  /* found terminating 1 */
                scan++;
            }
            /* Fragment length in bits = scan - frag_start + 1 (including the 1 bit) */
            uint32_t frag_len;
            if (scan < (alloc_start_bit + zone_total_alloc_bits))
                frag_len = scan - frag_start + 1;
            else
                frag_len = (alloc_start_bit + zone_total_alloc_bits) - frag_start;

            /* Ensure minimum fragment length */
            if (frag_len < (uint32_t)(idlen + 1))
                frag_len = idlen + 1;

            /* How many allocation units does this fragment represent?
             * All bits in the fragment are allocation units — including
             * any zone_spare bits. zone_spare only means fragments can't
             * START in that region; the bits still map to disc space. */
            uint32_t frag_alloc_units = frag_len;

            /* Clamp to remaining units in zone */
            if (unit_in_zone + frag_alloc_units > zone_alloc_bits)
                frag_alloc_units = zone_alloc_bits - unit_in_zone;

            /* Determine if this fragment is free.
             *
             * Fragment id meanings:
             *   0 = free space (no link to next free fragment)
             *   1 = bad sectors object
             *   2 = system object (boot block + map + root dir)
             *   >=3 = allocated disc objects
             *   Free chain entries have non-zero ids encoding bit offsets
             *
             * A fragment is free if:
             *   - Its id is 0 (standalone free / terminal in chain), OR
             *   - It's reachable from the FreeLink chain
             */
            int is_free = 0;
            if (frag_id == 0) {
                is_free = 1;
            } else if (free_bit_pos != 0 && frag_start == free_bit_pos) {
                is_free = 1;
                /* Advance free chain: frag_id = offset to next free fragment */
                free_bit_pos = frag_start + frag_id;
            }

            if (is_free) {
                /* Mark these units as free (clear bits in bitmap) */
                for (uint32_t i = 0; i < frag_alloc_units; i++) {
                    uint32_t idx = global_bit_offset + unit_in_zone + i;
                    if (idx < dm->total_alloc_units)
                        dm->alloc_bitmap[idx / 8] &= ~(1u << (idx % 8));
                }
                total_free += frag_alloc_units;
                free_frag_count++;
            }

            if (verbose > 1)
                printf("    Z%d frag%d: bit=%u id=%u len=%u alloc_units=%u %s\n",
                       z, frag_count, frag_start, frag_id, frag_len, frag_alloc_units,
                       is_free ? "FREE" : "alloc");
            frag_count++;

            bit += frag_len;
            bits_consumed += frag_len;
            unit_in_zone += frag_alloc_units;
        }

        if (verbose > 1 && (z < 5 || (z >= 57 && z <= 68)))
            printf("  Zone %d: %d frags (%d free), free_link=%u, free_bit_pos=%u, alloc_units=%u\n",
                   z, frag_count, free_frag_count, free_link, free_bit_pos, unit_in_zone);

        if (unit_in_zone != zone_alloc_bits && verbose) {
            printf("  WARNING: Zone %d: unit_in_zone=%u != zone_alloc_bits=%u (diff=%d)\n",
                   z, unit_in_zone, zone_alloc_bits, (int)unit_in_zone - (int)zone_alloc_bits);
        }

        global_bit_offset += zone_alloc_bits;
    }

    free(map_data);

    if (verbose) {
        uint32_t alloc = dm->total_alloc_units - total_free;
        printf("  Alloc units: %u total, %u allocated, %u free\n",
               dm->total_alloc_units, alloc, total_free);
        printf("  Allocated: %.1f MB, Free: %.1f MB\n",
               (double)alloc * dm->bytes_per_map_bit / (1024.0 * 1024.0),
               (double)total_free * dm->bytes_per_map_bit / (1024.0 * 1024.0));
        long expected = (long)alloc * dm->bytes_per_map_bit;
        printf("  Expected data: %ld, compacted stream: %zu\n",
               expected, reader->data_len);
        if (expected != (long)reader->data_len)
            printf("  MISMATCH (diff: %ld bytes)\n",
                   (long)reader->data_len - expected);
    }

    return dm;
}

static void free_disc_map(disc_map_t *dm)
{
    if (dm) {
        free(dm->alloc_bitmap);
        free(dm);
    }
}

/* ======================================================================
 * Helper functions
 * ====================================================================== */

/*
 * Print a hex+ASCII dump of a buffer, 16 bytes per line.
 * base_addr is the address label shown at the start of each line.
 */
static void hex_dump(const uint8_t *buf, size_t len, uint32_t base_addr)
{
    for (size_t i = 0; i < len; i++) {
        if ((i & 0xF) == 0)
            printf("  %04X: ", (unsigned)(base_addr + i));
        printf("%02X ", buf[i]);
        if ((i & 0xF) == 0xF) {
            printf(" |");
            for (size_t j = i - 15; j <= i; j++)
                printf("%c", (buf[j] >= 0x20 && buf[j] < 0x7F)
                       ? buf[j] : '.');
            printf("|\n");
        }
    }
    if (len & 0xF)
        printf("\n");
}

static int read_trailer(FILE *fp, long file_size, fcfs_trailer_t *trailer)
{
    if (file_size < TRAILER_SIZE) {
        fprintf(stderr, "Error: file too small for FCFS trailer\n");
        return -1;
    }

    fseek(fp, file_size - TRAILER_SIZE, SEEK_SET);
    if (fread(trailer, sizeof(*trailer), 1, fp) != 1) {
        perror("fread trailer");
        return -1;
    }

    if (trailer->magic != FCFS_MAGIC) {
        fprintf(stderr, "Error: bad magic 0x%08X (expected 0x%08X)\n",
                trailer->magic, FCFS_MAGIC);
        return -1;
    }

    return 0;
}

static uint32_t *read_offset_table(FILE *fp, long file_size,
                                   const fcfs_trailer_t *trailer,
                                   int *num_blocks_out)
{
    uint32_t table_size = trailer->offset_table_size;
    int num_blocks = (table_size / sizeof(uint32_t)) - 1;

    if (num_blocks <= 0) {
        fprintf(stderr, "Error: invalid offset table\n");
        return NULL;
    }

    fseek(fp, file_size - TRAILER_SIZE - table_size, SEEK_SET);
    uint32_t *table = malloc(table_size);
    if (!table || fread(table, table_size, 1, fp) != 1) {
        free(table);
        return NULL;
    }

    *num_blocks_out = num_blocks;
    return table;
}

static int read_compressed_block(FILE *fp, const uint32_t *offset_table,
                                 int block_num, uint8_t *out_buf)
{
    uint32_t block_start = offset_table[block_num];
    uint32_t block_end   = offset_table[block_num + 1];
    uint32_t comp_size   = block_end - block_start;

    if (comp_size == 0) {
        memset(out_buf, 0, BLOCK_SIZE);
        return BLOCK_SIZE;
    }

    uint8_t *comp_buf = malloc(comp_size);
    if (!comp_buf) return -1;

    fseek(fp, block_start, SEEK_SET);
    if (fread(comp_buf, comp_size, 1, fp) != 1) {
        free(comp_buf);
        return -1;
    }

    int decomp_size;
    switch (comp_buf[0]) {
    case COMP_FLAG_STORED:
        decomp_size = comp_size - 1;
        memcpy(out_buf, comp_buf + 1, decomp_size);
        break;
    case COMP_FLAG_LZ77:
        decomp_size = lz_decompress(comp_buf + 1, comp_size - 1,
                                      out_buf, BLOCK_SIZE);
        break;
    default:
        fprintf(stderr, "Error: unknown compression 0x%02X in block %d\n",
                comp_buf[0], block_num);
        free(comp_buf);
        return -1;
    }

    free(comp_buf);
    return decomp_size;
}

static int is_zero_block(const uint8_t *buf, size_t len)
{
    for (size_t i = 0; i < len; i++)
        if (buf[i] != 0) return 0;
    return 1;
}

/* ======================================================================
 * Compacted-data reader
 * ====================================================================== */

static int reader_init_type1(compacted_reader_t *r, FILE *fp, long file_size)
{
    memset(r, 0, sizeof(*r));
    r->fp = fp;
    r->type = FCFS_TYPE_COMPACTED;
    r->data_len = file_size - TRAILER_SIZE;
    r->cur_block = -1;
    return 0;
}

static int reader_init_type2(compacted_reader_t *r, FILE *fp, long file_size,
                             const fcfs_trailer_t *trailer)
{
    memset(r, 0, sizeof(*r));
    r->fp = fp;
    r->type = FCFS_TYPE_COMPRESSED;
    r->cur_block = -1;

    r->offset_table = read_offset_table(fp, file_size, trailer, &r->num_blocks);
    if (!r->offset_table) return -1;

    r->block_buf = malloc(BLOCK_SIZE);
    if (!r->block_buf) {
        free(r->offset_table);
        return -1;
    }

    /* Total decompressed (compacted) data length = num_blocks * 64KB.
     * The actual useful length may be less (last block may be short),
     * but this is the addressable range. */
    r->data_len = (size_t)r->num_blocks * BLOCK_SIZE;

    return 0;
}

static void reader_free(compacted_reader_t *r)
{
    free(r->offset_table);
    free(r->block_buf);
    memset(r, 0, sizeof(*r));
}

/*
 * Read 'len' bytes from offset 'off' in the compacted data stream.
 * Returns number of bytes actually read, or -1 on error.
 */
static int reader_read(compacted_reader_t *r, size_t off, void *buf, size_t len)
{
    if (off >= r->data_len)
        return 0;
    if (off + len > r->data_len)
        len = r->data_len - off;
    if (len == 0)
        return 0;

    if (r->type == FCFS_TYPE_COMPACTED) {
        /* Type 1: direct file read */
        fseek(r->fp, (long)off, SEEK_SET);
        return (int)fread(buf, 1, len, r->fp);
    }

    /* Type 2: decompress blocks on demand */
    uint8_t *dst = (uint8_t *)buf;
    size_t total = 0;

    while (len > 0) {
        int blk = (int)(off / BLOCK_SIZE);
        size_t blk_off = off % BLOCK_SIZE;

        if (blk >= r->num_blocks)
            break;

        /* Ensure this block is decompressed */
        if (blk != r->cur_block) {
            int dsz = read_compressed_block(r->fp, r->offset_table,
                                            blk, r->block_buf);
            if (dsz < 0) return -1;
            r->cur_block = blk;
            r->cur_block_len = dsz;
        }

        /* Copy from block buffer */
        size_t avail = (blk_off < (size_t)r->cur_block_len) ?
                       (size_t)r->cur_block_len - blk_off : 0;
        size_t chunk = (len < avail) ? len : avail;

        if (chunk > 0) {
            memcpy(dst, r->block_buf + blk_off, chunk);
            dst += chunk;
            off += chunk;
            len -= chunk;
            total += chunk;
        }

        /* If we exhausted this block's data, any remaining bytes in the
         * 64KB logical block are zeros (short last block) */
        if (chunk < len && blk_off + chunk < BLOCK_SIZE) {
            size_t pad = BLOCK_SIZE - (blk_off + chunk);
            if (pad > len) pad = len;
            memset(dst, 0, pad);
            dst += pad;
            off += pad;
            len -= pad;
            total += pad;
        }
    }

    return (int)total;
}

/*
 * Find the FileCore disc record in a buffer of image data.
 *
 * On a hard disc the disc record is at 0xDC0 — within the boot block
 * at disc address 0xC00 (see PRM vol. 2, ch. 28 "FileCore",
 * "The boot block").  On a floppy with nzones=1 the zone 0 map block
 * starts at disc address 0, and the disc record is at +0x04 (after the
 * 4-byte zone header).
 *
 * If the trailer's map_offset is available and falls within the buffer
 * it is tried first, since it gives the offset of zone 0 map block
 * (and the disc record is at map_offset + 4).
 *
 * Returns a pointer into buf, or NULL.
 */
static filecore_disc_record_t *find_disc_record(const uint8_t *buf,
                                                 size_t buflen,
                                                 const fcfs_trailer_t *trailer)
{
    /* Try trailer hint first: disc record at map_offset + 4 */
    uint32_t hint = trailer->map_offset;
    if (hint + 4 + sizeof(filecore_disc_record_t) <= buflen) {
        filecore_disc_record_t *dr =
            (filecore_disc_record_t *)(buf + hint + 4);
        if (dr_looks_valid(dr))
            return dr;
    }

    /* Standard locations (reliable for type 0 raw images; for types 1/2
     * the trailer hint above is the primary method, since these offsets
     * are disc addresses, not compacted-stream offsets):
     *   0x0004 — floppy: zone 0 map header (4 bytes) + disc record
     *   0x0DC0 — hard disc: boot block at disc address 0xC00, disc record
     *            at +0x1C0 within it.  This byte address is fixed regardless
     *            of sector size.
     *            See PRM vol 2, ch. 28, "The boot block". */
    static const uint32_t cands[] = { 0x0004, 0x0DC0 };
    for (int c = 0; c < 2; c++) {
        if (cands[c] + sizeof(filecore_disc_record_t) > buflen)
            continue;
        filecore_disc_record_t *dr =
            (filecore_disc_record_t *)(buf + cands[c]);
        if (dr_looks_valid(dr))
            return dr;
    }

    return NULL;
}

/* ======================================================================
 * Type 0: Raw (strip trailer, copy disc data)
 * ====================================================================== */

static int convert_raw(FILE *fp_in, FILE *fp_out,
                       const fcfs_trailer_t *trailer,
                       long file_size, int sparse)
{
    uint8_t head[0x1000];
    fseek(fp_in, 0, SEEK_SET);
    size_t hread = fread(head, 1, sizeof(head), fp_in);
    filecore_disc_record_t *dr = find_disc_record(head, hread, trailer);

    long data_end = file_size - TRAILER_SIZE;
    uint32_t disc_size = data_end;
    if (dr && dr->disc_size_lo > 0 && dr->disc_size_lo <= (uint32_t)data_end)
        disc_size = dr->disc_size_lo;

    if (verbose)
        printf("Raw image: %u bytes (%.1f MB)\n",
               disc_size, disc_size / (1024.0 * 1024.0));

    fseek(fp_in, 0, SEEK_SET);
    uint8_t buf[BLOCK_SIZE];
    uint32_t remaining = disc_size;

    while (remaining > 0) {
        size_t chunk = remaining > BLOCK_SIZE ? BLOCK_SIZE : remaining;
        size_t got = fread(buf, 1, chunk, fp_in);
        if (got == 0) break;

        if (sparse && is_zero_block(buf, got))
            fseek(fp_out, got, SEEK_CUR);
        else
            fwrite(buf, got, 1, fp_out);

        remaining -= got;
    }

    /* Ensure correct file size */
    fseek(fp_out, disc_size - 1, SEEK_SET);
    uint8_t z = 0;
    fwrite(&z, 1, 1, fp_out);

    return 0;
}

/* ======================================================================
 * Type 1: Compacted (allocated sectors only, free sectors omitted)
 *
 * The compacted file stores allocated allocation-units packed
 * sequentially in disc address order. Free units are simply absent.
 * To reconstruct:
 *   - Walk allocation units 0..N in order
 *   - If allocated: read bytes_per_map_bit bytes from file, write to output
 *   - If free: write zeros (or seek past for sparse)
 * ====================================================================== */

static int expand_compacted(compacted_reader_t *reader, FILE *fp_out,
                            const fcfs_trailer_t *trailer, int sparse)
{
    disc_map_t *dm = build_disc_map(reader, trailer);
    if (!dm) return -1;

    uint32_t bpmb = dm->bytes_per_map_bit;
    uint32_t total_units = dm->total_alloc_units;
    uint32_t disc_size = dm->disc_size;

    uint32_t alloc_count = 0;
    for (uint32_t i = 0; i < total_units; i++) {
        if (dm->alloc_bitmap[i / 8] & (1u << (i % 8)))
            alloc_count++;
    }

    if (verbose) {
        printf("Expanding compacted data:\n");
        printf("  Disc size: %u bytes (%.1f MB)\n",
               disc_size, disc_size / (1024.0 * 1024.0));
        printf("  Allocation unit: %u bytes, %u allocated, %u free\n",
               bpmb, alloc_count, total_units - alloc_count);
    }

    /*
     * Stream through allocation units. Read sequentially from the
     * compacted data via the reader, writing each unit at its correct
     * disc address.
     */
    uint8_t *unit_buf = malloc(bpmb);
    if (!unit_buf) {
        free_disc_map(dm);
        return -1;
    }

    static const uint8_t zeros[4096] = {0};
    size_t read_pos = 0;
    uint32_t units_alloc = 0, units_free = 0;

    for (uint32_t u = 0; u < total_units; u++) {
        int is_alloc = (dm->alloc_bitmap[u / 8] >> (u % 8)) & 1;
        long disc_offset = (long)u * bpmb;

        if (is_alloc) {
            int n = reader_read(reader, read_pos, unit_buf, bpmb);
            if (n < (int)bpmb)
                memset(unit_buf + (n > 0 ? n : 0), 0,
                       bpmb - (size_t)(n > 0 ? n : 0));

            fseek(fp_out, disc_offset, SEEK_SET);
            fwrite(unit_buf, bpmb, 1, fp_out);

            read_pos += bpmb;
            units_alloc++;
        } else {
            if (!sparse) {
                fseek(fp_out, disc_offset, SEEK_SET);
                size_t rem = bpmb;
                while (rem > 0) {
                    size_t chunk = rem < sizeof(zeros) ? rem : sizeof(zeros);
                    fwrite(zeros, chunk, 1, fp_out);
                    rem -= chunk;
                }
            }
            units_free++;
        }

        if (verbose && ((u + 1) % 10000 == 0 || u == total_units - 1)) {
            printf("\r  Unit %u/%u (%.1f%%)...",
                   u + 1, total_units,
                   100.0 * (u + 1) / total_units);
            fflush(stdout);
        }
    }

    /* Ensure output is exactly disc_size bytes */
    fseek(fp_out, disc_size - 1, SEEK_SET);
    uint8_t z = 0;
    fwrite(&z, 1, 1, fp_out);

    if (verbose) {
        printf("\n  Written: %u allocated units (%.1f MB)\n",
               units_alloc,
               (double)units_alloc * bpmb / (1024.0 * 1024.0));
        printf("  %s: %u free units (%.1f MB)\n",
               sparse ? "Skipped" : "Zeroed",
               units_free,
               (double)units_free * bpmb / (1024.0 * 1024.0));
        printf("  Output: %u bytes (%.1f MB)\n",
               disc_size, disc_size / (1024.0 * 1024.0));
    }

    free(unit_buf);
    free_disc_map(dm);
    return 0;
}

/* ======================================================================
 * Type 1 (compacted) and Type 2 (block-compressed) conversion
 *
 * Both types store the same compacted data: only allocated sectors,
 * packed sequentially. Type 1 stores it verbatim; type 2 compresses
 * it in 64 KB LZ77 blocks.
 *
 * Both paths initialise a compacted_reader_t (which streams data on
 * demand) and call expand_compacted() to reconstruct the full disc.
 * ====================================================================== */

static int convert_compacted(FILE *fp_in, FILE *fp_out,
                             const fcfs_trailer_t *trailer,
                             long file_size, int sparse)
{
    compacted_reader_t reader;
    if (reader_init_type1(&reader, fp_in, file_size) != 0)
        return -1;

    int ret = expand_compacted(&reader, fp_out, trailer, sparse);
    reader_free(&reader);
    return ret;
}

static int convert_compressed(FILE *fp_in, FILE *fp_out,
                              const fcfs_trailer_t *trailer,
                              long file_size, int sparse)
{
    compacted_reader_t reader;
    if (reader_init_type2(&reader, fp_in, file_size, trailer) != 0)
        return -1;

    if (verbose)
        printf("Block-compressed: %d blocks\n", reader.num_blocks);

    int ret = expand_compacted(&reader, fp_out, trailer, sparse);
    reader_free(&reader);
    return ret;
}

/* ======================================================================
 * Info display
 * ====================================================================== */

static void show_info(FILE *fp, const fcfs_trailer_t *trailer, long file_size)
{
    const char *type_names[] = {
        "Raw (full disc copy)",
        "Compacted (allocated sectors only)",
        "Block-compressed (LZ77, 64KB blocks)"
    };

    printf("FCFS image:\n");
    printf("  File size: %ld bytes (%.1f MB)\n",
           file_size, file_size / (1024.0 * 1024.0));
    printf("  Type: %u - %s\n", trailer->type,
           trailer->type <= 2 ? type_names[trailer->type] : "Unknown");
    printf("  Zone 0 map offset: 0x%X\n", trailer->map_offset);
    printf("  Offset table size: %u\n", trailer->offset_table_size);

    /* Hex dump the 256-byte trailer */
    printf("\nTrailer (last 0x100 bytes of file):\n");
    uint8_t trail_buf[TRAILER_SIZE];
    fseek(fp, file_size - TRAILER_SIZE, SEEK_SET);
    if (fread(trail_buf, 1, TRAILER_SIZE, fp) == TRAILER_SIZE)
        hex_dump(trail_buf, TRAILER_SIZE, 0);

    /* Hex dump the first 0x100 bytes of the file */
    printf("\nFile start (first 0x100 bytes):\n");
    uint8_t start_buf[0x100];
    fseek(fp, 0, SEEK_SET);
    size_t got = fread(start_buf, 1, sizeof(start_buf), fp);
    hex_dump(start_buf, got, 0);

    /* Search for disc record at various locations.
     * For type 2, we must decompress before searching. */
    printf("\nDisc record search:\n");
    uint8_t big_buf[2 * BLOCK_SIZE]; /* 128KB for first 2 decompressed blocks */
    size_t big_read = 0;

    if (trailer->type == FCFS_TYPE_COMPRESSED) {
        /* Decompress first 2 blocks to get disc addresses 0x00000–0x1FFFF */
        int nb;
        uint32_t *ot2 = read_offset_table(fp, file_size, trailer, &nb);
        if (ot2) {
            int hb = nb < 2 ? nb : 2;
            memset(big_buf, 0, sizeof(big_buf));
            for (int i = 0; i < hb; i++) {
                int dsz = read_compressed_block(fp, ot2, i, big_buf + i * BLOCK_SIZE);
                if (dsz > 0)
                    big_read += dsz;
            }
            free(ot2);
            printf("  (searching decompressed disc data)\n");
        }
    } else {
        fseek(fp, 0, SEEK_SET);
        big_read = fread(big_buf, 1, sizeof(big_buf), fp);
    }

    /* Search every 4-byte aligned offset for something that looks like
     * a valid disc record */
    int found = 0;
    for (uint32_t off = 0; off + sizeof(filecore_disc_record_t) <= big_read; off += 4) {
        filecore_disc_record_t *test =
            (filecore_disc_record_t *)(big_buf + off);
        if (dr_looks_valid(test)) {
            int nz = test->nzones;
            printf("  Candidate at disc address 0x%04X:\n", off);
            printf("    log2_sector: %u (%u bytes), log2_bpmb: %u (%u bytes)\n",
                   test->log2_sector_size, 1u << test->log2_sector_size,
                   test->log2_bpmb, 1u << test->log2_bpmb);
            printf("    sectors/track: %u, heads: %u, nzones: %d\n",
                   test->sectors_per_track, test->heads, nz);
            printf("    zone_spare: %u, disc_size: %u (%.1f MB)\n",
                   test->zone_spare, test->disc_size_lo,
                   test->disc_size_lo / (1024.0 * 1024.0));

            /* Hex context */
            printf("    Hex: ");
            for (int i = 0; i < 32 && off + i < big_read; i++)
                printf("%02X ", big_buf[off + i]);
            printf("\n");
            found++;
            if (found >= 5) break;
        }
    }
    if (!found)
        printf("  No valid disc record found in first %zuKB\n", big_read / 1024);

    /* Hex dump at 0xC00 (FileCore boot block on hard discs) */
    if (big_read > 0xE00) {
        printf("\nBoot block area (disc address 0xC00, 0x200 bytes):\n");
        hex_dump(big_buf + 0xC00, 0x200, 0xC00);
    }

    /* Find disc record using the same logic as conversion */
    filecore_disc_record_t *dr = NULL;
    filecore_disc_record_t dr_copy;
    
    if (big_read > 0) {
        filecore_disc_record_t *found = find_disc_record(big_buf, big_read,
                                                          trailer);
        if (found) {
            memcpy(&dr_copy, found, sizeof(dr_copy));
            dr = &dr_copy;
        }
    }
    if (!dr) {
        /* For types 0/1 where big_buf was read from raw file, try again
         * with a larger read if the first attempt was too small */
        uint8_t rawhead[0x1000];
        fseek(fp, 0, SEEK_SET);
        size_t n = fread(rawhead, 1, sizeof(rawhead), fp);
        filecore_disc_record_t *found = find_disc_record(rawhead, n, trailer);
        if (found) {
            memcpy(&dr_copy, found, sizeof(dr_copy));
            dr = &dr_copy;
        }
    }
    if (dr) {
        int nz = dr->nzones;
        uint32_t sector_size = 1u << dr->log2_sector_size;
        uint32_t bpmb = 1u << dr->log2_bpmb;

        printf("FileCore disc record:\n");
        printf("  Sector size: %u bytes (log2=%u)\n",
               sector_size, dr->log2_sector_size);
        printf("  Bytes per map bit: %u (log2=%u)\n", bpmb, dr->log2_bpmb);
        printf("  Sectors per track: %u\n", dr->sectors_per_track);
        printf("  Heads: %u\n", dr->heads);
        printf("  Zones: %d\n", nz);
        printf("  Zone spare: %u bits\n", dr->zone_spare);
        printf("  Disc size (from record): %u bytes (%.1f MB)\n",
               dr->disc_size_lo, dr->disc_size_lo / (1024.0 * 1024.0));

        /*
         * Cross-check: compute disc size from zone geometry.
         *
         * Zone 0 header: 64 bytes (4-byte check + 60-byte disc record)
         * Zone N header: 4 bytes (check only)
         *
         * zone_spare bits at the start of each zone's allocation area
         * ARE allocation units (they continue a fragment from the
         * previous zone). They must be counted.
         */
        uint32_t zone0_hdr = 0x40; /* standard: 4-byte check + 60-byte disc record */
        uint32_t zone0_bits = (sector_size - zone0_hdr) * 8;
        uint32_t zoneN_bits = (sector_size - 0x04) * 8;
        uint64_t max_capacity;
        if (nz == 1) {
            max_capacity = (uint64_t)zone0_bits * bpmb;
        } else {
            max_capacity = (uint64_t)zone0_bits * bpmb +
                           (uint64_t)(nz - 1) * zoneN_bits * bpmb;
        }
        uint64_t disc_sz = dr->disc_size_lo;
        printf("  Map capacity (from geometry): %llu bytes (%.1f MB)\n",
               (unsigned long long)max_capacity,
               max_capacity / (1024.0 * 1024.0));
        printf("  Bits per zone: zone0=%u, zoneN=%u (incl. %u zone_spare)\n",
               zone0_bits, zoneN_bits, dr->zone_spare);
        printf("  Total allocation units: %u (disc), %llu (map)\n",
               dr->disc_size_lo / bpmb,
               (unsigned long long)(max_capacity / bpmb));

        if (disc_sz > max_capacity) {
            printf("  WARNING: disc_size exceeds zone map capacity!\n");
        }
    }

    if (trailer->type == FCFS_TYPE_COMPACTED) {
        printf("Compaction details:\n");
        long data_size = file_size - TRAILER_SIZE;
        compacted_reader_t reader;
        if (reader_init_type1(&reader, fp, file_size) == 0) {
            disc_map_t *dm = build_disc_map(&reader, trailer);
            if (dm) {
                uint32_t alloc = 0;
                for (uint32_t i = 0; i < dm->total_alloc_units; i++)
                    if (dm->alloc_bitmap[i / 8] & (1u << (i % 8)))
                        alloc++;
                long expected = (long)alloc * dm->bytes_per_map_bit;
                printf("  File data region: %ld bytes (%.1f MB)\n",
                       data_size, data_size / (1024.0 * 1024.0));
                printf("  Allocated units: %u -> expected %ld bytes\n",
                       alloc, expected);
                if (expected != data_size)
                    printf("  MISMATCH: file data %ld != expected %ld "
                           "(diff %ld bytes)\n",
                           data_size, expected, data_size - expected);
                else
                    printf("  Cross-check: OK\n");
                printf("  Compaction: %.1f%% of full disc\n",
                       100.0 * data_size / dm->disc_size);
                free_disc_map(dm);
            }
            reader_free(&reader);
        }
    }

    if (trailer->type == FCFS_TYPE_COMPRESSED) {
        int num_blocks;
        uint32_t *ot = read_offset_table(fp, file_size, trailer, &num_blocks);
        if (ot) {
            int stored = 0, compressed = 0, empty = 0;
            for (int i = 0; i < num_blocks; i++) {
                if (ot[i + 1] <= ot[i]) { empty++; continue; }
                uint8_t flag;
                fseek(fp, ot[i], SEEK_SET);
                if (fread(&flag, 1, 1, fp) == 1) {
                    if (flag == COMP_FLAG_STORED) stored++;
                    else compressed++;
                }
            }
            printf("Compression details:\n");
            printf("  Blocks: %d (%d stored, %d compressed, %d empty)\n",
                   num_blocks, stored, compressed, empty);
            printf("  Logical size: %.1f MB\n",
                   (double)num_blocks * BLOCK_SIZE / (1024.0 * 1024.0));
            free(ot);
        }
    }
}

/* ======================================================================
 * Main
 * ====================================================================== */

static void usage(const char *progname)
{
    fprintf(stderr,
        "fcfs2raw - Convert FCFS disc images to raw sector images\n\n"
        "Usage: %s [-s] [-v] [-i] input.fcfs [output.raw]\n\n"
        "Options:\n"
        "  -s    Create sparse output (holes for free/zero blocks)\n"
        "  -v    Verbose output\n"
        "  -i    Info only\n\n"
        "Types:\n"
        "  0: Raw         - full disc copy, strip trailer\n"
        "  1: Compacted   - allocated sectors only, uses zone map\n"
        "  2: Compressed  - LZ77 block-compressed with offset table\n",
        progname);
}

int main(int argc, char **argv)
{
    int sparse = 0, info_only = 0;
    const char *input_path = NULL, *output_path = NULL;

    for (int i = 1; i < argc; i++) {
        if (argv[i][0] == '-') {
            for (int j = 1; argv[i][j]; j++) {
                switch (argv[i][j]) {
                case 's': sparse = 1; break;
                case 'v': verbose = 1; break;
                case 'i': info_only = 1; verbose = 1; break;
                default:
                    fprintf(stderr, "Unknown option: -%c\n", argv[i][j]);
                    usage(argv[0]);
                    return 1;
                }
            }
        } else if (!input_path) input_path = argv[i];
        else if (!output_path) output_path = argv[i];
        else { usage(argv[0]); return 1; }
    }

    if (!input_path) { usage(argv[0]); return 1; }

    char out_buf[512];
    if (!output_path && !info_only) {
        snprintf(out_buf, sizeof(out_buf), "%s", input_path);
        char *dot = strrchr(out_buf, '.');
        if (dot) strcpy(dot, ".raw");
        else strcat(out_buf, ".raw");
        output_path = out_buf;
    }

    FILE *fp_in = fopen(input_path, "rb");
    if (!fp_in) { perror(input_path); return 1; }

    fseek(fp_in, 0, SEEK_END);
    long file_size = ftell(fp_in);

    fcfs_trailer_t trailer;
    if (read_trailer(fp_in, file_size, &trailer) != 0) {
        fclose(fp_in);
        return 1;
    }

    if (info_only) {
        show_info(fp_in, &trailer, file_size);
        fclose(fp_in);
        return 0;
    }

    const char *tnames[] = { "Raw", "Compacted", "Compressed" };
    printf("FCFS: %s (type %u: %s)\n", input_path, trailer.type,
           trailer.type <= 2 ? tnames[trailer.type] : "?");
    printf("  -> %s%s\n", output_path, sparse ? " (sparse)" : "");

    FILE *fp_out = fopen(output_path, "wb");
    if (!fp_out) { perror(output_path); fclose(fp_in); return 1; }

    int ret;
    switch (trailer.type) {
    case FCFS_TYPE_RAW:
        ret = convert_raw(fp_in, fp_out, &trailer, file_size, sparse);
        break;
    case FCFS_TYPE_COMPACTED:
        ret = convert_compacted(fp_in, fp_out, &trailer, file_size, sparse);
        break;
    case FCFS_TYPE_COMPRESSED:
        ret = convert_compressed(fp_in, fp_out, &trailer, file_size, sparse);
        break;
    default:
        fprintf(stderr, "Unsupported type %u\n", trailer.type);
        ret = -1;
    }

    fclose(fp_out);
    fclose(fp_in);

    printf(ret == 0 ? "Done.\n" : "Failed.\n");
    return ret ? 1 : 0;
}
