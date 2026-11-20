"""Archive adjacency: rules/event interpretation, whole-case handoff and the
cross-reference catalog with its standalone reading-desk analysis."""

from .models import (  # noqa: F401
    ActiveHold,
    BlockReason,
    ConflictError,
    DocQualification,
    Document,
    Event,
    Hold,
    InvalidInterval,
    QualificationProduct,
    RejectError,
    Rule,
    RuleReason,
    UnknownReference,
)
from .rules_service import RuleEventStore  # noqa: F401
from .handoff_service import HandoffRepo  # noqa: F401
from .crossref_models import (  # noqa: F401
    CatalogEntry,
    CatalogSnapshot,
    Citation,
    CrossrefConflict,
    CrossrefReject,
    ResolvedCitation,
    ShelfRedirect,
)
from .crossref_service import CrossrefCatalog  # noqa: F401
from .reading_desk import ReadingDesk, load_snapshot_file  # noqa: F401

__all__ = [
    "RuleEventStore",
    "HandoffRepo",
    "QualificationProduct",
    "DocQualification",
    "RejectError",
    "ConflictError",
    "InvalidInterval",
    "UnknownReference",
    "CrossrefCatalog",
    "ReadingDesk",
    "CatalogEntry",
    "CatalogSnapshot",
    "Citation",
    "ShelfRedirect",
    "ResolvedCitation",
    "CrossrefReject",
    "CrossrefConflict",
    "load_snapshot_file",
]
