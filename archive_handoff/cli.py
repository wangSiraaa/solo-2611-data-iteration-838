"""Command-line entry point.

Commands are deliberately small and scriptable.  The handoff store and the
rule/event store are two separate SQLite databases; the CLI wires them via
qualification products only.

Typical flow::

    python -m archive_handoff.cli init --data artifacts/demo
    python -m archive_handoff.cli import-materials --data artifacts/demo \\
        --materials materials/751
    python -m archive_handoff.cli publish --data artifacts/demo --day 40 \\
        --product Q40
    python -m archive_handoff.cli submit --data artifacts/demo \\
        --request '{"handoff_id":"PARTIAL", ...}'

Use ``--materials <dir>`` to point at any directory with the same file layout
(documents.json, rules.json, events.jsonl, holds.jsonl).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .handoff_service import HandoffRepo
from .materials import import_materials
from .models import RejectError
from .rules_service import RuleEventStore
from .crossref_service import CrossrefCatalog
from .crossref_models import CrossrefReject
from .reading_desk import ReadingDesk


def _paths(data_dir: str) -> tuple[str, str]:
    return (
        os.path.join(data_dir, "rules_events.sqlite"),
        os.path.join(data_dir, "handoff.sqlite"),
    )


def _crossref_path(data_dir: str) -> str:
    return os.path.join(data_dir, "crossref.sqlite")


def _open(data_dir: str, live: bool = True) -> tuple[RuleEventStore, HandoffRepo]:
    """Open both stores.

    ``live=True`` gives the handoff store a read-only qualification provider so
    staleness checks see a hold/event revision immediately.  It never transfers
    custody off a live product: an actual handoff still requires an ingested,
    frozen product.
    """
    os.makedirs(data_dir, exist_ok=True)
    rules_db, handoff_db = _paths(data_dir)
    rules = RuleEventStore(rules_db)
    handoff = HandoffRepo(
        handoff_db, qualification_provider=rules.build_product if live else None
    )
    return rules, handoff


def _dump(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _read_request(args) -> dict:
    if args.request:
        return json.loads(args.request)
    if args.request_file:
        with open(args.request_file, encoding="utf-8") as fh:
            return json.load(fh)
    raise SystemExit("provide --request JSON or --request-file PATH")


def cmd_init(args) -> None:
    os.makedirs(args.data, exist_ok=True)
    rules, handoff = _open(args.data)
    rules.close()
    handoff.close()
    _dump({"data": os.path.abspath(args.data), "initialised": True})


def cmd_import(args) -> None:
    rules, handoff = _open(args.data)
    result = import_materials(rules, handoff, args.materials)
    rules.close()
    handoff.close()
    _dump(result)


def cmd_publish(args) -> None:
    rules, handoff = _open(args.data)
    product = rules.build_product(args.day, args.doc)
    saved = rules.save_product(args.product, product)
    ingested = handoff.ingest_product(args.product, product)
    path = rules.write_product_artifact(args.product, product, os.path.join(args.data, "products"))
    out = {
        "product_id": args.product,
        "as_of_day": args.day,
        "source_revision": rules.product_record(args.product)["source_revision"],
        "saved": saved,
        "ingested": ingested,
        "artifact": path,
        "documents": {
            k: {
                "computed_day": v.computed_day,
                "time_condition_met": v.time_condition_met,
                "releasable": v.releasable,
                "blocks": [b.kind for b in v.block_reasons],
            }
            for k, v in sorted(product.documents.items())
        },
    }
    rules.close()
    handoff.close()
    _dump(out)


def cmd_submit(args) -> None:
    rules, handoff = _open(args.data)
    try:
        result = handoff.submit(_read_request(args))
    except RejectError as exc:
        rules.close()
        handoff.close()
        _dump({"accepted": False, "code": exc.code, "detail": exc.detail})
        raise SystemExit(2)
    rules.close()
    handoff.close()
    _dump({"accepted": True, **result})


def cmd_prepare(args) -> None:
    rules, handoff = _open(args.data)
    try:
        result = handoff.prepare(_read_request(args))
    except RejectError as exc:
        rules.close()
        handoff.close()
        _dump({"accepted": False, "code": exc.code, "detail": exc.detail})
        raise SystemExit(2)
    rules.close()
    handoff.close()
    _dump({"accepted": True, **result})


def cmd_confirm(args) -> None:
    rules, handoff = _open(args.data)
    try:
        result = handoff.confirm(args.proposal, handoff_id=args.handoff)
    except RejectError as exc:
        rules.close()
        handoff.close()
        _dump({"accepted": False, "code": exc.code, "detail": exc.detail})
        raise SystemExit(2)
    rules.close()
    handoff.close()
    _dump({"accepted": True, **result})


def cmd_plan(args) -> None:
    rules, handoff = _open(args.data)
    try:
        result = handoff.prepare_and_publish(
            args.plan, args.product, args.day,
            from_party=args.from_party, to_party=args.to_party,
        )
    except RejectError as exc:
        rules.close()
        handoff.close()
        _dump({"accepted": False, "code": exc.code, "detail": exc.detail})
        raise SystemExit(2)
    rules.close()
    handoff.close()
    _dump(result)


def cmd_confirm_plan(args) -> None:
    rules, handoff = _open(args.data)
    try:
        result = handoff.confirm_handoff_list(args.plan, handoff_id=args.handoff)
    except RejectError as exc:
        rules.close()
        handoff.close()
        _dump({"accepted": False, "code": exc.code, "detail": exc.detail})
        raise SystemExit(2)
    rules.close()
    handoff.close()
    _dump({"accepted": True, **result})


def cmd_staleness(args) -> None:
    rules, handoff = _open(args.data)
    result = {"plan_id": args.plan, "stale_reasons": handoff.plan_staleness(args.plan)}
    rules.close()
    handoff.close()
    _dump(result)


def cmd_status(args) -> None:
    rules, handoff = _open(args.data)
    out = {
        "catalog_revision": rules.catalog_revision,
        "source_revisions": rules.source_revisions(),
        "custody": {
            case: handoff.current_custodian(case) for case in handoff.known_cases()
        },
        "handoffs": handoff.list_handoffs(),
    }
    rules.close()
    handoff.close()
    _dump(out)


def cmd_revise_hold(args) -> None:
    rules, _ = _open(args.data)
    hold = json.loads(args.hold)
    try:
        result = rules.import_holds([hold])
    except RejectError as exc:
        rules.close()
        _dump({"accepted": False, "code": exc.code, "detail": exc.detail})
        raise SystemExit(2)
    rules.close()
    _dump({"accepted": True, **result})


def cmd_ingest(args) -> None:
    """Import extra rules/documents/events/holds from a JSON file.

    The file is either a materials-style object or a bare list selected by
    ``--kind``.  Batches are atomic: one bad row rejects the whole batch.
    """
    rules, handoff = _open(args.data)
    with open(args.file, encoding="utf-8") as fh:
        payload = json.load(fh)
    try:
        if args.kind == "rules":
            rows = payload["rules"] if isinstance(payload, dict) else payload
            result = rules.import_rules(rows)
        elif args.kind == "documents":
            rows = payload["documents"] if isinstance(payload, dict) else payload
            result = rules.import_documents(rows)
            handoff.sync_catalog_membership(rows)
        elif args.kind == "events":
            rows = payload.get("events", []) if isinstance(payload, dict) else payload
            result = rules.import_events(rows)
        elif args.kind == "holds":
            rows = payload.get("holds", []) if isinstance(payload, dict) else payload
            result = rules.import_holds(rows)
        elif args.kind == "materials":
            result = import_materials(rules, handoff, args.file if os.path.isdir(args.file)
                                      else os.path.dirname(os.path.abspath(args.file)))
        else:
            raise SystemExit(f"unknown --kind {args.kind}")
    except RejectError as exc:
        rules.close()
        handoff.close()
        _dump({"accepted": False, "code": exc.code, "detail": exc.detail})
        raise SystemExit(2)
    rules.close()
    handoff.close()
    _dump({"accepted": True, **result})


def cmd_verify(args) -> None:
    rules, handoff = _open(args.data, live=False)
    report = handoff.verify()
    rules_report = rules.verify()
    report["rules"] = rules_report
    report["source_revisions"] = rules.source_revisions()
    report["products"] = rules_report["products"]
    if not rules_report["ok"]:
        report["ok"] = False
        report["errors"].extend(rules_report["errors"])
    rules.close()
    handoff.close()
    _dump(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_demo(args) -> None:
    """Run the documented 751 scenario end to end, writing artifacts."""
    from .scenario import run_scenario

    result = run_scenario(args.data, materials=args.materials)
    _dump(result)
    if not result.get("ok"):
        raise SystemExit(1)


def cmd_scale(args) -> None:
    """Generate and validate the scaling dataset (200/10/500/100)."""
    from .scenario import generate_scale, run_scale

    materials_dir = os.path.join(args.data, "materials")
    stats = generate_scale(materials_dir)
    result = run_scale(args.data, materials_dir)
    result["generated"] = stats
    _dump(result)
    if not result.get("ok"):
        raise SystemExit(1)


def cmd_operations(args) -> None:
    """Replay a materials directory's operations.jsonl end to end."""
    from .scenario import run_operations

    result = run_operations(args.data, materials=args.materials)
    _dump(result)
    if not result.get("ok"):
        raise SystemExit(1)


def cmd_requests(args) -> None:
    """Submit a JSONL file of fully-specified handoff requests.

    Each line is a complete request (handoff_id, case_id, doc_ids, from, to,
    as_of_day[, qualification_product_id]).  Every outcome is reported; the
    process exits non-zero only if any request was rejected, unless
    ``--allow-rejects`` is given.
    """
    rules, handoff = _open(args.data)
    outcomes = []
    rejected = 0
    with open(args.file, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            request = json.loads(line)
            rid = request.get("handoff_id", f"line{lineno}")
            try:
                result = handoff.submit(request)
                outcomes.append({"handoff_id": rid, "outcome": "accepted",
                                 "replayed": result["replayed"]})
            except RejectError as exc:
                rejected += 1
                outcomes.append({"handoff_id": rid, "outcome": "rejected",
                                 "code": exc.code, "detail": exc.detail})
    rules.close()
    handoff.close()
    _dump({"submitted": len(outcomes), "rejected": rejected, "outcomes": outcomes})
    if rejected and not args.allow_rejects:
        raise SystemExit(2)


def cmd_crossref_ingest(args) -> None:
    """Receive a catalog revision batch (entries / redirects / citations)."""
    catalog = CrossrefCatalog(_crossref_path(args.data))
    with open(args.file, encoding="utf-8") as fh:
        payload = json.load(fh)
    rows = payload[args.kind] if isinstance(payload, dict) else payload
    try:
        if args.kind == "entries":
            result = catalog.import_entries(rows)
        elif args.kind == "redirects":
            result = catalog.import_redirects(rows)
        else:
            result = catalog.import_citations(rows)
    except CrossrefReject as exc:
        catalog.close()
        _dump({"accepted": False, "code": exc.code, "detail": exc.detail})
        raise SystemExit(2)
    catalog.close()
    _dump({"accepted": True, "kind": args.kind, **result})


def cmd_crossref_publish(args) -> None:
    """Freeze the current catalog as an immutable, restorable edition."""
    catalog = CrossrefCatalog(_crossref_path(args.data))
    try:
        saved = catalog.publish_snapshot(args.edition)
        path = catalog.write_snapshot_artifact(
            args.edition, os.path.join(args.data, "crossref_editions")
        )
        snapshot = catalog.load_snapshot(args.edition)
    except CrossrefReject as exc:
        catalog.close()
        _dump({"accepted": False, "code": exc.code, "detail": exc.detail})
        raise SystemExit(2)
    catalog.close()
    _dump({
        "edition": args.edition,
        "saved": saved,
        "artifact": path,
        "entry_count": len(snapshot.entries),
        "citation_count": len(snapshot.citations),
        "problems": snapshot.problems,
    })


def _load_habits(args) -> dict:
    if args.habits:
        return json.loads(args.habits)
    if args.habits_file:
        with open(args.habits_file, encoding="utf-8") as fh:
            return json.load(fh)
    raise SystemExit("provide --habits JSON or --habits-file PATH")


def cmd_crossref_read(args) -> None:
    """Exact reading-desk probabilities for one frozen edition.

    With ``--snapshot-file`` the analysis runs entirely offline: no catalog
    database is opened, only the self-contained edition JSON.
    """
    habits = _load_habits(args)
    try:
        if args.snapshot_file:
            desk = ReadingDesk.from_file(args.snapshot_file, habits)
        else:
            catalog = CrossrefCatalog(_crossref_path(args.data))
            desk = ReadingDesk(catalog.load_snapshot(args.edition), habits)
            catalog.close()
        outcome = desk.analyze_all(args.target)
    except CrossrefReject as exc:
        _dump({"accepted": False, "code": exc.code, "detail": exc.detail})
        raise SystemExit(2)
    payload = outcome.as_dict()
    if args.entry:
        payload["headline"] = outcome.for_entry(args.entry).as_dict()
    _dump(payload)


def cmd_crossref_status(args) -> None:
    catalog = CrossrefCatalog(_crossref_path(args.data))
    _dump({
        "catalog_revision": catalog.catalog_revision,
        "snapshots": catalog.list_snapshots(),
        "open_problems": [
            {
                "citation_id": r.citation.citation_id,
                "entry_id": r.citation.entry_id,
                "status": r.status,
                "problem": r.problem,
            }
            for r in catalog.resolve_all() if r.status != "ok"
        ],
    })
    catalog.close()


def cmd_crossref_verify(args) -> None:
    catalog = CrossrefCatalog(_crossref_path(args.data))
    report = catalog.verify()
    catalog.close()
    _dump(report)
    if not report["ok"]:
        raise SystemExit(1)


def cmd_crossref_demo(args) -> None:
    """Replay the documented cross-reference story (editions, broken links)."""
    from .crossref_scenario import run_crossref_demo, assert_demo_expectations

    result = run_crossref_demo(args.data)
    try:
        assert_demo_expectations(result)
    except AssertionError as exc:
        result["ok"] = False
        result["assertion_error"] = str(exc)
    _dump(result)
    if not result.get("ok"):
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="archive_handoff")
    p.add_argument("--data", default="artifacts/demo", help="data directory")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("import-materials")
    sp.add_argument("--materials", required=True)
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("publish")
    sp.add_argument("--day", type=int, required=True)
    sp.add_argument("--product", required=True)
    sp.add_argument("--doc", action="append", help="limit to these doc_ids")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("submit")
    sp.add_argument("--request")
    sp.add_argument("--request-file")
    sp.set_defaults(func=cmd_submit)

    sp = sub.add_parser("prepare")
    sp.add_argument("--request")
    sp.add_argument("--request-file")
    sp.set_defaults(func=cmd_prepare)

    sp = sub.add_parser("confirm")
    sp.add_argument("--proposal", required=True)
    sp.add_argument("--handoff")
    sp.set_defaults(func=cmd_confirm)

    sp = sub.add_parser("plan")
    sp.add_argument("--plan", required=True)
    sp.add_argument("--product", required=True)
    sp.add_argument("--day", type=int, required=True)
    sp.add_argument("--from-party", default="LOCAL")
    sp.add_argument("--to-party", default="VAULT1")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("confirm-plan")
    sp.add_argument("--plan", required=True)
    sp.add_argument("--handoff")
    sp.set_defaults(func=cmd_confirm_plan)

    sp = sub.add_parser("staleness")
    sp.add_argument("--plan", required=True)
    sp.set_defaults(func=cmd_staleness)

    sp = sub.add_parser("status")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("revise-hold")
    sp.add_argument("--hold", required=True)
    sp.set_defaults(func=cmd_revise_hold)

    sp = sub.add_parser("ingest")
    sp.add_argument("--kind", required=True,
                    choices=["rules", "documents", "events", "holds", "materials"])
    sp.add_argument("--file", required=True)
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("verify")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("demo")
    sp.add_argument("--materials", default="materials/751")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("scale")
    sp.set_defaults(func=cmd_scale)

    sp = sub.add_parser("operations")
    sp.add_argument("--materials", default="materials/751")
    sp.set_defaults(func=cmd_operations)

    sp = sub.add_parser("requests")
    sp.add_argument("--file", required=True)
    sp.add_argument("--allow-rejects", action="store_true")
    sp.set_defaults(func=cmd_requests)

    # ------------------------------------------- cross-reference catalog / desk
    sp = sub.add_parser("crossref-ingest",
                        help="receive a catalog revision batch (entries/redirects/citations)")
    sp.add_argument("--kind", required=True, choices=["entries", "redirects", "citations"])
    sp.add_argument("--file", required=True)
    sp.set_defaults(func=cmd_crossref_ingest)

    sp = sub.add_parser("crossref-publish",
                        help="freeze the current catalog as a restorable edition")
    sp.add_argument("--edition", required=True)
    sp.set_defaults(func=cmd_crossref_publish)

    sp = sub.add_parser("crossref-read",
                        help="exact reading-desk probabilities for a frozen edition")
    sp.add_argument("--edition", help="edition id in the catalog store")
    sp.add_argument("--snapshot-file",
                    help="self-contained edition JSON; skips the catalog database entirely")
    sp.add_argument("--target", required=True, help="absorbing original-document entry id")
    sp.add_argument("--entry", help="headline entry to surface first")
    sp.add_argument("--habits", help='inline habit table JSON, e.g. {"direct":1}')
    sp.add_argument("--habits-file", help="JSON file with the reading desk's habit table")
    sp.set_defaults(func=cmd_crossref_read)

    sp = sub.add_parser("crossref-status")
    sp.set_defaults(func=cmd_crossref_status)

    sp = sub.add_parser("crossref-verify")
    sp.set_defaults(func=cmd_crossref_verify)

    sp = sub.add_parser("crossref-demo",
                        help="replay the documented see-also story incl. broken links")
    sp.set_defaults(func=cmd_crossref_demo)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
