/**
 * Author......: ARMlock reverse engineering project
 * License.....: MIT
 * Mode 99999.: ARMlock Password Hash — combination attack (a1)
 */

#ifdef KERNEL_STATIC
#include M2S(INCLUDE_PATH/inc_vendor.h)
#include M2S(INCLUDE_PATH/inc_types.h)
#include M2S(INCLUDE_PATH/inc_platform.cl)
#include M2S(INCLUDE_PATH/inc_common.cl)
#include M2S(INCLUDE_PATH/inc_scalar.cl)
#endif

DECLSPEC u32 armlock_toupper (const u32 c)
{
  return ((c >= 0x61) && (c <= 0x7a)) ? (c - 0x20) : c;
}

DECLSPEC void armlock_hash (PRIVATE_AS const u32 *w, const u32 pw_len, PRIVATE_AS u32 *out_v3, PRIVATE_AS u32 *out_v4)
{
  u32 v3 = 0x89ABCDEF;
  u32 v4 = 0x01234567;

  for (u32 i = 0; i < pw_len; i++)
  {
    const u32 c = armlock_toupper ((w[i / 4] >> ((i & 3) * 8)) & 0xFF);

    const u32 asr = (v3 >> 13) | ((v3 & 0x80000000u) ? 0xFFF80000u : 0u);
    const u32 rotated = asr | (v3 << 19);

    v3 = rotated + c;
    v4 ^= v3;
  }

  *out_v3 = v3;
  *out_v4 = v4;
}

KERNEL_FQ void m99999_mxx (KERN_ATTR_BASIC ())
{
  const u64 gid = get_global_id (0);

  if (gid >= GID_CNT) return;

  const u32 pw_len = pws[gid].pw_len;

  u32 w[64] = { 0 };

  for (u32 i = 0, idx = 0; i < pw_len; i += 4, idx++)
  {
    w[idx] = pws[gid].i[idx];
  }

  for (u32 il_pos = 0; il_pos < IL_CNT; il_pos++)
  {
    const u32 comb_len = combs_buf[il_pos].pw_len;
    const u32 total_len = pw_len + comb_len;

    if (total_len > 11) continue;

    u32 wc[64];

    for (u32 i = 0; i < 64; i++) wc[i] = w[i];

    /* Append combination word */
    switch_buffer_by_offset_le_S (combs_buf[il_pos].i, pw_len);

    for (u32 i = 0; i < 64; i++) wc[i] |= combs_buf[il_pos].i[i];

    u32 v3, v4;
    armlock_hash (wc, total_len, &v3, &v4);

    COMPARE_M_SCALAR (v3, v4, 0, 0);
  }
}

KERNEL_FQ void m99999_sxx (KERN_ATTR_BASIC ())
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

  u32 w[64] = { 0 };

  for (u32 i = 0, idx = 0; i < pw_len; i += 4, idx++)
  {
    w[idx] = pws[gid].i[idx];
  }

  for (u32 il_pos = 0; il_pos < IL_CNT; il_pos++)
  {
    const u32 comb_len = combs_buf[il_pos].pw_len;
    const u32 total_len = pw_len + comb_len;

    if (total_len > 11) continue;

    u32 wc[64];

    for (u32 i = 0; i < 64; i++) wc[i] = w[i];

    switch_buffer_by_offset_le_S (combs_buf[il_pos].i, pw_len);

    for (u32 i = 0; i < 64; i++) wc[i] |= combs_buf[il_pos].i[i];

    u32 v3, v4;
    armlock_hash (wc, total_len, &v3, &v4);

    COMPARE_S_SCALAR (v3, v4, 0, 0);
  }
}
