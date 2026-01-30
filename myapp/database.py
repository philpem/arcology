"""
Arcology Database Models

Models for the digital artefact catalogue system.
"""

from datetime import datetime
from typing import Optional
from sqlalchemy import (
    Column, ForeignKey, Sequence, Text, BigInteger, Index, Table
)
from sqlalchemy import Integer, String, Boolean, DateTime, Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
import bcrypt
import enum

from .extensions import db


# =============================================================================
# Enums
# =============================================================================

class ArtefactType(enum.Enum):
    """Types of digital artefacts."""
    FLOPPY_SCP = "floppy_scp"
    FLOPPY_IMD = "floppy_imd"
    FLOPPY_RAW = "floppy_raw"
    HDD_IMAGE = "hdd_image"
    CDROM_ISO = "cdrom_iso"
    CDROM_BIN = "cdrom_bin"
    SCAN_PDF = "scan_pdf"
    SCAN_IMAGE = "scan_image"
    OTHER = "other"


class AnalysisType(enum.Enum):
    """Types of analysis that can be performed."""
    FLUX_GRAPH = "flux_graph"
    SECTOR_DUMP = "sector_dump"
    FILE_LISTING = "file_listing"
    METADATA_EXTRACT = "metadata_extract"
    CHECKSUM_VERIFY = "checksum_verify"
    OTHER = "other"


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


# =============================================================================
# Association Tables
# =============================================================================

item_tags = Table(
    "item_tags",
    db.Model.metadata,
    Column("item_id", Integer, ForeignKey("items.id"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id"), primary_key=True),
)


# =============================================================================
# User Model (from template)
# =============================================================================

class User(db.Model):
    __tablename__ = 'user'
    id = Column(Integer, Sequence('user_id_seq'), primary_key=True)
    username = Column(String(50), nullable=False, unique=True)
    password_hash = Column(String(72), nullable=False)

    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False

    def get_id(self):
        if self.id is not None:
            return str(self.id)

    def setPassword(self, password):
        self.password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

    def checkPassword(self, password):
        try:
            return bcrypt.checkpw(password.encode('utf-8'), self.password_hash)
        except (ValueError, TypeError):
            return False


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    """Computer platform/system."""
    __tablename__ = "platforms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    manufacturer: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
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
    """Flexible tagging for items."""
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    items: Mapped[list["Item"]] = relationship(secondary=item_tags, back_populates="tags")


# =============================================================================
# Core Models
# =============================================================================

class Item(db.Model):
    """A logical item in the collection."""
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    platform_id: Mapped[Optional[int]] = mapped_column(ForeignKey("platforms.id"), index=True, nullable=True)
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    platform: Mapped[Optional["Platform"]] = relationship(back_populates="items")
    category: Mapped[Optional["Category"]] = relationship(back_populates="items")
    artefacts: Mapped[list["Artefact"]] = relationship(back_populates="item", cascade="all, delete-orphan")
    tags: Mapped[list["Tag"]] = relationship(secondary=item_tags, back_populates="items")
    external_references: Mapped[list["ExternalReference"]] = relationship(back_populates="item", cascade="all, delete-orphan")

    def get_reference(self, system_name: str) -> Optional["ExternalReference"]:
        for ref in self.external_references:
            if ref.system.name == system_name:
                return ref
        return None


class Artefact(db.Model):
    """A single digital artefact - one disc image, one scan, etc."""
    __tablename__ = "artefacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    label: Mapped[str] = mapped_column(String(255))
    artefact_type: Mapped[ArtefactType] = mapped_column(SQLEnum(ArtefactType))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    file_path: Mapped[str] = mapped_column(String(1000))
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    md5: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    media_metadata: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    item: Mapped["Item"] = relationship(back_populates="artefacts")
    analyses: Mapped[list["Analysis"]] = relationship(back_populates="artefact", cascade="all, delete-orphan")
    partitions: Mapped[list["Partition"]] = relationship(back_populates="artefact", cascade="all, delete-orphan")


class Analysis(db.Model):
    """Results from analysing an artefact."""
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    artefact_id: Mapped[int] = mapped_column(ForeignKey("artefacts.id"), index=True)
    analysis_type: Mapped[AnalysisType] = mapped_column(SQLEnum(AnalysisType))
    status: Mapped[AnalysisStatus] = mapped_column(SQLEnum(AnalysisStatus), default=AnalysisStatus.PENDING)
    tool_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    tool_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    output_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    output_path: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    success: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    artefact: Mapped["Artefact"] = relationship(back_populates="analyses")


# =============================================================================
# File Listings
# =============================================================================

class Partition(db.Model):
    """A partition or filesystem within an artefact."""
    __tablename__ = "partitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    artefact_id: Mapped[int] = mapped_column(ForeignKey("artefacts.id"), index=True)
    partition_index: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    filesystem: Mapped[FilesystemType] = mapped_column(SQLEnum(FilesystemType))
    start_sector: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sector_count: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    block_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_files: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_directories: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    unique_files: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    artefact: Mapped["Artefact"] = relationship(back_populates="partitions")
    files: Mapped[list["ExtractedFile"]] = relationship(back_populates="partition", cascade="all, delete-orphan")


class ExtractedFile(db.Model):
    """A file found within a partition."""
    __tablename__ = "extracted_files"

    id: Mapped[int] = mapped_column(primary_key=True)
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
    known_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("known_files.id"), index=True, nullable=True)
    is_known: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    partition: Mapped["Partition"] = relationship(back_populates="files")
    known_file: Mapped[Optional["KnownFile"]] = relationship()

    __table_args__ = (Index("ix_extracted_files_partition_known", "partition_id", "is_known"),)


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
    platform_id: Mapped[Optional[int]] = mapped_column(ForeignKey("platforms.id"), nullable=True)
    file_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    platform: Mapped[Optional["Platform"]] = relationship()
    known_files: Mapped[list["KnownFile"]] = relationship(back_populates="database", cascade="all, delete-orphan")


class KnownFile(db.Model):
    """A known file from a hash database."""
    __tablename__ = "known_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    database_id: Mapped[int] = mapped_column(ForeignKey("hash_databases.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255), index=True)
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    md5: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    sha1: Mapped[Optional[str]] = mapped_column(String(40), index=True, nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    crc32: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    product_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    product_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    database: Mapped["HashDatabase"] = relationship(back_populates="known_files")

    __table_args__ = (
        Index("ix_known_files_md5_size", "md5", "file_size"),
        Index("ix_known_files_sha1_size", "sha1", "file_size"),
    )


# vim: ts=4 sw=4 noet
