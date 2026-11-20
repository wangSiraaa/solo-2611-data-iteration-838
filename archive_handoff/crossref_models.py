"""Immutable domain records for the cross-reference ("see also") catalog.

This is a third service, independent of both the rule/event store and the
handoff repository.  It owns revisions attached to the same fictional
archives and cases; the whole-case preservation and handoff behaviour lives
in the other modules and is untouched here.

Vocabulary
----------
* ``CatalogEntry``  -- a stable "see also" item attached to one case (and,
  optionally, to one original archive document).  Each entry owns one
  *active* local shelf number inside its case.
* ``ShelfRedirect`` -- a withdrawn shelf number may record a stepwise
  transfer to another shelf number in the *same* case.  Chains that close a
  loop or stop at a missing shelf are kept, not repaired, and reported as
  problems when a citation tries to follow them.
* ``Citation``      -- one outgoing reference of an entry: a category
  (``direct``/``see_also``/...), a provenance string and a local shelf
  number in a case.  Several citations of different categories may point at
  the same shelf; citations are never deleted, even when their target is
  invalid, so the problematic wording survives for later cleanup.
* ``CatalogSnapshot`` -- an immutable, content-digested edition of the whole
  catalog at one revision.  The reading desk consumes snapshots, never the
  live tables, so later corrections can always restore an old edition.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional

# Resolution outcomes for one citation target.
RESOLUTION_OK = "ok"
RESOLUTION_MISSING = "missing"        # chain ends at a shelf with no holder
RESOLUTION_LOOP = "loop"              # stepwise transfer closes a cycle

# Entry kinds.  The absorbing original document is an ``original`` entry;
# ordinary catalog items are ``normal`` entries.
ENTRY_KIND_NORMAL = "normal"
ENTRY_KIND_ORIGINAL = "original"


def crossref_canon(obj) -> str:
    """Deterministic canonical JSON used for identity replays and digests."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(payload: dict) -> str:
    raw = crossref_canon(payload)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CrossrefReject(Exception):
    """An input batch/request must be rejected as a whole."""

    def __init__(self, code: str, detail: str, index: Optional[int] = None):
        self.code = code
        self.detail = detail
        self.index = index
        super().__init__(f"{code}: {detail}" + (f" (item {index})" if index is not None else ""))


class CrossrefConflict(CrossrefReject):
    """Same identifier replayed with different content, or a stale revision."""


@dataclass(frozen=True)
class CatalogEntry:
    entry_id: str
    revision: int
    case_id: str
    local_shelf: str               # active shelf number, unique within the case
    doc_id: Optional[str] = None   # attachment to the original archive, optional
    kind: str = ENTRY_KIND_NORMAL
    label: str = ""

    def as_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "revision": self.revision,
            "case_id": self.case_id,
            "doc_id": self.doc_id,
            "local_shelf": self.local_shelf,
            "kind": self.kind,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CatalogEntry":
        return cls(
            entry_id=d["entry_id"],
            revision=int(d["revision"]),
            case_id=d["case_id"],
            doc_id=d.get("doc_id"),
            local_shelf=d["local_shelf"],
            kind=d.get("kind", ENTRY_KIND_NORMAL),
            label=d.get("label", ""),
        )


@dataclass(frozen=True)
class ShelfRedirect:
    """A withdrawn shelf (``from_shelf``) transferred stepwise to ``to_shelf``.

    Both shelf numbers are local to ``case_id``; the same shelf number in a
    different case resolves independently.
    """

    redirect_id: str
    revision: int
    case_id: str
    from_shelf: str
    to_shelf: str
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "redirect_id": self.redirect_id,
            "revision": self.revision,
            "case_id": self.case_id,
            "from_shelf": self.from_shelf,
            "to_shelf": self.to_shelf,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ShelfRedirect":
        return cls(
            redirect_id=d["redirect_id"],
            revision=int(d["revision"]),
            case_id=d["case_id"],
            from_shelf=d["from_shelf"],
            to_shelf=d["to_shelf"],
            note=d.get("note", ""),
        )


@dataclass(frozen=True)
class Citation:
    citation_id: str
    revision: int
    entry_id: str
    category: str
    provenance: str
    target_case_id: str
    target_shelf: str
    weight: int = 1                 # positive; the reading desk may also reweight by category

    def as_dict(self) -> dict:
        return {
            "citation_id": self.citation_id,
            "revision": self.revision,
            "entry_id": self.entry_id,
            "category": self.category,
            "provenance": self.provenance,
            "target_case_id": self.target_case_id,
            "target_shelf": self.target_shelf,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Citation":
        return cls(
            citation_id=d["citation_id"],
            revision=int(d["revision"]),
            entry_id=d["entry_id"],
            category=d["category"],
            provenance=d["provenance"],
            target_case_id=d["target_case_id"],
            target_shelf=d["target_shelf"],
            weight=int(d.get("weight", 1)),
        )


@dataclass(frozen=True)
class ResolvedCitation:
    """A citation together with where its shelf chain actually lands."""

    citation: Citation
    status: str                    # one of the RESOLUTION_* constants
    target_entry_id: Optional[str]
    path: tuple[str, ...]          # shelf numbers walked (start first)
    problem: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "citation": self.citation.as_dict(),
            "status": self.status,
            "target_entry_id": self.target_entry_id,
            "path": list(self.path),
            "problem": self.problem,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ResolvedCitation":
        return cls(
            citation=Citation.from_dict(d["citation"]),
            status=d["status"],
            target_entry_id=d.get("target_entry_id"),
            path=tuple(d.get("path", [])),
            problem=d.get("problem"),
        )


@dataclass
class CatalogSnapshot:
    """Frozen edition of the whole cross-reference catalog.

    Snapshots are self-contained: the reading desk can recompute every
    probability from a snapshot plus a habit table without opening the
    catalog database.  ``digest`` covers all resolution-relevant content.
    """

    snapshot_id: str
    catalog_revision: int
    entries: list[CatalogEntry]
    citations: list[Citation]
    redirects: list[ShelfRedirect]
    resolved: list[ResolvedCitation]
    digest: str
    problems: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "catalog_revision": self.catalog_revision,
            "digest": self.digest,
            "entries": [e.as_dict() for e in sorted(self.entries, key=lambda x: x.entry_id)],
            "citations": [c.as_dict() for c in sorted(self.citations, key=lambda x: x.citation_id)],
            "redirects": [r.as_dict() for r in sorted(self.redirects, key=lambda x: x.redirect_id)],
            "resolved": [r.as_dict() for r in sorted(self.resolved, key=lambda x: x.citation.citation_id)],
            "problems": list(self.problems),
        }

    def payload(self) -> dict:
        """Everything the digest covers (identity columns excluded)."""
        return {
            "catalog_revision": self.catalog_revision,
            "entries": [e.as_dict() for e in sorted(self.entries, key=lambda x: x.entry_id)],
            "citations": [c.as_dict() for c in sorted(self.citations, key=lambda x: x.citation_id)],
            "redirects": [r.as_dict() for r in sorted(self.redirects, key=lambda x: x.redirect_id)],
            "resolved": [r.as_dict() for r in sorted(self.resolved, key=lambda x: x.citation.citation_id)],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CatalogSnapshot":
        return cls(
            snapshot_id=d["snapshot_id"],
            catalog_revision=int(d["catalog_revision"]),
            entries=[CatalogEntry.from_dict(x) for x in d.get("entries", [])],
            citations=[Citation.from_dict(x) for x in d.get("citations", [])],
            redirects=[ShelfRedirect.from_dict(x) for x in d.get("redirects", [])],
            resolved=[ResolvedCitation.from_dict(x) for x in d.get("resolved", [])],
            digest=d["digest"],
            problems=list(d.get("problems", [])),
        )

    def digest_matches(self) -> bool:
        return _digest(self.payload()) == self.digest


def compute_snapshot_digest(snapshot: CatalogSnapshot) -> str:
    return _digest(snapshot.payload())
