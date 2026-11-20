"""Reading-desk analysis: exact landing probabilities from a catalog edition.

The reading desk has its own *habit table* (category -> positive integer
weight) and never opens the catalog database.  It consumes a frozen
:class:`CatalogSnapshot` (a plain JSON document on disk is enough) and solves
the resulting finite Markov chain exactly with :class:`fractions.Fraction`.
No sampling is involved.

Reader behaviour
----------------
* Starting at an ordinary entry, the reader picks one of the entry's
  *positive-weight* citations uniformly at random by effective weight
  (``citation weight x habit-table category weight``).  Categories absent
  from the habit table carry weight zero and are not selectable.
* Clicking a citation whose shelf chain loops or ends at a missing shelf
  ends the visit as ``not_found``.  An entry with no selectable citation
  likewise ends as ``not_found``.
* Reaching the designated original-document entry absorbs the visit as
  ``found`` (that entry's own citations are never followed).
* A visit that can never reach either absorbing condition is ``loop``.

For every entry the desk returns exact rational probabilities for the three
outcomes and, conditioned on eventual success, the mean number of citations
clicked.  When the success probability is zero the conditional mean is
undefined and reported as ``None``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

from .crossref_models import (
    CatalogSnapshot,
    CrossrefReject,
    RESOLUTION_OK,
)

MAX_ENTRIES = 20


def _is_positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


@dataclass(frozen=True)
class Edge:
    """One selectable (or invalid) continuation leaving an entry."""

    citation_id: str
    category: str
    effective_weight: int
    status: str
    target_entry_id: Optional[str]
    problem: Optional[str]

    def as_dict(self) -> dict:
        return {
            "citation_id": self.citation_id,
            "category": self.category,
            "effective_weight": self.effective_weight,
            "status": self.status,
            "target_entry_id": self.target_entry_id,
            "problem": self.problem,
        }


@dataclass
class EntryResult:
    entry_id: str
    p_found: Fraction
    p_not_found: Fraction
    p_loop: Fraction
    # Unconditional expected click count over paths that end at the target.
    expected_clicks_to_target: Fraction
    # Conditional mean clicks given that the visit ends at the target.
    conditional_mean_clicks: Optional[Fraction]
    edges: list[Edge] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "p_found": _fraction_dict(self.p_found),
            "p_not_found": _fraction_dict(self.p_not_found),
            "p_loop": _fraction_dict(self.p_loop),
            "expected_clicks_to_target": _fraction_dict(self.expected_clicks_to_target),
            "conditional_mean_clicks": (
                None
                if self.conditional_mean_clicks is None
                else _fraction_dict(self.conditional_mean_clicks)
            ),
            "edges": [e.as_dict() for e in self.edges],
        }


@dataclass
class ReadingOutcome:
    snapshot_id: str
    target_entry_id: str
    habits: dict[str, int]
    entries: dict[str, EntryResult]
    loop_states: list[str]

    def for_entry(self, entry_id: str) -> EntryResult:
        try:
            return self.entries[entry_id]
        except KeyError:
            raise CrossrefReject("unknown_entry", f"entry {entry_id} is not in the snapshot")

    def as_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "target_entry_id": self.target_entry_id,
            "habits": dict(self.habits),
            "loop_states": list(self.loop_states),
            "entries": {k: v.as_dict() for k, v in sorted(self.entries.items())},
        }


def _fraction_dict(value: Fraction) -> dict:
    return {
        "numerator": value.numerator,
        "denominator": value.denominator,
        "approx": value.numerator / value.denominator,
    }


def _gaussian_solve(matrix: list[list[Fraction]], vector: list[Fraction]) -> list[Fraction]:
    """Exact Gaussian elimination with partial pivoting on ``A x = b``."""
    n = len(matrix)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    # Forward elimination: produce an upper-triangular block.
    for col in range(n):
        pivot = next((r for r in range(col, n) if a[r][col] != 0), None)
        if pivot is None:
            raise CrossrefReject(
                "singular_chain", "transition system is singular (unexpected absorbing structure)"
            )
        a[col], a[pivot] = a[pivot], a[col]
        pivot_value = a[col][col]
        for r in range(col + 1, n):
            factor = a[r][col] / pivot_value
            if factor == 0:
                continue
            a[r] = [v - factor * a[col][j] for j, v in enumerate(a[r])]
    # Back substitution on the upper triangle.
    solution = [Fraction(0) for _ in range(n)]
    for i in range(n - 1, -1, -1):
        residual = a[i][n] - sum(a[i][j] * solution[j] for j in range(i + 1, n))
        solution[i] = residual / a[i][i]
    return solution


class ReadingDesk:
    """Exact analyser bound to one frozen edition and one habit table."""

    def __init__(self, snapshot: CatalogSnapshot, habits: dict[str, int]):
        for category, weight in habits.items():
            if not isinstance(category, str) or not category:
                raise CrossrefReject("invalid_habit", "habit categories must be non-empty strings")
            if not _is_positive_int(weight):
                raise CrossrefReject(
                    "invalid_habit", f"habit weight for {category} must be a positive integer"
                )
        if not snapshot.digest_matches():
            raise CrossrefReject(
                "snapshot_tampered",
                f"snapshot {snapshot.snapshot_id} digest does not match its content",
            )
        if len(snapshot.entries) > MAX_ENTRIES:
            raise CrossrefReject(
                "catalog_too_large",
                f"a catalog holds at most {MAX_ENTRIES} entries, got {len(snapshot.entries)}",
            )
        entry_ids = {e.entry_id for e in snapshot.entries}
        if len(entry_ids) != len(snapshot.entries):
            raise CrossrefReject("invalid_snapshot", "duplicate entry_id in snapshot")
        for citation in snapshot.citations:
            if citation.entry_id not in entry_ids:
                raise CrossrefReject(
                    "invalid_snapshot",
                    f"citation {citation.citation_id} hangs off unknown entry {citation.entry_id}",
                )
            if not _is_positive_int(citation.weight):
                raise CrossrefReject(
                    "invalid_snapshot", f"citation {citation.citation_id} has a non-positive weight"
                )
        self.snapshot = snapshot
        self.habits = dict(habits)
        self._entry_ids = entry_ids
        self._resolved = {r.citation.citation_id: r for r in snapshot.resolved}

    # ------------------------------------------------------------ construction
    @classmethod
    def from_file(cls, path: str, habits: dict[str, int]) -> "ReadingDesk":
        """Open a self-contained edition JSON without any catalog database."""
        with open(path, encoding="utf-8") as fh:
            snapshot = CatalogSnapshot.from_dict(json.load(fh))
        return cls(snapshot, habits)

    def _edges_for(self, entry_id: str) -> list[Edge]:
        edges: list[Edge] = []
        for resolved in self.snapshot.resolved:
            if resolved.citation.entry_id != entry_id:
                continue
            category = resolved.citation.category
            category_weight = self.habits.get(category, 0)
            effective = resolved.citation.weight * category_weight
            if effective <= 0:
                continue  # zero-weight category: not selectable, ignored entirely
            edges.append(Edge(
                citation_id=resolved.citation.citation_id,
                category=category,
                effective_weight=effective,
                status=resolved.status,
                target_entry_id=resolved.target_entry_id,
                problem=resolved.problem,
            ))
        return edges

    # ------------------------------------------------------------------ solve
    def analyze_all(self, target_entry_id: str) -> ReadingOutcome:
        if target_entry_id not in self._entry_ids:
            raise CrossrefReject(
                "unknown_target", f"target entry {target_entry_id} is not in the snapshot"
            )

        edges: dict[str, list[Edge]] = {
            eid: self._edges_for(eid) for eid in self._entry_ids
        }
        totals = {eid: sum(e.effective_weight for e in es) for eid, es in edges.items()}

        # Partition the chain by reverse reachability from an absorbing
        # condition: reaching T, clicking an invalid pointer, or arriving at an
        # entry with no selectable citation.  States outside this closure can
        # never terminate and are the genuine infinite-loop states.
        absorbing_capable = self._absorbing_capable_set(edges, totals, target_entry_id)
        loop_states = sorted(self._entry_ids - absorbing_capable)

        solvable = sorted(absorbing_capable)
        index = {eid: i for i, eid in enumerate(solvable)}
        n = len(solvable)

        p_found = self._solve_probability(
            solvable, index, n, edges, totals, target_entry_id, kind="found"
        )
        p_not_found = self._solve_probability(
            solvable, index, n, edges, totals, target_entry_id, kind="not_found"
        )
        g_values = self._solve_click_expectation(
            solvable, index, n, edges, totals, target_entry_id, p_found
        )

        results: dict[str, EntryResult] = {}
        for eid in self._entry_ids:
            if eid in absorbing_capable:
                i = index[eid]
                found, nf, gv = p_found[i], p_not_found[i], g_values[i]
                loop_p = Fraction(1) - found - nf
                mean = (gv / found) if found > 0 else None
            else:
                # No absorbing condition is reachable from here: endless loop.
                found, nf, loop_p, gv = Fraction(0), Fraction(0), Fraction(1), Fraction(0)
                mean = None
            results[eid] = EntryResult(
                entry_id=eid,
                p_found=found,
                p_not_found=nf,
                p_loop=loop_p,
                expected_clicks_to_target=gv,
                conditional_mean_clicks=mean,
                edges=edges[eid],
            )
        return ReadingOutcome(
            snapshot_id=self.snapshot.snapshot_id,
            target_entry_id=target_entry_id,
            habits=self.habits,
            entries=results,
            loop_states=loop_states,
        )

    def analyze(self, entry_id: str, target_entry_id: str) -> EntryResult:
        if entry_id not in self._entry_ids:
            raise CrossrefReject("unknown_entry", f"entry {entry_id} is not in the snapshot")
        return self.analyze_all(target_entry_id).for_entry(entry_id)

    # ------------------------------------------------------------- internals
    def _absorbing_capable_set(
        self,
        edges: dict[str, list[Edge]],
        totals: dict[str, int],
        target_entry_id: str,
    ) -> set[str]:
        """Closure of states that can reach an absorbing condition at all.

        Seeds are the target entry, entries whose only continuations are
        invalid pointers (a click ends as ``not_found``) and entries with no
        selectable citation.  Valid edges are then walked *backwards*: any
        predecessor of a capable state is capable too.  Entries left outside
        the closure travel only between real entries forever and are reported
        as infinite-loop states.
        """
        seeds: set[str] = {target_entry_id}
        reverse: dict[str, set[str]] = {eid: set() for eid in self._entry_ids}
        for eid, es in edges.items():
            if totals[eid] == 0:
                seeds.add(eid)  # no selectable citation: immediate not_found
            for edge in es:
                if edge.status == RESOLUTION_OK and edge.target_entry_id is not None:
                    if edge.target_entry_id in reverse:
                        reverse[edge.target_entry_id].add(eid)
                else:
                    # Clicking this pointer ends the visit as not_found.
                    seeds.add(eid)

        capable: set[str] = set()
        stack = list(seeds)
        while stack:
            eid = stack.pop()
            if eid in capable or eid not in reverse:
                continue
            capable.add(eid)
            stack.extend(reverse[eid])
        return capable

    def _solve_probability(
        self, solvable, index, n, edges, totals, target_entry_id, *, kind: str
    ) -> list[Fraction]:
        matrix = [[Fraction(0) for _ in range(n)] for _ in range(n)]
        vector = [Fraction(0) for _ in range(n)]
        for eid in solvable:
            i = index[eid]
            matrix[i][i] = Fraction(1)
            if eid == target_entry_id:
                vector[i] = Fraction(1) if kind == "found" else Fraction(0)
                continue
            total = totals[eid]
            if total == 0:
                # No selectable citation: the visit ends not_found here.
                vector[i] = Fraction(1) if kind == "not_found" else Fraction(0)
                continue
            invalid_weight = 0
            for edge in edges[eid]:
                p = Fraction(edge.effective_weight, total)
                if edge.status == RESOLUTION_OK and edge.target_entry_id in index:
                    matrix[i][index[edge.target_entry_id]] -= p
                elif edge.status == RESOLUTION_OK:
                    # A real entry outside the absorbing closure: this mass
                    # loops forever and is neither found nor not_found.
                    continue
                else:
                    invalid_weight += edge.effective_weight
            if kind == "not_found" and invalid_weight:
                vector[i] = Fraction(invalid_weight, total)
        return _gaussian_solve(matrix, vector)

    def _solve_click_expectation(
        self, solvable, index, n, edges, totals, target_entry_id, p_found
    ) -> list[Fraction]:
        """g_i = unconditional expected clicks counted on successful paths.

        On every departure the reader clicks once; that click is charged to
        the successful-total expectation whenever the visit can still succeed
        from the current state, i.e. with probability ``a_i = p_found(i)``.
        Conditioning on the first step gives, for non-target states,

            g_i - sum_j P_ij g_j = a_i

        with ``g_target = 0`` (the visit ends there with no further click) and
        dead-end states carrying no successful continuation.  The conditional
        mean reported to the desk is ``g_i / a_i``.
        """
        matrix = [[Fraction(0) for _ in range(n)] for _ in range(n)]
        vector = [Fraction(0) for _ in range(n)]
        for eid in solvable:
            i = index[eid]
            matrix[i][i] = Fraction(1)
            if eid == target_entry_id:
                continue
            total = totals[eid]
            if total == 0:
                continue
            for edge in edges[eid]:
                if edge.status != RESOLUTION_OK or edge.target_entry_id not in index:
                    continue  # invalid pointer or a pure-loop state: no successful continuation
                j = index[edge.target_entry_id]
                p = Fraction(edge.effective_weight, total)
                if j != i:
                    # Self-edges stay absorbed in the unit diagonal.
                    matrix[i][j] -= p
            vector[i] = p_found[i]
        return _gaussian_solve(matrix, vector)


def load_snapshot_file(path: str) -> CatalogSnapshot:
    """Read and digest-check a frozen edition JSON for offline analysis."""
    with open(path, encoding="utf-8") as fh:
        snapshot = CatalogSnapshot.from_dict(json.load(fh))
    if not snapshot.digest_matches():
        raise CrossrefReject(
            "snapshot_tampered", f"snapshot file {path} digest does not match its content"
        )
    return snapshot
