"""
Arcology - Web-application enum definitions

All Python enums specific to the web app layer (not shared with the worker
or CLI).  Defined here to keep ``database.py`` free of non-model code, and
so that permission/visibility modules can import enum values without pulling
in the full SQLAlchemy model tree.

``database.py`` imports everything from this module and re-exports it, so
existing ``from .database import UserPermission`` call sites continue to work
unchanged.  New code should import directly from this module.
"""

import enum
from arcology_shared.enums import AnalysisStatus  # noqa: F401  (re-export)


class UserPermission(enum.Enum):
    """Permission level for a web UI user. Controls all actions in both the web UI and the API."""
    READ_ONLY  = "read_only"   # View everything; no modifications
    READ_WRITE = "read_write"  # Full CRUD access
    STAFF      = "staff"       # READ_WRITE + stale-job reset + priority-raising; below admin


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
    ARCHIVE = "archive"
    UNKNOWN = "unknown"
    OTHER = "other"


class StorageDirectory(enum.Enum):
    """Where an artefact file is stored."""
    UPLOADS = "uploads"    # Original user-uploaded files
    OUTPUTS = "outputs"    # Derived/generated files (from analysis)


class RestrictionType(enum.Enum):
    """Restriction categories that can be applied to artefacts to block downloads."""
    MALWARE = "malware"
    PII = "pii"
    COPYRIGHT = "copyright"
    LEGAL_HOLD = "legal_hold"
    EXPLICIT = "explicit"
    CORRUPTED = "corrupted"

    @property
    def label(self):
        """Human-readable display label, handling acronyms correctly."""
        _LABELS = {
            'malware': 'Malware',
            'pii': 'PII',
            'copyright': 'Copyright',
            'legal_hold': 'Legal Hold',
            'explicit': 'Explicit',
            'corrupted': 'Corrupted',
        }
        return _LABELS.get(self.value, self.value.replace('_', ' ').title())


class HashRescanStatus(enum.Enum):
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"


class ProductRecognitionStatus(enum.Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"

# vim: ts=4 sw=4 et
