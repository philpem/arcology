"""
Safe redirect-target validation for post-login ``?next=`` parameters.

Login flows (local password login and OIDC SSO) redirect the user to a
``next`` URL supplied as a query parameter.  An attacker who can craft a login
link with a hostile ``next`` value can turn the trusted login page into an
open-redirect for phishing, so the value must be confined to a same-origin
relative path.

The naive guard ``url.startswith('/') and not url.startswith('//')`` is NOT
sufficient: browsers normalise a backslash to a forward slash, so
``/\\evil.com`` is loaded as ``//evil.com`` (a scheme-relative URL pointing at
``evil.com``).  Browsers also strip TAB/CR/LF from URLs before parsing, which
can reform an absolute URL.  This helper rejects all of those.
"""


def is_safe_redirect_path(target) -> bool:
    """Return True only for a same-origin relative path safe to redirect to.

    Accepts a value only when it begins with a single ``/`` and contains no
    characters a browser would use to escape the current origin.  Rejects:

    - empty / None / non-string values;
    - any C0 control character or DEL (browsers strip TAB/CR/LF and may then
      reparse the remainder as an absolute URL);
    - any backslash (normalised to ``/`` by browsers, so ``/\\evil.com`` ->
      ``//evil.com``);
    - scheme-relative URLs (``//host``);
    - absolute URLs and pseudo-schemes (``http://…``, ``javascript:…``) — they
      do not start with ``/``.
    """
    if not target or not isinstance(target, str):
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in target):
        return False
    if '\\' in target:
        return False
    if not target.startswith('/'):
        return False
    if target.startswith('//'):
        return False
    return True


def safe_redirect_path(target, default: str) -> str:
    """Return *target* if it is a safe same-origin path, else *default*."""
    return target if is_safe_redirect_path(target) else default

# vim: ts=4 sw=4 et
