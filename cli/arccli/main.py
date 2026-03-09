"""
Arcology CLI — command-line client for Arcology digital artefact catalogue.

Entry point and argument parsing.
"""

import argparse
import sys

from .client import ArcologyClient, ArcologyError
from .config import get_config, create_config


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
	items_list.add_argument('--page', type=int, default=1, help='Page number')
	items_list.add_argument('--per-page', type=int, default=25, help='Items per page')

	# items create
	items_create = items_sub.add_parser('create', help='Create a new item')
	items_create.add_argument('--name', '-n', required=True, help='Item name')
	items_create.add_argument('--description', '-d', help='Item description')
	items_create.add_argument('--platform', '-p', type=int, help='Platform ID')
	items_create.add_argument('--category', '-c', type=int, help='Category ID')
	items_create.add_argument('--tags', help='Comma-separated tag names')

	# items view
	items_view = items_sub.add_parser('view', help='View item details')
	items_view.add_argument('uuid', help='Item UUID')

	# items update
	items_update = items_sub.add_parser('update', help='Update an item')
	items_update.add_argument('uuid', help='Item UUID')
	items_update.add_argument('--name', '-n', help='New name')
	items_update.add_argument('--description', '-d', help='New description')
	items_update.add_argument('--platform', '-p', type=int, help='New platform ID')
	items_update.add_argument('--category', '-c', type=int, help='New category ID')

	# items delete
	items_delete = items_sub.add_parser('delete', help='Delete an item')
	items_delete.add_argument('uuid', help='Item UUID')
	items_delete.add_argument('--yes', '-y', action='store_true', help='Skip confirmation')

	# ---- upload ----
	upload_parser = subparsers.add_parser('upload', help='Upload artefacts to an item')
	upload_parser.add_argument('item_uuid', help='Item UUID to upload to')
	upload_parser.add_argument('files', nargs='*', help='File(s) to upload')
	upload_parser.add_argument('--dir', help='Upload all files from directory')
	upload_parser.add_argument('--label', '-l', help='Artefact label (auto-generated from filename for multi-file)')
	upload_parser.add_argument('--type', '-t', help='Override artefact type (e.g. RAW_SECTOR, SCP, HFE)')
	upload_parser.add_argument('--no-analyse', action='store_true', help='Skip automatic analysis')

	# ---- download ----
	download_parser = subparsers.add_parser('download', help='Download an artefact')
	download_parser.add_argument('uuid', help='Artefact UUID')
	download_parser.add_argument('--output', '-o', help='Output path (default: original filename)')
	download_parser.add_argument('--force', '-f', action='store_true', help='Overwrite existing file')

	# ---- platforms / categories / tags ----
	subparsers.add_parser('platforms', help='List platforms')
	subparsers.add_parser('categories', help='List categories')
	subparsers.add_parser('tags', help='List tags')

	# ---- status ----
	status_parser = subparsers.add_parser('status', help='Show artefact analysis status')
	status_parser.add_argument('uuid', help='Artefact UUID')

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
	from .commands.items import (
		cmd_items_list, cmd_items_create, cmd_items_view,
		cmd_items_update, cmd_items_delete,
	)
	from .commands.upload import cmd_upload
	from .commands.download import cmd_download
	from .commands.taxonomy import cmd_platforms, cmd_categories, cmd_tags
	from .commands.status import cmd_health, cmd_status

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

	elif args.command == 'status':
		cmd_status(client, args)

	else:
		print(f"Unknown command: {args.command}", file=sys.stderr)
		sys.exit(1)

# vim: ts=4 sw=4 noet
