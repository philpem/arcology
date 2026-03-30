"""Pagination utilities for Arcology."""

from flask import request, current_app
from flask_login import current_user
from sqlalchemy import func

from ..extensions import db

_ALPHA = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
_ALPHA_SET = set(_ALPHA)

VALID_PER_PAGE = [25, 50, 100, 250]


def compute_letter_pages(query, field, per_page, current_page=1):
    """Compute letter-to-page mapping for alphabetical jump navigation.

    Given a query sorted alphabetically by *field*, compute which page each
    letter's first item falls on.  Returns a tuple of ``(letter_pages,
    current_letter)`` where *letter_pages* maps uppercase letters (and ``#``
    for non-alpha) to page numbers, and *current_letter* is the letter whose
    range the *current_page* falls within.

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

    # Sort alpha letters
    alpha_counts.sort(key=lambda x: x[0])

    # Build ordered list: # first (if present), then A-Z
    ordered = []
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


# vim: ts=4 sw=4 et
