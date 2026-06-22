"""Upload command: upload files as artefacts to an item."""

import os
import sys
from arcology_shared.hints import UPLOAD_HINT_INT_KEYS
from ..client import verify_artefact_hashes
from ..formatting import format_size, print_json

# Files to skip in directory uploads
JUNK_FILES = {'.DS_Store', 'Thumbs.db', 'desktop.ini', '._.'}
JUNK_DIRS = {'__MACOSX', '.Spotlight-V100', '.Trashes'}


def _is_junk(path: str) -> bool:
	"""Check if a file/directory should be skipped."""
	name = os.path.basename(path)
	if name in JUNK_FILES or name.startswith('._'):
		return True
	parts = path.split(os.sep)
	return any(d in JUNK_DIRS for d in parts)


def _collect_files(path: str) -> list[str]:
	"""Recursively collect files from a directory, skipping junk."""
	files = []
	for root, dirs, filenames in os.walk(path):
		# Filter out junk directories in-place
		dirs[:] = [d for d in dirs if d not in JUNK_DIRS]
		for filename in filenames:
			filepath = os.path.join(root, filename)
			if not _is_junk(filepath):
				files.append(filepath)
	files.sort()
	return files


def _parse_hints(hint_list: list[str] | None) -> dict | None:
	"""Parse a list of 'KEY=VALUE' strings into a hints dict.

	Integer-valued keys (e.g. dfi_clock_mhz) are coerced automatically.
	Returns None if hint_list is empty or None.
	"""
	if not hint_list:
		return None
	hints = {}
	for item in hint_list:
		if '=' not in item:
			print(f"Warning: ignoring malformed hint '{item}' (expected KEY=VALUE)", file=sys.stderr)
			continue
		key, _, value = item.partition('=')
		key = key.strip()
		value = value.strip()
		if key in UPLOAD_HINT_INT_KEYS:
			try:
				value = int(value)
			except ValueError:
				print(f"Warning: hint '{key}' expects an integer, got '{value}'", file=sys.stderr)
				continue
		hints[key] = value
	return hints or None


def _upload_one(client, item_uuid, filepath, label, artefact_type, auto_analyse, hints, json_mode):
	"""Upload a single file and report results."""
	filename = os.path.basename(filepath)
	file_size = os.path.getsize(filepath)
	prefix = f"Uploading {filename} ({format_size(file_size)})..."

	print(prefix, end=' ', flush=True)

	# Track whether chunked progress was shown (so we know how to print "done.")
	_chunked = [False]
	_SPINNER = r'/-\|'

	def _fmt_speed(bps):
		if bps >= 1024 * 1024:
			return f'{bps / (1024 * 1024):.1f} MB/s'
		if bps >= 1024:
			return f'{bps / 1024:.0f} KB/s'
		return f'{bps:.0f} B/s'

	def _fmt_eta(secs):
		secs = int(secs) + 1
		if secs >= 3600:
			return f'{secs // 3600}h {(secs % 3600) // 60}m'
		if secs >= 60:
			return f'{secs // 60}m {secs % 60}s'
		return f'{secs}s'

	def _progress(done, total, speed_bps=None, eta_secs=None):
		_chunked[0] = True
		info = f'{done}/{total} chunks'
		if speed_bps is not None:
			info += f' · {_fmt_speed(speed_bps)}'
		if eta_secs is not None:
			info += f' · ETA {_fmt_eta(eta_secs)}'
		print(f"\r{prefix}  [{info}]", end='', flush=True)

	def _status(state):
		if state == 'assembling':
			_chunked[0] = True

	def _tick(n):
		_chunked[0] = True
		spin = _SPINNER[n % len(_SPINNER)]
		print(f"\r{prefix}  {spin} assembling on server...", end='', flush=True)

	result = client.upload_artefact(
		item_uuid=item_uuid,
		filepath=filepath,
		label=label,
		artefact_type=artefact_type,
		auto_analyse=auto_analyse,
		hints=hints,
		progress_cb=_progress,
		status_cb=_status,
		tick_cb=_tick,
	)

	# Verify integrity against the server's recorded hashes.
	# None means the server returned no hashes to compare against.
	hash_ok = verify_artefact_hashes(filepath, result)

	if json_mode:
		result['_hash_verified'] = hash_ok
		print_json(result)
	else:
		if _chunked[0]:
			# Overwrite the chunk-progress line then end with a newline
			print(f"\r{prefix} done." + " " * 20)
		else:
			print("done.")
		print(f"  Artefact: {result['uuid']}")
		print(f"  Type:     {result['artefact_type']}")
		if result.get('queued_analyses'):
			print(f"  Queued:   {', '.join(result['queued_analyses'])}")
		if hash_ok is False:
			print("  WARNING: Hash mismatch — upload may be corrupted!", file=sys.stderr)
		elif hash_ok is None:
			print("  NOTE: server returned no hashes; integrity not verified.", file=sys.stderr)

	return hash_ok


def cmd_upload(client, args):
	"""Upload one or more files to an item."""
	item_uuid = args.item_uuid
	auto_analyse = not args.no_analyse
	artefact_type = args.type
	hints = _parse_hints(getattr(args, 'hints', None))

	# Collect files to upload
	if args.dir:
		if not os.path.isdir(args.dir):
			print(f"Error: '{args.dir}' is not a directory.", file=sys.stderr)
			sys.exit(1)
		files = _collect_files(args.dir)
		if not files:
			print(f"Error: no files found in '{args.dir}'.", file=sys.stderr)
			sys.exit(1)
		print(f"Found {len(files)} files in {args.dir}")
	elif args.files:
		files = []
		for f in args.files:
			if not os.path.isfile(f):
				print(f"Error: '{f}' is not a file.", file=sys.stderr)
				sys.exit(1)
			files.append(f)
	else:
		print("Error: provide FILE(s) or --dir PATH.", file=sys.stderr)
		sys.exit(1)

	# Upload each file
	successes = 0
	failures = 0
	hash_mismatches = 0
	for filepath in files:
		# Label: use --label for single file, filename stem for multi-file
		if args.label and len(files) == 1:
			label = args.label
		else:
			label = os.path.splitext(os.path.basename(filepath))[0]

		try:
			ok = _upload_one(client, item_uuid, filepath, label, artefact_type, auto_analyse, hints, args.json)
			successes += 1
			if ok is False:
				# The artefact WAS created; report corruption separately
				# from upload failures rather than calling it "failed".
				hash_mismatches += 1
		except Exception as e:
			print(f"FAILED: {e}", file=sys.stderr)
			failures += 1

	# Summary for multi-file uploads
	if len(files) > 1:
		print(f"\nUploaded: {successes}/{len(files)}", end='')
		if failures:
			print(f" ({failures} failed)", end='')
		if hash_mismatches:
			print(f" ({hash_mismatches} hash mismatch(es))", end='')
		print()

	if failures or hash_mismatches:
		sys.exit(1)

# vim: ts=4 sw=4 noet
