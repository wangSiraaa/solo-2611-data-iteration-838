"""SQLite-backed cross-reference catalog with frozen, restorable editions.

Responsibilities
----------------
* Receive catalog revisions attached to the same fictional archives/cases:
  entries (with one active local shelf each), stepwise shelf transfers and
  citations.  Batches are validated atomically with the same replay rules as
  the other stores: identical replay is a no-op, a different payload under
  the same revision conflicts, older revisions conflict.
* Resolve every citation's shelf chain at snapshot time.  A chain that
  closes a loop, or ends at a shelf with no holder, is reported distinctly
  and the problematic citation is preserved verbatim.
* Publish immutable :class:`CatalogSnapshot` editions.  A later correction
  never rewrites an old snapshot, so an old edition can always be reopened
  and re-analysed.

The store never computes reading-desk probabilities; that lives in
:mod:`reading_desk`, which works from a snapshot alone.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterable, Optional

from .crossref_models import (
    CatalogEntry,
    CatalogSnapshot,
    Citation,
    CrossrefConflict,
    CrossrefReject,
    ResolvedCitation,
    ShelfRedirect,
    compute_snapshot_digest,
    crossref_canon,
    ENTRY_KIND_NORMAL,
    ENTRY_KIND_ORIGINAL,
    RESOLUTION_LOOP,
    RESOLUTION_MISSING,
    RESOLUTION_OK,
)

SCHEMA_VERSION = 1


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class CrossrefCatalog:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._depth = 0
        self._init_schema()

    # ------------------------------------------------------------------ setup
    @contextmanager
    def _tx(self):
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except Exception:
            self._depth -= 1
            if self._depth == 0:
                self.conn.rollback()
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.commit()

    def _init_schema(self) -> None:
        with self._tx():
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entries (
                    entry_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    case_id TEXT NOT NULL,
                    doc_id TEXT,
                    local_shelf TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    canon TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_case_shelf
                    ON entries(case_id, local_shelf);
                CREATE TABLE IF NOT EXISTS redirects (
                    redirect_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    case_id TEXT NOT NULL,
                    from_shelf TEXT NOT NULL,
                    to_shelf TEXT NOT NULL,
                    note TEXT NOT NULL,
                    canon TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_redirects_case_from
                    ON redirects(case_id, from_shelf);
                CREATE TABLE IF NOT EXISTS citations (
                    citation_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    entry_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    target_case_id TEXT NOT NULL,
                    target_shelf TEXT NOT NULL,
                    weight INTEGER NOT NULL,
                    canon TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_citations_entry ON citations(entry_id);
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    catalog_revision INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('catalog_revision', '0')"
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- helpers
    def _meta_int(self, key: str) -> int:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return int(row["value"]) if row else 0

    def _set_meta_int(self, key: str, value: int) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    @property
    def catalog_revision(self) -> int:
        return self._meta_int("catalog_revision")

    def _bump_catalog(self) -> None:
        self._set_meta_int("catalog_revision", self.catalog_revision + 1)

    # --------------------------------------------------------------- entries
    def import_entries(self, entries: Iterable[dict | CatalogEntry]) -> dict:
        parsed: list[CatalogEntry] = []
        for i, raw in enumerate(entries):
            e = raw if isinstance(raw, CatalogEntry) else CatalogEntry(
                entry_id=str(raw.get("entry_id", "")),
                revision=raw.get("revision"),
                case_id=str(raw.get("case_id", "")),
                doc_id=(None if raw.get("doc_id") is None else str(raw.get("doc_id"))),
                local_shelf=str(raw.get("local_shelf", "")),
                kind=str(raw.get("kind", ENTRY_KIND_NORMAL)),
                label=str(raw.get("label", "")),
            )
            if not e.entry_id:
                raise CrossrefReject("invalid_entry", "entry_id required", i)
            if not _is_int(e.revision) or e.revision < 1:
                raise CrossrefReject("invalid_entry", "revision must be int >= 1", i)
            if not e.case_id:
                raise CrossrefReject("invalid_entry", "case_id required", i)
            if not e.local_shelf:
                raise CrossrefReject("invalid_entry", "local_shelf required", i)
            if e.kind not in (ENTRY_KIND_NORMAL, ENTRY_KIND_ORIGINAL):
                raise CrossrefReject("invalid_entry", f"unknown kind {e.kind}", i)
            parsed.append(e)

        # One active shelf may hold only one entry within a case.  Check the
        # batch against itself first, then against the folded live catalog.
        seen: dict[tuple[str, str], str] = {}
        for i, e in enumerate(parsed):
            key = (e.case_id, e.local_shelf)
            holder = seen.get(key)
            if holder is not None and holder != e.entry_id:
                raise CrossrefReject(
                    "duplicate_shelf",
                    f"shelf {e.local_shelf} in case {e.case_id} claimed by "
                    f"{holder} and {e.entry_id}",
                    i,
                )
            row = self.conn.execute(
                "SELECT entry_id FROM entries WHERE case_id=? AND local_shelf=?",
                key,
            ).fetchone()
            if row is not None and row["entry_id"] != e.entry_id:
                raise CrossrefReject(
                    "duplicate_shelf",
                    f"shelf {e.local_shelf} in case {e.case_id} already held by {row['entry_id']}",
                    i,
                )
            seen[key] = e.entry_id

        pending: dict[str, CatalogEntry] = {}
        changed = False
        for i, e in enumerate(parsed):
            row = self.conn.execute(
                "SELECT * FROM entries WHERE entry_id=?", (e.entry_id,)
            ).fetchone()
            current = pending.get(e.entry_id)
            if current is None and row is not None:
                current = self._row_to_entry(row)
            if current is not None:
                same_content = (
                    current.case_id == e.case_id
                    and current.doc_id == e.doc_id
                    and current.local_shelf == e.local_shelf
                    and current.kind == e.kind
                    and current.label == e.label
                )
                if e.revision == current.revision:
                    if not same_content:
                        raise CrossrefConflict(
                            "entry_conflict",
                            f"entry {e.entry_id} rev {e.revision} replayed with different content",
                            i,
                        )
                    continue
                if e.revision < current.revision:
                    raise CrossrefConflict(
                        "stale_revision",
                        f"entry {e.entry_id} rev {e.revision} older than stored rev {current.revision}",
                        i,
                    )
                pending[e.entry_id] = e
                changed = True
            else:
                pending[e.entry_id] = e
                changed = True

        if changed:
            with self._tx():
                for e in pending.values():
                    self.conn.execute(
                        "INSERT INTO entries(entry_id, revision, case_id, doc_id, local_shelf, "
                        "kind, label, canon) VALUES (?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(entry_id) DO UPDATE SET revision=excluded.revision, "
                        "case_id=excluded.case_id, doc_id=excluded.doc_id, "
                        "local_shelf=excluded.local_shelf, kind=excluded.kind, "
                        "label=excluded.label, canon=excluded.canon",
                        (e.entry_id, e.revision, e.case_id, e.doc_id, e.local_shelf,
                         e.kind, e.label, crossref_canon(e.as_dict())),
                    )
                self._bump_catalog()
        return {"accepted": len(parsed), "changed": changed}

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> CatalogEntry:
        return CatalogEntry(
            entry_id=row["entry_id"], revision=row["revision"], case_id=row["case_id"],
            doc_id=row["doc_id"], local_shelf=row["local_shelf"], kind=row["kind"],
            label=row["label"],
        )

    # ------------------------------------------------------------- redirects
    def import_redirects(self, redirects: Iterable[dict | ShelfRedirect]) -> dict:
        parsed: list[ShelfRedirect] = []
        for i, raw in enumerate(redirects):
            r = raw if isinstance(raw, ShelfRedirect) else ShelfRedirect(
                redirect_id=str(raw.get("redirect_id", "")),
                revision=raw.get("revision"),
                case_id=str(raw.get("case_id", "")),
                from_shelf=str(raw.get("from_shelf", "")),
                to_shelf=str(raw.get("to_shelf", "")),
                note=str(raw.get("note", "")),
            )
            if not r.redirect_id:
                raise CrossrefReject("invalid_redirect", "redirect_id required", i)
            if not _is_int(r.revision) or r.revision < 1:
                raise CrossrefReject("invalid_redirect", "revision must be int >= 1", i)
            if not r.case_id or not r.from_shelf or not r.to_shelf:
                raise CrossrefReject(
                    "invalid_redirect", "case_id, from_shelf and to_shelf required", i
                )
            if r.from_shelf == r.to_shelf:
                raise CrossrefReject(
                    "invalid_redirect", "a shelf cannot be transferred to itself", i
                )
            parsed.append(r)

        # A withdrawn shelf has at most one outgoing transfer.
        for i, r in enumerate(parsed):
            row = self.conn.execute(
                "SELECT redirect_id FROM redirects WHERE case_id=? AND from_shelf=?",
                (r.case_id, r.from_shelf),
            ).fetchone()
            if row is not None and row["redirect_id"] != r.redirect_id:
                raise CrossrefReject(
                    "duplicate_transfer",
                    f"shelf {r.from_shelf} in case {r.case_id} already has transfer "
                    f"{row['redirect_id']}",
                    i,
                )

        pending: dict[str, ShelfRedirect] = {}
        changed = False
        for i, r in enumerate(parsed):
            row = self.conn.execute(
                "SELECT * FROM redirects WHERE redirect_id=?", (r.redirect_id,)
            ).fetchone()
            current = pending.get(r.redirect_id)
            if current is None and row is not None:
                current = self._row_to_redirect(row)
            if current is not None:
                same_content = (
                    current.case_id == r.case_id
                    and current.from_shelf == r.from_shelf
                    and current.to_shelf == r.to_shelf
                    and current.note == r.note
                )
                if r.revision == current.revision:
                    if not same_content:
                        raise CrossrefConflict(
                            "redirect_conflict",
                            f"redirect {r.redirect_id} rev {r.revision} replayed with "
                            "different content",
                            i,
                        )
                    continue
                if r.revision < current.revision:
                    raise CrossrefConflict(
                        "stale_revision",
                        f"redirect {r.redirect_id} rev {r.revision} older than stored rev "
                        f"{current.revision}",
                        i,
                    )
                pending[r.redirect_id] = r
                changed = True
            else:
                pending[r.redirect_id] = r
                changed = True

        if changed:
            with self._tx():
                for r in pending.values():
                    self.conn.execute(
                        "INSERT INTO redirects(redirect_id, revision, case_id, from_shelf, "
                        "to_shelf, note, canon) VALUES (?,?,?,?,?,?,?) "
                        "ON CONFLICT(redirect_id) DO UPDATE SET revision=excluded.revision, "
                        "case_id=excluded.case_id, from_shelf=excluded.from_shelf, "
                        "to_shelf=excluded.to_shelf, note=excluded.note, canon=excluded.canon",
                        (r.redirect_id, r.revision, r.case_id, r.from_shelf, r.to_shelf,
                         r.note, crossref_canon(r.as_dict())),
                    )
                self._bump_catalog()
        return {"accepted": len(parsed), "changed": changed}

    @staticmethod
    def _row_to_redirect(row: sqlite3.Row) -> ShelfRedirect:
        return ShelfRedirect(
            redirect_id=row["redirect_id"], revision=row["revision"], case_id=row["case_id"],
            from_shelf=row["from_shelf"], to_shelf=row["to_shelf"], note=row["note"],
        )

    # -------------------------------------------------------------- citations
    def import_citations(self, citations: Iterable[dict | Citation]) -> dict:
        parsed: list[Citation] = []
        for i, raw in enumerate(citations):
            c = raw if isinstance(raw, Citation) else Citation(
                citation_id=str(raw.get("citation_id", "")),
                revision=raw.get("revision"),
                entry_id=str(raw.get("entry_id", "")),
                category=str(raw.get("category", "")),
                provenance=str(raw.get("provenance", "")),
                target_case_id=str(raw.get("target_case_id", "")),
                target_shelf=str(raw.get("target_shelf", "")),
                weight=int(raw.get("weight", 1)),
            )
            if not c.citation_id:
                raise CrossrefReject("invalid_citation", "citation_id required", i)
            if not _is_int(c.revision) or c.revision < 1:
                raise CrossrefReject("invalid_citation", "revision must be int >= 1", i)
            if not c.entry_id:
                raise CrossrefReject("invalid_citation", "entry_id required", i)
            if not c.category:
                raise CrossrefReject("invalid_citation", "category required", i)
            if not c.target_case_id or not c.target_shelf:
                raise CrossrefReject(
                    "invalid_citation", "target_case_id and target_shelf required", i
                )
            if not _is_int(c.weight) or c.weight < 1:
                raise CrossrefReject(
                    "invalid_citation", "weight must be a positive small integer", i
                )
            if self.conn.execute(
                "SELECT 1 FROM entries WHERE entry_id=?", (c.entry_id,)
            ).fetchone() is None:
                raise CrossrefReject(
                    "unknown_entry", f"citation {c.citation_id} references unknown entry {c.entry_id}",
                    i,
                )
            parsed.append(c)

        pending: dict[str, Citation] = {}
        changed = False
        for i, c in enumerate(parsed):
            row = self.conn.execute(
                "SELECT * FROM citations WHERE citation_id=?", (c.citation_id,)
            ).fetchone()
            current = pending.get(c.citation_id)
            if current is None and row is not None:
                current = self._row_to_citation(row)
            if current is not None:
                same_content = (
                    current.entry_id == c.entry_id
                    and current.category == c.category
                    and current.provenance == c.provenance
                    and current.target_case_id == c.target_case_id
                    and current.target_shelf == c.target_shelf
                    and current.weight == c.weight
                )
                if c.revision == current.revision:
                    if not same_content:
                        raise CrossrefConflict(
                            "citation_conflict",
                            f"citation {c.citation_id} rev {c.revision} replayed with "
                            "different content",
                            i,
                        )
                    continue
                if c.revision < current.revision:
                    raise CrossrefConflict(
                        "stale_revision",
                        f"citation {c.citation_id} rev {c.revision} older than stored rev "
                        f"{current.revision}",
                        i,
                    )
                pending[c.citation_id] = c
                changed = True
            else:
                pending[c.citation_id] = c
                changed = True

        if changed:
            with self._tx():
                for c in pending.values():
                    self.conn.execute(
                        "INSERT INTO citations(citation_id, revision, entry_id, category, "
                        "provenance, target_case_id, target_shelf, weight, canon) "
                        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(citation_id) DO UPDATE SET "
                        "revision=excluded.revision, entry_id=excluded.entry_id, "
                        "category=excluded.category, provenance=excluded.provenance, "
                        "target_case_id=excluded.target_case_id, target_shelf=excluded.target_shelf, "
                        "weight=excluded.weight, canon=excluded.canon",
                        (c.citation_id, c.revision, c.entry_id, c.category, c.provenance,
                         c.target_case_id, c.target_shelf, c.weight, crossref_canon(c.as_dict())),
                    )
                self._bump_catalog()
        return {"accepted": len(parsed), "changed": changed}

    @staticmethod
    def _row_to_citation(row: sqlite3.Row) -> Citation:
        return Citation(
            citation_id=row["citation_id"], revision=row["revision"], entry_id=row["entry_id"],
            category=row["category"], provenance=row["provenance"],
            target_case_id=row["target_case_id"], target_shelf=row["target_shelf"],
            weight=row["weight"],
        )

    # ------------------------------------------------------------- resolution
    def _active_holders(self) -> dict[tuple[str, str], str]:
        """(case_id, local_shelf) -> entry_id for active shelves only."""
        return {
            (r["case_id"], r["local_shelf"]): r["entry_id"]
            for r in self.conn.execute("SELECT case_id, local_shelf, entry_id FROM entries")
        }

    def _redirect_map(self) -> dict[tuple[str, str], str]:
        return {
            (r["case_id"], r["from_shelf"]): r["to_shelf"]
            for r in self.conn.execute("SELECT case_id, from_shelf, to_shelf FROM redirects")
        }

    def resolve_shelf(self, case_id: str, shelf: str) -> tuple[str, Optional[str], tuple[str, ...], Optional[str]]:
        """Follow stepwise transfers from a local shelf inside ``case_id``.

        Returns ``(status, target_entry_id, path, problem)``.  Active shelves
        resolve immediately to their holder; withdrawn shelves walk the
        redirect chain.  A repeated shelf is a loop; a dead end is missing.
        """
        holders = self._active_holders()
        redirects = self._redirect_map()
        path: list[str] = []
        current = shelf
        while True:
            if (case_id, current) in holders:
                return RESOLUTION_OK, holders[(case_id, current)], tuple(path + [current]), None
            path.append(current)
            nxt = redirects.get((case_id, current))
            if nxt is None:
                return (
                    RESOLUTION_MISSING,
                    None,
                    tuple(path),
                    f"shelf {current} in case {case_id} has no holder and no transfer",
                )
            if nxt in path:
                cycle = path[path.index(nxt):] + [nxt]
                return (
                    RESOLUTION_LOOP,
                    None,
                    tuple(path + [nxt]),
                    "transfer cycle: " + " -> ".join(cycle),
                )
            current = nxt

    def _all_entries(self) -> list[CatalogEntry]:
        return [
            self._row_to_entry(r)
            for r in self.conn.execute("SELECT * FROM entries ORDER BY entry_id")
        ]

    def _all_citations(self) -> list[Citation]:
        return [
            self._row_to_citation(r)
            for r in self.conn.execute("SELECT * FROM citations ORDER BY citation_id")
        ]

    def _all_redirects(self) -> list[ShelfRedirect]:
        return [
            self._row_to_redirect(r)
            for r in self.conn.execute("SELECT * FROM redirects ORDER BY redirect_id")
        ]

    def resolve_all(self) -> list[ResolvedCitation]:
        out: list[ResolvedCitation] = []
        for c in self._all_citations():
            status, target, path, problem = self.resolve_shelf(
                c.target_case_id, c.target_shelf
            )
            out.append(ResolvedCitation(
                citation=c, status=status, target_entry_id=target, path=path, problem=problem
            ))
        return out

    # --------------------------------------------------------------- snapshots
    def build_snapshot(self, snapshot_id: str) -> CatalogSnapshot:
        if not snapshot_id:
            raise CrossrefReject("invalid_snapshot", "snapshot_id required")
        entries = self._all_entries()
        citations = self._all_citations()
        redirects = self._all_redirects()
        resolved = self.resolve_all()
        problems = [
            {
                "citation_id": r.citation.citation_id,
                "entry_id": r.citation.entry_id,
                "status": r.status,
                "target_case_id": r.citation.target_case_id,
                "target_shelf": r.citation.target_shelf,
                "path": list(r.path),
                "problem": r.problem,
            }
            for r in resolved
            if r.status != RESOLUTION_OK
        ]
        snapshot = CatalogSnapshot(
            snapshot_id=snapshot_id,
            catalog_revision=self.catalog_revision,
            entries=entries,
            citations=citations,
            redirects=redirects,
            resolved=resolved,
            digest="",
            problems=problems,
        )
        snapshot.digest = compute_snapshot_digest(snapshot)
        return snapshot

    def publish_snapshot(self, snapshot_id: str) -> dict:
        """Freeze the current catalog under ``snapshot_id`` (replay is read-only)."""
        snapshot = self.build_snapshot(snapshot_id)
        payload = crossref_canon(snapshot.as_dict())
        row = self.conn.execute(
            "SELECT payload FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is not None:
            if row["payload"] != payload:
                raise CrossrefConflict(
                    "snapshot_conflict",
                    f"snapshot {snapshot_id} already published with different content",
                )
            return {"snapshot_id": snapshot_id, "replayed": True, "digest": snapshot.digest}
        with self._tx():
            self.conn.execute(
                "INSERT INTO snapshots(snapshot_id, catalog_revision, digest, payload) "
                "VALUES (?,?,?,?)",
                (snapshot_id, snapshot.catalog_revision, snapshot.digest, payload),
            )
        return {"snapshot_id": snapshot_id, "replayed": False, "digest": snapshot.digest}

    def load_snapshot(self, snapshot_id: str) -> CatalogSnapshot:
        row = self.conn.execute(
            "SELECT payload FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise CrossrefReject("unknown_snapshot", f"unknown snapshot {snapshot_id}")
        snapshot = CatalogSnapshot.from_dict(json.loads(row["payload"]))
        if not snapshot.digest_matches():
            raise CrossrefConflict(
                "snapshot_tampered", f"snapshot {snapshot_id} digest does not match its content"
            )
        return snapshot

    def list_snapshots(self) -> list[dict]:
        return [
            {
                "snapshot_id": r["snapshot_id"],
                "catalog_revision": r["catalog_revision"],
                "digest": r["digest"],
            }
            for r in self.conn.execute(
                "SELECT snapshot_id, catalog_revision, digest FROM snapshots ORDER BY snapshot_id"
            )
        ]

    # --------------------------------------------------------------- artifacts
    def write_snapshot_artifact(self, snapshot_id: str, directory: str) -> str:
        """Write a self-contained edition JSON the reading desk opens directly."""
        os.makedirs(directory, exist_ok=True)
        snapshot = self.load_snapshot(snapshot_id)
        path = os.path.join(directory, f"{snapshot_id}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(snapshot.as_dict(), fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        return path

    # ------------------------------------------------------------------ verify
    def verify(self) -> dict:
        """Reopen self-check: snapshot digests, shelf uniqueness and problems."""
        report: dict = {"ok": True, "errors": [], "catalog_revision": self.catalog_revision,
                        "snapshots": [], "problems": []}
        for r in self.conn.execute("SELECT snapshot_id, digest, payload FROM snapshots"):
            snapshot = CatalogSnapshot.from_dict(json.loads(r["payload"]))
            ok = snapshot.digest_matches() and snapshot.digest == r["digest"]
            report["snapshots"].append({"snapshot_id": r["snapshot_id"], "digest_ok": ok})
            if not ok:
                report["ok"] = False
                report["errors"].append(f"snapshot {r['snapshot_id']} digest mismatch")
        dupes = self.conn.execute(
            "SELECT case_id, local_shelf, COUNT(*) AS n FROM entries "
            "GROUP BY case_id, local_shelf HAVING n > 1"
        ).fetchall()
        if dupes:
            report["ok"] = False
            report["errors"].append("duplicate active shelf: " + json.dumps([dict(r) for r in dupes]))
        for resolved in self.resolve_all():
            if resolved.status != RESOLUTION_OK:
                report["problems"].append({
                    "citation_id": resolved.citation.citation_id,
                    "status": resolved.status,
                    "problem": resolved.problem,
                })
        return report
