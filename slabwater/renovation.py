"""Catalog-backed renovation: align two frozen plates, diff them, move sand.

The old plate and the target plate were each installed through the real,
unmodified :class:`Catalog.apply_batch`, but under different coordinate origins.
They therefore live in their own catalogs / frozen versions; nothing here
subtracts their arrays index-for-index.

Workflow
--------
1. submit the old tiles and the target tiles through two real catalogs;
2. align the frozen slices with their own coordinates and alignment tickets
   (:func:`slabwater.align.align_slices`);
3. hand only an *unambiguous* complete difference map to the workshop, which
   prices the cart routes (:func:`slabwater.transport.plan_transport`).

If the tickets leave more than one distinct difference map, the scenario is
``ambiguous``: every actual map and the disagreement cells are returned and no
single map is forwarded -- the workshop never gets to pick a cheapest one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .align import AlignmentError, align_slices
from .catalog import Catalog
from .model import Slice
from .transport import TransportError, plan_transport


class RenovationError(ValueError):
    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def _open_plate(
    db_path: str | Path,
    height: int,
    width: int,
    ceiling: int,
    tiles: Sequence[Mapping[str, Any]],
    label: str,
) -> tuple[Catalog, Slice]:
    catalog = Catalog(db_path, height, width, ceiling)
    result = catalog.apply_batch(list(tiles))
    if not result.ok:
        catalog.close()
        raise RenovationError(
            "batch_rejected",
            f"{label} batch was rejected by the catalog",
            {"label": label, "errors": [error.to_dict() for error in result.errors]},
        )
    assert result.version is not None
    return catalog, catalog.get_slice(result.version)


def _resolve(source: Mapping[str, Any], scenario: Mapping[str, Any], key: str, default: Any) -> Any:
    return scenario[key] if key in scenario else source.get(key, default)


def analyze(
    old: Slice,
    target: Slice,
    old_markers: Mapping[str, Any],
    target_markers: Mapping[str, Any],
    scenarios: Sequence[Mapping[str, Any]],
    defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Align, diff and price every scenario against the two frozen slices."""

    defaults = dict(defaults or {})
    report: dict[str, Any] = {
        "old": {
            "version": old.version,
            "height": old.height,
            "width": old.width,
            "elevation_hash": old.elevation_hash,
            "heights": [list(row) for row in old.heights],
        },
        "target": {
            "version": target.version,
            "height": target.height,
            "width": target.width,
            "elevation_hash": target.elevation_hash,
            "heights": [list(row) for row in target.heights],
        },
        "scenarios": [],
    }
    for scenario in scenarios:
        scenario_id = scenario.get("id", "scenario")
        tickets = _resolve(defaults, scenario, "tickets", [])
        correction = _resolve(defaults, scenario, "correction", 0)
        lanes = _resolve(defaults, scenario, "lanes", [])
        outsource_price = _resolve(defaults, scenario, "outsource_price", None)
        buy_price = _resolve(defaults, scenario, "buy_price", None)
        entry: dict[str, Any] = {"id": scenario_id, "tickets": list(tickets)}
        report["scenarios"].append(entry)
        try:
            alignment = align_slices(
                old,
                target,
                old_markers=old_markers,
                target_markers=target_markers,
                tickets=tickets,
                correction=correction,
            )
        except AlignmentError as exc:
            entry.update({"status": "malformed", "error": exc.to_dict()})
            continue
        entry["status"] = alignment.status
        entry["correction"] = alignment.correction
        entry["alignments"] = [transform.to_dict() for transform in alignment.alignments]
        entry["alignment_count"] = len(alignment.alignments)
        if alignment.status != "aligned":
            # Do not silently choose a map: hand back every actual difference map
            # and the cells where they disagree.
            entry["reason"] = alignment.reason
            entry["maps"] = [difference.to_dict() for difference in alignment.maps]
            entry["disagreement_cells"] = [
                [cell[0], cell[1]] for cell in alignment.disagreement_cells
            ]
            continue
        difference = alignment.maps[0]
        entry["difference"] = difference.to_dict()
        if outsource_price is None and buy_price is None and not lanes:
            # Nothing to price: still return the difference map for a dry run.
            entry["transport"] = None
            continue
        try:
            transport = plan_transport(
                difference.delta,
                lanes,
                outsource_price=outsource_price,
                buy_price=buy_price,
            )
        except TransportError as exc:
            entry["status"] = "transport_error"
            entry["error"] = exc.to_dict()
            continue
        entry["transport"] = transport.to_dict()
    return report


def run_material(material: Mapping[str, Any], db_dir: str | Path) -> dict[str, Any]:
    """End-to-end renovation run from a material document (see materials/)."""

    db_dir = Path(db_dir)
    db_dir.mkdir(parents=True, exist_ok=True)
    height = material["height"]
    width = material["width"]
    ceiling = material["ceiling"]
    old_catalog, old = _open_plate(
        db_dir / "old.db", height, width, ceiling, material["old_tiles"], "old"
    )
    try:
        target_catalog, target = _open_plate(
            db_dir / "target.db", height, width, ceiling, material["target_tiles"], "target"
        )
        try:
            report = analyze(
                old,
                target,
                old_markers=material.get("old_markers", {}),
                target_markers=material.get("target_markers", {}),
                scenarios=material.get("scenarios", [{"id": "default"}]),
                defaults={
                    "tickets": material.get("tickets", []),
                    "correction": material.get("vertical_correction", 0),
                    "lanes": material.get("lanes", []),
                    "outsource_price": (material.get("price") or {}).get("outsource"),
                    "buy_price": (material.get("price") or {}).get("buy"),
                },
            )
            report["versions"] = {
                "old": old_catalog.list_versions(),
                "target": target_catalog.list_versions(),
            }
        finally:
            target_catalog.close()
        return report
    finally:
        old_catalog.close()


def run_material_file(path: str | Path, db_dir: str | Path) -> dict[str, Any]:
    material = json.loads(Path(path).read_text(encoding="utf-8"))
    return run_material(material, db_dir)
