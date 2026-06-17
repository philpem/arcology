"""
Arcology CLI — command-line client for Arcology digital artefact catalogue.

Entry point and argument parsing.
"""

import argparse
import sys
from arcology_shared.hints import UPLOAD_HINT_KEYS
from .client import ArcologyClient, ArcologyError
from .config import create_config, get_config, read_profile


def main():
	parser = argparse.ArgumentParser(
		prog='arco',
		description='Command-line client for Arcology digital artefact catalogue',
	)
	parser.add_argument('--server', help='Arcology server URL')
	parser.add_argument('--api-key', dest='api_key', help='API key for authentication')
	parser.add_argument('--profile', default='default', help='Config profile name (default: default)')
	parser.add_argument('--json', action='store_true', help='Output raw JSON')
	parser.add_argument('--debug', action='store_true', help='Show full tracebacks on unexpected errors')

	subparsers = parser.add_subparsers(dest='command', help='Available commands')

	# ---- configure ----
	subparsers.add_parser('configure', help='Interactive configuration setup')

	# ---- health ----
	health_parser = subparsers.add_parser('health', help='Check server connectivity')
	health_parser.set_defaults(func='status:cmd_health')

	# ---- items ----
	items_parser = subparsers.add_parser('items', help='Item management')
	items_sub = items_parser.add_subparsers(dest='items_command', required=True)

	# items list
	items_list = items_sub.add_parser('list', help='List items')
	items_list.set_defaults(func='items:cmd_items_list')
	items_list.add_argument('--search', '-s', help='Search by name/description')
	items_list.add_argument('--platform', '-p', type=int, help='Filter by platform ID')
	items_list.add_argument('--category', '-c', type=int, help='Filter by category ID')
	items_list.add_argument('--tag', '-t', help='Filter by tag name')
	items_list.add_argument('--parent', help='Filter by parent item UUID (show children of this item)')
	items_list.add_argument('--root-only', action='store_true', help='Show only root (top-level) items')
	items_list.add_argument('--page', type=int, default=1, help='Page number')
	items_list.add_argument('--per-page', type=int, default=25, help='Items per page')

	# items create
	items_create = items_sub.add_parser('create', help='Create a new item')
	items_create.set_defaults(func='items:cmd_items_create')
	items_create.add_argument('--name', '-n', required=True, help='Item name')
	items_create.add_argument('--description', '-d', help='Item description')
	items_create.add_argument('--platform', '-p', type=int, help='Platform ID')
	items_create.add_argument('--category', '-c', type=int, help='Category ID')
	items_create.add_argument('--tags', help='Comma-separated tag names')
	items_create.add_argument('--parent', help='Parent item UUID (creates as child of this item)')

	# items view
	items_view = items_sub.add_parser('view', help='View item details')
	items_view.set_defaults(func='items:cmd_items_view')
	items_view.add_argument('uuid', help='Item UUID')

	# items update
	items_update = items_sub.add_parser('update', help='Update an item (use --parent to move it)')
	items_update.set_defaults(func='items:cmd_items_update')
	items_update.add_argument('uuid', help='Item UUID')
	items_update.add_argument('--name', '-n', help='New name')
	items_update.add_argument('--description', '-d', help='New description')
	items_update.add_argument('--platform', '-p', type=int, help='New platform ID')
	items_update.add_argument('--category', '-c', type=int, help='New category ID')
	items_update.add_argument('--parent', help='Move to a new parent item (provide parent UUID)')
	items_update.add_argument('--no-parent', action='store_true', help='Make this a root item (remove parent)')

	# items delete
	items_delete = items_sub.add_parser('delete', help='Delete an item and all its descendants')
	items_delete.set_defaults(func='items:cmd_items_delete')
	items_delete.add_argument('uuid', help='Item UUID')
	items_delete.add_argument('--yes', '-y', action='store_true', help='Skip confirmation')

	# ---- artefacts ----
	artefacts_parser = subparsers.add_parser('artefacts', help='Artefact management')
	artefacts_sub = artefacts_parser.add_subparsers(dest='artefacts_command', required=True)

	# artefacts move
	artefacts_move = artefacts_sub.add_parser('move', help='Move an artefact to a different item')
	artefacts_move.set_defaults(func='items:cmd_artefact_move')
	artefacts_move.add_argument('uuid', help='Artefact UUID')
	artefacts_move.add_argument('--to', required=True, dest='target_item_uuid', help='Target item UUID')

	# ---- upload ----
	upload_parser = subparsers.add_parser('upload', help='Upload artefacts to an item')
	upload_parser.set_defaults(func='upload:cmd_upload')
	upload_parser.add_argument('item_uuid', help='Item UUID to upload to')
	upload_parser.add_argument('files', nargs='*', help='File(s) to upload')
	upload_parser.add_argument('--dir', help='Upload all files from directory')
	upload_parser.add_argument('--label', '-l', help='Artefact label (auto-generated from filename for multi-file)')
	upload_parser.add_argument('--type', '-t', help='Override artefact type (e.g. RAW_SECTOR, SCP, HFE)')
	upload_parser.add_argument('--no-analyse', action='store_true', help='Skip automatic analysis')
	upload_parser.add_argument('--hint', action='append', metavar='KEY=VALUE', dest='hints',
	                           help='Analysis hint as KEY=VALUE (repeatable). '
	                                'Example: --hint dfi_clock_mhz=100. '
	                                'Supported keys: ' + ', '.join(UPLOAD_HINT_KEYS) + '. '
	                                '(dfi_clock_mhz is an integer MHz value for DFI clock '
	                                'override; the rest are free-text strings.)')

	# ---- download ----
	download_parser = subparsers.add_parser('download', help='Download an artefact')
	download_parser.set_defaults(func='download:cmd_download')
	download_parser.add_argument('uuid', help='Artefact UUID')
	download_parser.add_argument('--output', '-o', help='Output path (default: original filename)')
	download_parser.add_argument('--force', '-f', action='store_true', help='Overwrite existing file')

	# ---- platforms / categories / tags ----
	subparsers.add_parser('platforms', help='List platforms').set_defaults(func='taxonomy:cmd_platforms')
	subparsers.add_parser('categories', help='List categories').set_defaults(func='taxonomy:cmd_categories')
	subparsers.add_parser('tags', help='List tags').set_defaults(func='taxonomy:cmd_tags')

	# ---- debug ----
	debug_parser = subparsers.add_parser('debug', help='Debug and analysis diagnostic tools')
	debug_sub = debug_parser.add_subparsers(dest='debug_command', required=True)

	# debug analysis
	debug_analysis = debug_sub.add_parser('analysis', help='Show full analysis details')
	debug_analysis.set_defaults(func='debug:cmd_debug_analysis')
	debug_analysis.add_argument('uuid', help='Analysis UUID')

	# debug errors
	debug_errors = debug_sub.add_parser('errors', help='Show failed analyses for artefact tree')
	debug_errors.set_defaults(func='debug:cmd_debug_errors')
	debug_errors.add_argument('uuid', help='Artefact UUID')
	debug_errors.add_argument('--all', action='store_true', help='Show all analyses, not just failures')

	# debug tree
	debug_tree = debug_sub.add_parser('tree', help='Show artefact derivation tree')
	debug_tree.set_defaults(func='debug:cmd_debug_tree')
	debug_tree.add_argument('uuid', help='Artefact UUID')

	# debug processing-tree
	debug_ptree = debug_sub.add_parser('processing-tree', help='Show processing tree (artefact → path-grouped analyses → derived artefacts)')
	debug_ptree.set_defaults(func='debug:cmd_debug_processing_tree')
	debug_ptree.add_argument('uuid', help='Artefact UUID')

	# debug failures
	debug_failures = debug_sub.add_parser('failures', help='Search failed analyses system-wide')
	debug_failures.set_defaults(func='debug:cmd_debug_failures')
	debug_failures.add_argument('--type', dest='analysis_type', help='Filter by analysis type')
	debug_failures.add_argument('--tool', dest='tool_name', help='Filter by tool name')
	debug_failures.add_argument('--since', help='Failures after this date (ISO format)')
	debug_failures.add_argument('--until', help='Failures before this date (ISO format)')
	debug_failures.add_argument('--error', help='Filter by error message substring')
	debug_failures.add_argument('--page', type=int, default=1, help='Page number')
	debug_failures.add_argument('--per-page', type=int, default=50, dest='per_page', help='Results per page')

	# ---- status ----
	status_parser = subparsers.add_parser('status', help='Show artefact analysis status')
	status_parser.set_defaults(func='status:cmd_status')
	status_parser.add_argument('uuid', help='Artefact UUID')

	# ---- bulk-import ----
	bulk_parser = subparsers.add_parser('bulk-import', help='Bulk import a file archive into Arcology')
	bulk_parser.set_defaults(func='bulk_import:cmd_bulk_import')
	bulk_parser.add_argument('--archive-dir', default=None,
	                         help='Local mirror root (required unless --purge)')
	bulk_parser.add_argument('--tag', default=None,
	                         help='Tag for imported items (optional; required with --purge)')
	bulk_parser.add_argument('--categories', default=None,
	                         help='Filter by top-level directory (comma-separated)')
	bulk_parser.add_argument('--skip-dirs', default=None,
	                         help='Comma-separated directory names to skip')
	bulk_parser.add_argument('--skip-ext', default=None,
	                         help='Comma-separated extensions to skip (e.g. .pdf,.txt)')
	bulk_parser.add_argument('--platform', default=None,
	                         help='Platform name to assign')
	bulk_parser.add_argument('--name-prefix', default=None,
	                         help='Prefix for item names')
	bulk_parser.add_argument('--parent', default=None,
	                         help='Parent item UUID; created items are nested under it')
	bulk_parser.add_argument('--category-map', default=None,
	                         help='Directory-to-category mapping as K=V,...')
	bulk_parser.add_argument('--no-auto-analyse', action='store_true',
	                         help='Upload without triggering automatic analysis')
	bulk_parser.add_argument('--smart-labels', action='store_true',
	                         help='Strip single-char groupings and use filename alone when self-describing')
	bulk_parser.add_argument('--keep-compressed-duplicates', action='store_true',
	                         help='Upload every recognised image form; do not collapse '
	                              'raw/compressed/archived duplicates of the same image '
	                              '(default: keep only the best form, archive > compressed > raw)')
	bulk_parser.add_argument('--bundle-sidecars', action='store_true',
	                         help='Bundle each disk image with its loose sidecar files '
	                              '(ddrescue .map, readme, .log, checksums sharing its '
	                              'directory) into a single zip and upload that instead')
	bulk_parser.add_argument('--bundle-tmpdir', default=None,
	                         help='Directory for temporary bundle zips (default: system '
	                              'temp). Needs free space roughly equal to the image size.')
	bulk_parser.add_argument('--max-size', default=None, metavar='SIZE',
	                         help='Skip (and log) any source file larger than SIZE, e.g. '
	                              '50G, 500M. Suffixes K/M/G/T are 1024-based.')
	bulk_parser.add_argument('--flat', action='store_true',
	                         help='Treat archive-dir as a single collection')
	bulk_parser.add_argument('--arcarc', action='store_true',
	                         help='Preset for arcarc.nl (sets tag, prefix, category map, smart labels)')
	bulk_parser.add_argument('--resume', action='store_true',
	                         help='Skip artefacts whose filename already exists on the Item')
	bulk_parser.add_argument('--dry-run', action='store_true',
	                         help='Scan only, do not import')
	bulk_parser.add_argument('-v', '--verbose', action='store_true')
	bulk_parser.add_argument('--purge', action='store_true',
	                         help='Delete all items with the given tag instead of importing')
	bulk_parser.add_argument('--yes', '-y', action='store_true',
	                         help='Skip confirmation prompt for --purge')

	# ---- hashdb ----
	hashdb_parser = subparsers.add_parser('hashdb', help='Hash database management')
	hashdb_sub = hashdb_parser.add_subparsers(dest='hashdb_command', required=True)

	# hashdb list
	hashdb_sub.add_parser('list', help='List all hash databases').set_defaults(func='hashdb:cmd_hashdb_list')

	# hashdb export
	hashdb_export = hashdb_sub.add_parser('export', help='Export a hash database to a file')
	hashdb_export.set_defaults(func='hashdb:cmd_hashdb_export')
	hashdb_export.add_argument('id', type=int, help='Database ID')
	hashdb_export.add_argument('output_file', help='Output file path')
	hashdb_export.add_argument('--format', choices=['json', 'csv'], default='json')

	# hashdb import
	hashdb_import = hashdb_sub.add_parser('import', help='Import a hash database from a file')
	hashdb_import.set_defaults(func='hashdb:cmd_hashdb_import')
	hashdb_import.add_argument('input_file', help='Input file path')
	hashdb_import.add_argument('--format', choices=['json', 'csv', 'auto'], default='auto',
	                           help='File format (default: auto-detect from extension)')
	hashdb_import.add_argument('--name', help='Override database name')
	hashdb_import.add_argument('--merge', action='store_true',
	                           help='Add to an existing database with the same name')

	# hashdb generate-riscos
	hashdb_gen = hashdb_sub.add_parser('generate-riscos',
	                                   help='Generate a RISC OS HashDB JSON from items in Arcology')
	hashdb_gen.set_defaults(func='hashdb_generate:cmd_hashdb_generate_riscos')
	hashdb_gen.add_argument('--output', default='riscos-hashdb.json',
	                        help='Output JSON file (default: riscos-hashdb.json)')
	hashdb_gen.add_argument('--tag', action='append',
	                        help='Select items by tag (repeatable)')
	hashdb_gen.add_argument('--item', action='append',
	                        help='Select an item by UUID (repeatable)')
	hashdb_gen.add_argument('--platform',
	                        help='Select items by platform name')
	hashdb_gen.add_argument('--db-name', required=True, dest='db_name',
	                        help='HashDB name (required)')
	hashdb_gen.add_argument('--db-description', default='', dest='db_description',
	                        help='HashDB description')
	hashdb_gen.add_argument('--db-version', default=None, dest='db_version',
	                        help='HashDB version string (default: today)')
	hashdb_gen.add_argument('--source-url', default=None, dest='source_url',
	                        help='HashDB source URL')
	hashdb_gen.add_argument('--multi-disc', choices=['merge', 'separate', 'both'],
	                        default='separate', dest='multi_disc',
	                        help='Multi-disc handling (default: separate)')
	hashdb_gen.add_argument('--root-files', choices=['include', 'skip', 'flag'],
	                        default='skip', dest='root_files',
	                        help='Root-level (non-application) file handling (default: skip)')
	hashdb_gen.add_argument('--path-match', action='store_true', dest='path_match',
	                        help='Require generated products to match relative paths as well as hashes')
	hashdb_gen.add_argument('--require-mandatory', action='store_true', dest='require_mandatory',
	                        help='Skip products that have no mandatory file. Such products '
	                             'have no discriminating fingerprint and are ignored by the '
	                             'matcher; by default they are still emitted so a curator can '
	                             'add a mandatory file by hand.')
	hashdb_gen.add_argument('--no-global-check', action='store_false', dest='global_check',
	                        help='Skip the cross-catalogue /hash-lookup uniqueness check')
	hashdb_gen.add_argument('--include-known', action='store_true', dest='include_known',
	                        help='Allow files already present in an active hash database '
	                             'to be marked mandatory. Use when regenerating a database '
	                             'whose own files are, by definition, already known — '
	                             'otherwise every such application produces no mandatory file.')
	hashdb_gen.add_argument('-j', '--jobs', type=int, default=8,
	                        help='Concurrent API requests (default: 8; 1 = serial)')
	hashdb_gen.add_argument('-v', '--verbose', action='store_true')
	hashdb_gen.add_argument('--explain', action='store_true',
	                        help='For every application that produced no mandatory '
	                             'file, report why (per-app reason and a summary '
	                             'breakdown): no launch target found, target already '
	                             'in a hash database, shared across apps, etc.')
	hashdb_gen.add_argument('--dry-run', action='store_true',
	                        help='List selected items only; generate nothing')

	args = parser.parse_args()

	if not args.command:
		parser.print_help()
		sys.exit(1)

	# Handle configure command before requiring config
	if args.command == 'configure':
		_cmd_configure(args)
		return

	# Load config and create client
	try:
		config = get_config(args)
	except SystemExit:
		raise
	except Exception as e:
		print(f"Configuration error: {e}", file=sys.stderr)
		sys.exit(1)

	client = ArcologyClient(config['url'], config['api_key'])

	# Dispatch command
	try:
		_resolve_handler(args.func)(client, args)
	except ArcologyError as e:
		print(f"Error: {e}", file=sys.stderr)
		sys.exit(1)
	except KeyboardInterrupt:
		print("\nInterrupted.", file=sys.stderr)
		sys.exit(130)
	except Exception as e:
		if args.debug:
			raise
		print(f"Error: {e}", file=sys.stderr)
		print("(re-run with --debug for a full traceback)", file=sys.stderr)
		sys.exit(1)


def _cmd_configure(args):
	"""Interactive configuration setup.

	Pre-fills from the profile's existing values so re-running to change one
	field does not require retyping the other: an empty answer keeps the
	current value.  The secret API key is never echoed back in the prompt.
	"""
	print("Arcology CLI Configuration")
	print("-" * 30)
	profile = args.profile
	existing = read_profile(profile)
	current_url = existing.get('url', '')
	current_key = existing.get('api_key', '')

	url_prompt = f"Server URL [{current_url}]: " if current_url else "Server URL: "
	url = input(url_prompt).strip() or current_url
	if not url:
		print("Cancelled (no server URL provided).")
		return

	key_prompt = "API key [keep existing]: " if current_key else "API key: "
	api_key = input(key_prompt).strip() or current_key
	if not api_key:
		print("Cancelled (no API key provided).")
		return
	create_config(url, api_key, profile)


def _resolve_handler(spec):
	"""Resolve a 'module:function' handler spec from arccli.commands.

	Handlers are referenced by name in set_defaults(func=...) and imported
	lazily here so that running one command does not pay the import cost of
	every command module.
	"""
	import importlib
	module_name, func_name = spec.split(':')
	module = importlib.import_module(f'.commands.{module_name}', package=__package__)
	return getattr(module, func_name)


# vim: ts=4 sw=4 noet
