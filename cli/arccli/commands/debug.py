"""Debug commands for diagnosing analysis and extraction issues."""

import json
from datetime import datetime
from ..formatting import print_json, print_table, truncate


def _format_tool(data):
	"""Format tool_name + tool_version into a single string."""
	tool = data['tool_name'] or ''
	if data.get('tool_version'):
		tool += f" {data['tool_version']}"
	return tool


def _analysis_row(a, artefact_label_width=30):
	"""Build a table row [uuid, artefact, type, status, tool, error] from an analysis dict."""
	artefact = a.get('artefact')
	artefact_label = truncate(artefact['label'], artefact_label_width) if artefact else ''
	status = a['status'].upper() if a['status'] == 'failed' else a['status']
	return [
		a['uuid'][:8],
		artefact_label,
		a['analysis_type'],
		status,
		a['tool_name'] or '-',
		truncate(a['error_message'] or '', 50),
	]


def cmd_debug_analysis(client, args):
	"""Show full details for a single analysis, including process logs and error traces."""
	data = client.get_analysis(args.uuid)

	if args.json:
		print_json(data)
		return

	status = data['status'].upper() if data['status'] == 'failed' else data['status']

	print(f"Analysis: {data['uuid']}")
	print(f"  Type:    {data['analysis_type']}")
	print(f"  Status:  {status}")
	if data['tool_name']:
		print(f"  Tool:    {_format_tool(data)}")
	if data['success'] is not None:
		print(f"  Success: {data['success']}")

	print(f"\n  Created:   {data['created_at']}")
	if data['started_at']:
		print(f"  Started:   {data['started_at']}")
	if data['completed_at']:
		print(f"  Completed: {data['completed_at']}")
		if data['started_at']:
			try:
				started = datetime.fromisoformat(data['started_at'])
				completed = datetime.fromisoformat(data['completed_at'])
				print(f"  Duration:  {(completed - started).total_seconds():.1f}s")
			except (ValueError, TypeError):
				pass

	if data['artefact_uuid']:
		print(f"\n  Artefact: {data['artefact_uuid']}")
	if data['summary']:
		print(f"\n  Summary: {data['summary']}")
	if data['error_message']:
		print(f"\n  Error: {data['error_message']}")

	# Parse details JSON for process output and exception traces
	details = _parse_details(data['details'])
	if not details:
		return

	if details.get('exception_trace'):
		print("\n--- Exception Trace ---")
		print(details['exception_trace'])

	process_output = details.get('process_output')
	if isinstance(process_output, dict):
		process_output = [process_output]
	if isinstance(process_output, list):
		for i, po in enumerate(process_output):
			_print_process_output(po, po.get('label', f'Process {i + 1}'))


def _parse_details(details_raw):
	"""Parse analysis details JSON, returning dict or None."""
	if not details_raw:
		return None
	try:
		return json.loads(details_raw) if isinstance(details_raw, str) else details_raw
	except (json.JSONDecodeError, TypeError):
		return None


def _print_process_output(po, label):
	"""Print a single process output block."""
	print(f"\n--- {label} ---")
	if po.get('command'):
		cmd = po['command']
		if isinstance(cmd, list):
			cmd = ' '.join(cmd)
		print(f"  Command:  {cmd}")
	if po.get('returncode') is not None:
		print(f"  Exit code: {po['returncode']}")
	if po.get('duration_seconds') is not None:
		print(f"  Duration:  {po['duration_seconds']:.1f}s")
	if po.get('stdout'):
		print("  stdout:")
		for line in po['stdout'].splitlines():
			print(f"    {line}")
	if po.get('stderr'):
		print("  stderr:")
		for line in po['stderr'].splitlines():
			print(f"    {line}")


def cmd_debug_errors(client, args):
	"""Show failed analyses for an artefact and all its descendants."""
	status_filter = None if args.all else 'failed'
	data = client.get_artefact_analyses_recursive(args.uuid, status=status_filter)

	if args.json:
		print_json(data)
		return

	print(f"Artefact: {data['artefact_label']} ({data['artefact_uuid'][:8]})")
	if args.all:
		print(f"  {data['total']} analyses ({data['failed']} failed)")
	else:
		print(f"  {data['failed']} failed analyses (use --all to see all {data['total']})")

	analyses = data['analyses']
	if not analyses:
		print("\n  No matching analyses found.")
		return

	print()
	print_table(
		['UUID', 'Artefact', 'Type', 'Status', 'Tool', 'Error'],
		[_analysis_row(a) for a in analyses],
	)


def cmd_debug_tree(client, args):
	"""Show the artefact derivation tree (artefact -> analyses -> derived artefacts)."""
	data = client.get_artefact_tree(args.uuid)

	if args.json:
		print_json(data)
		return

	_print_tree_node(data['artefact'], indent=0)


def _status_icon(analysis) -> str:
	"""Single-character marker for an analysis status line."""
	status = analysis['status']
	if status == 'completed' and analysis['success'] is not False:
		return '+'
	if status == 'failed':
		return 'X'
	if status in ('pending', 'running'):
		return '~'
	return '?'


def _error_suffix(analysis) -> str:
	"""Quoted, truncated error message for an analysis line, or ''."""
	if analysis['error_message']:
		return f'  "{truncate(analysis["error_message"], 60)}"'
	return ''


def _print_tree_node(artefact, indent):
	"""Recursively print a derivation tree node."""
	prefix = '  ' * indent
	print(f"{prefix}[{artefact['artefact_type']}] {artefact['label']} ({artefact['uuid'][:8]})")

	for analysis in artefact.get('analyses', []):
		status = analysis['status']
		tool = _format_tool(analysis)

		icon = _status_icon(analysis)
		error_suffix = _error_suffix(analysis)

		print(f"{prefix}  {icon} {analysis['analysis_type']}  {status}  {tool}{error_suffix}")

		for produced in analysis.get('produced_artefacts', []):
			_print_tree_node(produced, indent + 2)


def cmd_debug_processing_tree(client, args):
	"""Show the processing tree (artefact → path-grouped analyses → derived artefacts)."""
	data = client.get_processing_tree(args.uuid)

	if args.json:
		print_json(data)
		return

	counts = data.get('status_counts', {})
	total = data.get('total_count', 0)
	parts = [f"{total} analyses"]
	for status in ('completed', 'running', 'pending', 'failed'):
		n = counts.get(status, 0)
		if n:
			parts.append(f"{n} {status}")
	print(', '.join(parts))
	print()
	_print_processing_node(data['artefact'], indent=0)


def _print_processing_node(node, indent):
	"""Recursively print a processing tree node."""
	prefix = '  ' * indent
	print(f"{prefix}[{node['artefact_type']}] {node['label']} ({node['uuid'][:8]})")

	for analysis in node.get('analyses', []):
		_print_processing_analysis(analysis, prefix + '  ')

	path_tree = node.get('path_tree')
	if path_tree:
		_print_path_tree(path_tree, prefix + '  ')

	for child in node.get('children', []):
		_print_processing_node(child, indent + 2)


def _print_processing_analysis(analysis, prefix):
	"""Print a single analysis line."""
	status = analysis['status']
	icon = _status_icon(analysis)
	error_suffix = _error_suffix(analysis)
	print(f"{prefix}{icon} {analysis['analysis_type']}  {status}{error_suffix}")


def _print_path_tree(node, prefix, path=''):
	"""Recursively print the path-grouped sub-tree."""
	for name in sorted(node.get('children', {}).keys()):
		child = node['children'][name]
		child_path = f"{path}/{name}" if path else name
		print(f"{prefix}  {child_path}/")
		for analysis in child.get('analyses', []):
			_print_processing_analysis(analysis, prefix + '    ')
		_print_path_tree(child, prefix + '  ', child_path)


def cmd_debug_failures(client, args):
	"""Search for failed analyses across the system."""
	params = {
		'analysis_type': args.analysis_type,
		'tool_name': args.tool_name,
		'since': args.since,
		'until': args.until,
		'error': args.error,
		'page': args.page,
		'per_page': args.per_page,
	}
	data = client.search_failures(**params)

	if args.json:
		print_json(data)
		return

	failures = data['failures']
	if not failures:
		print("No failed analyses found.")
		return

	# Add date column to the shared row format
	rows = []
	for a in failures:
		row = _analysis_row(a, artefact_label_width=25)
		completed = (a['completed_at'] or '')[:10]  # date only
		row.append(completed)
		rows.append(row)

	print_table(['UUID', 'Artefact', 'Type', 'Status', 'Tool', 'Error', 'Date'], rows)
	print(f"\nShowing {len(failures)} of {data['total']} failures (page {data['page']}, {data['per_page']}/page)")

# vim: ts=4 sw=4 noet
