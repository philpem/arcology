"""Taxonomy listing commands: platforms, categories, tags."""

from ..formatting import print_json, print_table


def cmd_platforms(client, args):
	"""List all platforms."""
	data = client.list_platforms()
	if args.json:
		print_json(data)
		return

	platforms = data.get('platforms', [])
	rows = [[str(p['id']), p['name'], p.get('description') or ''] for p in platforms]
	print_table(['ID', 'Name', 'Description'], rows)


def cmd_categories(client, args):
	"""List all categories."""
	data = client.list_categories()
	if args.json:
		print_json(data)
		return

	categories = data.get('categories', [])
	rows = [[str(c['id']), c['name'], c.get('description') or ''] for c in categories]
	print_table(['ID', 'Name', 'Description'], rows)


def cmd_tags(client, args):
	"""List all tags."""
	data = client.list_tags()
	if args.json:
		print_json(data)
		return

	tags = data.get('tags', [])
	rows = [[str(t['id']), t['name']] for t in tags]
	print_table(['ID', 'Name'], rows)

# vim: ts=4 sw=4 noet
