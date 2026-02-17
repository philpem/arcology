"""
Slug generation utilities for Arcology.

Generates URL-safe slugs from item/artefact/analysis names for use in
file paths and URLs. Slugs are immutable once set.
"""

import re
from typing import Optional


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

    # Generate new slug
    slug = generate_slug(text, max_length=max_length)

    # Save to object
    if hasattr(obj, 'slug'):
        obj.slug = slug
        db.session.commit()
    else:
        raise AttributeError(f"Object {obj} does not have 'slug' field")

    return slug


def get_slug(obj) -> Optional[str]:
    """
    Get slug from object without creating one.

    Args:
        obj: Database object with slug field

    Returns:
        Slug string if exists, None otherwise
    """
    return getattr(obj, 'slug', None)


def ensure_unique_slug(base_slug: str, model_class, existing_id: Optional[int] = None) -> str:
    """
    Ensure slug is unique by appending number if necessary.

    Args:
        base_slug: Base slug to check
        model_class: SQLAlchemy model class (Item, Artefact, etc.)
        existing_id: ID to exclude from uniqueness check (for updates)

    Returns:
        Unique slug (may have -2, -3, etc. appended)

    Examples:
        >>> ensure_unique_slug('test', Item)
        'test'  # If no conflicts
        >>> ensure_unique_slug('test', Item)
        'test-2'  # If 'test' already exists
    """
    from sqlalchemy import and_

    # Check if base slug is available
    query = model_class.query.filter(model_class.slug == base_slug)
    if existing_id:
        query = query.filter(model_class.id != existing_id)

    if query.first() is None:
        return base_slug

    # Try numbered variants
    counter = 2
    while counter < 1000:  # Safety limit
        candidate = f"{base_slug}-{counter}"
        query = model_class.query.filter(model_class.slug == candidate)
        if existing_id:
            query = query.filter(model_class.id != existing_id)

        if query.first() is None:
            return candidate

        counter += 1

    # Fallback with timestamp if too many conflicts
    import time
    return f"{base_slug}-{int(time.time())}"
