"""Documented, fully local replay of the cross-reference reading-desk story.

The walkthrough builds one fictional case ``C1`` and freezes four catalog
editions.  It reproduces the worked example from the brief verbatim:

* ``see-v1`` -- entry A has one ``direct`` citation to the original target T
  and one ``see_also`` citation to B; B points at itself.  Equal category
  weights give half the readers one-click success and half an endless loop.
* ``see-v2`` -- B's self citation is corrected back to A.  Every reader
  eventually reaches T; the mean click count conditioned on success is 3.
* ``see-v3`` -- the first two editions are kept untouched; the live catalog
  returns to the v1 topology (B points at itself again) and only A's weights
  are revised to 3 (direct) and 1 (see_also).  The success probability
  becomes 3/4; the desk's own 3:1 habit table on the same edition yields 9/10.
* ``see-v4`` -- broken links are exercised too: a shelf transfer that ends
  at a missing shelf and a transfer chain that closes a loop.  The offending
  citations are preserved in the edition for cleanup.

Old editions are reopened from their self-contained JSON artifacts *after*
the catalog has moved on, proving later corrections never rewrite history.
An extra catalog demonstrates that a zero success probability leaves the
conditional mean undefined.
"""

from __future__ import annotations

import json
import os
from fractions import Fraction

from .crossref_models import (
    CatalogEntry,
    Citation,
    CrossrefReject,
    ENTRY_KIND_ORIGINAL,
    ShelfRedirect,
)
from .crossref_service import CrossrefCatalog
from .reading_desk import ReadingDesk

EQUAL_HABITS = {"direct": 1, "see_also": 1}
WEIGHTED_HABITS = {"direct": 3, "see_also": 1}


def _frac(value: Fraction) -> dict:
    return {"numerator": value.numerator, "denominator": value.denominator,
            "text": f"{value.numerator}/{value.denominator}"}


def _entry_summary(outcome, entry_id: str) -> dict:
    result = outcome.for_entry(entry_id)
    return {
        "entry_id": entry_id,
        "p_found": _frac(result.p_found),
        "p_not_found": _frac(result.p_not_found),
        "p_loop": _frac(result.p_loop),
        "conditional_mean_clicks": (
            None
            if result.conditional_mean_clicks is None
            else _frac(result.conditional_mean_clicks)
        ),
    }


def _seed_case(catalog: CrossrefCatalog) -> None:
    catalog.import_entries([
        CatalogEntry("A", 1, "C1", doc_id="D1", local_shelf="S-A"),
        CatalogEntry("B", 1, "C1", doc_id="D2", local_shelf="S-B"),
        CatalogEntry("T", 1, "C1", doc_id="DT", local_shelf="S-T",
                     kind=ENTRY_KIND_ORIGINAL, label="absorbing original document"),
    ])
    catalog.import_citations([
        Citation("cA1", 1, "A", "direct", "old-catalog p.1", "C1", "S-T"),
        Citation("cA2", 1, "A", "see_also", "old-catalog p.2", "C1", "S-B"),
        Citation("cB1", 1, "B", "see_also", "old-catalog p.3", "C1", "S-B"),
    ])


def _add_broken_links(catalog: CrossrefCatalog) -> None:
    """Shelf replacements with a dead-end chain and a cyclic chain.

    * shelf ``S-GONE`` was withdrawn and transferred to ``S-DEAD``, which no
      longer exists: any citation landing there is a missing target;
    * shelves ``S-X``/``S-Y`` transfer to each other: citations landing there
      walk a loop.  The citations themselves are never deleted.
    """
    catalog.import_entries([
        CatalogEntry("M", 1, "C1", doc_id="D3", local_shelf="S-M"),
        CatalogEntry("L", 1, "C1", doc_id="D4", local_shelf="S-L"),
    ])
    catalog.import_redirects([
        ShelfRedirect("r1", 1, "C1", "S-GONE", "S-DEAD", note="first re-shelving"),
        ShelfRedirect("r2", 1, "C1", "S-X", "S-Y", note="cyclic re-shelving"),
        ShelfRedirect("r3", 1, "C1", "S-Y", "S-X", note="cyclic re-shelving"),
    ])
    catalog.import_citations([
        Citation("cM1", 1, "M", "see_also", "supplement p.9", "C1", "S-GONE"),
        Citation("cL1", 1, "L", "see_also", "supplement p.10", "C1", "S-X"),
    ])


def run_crossref_demo(data_dir: str) -> dict:
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "crossref.sqlite")
    editions_dir = os.path.join(data_dir, "crossref_editions")
    catalog = CrossrefCatalog(db_path)
    steps: list[dict] = []
    ok = True

    def record(label: str, **payload) -> None:
        steps.append({"step": label, **payload})

    try:
        # ---------------------------------------------------------- edition 1
        _seed_case(catalog)
        record("seed_v1", catalog_revision=catalog.catalog_revision)
        catalog.publish_snapshot("see-v1")
        v1_path = catalog.write_snapshot_artifact("see-v1", editions_dir)
        desk_v1 = ReadingDesk(catalog.load_snapshot("see-v1"), EQUAL_HABITS)
        outcome_v1 = desk_v1.analyze_all("T")
        record("v1_equal_habits",
               habits=EQUAL_HABITS,
               A=_entry_summary(outcome_v1, "A"),
               B=_entry_summary(outcome_v1, "B"),
               artifact=v1_path)

        # ---------------------------------------------------------- edition 2
        # B's citation is corrected: self-loop -> back to A.
        catalog.import_citations([
            Citation("cB1", 2, "B", "see_also", "old-catalog p.3 (corrected)", "C1", "S-A"),
        ])
        catalog.publish_snapshot("see-v2")
        v2_path = catalog.write_snapshot_artifact("see-v2", editions_dir)
        desk_v2 = ReadingDesk(catalog.load_snapshot("see-v2"), EQUAL_HABITS)
        outcome_v2 = desk_v2.analyze_all("T")
        record("v2_b_corrected_to_a",
               habits=EQUAL_HABITS,
               A=_entry_summary(outcome_v2, "A"),
               B=_entry_summary(outcome_v2, "B"),
               artifact=v2_path)

        # ---------------------------------------------------------- edition 3
        # The first two editions stay frozen.  For the weighted re-run the
        # catalog returns to the v1 topology (B points at itself again) while
        # only A's direct/see_also weights change to 3 and 1.
        catalog.import_citations([
            Citation("cB1", 3, "B", "see_also", "old-catalog p.3", "C1", "S-B"),
            Citation("cA1", 2, "A", "direct", "old-catalog p.1", "C1", "S-T", weight=3),
            Citation("cA2", 2, "A", "see_also", "old-catalog p.2", "C1", "S-B", weight=1),
        ])
        catalog.publish_snapshot("see-v3")
        v3_path = catalog.write_snapshot_artifact("see-v3", editions_dir)
        desk_v3 = ReadingDesk(catalog.load_snapshot("see-v3"), EQUAL_HABITS)
        outcome_v3 = desk_v3.analyze_all("T")
        record("v3_weights_3_to_1",
               habits=EQUAL_HABITS,
               A=_entry_summary(outcome_v3, "A"),
               B=_entry_summary(outcome_v3, "B"),
               artifact=v3_path)

        # The same frozen edition under the desk's own alternative habit table.
        desk_v3_weighted = ReadingDesk(catalog.load_snapshot("see-v3"), WEIGHTED_HABITS)
        record("v3_under_weighted_habits",
               habits=WEIGHTED_HABITS,
               A=_entry_summary(desk_v3_weighted.analyze_all("T"), "A"))

        # -------------------------------------------------- broken-link edition
        _add_broken_links(catalog)
        catalog.publish_snapshot("see-v4")
        v4_path = catalog.write_snapshot_artifact("see-v4", editions_dir)
        desk_v4 = ReadingDesk(catalog.load_snapshot("see-v4"), EQUAL_HABITS)
        outcome_v4 = desk_v4.analyze_all("T")
        snapshot_v4 = catalog.load_snapshot("see-v4")
        record("v4_broken_links",
               habits=EQUAL_HABITS,
               A=_entry_summary(outcome_v4, "A"),
               M=_entry_summary(outcome_v4, "M"),
               L=_entry_summary(outcome_v4, "L"),
               problems=snapshot_v4.problems,
               artifact=v4_path)

        # ----------------------------- reopen old editions from file, offline
        reopened_v1 = ReadingDesk.from_file(v1_path, EQUAL_HABITS).analyze_all("T")
        reopened_v2 = ReadingDesk.from_file(v2_path, EQUAL_HABITS).analyze_all("T")
        record("reopen_old_editions_from_artifact",
               v1_A=_entry_summary(reopened_v1, "A"),
               v2_A=_entry_summary(reopened_v2, "A"))

        # ------------------- zero success probability: conditional mean undefined
        catalog.import_entries([
            CatalogEntry("U", 1, "C2", doc_id="D9", local_shelf="S-U"),
        ])
        catalog.import_citations([
            Citation("cU1", 1, "U", "see_also", "isolated register", "C2", "S-U"),
        ])
        catalog.publish_snapshot("see-v5")
        v5_path = catalog.write_snapshot_artifact("see-v5", editions_dir)
        desk_v5 = ReadingDesk(catalog.load_snapshot("see-v5"), EQUAL_HABITS)
        outcome_v5 = desk_v5.analyze_all("T")
        record("v5_unreachable_target_undefined_mean",
               U=_entry_summary(outcome_v5, "U"),
               artifact=v5_path)

        report = catalog.verify()
        if not report["ok"]:
            ok = False
    except CrossrefReject as exc:
        ok = False
        record("UNEXPECTED_REJECT", code=exc.code, detail=exc.detail)
    finally:
        catalog.close()

    result = {"ok": ok, "data_dir": os.path.abspath(data_dir), "steps": steps,
              "editions_dir": os.path.abspath(editions_dir)}
    with open(os.path.join(data_dir, "crossref_demo.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return result


def assert_demo_expectations(result: dict) -> None:
    """Programmatic checks used by the test suite and the CLI."""
    steps = {s["step"]: s for s in result["steps"]}

    def f(step: str, entry: str, key: str) -> Fraction:
        node = steps[step][entry][key]
        return Fraction(node["numerator"], node["denominator"])

    # Edition 1: half succeed in one click, half loop forever.
    assert f("v1_equal_habits", "A", "p_found") == Fraction(1, 2)
    assert f("v1_equal_habits", "A", "p_loop") == Fraction(1, 2)
    assert f("v1_equal_habits", "A", "p_not_found") == Fraction(0)
    assert f("v1_equal_habits", "A", "conditional_mean_clicks") == Fraction(1)

    # Edition 2: everyone reaches T; mean 3 clicks from A (B itself: 4).
    assert f("v2_b_corrected_to_a", "A", "p_found") == Fraction(1)
    assert f("v2_b_corrected_to_a", "A", "p_loop") == Fraction(0)
    assert f("v2_b_corrected_to_a", "A", "conditional_mean_clicks") == Fraction(3)
    assert f("v2_b_corrected_to_a", "B", "conditional_mean_clicks") == Fraction(4)

    # Edition 3: v1 topology retained (B loops to itself); direct weighted 3
    # vs see_also 1, so success is 3/4 and the only successful path clicks once.
    assert f("v3_weights_3_to_1", "A", "p_found") == Fraction(3, 4)
    assert f("v3_weights_3_to_1", "A", "p_loop") == Fraction(1, 4)
    assert f("v3_weights_3_to_1", "A", "p_not_found") == Fraction(0)
    assert f("v3_weights_3_to_1", "A", "conditional_mean_clicks") == Fraction(1)

    # The desk's own 3:1 habit table on the same frozen edition: effective
    # weights 9:1, giving 9/10 success.
    weighted = steps["v3_under_weighted_habits"]["A"]
    assert Fraction(weighted["p_found"]["numerator"],
                    weighted["p_found"]["denominator"]) == Fraction(9, 10)
    assert weighted["conditional_mean_clicks"]["text"] == "1/1"

    # Broken links: both failures end the visit as not_found on the click.
    # A cyclic shelf transfer and a dead-end shelf are invalid pointers, not
    # an endless walk between real entries; the citations stay on record.
    assert f("v4_broken_links", "M", "p_not_found") == Fraction(1)
    assert f("v4_broken_links", "M", "p_loop") == Fraction(0)
    assert f("v4_broken_links", "L", "p_not_found") == Fraction(1)
    assert f("v4_broken_links", "L", "p_loop") == Fraction(0)
    statuses = {p["citation_id"]: p["status"] for p in steps["v4_broken_links"]["problems"]}
    assert statuses["cM1"] == "missing"
    assert statuses["cL1"] == "loop"

    # Reopened old editions still reproduce the original numbers.
    assert f("reopen_old_editions_from_artifact", "v1_A", "p_found") == Fraction(1, 2)
    assert f("reopen_old_editions_from_artifact", "v2_A", "conditional_mean_clicks") == Fraction(3)

    # Unreachable target: success zero, conditional mean explicitly undefined.
    assert f("v5_unreachable_target_undefined_mean", "U", "p_found") == Fraction(0)
    assert steps["v5_unreachable_target_undefined_mean"]["U"]["conditional_mean_clicks"] is None
