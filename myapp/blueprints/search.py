"""
Arcology - Search Blueprint

Global cross-item search using a prefix query syntax.
"""

import re
from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy import or_, and_, distinct

from ..extensions import db
from ..database import (
    Item, Artefact, Partition, ExtractedFile, FilesystemType,
    ArtefactProtection, ArtefactMastering,
)
from ..riscos_filetypes import lookup_filetype_hex

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/search', template_folder='templates')


def init_app(app):
    app.add_menu_item("Search", f"{ROUTENAME}.index", 50)


# =============================================================================
# Query parser
# =============================================================================

# Prefix aliases → canonical key
_ALIASES = {
    'file':       'filename',
    'filetype':   'type',
    'disc':       'label',
    'gnu':        'ident',
    'gnufile':    'ident',
    'filesystem': 'fs',
    'prot':       'protection',
}

# Regex: optional-quote value after colon, or bare word
_TOKEN_RE = re.compile(
    r'(\w+):"([^"]+)"'   # key:"quoted value"
    r'|(\w+):(\S+)'      # key:value
    r'|"([^"]+)"'        # "bare quoted phrase"
    r'|(\S+)',            # bare word
    re.UNICODE,
)

RESULT_LIMIT = 200


def parse_query(raw: str) -> dict:
    """Parse a search query string into a dict of {key: [values]}.

    Keys: md5, sha1, sha256, filename, path, type, ext, ident,
          label, fs, protection, mastering, text (bare words).
    """
    tokens: dict[str, list[str]] = {}

    for m in _TOKEN_RE.finditer(raw or ''):
        if m.group(1):   # key:"quoted value"
            key, val = m.group(1).lower(), m.group(2)
        elif m.group(3): # key:value
            key, val = m.group(3).lower(), m.group(4)
        elif m.group(5): # "bare quoted phrase"
            key, val = 'text', m.group(5)
        else:             # bare word
            key, val = 'text', m.group(6)

        key = _ALIASES.get(key, key)
        tokens.setdefault(key, []).append(val)

    return tokens


# =============================================================================
# Routes
# =============================================================================

@blueprint.route('/')
@login_required
def index():
    q = request.args.get('q', '').strip()
    tokens = parse_query(q)

    results = _run_search(tokens) if q else None

    known_protection_types = sorted(
        v for (v,) in db.session.query(distinct(ArtefactProtection.protection_type)).all()
    )
    known_mastering_types = sorted(
        v for (v,) in db.session.query(distinct(ArtefactMastering.mastering_type)).all()
    )

    return render_template(
        'search/index.html',
        q=q,
        tokens=tokens,
        results=results,
        RESULT_LIMIT=RESULT_LIMIT,
        FilesystemType=FilesystemType,
        known_protection_types=known_protection_types,
        known_mastering_types=known_mastering_types,
    )


# =============================================================================
# Search logic
# =============================================================================

def _ilike(col, val):
    """Case-insensitive substring filter, with * as wildcard."""
    pattern = val.replace('*', '%')
    if '%' not in pattern:
        pattern = f'%{pattern}%'
    return col.ilike(pattern)


def _resolve_riscos_type(val: str):
    """Return an ExtractedFile.risc_os_filetype filter for a type: term.

    Accepts either a 3-digit hex code (e.g. 'fea') or a human-readable RISC OS
    filetype name (e.g. 'Desktop').  Returns a SQLAlchemy column expression, or
    None if the value cannot be resolved to a known hex code.
    """
    hex_code = lookup_filetype_hex(val)
    if hex_code is not None:
        return ExtractedFile.risc_os_filetype == hex_code
    # Unknown name/code — match literally (may simply return no rows)
    return ExtractedFile.risc_os_filetype == val.lower()


def _run_search(tokens: dict) -> dict:
    """Execute queries and return result buckets."""
    results = {
        'files':             [],
        'artefacts':         [],
        'catalogue_items':   [],
        'truncated':         {},
    }

    has_file_terms = any(k in tokens for k in ('md5', 'sha1', 'sha256', 'filename', 'path', 'type', 'ext'))
    has_disc_terms = any(k in tokens for k in ('label', 'ident', 'fs'))
    has_prot_terms = 'protection' in tokens
    has_mast_terms = 'mastering' in tokens
    has_artefact_hash = any(k in tokens for k in ('md5', 'sha256')) and not has_file_terms
    has_text = 'text' in tokens

    # --- File search ---
    # Triggered by hash, filename, path, type, ext filters.
    # Also triggered by hash even if no other file term present (hash matches both files and artefacts).
    if has_file_terms or any(k in tokens for k in ('md5', 'sha1', 'sha256')):
        # Build per-key filter lists: OR within a key, AND across keys.
        # e.g. "path:!Killer type:feb" → path matches AND type matches
        #      "type:feb type:ffa"     → type is feb OR ffa
        per_key = {}
        for h in tokens.get('md5', []):
            per_key.setdefault('md5', []).append(ExtractedFile.md5 == h.lower())
        for h in tokens.get('sha1', []):
            per_key.setdefault('sha1', []).append(ExtractedFile.sha1 == h.lower())
        for h in tokens.get('sha256', []):
            per_key.setdefault('sha256', []).append(ExtractedFile.sha256 == h.lower())
        for v in tokens.get('filename', []):
            per_key.setdefault('filename', []).append(_ilike(ExtractedFile.filename, v))
        for v in tokens.get('path', []):
            per_key.setdefault('path', []).append(_ilike(ExtractedFile.path, v))
        for v in tokens.get('type', []):
            per_key.setdefault('type', []).append(_resolve_riscos_type(v))
        for v in tokens.get('ext', []):
            per_key.setdefault('ext', []).append(ExtractedFile.extension == v.lower())

        if per_key:
            combined = and_(*[or_(*clauses) for clauses in per_key.values()])
            q = (
                db.session.query(ExtractedFile, Partition, Artefact, Item)
                .join(Partition, ExtractedFile.partition_id == Partition.id)
                .join(Artefact, Partition.artefact_id == Artefact.id)
                .join(Item, Artefact.item_id == Item.id)
                .filter(combined)
                .filter(ExtractedFile.is_directory == False)
                .order_by(Item.name, Artefact.label, ExtractedFile.path)
                .limit(RESULT_LIMIT + 1)
                .all()
            )
            if len(q) > RESULT_LIMIT:
                results['truncated']['files'] = True
                q = q[:RESULT_LIMIT]
            results['files'] = q

    # --- Disc/partition search ---
    # Triggered by label, ident, fs filters.
    if has_disc_terms:
        per_key = {}
        for v in tokens.get('label', []):
            per_key.setdefault('label', []).append(_ilike(Partition.label, v))
        for v in tokens.get('ident', []):
            per_key.setdefault('ident', []).append(_ilike(Partition.gnu_file_type, v))
        for v in tokens.get('fs', []):
            try:
                fs_val = FilesystemType(v.lower())
                per_key.setdefault('fs', []).append(Partition.filesystem == fs_val)
            except ValueError:
                per_key.setdefault('fs', []).append(_ilike(Partition.container_format, v))

        if per_key:
            combined = and_(*[or_(*clauses) for clauses in per_key.values()])
            q = (
                db.session.query(Partition, Artefact, Item)
                .join(Artefact, Partition.artefact_id == Artefact.id)
                .join(Item, Artefact.item_id == Item.id)
                .filter(combined)
                .order_by(Item.name, Artefact.label, Partition.partition_index)
                .limit(RESULT_LIMIT + 1)
                .all()
            )
            if len(q) > RESULT_LIMIT:
                results['truncated']['artefacts'] = True
                q = q[:RESULT_LIMIT]
            # Store as artefact-level results with partition detail
            results['artefacts'].extend([
                {'type': 'partition', 'partition': p, 'artefact': a, 'item': i}
                for p, a, i in q
            ])

    # --- Protection indicator search ---
    if has_prot_terms:
        for prot_type in tokens.get('protection', []):
            q = (
                db.session.query(ArtefactProtection, Artefact, Item)
                .join(Artefact, ArtefactProtection.artefact_id == Artefact.id)
                .join(Item, Artefact.item_id == Item.id)
                .filter(ArtefactProtection.protection_type == prot_type.lower())
                .order_by(Item.name, Artefact.label)
                .all()
            )
            # Deduplicate by artefact id in Python (an artefact may have multiple
            # matching indicators, e.g. bad_crc on several tracks).
            seen = set()
            deduped = []
            for row in q:
                _, a, i = row
                if a.id not in seen:
                    seen.add(a.id)
                    deduped.append(row)
            if len(deduped) > RESULT_LIMIT:
                results['truncated']['artefacts'] = True
                deduped = deduped[:RESULT_LIMIT]
            results['artefacts'].extend([
                {'type': 'protection', 'protection_type': prot_type, 'artefact': a, 'item': i}
                for _, a, i in deduped
            ])

    # --- Mastering indicator search ---
    if has_mast_terms:
        for mast_type in tokens.get('mastering', []):
            q = (
                db.session.query(ArtefactMastering, Artefact, Item)
                .join(Artefact, ArtefactMastering.artefact_id == Artefact.id)
                .join(Item, Artefact.item_id == Item.id)
                .filter(ArtefactMastering.mastering_type == mast_type.lower())
                .order_by(Item.name, Artefact.label)
                .all()
            )
            seen = set()
            deduped = []
            for row in q:
                _, a, i = row
                if a.id not in seen:
                    seen.add(a.id)
                    deduped.append(row)
            if len(deduped) > RESULT_LIMIT:
                results['truncated']['artefacts'] = True
                deduped = deduped[:RESULT_LIMIT]
            results['artefacts'].extend([
                {'type': 'mastering', 'mastering_type': mast_type, 'artefact': a, 'item': i}
                for _, a, i in deduped
            ])

    # --- Artefact hash search ---
    # Hash terms not paired with file terms also search artefact-level hashes.
    if any(k in tokens for k in ('md5', 'sha256')):
        art_filters = []
        for h in tokens.get('md5', []):
            art_filters.append(Artefact.md5 == h.lower())
        for h in tokens.get('sha256', []):
            art_filters.append(Artefact.sha256 == h.lower())

        if art_filters:
            q = (
                db.session.query(Artefact, Item)
                .join(Item, Artefact.item_id == Item.id)
                .filter(or_(*art_filters))
                .order_by(Item.name, Artefact.label)
                .limit(RESULT_LIMIT + 1)
                .all()
            )
            if len(q) > RESULT_LIMIT:
                results['truncated']['artefacts'] = True
                q = q[:RESULT_LIMIT]
            results['artefacts'].extend([
                {'type': 'artefact_hash', 'artefact': a, 'item': i}
                for a, i in q
            ])

    # --- Free-text search on item name/description ---
    if has_text:
        text_filters = []
        for v in tokens.get('text', []):
            pattern = f'%{v}%'
            text_filters.append(Item.name.ilike(pattern))
            text_filters.append(Item.description.ilike(pattern))

        if text_filters:
            q = (
                Item.query
                .filter(or_(*text_filters))
                .order_by(Item.name)
                .limit(RESULT_LIMIT + 1)
                .all()
            )
            if len(q) > RESULT_LIMIT:
                results['truncated']['items'] = True
                q = q[:RESULT_LIMIT]
            results['catalogue_items'] = q

    return results


# vim: ts=4 sw=4 et
