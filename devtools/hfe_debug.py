#!/usr/bin/env python3
"""
HFE format debug tool — inspect and analyse an HFE floppy image offline.

Usage:
    python3 devtools/hfe_debug.py [options] image.hfe

Options:
    -v / --verbose       Enable DEBUG logging from the HFE library (shows
                         every sync word, IDAM, and DAM found)
    -t N / --track N     Also print a detailed sector listing for track N
    --no-protection      Skip copy-protection analysis
    --no-mastering       Skip mastering-data analysis

Run from the repository root.  No worker stack or database required.
"""

import argparse
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging must be configured BEFORE any arcworker import that might also
# call logging.basicConfig (a second call is a no-op, so we go first).
# ---------------------------------------------------------------------------
logging.basicConfig(format='%(levelname)s %(name)s: %(message)s',
                    level=logging.WARNING)

# ---------------------------------------------------------------------------
# Import hfe.py directly, without pulling in the full arcworker package
# (which has Docker-oriented deps like requests, psycopg2, etc.)
# ---------------------------------------------------------------------------
_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo / 'worker' / 'arcworker' / 'tools'))
import hfe  # noqa: E402  (intentional late import after sys.path fixup)

# ---------------------------------------------------------------------------
# Human-readable labels
# ---------------------------------------------------------------------------

_ENC_LABEL = {
	'mfm':       'MFM (ISOIBM)',
	'fm':        'FM (ISOIBM)',
	'amiga_mfm': 'Amiga MFM',
	'unknown':   'Unknown',
}

# For unknown encoding, try MFM first; fall back to FM if MFM yields nothing.
_ENC_TRY = {
	'mfm':       ['mfm'],
	'fm':        ['fm'],
	'amiga_mfm': ['amiga_mfm'],
	'unknown':   ['mfm', 'fm'],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_sector(s: dict) -> str:
	crc     = 'CRC OK' if s.get('crc_valid') else 'BAD CRC'
	dam     = s.get('dam_type') or '(no DAM)'
	cyl     = s.get('cyl')
	head    = s.get('head')
	sect    = s.get('sect')
	nc      = s.get('size_code')
	size    = s.get('size_used') or s.get('declared_size', '?')
	decl    = s.get('declared_size', size)
	override = f' [size overridden {decl}→{size}]' if s.get('size_was_overridden') else ''
	return f"C={cyl} H={head} R={sect} N={nc} ({size}B) {dam} {crc}{override}"


def _walk_best(track_bytes: bytes, encoding: str) -> tuple[list[dict], str]:
	"""Walk with the best encoding; return (sectors, encoding_used)."""
	encs = _ENC_TRY.get(encoding, ['mfm'])
	best_sectors: list[dict] = []
	best_enc = encs[0]
	for enc in encs:
		sectors = hfe.walk_track(track_bytes, enc)
		if len(sectors) > len(best_sectors):
			best_sectors = sectors
			best_enc = enc
		if best_sectors:
			break   # found something; don't try further for unknown encoding
	return best_sectors, best_enc


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
	ap = argparse.ArgumentParser(
		description='Inspect and analyse an HFE floppy image',
		formatter_class=argparse.RawDescriptionHelpFormatter,
	)
	ap.add_argument('hfe_file', help='Path to HFE image')
	ap.add_argument('-v', '--verbose', action='store_true',
	                help='Enable DEBUG logging from the HFE library')
	ap.add_argument('-t', '--track', type=int, metavar='N',
	                help='Print detailed sector listing for track N')
	ap.add_argument('--no-protection', action='store_true',
	                help='Skip copy-protection analysis')
	ap.add_argument('--no-mastering', action='store_true',
	                help='Skip mastering-data analysis')
	args = ap.parse_args()

	if args.verbose:
		logging.getLogger('hfe').setLevel(logging.DEBUG)

	path = Path(args.hfe_file)
	if not path.exists():
		sys.exit(f'File not found: {path}')

	# ── Header ──────────────────────────────────────────────────────────────
	try:
		hdr = hfe.parse_hfe_header(path)
	except Exception as exc:
		sys.exit(f'Failed to parse HFE header: {exc}')

	n_tracks  = hdr['n_tracks']
	n_sides   = hdr['n_sides']
	encoding  = hdr['track_encoding']
	hfe_ver   = hdr['version'] if isinstance(hdr['version'], int) else 1
	track_list = hdr['track_list']

	print(f"HFE file : {path}")
	print(f"Version  : {hdr['hfe_version_str']}")
	print(f"Tracks   : {n_tracks}")
	print(f"Sides    : {n_sides}")
	print(f"Encoding : {_ENC_LABEL.get(encoding, encoding)}")
	print(f"Bit rate : {hdr['bit_rate_kbps']} kbps")
	print(f"RPM      : {hdr['floppy_rpm']}")
	print()

	# ── Per-track summary ───────────────────────────────────────────────────
	with open(path, 'rb') as f:
		for t_idx in range(n_tracks):
			if t_idx >= len(track_list):
				break
			entry = track_list[t_idx]

			# Optional: detailed listing header
			if args.track is not None and t_idx == args.track:
				print(f"--- Track {t_idx} detail ---")

			side_summaries = []
			for side in range(n_sides):
				try:
					tb, weak = hfe.get_track_bytes(f, entry, side,
					                               hfe_version=hfe_ver)
				except Exception as exc:
					side_summaries.append(f"side {side}: ERROR ({exc})")
					continue

				sectors, enc_used = _walk_best(tb, encoding)

				# If encoding is unknown and both MFM and FM give results,
				# report both counts.
				if encoding == 'unknown':
					mfm_sects = hfe.walk_track(tb, 'mfm')
					fm_sects  = hfe.walk_track(tb, 'fm')
					if mfm_sects and fm_sects:
						enc_note = (f"{len(mfm_sects)} sector{'s' if len(mfm_sects)!=1 else ''} [MFM]"
						            f" / {len(fm_sects)} sector{'s' if len(fm_sects)!=1 else ''} [FM]")
						sectors = mfm_sects  # use MFM for detail listing
					else:
						n = len(sectors)
						enc_note = f"{n} sector{'s' if n!=1 else ''} [{enc_used.upper()}]"
				else:
					n = len(sectors)
					enc_note = f"{n} sector{'s' if n!=1 else ''} [{enc_used.upper()}]"

				weak_note = f" weak×{len(weak)}" if weak else ""
				raw_note  = f" ({len(tb)}B raw)"
				side_summaries.append(f"side {side}: {enc_note}{weak_note}{raw_note}")

				# Detailed sector listing for the requested track
				if args.track is not None and t_idx == args.track:
					for s in sectors:
						print(f"  {_fmt_sector(s)}")

			print(f"Track {t_idx:3d}: " + " | ".join(side_summaries))

			if args.track is not None and t_idx == args.track:
				print()

	print()

	# ── Protection analysis ─────────────────────────────────────────────────
	if not args.no_protection:
		print("=== Protection Analysis ===")
		try:
			result = hfe.analyse_hfe_protection(path)
		except Exception as exc:
			print(f"  ERROR: {exc}")
		else:
			inds = result.get('indicators', [])
			if not inds:
				print("  No protection indicators found.")
			for ind in inds:
				t     = ind.get('track')
				s     = ind.get('side')
				itype = ind.get('type')
				rest  = {k: v for k, v in ind.items()
				         if k not in ('type', 'track', 'side', 'offsets')}
				rest_str = '  '.join(f'{k}={v}' for k, v in rest.items())
				print(f"  [{itype}] track {t} side {s}:  {rest_str}")
				if itype == 'weak_bits' and 'offsets' in ind:
					offs = ind['offsets']
					print(f"    offsets: {offs}")
		print()

	# ── Mastering analysis ───────────────────────────────────────────────────
	if not args.no_mastering:
		print("=== Mastering Analysis ===")
		try:
			result = hfe.analyse_hfe_mastering(path)
		except Exception as exc:
			print(f"  ERROR: {exc}")
		else:
			inds = result.get('indicators', [])
			if not inds:
				print("  No mastering indicators found.")
			for ind in inds:
				t     = ind.get('track')
				s     = ind.get('side')
				itype = ind.get('type')
				if itype == 'traceback':
					fields = ' | '.join(ind.get('fields', []))
					print(f"  [traceback] track {t} side {s}: {fields}")
				elif itype == 'bcd_timestamp_record':
					ts       = ind.get('timestamp', '?')
					fmt_code = ind.get('format_code', '')
					fmt_desc = ind.get('format_description', '')
					serial   = ind.get('serial_number', '')
					text_c   = ind.get('text_c')
					decl     = ind.get('declared_size', '?')
					actual   = ind.get('actual_size', '?')
					crc_note = 'CRC OK' if ind.get('crc_valid') else 'BAD CRC'
					sz_note  = (f'{decl}B' if decl == actual
					            else f'{decl}→{actual}B [overridden]')
					print(f"  [bcd_timestamp_record] track {t} side {s}: "
					      f"{ts}  size={sz_note}  {crc_note}")
					print(f"    format_code={fmt_code!r}  format_desc={fmt_desc!r}")
					print(f"    serial_number={serial!r}")
					if text_c is not None:
						print(f"    text_c={text_c!r}")
				else:
					print(f"  [{itype}] track {t} side {s}")
		print()


if __name__ == '__main__':
	main()

# vim: ts=4 sw=4 noet
