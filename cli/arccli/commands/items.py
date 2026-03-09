"""Item management commands: list, create, view, update, delete."""

import sys

from ..formatting import print_json, print_table, truncate


def cmd_items_list(client, args):
	"""List items with optional filtering."""
	params = {
		'q': args.search,
		'platform_id': args.platform,
		'category_id': args.category,
		'tag': args.tag,
		'page': args.page,
		'per_page': args.per_page,
	}
	data = client.list_items(**params)

	if args.json:
		print_json(data)
		return

	items = data.get('items', [])
	rows = []
	for item in items:
		platform = item['platform']['name'] if item.get('platform') else '-'
		category = item['category']['name'] if item.get('category') else '-'
		rows.append([
			item['uuid'][:12],
			truncate(item['name'], 40),
			truncate(platform, 20),
			truncate(category, 20),
			str(item.get('artefact_count', 0)),
		])

	print_table(['UUID', 'Name', 'Platform', 'Category', 'Artefacts'], rows)

	total = data.get('total', 0)
	page = data.get('page', 1)
	pages = data.get('pages', 1)
	if pages > 1:
		print(f"\nPage {page}/{pages} ({total} total items)")


def cmd_items_create(client, args):
	"""Create a new item."""
	data = {
		'name': args.name,
		'description': args.description,
		'platform_id': args.platform,
		'category_id': args.category,
	}
	if args.tags:
		data['tags'] = [t.strip() for t in args.tags.split(',')]

	result = client.create_item(**data)

	if args.json:
		print_json(result)
		return

	print(f"Created item: {result['uuid']}")
	print(f"  Name: {result['name']}")


def cmd_items_view(client, args):
	"""View item details."""
	data = client.get_item(args.uuid)

	if args.json:
		print_json(data)
		return

	print(f"Item: {data['name']}")
	print(f"  UUID:        {data['uuid']}")
	if data.get('description'):
		print(f"  Description: {data['description']}")
	if data.get('platform'):
		print(f"  Platform:    {data['platform']['name']}")
	if data.get('category'):
		print(f"  Category:    {data['category']['name']}")
	if data.get('tags'):
		print(f"  Tags:        {', '.join(data['tags'])}")
	print(f"  Created:     {data['created_at']}")

	artefacts = data.get('artefacts', [])
	if artefacts:
		print(f"\n  Artefacts ({len(artefacts)}):")
		for a in artefacts:
			size = a.get('file_size', '')
			if isinstance(size, int):
				from ..formatting import format_size
				size = format_size(size)
			print(f"    {a['uuid'][:12]}  {a['label']}  [{a['artefact_type']}]  {size}")


def cmd_items_update(client, args):
	"""Update an item."""
	data = {}
	if args.name is not None:
		data['name'] = args.name
	if args.description is not None:
		data['description'] = args.description
	if args.platform is not None:
		data['platform_id'] = args.platform
	if args.category is not None:
		data['category_id'] = args.category

	if not data:
		print("Error: no fields to update.", file=sys.stderr)
		sys.exit(1)

	result = client.update_item(args.uuid, **data)

	if args.json:
		print_json(result)
		return

	print(f"Updated item: {result['uuid']}")


def cmd_items_delete(client, args):
	"""Delete an item."""
	if not args.yes:
		confirm = input(f"Delete item {args.uuid}? This cannot be undone. [y/N] ")
		if confirm.lower() != 'y':
			print("Cancelled.")
			return

	client.delete_item(args.uuid)
	print(f"Deleted item: {args.uuid}")

# vim: ts=4 sw=4 noet
