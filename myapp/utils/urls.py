"""Safety helpers for user-supplied URLs rendered into ``<a href>``.

Distinct from ``safe_redirect.py`` (which confines a post-login ``?next=`` to a
same-origin *relative* path): these URLs — external-reference links and
hash-database source URLs — are *meant* to point off-site, so http/https and
relative/scheme-relative forms are all legitimate.  The danger is a
script-executing pseudo-scheme (``javascript:``, ``data:``, ``vbscript:``) that
runs when a user clicks the link, so those are rejected.
"""

from urllib.parse import urlsplit

# Schemes allowed to appear on an outbound link.  A URL with no scheme
# (relative ``/path`` or scheme-relative ``//host``) is also allowed — it simply
# navigates, it cannot execute.
_SAFE_URL_SCHEMES = frozenset({'http', 'https'})


def safe_external_url(url):
    """Return *url* if it is safe to place in an ``href``, else ``None``.

    Permits absolute http/https URLs and relative/scheme-relative URLs; rejects
    dangerous pseudo-schemes (``javascript:``, ``data:``, ``vbscript:``,
    ``file:`` …).  Browsers ignore ASCII whitespace/tab/newline when resolving
    the scheme, and ``urlsplit`` strips tab/newline; we additionally strip
    surrounding whitespace before checking so ``"  javascript:…"`` cannot slip
    through while a benign leading space would.
    """
    if not url or not isinstance(url, str):
        return None
    scheme = urlsplit(url.strip()).scheme.lower()
    if scheme == '' or scheme in _SAFE_URL_SCHEMES:
        return url
    return None

# vim: ts=4 sw=4 et
