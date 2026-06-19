"""
arco hashdb generate-riscos — Generate a RISC OS HashDB JSON from items in Arcology.

Scans selected items (by tag, platform, or explicit UUID), fetches their
extracted file listings, identifies RISC OS application directories (those whose
top-level component begins with '!'), parses each application's !Run Obey file to
find the program it launches, and emits HashDB-compatible JSON ready for
`arco hashdb import`.

Classification (Mandatory == is_required, Optional == not is_required):

  * The file(s) launched by !Run — an Absolute/Module run via Run/RMRun/RMLoad,
    or a BASIC image via `BASIC -quit <file>` — are marked **Mandatory**,
    provided the file is *unique*: it appears in only one application across the
    selected items, and is not already present in an active hash database
    (unless --include-known).  With --global-check, uniqueness is additionally
    required across the entire catalogue, not just the selected items.
  * !Run and !Boot themselves are always **Optional**: their bytes legitimately
    vary (e.g. innoculation against the Extend virus appends a commented 0xFF
    byte at EOF), so they must never gate a product match.
  * Shared files (identical content in more than one application) are added as
    **Optional** everywhere, so they add confidence but never gate a match.
  * If !Run cannot be parsed, a filetype heuristic is used as a fallback to pick
    Mandatory candidates (still gated by uniqueness).

Applications that appear on many artefacts (the original distribution plus copies
bundled elsewhere — !ArcFS, !System, !Fonts, !Scrap, !Boot, SerialDev, …) can be
pinned to a "golden" source with --canonical-sources (a "<app-dir> <regex>" rule
file); copies on non-matching artefacts are dropped.  --dump-canonical writes an
editable starting point listing every application found on more than one artefact.

This tool is RISC OS-specific but not collection-specific: use it for the arcarc
archive, a batch of museum disc images, or any other RISC OS material.
"""

import json
import logging
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from ..client import ArcologyClient

log = logging.getLogger('arco.hashdb.generate-riscos')


# ---------------------------------------------------------------------------
# RISC OS filetype constants for fallback classification
# ---------------------------------------------------------------------------

FT_BASIC = 'ffb'
FT_ABSOLUTE = 'ff8'
FT_TEMPLATE = 'fec'
FT_SPRITE = 'ff9'
FT_MODULE = 'ffa'
FT_UTILITY = 'ffc'

REQUIRED_FILETYPES = {FT_BASIC, FT_ABSOLUTE, FT_TEMPLATE, FT_MODULE, FT_UTILITY}


# ---------------------------------------------------------------------------
# Artefact label parsing (for multi-disc detection / version)
# ---------------------------------------------------------------------------

_DISC_RE = re.compile(
    r'(?:\s*\(Disk\s+(\d+)\s+of\s+(\d+)\))',
    re.IGNORECASE,
)

_VERSION_RE = re.compile(
    r'\(v([^)]+)\)',
    re.IGNORECASE,
)

# Strip trailing TOSEC/Arcarc parenthetical metadata groups such as
# (1993)(Oak Solutions)(GB) or [b] from the end of a label, stopping at a
# version tag like (v1.5) so that is preserved.
_TOSEC_TRAILING_RE = re.compile(
    r'(\s*(?!\(v[^)]*\))[\(\[][^\)\]]*[\)\]])+\s*$',
    re.IGNORECASE,
)

# Strip space-preceded parenthetical groups remaining after the trailing strip,
# e.g. the mid-label freeware/language tag in "100Chess (FR) 1.05".
# Version tags like (v1.5) are excluded via negative lookahead.
# Groups with no space before the bracket (e.g. Word(NG)) are also unaffected.
_TOSEC_TAG_RE = re.compile(
    r'\s+(?!\(v[^)]*\))[\(\[][^\)\]]*[\)\]]',
    re.IGNORECASE,
)

# Normalise parenthesised version tags to bare form: (v1.5) -> v1.5.
_TOSEC_VERSION_UNWRAP_RE = re.compile(r'\(v([^)]*)\)', re.IGNORECASE)

# Collapse runs of spaces left behind after stripping.
_MULTI_SPACE_RE = re.compile(r'  +')

# Known archive/disc-image extensions that may appear in artefact labels
# when the label was derived from the original filename.
_KNOWN_EXTS_RE = re.compile(
    r'\.(zip|adf|ssd|dsd|adl|adm|ads|img|hfe|scp|dfi|a2r|arc|zoo'
    r'|gz|bz2|xz|lzh|lha|tar|7z|rar|dsk|ima|st|msa)$',
    re.IGNORECASE,
)


def strip_tosec_metadata(label: str) -> str:
    """Strip TOSEC/Arcarc parenthetical tag groups from a label and normalise.

    1. Strip trailing groups (catches chained ``(1992)(Publisher)`` with no
       space between them), stopping at a ``(v...)`` version tag.
    2. Strip any remaining space-preceded non-version groups (catches
       mid-label tags like the freeware marker in ``100Chess (FR) 1.05``).
    3. Unwrap ``(v...)`` to bare ``v...`` so ``Elite (v1.5)`` and
       ``Elite v1.5`` both produce the same title.
    4. Collapse any doubled spaces left by the stripping steps.

    Groups with no space before the opening bracket are preserved, e.g.
    ``Word(NG) 1.0`` stays intact.
    """
    result = _TOSEC_TRAILING_RE.sub('', label)
    result = _TOSEC_TAG_RE.sub(' ', result)
    result = _TOSEC_VERSION_UNWRAP_RE.sub(r'v\1', result)
    result = _MULTI_SPACE_RE.sub(' ', result)
    return result.strip()


def parse_artefact_label(label: str) -> dict:
    """Parse an Arcarc/TOSEC-style artefact label.

    The label is the per-artefact product name (set at upload time by the
    Arcarc/TOSEC filename parser), e.g. ``BeebIt (FR) 0.53 (Disk 1 of 2)``.
    Returns its disc number/total, any ``(vX)`` version, and ``clean_name`` —
    the label with the ``(Disk N of M)`` suffix removed — which is the right
    basis for a product title.
    """
    result = {'disc_number': None, 'disc_total': None, 'version': None}

    m = _DISC_RE.search(label)
    if m:
        result['disc_number'] = int(m.group(1))
        result['disc_total'] = int(m.group(2))

    m = _VERSION_RE.search(label)
    if m:
        result['version'] = m.group(1)

    # The clean product name drops the disc parenthetical (_DISC_RE eats the
    # leading whitespace too); collapse any doubled spaces left behind.
    clean = re.sub(r'\s{2,}', ' ', _DISC_RE.sub('', label)).strip()
    result['clean_name'] = clean

    return result


# ---------------------------------------------------------------------------
# Application directory identification
# ---------------------------------------------------------------------------

def find_app_directories(files: list[dict], root_mode: str,
                         disc_label: str) -> dict[str, list[dict]]:
    """Group extracted files by application directory (top-level '!' component)."""
    app_dirs: dict[str, list[dict]] = {}
    root_files: list[dict] = []

    for f in files:
        if f.get('is_directory'):
            continue

        path = f.get('path', '')
        parts = path.split('/') if '/' in path else [path]

        if len(parts) > 1 and parts[0].startswith('!'):
            app_dir = parts[0]
            app_dirs.setdefault(app_dir, []).append(f)
        else:
            root_files.append(f)

    if root_files and root_mode != 'skip':
        key = f'[Root] {disc_label}' if root_mode == 'flag' else disc_label
        app_dirs[key] = root_files

    return app_dirs


# ---------------------------------------------------------------------------
# !Run Obey file parsing
# ---------------------------------------------------------------------------

_LAUNCH_CMDS = {'run', 'rmrun', 'rmload'}

# Sentinel standing in for the application (Obey) directory during expansion.
_ROOT = '\x00'

# System variables that always resolve to the application directory.
_OBEY_VARS = {'obey$dir', 'obey$path'}

_SET_RE = re.compile(r'^Set(?:Macro|Eval)?\s+(\S+)\s+(.+)$', re.IGNORECASE)
_VAR_RE = re.compile(r'<([^>]+)>')


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        return token[1:-1]
    return token


def _build_var_map(lines: list[str]) -> dict[str, str]:
    """Collect `Set <name> <value>` assignments (case-insensitive names)."""
    varmap: dict[str, str] = {}
    for line in lines:
        m = _SET_RE.match(line)
        if m:
            varmap[m.group(1).lower()] = _strip_quotes(m.group(2).strip())
    return varmap


def _expand(token: str, varmap: dict[str, str], depth: int = 0) -> str:
    """Expand <var> references, anchoring application-directory variables on the
    _ROOT sentinel.  Handles the `Set App$Dir <Obey$Dir>` indirection pattern
    (and chains/subdirectories thereof).

    Strict resolution: only Obey$Dir/Obey$Path and variables explicitly Set in
    the Obey file count as in-app.  A variable the file never Set is assumed to
    be external and is left unresolved, so its target is not treated as an
    in-app file.
    """
    if depth > 16:  # guard against pathological/circular definitions
        return token

    def repl(m):
        name = m.group(1).lower()
        if name in _OBEY_VARS:
            return _ROOT
        if name in varmap:
            return _expand(varmap[name], varmap, depth + 1)
        return m.group(0)  # unset variable -> external, left unresolved

    return _VAR_RE.sub(repl, token)


def _resolve_obey_path(token: str, varmap: dict[str, str] | None = None) -> str | None:
    """Resolve an Obey file reference to a path relative to the app directory.

    '<Obey$Dir>.!RunImage'                      -> '!RunImage'
    '<App$Dir>.bin.loader'                       -> 'bin/loader'
    '<App$Dir>.loader' with Set App$Dir=<..>.Bin -> 'bin/loader'
    '<System$Dir>.Modules.Foo'                   -> None (external)

    Returns None for tokens that are not in-app file references (options,
    argument placeholders, external/unresolved variables).
    """
    token = _strip_quotes(token).strip()
    if not token or token.startswith('-') or token.startswith('%'):
        return None

    expanded = _expand(token, varmap or {})
    if _ROOT in expanded:
        rest = expanded.split(_ROOT, 1)[1].lstrip('.')
        return rest.replace('.', '/') or None
    if '<' not in token:
        # A bare relative path (no variable) is taken to be app-relative.
        return token.replace('.', '/')
    # An unresolved/external variable reference is not an in-app file.
    return None


def _clean_obey_lines(text: str) -> list[str]:
    """Split Obey-file text into non-blank, non-comment, star-stripped lines."""
    cleaned: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('|'):  # blank or comment
            continue
        if line.startswith('*'):              # explicit star command
            line = line[1:].strip()
        if line:
            cleaned.append(line)
    return cleaned


def parse_run_obey(text: str, extra_vars: dict[str, str] | None = None) -> list[str]:
    """Parse a RISC OS !Run Obey file, returning the app-relative paths of the
    files it launches (lowercased, '/'-separated).  Best-effort.

    A first pass collects `Set`/`SetMacro` variable assignments so that path
    references built from them (the common `Set App$Dir <Obey$Dir>` idiom, and
    subdirectory/chained variants) resolve to the correct app-relative path.

    *extra_vars* pre-seeds the variable map (e.g. with `Set`s harvested from
    !Boot, which runs before !Run); assignments in this file take precedence.
    """
    cleaned = _clean_obey_lines(text)
    varmap = dict(extra_vars or {})
    varmap.update(_build_var_map(cleaned))

    targets: list[str] = []

    def add(token):
        if not token:
            return
        rel = _resolve_obey_path(token, varmap)
        if rel:
            targets.append(rel.lower())

    for line in cleaned:
        tokens = line.split()
        if not tokens:
            continue
        cmd = tokens[0].lower()

        if cmd in _LAUNCH_CMDS:
            if len(tokens) > 1:
                add(tokens[1])
        elif cmd == 'rmensure':
            # RMEnsure <module> <version> <action ...>; capture an RM* target.
            lowered = [t.lower() for t in tokens]
            for kw in ('rmload', 'rmrun'):
                if kw in lowered:
                    idx = lowered.index(kw)
                    if idx + 1 < len(tokens):
                        add(tokens[idx + 1])
        elif cmd == 'basic':
            # BASIC [-quit|-load] <file> ...  (the file arg is the BASIC image)
            lowered = [t.lower() for t in tokens]
            target = None
            for opt in ('-quit', '-load'):
                if opt in lowered:
                    idx = lowered.index(opt)
                    if idx + 1 < len(tokens):
                        target = tokens[idx + 1]
                    break
            if target is None:
                for t in tokens[1:]:
                    if not t.startswith('-'):
                        target = t
                        break
            add(target)

    # De-duplicate, preserving order.
    seen = set()
    result = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


# ---------------------------------------------------------------------------
# Required/Optional classification
# ---------------------------------------------------------------------------

def _app_relative(app_dir_name: str, path: str) -> str:
    """Return *path* relative to *app_dir_name* (or the path itself)."""
    prefix = app_dir_name + '/'
    return path[len(prefix):] if path.startswith(prefix) else path


def _filetype_mandatory(f: dict) -> bool:
    """Fallback heuristic: is this file an executable that should be Mandatory?"""
    filetype = f.get('risc_os_filetype')
    if filetype in REQUIRED_FILETYPES:
        return True
    filename = f.get('filename', '')
    if filetype == FT_SPRITE and re.match(r'^!Sprites\d*$', filename, re.IGNORECASE):
        return True
    return False


def _find_app_file(app_files: list[dict], leaf: str) -> dict | None:
    """Find a file in the application directory by leaf filename (e.g. '!run')."""
    for f in app_files:
        if f.get('filename', '').lower() == leaf and not f.get('is_directory'):
            return f
    return None


def get_launched_set(client: ArcologyClient, app_files: list[dict],
                     verbose: bool = False) -> set[str]:
    """Locate the application's !Run file, download it, and parse the set of
    app-relative paths (lowercased) that it launches.  Empty set if no !Run.

    !Boot is consulted too (it runs before !Run and frequently sets the app's
    path variable): its `Set` assignments seed the variable map used to resolve
    !Run's references, so a variable defined only in !Boot still resolves.
    """
    run_file = _find_app_file(app_files, '!run')
    if not run_file or not run_file.get('uuid'):
        return set()

    boot_vars: dict[str, str] = {}
    boot_file = _find_app_file(app_files, '!boot')
    if boot_file and boot_file.get('uuid'):
        try:
            boot_data = client.download_extracted_file_bytes(boot_file['uuid'])
            boot_vars = _build_var_map(
                _clean_obey_lines(boot_data.decode('latin-1', errors='replace'))
            )
        except Exception as exc:  # noqa: BLE001 - !Boot vars are best-effort
            log.debug('    Could not read !Boot: %s', exc)

    try:
        data = client.download_extracted_file_bytes(run_file['uuid'])
        launched = set(parse_run_obey(data.decode('latin-1', errors='replace'),
                                      extra_vars=boot_vars))
        if verbose:
            log.info('    !Run launches: %s', ', '.join(sorted(launched)) or '(none parsed)')
        return launched
    except Exception as exc:  # noqa: BLE001 - best-effort; fall back to heuristic
        log.debug('    Could not read/parse !Run: %s', exc)
        return set()


def classify_app_files(app_dir_name: str, app_files: list[dict],
                       launched_set: set[str], is_unique,
                       verbose: bool = False) -> list[tuple[dict, bool]]:
    """Classify each file in an application directory as Mandatory or Optional.

    Returns a list of (file_dict, is_required) tuples.  *is_unique* is a callable
    taking a file dict and returning whether the file is unique enough to be
    Mandatory.
    """
    results: list[tuple[dict, bool]] = []
    any_mandatory = False

    for f in app_files:
        if f.get('is_directory'):
            continue
        leaf = f.get('filename', '').lower()
        if leaf in ('!run', '!boot'):
            results.append((f, False))
            continue

        rel = _app_relative(app_dir_name, f.get('path', '')).lower()
        is_launched = rel in launched_set or any(
            t.rsplit('/', 1)[-1] == leaf for t in launched_set
        )
        if is_launched and is_unique(f):
            results.append((f, True))
            any_mandatory = True
        else:
            results.append((f, False))

    # Fallback: nothing was launched/matched -> use the filetype heuristic
    # (still gated by uniqueness), excluding !Run/!Boot.
    if not any_mandatory:
        for i, (f, _req) in enumerate(results):
            leaf = f.get('filename', '').lower()
            if leaf in ('!run', '!boot'):
                continue
            if _filetype_mandatory(f) and is_unique(f):
                results[i] = (f, True)

    return results


def build_product_files(classified: list[tuple[dict, bool]],
                        verbose: bool = False) -> list[dict]:
    """Convert classified files to HashDB KnownFile entries."""
    result = []
    for f, is_req in classified:
        entry = {
            'filename': f.get('filename', ''),
            'file_size': f.get('file_size'),
            'is_required': is_req,
            'relative_path': f.get('path', ''),
        }
        if f.get('md5'):
            entry['md5'] = f['md5']
        if f.get('sha1'):
            entry['sha1'] = f['sha1']
        if f.get('sha256'):
            entry['sha256'] = f['sha256']
        if not entry.get('md5') and not entry.get('sha1'):
            continue  # cannot match a file with no hash
        result.append(entry)
        if verbose:
            log.info('    %s %s', 'MANDATORY' if is_req else 'optional',
                     f.get('path', ''))
    return result


# ---------------------------------------------------------------------------
# Product title construction
# ---------------------------------------------------------------------------

def _product_context(clean_name: str | None, item_name: str | None) -> str:
    """The provenance shown after the app-dir name in a product title.

    Prefer the artefact's clean product name (e.g. ``BeebIt (FR) 0.53``) — that
    identifies the actual software — and fall back to the item name only when an
    artefact has no usable label.
    """
    return (clean_name or '').strip() or (item_name or '').strip()


def build_product_title(app_dir_name: str, context: str | None = None,
                        disc_number: int | None = None) -> str:
    """Compose a product title: app-dir name plus product provenance."""
    title = f'{app_dir_name} - {context}' if context else app_dir_name
    if disc_number is not None:
        title += f' (Disk {disc_number})'
    return title


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------

# Diagnostic reason codes for why an application produced no Mandatory file,
# in priority order, with the human-readable label shown by --explain.
NO_MANDATORY_REASONS = (
    ('no-launch-target',
     'no launch target found (!Run not parsed in a recognised form and no '
     'RISC OS executable filetype metadata on any file)'),
    ('known',
     'launch target already present in an active hash database (is_known)'),
    ('shared',
     'launch target content shared across applications (not unique)'),
    ('global',
     'launch target also present elsewhere in the catalogue '
     '(cross-catalogue --global-check)'),
    ('no-md5',
     'launch target has no MD5 hash (uniqueness is judged on MD5)'),
    ('unknown', 'undetermined'),
)


_REASON_LABELS = dict(NO_MANDATORY_REASONS)


def _reason_label(code: str | None) -> str:
    """Human-readable label for a NO_MANDATORY_REASONS code."""
    return _REASON_LABELS.get(code, code or 'undetermined')


def local_uniqueness_failure(f: dict, md5_appkeys: dict[str, set],
                             include_known: bool = False) -> str | None:
    """Return the *local* reason a file cannot be Mandatory, or None if it is
    locally eligible (in which case only the global check could still reject it).

    Mirrors the local portion of make_is_unique()'s predicate exactly so the
    --explain diagnosis agrees with the actual classification.  When
    *include_known* is set, a file already present in an active hash database is
    not disqualified on that basis (used when regenerating a database whose own
    files are flagged is_known).
    """
    if f.get('is_known') and not include_known:
        return 'known'
    md5 = (f.get('md5') or '').lower()
    if not md5:
        return 'no-md5'
    keys = md5_appkeys.get(md5)
    if keys is None or len(keys) != 1:
        return 'shared'
    return None


def make_is_unique(client: ArcologyClient, md5_appkeys: dict[str, set],
                   global_check: bool, include_known: bool = False):
    """Build the is_unique() predicate.

    A file is Mandatory-eligible when it is not already in an active hash
    database (is_known) and its content is unique to one application.  Local
    uniqueness is judged from *md5_appkeys* (built across the selected items, so
    uniqueness is already scoped to the selection); when the opt-in
    *global_check* is on, /hash-lookup additionally confirms the file does not
    also appear in other items across the whole catalogue.

    When *include_known* is set, the "already in a hash database" disqualifier
    is dropped (both the local is_known flag and the global /hash-lookup
    known_file match) — required when regenerating a database whose own files
    are, by definition, already known.

    The returned callable carries *md5_appkeys*, *global_check* and
    *include_known* as attributes so the --explain diagnosis can reproduce the
    local reasoning without the closure having to be unpacked.
    """
    def is_unique(f: dict) -> bool:
        if local_uniqueness_failure(f, md5_appkeys, include_known) is not None:
            return False
        if global_check:
            try:
                data = client.hash_lookup(md5=f.get('md5'), sha1=f.get('sha1'))
            except Exception:  # noqa: BLE001 - network hiccup: trust local result
                return True
            if data.get('known_file') and not include_known:
                return False
            item_ids = {fi.get('item_id') for fi in data.get('found_in', [])}
            if len(item_ids) > 1:
                return False
        return True

    is_unique.md5_appkeys = md5_appkeys
    is_unique.global_check = global_check
    is_unique.include_known = include_known
    return is_unique


def diagnose_no_mandatory(app_dir_name: str, app_files: list[dict],
                          launched_set: set[str], is_unique) -> str:
    """Explain why an application directory produced no Mandatory file.

    Returns one of the reason codes in NO_MANDATORY_REASONS.  Uses the same
    launch-target identification as classify_app_files() (parsed !Run targets,
    or the executable-filetype fallback), then reports the dominant reason those
    candidates were rejected by the uniqueness gate.
    """
    md5_appkeys = getattr(is_unique, 'md5_appkeys', {})
    global_check = getattr(is_unique, 'global_check', False)
    include_known = getattr(is_unique, 'include_known', False)

    candidates: list[dict] = []
    for f in app_files:
        if f.get('is_directory'):
            continue
        leaf = f.get('filename', '').lower()
        if leaf in ('!run', '!boot'):
            continue
        rel = _app_relative(app_dir_name, f.get('path', '')).lower()
        is_launched = rel in launched_set or any(
            t.rsplit('/', 1)[-1] == leaf for t in launched_set
        )
        if is_launched or _filetype_mandatory(f):
            candidates.append(f)

    if not candidates:
        return 'no-launch-target'

    reasons = set()
    for f in candidates:
        r = local_uniqueness_failure(f, md5_appkeys, include_known)
        if r is None:
            # Locally eligible -> only the global check could have rejected it.
            reasons.add('global' if global_check else 'unknown')
        else:
            reasons.add(r)

    for code, _label in NO_MANDATORY_REASONS:
        if code in reasons:
            return code
    return 'unknown'


# ---------------------------------------------------------------------------
# Canonical sources ("golden" copy disambiguation)
# ---------------------------------------------------------------------------
#
# Some applications (!ArcFS, !System, !Fonts, !Scrap, !Boot, SerialDev, …)
# legitimately appear across many artefacts: the original "golden" distribution,
# plus copies bundled on magazine cover discs or inside other utilities.  Their
# byte-identical launch targets then look "shared" and are demoted to Optional,
# and the bundled copies generate junk products (e.g. "!ArcFS - RiscCAD 8").
#
# A canonical-sources file names, per application directory, a regex that the
# artefact's product name (or raw label) must match for that copy to be kept.
# Copies on non-matching artefacts are dropped entirely — removed from the
# uniqueness map (so the golden launch target becomes unique again) and from the
# product output.  Application directories with no rule are unaffected.
#
# File format: one rule per line, "<app-dir> <regex>", split on the first run of
# whitespace (so the regex may contain spaces).  Blank lines and lines starting
# with '#' are ignored.  Repeated lines for the same app-dir are OR'd.  The regex
# is anchored at the start (re.match) and matched case-insensitively.


def parse_canonical_sources(text: str) -> dict[str, list]:
    """Parse canonical-sources text into {app_dir_lower: [(regex, title_override|None), ...]}.

    Each rule line has the form ``<app-dir> <regex> [-> title-override]``.  The
    optional title override is introduced by ``->`` (with any surrounding
    whitespace) and is a literal string that replaces the auto-derived product
    title context when that copy is kept.  Use it to give bundled copies a
    descriptive name, e.g.:

        !FormEd    ANSI C Release 3 -> FormEd 2.45 (from Acorn C R3)

    Whitespace around ``->`` is flexible: ``regex->title``, ``regex -> title``,
    and ``regex  ->  title`` are all equivalent.

    Raises ValueError on a malformed line, an empty title after ``->``, or an
    invalid regex.
    """
    rules: dict[str, list] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise ValueError(
                f'line {lineno}: expected "<app-dir> <regex>", got: {raw!r}')
        app_dir, rest = parts
        arrow_split = re.split(r'\s*->\s*', rest, maxsplit=1)
        if len(arrow_split) == 2:
            pattern = arrow_split[0].rstrip()
            title_override = arrow_split[1].strip()
            if not title_override:
                raise ValueError(
                    f'line {lineno}: empty title after "->": {raw!r}')
        else:
            pattern = rest
            title_override = None
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                f'line {lineno}: invalid regex {pattern!r}: {exc}') from exc
        rules.setdefault(app_dir.lower(), []).append((compiled, title_override))
    return rules


def load_canonical_sources(path: str) -> dict[str, list]:
    """Load and parse a canonical-sources file."""
    with open(path, encoding='utf-8') as fh:
        return parse_canonical_sources(fh.read())


def canonical_accepts(rules: dict[str, list], app_dir_name: str,
                      clean_name: str, label: str) -> tuple[bool | None, str | None]:
    """Decide whether an app-dir copy on a given artefact is the canonical one.

    Returns ``(verdict, title_override)``:
    - ``(None, None)``  — no rule covers this app-dir (unaffected)
    - ``(True, str|None)`` — rule matched; title_override is the literal
      third-column string when present, else None (use the auto-derived context)
    - ``(False, None)`` — rules exist but none matched (reject this copy)

    Matches the regex against *either* the cleaned product name or the raw label.
    """
    pats = rules.get(app_dir_name.lower())
    if not pats:
        return None, None
    for pat, override in pats:
        if (clean_name and pat.match(clean_name)) or (label and pat.match(label)):
            return True, override
    return False, None


def apply_canonical_filter(gathered: list, rules: dict[str, list]) -> tuple[int, set]:
    """Drop non-canonical app-dir copies from *gathered* in place.

    Returns ``(dropped_count, matched_rule_keys)`` so the caller can report how
    many copies were rejected and warn about rules that matched nothing.
    """
    dropped = 0
    matched_keys: set = set()
    for g in gathered:
        for ar in g['artefact_results']:
            clean = ar.get('clean_name') or ''
            label = ar.get('label') or ''
            kept = {}
            for name, files in ar['app_dirs'].items():
                verdict, override = canonical_accepts(rules, name, clean, label)
                if verdict is False:
                    dropped += 1
                    continue
                if verdict is True:
                    matched_keys.add(name.lower())
                    if override:
                        ar.setdefault('title_overrides', {})[name] = override
                kept[name] = files
            ar['app_dirs'] = kept
    return dropped, matched_keys


def _appdir_fingerprint(files: list[dict]) -> frozenset:
    """Content fingerprint of an app-dir copy: the set of its file hashes."""
    return frozenset(
        (f.get('md5') or f.get('sha1') or f.get('sha256'))
        for f in files if not f.get('is_directory')
    )


def _matches_app_base(app_dir_name: str, product_name: str) -> bool:
    """True if *product_name* looks like the app's own release (its name starts
    with the app-dir's base name, e.g. !65Host ↔ "65Host 1.14")."""
    base = app_dir_name[1:] if app_dir_name.startswith('!') else app_dir_name
    return bool(re.match(r'^' + re.escape(base) + r'(\b|\s|$)',
                         product_name or '', re.IGNORECASE))


def collect_canonical_candidates(gathered: list) -> dict[str, list]:
    """Find app-dirs that genuinely need a golden-source decision.

    Returns ``{app_dir_name: [product_name_per_occurrence, ...]}``.  App-dir
    names are grouped case-insensitively (RISC OS Filecore is case-insensitive,
    so !ARCFS and !ArcFS are the same application); the most common spelling is
    used for display.  An app-dir is *omitted* (needs no rule) when either:

    * every copy is byte-identical (identical-copy merging collapses them), or
    * every copy sits on an artefact named after the app — these are just
      different versions of the app's own release discs (e.g. !65Host on
      "65Host 1.14"/"1.17"/"1.20"), which should all be kept.

    What remains are apps appearing on at least one artefact *not* named after
    them — a likely copy bundled with an unrelated product (e.g. !ArcFS on
    "RiscCAD 8") that a curator must adjudicate.  App-dir names containing
    whitespace (synthetic root/disc keys) are skipped.
    """
    occ: dict[str, dict] = {}  # lower -> {'names': Counter, 'entries': [(disp, fp)]}
    for g in gathered:
        for ar in g['artefact_results']:
            disp = (ar.get('clean_name') or ar.get('label') or '').strip()
            for name, files in ar['app_dirs'].items():
                if any(c.isspace() for c in name):
                    continue
                slot = occ.setdefault(name.lower(),
                                      {'names': Counter(), 'entries': []})
                slot['names'][name] += 1
                slot['entries'].append((disp, _appdir_fingerprint(files)))

    result: dict[str, list] = {}
    for slot in occ.values():
        entries = slot['entries']
        if len(entries) < 2:
            continue
        if len({fp for _disp, fp in entries}) < 2:
            continue  # all copies identical -> auto-merged, no rule needed
        display = slot['names'].most_common(1)[0][0]
        names = [disp for disp, _fp in entries]
        if all(_matches_app_base(display, d) for d in names):
            continue  # all the app's own (versioned) discs -> keep all
        result[display] = names
    return result


def render_canonical_candidates(candidates: dict[str, list]) -> str:
    """Render ambiguous-app candidates as an editable canonical-sources file.

    Copies on artefacts named after the app (its own releases/versions) are left
    *active* (uncommented) so they are all kept; copies on unrelated artefacts
    (likely bundled with another product) are commented out for the curator to
    confirm or drop.  When no copy is named after the app, all lines are
    commented and a hint is shown.  Active lines are listed first.
    """
    out = [
        '# Canonical sources candidates — generated by',
        '#   arco hashdb generate-riscos ... --dump-canonical',
        '#',
        '# Each application below appears with DIFFERING content on more than one',
        '# artefact (byte-identical copies are merged automatically and omitted).',
        '# Lines for the app\'s own releases are pre-activated (kept); copies that',
        '# look bundled with an unrelated product are commented out — uncomment',
        '# any you want to keep, or delete the rest, then pass this file via',
        '# --canonical-sources.',
        '#',
        '# The regex is matched case-insensitively, anchored at the start, against',
        '# the artefact product name or raw label.',
        '#',
        '# Format: <app-dir> <regex> [-> title-override]',
        '#',
        '# An optional title override introduced by " -> " replaces the auto-derived',
        '# product title context (normally the artefact label).  Use it for bundled',
        '# copies whose host artefact label would produce a misleading name, e.g.:',
        '#   !FormEd    ANSI C Release 3 -> FormEd 2.45 (from Acorn C R3)',
        '',
    ]
    if not candidates:
        out.append('# (no ambiguous applications found in the selection)')
        return '\n'.join(out) + '\n'

    for name in sorted(candidates):
        occ = candidates[name]
        uniq = sorted(set(occ))
        # Copies on artefacts named after the app are kept (active); these are
        # the app's own releases/versions.  Copies on unrelated artefacts (likely
        # bundles) are commented for the curator to confirm/drop.
        matches = [n for n in uniq if _matches_app_base(name, n)]
        ordered = matches + [n for n in uniq if n not in matches]

        out.append(f'# {name} — appears on {len(occ)} artefact(s):')
        if not matches:
            out.append('#   (could not identify the app by name — '
                       'uncomment the golden source(s) to keep)')
        for n in ordered:
            # Escape regex metacharacters but keep spaces/hyphens readable.
            pat = (re.escape(n).replace('\\ ', ' ').replace('\\-', '-')) if n else '.*'
            prefix = '' if n in matches else '#'
            out.append(f'{prefix}{name}    {pat}')
        out.append('')
    return '\n'.join(out) + '\n'


# ---------------------------------------------------------------------------
# Identical-copy merging
# ---------------------------------------------------------------------------
#
# An application bundled with several products (the classic case is Equasor,
# shipped with Impression Publisher and Impression Style as well as standalone)
# yields one product instance per artefact.  When those copies are byte-identical
# they are the same program and should collapse into a single product entry.
# Copies that genuinely differ (a different build/version) keep separate entries.


def _product_fingerprint(product: dict) -> frozenset:
    """Content fingerprint of a product: its file hashes + required flags.

    Two products with the same fingerprint contain byte-identical files."""
    return frozenset(
        (f.get('md5') or f.get('sha1') or f.get('sha256'), bool(f.get('is_required')))
        for f in product['files']
    )


def _golden_rank(product: dict) -> tuple:
    """Sort key picking the best representative of an identical-copy group:
    prefer a context (product name) that starts with the app's base name (the
    standalone/golden release rather than a bundle), then the shorter name."""
    app = product.get('_app_dir', '') or ''
    base = app[1:] if app.startswith('!') else app
    ctx = product.get('_context', '') or ''
    starts = bool(re.match(r'^' + re.escape(base) + r'(\b|\s|$)', ctx, re.IGNORECASE))
    return (starts, -len(ctx))


def merge_identical_products(products: list[dict]) -> tuple[list[dict], int]:
    """Collapse byte-identical copies of the same application into one product.

    Groups by (app-dir, content fingerprint); within a group the best-named copy
    (see _golden_rank) is kept and its description gains an "also in ..." note
    listing the other sources.  Returns ``(deduped, merged_count)`` preserving
    first-seen order.
    """
    groups: dict = {}
    order: list = []
    for p in products:
        # Case-insensitive app-dir grouping (RISC OS Filecore is case-insensitive).
        key = ((p.get('_app_dir') or '').lower(), _product_fingerprint(p))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)

    result = []
    merged = 0
    for key in order:
        grp = groups[key]
        if len(grp) == 1:
            result.append(grp[0])
            continue
        best = max(grp, key=_golden_rank)
        others = [p.get('_context', '') for p in grp if p is not best]
        others = sorted({o for o in others if o})
        if others:
            best['description'] += ' [also in: ' + ', '.join(others) + ']'
        merged += len(grp) - 1
        result.append(best)
    return result, merged


# ---------------------------------------------------------------------------
# Item gathering and product building
# ---------------------------------------------------------------------------

def _run_jobs(fn, items: list, jobs: int) -> list:
    """Map *fn* over *items*, in parallel when jobs > 1, preserving order.

    Used for the read-only API fan-out (artefact/partition fetches, !Run/!Boot
    downloads, hash lookups).  requests.Session is safe for concurrent GETs.
    """
    if jobs > 1 and len(items) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            return list(ex.map(fn, items))
    return [fn(it) for it in items]


def _gather_artefact(client: ArcologyClient, art: dict, root_files: str,
                     idx: int, n_art: int) -> dict | None:
    """Fetch one artefact's partitions/files and group by application dir."""
    art_label = art.get('label', art.get('original_filename', ''))
    # Strip archive/disc-image extensions so display names are clean even when
    # the label was derived from the original filename (e.g. "Foo 1.0.zip").
    display_label = _KNOWN_EXTS_RE.sub('', art_label)
    art_detail = client.get_artefact(art['uuid'])
    partitions = art_detail.get('partitions', [])
    if not partitions:
        log.info('    [%d/%d] %s: no partitions', idx, n_art, art_label)
        return None

    all_files = []
    for part in partitions:
        all_files.extend(
            client.get_partition_files_all(part['uuid'], show_known='true')
        )
    if not all_files:
        log.info('    [%d/%d] %s: %d partition(s), no files',
                 idx, n_art, art_label, len(partitions))
        return None

    app_dirs = find_app_directories(all_files, root_files, art_label)
    log.info('    [%d/%d] %s: %d partition(s), %d file(s), %d app(s)',
             idx, n_art, art_label, len(partitions), len(all_files), len(app_dirs))

    parsed = parse_artefact_label(display_label)
    full_name = parsed['clean_name']          # e.g. "Rephorm 1.04 (1993)(Oak Solutions)"
    clean_name = strip_tosec_metadata(full_name)  # e.g. "Rephorm 1.04"
    return {
        'label': art_label,
        'full_name': full_name,
        'clean_name': clean_name,
        'disc_number': parsed['disc_number'],
        'version': parsed['version'],
        'app_dirs': app_dirs,
    }


def _gather_item(client: ArcologyClient, item: dict, filter_tags: list[str],
                 root_files: str, jobs: int) -> dict:
    """Fetch an item's artefacts/partitions/files and group by application dir."""
    item_name = item['name']

    # Reuse the artefact list when the item was already fetched in detail
    # (the --item selection path), otherwise fetch it now.
    artefacts = item.get('artefacts')
    if artefacts is None:
        artefacts = client.get_item(item['uuid']).get('artefacts', [])
    n_art = len(artefacts)
    log.info('  %d artefact(s)', n_art)

    results = _run_jobs(
        lambda pair: _gather_artefact(client, pair[1], root_files, pair[0], n_art),
        list(enumerate(artefacts, 1)),
        jobs,
    )
    artefact_results = [r for r in results if r]

    version = next((ar['version'] for ar in artefact_results if ar['version']), None)
    return {
        'item': item,
        'item_name': item_name,
        'version': version,
        'artefact_results': artefact_results,
    }


def _build_products(client: ArcologyClient, g: dict, args, is_unique,
                    jobs: int) -> tuple[list[dict], 'Counter']:
    """Build HashDB product dicts for one gathered item.

    Returns ``(products, reasons)`` where *reasons* is a Counter of the
    NO_MANDATORY_REASONS codes for every application that produced no Mandatory
    file (whether emitted or dropped by --require-mandatory).
    """
    artefact_results = g['artefact_results']
    is_multi_disc = len(artefact_results) > 1
    item_name = g['item_name']

    # Assemble the per-application work items, then process them concurrently
    # (each one downloads !Run/!Boot and may issue hash lookups).  Each task
    # carries its own context — the artefact's product name — so the title
    # identifies the actual software, not the (collection) item name.
    # Tasks: (app_dir_name, app_files, disc_number, suffix, short_context, full_context)
    # short_context: TOSEC metadata stripped (for the JSON title / lozenge label)
    # full_context:  year/publisher retained  (for the JSON description / hover text)
    tasks = []

    if args.multi_disc in ('separate', 'both'):
        for ar in artefact_results:
            disc_num = ar['disc_number'] if is_multi_disc else None
            overrides = ar.get('title_overrides', {})
            for app_dir_name, app_files in ar['app_dirs'].items():
                override = overrides.get(app_dir_name)
                short_ctx = override or _product_context(ar.get('clean_name'), item_name)
                full_ctx = _product_context(ar.get('full_name'), item_name)
                tasks.append((app_dir_name, app_files, disc_num, '', short_ctx, full_ctx))

    if args.multi_disc in ('merge', 'both'):
        merged: dict[str, list[dict]] = {}
        merged_context: dict[str, str] = {}
        merged_full_context: dict[str, str] = {}
        for ar in artefact_results:
            disc_num = ar['disc_number']
            overrides = ar.get('title_overrides', {})
            for app_dir_name, app_files in ar['app_dirs'].items():
                override = overrides.get(app_dir_name)
                short_ctx = override or _product_context(ar.get('clean_name'), item_name)
                full_ctx = _product_context(ar.get('full_name'), item_name)
                if override or app_dir_name not in merged_context:
                    merged_context[app_dir_name] = short_ctx
                    merged_full_context[app_dir_name] = full_ctx
                bucket = merged.setdefault(app_dir_name, [])
                for f in app_files:
                    if is_multi_disc and disc_num is not None:
                        f = dict(f)
                        f['path'] = f'Disk {disc_num}/{f.get("path", "")}'
                    bucket.append(f)

        for app_dir_name, all_files in merged.items():
            seen = set()
            deduped = []
            for f in all_files:
                key = (f.get('md5', ''), f.get('path', ''))
                if key not in seen:
                    seen.add(key)
                    deduped.append(f)
            suffix = ' [All Discs]' if (args.multi_disc == 'both' and is_multi_disc) else ''
            tasks.append((app_dir_name, deduped, None, suffix,
                          merged_context[app_dir_name],
                          merged_full_context[app_dir_name]))

    explain = getattr(args, 'explain', False)

    def make_product(task):
        app_dir_name, app_files, disc_number, suffix, short_context, full_context = task
        launched = get_launched_set(client, app_files, verbose=args.verbose)
        classified = classify_app_files(app_dir_name, app_files, launched,
                                        is_unique, verbose=args.verbose)
        pfiles = build_product_files(classified, verbose=args.verbose)
        if not pfiles:
            return (None, None)
        mandatory = sum(1 for f in pfiles if f['is_required'])
        product_title = build_product_title(app_dir_name, short_context, disc_number) + suffix

        reason = None
        if mandatory == 0:
            reason = diagnose_no_mandatory(app_dir_name, app_files, launched, is_unique)

        # A product with no mandatory file has no discriminating fingerprint and
        # is ignored by the matcher.  With --require-mandatory, drop it here
        # rather than emitting an unmatchable product.
        if mandatory == 0 and getattr(args, 'require_mandatory', False):
            if explain:
                log.info('    %s: skipped (no mandatory file: %s)',
                         product_title, _reason_label(reason))
            else:
                log.info('    %s: skipped (no mandatory file)', product_title)
            return (None, reason)

        if explain and mandatory == 0:
            log.info('    %s: %3d mandatory, %3d optional  [%s]',
                     product_title, mandatory, len(pfiles) - mandatory,
                     _reason_label(reason))
        else:
            log.info('    %s: %3d mandatory, %3d optional',
                     product_title, mandatory, len(pfiles) - mandatory)
        return ({
            'title': product_title,
            'description': build_product_title(app_dir_name, full_context, disc_number),
            'path_match_enabled': bool(getattr(args, 'path_match', False)),
            'files': pfiles,
            # Private metadata for identical-copy merging; stripped before output.
            '_app_dir': app_dir_name,
            '_context': short_context,
        }, reason)

    results = _run_jobs(make_product, tasks, jobs)
    products = [p for p, _r in results if p]
    reasons = Counter(r for _p, r in results if r)
    return products, reasons


# ---------------------------------------------------------------------------
# Item selection
# ---------------------------------------------------------------------------

def _select_items(client: ArcologyClient, args) -> list[dict]:
    """Select items by explicit UUID, or by tag(s) and/or platform."""
    if getattr(args, 'item', None):
        return [client.get_item(uuid) for uuid in args.item]

    platform_id = None
    if getattr(args, 'platform', None):
        platform_id = client.lookup_platform(args.platform)
        if platform_id is None:
            log.error('Platform not found: %s', args.platform)
            sys.exit(1)

    tags = args.tag or [None]
    seen = set()
    result = []
    for t in tags:
        params = {}
        if t:
            params['tag'] = t
        if platform_id is not None:
            params['platform_id'] = platform_id
        for it in client.list_items_all(**params):
            if it['uuid'] not in seen:
                seen.add(it['uuid'])
                result.append(it)
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def cmd_hashdb_generate_riscos(client: ArcologyClient, args):
    """Generate RISC OS HashDB JSON from items in Arcology."""
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format='%(message)s', stream=sys.stderr)

    if not (getattr(args, 'tag', None) or getattr(args, 'item', None)
            or getattr(args, 'platform', None)):
        log.error('Select items with at least one of --tag, --item or --platform.')
        sys.exit(1)

    filter_tags = list(args.tag or [])
    jobs = max(1, getattr(args, 'jobs', 1) or 1)

    canonical_rules: dict[str, list] = {}
    if getattr(args, 'canonical_sources', None):
        try:
            canonical_rules = load_canonical_sources(args.canonical_sources)
        except (OSError, ValueError) as exc:
            log.error('--canonical-sources: %s', exc)
            sys.exit(1)
        log.info('Loaded %d canonical-source rule(s) for %d application(s)',
                 sum(len(v) for v in canonical_rules.values()), len(canonical_rules))

    items = _select_items(client, args)
    if not items:
        log.error('No items matched the selection.')
        sys.exit(1)
    log.info('Selected %d item(s) (concurrency: %d)', len(items), jobs)

    if args.dry_run:
        log.info('')
        log.info('=== DRY RUN ===')
        for item in items:
            tags = ', '.join(item.get('tags', []))
            log.info('  %s [%s] (%s artefact(s))', item['name'], tags,
                     item.get('artefact_count', '?'))
        if getattr(args, 'json', False):
            from ..formatting import print_json
            print_json({
                'dry_run': True,
                'items': [
                    {'name': it['name'], 'tags': it.get('tags', []),
                     'artefact_count': it.get('artefact_count', 0)}
                    for it in items
                ],
            })
        return

    # Pass 1: gather every item's application files.
    log.info('Gathering files...')
    gathered = []
    for i, item in enumerate(items, 1):
        log.info('[%d/%d] %s', i, len(items), item['name'])
        g = _gather_item(client, item, filter_tags, args.root_files, jobs)
        if g['artefact_results']:
            gathered.append(g)

    # Apply canonical-source rules: drop non-golden copies before they enter the
    # uniqueness map or product output.
    if canonical_rules:
        dropped, matched = apply_canonical_filter(gathered, canonical_rules)
        log.info('Canonical sources: dropped %d non-canonical application copy(ies)',
                 dropped)
        never = sorted(set(canonical_rules) - matched)
        if never:
            log.warning('Canonical rules that matched nothing in the selection: %s',
                        ', '.join(never))

    # --dump-canonical: write an editable candidates file and stop.  Run after
    # canonical filtering so already-resolved apps drop out (iterative workflow).
    if getattr(args, 'dump_canonical', None):
        candidates = collect_canonical_candidates(gathered)
        with open(args.dump_canonical, 'w', encoding='utf-8') as fh:
            fh.write(render_canonical_candidates(candidates))
        log.info('Wrote %d ambiguous application(s) to %s',
                 len(candidates), args.dump_canonical)
        log.info('Edit it (keep the golden source per app), then pass it via '
                 '--canonical-sources.')
        return

    # Build the cross-collection uniqueness map: md5 -> {(item_uuid, app_dir)}.
    md5_appkeys: dict[str, set] = {}
    for g in gathered:
        item_uuid = g['item']['uuid']
        for ar in g['artefact_results']:
            for app_dir_name, files in ar['app_dirs'].items():
                # Group case-insensitively: RISC OS Filecore is case-insensitive,
                # so !ARCFS and !ArcFS are the same application directory.
                key = (item_uuid, app_dir_name.lower())
                for f in files:
                    md5 = (f.get('md5') or '').lower()
                    if md5:
                        md5_appkeys.setdefault(md5, set()).add(key)

    is_unique = make_is_unique(client, md5_appkeys, args.global_check,
                               include_known=getattr(args, 'include_known', False))

    # Pass 2: build products.
    n_apps = sum(len(ar['app_dirs']) for g in gathered for ar in g['artefact_results'])
    log.info('Building products from %d application instance(s)...', n_apps)
    all_products = []
    items_with_products = 0
    no_mandatory_reasons: Counter = Counter()
    for g in gathered:
        products, reasons = _build_products(client, g, args, is_unique, jobs)
        no_mandatory_reasons.update(reasons)
        if products:
            all_products.extend(products)
            items_with_products += 1

    # Collapse byte-identical copies of the same app (e.g. Equasor bundled with
    # several Impression releases) into one product, unless disabled.
    merged_count = 0
    if getattr(args, 'merge_duplicates', True):
        all_products, merged_count = merge_identical_products(all_products)
        if merged_count:
            log.info('Merged %d identical duplicate product copy(ies) into their '
                     'canonical entry', merged_count)

    # Strip private merge metadata before serialising.
    for p in all_products:
        p.pop('_app_dir', None)
        p.pop('_context', None)

    output_data = {
        'schema_version': 1,
        'database': {
            'name': args.db_name,
            'description': args.db_description or '',
            'version': args.db_version or date.today().isoformat(),
            'enable_product_recognition': True,
        },
        'products': all_products,
    }
    if args.source_url:
        output_data['database']['source_url'] = args.source_url

    total_files = sum(len(p['files']) for p in all_products)
    mandatory_files = sum(sum(1 for f in p['files'] if f['is_required'])
                          for p in all_products)
    optional_files = total_files - mandatory_files
    products_no_mandatory = sum(no_mandatory_reasons.values())

    with open(args.output, 'w', encoding='utf-8') as fh:
        json.dump(output_data, fh, indent=2, default=str)

    log.info('')
    log.info('=== Summary ===')
    log.info('Items processed:  %d (%d produced products)',
             len(items), items_with_products)
    log.info('Products:         %d', len(all_products))
    if merged_count:
        log.info('Merged copies:    %d identical duplicate(s) collapsed', merged_count)
    log.info('Files:            %d (%d mandatory, %d optional)',
             total_files, mandatory_files, optional_files)
    if products_no_mandatory:
        log.info('No mandatory:     %d application(s) produced no mandatory file',
                 products_no_mandatory)
        if getattr(args, 'explain', False):
            for code, _label in NO_MANDATORY_REASONS:
                n = no_mandatory_reasons.get(code, 0)
                if n:
                    log.info('  %5d  %s', n, _reason_label(code))
        else:
            log.info('                  (re-run with --explain to see why)')
    log.info('Output:           %s', args.output)
    log.info('')
    log.info('Import with:  arco hashdb import %s', args.output)

    if getattr(args, 'json', False):
        from ..formatting import print_json
        print_json({
            'output': args.output,
            'items_processed': len(items),
            'items_with_products': items_with_products,
            'products': len(all_products),
            'files': total_files,
            'mandatory_files': mandatory_files,
            'optional_files': optional_files,
            'products_no_mandatory': products_no_mandatory,
            'no_mandatory_reasons': dict(no_mandatory_reasons),
            'merged_duplicates': merged_count,
        })

# vim: ts=4 sw=4 et
