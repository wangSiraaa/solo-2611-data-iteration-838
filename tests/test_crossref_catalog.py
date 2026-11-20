"""Unittest suite for the cross-reference catalog store.

Covers shelf uniqueness inside a case (same shelf number in different cases
is independent), revision/replay semantics, stepwise shelf transfers with
loop and missing-target distinction, preserved problematic citations and
frozen, restorable snapshots.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from archive_handoff import (
    CatalogEntry,
    Citation,
    CrossrefCatalog,
    CrossrefConflict,
    CrossrefReject,
    ShelfRedirect,
)
from archive_handoff.crossref_models import RESOLUTION_LOOP, RESOLUTION_MISSING, RESOLUTION_OK


def seed(catalog: CrossrefCatalog) -> None:
    catalog.import_entries([
        CatalogEntry("A", 1, "C1", doc_id="D1", local_shelf="S-A"),
        CatalogEntry("B", 1, "C1", doc_id="D2", local_shelf="S-B"),
        CatalogEntry("T", 1, "C1", doc_id="DT", local_shelf="S-T", kind="original"),
    ])
    catalog.import_citations([
        Citation("cA1", 1, "A", "direct", "p1", "C1", "S-T"),
        Citation("cA2", 1, "A", "see_also", "p2", "C1", "S-B"),
        Citation("cB1", 1, "B", "see_also", "p3", "C1", "S-B"),
    ])


class CatalogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog = CrossrefCatalog(os.path.join(self.tmp.name, "crossref.db"))
        seed(self.catalog)

    def tearDown(self):
        self.catalog.close()
        self.tmp.cleanup()


class ShelfUniqueness(CatalogCase):
    def test_active_shelf_holds_one_entry_per_case(self):
        with self.assertRaises(CrossrefReject) as ctx:
            self.catalog.import_entries([
                CatalogEntry("Z", 1, "C1", doc_id="DZ", local_shelf="S-A"),
            ])
        self.assertEqual(ctx.exception.code, "duplicate_shelf")

    def test_same_shelf_number_in_other_case_is_independent(self):
        result = self.catalog.import_entries([
            CatalogEntry("A2", 1, "C2", doc_id="D8", local_shelf="S-A"),
        ])
        self.assertTrue(result["changed"])
        status, target, _, _ = self.catalog.resolve_shelf("C2", "S-A")
        self.assertEqual(status, RESOLUTION_OK)
        self.assertEqual(target, "A2")

    def test_duplicate_within_one_batch_is_rejected(self):
        with self.assertRaises(CrossrefReject) as ctx:
            self.catalog.import_entries([
                CatalogEntry("X", 1, "C9", local_shelf="S1"),
                CatalogEntry("Y", 1, "C9", local_shelf="S1"),
            ])
        self.assertEqual(ctx.exception.code, "duplicate_shelf")


class RevisionSemantics(CatalogCase):
    def test_identical_replay_is_noop(self):
        first = self.catalog.import_citations([
            Citation("cA1", 1, "A", "direct", "p1", "C1", "S-T"),
        ])
        self.assertFalse(first["changed"])

    def test_same_revision_different_content_conflicts(self):
        with self.assertRaises(CrossrefConflict) as ctx:
            self.catalog.import_citations([
                Citation("cA1", 1, "A", "direct", "changed", "C1", "S-T"),
            ])
        self.assertEqual(ctx.exception.code, "citation_conflict")

    def test_older_revision_conflicts(self):
        self.catalog.import_citations([
            Citation("cA1", 3, "A", "direct", "p1", "C1", "S-T", weight=2),
        ])
        with self.assertRaises(CrossrefConflict) as ctx:
            self.catalog.import_citations([
                Citation("cA1", 2, "A", "direct", "p1", "C1", "S-T"),
            ])
        self.assertEqual(ctx.exception.code, "stale_revision")

    def test_batch_is_atomic(self):
        before = self.catalog.catalog_revision
        with self.assertRaises(CrossrefReject):
            self.catalog.import_entries([
                CatalogEntry("OK", 1, "C1", local_shelf="S-OK"),
                CatalogEntry("BAD", 1, "C1", local_shelf="S-A"),  # clashes
            ])
        self.assertEqual(self.catalog.catalog_revision, before)
        status, _, _, _ = self.catalog.resolve_shelf("C1", "S-OK")
        self.assertEqual(status, RESOLUTION_MISSING)

    def test_unknown_entry_rejects_citation_batch(self):
        with self.assertRaises(CrossrefReject) as ctx:
            self.catalog.import_citations([
                Citation("cX", 1, "GHOST", "direct", "p", "C1", "S-T"),
            ])
        self.assertEqual(ctx.exception.code, "unknown_entry")

    def test_positive_weight_required(self):
        with self.assertRaises(CrossrefReject):
            self.catalog.import_citations([
                Citation("cW", 1, "A", "direct", "p", "C1", "S-T", weight=0),
            ])


class ShelfTransfers(CatalogCase):
    def _redirects(self):
        self.catalog.import_redirects([
            ShelfRedirect("r1", 1, "C1", "S-GONE", "S-DEAD"),
            ShelfRedirect("r2", 1, "C1", "S-X", "S-Y"),
            ShelfRedirect("r3", 1, "C1", "S-Y", "S-X"),
            ShelfRedirect("r4", 1, "C1", "S-OLD", "S-A"),
        ])

    def test_stepwise_transfer_reaches_active_holder(self):
        self._redirects()
        status, target, path, _ = self.catalog.resolve_shelf("C1", "S-OLD")
        self.assertEqual(status, RESOLUTION_OK)
        self.assertEqual(target, "A")
        self.assertEqual(path, ("S-OLD", "S-A"))

    def test_missing_target_is_distinct_from_loop(self):
        self._redirects()
        status, target, path, problem = self.catalog.resolve_shelf("C1", "S-GONE")
        self.assertEqual(status, RESOLUTION_MISSING)
        self.assertIsNone(target)
        self.assertEqual(path, ("S-GONE", "S-DEAD"))
        self.assertIn("no holder", problem)

    def test_transfer_cycle_is_reported_with_path(self):
        self._redirects()
        status, target, path, problem = self.catalog.resolve_shelf("C1", "S-X")
        self.assertEqual(status, RESOLUTION_LOOP)
        self.assertIsNone(target)
        self.assertEqual(path, ("S-X", "S-Y", "S-X"))
        self.assertIn("cycle", problem)

    def test_self_transfer_rejected(self):
        with self.assertRaises(CrossrefReject):
            self.catalog.import_redirects([
                ShelfRedirect("r", 1, "C1", "S-A", "S-A"),
            ])

    def test_one_outgoing_transfer_per_withdrawn_shelf(self):
        self.catalog.import_redirects([ShelfRedirect("r", 1, "C1", "S-GONE", "S-DEAD")])
        with self.assertRaises(CrossrefReject) as ctx:
            self.catalog.import_redirects([
                ShelfRedirect("r2", 1, "C1", "S-GONE", "S-A"),
            ])
        self.assertEqual(ctx.exception.code, "duplicate_transfer")

    def test_problematic_citations_are_preserved(self):
        self._redirects()
        self.catalog.import_citations([
            Citation("cM1", 1, "A", "see_also", "old note", "C1", "S-GONE"),
            Citation("cL1", 1, "A", "direct", "other note", "C1", "S-X"),
        ])
        resolved = {r.citation.citation_id: r for r in self.catalog.resolve_all()}
        self.assertEqual(resolved["cM1"].status, RESOLUTION_MISSING)
        self.assertEqual(resolved["cL1"].status, RESOLUTION_LOOP)
        # original wording survives for cleanup
        self.assertEqual(resolved["cM1"].citation.provenance, "old note")


class Snapshots(CatalogCase):
    def test_snapshot_freezes_and_reopens_identically(self):
        self.catalog.publish_snapshot("e1")
        first = self.catalog.load_snapshot("e1")
        # A later correction must not rewrite the frozen edition.
        self.catalog.import_citations([
            Citation("cB1", 2, "B", "see_also", "corrected", "C1", "S-A"),
        ])
        self.catalog.publish_snapshot("e2")
        again = self.catalog.load_snapshot("e1")
        self.assertEqual(first.digest, again.digest)
        self.assertTrue(again.digest_matches())
        self.assertEqual(
            [r.citation.revision for r in again.resolved if r.citation.citation_id == "cB1"],
            [1],
        )

    def test_snapshot_id_replay_conflict(self):
        self.catalog.publish_snapshot("e1")
        self.catalog.import_citations([
            Citation("cB1", 2, "B", "see_also", "corrected", "C1", "S-A"),
        ])
        with self.assertRaises(CrossrefConflict) as ctx:
            self.catalog.publish_snapshot("e1")
        self.assertEqual(ctx.exception.code, "snapshot_conflict")

    def test_artifact_roundtrip_offline(self):
        self.catalog.publish_snapshot("e1")
        path = self.catalog.write_snapshot_artifact("e1", os.path.join(self.tmp.name, "editions"))
        from archive_handoff.reading_desk import load_snapshot_file

        snapshot = load_snapshot_file(path)
        self.assertEqual(snapshot.snapshot_id, "e1")
        self.assertTrue(snapshot.digest_matches())

    def test_verify_reports_tamper_and_problems(self):
        self.catalog.import_redirects([ShelfRedirect("r", 1, "C1", "S-X", "S-Y"),
                                       ShelfRedirect("r2", 1, "C1", "S-Y", "S-X")])
        self.catalog.import_citations([
            Citation("cL", 1, "A", "see_also", "p", "C1", "S-X"),
        ])
        report = self.catalog.verify()
        self.assertTrue(report["ok"])  # problems are kept, not integrity errors
        self.assertEqual([p["status"] for p in report["problems"]], ["loop"])


if __name__ == "__main__":
    unittest.main()
