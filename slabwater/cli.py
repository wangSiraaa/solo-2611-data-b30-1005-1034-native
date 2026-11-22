"""Command-line driver for the synthetic catalog/publisher.

Examples::

    python -m slabwater.cli run materials/772/input.json
    python -m slabwater.cli verify <db_dir> <plan_id>
    python -m slabwater.cli reopen <db_dir> <plan_id>

The ``run`` subcommand builds the catalog from the material's tile batches,
processes every request and prints a JSON report.  It is a demonstration
harness, not a service.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .lakes import (
    OldName,
    build_ladder,
    depth_product_from_plan,
    kinship_report,
    ladder_report,
    migrate,
)
from .publisher import Publisher


def run_material(path: str | Path, db_dir: str | Path | None = None) -> dict[str, Any]:
    material = json.loads(Path(path).read_text(encoding="utf-8"))
    db_dir = Path(db_dir) if db_dir else Path(tempfile.mkdtemp(prefix="slabwater-"))
    db_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(db_dir / "catalog.db", material["height"], material["width"], material["ceiling"])
    publisher = Publisher(db_dir / "plans.db", catalog)
    report: dict[str, Any] = {"db_dir": str(db_dir), "batches": [], "requests": []}
    try:
        initial = catalog.apply_batch(material["initial_tiles"])
        report["batches"].append(
            {"name": "initial", "batch": initial.to_dict(), "version": initial.version}
        )
        if "correction" in material:
            correction = catalog.apply_batch([material["correction"]])
            report["batches"].append(
                {"name": "correction", "batch": correction.to_dict(), "version": correction.version}
            )
        if "invalid_correction" in material:
            invalid = catalog.apply_batch([material["invalid_correction"]])
            report["batches"].append(
                {
                    "name": "invalid_correction",
                    "batch": invalid.to_dict(),
                    "version": invalid.version,
                }
            )
        report["versions"] = catalog.list_versions()
        for request in material["requests"]:
            result = publisher.publish(request, default_outlets=material.get("outlets"))
            entry: dict[str, Any] = {
                "request": request,
                "status": result.status,
                "replayed": result.replayed,
                "plan_id": result.plan_id,
                "artifact": result.artifact,
            }
            if result.status == "rejected":
                stored = publisher.outcome_for_request(request["id"])
                entry["evidence"] = stored["payload"]["evidence"]
            if result.status == "error":
                entry["error"] = publisher.outcome_for_request(request["id"])["payload"]
            report["requests"].append(entry)
        # Reject-on-invalid surface check: the invalid correction must not have
        # produced a version; list_versions above is the evidence.
        report["plans"] = publisher.list_plans()
    finally:
        publisher.close()
        catalog.close()
    return report


def run_lakes(
    path: str | Path,
    db_dir: str | Path | None = None,
    max_names: int | None = None,
) -> dict[str, Any]:
    """Publish closed-basin depth products and migrate old lake names.

    The old and new editions are each published as a signed plan with no opened
    outlet (closed basin, level = ceiling).  Their per-cell water depth
    (``storage``) is the *only* naming input; heights and total storage are never
    consulted for naming.
    """

    material = json.loads(Path(path).read_text(encoding="utf-8"))
    db_dir = Path(db_dir) if db_dir else Path(tempfile.mkdtemp(prefix="slabwater-lakes-"))
    db_dir.mkdir(parents=True, exist_ok=True)
    height = material["height"]
    width = material["width"]
    ceiling = material["ceiling"]
    catalog = Catalog(db_dir / "catalog.db", height, width, ceiling)
    publisher = Publisher(db_dir / "plans.db", catalog)
    report: dict[str, Any] = {"db_dir": str(db_dir), "editions": {}, "batches": []}
    try:
        batches = {
            "old": material["old"]["tiles"],
            "new": material["new"]["tiles"],
        }
        versions: dict[str, int] = {}
        for label in ("old", "new"):
            result = catalog.apply_batch(batches[label])
            report["batches"].append(
                {"edition": label, "ok": result.ok, "version": result.version}
            )
            if not result.ok:
                raise ValueError(f"{label} batch rejected: {[e.to_dict() for e in result.errors]}")
            assert result.version is not None
            versions[label] = result.version
        products = {}
        for label in ("old", "new"):
            version = versions[label]
            request = {
                "id": f"depth-{label}-v{version}",
                "source_revision": version,
                "opens": 0,
                "total_max": 10**12,
                "cell_max": [],
                "outlets": [],
            }
            published = publisher.publish(request)
            if not published.ok:
                raise ValueError(f"{label} depth product rejected: {published.status}")
            product = depth_product_from_plan(publisher, published.plan_id)
            products[label] = product
            report["editions"][label] = {
                "version": product.version,
                "elevation_hash": product.elevation_hash,
                "depth": [list(row) for row in product.depths],
            }
        thresholds = material["thresholds"]
        old_ladder = build_ladder(products["old"], thresholds)
        new_ladder = build_ladder(products["new"], thresholds)
        names = [OldName.from_dict(raw) for raw in material["old_names"]]
        cap = max_names if max_names is not None else material.get("max_names")
        result = migrate(old_ladder, new_ladder, names, cap)
        report["old_ladder"] = ladder_report(old_ladder)
        report["new_ladder"] = ladder_report(new_ladder)
        report["migration"] = result.to_dict()
        report["migration"]["kinship"] = kinship_report(result, new_ladder)
    finally:
        publisher.close()
        catalog.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slabwater", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="run a material input file end to end")
    run_parser.add_argument("input", help="path to input.json")
    run_parser.add_argument("--db-dir", default=None, help="directory for catalog.db/plans.db")

    lakes_parser = sub.add_parser("lakes", help="publish depth products and migrate lake names")
    lakes_parser.add_argument("input", help="path to a lakes material file")
    lakes_parser.add_argument("--db-dir", default=None, help="directory for catalog.db/plans.db")
    lakes_parser.add_argument(
        "--max-names", type=int, default=None,
        help="override the material's max retained name count",
    )

    verify_parser = sub.add_parser("verify", help="verify a stored plan")
    verify_parser.add_argument("db_dir")
    verify_parser.add_argument("plan_id")

    reopen_parser = sub.add_parser("reopen", help="re-open a stored plan artifact")
    reopen_parser.add_argument("db_dir")
    reopen_parser.add_argument("plan_id")

    args = parser.parse_args(argv)
    if args.command == "run":
        report = run_material(args.input, args.db_dir)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    if args.command == "lakes":
        report = run_lakes(args.input, args.db_dir, max_names=args.max_names)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    db_dir = Path(args.db_dir)
    catalog = Catalog.open(db_dir / "catalog.db")
    publisher = Publisher(db_dir / "plans.db", catalog)
    try:
        if args.command == "verify":
            result = publisher.verify(args.plan_id).to_dict()
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["ok"] else 1
        reopened = publisher.reopen(args.plan_id)
        print(json.dumps(reopened.to_dict(), indent=2, ensure_ascii=False))
        return 0
    finally:
        publisher.close()
        catalog.close()


if __name__ == "__main__":
    sys.exit(main())
