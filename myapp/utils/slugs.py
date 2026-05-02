"""
Slug generation utilities for Arcology.

Generates URL-safe slugs from item/artefact/analysis names for use in
file paths and URLs. Slugs are immutable once set.
"""

import re


def generate_slug(text: str, max_length: int = 200) -> str:
    """
    Generate a URL-safe slug from text.

    Rules:
    - Convert to lowercase
    - Replace common accession number separators with dash
    - Remove unsafe characters (keep only a-z, 0-9, dash)
    - Collapse multiple dashes to single dash
    - Trim to max_length
    - Strip leading/trailing dashes

    Args:
        text: Input text to slugify
        max_length: Maximum length of slug (default: 200)

    Returns:
        URL-safe slug string

    Examples:
        >>> generate_slug("FBX3-01 KUAI")
        'fbx3-01-kuai'
        >>> generate_slug("Disc 1/4: Install")
        'disc-1-4-install'
        >>> generate_slug("Test_Archive.zip")
        'test-archive-zip'
    """
    if not text:
        return 'untitled'

    # Convert to lowercase
    slug = text.lower()

    # Common accession number field separators → dash
    # Also handles: file extensions, directory paths, colons, etc.
    separators = ['/', '.', ':', ';', ',', '_', ' ', '\t', '\n']
    for sep in separators:
        slug = slug.replace(sep, '-')

    # Remove non-alphanumeric except dash
    slug = re.sub(r'[^a-z0-9-]', '', slug)

    # Collapse multiple dashes to single dash
    slug = re.sub(r'-+', '-', slug)

    # Strip leading/trailing dashes
    slug = slug.strip('-')

    # Truncate to max length
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip('-')

    # Fallback if slug is empty after processing
    return slug if slug else 'untitled'


def get_or_create_slug(obj, text_field: str, max_length: int = 200) -> str:
    """
    Get existing slug or create and save new one.

    Slugs are immutable once set - this function will not regenerate
    an existing slug even if the source text has changed.

    Args:
        obj: Database object (Item, Artefact, Analysis, Partition)
        text_field: Name of field to use for slug generation (e.g., 'name', 'label')
        max_length: Maximum slug length

    Returns:
        Slug string (existing or newly generated)

    Raises:
        AttributeError: If obj doesn't have slug or text_field attribute
        ValueError: If text_field is empty and no existing slug

    Examples:
        >>> item = Item(name="RISC OS 3.11")
        >>> slug = get_or_create_slug(item, 'name')
        >>> print(slug)
        'risc-os-3-11'
        >>> item.slug
        'risc-os-3-11'
    """
    from myapp.extensions import db

    # Return existing slug if present
    if hasattr(obj, 'slug') and obj.slug:
        return obj.slug

    # Get source text
    if not hasattr(obj, text_field):
        raise AttributeError(f"Object {obj} does not have field '{text_field}'")

    text = getattr(obj, text_field)
    if not text:
        # If no text and no existing slug, can't generate
        raise ValueError(f"Cannot generate slug: {text_field} is empty and no existing slug")

    # Generate a unique slug (mirrors the API creation path)
    from myapp.database import Item
    model_class = type(obj)
    base_slug = generate_slug(text, max_length=max_length)
    scope = None if model_class is Item else {'item_id': obj.item_id}
    slug = ensure_unique_slug(base_slug, model_class, scope_filter=scope)

    # Save to object
    if hasattr(obj, 'slug'):
        obj.slug = slug
        db.session.commit()
    else:
        raise AttributeError(f"Object {obj} does not have 'slug' field")

    return slug


def get_slug(obj) -> str | None:
    """
    Get slug from object without creating one.

    Args:
        obj: Database object with slug field

    Returns:
        Slug string if exists, None otherwise
    """
    return getattr(obj, 'slug', None)


def ensure_unique_slug(base_slug: str, model_class, existing_id: int | None = None,
                       scope_filter: dict | None = None) -> str:
    """
    Ensure slug is unique by appending number if necessary.

    Args:
        base_slug: Base slug to check
        model_class: SQLAlchemy model class (Item, Artefact, etc.)
        existing_id: ID to exclude from uniqueness check (for updates)
        scope_filter: Optional dict of extra filter kwargs to scope uniqueness
                      (e.g. {'item_id': 5} to check uniqueness within one item)

    Returns:
        Unique slug (may have -2, -3, etc. appended)

    Examples:
        >>> ensure_unique_slug('test', Item)
        'test'  # If no conflicts
        >>> ensure_unique_slug('test', Item)
        'test-2'  # If 'test' already exists
        >>> ensure_unique_slug('disc-1', Artefact, scope_filter={'item_id': 3})
        'disc-1'  # Unique within item 3
    """
    def _build_query(slug_value):
        q = model_class.query.filter(model_class.slug == slug_value)
        if existing_id:
            q = q.filter(model_class.id != existing_id)
        if scope_filter:
            q = q.filter_by(**scope_filter)
        return q

    if _build_query(base_slug).first() is None:
        return base_slug

    # Try numbered variants
    counter = 2
    while counter < 1000:  # Safety limit
        candidate = f"{base_slug}-{counter}"
        if _build_query(candidate).first() is None:
            return candidate
        counter += 1

    # Fallback with timestamp if too many conflicts
    import time
    return f"{base_slug}-{int(time.time())}"


def lookup_by_identifier(model_class, identifier: str):
    """
    Look up a model by full UUID (32 hex chars) or short-UUID+slug identifier.

    Accepts:
      '3f4a9b2cabc123def456789012345678'  -> exact UUID match
      '3f4a9b2c'                           -> first-8-char UUID prefix match
      '3f4a9b2c-elite-bbc-micro'           -> first-8-char UUID prefix match
                                              (slug suffix is decorative, ignored for lookup)

    Returns:
        Model instance, or aborts with 404 if not found or identifier is invalid.
    """
    from flask import abort
    if re.fullmatch(r'[0-9a-f]{32}', identifier):
        return model_class.query.filter_by(uuid=identifier).first_or_404()
    if len(identifier) >= 8 and re.fullmatch(r'[0-9a-f]{8}', identifier[:8]):
        prefix = identifier[:8]
        return model_class.query.filter(
            model_class.uuid.startswith(prefix)
        ).first_or_404()
    abort(404)


def lookup_artefact_by_id(item, artefact_id: str):
    """
    Look up an artefact within an item by slug, full UUID, or short UUID prefix.

    Resolution order:
      1. Full 32-char UUID → exact UUID match (scoped to item)
      2. Pure slug (no leading hex chars) → slug match within item
      3. 8-char hex prefix (optionally followed by -slug) → UUID prefix match within item

    Returns:
        Artefact instance, or aborts with 404 if not found or identifier is invalid.
    """
    from flask import abort
    from myapp.database import Artefact

    # Full UUID
    if re.fullmatch(r'[0-9a-f]{32}', artefact_id):
        return Artefact.query.filter_by(
            uuid=artefact_id, item_id=item.id
        ).first_or_404()

    # 8-char hex prefix (new short-UUID style, with optional -slug suffix)
    if len(artefact_id) >= 8 and re.fullmatch(r'[0-9a-f]{8}', artefact_id[:8]):
        prefix = artefact_id[:8]
        return Artefact.query.filter(
            Artefact.item_id == item.id,
            Artefact.uuid.startswith(prefix)
        ).first_or_404()

    # Pure slug lookup within item
    artefact = Artefact.query.filter_by(slug=artefact_id, item_id=item.id).first()
    if artefact:
        return artefact

    abort(404)

# vim: ts=4 sw=4 et
