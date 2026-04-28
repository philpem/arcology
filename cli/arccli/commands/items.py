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
	if args.parent:
		params['parent_uuid'] = args.parent
	elif args.root_only:
		params['parent_uuid'] = 'none'

	data = client.list_items(**params)

	if args.json:
		print_json(data)
		return

	items = data.get('items', [])
	rows = []
	for item in items:
		# Build path prefix for non-root items
		path = item.get('path', [])
		name = item['name']
		if path:
			name = ' / '.join([p['name'] for p in path]) + ' / ' + name
		platform = item['platform']['name'] if item.get('platform') else \
			(item['effective_platform']['name'] + ' (inh.)' if item.get('effective_platform') else '-')
		category = item['category']['name'] if item.get('category') else \
			(item['effective_category']['name'] + ' (inh.)' if item.get('effective_category') else '-')
		rows.append([
			item['uuid'][:12],
			truncate(name, 50),
			truncate(platform, 20),
			truncate(category, 20),
			str(item.get('child_count', 0)),
			str(item.get('artefact_count', 0)),
		])

	print_table(['UUID', 'Name', 'Platform', 'Category', 'Children', 'Artefacts'], rows)

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
	if args.parent:
		data['parent_uuid'] = args.parent

	result = client.create_item(**data)

	if args.json:
		print_json(result)
		return

	print(f"Created item: {result['uuid']}")
	print(f"  Name: {result['name']}")
	if result.get('parent_uuid'):
		print(f"  Parent: {result['parent_uuid']} ({result.get('parent_name', '')})")


def cmd_items_view(client, args):
	"""View item details."""
	data = client.get_item(args.uuid)

	if args.json:
		print_json(data)
		return

	# Show full path
	path = data.get('path', [])
	if path:
		print(f"Path:  {' / '.join(p['name'] for p in path)} / {data['name']}")
	print(f"Item: {data['name']}")
	print(f"  UUID:        {data['uuid']}")
	if data.get('parent_uuid'):
		print(f"  Parent:      {data['parent_uuid']} ({data.get('parent_name', '')})")
	if data.get('description'):
		print(f"  Description: {data['description']}")
	if data.get('platform'):
		print(f"  Platform:    {data['platform']['name']}")
	elif data.get('effective_platform'):
		print(f"  Platform:    {data['effective_platform']['name']} (inherited)")
	if data.get('category'):
		print(f"  Category:    {data['category']['name']}")
	elif data.get('effective_category'):
		print(f"  Category:    {data['effective_category']['name']} (inherited)")
	if data.get('tags'):
		print(f"  Tags:        {', '.join(data['tags'])}")
	print(f"  Children:    {data.get('child_count', 0)}")
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
	"""Update an item (including moving it to a different parent)."""
	data = {}
	if args.name is not None:
		data['name'] = args.name
	if args.description is not None:
		data['description'] = args.description
	if args.platform is not None:
		data['platform_id'] = args.platform
	if args.category is not None:
		data['category_id'] = args.category
	if args.parent is not None:
		data['parent_uuid'] = args.parent
	if args.no_parent:
		data['parent_uuid'] = None

	if not data:
		print("Error: no fields to update.", file=sys.stderr)
		sys.exit(1)

	result = client.update_item(args.uuid, **data)

	if args.json:
		print_json(result)
		return

	print(f"Updated item: {result['uuid']}")
	if result.get('parent_uuid'):
		print(f"  Parent: {result['parent_uuid']} ({result.get('parent_name', '')})")
	else:
		print("  Parent: (root item)")


def cmd_items_delete(client, args):
	"""Delete an item."""
	if not args.yes:
		confirm = input(f"Delete item {args.uuid} and all its children and artefacts? This cannot be undone. [y/N] ")
		if confirm.lower() != 'y':
			print("Cancelled.")
			return

	client.delete_item(args.uuid)
	print(f"Deleted item: {args.uuid}")


def cmd_artefact_move(client, args):
	"""Move an artefact to a different item."""
	result = client.move_artefact(args.uuid, args.target_item_uuid)

	if args.json:
		print_json(result)
	else:
		print(f"Moved artefact '{result.get('label', args.uuid)}' to item '{result.get('item_name', result.get('item_uuid', ''))}'")

# vim: ts=4 sw=4 noet
