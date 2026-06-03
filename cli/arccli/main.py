"""
Arcology CLI — command-line client for Arcology digital artefact catalogue.

Entry point and argument parsing.
"""

import argparse
import sys
from .client import ArcologyClient, ArcologyError
from .config import create_config, get_config


def main():
	parser = argparse.ArgumentParser(
		prog='arco',
		description='Command-line client for Arcology digital artefact catalogue',
	)
	parser.add_argument('--server', help='Arcology server URL')
	parser.add_argument('--api-key', dest='api_key', help='API key for authentication')
	parser.add_argument('--profile', default='default', help='Config profile name (default: default)')
	parser.add_argument('--json', action='store_true', help='Output raw JSON')

	subparsers = parser.add_subparsers(dest='command', help='Available commands')

	# ---- configure ----
	subparsers.add_parser('configure', help='Interactive configuration setup')

	# ---- health ----
	subparsers.add_parser('health', help='Check server connectivity')

	# ---- items ----
	items_parser = subparsers.add_parser('items', help='Item management')
	items_sub = items_parser.add_subparsers(dest='items_command')

	# items list
	items_list = items_sub.add_parser('list', help='List items')
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
	items_create.add_argument('--name', '-n', required=True, help='Item name')
	items_create.add_argument('--description', '-d', help='Item description')
	items_create.add_argument('--platform', '-p', type=int, help='Platform ID')
	items_create.add_argument('--category', '-c', type=int, help='Category ID')
	items_create.add_argument('--tags', help='Comma-separated tag names')
	items_create.add_argument('--parent', help='Parent item UUID (creates as child of this item)')

	# items view
	items_view = items_sub.add_parser('view', help='View item details')
	items_view.add_argument('uuid', help='Item UUID')

	# items update
	items_update = items_sub.add_parser('update', help='Update an item (use --parent to move it)')
	items_update.add_argument('uuid', help='Item UUID')
	items_update.add_argument('--name', '-n', help='New name')
	items_update.add_argument('--description', '-d', help='New description')
	items_update.add_argument('--platform', '-p', type=int, help='New platform ID')
	items_update.add_argument('--category', '-c', type=int, help='New category ID')
	items_update.add_argument('--parent', help='Move to a new parent item (provide parent UUID)')
	items_update.add_argument('--no-parent', action='store_true', help='Make this a root item (remove parent)')

	# items delete
	items_delete = items_sub.add_parser('delete', help='Delete an item and all its descendants')
	items_delete.add_argument('uuid', help='Item UUID')
	items_delete.add_argument('--yes', '-y', action='store_true', help='Skip confirmation')

	# ---- artefacts ----
	artefacts_parser = subparsers.add_parser('artefacts', help='Artefact management')
	artefacts_sub = artefacts_parser.add_subparsers(dest='artefacts_command')

	# artefacts move
	artefacts_move = artefacts_sub.add_parser('move', help='Move an artefact to a different item')
	artefacts_move.add_argument('uuid', help='Artefact UUID')
	artefacts_move.add_argument('--to', required=True, dest='target_item_uuid', help='Target item UUID')

	# ---- upload ----
	upload_parser = subparsers.add_parser('upload', help='Upload artefacts to an item')
	upload_parser.add_argument('item_uuid', help='Item UUID to upload to')
	upload_parser.add_argument('files', nargs='*', help='File(s) to upload')
	upload_parser.add_argument('--dir', help='Upload all files from directory')
	upload_parser.add_argument('--label', '-l', help='Artefact label (auto-generated from filename for multi-file)')
	upload_parser.add_argument('--type', '-t', help='Override artefact type (e.g. RAW_SECTOR, SCP, HFE)')
	upload_parser.add_argument('--no-analyse', action='store_true', help='Skip automatic analysis')
	upload_parser.add_argument('--hint', action='append', metavar='KEY=VALUE', dest='hints',
	                           help='Analysis hint as KEY=VALUE (repeatable). '
	                                'Example: --hint dfi_clock_mhz=100. '
	                                'Supported keys: dfi_clock_mhz (int, MHz for DFI clock override), '
	                                'platform (str), filesystem (str).')

	# ---- download ----
	download_parser = subparsers.add_parser('download', help='Download an artefact')
	download_parser.add_argument('uuid', help='Artefact UUID')
	download_parser.add_argument('--output', '-o', help='Output path (default: original filename)')
	download_parser.add_argument('--force', '-f', action='store_true', help='Overwrite existing file')

	# ---- platforms / categories / tags ----
	subparsers.add_parser('platforms', help='List platforms')
	subparsers.add_parser('categories', help='List categories')
	subparsers.add_parser('tags', help='List tags')

	# ---- debug ----
	debug_parser = subparsers.add_parser('debug', help='Debug and analysis diagnostic tools')
	debug_sub = debug_parser.add_subparsers(dest='debug_command')

	# debug analysis
	debug_analysis = debug_sub.add_parser('analysis', help='Show full analysis details')
	debug_analysis.add_argument('uuid', help='Analysis UUID')

	# debug errors
	debug_errors = debug_sub.add_parser('errors', help='Show failed analyses for artefact tree')
	debug_errors.add_argument('uuid', help='Artefact UUID')
	debug_errors.add_argument('--all', action='store_true', help='Show all analyses, not just failures')

	# debug tree
	debug_tree = debug_sub.add_parser('tree', help='Show artefact derivation tree')
	debug_tree.add_argument('uuid', help='Artefact UUID')

	# debug processing-tree
	debug_ptree = debug_sub.add_parser('processing-tree', help='Show processing tree (artefact → path-grouped analyses → derived artefacts)')
	debug_ptree.add_argument('uuid', help='Artefact UUID')

	# debug failures
	debug_failures = debug_sub.add_parser('failures', help='Search failed analyses system-wide')
	debug_failures.add_argument('--type', dest='analysis_type', help='Filter by analysis type')
	debug_failures.add_argument('--tool', dest='tool_name', help='Filter by tool name')
	debug_failures.add_argument('--since', help='Failures after this date (ISO format)')
	debug_failures.add_argument('--until', help='Failures before this date (ISO format)')
	debug_failures.add_argument('--error', help='Filter by error message substring')
	debug_failures.add_argument('--page', type=int, default=1, help='Page number')
	debug_failures.add_argument('--per-page', type=int, default=50, dest='per_page', help='Results per page')

	# ---- status ----
	status_parser = subparsers.add_parser('status', help='Show artefact analysis status')
	status_parser.add_argument('uuid', help='Artefact UUID')

	# ---- bulk-import ----
	bulk_parser = subparsers.add_parser('bulk-import', help='Bulk import a file archive into Arcology')
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
	hashdb_sub = hashdb_parser.add_subparsers(dest='hashdb_command')

	# hashdb list
	hashdb_sub.add_parser('list', help='List all hash databases')

	# hashdb export
	hashdb_export = hashdb_sub.add_parser('export', help='Export a hash database to a file')
	hashdb_export.add_argument('id', type=int, help='Database ID')
	hashdb_export.add_argument('output_file', help='Output file path')
	hashdb_export.add_argument('--format', choices=['json', 'csv'], default='json')

	# hashdb import
	hashdb_import = hashdb_sub.add_parser('import', help='Import a hash database from a file')
	hashdb_import.add_argument('input_file', help='Input file path')
	hashdb_import.add_argument('--format', choices=['json', 'csv', 'auto'], default='auto',
	                           help='File format (default: auto-detect from extension)')
	hashdb_import.add_argument('--name', help='Override database name')
	hashdb_import.add_argument('--merge', action='store_true',
	                           help='Add to an existing database with the same name')

	# hashdb generate-arcarc
	hashdb_gen = hashdb_sub.add_parser('generate-arcarc',
	                                   help='Generate HashDB JSON from arcarc items in Arcology')
	hashdb_gen.add_argument('--output', default='arcarc-hashdb.json',
	                        help='Output JSON file (default: arcarc-hashdb.json)')
	hashdb_gen.add_argument('--tag', default='arcarc',
	                        help='Filter items by tag (default: arcarc)')
	hashdb_gen.add_argument('--multi-disc', choices=['merge', 'separate', 'both'],
	                        default='separate', dest='multi_disc',
	                        help='Multi-disc handling (default: separate)')
	hashdb_gen.add_argument('--root-files', choices=['include', 'skip', 'flag'],
	                        default='include', dest='root_files',
	                        help='Root-level file handling (default: include)')
	hashdb_gen.add_argument('--db-name', default='Arcarc RISC OS Archive', dest='db_name',
	                        help='HashDB name')
	hashdb_gen.add_argument('-v', '--verbose', action='store_true')
	hashdb_gen.add_argument('--dry-run', action='store_true',
	                        help='Scan only, report what would be included')

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
		_dispatch(client, args)
	except ArcologyError as e:
		print(f"Error: {e}", file=sys.stderr)
		sys.exit(1)
	except KeyboardInterrupt:
		print("\nInterrupted.", file=sys.stderr)
		sys.exit(130)
	except Exception as e:
		print(f"Error: {e}", file=sys.stderr)
		sys.exit(1)


def _cmd_configure(args):
	"""Interactive configuration setup."""
	print("Arcology CLI Configuration")
	print("-" * 30)
	profile = args.profile
	url = input(f"Server URL [{profile}]: ").strip()
	if not url:
		print("Cancelled.")
		return
	api_key = input("API key: ").strip()
	if not api_key:
		print("Cancelled.")
		return
	create_config(url, api_key, profile)


def _dispatch(client, args):
	"""Route to the appropriate command handler."""
	from .commands.download import cmd_download
	from .commands.items import (
		cmd_artefact_move,
		cmd_items_create,
		cmd_items_delete,
		cmd_items_list,
		cmd_items_update,
		cmd_items_view,
	)
	from .commands.status import cmd_health, cmd_status
	from .commands.taxonomy import cmd_categories, cmd_platforms, cmd_tags
	from .commands.upload import cmd_upload

	if args.command == 'health':
		cmd_health(client, args)

	elif args.command == 'items':
		if args.items_command == 'list':
			cmd_items_list(client, args)
		elif args.items_command == 'create':
			cmd_items_create(client, args)
		elif args.items_command == 'view':
			cmd_items_view(client, args)
		elif args.items_command == 'update':
			cmd_items_update(client, args)
		elif args.items_command == 'delete':
			cmd_items_delete(client, args)
		else:
			print("Usage: arco items {list|create|view|update|delete}", file=sys.stderr)
			sys.exit(1)

	elif args.command == 'artefacts':
		if args.artefacts_command == 'move':
			cmd_artefact_move(client, args)
		else:
			print("Usage: arco artefacts {move}", file=sys.stderr)
			sys.exit(1)

	elif args.command == 'upload':
		cmd_upload(client, args)

	elif args.command == 'download':
		cmd_download(client, args)

	elif args.command == 'platforms':
		cmd_platforms(client, args)

	elif args.command == 'categories':
		cmd_categories(client, args)

	elif args.command == 'tags':
		cmd_tags(client, args)

	elif args.command == 'debug':
		from .commands.debug import (
			cmd_debug_analysis,
			cmd_debug_errors,
			cmd_debug_failures,
			cmd_debug_processing_tree,
			cmd_debug_tree,
		)
		if args.debug_command == 'analysis':
			cmd_debug_analysis(client, args)
		elif args.debug_command == 'errors':
			cmd_debug_errors(client, args)
		elif args.debug_command == 'tree':
			cmd_debug_tree(client, args)
		elif args.debug_command == 'processing-tree':
			cmd_debug_processing_tree(client, args)
		elif args.debug_command == 'failures':
			cmd_debug_failures(client, args)
		else:
			print("Usage: arco debug {analysis|errors|tree|processing-tree|failures}", file=sys.stderr)
			sys.exit(1)

	elif args.command == 'status':
		cmd_status(client, args)

	elif args.command == 'bulk-import':
		from .commands.bulk_import import cmd_bulk_import
		cmd_bulk_import(client, args)

	elif args.command == 'hashdb':
		from .commands.hashdb import cmd_hashdb_export, cmd_hashdb_import, cmd_hashdb_list
		from .commands.hashdb_generate import cmd_hashdb_generate_arcarc
		if args.hashdb_command == 'list':
			cmd_hashdb_list(client, args)
		elif args.hashdb_command == 'export':
			cmd_hashdb_export(client, args)
		elif args.hashdb_command == 'import':
			cmd_hashdb_import(client, args)
		elif args.hashdb_command == 'generate-arcarc':
			cmd_hashdb_generate_arcarc(client, args)
		else:
			print("Usage: arco hashdb {list|export|import|generate-arcarc}", file=sys.stderr)
			sys.exit(1)

	else:
		print(f"Unknown command: {args.command}", file=sys.stderr)
		sys.exit(1)

# vim: ts=4 sw=4 noet
