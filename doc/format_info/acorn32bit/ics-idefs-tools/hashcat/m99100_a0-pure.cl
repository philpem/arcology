/**
 * m99100_a0-pure.cl — IDEFS hash, attack mode 0 (wordlist + rules)
 *
 * Uses inline digest comparison to avoid COMPARE_S/M macro
 * compatibility issues across hashcat versions.
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

#define IDEFS_KEY 0x01810284u

KERNEL_FQ void m99100_mxx (KERN_ATTR_RULES ())
{
  const u64 gid = get_global_id (0);

  if (gid >= GID_CNT) return;

  COPY_PW (pws[gid]);

  for (u32 il_pos = 0; il_pos < IL_CNT; il_pos += VECT_SIZE)
  {
    pw_t tmp = PASTE_PW;

    tmp.pw_len = apply_rules (rules_buf[il_pos].cmds, tmp.i, tmp.pw_len);

    const u32 pw_len = tmp.pw_len;

    u32 lo = 0;
    u32 hi = 0;
    int started = 0;
    int count = 0;

    for (u32 i = 0; i < pw_len && count < 10; i++)
    {
      const u32 c = (tmp.i[i / 4] >> ((i & 3) * 8)) & 0xFF;

      if (!started) { if (c == 0x20) continue; started = 1; }
      if (c <= 0x20) break;

      const u32 ch = (c - 0x2A) & 0xFF;
      const u32 top6 = lo & 0xFC000000u;

      hi = top6 ^ hc_rotr32_S (hi, 26) ^ IDEFS_KEY;
      lo = ch   ^ (lo << 6)             ^ IDEFS_KEY;

      count++;
    }

    for (u32 d = 0; d < DIGESTS_CNT; d++)
    {
      const u32 d_idx = DIGESTS_OFFSET_HOST + d;

      if (lo != digests_buf[d_idx].digest_buf[DGST_R0]) continue;
      if (hi != digests_buf[d_idx].digest_buf[DGST_R1]) continue;

      if (hc_atomic_inc (&hashes_shown[d_idx]) == 0)
      {
        mark_hash (plains_buf, d_return_buf, SALT_POS_HOST, DIGESTS_CNT, d, d_idx, gid, il_pos, 0, 0);
      }
    }
  }
}

KERNEL_FQ void m99100_sxx (KERN_ATTR_RULES ())
{
  const u64 gid = get_global_id (0);

  if (gid >= GID_CNT) return;

  const u32 ref_lo = digests_buf[DIGESTS_OFFSET_HOST].digest_buf[DGST_R0];
  const u32 ref_hi = digests_buf[DIGESTS_OFFSET_HOST].digest_buf[DGST_R1];

  COPY_PW (pws[gid]);

  for (u32 il_pos = 0; il_pos < IL_CNT; il_pos += VECT_SIZE)
  {
    pw_t tmp = PASTE_PW;

    tmp.pw_len = apply_rules (rules_buf[il_pos].cmds, tmp.i, tmp.pw_len);

    const u32 pw_len = tmp.pw_len;

    u32 lo = 0;
    u32 hi = 0;
    int started = 0;
    int count = 0;

    for (u32 i = 0; i < pw_len && count < 10; i++)
    {
      const u32 c = (tmp.i[i / 4] >> ((i & 3) * 8)) & 0xFF;

      if (!started) { if (c == 0x20) continue; started = 1; }
      if (c <= 0x20) break;

      const u32 ch = (c - 0x2A) & 0xFF;
      const u32 top6 = lo & 0xFC000000u;

      hi = top6 ^ hc_rotr32_S (hi, 26) ^ IDEFS_KEY;
      lo = ch   ^ (lo << 6)             ^ IDEFS_KEY;

      count++;
    }

    if (lo != ref_lo) continue;
    if (hi != ref_hi) continue;

    if (hc_atomic_inc (&hashes_shown[DIGESTS_OFFSET_HOST]) == 0)
    {
      mark_hash (plains_buf, d_return_buf, SALT_POS_HOST, DIGESTS_CNT, 0, DIGESTS_OFFSET_HOST, gid, il_pos, 0, 0);
    }
  }
}
