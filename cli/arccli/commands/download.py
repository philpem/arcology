"""Download command: download artefact files."""

import os
import sys
from ..formatting import format_size, print_json


def cmd_download(client, args):
	"""Download an artefact file."""
	uuid = args.uuid

	# Get artefact info to determine filename
	artefact = client.get_artefact(uuid)
	original_name = artefact.get('original_filename', f'{uuid}.bin')

	output_path = args.output or original_name
	if os.path.isdir(output_path):
		output_path = os.path.join(output_path, original_name)

	if os.path.exists(output_path) and not args.force:
		print(f"Error: '{output_path}' already exists. Use --force to overwrite.", file=sys.stderr)
		sys.exit(1)

	if not args.json:
		print(f"Downloading {original_name}...", end=' ', flush=True)
	client.download_artefact(uuid, output_path)
	size = os.path.getsize(output_path)
	if args.json:
		print_json({
			'uuid': uuid,
			'original_filename': original_name,
			'output_path': output_path,
			'file_size': size,
		})
	else:
		print(f"done ({format_size(size)})")
		print(f"  Saved to: {output_path}")

# vim: ts=4 sw=4 noet
