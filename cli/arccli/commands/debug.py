"""Debug commands for diagnosing analysis and extraction issues."""

import json
import sys

from ..formatting import print_json, print_table, truncate


def cmd_debug_analysis(client, args):
	"""Show full details for a single analysis, including process logs and error traces."""
	data = client.get_analysis(args.uuid)

	if args.json:
		print_json(data)
		return

	status = data.get('status', 'unknown')
	status_display = status.upper() if status == 'failed' else status

	print(f"Analysis: {data['uuid']}")
	print(f"  Type:    {data['analysis_type']}")
	print(f"  Status:  {status_display}")
	if data.get('tool_name'):
		tool = data['tool_name']
		if data.get('tool_version'):
			tool += f" {data['tool_version']}"
		print(f"  Tool:    {tool}")
	if data.get('success') is not None:
		print(f"  Success: {data['success']}")

	# Timestamps and duration
	print(f"\n  Created:   {data.get('created_at', '-')}")
	if data.get('started_at'):
		print(f"  Started:   {data['started_at']}")
	if data.get('completed_at'):
		print(f"  Completed: {data['completed_at']}")
	if data.get('started_at') and data.get('completed_at'):
		from datetime import datetime
		try:
			started = datetime.fromisoformat(data['started_at'])
			completed = datetime.fromisoformat(data['completed_at'])
			duration = (completed - started).total_seconds()
			print(f"  Duration:  {duration:.1f}s")
		except (ValueError, TypeError):
			pass

	# Artefact info
	if data.get('artefact_uuid'):
		print(f"\n  Artefact: {data['artefact_uuid']}")

	# Summary
	if data.get('summary'):
		print(f"\n  Summary: {data['summary']}")

	# Error
	if data.get('error_message'):
		print(f"\n  Error: {data['error_message']}")

	# Parse details JSON for process output and exception traces
	details_raw = data.get('details')
	if details_raw:
		try:
			details = json.loads(details_raw) if isinstance(details_raw, str) else details_raw
		except (json.JSONDecodeError, TypeError):
			details = None

		if details:
			# Exception trace
			if details.get('exception_trace'):
				print(f"\n--- Exception Trace ---")
				print(details['exception_trace'])

			# Process output entries
			process_output = details.get('process_output')
			if isinstance(process_output, dict):
				process_output = [process_output]
			if isinstance(process_output, list):
				for i, po in enumerate(process_output):
					label = po.get('label', f'Process {i + 1}')
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
						print(f"  stdout:")
						for line in po['stdout'].splitlines():
							print(f"    {line}")
					if po.get('stderr'):
						print(f"  stderr:")
						for line in po['stderr'].splitlines():
							print(f"    {line}")


def cmd_debug_errors(client, args):
	"""Show failed analyses for an artefact and all its descendants."""
	status_filter = None if args.all else 'failed'
	data = client.get_artefact_analyses_recursive(args.uuid, status=status_filter)

	if args.json:
		print_json(data)
		return

	analyses = data.get('analyses', [])
	total = data.get('total', len(analyses))
	failed = data.get('failed', 0)

	print(f"Artefact: {data.get('artefact_label', args.uuid)} ({data.get('artefact_uuid', args.uuid)[:8]})")
	if args.all:
		print(f"  {total} analyses ({failed} failed)")
	else:
		print(f"  {failed} failed analyses (use --all to see all {total})")

	if not analyses:
		print("\n  No matching analyses found.")
		return

	rows = []
	for a in analyses:
		artefact_info = ''
		if a.get('artefact'):
			artefact_info = truncate(a['artefact'].get('label', a['artefact'].get('uuid', '')[:8]), 30)
		rows.append([
			a.get('uuid', '')[:8],
			artefact_info,
			a.get('analysis_type', ''),
			a.get('status', '').upper() if a.get('status') == 'failed' else a.get('status', ''),
			a.get('tool_name') or '-',
			truncate(a.get('error_message') or '', 50),
		])

	print()
	print_table(['UUID', 'Artefact', 'Type', 'Status', 'Tool', 'Error'], rows)


def cmd_debug_tree(client, args):
	"""Show the artefact derivation tree (artefact -> analyses -> derived artefacts)."""
	data = client.get_artefact_tree(args.uuid)

	if args.json:
		print_json(data)
		return

	artefact = data.get('artefact', {})
	_print_tree_node(artefact, indent=0)


def _print_tree_node(artefact, indent):
	"""Recursively print a derivation tree node."""
	prefix = '  ' * indent
	atype = artefact.get('artefact_type', '?')
	label = artefact.get('label', artefact.get('original_filename', '?'))
	uuid_short = artefact.get('uuid', '')[:8]
	print(f"{prefix}[{atype}] {label} ({uuid_short})")

	for analysis in artefact.get('analyses', []):
		status = analysis.get('status', 'unknown')
		atype = analysis.get('analysis_type', '?')
		tool = analysis.get('tool_name') or ''
		if analysis.get('tool_version'):
			tool += f" {analysis['tool_version']}"

		if status == 'completed' and analysis.get('success') is not False:
			icon = '+'
		elif status == 'failed':
			icon = 'X'
		elif status in ('pending', 'running'):
			icon = '~'
		else:
			icon = '?'

		error_suffix = ''
		if analysis.get('error_message'):
			error_suffix = f'  "{truncate(analysis["error_message"], 60)}"'

		print(f"{prefix}  {icon} {atype}  {status}  {tool}{error_suffix}")

		for produced in analysis.get('produced_artefacts', []):
			_print_tree_node(produced, indent + 2)


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

	failures = data.get('failures', [])
	total = data.get('total', len(failures))
	page = data.get('page', 1)
	per_page = data.get('per_page', 50)

	if not failures:
		print("No failed analyses found.")
		return

	rows = []
	for a in failures:
		artefact_info = ''
		if a.get('artefact'):
			artefact_info = truncate(a['artefact'].get('label', ''), 25)
		completed = a.get('completed_at', '')
		if completed:
			completed = completed[:10]  # date only
		rows.append([
			a.get('uuid', '')[:8],
			artefact_info,
			a.get('analysis_type', ''),
			a.get('tool_name') or '-',
			truncate(a.get('error_message') or '', 50),
			completed,
		])

	print_table(['UUID', 'Artefact', 'Type', 'Tool', 'Error', 'Date'], rows)
	print(f"\nShowing {len(failures)} of {total} failures (page {page}, {per_page}/page)")

# vim: ts=4 sw=4 noet
