"""
Arcology Database Models

Models for the digital artefact catalogue system.
"""

from datetime import datetime, timezone
from typing import Optional
import secrets
import uuid as uuid_module
from sqlalchemy import (
    Column, ForeignKey, Sequence, Text, BigInteger, Index, Table
)
from sqlalchemy import Integer, String, Boolean, DateTime, Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
import bcrypt
import enum

from .extensions import db


def generate_uuid() -> str:
    """Generate a new UUID4 string for use as a public identifier."""
    return uuid_module.uuid4().hex


# =============================================================================
# Enums
# =============================================================================

class UserPermission(enum.Enum):
    """Permission level for a web UI user. Controls all actions in both the web UI and the API."""
    READ_ONLY  = "read_only"   # View everything; no modifications
    READ_WRITE = "read_write"  # Full CRUD access


class ApiKeyPermission(enum.Enum):
    """Permission level for an application API key."""
    READ_ONLY   = "read_only"    # GET requests only
    READ_UPLOAD = "read_upload"  # GET + create items/artefacts/analysis (no DELETE or PUT-to-update)
    READ_WRITE  = "read_write"   # Full access (GET + POST + PUT + DELETE)


_API_KEY_PERMISSION_ORDER = [
    ApiKeyPermission.READ_ONLY,
    ApiKeyPermission.READ_UPLOAD,
    ApiKeyPermission.READ_WRITE,
]


from shared.enums import ArtefactType, AnalysisType


class AnalysisStatus(enum.Enum):
    """Status of an analysis job."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class FilesystemType(enum.Enum):
    """Known filesystem types."""
    FAT12 = "fat12"
    FAT16 = "fat16"
    FAT32 = "fat32"
    NTFS = "ntfs"
    HPFS = "hpfs"
    HFS = "hfs"
    HFS_PLUS = "hfs_plus"
    ADFS = "adfs"
    DFS = "dfs"
    AMIGA_OFS = "amiga_ofs"
    AMIGA_FFS = "amiga_ffs"
    ISO9660 = "iso9660"
    CDFS = "cdfs"
    CPM = "cpm"
    UNKNOWN = "unknown"
    OTHER = "other"


class StorageDirectory(enum.Enum):
    """Where an artefact file is stored."""
    UPLOADS = "uploads"    # Original user-uploaded files
    OUTPUTS = "outputs"    # Derived/generated files (from analysis)


# =============================================================================
# Association Tables
# =============================================================================

item_tags = Table(
    "item_tags",
    db.Model.metadata,
    Column("item_id", Integer, ForeignKey("items.id"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id"), primary_key=True),
)

artefact_tags = Table(
    "artefact_tags",
    db.Model.metadata,
    Column("artefact_id", Integer, ForeignKey("artefacts.id"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id"), primary_key=True),
)


# =============================================================================
# User Model (from template)
# =============================================================================

class User(db.Model):
    __tablename__ = 'user'
    id            = Column(Integer, Sequence('user_id_seq'), primary_key=True)
    username      = Column(String(50), nullable=False, unique=True)
    password_hash = Column(String(72), nullable=False)
    is_admin      = Column(Boolean, nullable=False, default=False)
    permission    = Column(SQLEnum(UserPermission), nullable=False, default=UserPermission.READ_WRITE)
    can_use_api   = Column(Boolean, nullable=False, default=False)

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False

    def get_id(self):
        if self.id is not None:
            return str(self.id)

    def has_permission(self, required: UserPermission) -> bool:
        order = [UserPermission.READ_ONLY, UserPermission.READ_WRITE]
        return order.index(self.permission) >= order.index(required)

    def setPassword(self, password):
        self.password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    def checkPassword(self, password):
        try:
            return bcrypt.checkpw(password.encode('utf-8'), self.password_hash.encode('utf-8'))
        except (ValueError, TypeError):
            return False


class ApiKey(db.Model):
    """An application key granting programmatic access to the REST API."""
    __tablename__ = 'api_keys'

    id:           Mapped[int]                  = mapped_column(primary_key=True)
    user_id:      Mapped[int]                  = mapped_column(ForeignKey("user.id"), index=True)
    name:         Mapped[str]                  = mapped_column(String(100))
    key_prefix:   Mapped[str]                  = mapped_column(String(8))   # First 8 hex chars; display only
    key_hash:     Mapped[str]                  = mapped_column(String(72), unique=True, index=True)
    permission:   Mapped[ApiKeyPermission]     = mapped_column(SQLEnum(ApiKeyPermission))
    is_active:    Mapped[bool]                 = mapped_column(Boolean, default=True)
    created_at:   Mapped[datetime]             = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at: Mapped[Optional[datetime]]   = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="api_keys")

    def effective_permission(self) -> ApiKeyPermission:
        """Return the key's permission capped by the owning user's permission."""
        if self.user.permission == UserPermission.READ_ONLY:
            return ApiKeyPermission.READ_ONLY
        return self.permission

    @classmethod
    def create(cls, user_id: int, name: str, permission: ApiKeyPermission) -> tuple["ApiKey", str]:
        """
        Create a new ApiKey.  Returns (key_object, raw_key).
        The raw_key is shown to the user exactly once; only the bcrypt hash is stored.
        """
        raw    = f"arc_{secrets.token_hex(32)}"
        prefix = raw[4:12]
        hashed = bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode('utf-8')
        return cls(user_id=user_id, name=name, key_prefix=prefix,
                   key_hash=hashed, permission=permission), raw

    @classmethod
    def verify(cls, raw_key: str) -> Optional["ApiKey"]:
        """
        Look up an active key by its raw value.
        Returns the ApiKey, or None if missing/invalid/inactive.
        """
        if not raw_key or not raw_key.startswith('arc_'):
            return None
        prefix = raw_key[4:12]
        candidates = cls.query.filter_by(key_prefix=prefix, is_active=True).all()
        for key in candidates:
            try:
                if bcrypt.checkpw(raw_key.encode(), key.key_hash.encode()):
                    return key
            except (ValueError, TypeError):
                continue
        return None


# =============================================================================
# External System Integration
# =============================================================================

class ExternalSystem(db.Model):
    """
    A cataloguing system that Arcology can link to.
    """
    __tablename__ = "external_systems"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    system_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    base_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    url_template: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    references: Mapped[list["ExternalReference"]] = relationship(
        back_populates="system", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<ExternalSystem {self.id}: {self.name}>"


class ExternalReference(db.Model):
    """A link between an Arcology item and an external system record."""
    __tablename__ = "external_references"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("external_systems.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(200), index=True)
    external_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    item: Mapped["Item"] = relationship(back_populates="external_references")
    system: Mapped["ExternalSystem"] = relationship(back_populates="references")

    __table_args__ = (
        Index("ix_external_references_system_external", "system_id", "external_id"),
    )

    @property
    def url(self) -> Optional[str]:
        if self.external_url:
            return self.external_url
        if self.system.base_url and self.system.url_template:
            return self.system.base_url + self.system.url_template.format(id=self.external_id)
        return None


# =============================================================================
# Taxonomy
# =============================================================================

class Platform(db.Model):
    """Computer platform/system - hierarchical."""
    __tablename__ = "platforms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("platforms.id"), nullable=True)

    parent: Mapped[Optional["Platform"]] = relationship(back_populates="children", remote_side=[id])
    children: Mapped[list["Platform"]] = relationship(back_populates="parent")
    items: Mapped[list["Item"]] = relationship(back_populates="platform")


class Category(db.Model):
    """Software category."""
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"), nullable=True)

    parent: Mapped[Optional["Category"]] = relationship(back_populates="children", remote_side=[id])
    children: Mapped[list["Category"]] = relationship(back_populates="parent")
    items: Mapped[list["Item"]] = relationship(back_populates="category")


class Tag(db.Model):
    """Flexible tagging for items and artefacts."""
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    items: Mapped[list["Item"]] = relationship(secondary=item_tags, back_populates="tags")
    artefacts: Mapped[list["Artefact"]] = relationship(secondary=artefact_tags, back_populates="tags")


# =============================================================================
# Core Models
# =============================================================================

class Item(db.Model):
    """A logical item in the collection."""
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(32), unique=True, index=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(255), index=True)
    slug: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)  # URL-safe slug (immutable once set)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    platform_id: Mapped[Optional[int]] = mapped_column(ForeignKey("platforms.id"), index=True, nullable=True)
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    platform: Mapped[Optional["Platform"]] = relationship(back_populates="items")
    category: Mapped[Optional["Category"]] = relationship(back_populates="items")
    artefacts: Mapped[list["Artefact"]] = relationship(back_populates="item", cascade="all, delete-orphan")
    tags: Mapped[list["Tag"]] = relationship(secondary=item_tags, back_populates="items")
    external_references: Mapped[list["ExternalReference"]] = relationship(back_populates="item", cascade="all, delete-orphan")

    @property
    def url_id(self) -> str:
        """Short URL identifier: 8-char UUID prefix, plus slug if available."""
        prefix = self.uuid[:8]
        if self.slug:
            return f"{prefix}-{self.slug}"
        return prefix

    def get_reference(self, system_name: str) -> Optional["ExternalReference"]:
        for ref in self.external_references:
            if ref.system.name == system_name:
                return ref
        return None


class Artefact(db.Model):
    """A single digital artefact - one disc image, one scan, etc."""
    __tablename__ = "artefacts"
    __table_args__ = (
        # Prevent duplicate derived artefacts for the same analysis run + output file.
        # NULL values are not considered equal in SQL so original (non-derived)
        # artefacts (where derived_from_analysis_id IS NULL) are unaffected.
        db.UniqueConstraint(
            'derived_from_analysis_id', 'storage_path',
            name='uq_artefact_analysis_storage_path',
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(32), unique=True, index=True, default=generate_uuid)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    label: Mapped[str] = mapped_column(String(255))
    slug: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)  # URL-safe slug (immutable once set)
    artefact_type: Mapped[ArtefactType] = mapped_column(SQLEnum(ArtefactType))
    type_overridden: Mapped[bool] = mapped_column(Boolean, default=False)  # Was type manually set?
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # File storage
    original_filename: Mapped[str] = mapped_column(String(255))  # User's original filename
    storage_path: Mapped[str] = mapped_column(String(1000))      # Filename in storage folder
    storage_directory: Mapped[StorageDirectory] = mapped_column(
        SQLEnum(StorageDirectory), default=StorageDirectory.UPLOADS
    )  # Which folder: uploads (original) or outputs (derived)
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    
    # Hashes (computed after upload)
    md5: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    
    # Format-specific metadata (JSON)
    media_metadata: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Derivation chain - if this artefact was produced by analysing another
    parent_artefact_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("artefacts.id"), index=True, nullable=True
    )
    derived_from_analysis_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("analyses.id"), index=True, nullable=True
    )
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    item: Mapped["Item"] = relationship(back_populates="artefacts")
    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="artefact", cascade="all, delete-orphan",
        foreign_keys="Analysis.artefact_id"
    )
    partitions: Mapped[list["Partition"]] = relationship(back_populates="artefact", cascade="all, delete-orphan")
    protection_indicators: Mapped[list["ArtefactProtection"]] = relationship(
        back_populates="artefact", cascade="all, delete-orphan"
    )
    mastering_indicators: Mapped[list["ArtefactMastering"]] = relationship(
        back_populates="artefact", cascade="all, delete-orphan"
    )

    # Derived artefacts (e.g., sector image from flux decode)
    parent_artefact: Mapped[Optional["Artefact"]] = relationship(
        back_populates="derived_artefacts", remote_side=[id],
        foreign_keys=[parent_artefact_id]
    )
    derived_artefacts: Mapped[list["Artefact"]] = relationship(
        back_populates="parent_artefact", foreign_keys=[parent_artefact_id],
        cascade="all, delete-orphan"
    )
    derived_from_analysis: Mapped[Optional["Analysis"]] = relationship(
        foreign_keys=[derived_from_analysis_id]
    )
    tags: Mapped[list["Tag"]] = relationship(secondary=artefact_tags, back_populates="artefacts")

    @property
    def root_artefact(self) -> "Artefact":
        """Walk up the parent chain to the original uploaded artefact (no parent)."""
        a = self
        while a.parent_artefact_id is not None:
            a = a.parent_artefact
        return a

    @property
    def url_slug(self) -> str:
        """Slug-based URL segment for use within an item URL."""
        return self.slug if self.slug else self.uuid[:8]


class Analysis(db.Model):
    """Results from analysing an artefact - auto-triggered based on artefact type."""
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(32), unique=True, index=True, default=generate_uuid)
    artefact_id: Mapped[int] = mapped_column(ForeignKey("artefacts.id"), index=True)
    analysis_type: Mapped[AnalysisType] = mapped_column(SQLEnum(AnalysisType))
    slug: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)  # URL-safe slug (immutable once set)
    status: Mapped[AnalysisStatus] = mapped_column(SQLEnum(AnalysisStatus), default=AnalysisStatus.PENDING)
    
    # Tool info (filled by worker)
    tool_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    tool_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    
    # Hints to help analysis (JSON) - e.g., {"platform": "bbc_micro", "filesystem": "adfs"}
    hints: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Results
    output_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    output_path: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    success: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON for structured results
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    artefact: Mapped["Artefact"] = relationship(
        back_populates="analyses", foreign_keys=[artefact_id]
    )
    
    # Artefacts produced by this analysis (e.g., decoded sector image from flux)
    produced_artefacts: Mapped[list["Artefact"]] = relationship(
        foreign_keys="Artefact.derived_from_analysis_id",
        viewonly=True
    )


# =============================================================================
# File Listings
# =============================================================================

class Partition(db.Model):
    """A partition or filesystem within an artefact."""
    __tablename__ = "partitions"
    __table_args__ = (
        db.UniqueConstraint('artefact_id', 'partition_index', name='uq_partition_artefact_index'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(32), unique=True, index=True, default=generate_uuid)
    artefact_id: Mapped[int] = mapped_column(ForeignKey("artefacts.id"), index=True)
    partition_index: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    slug: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)  # URL-safe slug (immutable once set)
    filesystem: Mapped[FilesystemType] = mapped_column(SQLEnum(FilesystemType))
    container_format: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Detailed format from disc image tools (e.g., "Acorn ADFS E")
    start_sector: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sector_count: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    block_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_files: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_directories: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    unique_files: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    detection_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON from partition detection (sfdisk, etc.)
    gnu_file_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)  # Output of file(1) on the image
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    artefact: Mapped["Artefact"] = relationship(back_populates="partitions")
    files: Mapped[list["ExtractedFile"]] = relationship(back_populates="partition", cascade="all, delete-orphan")
    recognised_products: Mapped[list["RecognisedProduct"]] = relationship(back_populates="partition", cascade="all, delete-orphan")


class ExtractedFile(db.Model):
    """A file found within a partition."""
    __tablename__ = "extracted_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(32), unique=True, index=True, default=generate_uuid)
    partition_id: Mapped[int] = mapped_column(ForeignKey("partitions.id"), index=True)
    path: Mapped[str] = mapped_column(String(1000))
    filename: Mapped[str] = mapped_column(String(255), index=True)
    extension: Mapped[Optional[str]] = mapped_column(String(20), index=True, nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    modified_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    accessed_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    attributes: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    md5: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    sha1: Mapped[Optional[str]] = mapped_column(String(40), index=True, nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    crc32: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    known_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("known_files.id", ondelete="SET NULL"), index=True, nullable=True)
    is_known: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Archive/nested file support
    parent_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("extracted_files.id"), nullable=True, index=True)
    is_archive: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_directory: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # True if this is a directory entry
    archive_format: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # e.g., 'ArcFS', 'ZIP', 'CFS'
    risc_os_filetype: Mapped[Optional[str]] = mapped_column(String(3), nullable=True, index=True)  # Hex filetype (e.g., '3fb')
    extraction_depth: Mapped[int] = mapped_column(Integer, default=0)  # Nesting level (0=top-level)

    partition: Mapped["Partition"] = relationship(back_populates="files")
    known_file: Mapped[Optional["KnownFile"]] = relationship()

    # Self-referential relationship for parent/child archives
    parent_file: Mapped[Optional["ExtractedFile"]] = relationship(
        "ExtractedFile",
        remote_side=[id],
        foreign_keys=[parent_file_id],
        backref="child_files"
    )

    __table_args__ = (
        Index("ix_extracted_files_partition_known", "partition_id", "is_known"),
        Index("ix_extracted_files_archive", "is_archive", "risc_os_filetype"),
        Index("ix_extracted_files_parent", "parent_file_id", "extraction_depth"),
    )


# =============================================================================
# Search Index Tables
# =============================================================================

class ArtefactProtection(db.Model):
    """Copy protection indicators detected on a disc artefact.

    Populated server-side when a DISC_PROTECTION_DETECT analysis completes.
    One row per indicator instance (a single disc may have many).
    """
    __tablename__ = 'artefact_protection'

    id: Mapped[int] = mapped_column(primary_key=True)
    artefact_id: Mapped[int] = mapped_column(ForeignKey('artefacts.id'), index=True)
    protection_type: Mapped[str] = mapped_column(String(64), index=True)
    # Known values: 'weak_bits', 'bad_crc', 'id_mismatch', 'ddam', 'duplicate_id'
    track: Mapped[Optional[int]] = mapped_column(nullable=True)
    side: Mapped[Optional[int]] = mapped_column(nullable=True)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # e.g. sector ID string

    artefact: Mapped["Artefact"] = relationship(back_populates="protection_indicators")


class ArtefactMastering(db.Model):
    """Mastering / duplicator fingerprint indicators detected on a disc artefact.

    Populated server-side when a DISC_MASTERING_DETECT analysis completes.
    One row per indicator instance found.
    """
    __tablename__ = 'artefact_mastering'

    id: Mapped[int] = mapped_column(primary_key=True)
    artefact_id: Mapped[int] = mapped_column(ForeignKey('artefacts.id'), index=True)
    mastering_type: Mapped[str] = mapped_column(String(64), index=True)
    # Known values: 'traceback', 'bcd_timestamp', 'unknown_mastering'
    track: Mapped[Optional[int]] = mapped_column(nullable=True)
    decoded: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Decoded mastering data string

    artefact: Mapped["Artefact"] = relationship(back_populates="mastering_indicators")


# =============================================================================
# Known File Database
# =============================================================================

class HashDatabase(db.Model):
    """A source of known file hashes for elimination."""
    __tablename__ = "hash_databases"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    platform_id: Mapped[Optional[int]] = mapped_column(ForeignKey("platforms.id", ondelete="SET NULL"), nullable=True)
    file_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    enable_product_recognition: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    platform: Mapped[Optional["Platform"]] = relationship()
    known_files: Mapped[list["KnownFile"]] = relationship(back_populates="database", cascade="all, delete-orphan")
    known_products: Mapped[list["KnownProduct"]] = relationship(back_populates="database", cascade="all, delete-orphan")


class KnownProduct(db.Model):
    """A named product/application/group within a hash database."""
    __tablename__ = "known_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    database_id: Mapped[int] = mapped_column(ForeignKey("hash_databases.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    path_match_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    database: Mapped["HashDatabase"] = relationship(back_populates="known_products")
    known_files: Mapped[list["KnownFile"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    recognised_in: Mapped[list["RecognisedProduct"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class KnownFile(db.Model):
    """A known file from a hash database, grouped under a KnownProduct."""
    __tablename__ = "known_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    database_id: Mapped[int] = mapped_column(ForeignKey("hash_databases.id"), index=True)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("known_products.id"), index=True, nullable=True)
    filename: Mapped[str] = mapped_column(String(255), index=True)
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    md5: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    sha1: Mapped[Optional[str]] = mapped_column(String(40), index=True, nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    crc32: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    relative_path: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    product_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    product_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    database: Mapped["HashDatabase"] = relationship(back_populates="known_files")
    product: Mapped[Optional["KnownProduct"]] = relationship(back_populates="known_files")

    __table_args__ = (
        Index("ix_known_files_md5_size", "md5", "file_size"),
        Index("ix_known_files_sha1_size", "sha1", "file_size"),
    )


class RecognisedProduct(db.Model):
    """Result of a PRODUCT_RECOGNITION analysis: a folder matched a KnownProduct."""
    __tablename__ = "recognised_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    partition_id: Mapped[int] = mapped_column(ForeignKey("partitions.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("known_products.id"), index=True)
    folder_path: Mapped[str] = mapped_column(String(1000))
    required_matched: Mapped[int] = mapped_column(Integer, default=0)
    required_total: Mapped[int] = mapped_column(Integer, default=0)
    optional_matched: Mapped[int] = mapped_column(Integer, default=0)
    optional_total: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    partition: Mapped["Partition"] = relationship(back_populates="recognised_products")
    product: Mapped["KnownProduct"] = relationship(back_populates="recognised_in")

    __table_args__ = (
        Index("ix_recognised_products_partition_product", "partition_id", "product_id"),
    )


# vim: ts=4 sw=4 et
