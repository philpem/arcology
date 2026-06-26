"""Golden-output normalisation.

Strips non-deterministic and environment-specific noise from the result
document so two runs on different machines (or after a tool version bump that
doesn't change behaviour) produce byte-identical JSON.

Rules:
  * Replace the absolute uploads/outputs/work roots with stable placeholders
    wherever they appear (including inside JSON-encoded ``details`` and hints).
  * Decode JSON-encoded ``hints`` and ``details`` strings to objects so the
    golden is readable and field-order-stable.
  * Drop volatile fields: ``modified_time``, ``process_output``,
    ``exception_trace``, and any ``file(1)`` version detail.
  * Sort file records by path.

Fake-issued ids (``it-art-0001`` …) are already deterministic, so no id
placeholder rewriting is needed.
"""

import json
import re

# Keys whose values vary by tool/library version or run, dropped at ANY depth
# (PARTITION_DETECT nests sfdisk/file output deep inside `details`).
_VOLATILE_DETAIL_KEYS = frozenset((
    'process_output', 'exception_trace', 'file_output', 'file_type_raw',
    'file_type',        # `file(1)` / libmagic output — version-dependent string
    'modified_time', 'created_time',
))

# `... [file: <libmagic output>]` clause appended to some summaries; the magic
# string drifts across libmagic versions, so strip from `[file:` to end.
_FILE_CLAUSE_RE = re.compile(r'\s*\[file:.*$')


def _replace_roots(text: str, roots: dict[str, str]) -> str:
    for actual, placeholder in roots.items():
        text = text.replace(actual, placeholder)
    return text


def _clean_scalar(value, roots):
    return _replace_roots(value, roots) if isinstance(value, str) else value


def _decode_maybe_json(value):
    """Return the parsed object if *value* is a JSON string, else *value*."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in '{[':
        return value
    try:
        return json.loads(stripped)
    except ValueError:
        return value


def _clean_details(details, roots):
    return _clean_value(_decode_maybe_json(details), roots)


def _clean_value(value, roots):
    """Recursively replace roots and drop volatile keys at any depth."""
    if isinstance(value, dict):
        return {
            k: _clean_value(v, roots)
            for k, v in value.items()
            if k not in _VOLATILE_DETAIL_KEYS
        }
    if isinstance(value, list):
        return [_clean_value(v, roots) for v in value]
    return _clean_scalar(value, roots)


def _clean_summary(summary, roots):
    if not isinstance(summary, str):
        return summary
    return _replace_roots(_FILE_CLAUSE_RE.sub('', summary), roots)


def _clean_event(event, roots):
    cleaned = {}
    for key, val in event.items():
        if key == 'details':
            cleaned[key] = _clean_details(val, roots) if val is not None else None
        elif key == 'hints':
            cleaned[key] = _clean_value(_decode_maybe_json(val), roots)
        elif key == 'summary':
            cleaned[key] = _clean_summary(val, roots)
        else:
            cleaned[key] = _clean_value(val, roots)
    return cleaned


def _clean_file_record(record, roots):
    keep = ('id', 'path', 'filename', 'extension', 'file_size', 'md5', 'sha1', 'sha256',
            'is_directory', 'is_archive', 'archive_format', 'risc_os_filetype',
            'load_address', 'exec_address', 'attributes', 'parent_file_id',
            'extraction_depth', 'partition_uuid')
    cleaned = {}
    for key in keep:
        if key in record:
            cleaned[key] = _clean_scalar(record[key], roots)
    return cleaned


def _clean_artefact(artefact, roots):
    drop = ('item',)
    cleaned = {}
    for key, val in artefact.items():
        if key in drop:
            continue
        cleaned[key] = _clean_value(_decode_maybe_json(val) if key == 'hints' else val, roots)
    return cleaned


def normalise(result: dict, roots: dict[str, str]) -> dict:
    """Return a normalised copy of the driver's result document."""
    out = {'case': result['case']}
    out['events'] = [_clean_event(e, roots) for e in result.get('events', [])]
    out['partitions'] = [
        {
            **{k: _clean_value(v, roots) for k, v in p.items() if k != 'files'},
            'files': sorted(
                (_clean_file_record(f, roots) for f in p.get('files', [])),
                key=lambda r: r.get('path', ''),
            ),
        }
        for p in result.get('partitions', [])
    ]
    out['artefacts'] = [_clean_artefact(a, roots) for a in result.get('artefacts', [])]
    out['final_queue'] = [_clean_value(q, roots) for q in result.get('final_queue', [])]
    out['output_tree'] = sorted(
        _replace_roots(p, roots) for p in result.get('output_tree', [])
    )
    return out

# vim: ts=4 sw=4 et
