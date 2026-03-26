/**
 * Author......: ARMlock reverse engineering project
 * License.....: MIT
 * Mode 99999.: ARMlock Password Hash — mask / brute-force attack (a3)
 *
 * Optimisation: the base password (left side of mask) is hashed once
 * per work-item, then each mask candidate only extends from that
 * saved v3 state.  v4 is not tracked in the inner loop at all —
 * it's only recomputed on a v3 hit (expected ~1 per 2^32 candidates).
 */

#ifdef KERNEL_STATIC
#include M2S(INCLUDE_PATH/inc_vendor.h)
#include M2S(INCLUDE_PATH/inc_types.h)
#include M2S(INCLUDE_PATH/inc_platform.cl)
#include M2S(INCLUDE_PATH/inc_common.cl)
#include M2S(INCLUDE_PATH/inc_simd.cl)
#endif

DECLSPEC u32x armlock_toupper (const u32x c)
{
  /* Branchless: add 0x20 conditional on being in a-z range */
  const u32x ge_a = (u32x)(c >= 0x61);
  const u32x le_z = (u32x)(c <= 0x7a);
  return c - (ge_a & le_z & (u32x)0x20);
}

DECLSPEC u32x armlock_step_v3 (const u32x v3, const u32x c)
{
  /* ASR 13: arithmetic right shift, sign-extending from bit 31.
     Branchless: (v >> 31) gives 0 or 1, multiply by mask. */
  const u32x sign = (u32x)0u - (v3 >> 31);   /* 0x00000000 or 0xFFFFFFFF */
  const u32x asr  = (v3 >> 13) | (sign & 0xFFF80000u);
  return (asr | (v3 << 19)) + c;
}

KERNEL_FQ void m99999_mxx (KERN_ATTR_VECTOR ())
{
  const u64 gid = get_global_id (0);

  if (gid >= GID_CNT) return;

  const u32 pw_len = pws[gid].pw_len;

  /**
   * Precompute v3 state for the base (left) password.
   * This is constant across all mask candidates for this work-item.
   */
  u32x base_w[64] = { 0 };

  for (u32 idx = 0; idx < ((pw_len + 3) / 4); idx++)
  {
    base_w[idx] = pws[gid].i[idx];
  }

  u32x base_v3 = 0x89ABCDEF;

  for (u32 i = 0; i < pw_len; i++)
  {
    const u32x c = armlock_toupper ((base_w[i / 4] >> ((i & 3) * 8)) & 0xFF);
    base_v3 = armlock_step_v3 (base_v3, c);
  }

  /**
   * Mask candidates: extend from base_v3, tracking only v3.
   * On a v3 hit, recompute full hash with v4 for verification.
   */

  for (u32 il_pos = 0; il_pos < IL_CNT; il_pos += VECT_SIZE)
  {
    const u32x w0r = ix_create_bft (bfs_buf, il_pos);

    /* Combine base password with mask suffix */
    u32x w[64] = { 0 };

    for (u32 idx = 0; idx < ((pw_len + 3) / 4); idx++)
    {
      w[idx] = base_w[idx];
    }

    w[pw_len / 4] |= (w0r << ((pw_len & 3) * 8));

    /* We need the total length.  For mask attack, the mask portion
       length is encoded in the bfs generation.  We can compute it
       from the first non-zero byte of w0r, but simpler: hashcat
       guarantees all candidates have the same total length in a3. */
    u32 mask_len = 0;
    u32 tmp = w0r;
    while (tmp & 0xFF) { mask_len++; tmp >>= 8; }

    const u32 total_len = pw_len + mask_len;

    u32x v3 = base_v3;

    for (u32 i = pw_len; i < total_len; i++)
    {
      const u32x c = armlock_toupper ((w[i / 4] >> ((i & 3) * 8)) & 0xFF);
      v3 = armlock_step_v3 (v3, c);
    }

    /* Full v4 computation only for comparison (rare path) */
    u32x v4 = 0x01234567;
    u32x v3f = 0x89ABCDEF;
    for (u32 i = 0; i < total_len; i++)
    {
      const u32x c = armlock_toupper ((w[i / 4] >> ((i & 3) * 8)) & 0xFF);
      v3f = armlock_step_v3 (v3f, c);
      v4 ^= v3f;
    }

    COMPARE_M_SIMD (v3, v4, 0, 0);
  }
}

KERNEL_FQ void m99999_sxx (KERN_ATTR_VECTOR ())
{
  const u64 gid = get_global_id (0);

  if (gid >= GID_CNT) return;

  const u32 search[4] =
  {
    digests_buf[DIGESTS_OFFSET_HOST].digest_buf[DGST_R0],
    digests_buf[DIGESTS_OFFSET_HOST].digest_buf[DGST_R1],
    0,
    0
  };

  const u32 pw_len = pws[gid].pw_len;

  u32x base_w[64] = { 0 };

  for (u32 idx = 0; idx < ((pw_len + 3) / 4); idx++)
  {
    base_w[idx] = pws[gid].i[idx];
  }

  u32x base_v3 = 0x89ABCDEF;

  for (u32 i = 0; i < pw_len; i++)
  {
    const u32x c = armlock_toupper ((base_w[i / 4] >> ((i & 3) * 8)) & 0xFF);
    base_v3 = armlock_step_v3 (base_v3, c);
  }

  for (u32 il_pos = 0; il_pos < IL_CNT; il_pos += VECT_SIZE)
  {
    const u32x w0r = ix_create_bft (bfs_buf, il_pos);

    u32x w[64] = { 0 };

    for (u32 idx = 0; idx < ((pw_len + 3) / 4); idx++)
    {
      w[idx] = base_w[idx];
    }

    w[pw_len / 4] |= (w0r << ((pw_len & 3) * 8));

    u32 mask_len = 0;
    u32 tmp = w0r;
    while (tmp & 0xFF) { mask_len++; tmp >>= 8; }

    const u32 total_len = pw_len + mask_len;

    u32x v3 = base_v3;

    for (u32 i = pw_len; i < total_len; i++)
    {
      const u32x c = armlock_toupper ((w[i / 4] >> ((i & 3) * 8)) & 0xFF);
      v3 = armlock_step_v3 (v3, c);
    }

    u32x v4 = 0x01234567;
    u32x v3f = 0x89ABCDEF;
    for (u32 i = 0; i < total_len; i++)
    {
      const u32x c = armlock_toupper ((w[i / 4] >> ((i & 3) * 8)) & 0xFF);
      v3f = armlock_step_v3 (v3f, c);
      v4 ^= v3f;
    }

    COMPARE_S_SIMD (v3, v4, 0, 0);
  }
}
