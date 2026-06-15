"""Arcology - Restriction collection service

Pure query/collection helpers for artefact- and file-level download
restrictions, shared by the web download gates and the REST API.  The
web-specific enforcement wrappers (flash + redirect) live in the artefacts
blueprint; the JSON enforcement lives in the API blueprint — both consume
these collectors.

Moved verbatim from myapp/blueprints/artefacts.py.
"""

from ..database import (
    ArtefactRestriction,
    ExtractedFile,
    ExtractedFileRestriction,
    Partition,
)
from ..extensions import db
from .artefact_lifecycle import get_all_derived_artefact_ids


def collect_ancestor_file_restrictions(ef):
    """Return all ExtractedFileRestriction objects on any ancestor of ef.

    Walks up the parent_file chain.  If an archive is restricted, every file
    inside it is also effectively restricted.
    """
    restrictions = []
    current = ef.parent_file
    while current is not None:
        restrictions.extend(current.restrictions)
        current = current.parent_file
    return restrictions


def collect_all_file_restrictions(ef):
    """Return all ExtractedFileRestriction objects on ef and every descendant.

    For non-archive files this is O(1).  For archives the child_files tree is
    walked recursively; SQLAlchemy loads each level on access via the backref.
    """
    restrictions = list(ef.restrictions)
    for child in ef.child_files:
        restrictions.extend(collect_all_file_restrictions(child))
    return restrictions


def artefact_contained_file_restrictions(artefact):
    """All ExtractedFileRestriction objects on the artefact's own extracted files.

    A single query over every partition of this artefact.  Shared by the web
    download gate and the REST API so both block downloading an artefact whose
    extracted contents are restricted.
    """
    return (
        ExtractedFileRestriction.query
        .join(ExtractedFile, ExtractedFileRestriction.extracted_file_id == ExtractedFile.id)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .filter(Partition.artefact_id == artefact.id)
        .all()
    )


def grantable_bypass_rtypes(artefact, all_artefact_ids=None):
    """RestrictionTypes a per-user bypass can be granted for on this artefact.

    Covers both artefact-level and file-level restrictions across this artefact
    *and any artefacts derived from it* — the artefact detail page surfaces
    restrictions from the whole derived tree, so the grant form must offer those
    types too.

    A grant is created against this artefact; download enforcement walks each
    restricted artefact/file up its derivation chain (Artefact.ancestor_ids), so
    a grant here cascades to cover restrictions anywhere in the tree below it.

    Pass *all_artefact_ids* (current artefact ID + its derived IDs) when the
    caller has already computed them, to avoid re-running the derived-IDs
    recursive CTE.
    """
    if all_artefact_ids is not None:
        all_ids = all_artefact_ids
    else:
        all_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)
    art_rtypes = (
        db.session.query(ArtefactRestriction.restriction_type)
        .filter(ArtefactRestriction.artefact_id.in_(all_ids))
        .distinct()
    )
    types = {row[0] for row in art_rtypes}
    file_rtypes = (
        db.session.query(ExtractedFileRestriction.restriction_type)
        .join(ExtractedFile, ExtractedFileRestriction.extracted_file_id == ExtractedFile.id)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .filter(Partition.artefact_id.in_(all_ids))
        .distinct()
    )
    types.update(row[0] for row in file_rtypes)
    return types

# vim: ts=4 sw=4 et
