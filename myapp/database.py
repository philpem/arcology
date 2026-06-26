"""
Arcology Database Models

Models for the digital artefact catalogue system.
"""

import secrets
import uuid as uuid_module
from datetime import datetime, timezone
from typing import Optional
import bcrypt
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Table,
    Text,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import false as sa_false
from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select
from sqlalchemy import text as sa_text
from sqlalchemy import true as sa_true
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, column_property, mapped_column, relationship
from sqlalchemy.types import TypeDecorator
from arcology_shared.enums import AnalysisType, ArtefactType
from .enums import (  # noqa: F401 — re-exported for backward-compat call sites
    _API_KEY_PERMISSION_ORDER,
    AnalysisStatus,
    ApiKeyPermission,
    FilesystemType,
    HashRescanStatus,
    ProductRecognitionStatus,
    RestrictionType,
    StorageDirectory,
    UserPermission,
)
from .extensions import db


class _TolerantEnum(TypeDecorator):
    """Enum column that returns None for DB values absent from the Python enum.

    Acts as a crash-shield when the DB contains a value added by a feature
    branch whose migration was downgraded without cleaning up the rows first
    (e.g. NSFW_SCAN left behind after switching back to master).  In a healthy
    database — where downgrade() properly deletes orphan rows — this path is
    never taken.

    DDL still emits as a native SQLEnum (PG enum type / SQLite VARCHAR).  We
    bypass SQLEnum's result_processor so that unknown DB values become None
    instead of raising LookupError before user code can intercept them.
    """
    impl = SQLEnum
    cache_ok = True

    def __init__(self, enum_cls, **kw):
        self._enum_cls = enum_cls
        super().__init__(enum_cls, **kw)

    def result_processor(self, dialect, coltype):
        enum_cls = self._enum_cls

        def process(value):
            if value is None or isinstance(value, enum_cls):
                return value
            try:
                return enum_cls[value]
            except KeyError:
                return None

        return process


def generate_uuid() -> str:
    """Generate a new UUID4 string for use as a public identifier."""
    return uuid_module.uuid4().hex


# Analysis job priority.  Higher values are picked up first.
# ANALYSIS_PRIORITY_LOW: demote a job below normal API/CLI submissions.
# ANALYSIS_PRIORITY_NORMAL: default for API/CLI-submitted jobs.
# ANALYSIS_PRIORITY_HIGH: default for web UI uploads and re-analyses,
#   keeping interactive jobs ahead of bulk API/CLI submissions.
# ANALYSIS_PRIORITY_URGENT: above the web default; raising a re-analysis to this
#   tier requires the can_prioritise_analyses grant (see can_raise_analysis_priority).
# Override the web-UI default via WEB_UI_ANALYSIS_PRIORITY in myapp.cfg or environment.
ANALYSIS_PRIORITY_LOW = -10
ANALYSIS_PRIORITY_NORMAL = 0
ANALYSIS_PRIORITY_HIGH = 10
ANALYSIS_PRIORITY_URGENT = 20

# Single source for the user-facing priority tiers, as ordered (value, label)
# pairs (low -> high).  Used by the re-analyse form, the reprioritise controls,
# and the CLI so the available tiers cannot drift across them.
ANALYSIS_PRIORITY_TIERS = (
    (ANALYSIS_PRIORITY_LOW, 'Low'),
    (ANALYSIS_PRIORITY_NORMAL, 'Normal'),
    (ANALYSIS_PRIORITY_HIGH, 'High'),
    (ANALYSIS_PRIORITY_URGENT, 'Urgent'),
)

# =============================================================================
# Blob deduplication tables (defined before Artefact for FK references)
# =============================================================================

class UploadBlob(db.Model):
    """Globally deduplicated content stored under uploads/."""
    __tablename__ = "upload_blobs"
    __table_args__ = (
        db.UniqueConstraint("file_size", "sha256", name="uq_upload_blob_size_sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    md5: Mapped[str | None] = mapped_column(String(32), nullable=True)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    artefacts: Mapped[list["Artefact"]] = relationship(back_populates="upload_blob")


class OutputBlob(db.Model):
    """Globally deduplicated content stored under outputs/."""
    __tablename__ = "output_blobs"
    __table_args__ = (
        db.UniqueConstraint("file_size", "sha256", name="uq_output_blob_size_sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    md5: Mapped[str | None] = mapped_column(String(32), nullable=True)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    artefacts: Mapped[list["Artefact"]] = relationship(back_populates="output_blob")


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

group_memberships = Table(
    "group_memberships",
    db.Model.metadata,
    Column("user_id", Integer, ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", Integer, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)


# =============================================================================
# User Model (from template)
# =============================================================================

class User(db.Model):
    __tablename__ = 'user'
    __table_args__ = (
        db.UniqueConstraint('oidc_sub', name='uq_user_oidc_sub'),
    )
    id            = Column(Integer, Sequence('user_id_seq'), primary_key=True)
    username      = Column(String(50), nullable=False, unique=True)
    password_hash = Column(String(72), nullable=False)
    is_admin      = Column(Boolean, nullable=False, default=False, server_default=sa_false())
    permission    = Column(SQLEnum(UserPermission), nullable=False, default=UserPermission.READ_WRITE, server_default=UserPermission.READ_WRITE.name)
    can_use_api   = Column(Boolean, nullable=False, default=False, server_default=sa_false())
    can_prioritise_analyses = Column(Boolean, nullable=False, default=False, server_default=sa_false())
    preferences   = Column(JSON, nullable=True, default=None)
    # SSO / OIDC fields (null for local-only accounts)
    oidc_sub      = Column(String(255), nullable=True, index=True)
    email         = Column(String(255), nullable=True)
    oidc_managed  = Column(Boolean, nullable=False, default=False, server_default=sa_false())

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    restriction_bypasses: Mapped[list["UserRestrictionBypass"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    artefact_bypasses: Mapped[list["UserArtefactBypass"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
        foreign_keys="UserArtefactBypass.user_id",
    )
    groups: Mapped[list["Group"]] = relationship(secondary=group_memberships, back_populates="members")

    def can_bypass_restriction(self, restriction_type) -> bool:
        """Check if this user can bypass a specific restriction type.

        Admins implicitly bypass all restriction types.
        """
        if self.is_admin:
            return True
        return any(rb.restriction_type == restriction_type for rb in self.restriction_bypasses)

    def can_bypass_all_restrictions(self, restrictions, artefact_id=None) -> bool:
        """Check if this user can bypass all of the given ArtefactRestriction objects.

        Checks global per-type bypasses first.  When ``artefact_id`` is supplied,
        also checks per-artefact bypasses for any remaining restriction types
        not covered by the global grants.

        ``artefact_id`` may be a single id or an iterable of ids.  Passing the
        chain of an artefact and its ancestors (see ``Artefact.ancestor_ids``)
        lets a grant on an original uploaded artefact cover restricted files in
        artefacts derived from it.

        Admins implicitly bypass all restriction types.
        """
        if not restrictions:
            return True
        if self.is_admin:
            return True
        global_bypass_types = {rb.restriction_type for rb in self.restriction_bypasses}
        missing = [r for r in restrictions if r.restriction_type not in global_bypass_types]
        if not missing:
            return True
        if artefact_id is None:
            return False
        allowed_ids = {artefact_id} if isinstance(artefact_id, int) else set(artefact_id)
        # Filter the user's per-artefact grants in memory.  The
        # ``artefact_bypasses`` relationship is lazy-loaded once and cached on
        # the instance, so repeated calls in a loop (e.g. when rendering a tree
        # of derived artefacts) do not issue a query per artefact.
        specific_types = {
            ab.restriction_type
            for ab in self.artefact_bypasses
            if ab.artefact_id in allowed_ids
        }
        return all(r.restriction_type in specific_types for r in missing)

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
        order = [UserPermission.READ_ONLY, UserPermission.READ_WRITE, UserPermission.STAFF]
        return order.index(self.permission) >= order.index(required)

    def can_raise_analysis_priority(self) -> bool:
        """Whether this user may raise a re-analysis above the web-UI default.

        Admins and staff always may; other users need the explicit
        can_prioritise_analyses grant (settable in the admin UI or via the
        OIDC_ROLE_PRIORITISE SSO role).
        """
        return (
            self.is_admin
            or self.has_permission(UserPermission.STAFF)
            or self.can_prioritise_analyses
        )

    def setPassword(self, password):
        self.password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    def checkPassword(self, password):
        try:
            return bcrypt.checkpw(password.encode('utf-8'), self.password_hash.encode('utf-8'))
        except (ValueError, TypeError):
            return False

    def get_preference(self, key, default=None):
        """Return a single preference value, or *default* if not set."""
        if self.preferences is None:
            return default
        return self.preferences.get(key, default)

    def set_preference(self, key, value):
        """Set a single preference value and mark the column as modified.

        Reassigns the entire dict so SQLAlchemy detects the change
        (JSON columns do not track in-place mutations).
        """
        if self.preferences is None:
            self.preferences = {}
        updated = dict(self.preferences)
        updated[key] = value
        self.preferences = updated


class ApiKey(db.Model):
    """An application key granting programmatic access to the REST API."""
    __tablename__ = 'api_keys'
    __table_args__ = (
        db.UniqueConstraint('key_hash'),
        # Hot path: API key authentication looks up active keys by prefix.
        Index('ix_api_keys_prefix_active', 'key_prefix', 'is_active'),
    )

    id:           Mapped[int]                  = mapped_column(primary_key=True)
    user_id:      Mapped[int]                  = mapped_column(ForeignKey("user.id"), index=True)
    name:         Mapped[str]                  = mapped_column(String(100))
    key_prefix:   Mapped[str]                  = mapped_column(String(8))   # First 8 hex chars; display only
    key_hash:     Mapped[str]                  = mapped_column(String(72), unique=True, index=True)
    permission:   Mapped[ApiKeyPermission]     = mapped_column(SQLEnum(ApiKeyPermission))
    is_active:    Mapped[bool]                 = mapped_column(Boolean, default=True)
    created_at:   Mapped[datetime]             = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at: Mapped[datetime | None]   = mapped_column(DateTime, nullable=True)

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
        Returns the ApiKey, or None if missing/invalid/inactive/revoked.
        """
        if not raw_key or not raw_key.startswith('arc_'):
            return None
        prefix = raw_key[4:12]
        candidates = cls.query.filter_by(key_prefix=prefix, is_active=True).all()
        for key in candidates:
            try:
                if bcrypt.checkpw(raw_key.encode(), key.key_hash.encode()):
                    if not key.user.can_use_api:
                        return None
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
    system_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    url_template: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    external_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    item: Mapped["Item"] = relationship(back_populates="external_references")
    system: Mapped["ExternalSystem"] = relationship(back_populates="references")

    __table_args__ = (
        Index("ix_external_references_system_external", "system_id", "external_id"),
    )

    @property
    def url(self) -> str | None:
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
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("platforms.id"), nullable=True)

    parent: Mapped[Optional["Platform"]] = relationship(back_populates="children", remote_side=[id])
    children: Mapped[list["Platform"]] = relationship(back_populates="parent")
    items: Mapped[list["Item"]] = relationship(back_populates="platform")


class Category(db.Model):
    """Software category."""
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)

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

    @classmethod
    def all_for_picker(cls):
        """All tags, ordered by name, for the tag-picker autocomplete UI."""
        return cls.query.order_by(cls.name).all()


# =============================================================================
# Core Models
# =============================================================================

class Item(db.Model):
    """A logical item in the collection."""
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(32), unique=True, index=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(255), index=True)
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)  # URL-safe slug (immutable once set)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform_id: Mapped[int | None] = mapped_column(ForeignKey("platforms.id"), index=True, nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), index=True, nullable=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), index=True, nullable=True)
    # Privacy / ownership.  owner_id is the user who created the item (web user
    # or the user owning the API key used for upload).  is_private is the
    # explicit flag set by a user; private_effective is the denormalised result
    # of "own flag OR any ancestor private" and is what queries filter on.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), index=True, nullable=True
    )
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    private_effective: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false(), index=True
    )
    # Set when a delete has been requested: the whole item subtree is flagged here
    # in the web request and hidden from every visibility surface, then the task
    # runner's ITEM_DELETE job batch-deletes the rows.  Indexed because the
    # visibility clauses filter on it on every list query.
    pending_deletion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    platform: Mapped[Optional["Platform"]] = relationship(back_populates="items")
    category: Mapped[Optional["Category"]] = relationship(back_populates="items")
    owner: Mapped[Optional["User"]] = relationship("User", foreign_keys=[owner_id])
    parent: Mapped[Optional["Item"]] = relationship(back_populates="children", remote_side=[id])
    children: Mapped[list["Item"]] = relationship(back_populates="parent", cascade="all, delete-orphan")
    artefacts: Mapped[list["Artefact"]] = relationship(back_populates="item", cascade="all, delete-orphan")
    tags: Mapped[list["Tag"]] = relationship(secondary=item_tags, back_populates="items")
    external_references: Mapped[list["ExternalReference"]] = relationship(back_populates="item", cascade="all, delete-orphan")
    shares: Mapped[list["ItemShare"]] = relationship(back_populates="item", cascade="all, delete-orphan")

    @property
    def url_id(self) -> str:
        """Short URL identifier: 8-char UUID prefix, plus slug if available."""
        prefix = self.uuid[:8]
        if self.slug:
            return f"{prefix}-{self.slug}"
        return prefix

    @property
    def ancestors(self) -> list["Item"]:
        """Walk up the parent chain; returns [root, ..., grandparent, parent]."""
        chain = []
        current = self.parent
        while current is not None:
            chain.append(current)
            current = current.parent
        chain.reverse()
        return chain

    @property
    def breadcrumb_path(self) -> list["Item"]:
        """Full path including self: [root, ..., parent, self]."""
        return self.ancestors + [self]

    @property
    def effective_platform(self):
        """Own platform, or the nearest ancestor's platform."""
        if self.platform:
            return self.platform
        for ancestor in reversed(self.ancestors):
            if ancestor.platform:
                return ancestor.platform
        return None

    @property
    def effective_category(self):
        """Own category, or the nearest ancestor's category."""
        if self.category:
            return self.category
        for ancestor in reversed(self.ancestors):
            if ancestor.category:
                return ancestor.category
        return None

    def is_ancestor_of(self, other: "Item") -> bool:
        """Return True if self is an ancestor of other (used for cycle prevention)."""
        current = other.parent
        while current is not None:
            if current.id == self.id:
                return True
            current = current.parent
        return False

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
        db.CheckConstraint(
            "NOT (upload_blob_id IS NOT NULL AND output_blob_id IS NOT NULL)",
            name="ck_artefact_at_most_one_blob",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(32), unique=True, index=True, default=generate_uuid)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    # Privacy / ownership.  owner_id is the user who uploaded the artefact (web
    # user or API-key user); derived artefacts inherit the parent's owner.
    # is_private marks an individual artefact private even inside a public item;
    # an artefact is also effectively private when its item is private (strict
    # descend, via Item.private_effective).
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), index=True, nullable=True
    )
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    # Set when a delete has been requested: the artefact (and its derived subtree)
    # are flagged here in the web request and hidden from every visibility surface,
    # then the task runner's ARTEFACT_DELETE job batch-deletes the rows.  Indexed
    # because the visibility clauses filter on it on every list query.
    pending_deletion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false(), index=True
    )
    label: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)  # URL-safe slug (immutable once set)
    artefact_type: Mapped[ArtefactType] = mapped_column(_TolerantEnum(ArtefactType))
    type_overridden: Mapped[bool] = mapped_column(Boolean, default=False)  # Was type manually set?
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # File storage
    original_filename: Mapped[str] = mapped_column(String(255))  # User's original filename
    # Upload artefacts: physical file path (equals upload_blob.storage_path).
    # Derived artefacts: logical lineage key (derived/{analysis_id}/{hash});
    # the physical path is output_blob.storage_path.
    storage_path: Mapped[str] = mapped_column(String(1000))
    storage_directory: Mapped[StorageDirectory] = mapped_column(
        SQLEnum(StorageDirectory), default=StorageDirectory.UPLOADS
    )  # Which folder: uploads (original) or outputs (derived)
    upload_blob_id: Mapped[int | None] = mapped_column(
        ForeignKey("upload_blobs.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    output_blob_id: Mapped[int | None] = mapped_column(
        ForeignKey("output_blobs.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    
    # Hashes (computed after upload)
    md5: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Fuzzy hash (TLSH) for byte-level similarity.  Skipped for flux artefact
    # types (SCP/DFI/A2R) where raw bytes carry timing noise.  NULL when not yet
    # computed, the file is too small, or py-tlsh is unavailable.
    tlsh: Mapped[str | None] = mapped_column(String(72), nullable=True)
    # Set True when this artefact's extracted-file set changes (e.g. after an
    # extraction completes), marking its content-set similarity cache stale.
    # Cleared by recompute_for_artefact / a full rebuild.  Drained incrementally
    # by `flask refresh-similarity` and the task runner's similarity-delta sweep,
    # so the cache stays fresh without a full O(n^2) rebuild.  Indexed so the
    # "what's stale?" scan is cheap when few rows are dirty.
    similarity_dirty: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false(), index=True
    )

    # Format-specific metadata (JSON)
    media_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # Derivation chain - if this artefact was produced by analysing another
    parent_artefact_id: Mapped[int | None] = mapped_column(
        ForeignKey("artefacts.id"), index=True, nullable=True
    )
    derived_from_analysis_id: Mapped[int | None] = mapped_column(
        ForeignKey("analyses.id", ondelete="SET NULL"), index=True, nullable=True
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
    riscos_modules: Mapped[list["RiscosModule"]] = relationship(
        back_populates="artefact", cascade="all, delete-orphan"
    )
    replay_movies: Mapped[list["ReplayMovie"]] = relationship(
        back_populates="artefact", cascade="all, delete-orphan"
    )
    media_files: Mapped[list["MediaFile"]] = relationship(
        back_populates="artefact", cascade="all, delete-orphan"
    )
    user_bypasses: Mapped[list["UserArtefactBypass"]] = relationship(
        back_populates="artefact", cascade="all, delete-orphan",
        foreign_keys="UserArtefactBypass.artefact_id",
    )

    # Derived artefacts (e.g., sector image from flux decode)
    parent_artefact: Mapped[Optional["Artefact"]] = relationship(
        back_populates="derived_artefacts", remote_side=[id],
        foreign_keys=[parent_artefact_id]
    )
    derived_artefacts: Mapped[list["Artefact"]] = relationship(
        back_populates="parent_artefact", foreign_keys=[parent_artefact_id],
        cascade="all, delete-orphan", order_by="Artefact.label"
    )
    derived_from_analysis: Mapped[Optional["Analysis"]] = relationship(
        foreign_keys=[derived_from_analysis_id],
        passive_deletes=True,
    )
    tags: Mapped[list["Tag"]] = relationship(secondary=artefact_tags, back_populates="artefacts")
    restrictions: Mapped[list["ArtefactRestriction"]] = relationship(
        back_populates="artefact", cascade="all, delete-orphan"
    )
    owner: Mapped[Optional["User"]] = relationship("User", foreign_keys=[owner_id])
    upload_blob: Mapped[Optional["UploadBlob"]] = relationship(back_populates="artefacts")
    output_blob: Mapped[Optional["OutputBlob"]] = relationship(back_populates="artefacts")

    @property
    def effective_private(self) -> bool:
        """True if this artefact is private itself or via its (private) item."""
        return bool(self.is_private or (self.item is not None and self.item.private_effective))

    @property
    def is_restricted(self) -> bool:
        """True if this artefact has any active download restrictions."""
        return len(self.restrictions) > 0

    @property
    def root_artefact(self) -> "Artefact":
        """Walk up the parent chain to the original uploaded artefact (no parent)."""
        a = self
        while a.parent_artefact_id is not None:
            a = a.parent_artefact
        return a

    @property
    def ancestor_ids(self) -> set[int]:
        """IDs of this artefact and all its ancestors up the derivation chain.

        A per-artefact download bypass granted on any ancestor (e.g. the
        original uploaded artefact) therefore cascades to cover restrictions on
        artefacts derived from it.  Pass this to
        ``User.can_bypass_all_restrictions(..., artefact_id=...)``.
        """
        ids = set()
        a = self
        while a is not None:
            ids.add(a.id)
            a = a.parent_artefact
        return ids

    @property
    def effective_restrictions(self) -> list["ArtefactRestriction"]:
        """This artefact's own download restrictions plus those inherited from
        any ancestor up the derivation chain.

        A restriction on a container artefact (e.g. a ZIP) covers everything
        derived from it — the bytes and analysis outputs of artefacts extracted
        and promoted out of it.  This mirrors the bypass side: grants cascade
        down ``ancestor_ids``, so collecting restrictions up the same chain
        keeps the two symmetric.  Walks ``parent_artefact``.
        """
        result = list(self.restrictions)
        a = self.parent_artefact
        while a is not None:
            result.extend(a.restrictions)
            a = a.parent_artefact
        return result

    @property
    def url_slug(self) -> str:
        """Slug-based URL segment for use within an item URL."""
        return self.slug if self.slug else self.uuid[:8]


class Analysis(db.Model):
    """Results from analysing an artefact - auto-triggered based on artefact type."""
    __tablename__ = "analyses"
    __table_args__ = (
        # Hot paths: queue listing (status + created_at) and worker job claim
        # (status + priority + created_at).
        Index('ix_analyses_status_created', 'status', 'created_at'),
        Index('ix_analyses_status_priority_created', 'status', 'priority', 'created_at'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(32), unique=True, index=True, default=generate_uuid)
    # Nullable: CLEANUP jobs queued by bulk item deletion outlive their
    # artefacts and carry storage keys in hints instead.
    artefact_id: Mapped[int | None] = mapped_column(ForeignKey("artefacts.id"), index=True, nullable=True)
    analysis_type: Mapped[AnalysisType] = mapped_column(_TolerantEnum(AnalysisType))
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)  # URL-safe slug (immutable once set)
    status: Mapped[AnalysisStatus] = mapped_column(SQLEnum(AnalysisStatus), default=AnalysisStatus.PENDING, index=True)
    
    # Tool info (filled by worker)
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tool_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    
    # Hints to help analysis (JSON) - e.g., {"platform": "bbc_micro", "filesystem": "adfs"}
    hints: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # Results
    output_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    output_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON for structured results
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Live progress for long-running jobs (set by the worker while RUNNING,
    # cleared on completion).  Kept separate from `summary` (the final result)
    # so the UI can show a real progress bar, and so progress_updated_at can
    # drive heartbeat-based stale-job detection: a job still reporting progress
    # (or heartbeating) is not stuck, even past STALE_JOB_TIMEOUT_SECONDS.
    progress_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_current: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Queue priority: higher value = picked up sooner (see ANALYSIS_PRIORITY_* constants).
    # No single-column index: ix_analyses_status_priority_created covers queue scans.
    priority: Mapped[int] = mapped_column(Integer, default=ANALYSIS_PRIORITY_NORMAL, server_default=sa_text('0'))

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    artefact: Mapped["Artefact"] = relationship(
        back_populates="analyses", foreign_keys=[artefact_id]
    )
    
    # Artefacts produced by this analysis (e.g., decoded sector image from flux)
    produced_artefacts: Mapped[list["Artefact"]] = relationship(
        foreign_keys="Artefact.derived_from_analysis_id",
        viewonly=True
    )

    @property
    def last_activity_at(self):
        """Most recent sign of life: last progress/heartbeat, else start time.

        Templates use this for the "stale" indicator; SQL paths use the
        equivalent ``func.coalesce(progress_updated_at, started_at)``.
        """
        return self.progress_updated_at or self.started_at


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
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)  # URL-safe slug (immutable once set)
    filesystem: Mapped[FilesystemType] = mapped_column(SQLEnum(FilesystemType))
    container_format: Mapped[str | None] = mapped_column(Text, nullable=True)  # Detailed format from disc image tools (e.g., "Acorn ADFS E")
    archive_comment: Mapped[str | None] = mapped_column(Text, nullable=True)  # ZIP-style archive-wide comment, decoded as text
    start_sector: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sector_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    block_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_files: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_directories: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    unique_files: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detection_details: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON from partition detection (sfdisk, etc.)
    gnu_file_type: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)  # Output of file(1) on the image
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
    extension: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    modified_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accessed_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attributes: Mapped[str | None] = mapped_column(String(50), nullable=True)
    md5: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    sha1: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tlsh: Mapped[str | None] = mapped_column(String(72), nullable=True)  # fuzzy hash (TLSH) for near-duplicate files
    known_file_id: Mapped[int | None] = mapped_column(ForeignKey("known_files.id", ondelete="SET NULL"), index=True, nullable=True)

    # Archive/nested file support
    parent_file_id: Mapped[int | None] = mapped_column(ForeignKey("extracted_files.id"), nullable=True, index=True)
    is_archive: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_directory: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # True if this is a directory entry
    archive_format: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g., 'ArcFS', 'ZIP', 'CFS'
    archive_comment: Mapped[str | None] = mapped_column(Text, nullable=True)  # ZIP-style archive-wide comment, decoded as text
    risc_os_filetype: Mapped[str | None] = mapped_column(String(3), nullable=True, index=True)  # Hex filetype (e.g., '3fb')
    load_address: Mapped[str | None] = mapped_column(String(8), nullable=True)  # RISC OS load address (8-char hex, e.g., 'fffff300')
    exec_address: Mapped[str | None] = mapped_column(String(8), nullable=True)  # RISC OS exec address (8-char hex)
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

    restrictions: Mapped[list["ExtractedFileRestriction"]] = relationship(
        back_populates="extracted_file", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_extracted_files_partition_known_file", "partition_id", "known_file_id"),
        Index("ix_extracted_files_archive", "is_archive", "risc_os_filetype"),
        Index("ix_extracted_files_parent", "parent_file_id", "extraction_depth"),
        Index("ix_extracted_files_partition_path", "partition_id", "path"),
        Index("ix_extracted_files_sha256_size", "sha256", "file_size"),
        # Supports the task runner's keyset-batched deletion
        # (WHERE partition_id IN (...) AND id > cursor ORDER BY id): each batch
        # is a cheap index range scan rather than a per-batch sort of the
        # partition's rows.
        Index("ix_extracted_files_partition_id_id", "partition_id", "id"),
    )

    @hybrid_property
    def is_known(self) -> bool:
        """Whether this file matches a KnownFile in a hash database.

        Derived solely from ``known_file_id`` so it can never diverge from the
        link the way the former denormalised ``is_known`` column did (a deleted
        KnownFile nulls ``known_file_id`` via ON DELETE SET NULL, and this
        follows automatically).  Read-only: to mark a file (un)known, set
        ``known_file_id``.
        """
        return self.known_file_id is not None

    @is_known.expression
    def is_known(cls):
        return cls.known_file_id.isnot(None)

    @property
    def browse_path(self) -> str:
        """Path parameter value to navigate to this file in context.

        For archives and directories, returns path + '/' to browse inside.
        For regular files, returns the parent directory path so the file
        is shown in its directory context.
        Root-level files return '' (no path filter, shows all files).
        """
        if self.is_archive or self.is_directory:
            return self.path + '/'
        if '/' in self.path:
            return self.path.rsplit('/', 1)[0] + '/'
        return ''

    @property
    def is_restricted(self) -> bool:
        """True if this file has any active download restrictions."""
        return len(self.restrictions) > 0


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
    track: Mapped[int | None] = mapped_column(nullable=True)
    side: Mapped[int | None] = mapped_column(nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)  # e.g. sector ID string

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
    # Known values: 'traceback', 'formaster', 'unknown_mastering'
    track: Mapped[int | None] = mapped_column(nullable=True)
    decoded: Mapped[str | None] = mapped_column(Text, nullable=True)  # Decoded mastering data string

    artefact: Mapped["Artefact"] = relationship(back_populates="mastering_indicators")


class RiscosModule(db.Model):
    """RISC OS relocatable module metadata extracted from a disc or archive.

    Populated server-side when a RISCOS_MODULE_PARSE analysis completes.
    One row per module file found (a disc image may contain many).
    """
    __tablename__ = 'riscos_modules'

    id: Mapped[int] = mapped_column(primary_key=True)
    artefact_id: Mapped[int] = mapped_column(ForeignKey('artefacts.id'), index=True)
    title_string: Mapped[str] = mapped_column(String(255), index=True)  # Internal module name (e.g., "WindowManager")
    help_title: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Display name from help string (e.g., "Window Manager")
    version: Mapped[str | None] = mapped_column(String(20), nullable=True)  # e.g., "2.05"
    date: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ISO date (e.g., "1990-01-31")
    swi_chunk: Mapped[int | None] = mapped_column(Integer, nullable=True)  # SWI base number
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)  # Path within extraction
    module_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # SHA-256
    commands: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of command names
    swi_names: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of full SWI names (e.g. ADFS_DiscOp)

    artefact: Mapped["Artefact"] = relationship(back_populates="riscos_modules")


class ReplayMovie(db.Model):
    """Acorn Replay / ARMovie (RISC OS filetype &AE7) metadata.

    Populated server-side when a REPLAY_PROCESS analysis completes (which both
    parses the header and transcodes the video).  One row per ARMovie file found
    in an extraction (a disc image may contain several).
    Like RiscosModule, ARMovie files are only ever encountered as extracted
    files (never standalone artefacts), so ``file_path`` is always set.
    """
    __tablename__ = 'replay_movies'

    id: Mapped[int] = mapped_column(primary_key=True)
    artefact_id: Mapped[int] = mapped_column(ForeignKey('artefacts.id'), index=True)
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True, index=True)  # Path within extraction
    title: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    copyright: Mapped[str | None] = mapped_column(String(255), nullable=True)
    video_format: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # compression/codec id (0 = sound-only)
    video_label: Mapped[str | None] = mapped_column(String(64), nullable=True)  # codec name/label as written in the header (e.g. "1K", "Moving Lines")
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pixel_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frame_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    sound_format: Mapped[int | None] = mapped_column(Integer, nullable=True)  # sound codec id (0 = silent)
    sound_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sound_channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sound_precision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frames_per_chunk: Mapped[float | None] = mapped_column(Float, nullable=True)
    number_of_chunks: Mapped[int | None] = mapped_column(Integer, nullable=True)  # entry count (highest index + 1)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Transcoded outputs, populated by the REPLAY_PROCESS analysis (scotch + ffmpeg).
    # Relative paths under the 'outputs' storage directory, served via get_output_file.
    mp4_output_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    poster_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Content-addressed dedup anchors: when the transcoded MP4/poster is stored as
    # a shared, refcounted OutputBlob (keyed on the source file's hash), these FKs
    # reference it so the GC only deletes the bytes once nothing references them.
    # Null for legacy rows whose outputs predate content-addressed transcoding.
    mp4_output_blob_id: Mapped[int | None] = mapped_column(
        ForeignKey('output_blobs.id', ondelete='SET NULL'), nullable=True, index=True)
    poster_blob_id: Mapped[int | None] = mapped_column(
        ForeignKey('output_blobs.id', ondelete='SET NULL'), nullable=True, index=True)

    artefact: Mapped["Artefact"] = relationship(back_populates="replay_movies")


class MediaFile(db.Model):
    """Generic time-based media (audio/video) found in an extraction.

    Populated server-side when a MEDIA_TRANSCODE analysis completes.  One row
    per **non-native** media container (AVI/QuickTime/MPEG/...) that was
    transcoded to a browser-playable MP4/M4A.  Browser-native media (MP4/WebM/
    MP3/...) is NOT recorded here — it has no analysis and is discovered live
    from ``ExtractedFile`` and streamed directly.

    Mirrors :class:`ReplayMovie` (Replay stays on its own dedicated pipeline);
    this is the equivalent for arbitrary ffmpeg-handled media.  Codec / track
    metadata is captured from ffprobe so the viewer can show the same kind of
    technical detail it shows for Replay movies.
    """
    __tablename__ = 'media_files'

    id: Mapped[int] = mapped_column(primary_key=True)
    artefact_id: Mapped[int] = mapped_column(ForeignKey('artefacts.id'), index=True)
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True, index=True)  # source container path within extraction
    media_kind: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)  # 'video' | 'audio'
    container_format: Mapped[str | None] = mapped_column(String(64), nullable=True)  # ffprobe format_name (e.g. "avi", "mov,mp4,...")
    # Video track (null for audio-only)
    video_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. "mpeg2video", "h264"
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frame_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Audio track (null for silent video)
    audio_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. "mp3", "pcm_s16le"
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Hz
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_audio: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Transcoded outputs (relative paths under the 'outputs' storage dir, served
    # via get_output_file).  mp4_output_path holds an MP4 for video or an M4A
    # for audio-only sources.
    mp4_output_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    poster_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Content-addressed dedup anchors (see ReplayMovie): reference the shared,
    # refcounted OutputBlob holding the transcoded MP4/poster bytes.  Null for
    # legacy rows whose outputs predate content-addressed transcoding.
    mp4_output_blob_id: Mapped[int | None] = mapped_column(
        ForeignKey('output_blobs.id', ondelete='SET NULL'), nullable=True, index=True)
    poster_blob_id: Mapped[int | None] = mapped_column(
        ForeignKey('output_blobs.id', ondelete='SET NULL'), nullable=True, index=True)

    artefact: Mapped["Artefact"] = relationship(back_populates="media_files")


# =============================================================================
# Download Restrictions
# =============================================================================

class ArtefactRestriction(db.Model):
    """A download restriction applied to an artefact.

    Each artefact can have multiple restrictions of different types.
    A unique constraint prevents duplicate restriction types on the same artefact.
    """
    __tablename__ = "artefact_restrictions"
    __table_args__ = (
        db.UniqueConstraint('artefact_id', 'restriction_type', name='uq_artefact_restriction_type'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    artefact_id: Mapped[int] = mapped_column(ForeignKey("artefacts.id"), index=True)
    restriction_type: Mapped[RestrictionType] = mapped_column(SQLEnum(RestrictionType))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), server_default=sa_text('CURRENT_TIMESTAMP'))

    artefact: Mapped["Artefact"] = relationship(back_populates="restrictions")
    added_by: Mapped[Optional["User"]] = relationship()


class ExtractedFileRestriction(db.Model):
    """A restriction on an individual extracted file.

    Blocks only this file's download (and any ancestor archive/directory in the
    same partition tree); sibling files and the parent artefact are unaffected
    unless they also have restrictions.
    """
    __tablename__ = "extracted_file_restrictions"
    __table_args__ = (
        db.UniqueConstraint('extracted_file_id', 'restriction_type',
                            name='uq_extracted_file_restriction_type'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    extracted_file_id: Mapped[int] = mapped_column(ForeignKey("extracted_files.id"), index=True)
    restriction_type: Mapped[RestrictionType] = mapped_column(SQLEnum(RestrictionType))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    extracted_file: Mapped["ExtractedFile"] = relationship(back_populates="restrictions")
    added_by: Mapped[Optional["User"]] = relationship()


class UserRestrictionBypass(db.Model):
    """Per-restriction-type bypass permission for a user.

    Users with a bypass entry for a given restriction type can still
    download artefacts restricted with that type (after confirmation).
    """
    __tablename__ = "user_restriction_bypasses"
    __table_args__ = (
        db.UniqueConstraint('user_id', 'restriction_type', name='uq_user_restriction_bypass'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    restriction_type: Mapped[RestrictionType] = mapped_column(SQLEnum(RestrictionType))

    user: Mapped["User"] = relationship(back_populates="restriction_bypasses")


class UserArtefactBypass(db.Model):
    """Per-artefact, per-restriction-type download bypass granted to a specific user.

    More granular than UserRestrictionBypass: grants access to one artefact
    only, rather than all artefacts of a given restriction type.  Intended
    for cases where a curator needs to share one restricted artefact with a
    researcher without granting a collection-wide bypass.

    Checked after global UserRestrictionBypass in User.can_bypass_all_restrictions().
    """
    __tablename__ = "user_artefact_bypasses"
    __table_args__ = (
        db.UniqueConstraint('user_id', 'artefact_id', 'restriction_type',
                            name='uq_user_artefact_bypass'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    artefact_id: Mapped[int] = mapped_column(ForeignKey("artefacts.id", ondelete="CASCADE"), index=True)
    restriction_type: Mapped[RestrictionType] = mapped_column(SQLEnum(RestrictionType))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    granted_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="artefact_bypasses",
                                        foreign_keys=[user_id])
    artefact: Mapped["Artefact"] = relationship(back_populates="user_bypasses",
                                                foreign_keys=[artefact_id])
    granted_by: Mapped["User | None"] = relationship(foreign_keys=[granted_by_id])


# =============================================================================
# Known File Database
# =============================================================================

class HashDatabase(db.Model):
    """A source of known file hashes for elimination."""
    __tablename__ = "hash_databases"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    platform_id: Mapped[int | None] = mapped_column(ForeignKey("platforms.id", ondelete="SET NULL"), nullable=True)
    # file_count is a derived column_property (defined after KnownFile, below),
    # not a stored column — see the comment there.
    enable_product_recognition: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=sa_false())
    product_recognition_status: Mapped[ProductRecognitionStatus | None] = mapped_column(
        SQLEnum(ProductRecognitionStatus, name="productrecognitionstatus"),
        nullable=True,
    )
    product_recognition_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    product_recognition_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=sa_true())
    # Soft-delete marker: set together with is_active=False when a (potentially
    # huge) database is scheduled for background reaping by a HASHDB_DELETE
    # worker job.  is_active=False removes it from all matching/restriction
    # queries for free; is_deleting additionally hides it from listings and
    # blocks management routes while the worker drains its rows.
    is_deleting: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=sa_false())
    # When set, files linked to this database are dropped from content-set
    # similarity (e.g. a base-OS hashdb, so a stock RISC OS install does not make
    # every system disc match every other).  Reserve for OS/runtime boilerplate,
    # not application software.
    exclude_from_similarity: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=sa_false())
    restriction_type: Mapped[RestrictionType | None] = mapped_column(
        SQLEnum(RestrictionType), nullable=True
    )  # If set, artefacts matching this DB's files are automatically restricted
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    platform: Mapped[Optional["Platform"]] = relationship()
    known_files: Mapped[list["KnownFile"]] = relationship(back_populates="database", cascade="all, delete-orphan")
    known_products: Mapped[list["KnownProduct"]] = relationship(back_populates="database", cascade="all, delete-orphan")


class KnownProduct(db.Model):
    """A named product/application/group within a hash database."""
    __tablename__ = "known_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    database_id: Mapped[int] = mapped_column(ForeignKey("hash_databases.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    path_match_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=sa_false())
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
    product_id: Mapped[int | None] = mapped_column(ForeignKey("known_products.id", ondelete="SET NULL"), index=True, nullable=True)
    filename: Mapped[str] = mapped_column(String(255), index=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    md5: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    sha1: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    crc32: Mapped[str | None] = mapped_column(String(8), nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=sa_true())
    relative_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    product_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    product_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    database: Mapped["HashDatabase"] = relationship(back_populates="known_files")
    product: Mapped[Optional["KnownProduct"]] = relationship(back_populates="known_files")

    __table_args__ = (
        Index("ix_known_files_md5_size", "md5", "file_size"),
        Index("ix_known_files_sha1_size", "sha1", "file_size"),
        # Composite indexes for the per-(database, product) duplicate check in
        # the hashdb import path (_existing_known_file).
        Index("ix_known_files_db_product_md5", "database_id", "product_id", "md5"),
        Index("ix_known_files_db_product_sha1", "database_id", "product_id", "sha1"),
        Index("ix_known_files_db_product_sha256", "database_id", "product_id", "sha256"),
    )


# file_count is derived from the actual known_files rows rather than stored, so
# it can never drift from them the way the old denormalised counter did (it was
# incremented on import but only decremented on single-file deletes — a product
# delete left it overcounting; see issue #637).  A correlated scalar subquery is
# emitted per loaded HashDatabase row.  hash_databases is a small table and is
# only loaded as full entities on admin/display paths (the index, the detail
# view, the REST serializer) — never in the per-file matching hot path, which
# queries KnownFile directly — so this is cheap and avoids the N+1 a
# per-instance COUNT would cause on the database listing.
HashDatabase.file_count = column_property(
    sa_select(sa_func.count(KnownFile.id))
    .where(KnownFile.database_id == HashDatabase.id)
    .correlate_except(KnownFile)
    .scalar_subquery(),
    deferred=False,
)


class HashRescanJob(db.Model):
    """Tracks a background hash-rescan operation triggered from the UI.

    One row per rescan run.  The status column is written by the background
    thread and read by any gunicorn worker that renders the hashdb pages —
    all coordination goes through the database so the status is consistent
    across all worker processes.
    """
    __tablename__ = "hash_rescan_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Which database triggered the rescan (NULL = triggered from the index page).
    database_id: Mapped[int | None] = mapped_column(
        ForeignKey("hash_databases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[HashRescanStatus] = mapped_column(
        SQLEnum(HashRescanStatus, name="hashrescanstatus"), nullable=False
    )
    files_updated: Mapped[int | None] = mapped_column(Integer, nullable=True)
    files_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queued_analyses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    database: Mapped[Optional["HashDatabase"]] = relationship()


class RecognisedProduct(db.Model):
    """Result of a PRODUCT_RECOGNITION analysis: a folder matched a KnownProduct."""
    __tablename__ = "recognised_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    partition_id: Mapped[int] = mapped_column(ForeignKey("partitions.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("known_products.id", ondelete="CASCADE"), index=True)
    folder_path: Mapped[str] = mapped_column(String(1000))
    required_matched: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text('0'))
    required_total: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text('0'))
    optional_matched: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text('0'))
    optional_total: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text('0'))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    partition: Mapped["Partition"] = relationship(back_populates="recognised_products")
    product: Mapped["KnownProduct"] = relationship(back_populates="recognised_in")

    __table_args__ = (
        Index("ix_recognised_products_partition_product", "partition_id", "product_id"),
        Index(
            "uq_recognised_products_partition_product_folder",
            "partition_id",
            "product_id",
            "folder_path",
            unique=True,
        ),
    )


# =============================================================================
# Groups and Sharing
# =============================================================================

class Group(db.Model):
    """A named group of users, used for sharing private items."""
    __tablename__ = "groups"
    __table_args__ = (
        db.UniqueConstraint('name', name='uq_groups_name'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'local' = manually managed; 'oidc' = synced from identity-provider group claim
    source: Mapped[str] = mapped_column(String(20), nullable=False, default='local', server_default='local')
    oidc_claim_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), server_default=sa_text('CURRENT_TIMESTAMP'))

    members: Mapped[list["User"]] = relationship(secondary=group_memberships, back_populates="groups")


class ItemShare(db.Model):
    """An explicit share grant giving a user or group access to a private item."""
    __tablename__ = "item_shares"
    __table_args__ = (
        db.UniqueConstraint('item_id', 'user_id', name='uq_item_share_user'),
        db.UniqueConstraint('item_id', 'group_id', name='uq_item_share_group'),
        db.CheckConstraint(
            "(user_id IS NOT NULL AND group_id IS NULL) OR (user_id IS NULL AND group_id IS NOT NULL)",
            name='ck_item_shares_exactly_one_principal',
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), nullable=True, index=True)
    permission: Mapped[str] = mapped_column(String(20), nullable=False, default='viewer', server_default='viewer')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), server_default=sa_text('CURRENT_TIMESTAMP'))

    item: Mapped["Item"] = relationship(back_populates="shares")
    user: Mapped[Optional["User"]] = relationship("User", foreign_keys=[user_id])
    group: Mapped[Optional["Group"]] = relationship("Group")


# =============================================================================
# Similarity / fuzzy matching
# =============================================================================
# Cached content-set similarity between artefacts (and between directory-subtree
# "components").  Two artefacts are similar when they contain substantially the
# same files, compared by content hash rather than container bytes -- so
# differing compression (Spark vs ZIP) or flux timing noise does not affect the
# result.  These tables are populated by `flask rebuild-similarity`
# (myapp/services/similarity.py).  Pairs are stored canonically with
# a_id < b_id so each appears once.

class ArtefactSimilarity(db.Model):
    """Cached size-weighted Jaccard similarity between two artefacts."""
    __tablename__ = "artefact_similarity"
    __table_args__ = (
        db.UniqueConstraint("artefact_a_id", "artefact_b_id", name="uq_artefact_similarity_pair"),
        Index("ix_artefact_similarity_a", "artefact_a_id", "score"),
        Index("ix_artefact_similarity_b", "artefact_b_id", "score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    artefact_a_id: Mapped[int] = mapped_column(ForeignKey("artefacts.id", ondelete="CASCADE"), index=True)
    artefact_b_id: Mapped[int] = mapped_column(ForeignKey("artefacts.id", ondelete="CASCADE"), index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)  # size-weighted Jaccard, 0..1
    shared_files: Mapped[int] = mapped_column(Integer, nullable=False)
    union_files: Mapped[int] = mapped_column(Integer, nullable=False)
    shared_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    union_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    artefact_a: Mapped["Artefact"] = relationship(foreign_keys=[artefact_a_id])
    artefact_b: Mapped["Artefact"] = relationship(foreign_keys=[artefact_b_id])


class ArtefactComponent(db.Model):
    """A directory-subtree 'component' of an artefact (e.g. a RISC OS !App).

    Captures the content set of every file beneath a root directory so the same
    application can be matched across different discs regardless of the
    surrounding disc content.  Reconciled in place on each refresh (keyed by
    ``(partition_id, root_path)``) so the row id stays stable, keeping the
    cross-artefact ``ComponentSimilarity`` foreign keys valid under concurrency.
    """
    __tablename__ = "artefact_components"
    __table_args__ = (
        Index("ix_artefact_components_artefact", "artefact_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(32), unique=True, index=True, default=generate_uuid)
    artefact_id: Mapped[int] = mapped_column(ForeignKey("artefacts.id", ondelete="CASCADE"), index=True)
    partition_id: Mapped[int] = mapped_column(ForeignKey("partitions.id", ondelete="CASCADE"), index=True)
    root_path: Mapped[str] = mapped_column(String(1000))  # subtree root within the partition
    name: Mapped[str] = mapped_column(String(255))        # leaf directory name (display)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    artefact: Mapped["Artefact"] = relationship()
    partition: Mapped["Partition"] = relationship()


class ComponentSimilarity(db.Model):
    """Cached size-weighted Jaccard similarity between two components."""
    __tablename__ = "component_similarity"
    __table_args__ = (
        db.UniqueConstraint("component_a_id", "component_b_id", name="uq_component_similarity_pair"),
        Index("ix_component_similarity_a", "component_a_id", "score"),
        Index("ix_component_similarity_b", "component_b_id", "score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    component_a_id: Mapped[int] = mapped_column(ForeignKey("artefact_components.id", ondelete="CASCADE"), index=True)
    component_b_id: Mapped[int] = mapped_column(ForeignKey("artefact_components.id", ondelete="CASCADE"), index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    shared_files: Mapped[int] = mapped_column(Integer, nullable=False)
    union_files: Mapped[int] = mapped_column(Integer, nullable=False)
    shared_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    union_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    component_a: Mapped["ArtefactComponent"] = relationship(foreign_keys=[component_a_id])
    component_b: Mapped["ArtefactComponent"] = relationship(foreign_keys=[component_b_id])


class WorkerHeartbeat(db.Model):
    """Liveness record for an analysis worker, used to size the fairness cap.

    Workers are otherwise anonymous (they share one API key).  Each worker
    process self-generates a random id at startup and stamps it on every poll
    and progress heartbeat (see record_worker_heartbeat); the number of rows
    seen within a freshness window is the live worker count the heavy-job cap
    scales against (myapp/services/analysis_queue.py).

    One row per worker id.  A restarted worker takes a new id, so its old row
    lingers until it ages out of the freshness window; the read-time freshness
    filter ignores it immediately and the taskrunner physically GCs aged rows,
    so the table stays bounded.
    """
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        index=True,
    )
    first_seen: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)


# vim: ts=4 sw=4 et
