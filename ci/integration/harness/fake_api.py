"""In-memory fake of the Arcology web app, for worker integration tests.

``FakeServerAPI`` subclasses the *real* worker API client
(``arcworker.api.ArcologyAPI``) and overrides a single seam:
``_request_response``.  Every public method of the real client — ``get/put/
post/patch`` and the high-level helpers (``register_derived_artefact``,
``register_file_listing``, ``post_file_records``, ``queue_analysis``,
``update_analysis``) — funnels through ``_request_response``, so by replacing
just that one method we keep 100% of the real client logic (real hashing, real
storage writes, real wire payloads) and only simulate the *server* (Flask app +
database).

The server is a small dispatch table over (HTTP method, endpoint).  It records
a semantic event for every call (the test's behavioural assertion) and
simulates the web app's auto-analysis scheduling: registering a derived
artefact, or queueing an analysis, enqueues follow-on jobs exactly as
``queue_analyses_for_artefact`` would, using ``IT_ANALYSIS_MAP``.

Unknown (method, endpoint) pairs return a 500 whose ``raise_for_status()``
raises, so a coverage gap fails loudly instead of silently mis-simulating.
"""

import json
import re
from urllib.parse import parse_qsl, urlsplit
from arcworker.api import ArcologyAPI
from arcology_shared.enums import ArtefactType
from .analysis_map import IT_ANALYSIS_MAP


class FakeResponse:
    """Minimal stand-in for ``requests.Response``.

    Implements only what the real client touches: ``status_code``, ``json()``,
    ``raise_for_status()`` and ``text``.
    """

    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _HTTPError(f"{self.status_code} for fake request", self)


class _HTTPError(Exception):
    """Mimics requests.HTTPError closely enough for the client's handlers.

    The real ``update_analysis`` catches ``requests.HTTPError``; it also has a
    bare ``except Exception`` fallback, so this plain subclass is sufficient.
    """

    def __init__(self, message, response):
        super().__init__(message)
        self.response = response


def _slugify(value: str) -> str:
    """Lower-case, hyphenated slug fragment for deterministic output paths."""
    slug = re.sub(r'[^a-z0-9]+', '-', (value or '').lower()).strip('-')
    return slug or 'untitled'


class FakeServerAPI(ArcologyAPI):
    """Real worker client wired to an in-memory fake server."""

    def __init__(self, api_url, upload_dir, output_dir, api_key='', storage=None,
                 analysis_map=None):
        super().__init__(api_url, upload_dir, output_dir, api_key=api_key,
                         storage=storage)
        self.analysis_map = analysis_map or IT_ANALYSIS_MAP

        # Fixed item all artefacts belong to.
        self.item = {'uuid': 'it-item', 'slug': 'it-item'}

        # Server state.
        self.artefacts: dict[str, dict] = {}
        self.partitions: dict[str, dict] = {}
        self.analyses: dict[int, dict] = {}
        self.files: list[dict] = []
        self.files_by_id: dict[int, dict] = {}
        self.pending: list[int] = []      # FIFO of analysis ids awaiting a run
        self.events: list[dict] = []      # ordered recording

        self._art_seq = 0
        self._an_seq = 0
        self._part_seq = 0
        self._file_seq = 0

    # ── id helpers ──────────────────────────────────────────────────────
    def _next_artefact_uuid(self) -> str:
        self._art_seq += 1
        return f"it-art-{self._art_seq:04d}"

    def _next_partition_uuid(self) -> str:
        self._part_seq += 1
        return f"it-part-{self._part_seq:04d}"

    def _new_analysis(self, artefact_uuid: str, analysis_type: str,
                      hints_json: str | None) -> dict:
        self._an_seq += 1
        analysis = {
            'id': self._an_seq,
            'uuid': f"it-an-{self._an_seq:04d}",
            'slug': _slugify(analysis_type),
            'analysis_type': analysis_type,
            'artefact_uuid': artefact_uuid,
            'hints': hints_json,
            'status': 'pending',
        }
        self.analyses[analysis['id']] = analysis
        self.pending.append(analysis['id'])
        return analysis

    # ── seeding (used by the driver, not over the wire) ─────────────────
    def add_root_artefact(self, artefact: dict) -> dict:
        """Register the uploaded fixture as the root artefact (it-art-0000)."""
        artefact = dict(artefact)
        artefact.setdefault('uuid', 'it-art-0000')
        artefact.setdefault('slug', 'root')
        artefact['item'] = self.item
        self.artefacts[artefact['uuid']] = artefact
        return artefact

    def seed_analysis(self, artefact_uuid: str, analysis_type: str,
                      hints: dict | None = None) -> dict:
        """Queue an initial analysis (as ANALYSIS_MAP would on upload)."""
        hints_json = json.dumps(hints) if hints else None
        analysis = self._new_analysis(artefact_uuid, analysis_type, hints_json)
        self.events.append({
            'call': 'seed_analysis',
            'type': analysis_type,
            'artefact': artefact_uuid,
            'hints': hints or None,
        })
        return analysis

    # ── auto-analysis scheduling (mirrors queue_analyses_for_artefact) ──
    def _auto_queue(self, artefact_uuid: str, artefact_type_value: str,
                    skip: set[str], hints_json: str | None):
        try:
            artefact_type = ArtefactType(artefact_type_value)
        except ValueError:
            return
        for analysis_type in self.analysis_map.get(artefact_type, []):
            if analysis_type.name in skip:
                continue
            self._new_analysis(artefact_uuid, analysis_type.value, hints_json)

    # ── the single overridden seam ──────────────────────────────────────
    def _request_response(self, method, endpoint, *, data=None):
        method = method.lower()
        split = urlsplit(endpoint)
        path = split.path
        query = dict(parse_qsl(split.query))
        handler = self._route(method, path)
        if handler is None:
            return FakeResponse(500, {'error': f'unrouted {method} {path}'})
        status, payload = handler(data or {}, query)
        return FakeResponse(status, payload)

    def _route(self, method, path):
        for pattern, verb, fn in self._ROUTES:
            if verb != method:
                continue
            m = pattern.match(path)
            if m:
                return lambda data, query, _m=m, _fn=fn: _fn(self, data, query, *_m.groups())
        return None

    # ── endpoint handlers ───────────────────────────────────────────────
    # Each takes (data, query, *path_groups).  Read endpoints (GET) do not
    # record an event — only state-changing calls and queue operations do, so
    # the golden's event log stays focused on behaviour rather than queries.
    def _put_analysis(self, data, query, analysis_id):
        analysis = self.analyses.get(int(analysis_id))
        if analysis is None:
            return 404, {'error': 'analysis not found'}
        analysis.update(data)
        if data.get('success') is True or data.get('status') == 'complete':
            analysis['status'] = 'complete'
        elif data.get('status'):
            analysis['status'] = data['status']
        self.events.append({
            'call': 'update_analysis',
            'type': analysis['analysis_type'],
            'status': analysis['status'],
            'summary': data.get('summary'),
            'output_path': data.get('output_path'),
            'tool_name': data.get('tool_name'),
            'error_message': data.get('error_message'),
            'details': data.get('details'),
        })
        return 200, dict(analysis)

    def _post_partition(self, data, query, artefact_uuid):
        uuid = self._next_partition_uuid()
        partition = {
            'uuid': uuid,
            'artefact_uuid': artefact_uuid,
            'partition_index': data.get('partition_index', 0),
            'filesystem': data.get('filesystem'),
            'container_format': data.get('container_format'),
            'label': data.get('label'),
            'archive_comment': data.get('archive_comment'),
            'total_files': data.get('total_files'),
        }
        partition['slug'] = _slugify(str(partition['partition_index']))
        self.partitions[uuid] = partition
        self.events.append({
            'call': 'register_partition',
            'artefact': artefact_uuid,
            'filesystem': partition['filesystem'],
            'container_format': partition['container_format'],
            'label': partition['label'],
            'archive_comment': partition['archive_comment'],
            'total_files': partition['total_files'],
        })
        return 200, dict(partition)

    def _post_files(self, data, query, partition_uuid):
        # Mirror the web app's add_files: prefix a child file's path with its
        # parent archive's path (when the parent is_archive), dedup by
        # (partition, path), and assign an integer id.  Records are sorted by
        # final path before id assignment so ids are deterministic across runs
        # (the worker enumerates via rglob, whose order is not stable, but ids
        # are opaque and used only relationally, so ordering them by path is a
        # harmless way to make the golden reproducible).
        records = data.get('files', [])
        existing_paths = {f['path'] for f in self.files
                          if f['partition_uuid'] == partition_uuid}
        prepared = []
        for rec in records:
            path = rec['path']
            parent_id = rec.get('parent_file_id')
            if parent_id is not None:
                parent = self.files_by_id.get(int(parent_id))
                if parent and parent.get('is_archive'):
                    if not path.startswith(parent['path'] + '/'):
                        path = parent['path'] + '/' + path
            prepared.append((path, rec))

        added = 0
        skipped = 0
        for path, rec in sorted(prepared, key=lambda pr: pr[0]):
            if path in existing_paths:
                skipped += 1
                continue
            existing_paths.add(path)
            self._file_seq += 1
            stored = dict(rec)
            stored['id'] = self._file_seq
            stored['path'] = path
            stored['partition_uuid'] = partition_uuid
            stored['is_archive'] = False
            stored.setdefault('archive_format', None)
            stored.setdefault('archive_comment', None)
            stored.setdefault('is_known', False)
            stored.setdefault('extraction_depth', rec.get('extraction_depth', 0))
            self.files.append(stored)
            self.files_by_id[stored['id']] = stored
            added += 1

        event = {'call': 'post_files', 'partition': partition_uuid, 'added': added}
        if skipped:
            event['skipped'] = skipped
        self.events.append(event)
        response = {'added': added}
        if skipped:
            response['skipped'] = skipped
        return 200, response

    def _get_partition_files(self, data, query, partition_uuid):
        files = [f for f in self.files if f['partition_uuid'] == partition_uuid]
        if query.get('show_known', 'false').lower() != 'true':
            files = [f for f in files if not f.get('is_known')]
        if 'is_archive' in query:
            want = query['is_archive'].lower() == 'true'
            files = [f for f in files if bool(f.get('is_archive')) == want]
        if 'path_prefix' in query:
            prefix = query['path_prefix'] + '/'
            files = [f for f in files if f['path'].startswith(prefix)]
        if 'extraction_depth' in query:
            depth = int(query['extraction_depth'])
            files = [f for f in files if f.get('extraction_depth', 0) == depth]
        files = sorted(files, key=lambda f: f['path'])
        return 200, {
            'files': [dict(f) for f in files],
            'total': len(files),
            'page': 1,
            'per_page': len(files) or 1,
            'pages': 1,
        }

    def _get_partition(self, data, query, partition_uuid):
        partition = self.partitions.get(partition_uuid)
        if partition is None:
            return 404, {'error': 'partition not found'}
        return 200, {'partition': dict(partition)}

    def _get_file(self, data, query, file_id):
        f = self.files_by_id.get(int(file_id))
        if f is None:
            return 404, {'error': 'file not found'}
        return 200, dict(f)

    def _post_mark_archive(self, data, query, file_id):
        f = self.files_by_id.get(int(file_id))
        if f is None:
            return 404, {'error': 'file not found'}
        f['is_archive'] = data.get('is_archive', True)
        if 'archive_format' in data:
            f['archive_format'] = data.get('archive_format')
        if 'archive_comment' in data:
            f['archive_comment'] = data.get('archive_comment')
        self.events.append({
            'call': 'mark_archive',
            'path': f['path'],
            'archive_format': f.get('archive_format'),
            'archive_comment': f.get('archive_comment'),
        })
        return 200, dict(f)

    def _post_queue_analysis(self, data, query, artefact_uuid):
        analysis_type = data.get('analysis_type')
        hints_json = data.get('hints')   # already a JSON string on the wire
        analysis = self._new_analysis(artefact_uuid, analysis_type, hints_json)
        self.events.append({
            'call': 'queue_analysis',
            'type': analysis_type,
            'artefact': artefact_uuid,
            'hints': json.loads(hints_json) if hints_json else None,
        })
        return 200, dict(analysis)

    def _post_produce_artefact(self, data, query, analysis_id):
        uuid = self._next_artefact_uuid()
        artefact = {
            'uuid': uuid,
            'slug': _slugify(data.get('label', 'derived')),
            'item': self.item,
            'label': data.get('label'),
            'original_filename': data.get('original_filename'),
            'storage_path': data.get('storage_path'),
            'blob_storage_path': data.get('blob_storage_path'),
            'storage_directory': data.get('storage_directory', 'outputs'),
            'artefact_type': data.get('artefact_type'),
            'file_size': data.get('file_size'),
            'md5': data.get('md5'),
            'sha256': data.get('sha256'),
            'derived_from_analysis_id': int(analysis_id),
        }
        self.artefacts[uuid] = artefact

        if data.get('auto_analyse', True):
            skip = set(data.get('skip_analyses') or [])
            hints = data.get('hints')
            hints_json = json.dumps(hints) if hints else None
            self._auto_queue(uuid, artefact['artefact_type'], skip, hints_json)

        self.events.append({
            'call': 'produce_artefact',
            'artefact_type': artefact['artefact_type'],
            'label': artefact['label'],
            'auto_analyse': bool(data.get('auto_analyse', True)),
            'skip_analyses': data.get('skip_analyses') or None,
        })
        return 200, {'artefact': dict(artefact)}

    # Ordered (pattern, method, handler).  First match wins.
    _ROUTES = [
        (re.compile(r'^/analysis/(\d+)$'), 'put', _put_analysis),
        (re.compile(r'^/analysis/(\d+)/produce-artefact$'), 'post', _post_produce_artefact),
        (re.compile(r'^/artefacts/([^/]+)/partitions$'), 'post', _post_partition),
        (re.compile(r'^/artefacts/([^/]+)/analysis$'), 'post', _post_queue_analysis),
        (re.compile(r'^/partitions/([^/]+)/files$'), 'post', _post_files),
        (re.compile(r'^/partitions/([^/]+)/files$'), 'get', _get_partition_files),
        (re.compile(r'^/partitions/([^/]+)$'), 'get', _get_partition),
        (re.compile(r'^/files/(\d+)$'), 'get', _get_file),
        (re.compile(r'^/files/(\d+)/mark_archive$'), 'post', _post_mark_archive),
    ]

# vim: ts=4 sw=4 et
