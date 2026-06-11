"""
Output formatting helpers for the Arcology CLI.

Supports human-readable table output (default) and raw JSON mode.
"""

import json
import sys


def print_json(data):
	"""Print data as formatted JSON."""
	print(json.dumps(data, indent=2))


def print_table(headers: list[str], rows: list[list[str]], file=sys.stdout):
	"""Print aligned table with headers."""
	if not rows:
		print("No results.", file=file)
		return

	# Calculate column widths
	widths = [len(h) for h in headers]
	for row in rows:
		for i, cell in enumerate(row):
			widths[i] = max(widths[i], len(str(cell)))

	# Print header
	header_line = '  '.join(h.ljust(widths[i]) for i, h in enumerate(headers))
	print(header_line, file=file)
	print('  '.join('-' * w for w in widths), file=file)

	# Print rows
	for row in rows:
		line = '  '.join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))
		print(line, file=file)


def format_size(size_bytes):
	"""Format a byte count in human-readable binary units (e.g. '12.3 GiB')."""
	if size_bytes is None:
		return '-'
	for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
		if abs(size_bytes) < 1024:
			return f"{size_bytes:.1f} {unit}" if unit != 'B' else f"{size_bytes} {unit}"
		size_bytes /= 1024
	return f"{size_bytes:.1f} PiB"


def truncate(s: str, maxlen: int = 40) -> str:
	"""Truncate string with ellipsis if too long."""
	if s is None:
		return ''
	return s if len(s) <= maxlen else s[:maxlen - 3] + '...'

# vim: ts=4 sw=4 noet
