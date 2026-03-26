"""Taxonomy listing commands: platforms, categories, tags."""

from ..formatting import print_json, print_table


def _taxonomy_command(client, args, client_method, data_key, headers, row_fn):
	data = client_method()
	if args.json:
		print_json(data)
		return
	rows = [row_fn(item) for item in data.get(data_key, [])]
	print_table(headers, rows)


def cmd_platforms(client, args):
	"""List all platforms."""
	_taxonomy_command(client, args, client.list_platforms, 'platforms',
		['ID', 'Name', 'Description'],
		lambda p: [str(p['id']), p['name'], p.get('description') or ''])


def cmd_categories(client, args):
	"""List all categories."""
	_taxonomy_command(client, args, client.list_categories, 'categories',
		['ID', 'Name', 'Description'],
		lambda c: [str(c['id']), c['name'], c.get('description') or ''])


def cmd_tags(client, args):
	"""List all tags."""
	_taxonomy_command(client, args, client.list_tags, 'tags',
		['ID', 'Name'],
		lambda t: [str(t['id']), t['name']])

# vim: ts=4 sw=4 noet
