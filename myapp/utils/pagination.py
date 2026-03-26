"""Pagination utilities for Arcology."""

from sqlalchemy import func


_ALPHA = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
_ALPHA_SET = set(_ALPHA)


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


# vim: ts=4 sw=4 et
