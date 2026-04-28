"""Upload command: upload files as artefacts to an item."""

import os
import sys

from ..client import compute_file_hashes
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
	_INT_KEYS = {'dfi_clock_mhz'}
	for item in hint_list:
		if '=' not in item:
			print(f"Warning: ignoring malformed hint '{item}' (expected KEY=VALUE)", file=sys.stderr)
			continue
		key, _, value = item.partition('=')
		key = key.strip()
		value = value.strip()
		if key in _INT_KEYS:
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

	# Compute hashes client-side for integrity verification
	local_md5, local_sha256 = compute_file_hashes(filepath)

	# Track whether chunked progress was shown (so we know how to print "done.")
	_chunked = [False]

	def _progress(done, total):
		_chunked[0] = True
		print(f"\r{prefix}  [{done}/{total} chunks]", end='', flush=True)

	result = client.upload_artefact(
		item_uuid=item_uuid,
		filepath=filepath,
		label=label,
		artefact_type=artefact_type,
		auto_analyse=auto_analyse,
		hints=hints,
		progress_cb=_progress,
	)

	if result.get('duplicate'):
		if json_mode:
			print_json(result)
		else:
			if _chunked[0]:
				print(f"\r{prefix} skipped (duplicate)." + " " * 20)
			else:
				print("skipped (duplicate).")
			print(f"  Existing: {result['uuid']}")
		return True

	# Verify integrity
	server_md5 = result.get('md5')
	server_sha256 = result.get('sha256')
	hash_ok = True
	if server_md5 and server_md5 != local_md5:
		hash_ok = False
	if server_sha256 and server_sha256 != local_sha256:
		hash_ok = False

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
		if not hash_ok:
			print("  WARNING: Hash mismatch — upload may be corrupted!", file=sys.stderr)

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
	for filepath in files:
		# Label: use --label for single file, filename stem for multi-file
		if args.label and len(files) == 1:
			label = args.label
		else:
			label = os.path.splitext(os.path.basename(filepath))[0]

		try:
			ok = _upload_one(client, item_uuid, filepath, label, artefact_type, auto_analyse, hints, args.json)
			if ok:
				successes += 1
			else:
				failures += 1  # hash mismatch — upload may be corrupted
		except Exception as e:
			print(f"FAILED: {e}", file=sys.stderr)
			failures += 1

	# Summary for multi-file uploads
	if len(files) > 1:
		print(f"\nUploaded: {successes}/{len(files)}", end='')
		if failures:
			print(f" ({failures} failed)", end='')
		print()

	if failures:
		sys.exit(1)

# vim: ts=4 sw=4 noet
