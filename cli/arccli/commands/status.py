"""Status commands: health check, analysis status."""

import sys

from ..formatting import print_json, print_table


def cmd_health(client, args):
	"""Check server connectivity and health."""
	try:
		data = client.health()
		if args.json:
			print_json(data)
		else:
			status = data.get('status', 'unknown')
			print(f"Server: {client.base_url}")
			print(f"Status: {status}")
	except Exception as e:
		print(f"Cannot connect to {client.base_url}: {e}", file=sys.stderr)
		sys.exit(1)


def cmd_status(client, args):
	"""Show analysis status for an artefact."""
	data = client.get_artefact(args.uuid)

	if args.json:
		print_json(data)
		return

	print(f"Artefact: {data['label']}")
	print(f"  UUID: {data['uuid']}")
	print(f"  Type: {data['artefact_type']}")
	print(f"  File: {data['original_filename']}")

	partitions = data.get('partitions', [])
	if partitions:
		print(f"\n  Partitions ({len(partitions)}):")
		for p in partitions:
			fs = p.get('filesystem', 'unknown')
			files = p.get('total_files', 0)
			print(f"    [{p['partition_index']}] {fs} — {files} files")

# vim: ts=4 sw=4 noet
