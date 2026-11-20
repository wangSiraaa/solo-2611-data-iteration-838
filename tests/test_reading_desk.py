"""Unittest suite for the standalone reading-desk analyser.

All results are exact rationals solved over the finite Markov chain; there is
no sampling.  Chains are built as in-memory catalog snapshots, and one case
also reopens a snapshot file without a catalog database to prove offline
recomputation.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from fractions import Fraction

from archive_handoff import (
    CatalogEntry,
    Citation,
    CrossrefCatalog,
    CrossrefReject,
    ReadingDesk,
    ShelfRedirect,
)


def build_catalog() -> CrossrefCatalog:
    catalog = CrossrefCatalog(":memory:")
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
    return catalog


EQUAL = {"direct": 1, "see_also": 1}


class WorkedExample(unittest.TestCase):
    def setUp(self):
        self.catalog = build_catalog()

    def tearDown(self):
        self.catalog.close()

    def test_v1_half_success_half_loop(self):
        snapshot = self.catalog.build_snapshot("v1")
        outcome = ReadingDesk(snapshot, EQUAL).analyze_all("T")
        a = outcome.for_entry("A")
        self.assertEqual(a.p_found, Fraction(1, 2))
        self.assertEqual(a.p_not_found, Fraction(0))
        self.assertEqual(a.p_loop, Fraction(1, 2))
        self.assertEqual(a.conditional_mean_clicks, Fraction(1))
        b = outcome.for_entry("B")
        self.assertEqual(b.p_loop, Fraction(1))
        self.assertIsNone(b.conditional_mean_clicks)
        self.assertEqual(outcome.loop_states, ["B"])
        # probabilities partition the whole space
        for result in outcome.entries.values():
            self.assertEqual(result.p_found + result.p_not_found + result.p_loop, Fraction(1))

    def test_v2_corrected_self_loop_everyone_succeeds_mean_three(self):
        self.catalog.import_citations([
            Citation("cB1", 2, "B", "see_also", "corrected", "C1", "S-A"),
        ])
        outcome = ReadingDesk(self.catalog.build_snapshot("v2"), EQUAL).analyze_all("T")
        a = outcome.for_entry("A")
        self.assertEqual(a.p_found, Fraction(1))
        self.assertEqual(a.p_loop, Fraction(0))
        self.assertEqual(a.conditional_mean_clicks, Fraction(3))
        # From B: B->A then A's geometric detours; mean 4 clicks.
        self.assertEqual(outcome.for_entry("B").conditional_mean_clicks, Fraction(4))
        self.assertEqual(outcome.loop_states, [])

    def test_v3_weight_three_to_one_gives_three_quarters(self):
        self.catalog.import_citations([
            Citation("cA1", 2, "A", "direct", "p1", "C1", "S-T", weight=3),
            Citation("cA2", 2, "A", "see_also", "p2", "C1", "S-B", weight=1),
        ])
        outcome = ReadingDesk(self.catalog.build_snapshot("v3"), EQUAL).analyze_all("T")
        a = outcome.for_entry("A")
        self.assertEqual(a.p_found, Fraction(3, 4))
        self.assertEqual(a.p_loop, Fraction(1, 4))
        self.assertEqual(a.conditional_mean_clicks, Fraction(1))

    def test_desk_habit_table_reweights_without_touching_catalog(self):
        # Same frozen v1 edition, desk-side 3:1 habits -> effective 3:1.
        snapshot = self.catalog.build_snapshot("v1")
        outcome = ReadingDesk(snapshot, {"direct": 3, "see_also": 1}).analyze_all("T")
        self.assertEqual(outcome.for_entry("A").p_found, Fraction(3, 4))
        # The catalog itself still carries equal citation weights.
        self.assertEqual(
            [c.weight for c in snapshot.citations if c.citation_id == "cA1"], [1]
        )


class ZeroWeightAndMissingCategories(unittest.TestCase):
    def setUp(self):
        self.catalog = build_catalog()

    def tearDown(self):
        self.catalog.close()

    def test_unlisted_category_has_zero_weight(self):
        # Only "direct" is a reading habit: A's see_also citation is inert,
        # so every reader goes straight to T.
        outcome = ReadingDesk(self.catalog.build_snapshot("s"), {"direct": 1}).analyze_all("T")
        a = outcome.for_entry("A")
        self.assertEqual(a.p_found, Fraction(1))
        self.assertEqual(len(a.edges), 1)
        self.assertEqual(a.edges[0].citation_id, "cA1")

    def test_no_selectable_citation_ends_not_found(self):
        # B's only category is absent from the habits: dead end.
        outcome = ReadingDesk(self.catalog.build_snapshot("s"), {"direct": 1}).analyze_all("T")
        b = outcome.for_entry("B")
        self.assertEqual(b.p_not_found, Fraction(1))
        self.assertEqual(b.p_found, Fraction(0))
        self.assertIsNone(b.conditional_mean_clicks)

    def test_zero_success_leaves_conditional_mean_undefined(self):
        # Entry B can never reach T (pure self loop): mean is None, not 0.
        outcome = ReadingDesk(self.catalog.build_snapshot("s"), EQUAL).analyze_all("T")
        self.assertIsNone(outcome.for_entry("B").conditional_mean_clicks)
        self.assertEqual(outcome.for_entry("B").p_found, Fraction(0))

    def test_invalid_habit_weights_rejected(self):
        snapshot = self.catalog.build_snapshot("s")
        with self.assertRaises(CrossrefReject):
            ReadingDesk(snapshot, {"direct": 0})
        with self.assertRaises(CrossrefReject):
            ReadingDesk(snapshot, {"direct": 1.5})  # type: ignore[dict-item]


class InvalidPointers(unittest.TestCase):
    def test_missing_shelf_and_transfer_loop_end_as_not_found(self):
        catalog = CrossrefCatalog(":memory:")
        catalog.import_entries([
            CatalogEntry("M", 1, "C1", local_shelf="S-M"),
            CatalogEntry("L", 1, "C1", local_shelf="S-L"),
            CatalogEntry("T", 1, "C1", local_shelf="S-T", kind="original"),
        ])
        catalog.import_redirects([
            ShelfRedirect("r1", 1, "C1", "S-GONE", "S-DEAD"),
            ShelfRedirect("r2", 1, "C1", "S-X", "S-Y"),
            ShelfRedirect("r3", 1, "C1", "S-Y", "S-X"),
        ])
        catalog.import_citations([
            Citation("cM1", 1, "M", "see_also", "p", "C1", "S-GONE"),
            Citation("cL1", 1, "L", "see_also", "p", "C1", "S-X"),
        ])
        outcome = ReadingDesk(catalog.build_snapshot("s"), EQUAL).analyze_all("T")
        self.assertEqual(outcome.for_entry("M").p_not_found, Fraction(1))
        self.assertEqual(outcome.for_entry("L").p_not_found, Fraction(1))
        self.assertEqual(outcome.loop_states, [])  # invalid pointers are not walks
        catalog.close()

    def test_invalid_pointer_mass_mixes_with_success(self):
        # A: one good pointer to T, one dead pointer, equal weights.
        catalog = CrossrefCatalog(":memory:")
        catalog.import_entries([
            CatalogEntry("A", 1, "C1", local_shelf="S-A"),
            CatalogEntry("T", 1, "C1", local_shelf="S-T", kind="original"),
        ])
        catalog.import_citations([
            Citation("g", 1, "A", "direct", "p", "C1", "S-T"),
            Citation("bad", 1, "A", "see_also", "p", "C1", "S-DEAD"),
        ])
        outcome = ReadingDesk(catalog.build_snapshot("s"), EQUAL).analyze_all("T")
        a = outcome.for_entry("A")
        self.assertEqual(a.p_found, Fraction(1, 2))
        self.assertEqual(a.p_not_found, Fraction(1, 2))
        self.assertEqual(a.p_loop, Fraction(0))
        catalog.close()


class MultipleCitationsSameTarget(unittest.TestCase):
    def test_two_categories_to_same_target_combine_weights(self):
        catalog = CrossrefCatalog(":memory:")
        catalog.import_entries([
            CatalogEntry("A", 1, "C1", local_shelf="S-A"),
            CatalogEntry("T", 1, "C1", local_shelf="S-T", kind="original"),
        ])
        catalog.import_citations([
            Citation("g1", 1, "A", "direct", "p", "C1", "S-T", weight=2),
            Citation("g2", 1, "A", "see_also", "p", "C1", "S-T", weight=3),
        ])
        outcome = ReadingDesk(catalog.build_snapshot("s"), EQUAL).analyze_all("T")
        self.assertEqual(outcome.for_entry("A").p_found, Fraction(1))
        self.assertEqual(
            sorted(e.effective_weight for e in outcome.for_entry("A").edges), [2, 3]
        )
        catalog.close()


class LongerChainAndLimits(unittest.TestCase):
    def test_three_hop_chain_exact_mean(self):
        # A -> B -> C -> T, deterministic.  Mean clicks: A=3, B=2, C=1.
        catalog = CrossrefCatalog(":memory:")
        catalog.import_entries([
            CatalogEntry("A", 1, "C1", local_shelf="S-A"),
            CatalogEntry("B", 1, "C1", local_shelf="S-B"),
            CatalogEntry("C", 1, "C1", local_shelf="S-C"),
            CatalogEntry("T", 1, "C1", local_shelf="S-T", kind="original"),
        ])
        catalog.import_citations([
            Citation("a", 1, "A", "direct", "p", "C1", "S-B"),
            Citation("b", 1, "B", "direct", "p", "C1", "S-C"),
            Citation("c", 1, "C", "direct", "p", "C1", "S-T"),
        ])
        outcome = ReadingDesk(catalog.build_snapshot("s"), EQUAL).analyze_all("T")
        self.assertEqual(outcome.for_entry("A").conditional_mean_clicks, Fraction(3))
        self.assertEqual(outcome.for_entry("B").conditional_mean_clicks, Fraction(2))
        self.assertEqual(outcome.for_entry("C").conditional_mean_clicks, Fraction(1))
        catalog.close()

    def test_catalog_over_twenty_entries_rejected(self):
        catalog = CrossrefCatalog(":memory:")
        catalog.import_entries([
            CatalogEntry(f"E{i:02d}", 1, "C1", local_shelf=f"S{i:02d}") for i in range(21)
        ])
        with self.assertRaises(CrossrefReject) as ctx:
            ReadingDesk(catalog.build_snapshot("big"), EQUAL)
        self.assertEqual(ctx.exception.code, "catalog_too_large")
        catalog.close()

    def test_unknown_target_and_entry_rejected(self):
        catalog = build_catalog()
        desk = ReadingDesk(catalog.build_snapshot("s"), EQUAL)
        with self.assertRaises(CrossrefReject):
            desk.analyze_all("GHOST")
        with self.assertRaises(CrossrefReject):
            desk.analyze("GHOST", "T")
        catalog.close()


class OfflineReopen(unittest.TestCase):
    def test_snapshot_file_analysed_without_database(self):
        catalog = build_catalog()
        catalog.publish_snapshot("e1")
        with tempfile.TemporaryDirectory() as tmp:
            path = catalog.write_snapshot_artifact("e1", tmp)
            catalog.close()  # drop the in-memory catalog entirely
            desk = ReadingDesk.from_file(path, EQUAL)
            outcome = desk.analyze_all("T")
            self.assertEqual(outcome.for_entry("A").p_found, Fraction(1, 2))

    def test_tampered_snapshot_digest_rejected(self):
        catalog = build_catalog()
        catalog.publish_snapshot("e1")
        with tempfile.TemporaryDirectory() as tmp:
            path = catalog.write_snapshot_artifact("e1", tmp)
            catalog.close()
            import json

            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["entries"][0]["label"] = "tampered"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            with self.assertRaises(CrossrefReject) as ctx:
                ReadingDesk.from_file(path, EQUAL)
            self.assertEqual(ctx.exception.code, "snapshot_tampered")


if __name__ == "__main__":
    unittest.main()
