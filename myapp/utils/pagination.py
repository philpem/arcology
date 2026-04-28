"""Pagination utilities for Arcology."""

from flask import current_app, request
from flask_login import current_user
from sqlalchemy import func

from ..extensions import db

_ALPHA = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
_ALPHA_SET = set(_ALPHA)

VALID_PER_PAGE = [25, 50, 100, 250]


def compute_letter_pages(query, field, per_page, current_page=1, descending=False):
    """Compute letter-to-page mapping for alphabetical jump navigation.

    Given a query sorted alphabetically by *field*, compute which page each
    letter's first item falls on.  Returns a tuple of ``(letter_pages,
    current_letter)`` where *letter_pages* maps uppercase letters (and ``#``
    for non-alpha) to page numbers, and *current_letter* is the letter whose
    range the *current_page* falls within.

    Set *descending=True* when the query is sorted Z→A.  In that case the
    mapping is built in reverse order (Z first, ``#`` last) so that page
    numbers reflect the actual data layout.

    Works with both PostgreSQL and SQLite.
    """
    if per_page <= 0:
        return {}, ''

    first_char = func.upper(func.substr(field, 1, 1))
    # Strip any existing ORDER BY from the query — with_entities + group_by
    # builds its own SELECT and PostgreSQL rejects ORDER BY columns that are
    # not in the GROUP BY or an aggregate.
    rows = (
        query
        .order_by(None)
        .with_entities(first_char.label('letter'), func.count().label('cnt'))
        .group_by(first_char)
        .order_by(first_char)
        .all()
    )

    if not rows:
        return {}, ''

    # Separate alpha vs non-alpha, sorted alphabetically
    alpha_counts = []  # [(letter, count), ...]
    non_alpha_count = 0
    for letter, cnt in rows:
        if not letter:
            continue
        if letter in _ALPHA_SET:
            alpha_counts.append((letter, cnt))
        else:
            non_alpha_count += cnt

    # Sort alpha letters A→Z; reverse to Z→A when descending
    alpha_counts.sort(key=lambda x: x[0], reverse=descending)

    # Build ordered list.
    # Ascending:  # first (digits sort before letters), then A-Z
    # Descending: Z-A first, then # last (digits sort after letters in DESC)
    ordered = []
    if descending:
        ordered.extend(alpha_counts)
        if non_alpha_count > 0:
            ordered.append(('#', non_alpha_count))
    else:
        if non_alpha_count > 0:
            ordered.append(('#', non_alpha_count))
        ordered.extend(alpha_counts)

    # Compute cumulative offset -> page number for each letter
    letter_pages = {}
    cumulative = 0
    # Also track which letter each page falls within
    page_letter_ranges = []  # [(start_page, end_page, letter), ...]
    for letter, cnt in ordered:
        start_page = cumulative // per_page + 1
        letter_pages[letter] = start_page
        end_offset = cumulative + cnt - 1
        end_page = end_offset // per_page + 1
        page_letter_ranges.append((start_page, end_page, letter))
        cumulative += cnt

    # Determine current_letter from current_page
    current_letter = ''
    for start_page, end_page, letter in page_letter_ranges:
        if start_page <= current_page <= end_page:
            current_letter = letter
            break

    return letter_pages, current_letter


def resolve_per_page(config_key, config_default=25):
    """Resolve the per_page value from request args, user preference, or config.

    Returns ``(per_page, page, view_all)``.

    Priority:
      1. Explicit ``per_page`` query parameter (if valid or 0 for "all")
      2. User's saved ``per_page`` preference
      3. Application config value for *config_key*
      4. *config_default*

    When an explicit per_page is provided and differs from the user's stored
    preference, the preference is updated automatically.
    """
    page = request.args.get('page', 1, type=int)
    per_page_param = request.args.get('per_page', None, type=int)
    view_all = per_page_param == 0

    if per_page_param in VALID_PER_PAGE:
        per_page = per_page_param
        # Save preference if it changed
        if (current_user.is_authenticated
                and current_user.get_preference('per_page') != per_page):
            current_user.set_preference('per_page', per_page)
            db.session.commit()
    elif view_all:
        per_page = 10000
        page = 1
    else:
        # Try user preference, then config
        saved = None
        if current_user.is_authenticated:
            saved = current_user.get_preference('per_page')
        if saved in VALID_PER_PAGE:
            per_page = saved
        else:
            per_page = current_app.config.get(config_key, config_default)

    return per_page, page, view_all


def resolve_sort(param_name, valid_options, preference_key, default):
    """Resolve sort order from request arg, user preference, or default.

    Returns the resolved sort key (a string from *valid_options*).

    Priority:
      1. Explicit query parameter named *param_name* (if present and valid)
      2. User's saved preference under *preference_key*
      3. *default*

    When an explicit value is provided and differs from the user's stored
    preference, the preference is updated automatically.
    """
    sort_param = request.args.get(param_name)

    if sort_param in valid_options:
        if (current_user.is_authenticated
                and current_user.get_preference(preference_key) != sort_param):
            current_user.set_preference(preference_key, sort_param)
            db.session.commit()
        return sort_param

    if current_user.is_authenticated:
        saved = current_user.get_preference(preference_key)
        if saved in valid_options:
            return saved

    return default


class ListPagination:
    """Pagination wrapper for in-memory lists.

    Provides the same interface as Flask-SQLAlchemy's ``Pagination`` object
    so it can be used with the ``render_pagination_compact`` Jinja macro.
    """

    def __init__(self, items, page, per_page):
        self.total = len(items)
        self.per_page = per_page
        self.pages = max(1, -(-self.total // per_page))  # ceil division
        self.page = max(1, min(page, self.pages))  # clamp to valid range
        start = (self.page - 1) * per_page
        self.items = items[start:start + per_page]
        self.has_prev = self.page > 1
        self.has_next = self.page < self.pages
        self.prev_num = self.page - 1 if self.has_prev else None
        self.next_num = self.page + 1 if self.has_next else None

    def iter_pages(self, left_edge=2, left_current=2, right_current=3, right_edge=2):
        """Yield page numbers with ``None`` gaps, matching Flask-SQLAlchemy."""
        last = 0
        for num in range(1, self.pages + 1):
            if (num <= left_edge
                    or (self.page - left_current <= num <= self.page + right_current)
                    or num > self.pages - right_edge):
                if last + 1 != num:
                    yield None
                yield num
                last = num


# vim: ts=4 sw=4 et
