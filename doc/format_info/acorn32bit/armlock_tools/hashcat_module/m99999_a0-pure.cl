/**
 * Author......: ARMlock reverse engineering project
 * License.....: MIT
 * Mode 99999.: ARMlock Password Hash — straight attack (a0)
 */

#ifdef KERNEL_STATIC
#include M2S(INCLUDE_PATH/inc_vendor.h)
#include M2S(INCLUDE_PATH/inc_types.h)
#include M2S(INCLUDE_PATH/inc_platform.cl)
#include M2S(INCLUDE_PATH/inc_common.cl)
#include M2S(INCLUDE_PATH/inc_rp.h)
#include M2S(INCLUDE_PATH/inc_rp.cl)
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

    /* ASR 13: arithmetic shift right preserving sign bit */
    const u32 asr = (v3 >> 13) | ((v3 & 0x80000000u) ? 0xFFF80000u : 0u);
    const u32 rotated = asr | (v3 << 19);

    v3 = rotated + c;
    v4 ^= v3;
  }

  *out_v3 = v3;
  *out_v4 = v4;
}

KERNEL_FQ void m99999_mxx (KERN_ATTR_RULES ())
{
  const u64 gid = get_global_id (0);

  if (gid >= GID_CNT) return;

  COPY_PW (pws[gid]);

  for (u32 il_pos = 0; il_pos < IL_CNT; il_pos++)
  {
    pw_t tmp = PASTE_PW;

    tmp.pw_len = apply_rules (rules_buf[il_pos].cmds, tmp.i, tmp.pw_len);

    u32 v3, v4;
    armlock_hash (tmp.i, tmp.pw_len, &v3, &v4);

    COMPARE_M_SCALAR (v3, v4, 0, 0);
  }
}

KERNEL_FQ void m99999_sxx (KERN_ATTR_RULES ())
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

  COPY_PW (pws[gid]);

  for (u32 il_pos = 0; il_pos < IL_CNT; il_pos++)
  {
    pw_t tmp = PASTE_PW;

    tmp.pw_len = apply_rules (rules_buf[il_pos].cmds, tmp.i, tmp.pw_len);

    u32 v3, v4;
    armlock_hash (tmp.i, tmp.pw_len, &v3, &v4);

    COMPARE_S_SCALAR (v3, v4, 0, 0);
  }
}
