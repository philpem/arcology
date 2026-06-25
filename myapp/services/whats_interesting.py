"""'What's interesting about this disc?' triage summary.

The curator's question for a freshly-ingested disc image is not "how rare is
this within my collection" but "how does this differ from what I already
*know*" — i.e. from the reference hash databases we hold.  This module buckets
every (non-directory) extracted file on an artefact tree against those
references:

* **Standard OS** — the file is linked to a :class:`HashDatabase` flagged
  ``exclude_from_similarity`` (base-OS / runtime boilerplate).  Boring: a stock
  RISC OS install tells us nothing.
* **Recognised software** — the file is linked to a *normal* (non-excluded)
  reference database, or its folder was matched by product recognition.  Worth
  naming ("contains !ArtWorks").
* **Unknown** — ``known_file_id IS NULL``: not in any reference.  This is the
  headline — candidate user content or unarchived material.

The summary is pure synthesis of existing data (no schema change).  It is scoped
to a single artefact tree (the one the viewer is already permitted to see), and
uses the same ``all_artefact_ids`` set as the file listing on the same page, so
its counts agree with the table below it.
"""

from dataclasses import dataclass, field
from ..database import (
    ExtractedFile,
    HashDatabase,
    KnownFile,
    KnownProduct,
    Partition,
    RecognisedProduct,
    db,
)

# Cap the number of named products / OS databases we surface in the card so a
# pathological disc cannot produce an unbounded list.
_NAME_LIMIT = 100


@dataclass
class Bucket:
    """A file-count and byte-total for one triage category."""

    count: int = 0
    size: int = 0


@dataclass
class InterestingSummary:
    """Triage of an artefact tree's files against the reference hash databases."""

    standard_os: Bucket = field(default_factory=Bucket)
    recognised: Bucket = field(default_factory=Bucket)
    unknown: Bucket = field(default_factory=Bucket)
    # Distinct names of the excluded (base-OS) databases that matched, e.g.
    # "RISC OS 3.6" — used for the "Standard <X> (hidden)" lead-in.
    standard_os_names: list[str] = field(default_factory=list)
    # Distinct recognised-software (non-base) product titles, e.g.
    # "!ArtWorks 1.5".  Populated from per-file hash matches *and* folder-level
    # product recognition, so this can be non-empty even when ``recognised.count``
    # is 0 (a product identified by folder with no individual file hash-match).
    recognised_products: list[str] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        return self.standard_os.count + self.recognised.count + self.unknown.count

    @property
    def total_bytes(self) -> int:
        return self.standard_os.size + self.recognised.size + self.unknown.size

    @property
    def has_files(self) -> bool:
        return self.total_count > 0

    @property
    def has_references(self) -> bool:
        """Whether anything on the disc matched a reference at all.

        When nothing is known the card is just "everything is unknown", which is
        less interesting than a contrast — callers may choose a quieter
        presentation in that case.
        """
        return self.standard_os.count > 0 or self.recognised.count > 0


def summarise_artefact(all_artefact_ids) -> InterestingSummary:
    """Bucket every non-directory extracted file under *all_artefact_ids*.

    *all_artefact_ids* is the artefact plus its derived artefacts (the same set
    the file listing uses), so the buckets agree with the table on the page.
    """
    summary = InterestingSummary()
    if not all_artefact_ids:
        return summary

    # One grouped pass over the files: the outer joins leave excluded == NULL for
    # files with no known-file link (the "unknown" bucket), True for base-OS
    # databases, and False for normal reference databases.
    rows = (
        db.session.query(
            HashDatabase.exclude_from_similarity,
            db.func.count(ExtractedFile.id),
            db.func.coalesce(db.func.sum(ExtractedFile.file_size), 0),
        )
        .select_from(ExtractedFile)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .outerjoin(KnownFile, ExtractedFile.known_file_id == KnownFile.id)
        .outerjoin(HashDatabase, KnownFile.database_id == HashDatabase.id)
        .filter(Partition.artefact_id.in_(all_artefact_ids))
        .filter(ExtractedFile.is_directory.is_(False))
        .group_by(HashDatabase.exclude_from_similarity)
        .all()
    )
    for excluded, count, size in rows:
        bucket = Bucket(int(count or 0), int(size or 0))
        if excluded is None:
            summary.unknown = bucket
        elif excluded:
            summary.standard_os = bucket
        else:
            summary.recognised = bucket

    if not summary.has_files:
        return summary

    # Names of the base-OS databases that matched (for "Standard RISC OS 3.6").
    # Only files linked to an excluded database can contribute, so the bucket
    # count already tells us whether this scan can return anything — skip it
    # entirely when nothing landed in the standard-OS bucket.
    if summary.standard_os.count:
        summary.standard_os_names = [
            name
            for (name,) in (
                db.session.query(HashDatabase.name)
                .select_from(ExtractedFile)
                .join(Partition, ExtractedFile.partition_id == Partition.id)
                .join(KnownFile, ExtractedFile.known_file_id == KnownFile.id)
                .join(HashDatabase, KnownFile.database_id == HashDatabase.id)
                .filter(Partition.artefact_id.in_(all_artefact_ids))
                .filter(ExtractedFile.is_directory.is_(False))
                .filter(HashDatabase.exclude_from_similarity.is_(True))
                .distinct()
                .order_by(HashDatabase.name)
                .limit(_NAME_LIMIT)
                .all()
            )
        ]

    # Recognised-software product names, from two sources:
    #  1. products linked directly via non-excluded known files (per-file hash
    #     match) — only possible when the recognised bucket is non-empty;
    #  2. folder-level product recognition (independent of per-file hash links,
    #     so it can name a product — "contains !Draw" — even when no individual
    #     file hash-matched, i.e. with recognised.count == 0).
    # Both restrict to non-base (non-excluded) databases so base-OS products
    # never leak in.  Each query orders by title before its cap so the merged,
    # re-capped result is the true alphabetical head rather than an arbitrary
    # truncation of two independently-capped fetches.
    titles: set[str] = set()
    if summary.recognised.count:
        for (title,) in (
            db.session.query(KnownProduct.title)
            .select_from(ExtractedFile)
            .join(Partition, ExtractedFile.partition_id == Partition.id)
            .join(KnownFile, ExtractedFile.known_file_id == KnownFile.id)
            .join(HashDatabase, KnownFile.database_id == HashDatabase.id)
            .join(KnownProduct, KnownFile.product_id == KnownProduct.id)
            .filter(Partition.artefact_id.in_(all_artefact_ids))
            .filter(ExtractedFile.is_directory.is_(False))
            .filter(HashDatabase.exclude_from_similarity.is_(False))
            .distinct()
            .order_by(KnownProduct.title)
            .limit(_NAME_LIMIT)
            .all()
        ):
            titles.add(title)
    for (title,) in (
        db.session.query(KnownProduct.title)
        .select_from(RecognisedProduct)
        .join(KnownProduct, RecognisedProduct.product_id == KnownProduct.id)
        .join(HashDatabase, KnownProduct.database_id == HashDatabase.id)
        .join(Partition, RecognisedProduct.partition_id == Partition.id)
        .filter(Partition.artefact_id.in_(all_artefact_ids))
        .filter(HashDatabase.exclude_from_similarity.is_(False))
        .distinct()
        .order_by(KnownProduct.title)
        .limit(_NAME_LIMIT)
        .all()
    ):
        titles.add(title)
    summary.recognised_products = sorted(titles)[:_NAME_LIMIT]

    return summary

# vim: ts=4 sw=4 et
