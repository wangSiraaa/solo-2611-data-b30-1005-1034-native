"""End-to-end foam-mold demonstration driver.

Runs the closed 1x3 and multi-edge 1x3 materials (self-contained catalog +
publisher + mold shop) and the 772 material's mold on top of its real published
plan.  Like :mod:`slabwater.cli`, this is a demonstration harness, not a
service; plain quotes never peel a positioning sticker, only a confirmed work
order does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .molding import (
    build_spec,
    optimize,
    review_grid,
    target_from_plan,
)
from .moldshop import MoldShop
from .publisher import Publisher

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> dict[str, Any]:
    return json.loads((ROOT / "materials" / name / "input.json").read_text(encoding="utf-8"))


def run_closed(shop_db_dir: Path) -> dict[str, Any]:
    material = _load("closed1x3")
    catalog = Catalog(shop_db_dir / "catalog.db", material["height"], material["width"],
                      material["ceiling"])
    publisher = Publisher(shop_db_dir / "plans.db", catalog)
    shop = MoldShop(shop_db_dir / "shop.db")
    report: dict[str, Any] = {}
    try:
        catalog.apply_batch(material["tiles_v1"])
        raised = catalog.apply_batch(material["tiles_v2_raised"])
        report["raised_version"] = raised.version
        plan_v1 = publisher.publish(material["requests"][0], default_outlets=[]).artifact
        plan_v2 = publisher.publish(material["requests"][1], default_outlets=[]).artifact
        mold = material["mold"]
        # --- baseline: holder fits two knife types, sticker available ------
        target1 = target_from_plan(plan_v1, mold["blank_height"])
        spec1 = build_spec(target1, mold["tools"], mold["strokes"], mold["rack_capacity"])
        shop.sync_inventory(spec1)
        base = optimize(spec1)
        # --- raised middle cell --------------------------------------------
        target2 = target_from_plan(plan_v2, mold["blank_height"])
        spec2 = build_spec(target2, mold["tools"], mold["strokes"], mold["rack_capacity"])
        raised_report = optimize(spec2)
        # --- rack limited to a single knife type ---------------------------
        spec1_rack1 = build_spec(target1, mold["tools"], mold["strokes"], rack_capacity=1)
        rack1 = optimize(spec1_rack1)
        # --- same opening conditions, but keep the W sticker ---------------
        reserved = optimize(spec1, forbidden_stickers=["S-W"])
        report.update(
            {
                "plan_v1": plan_v1["plan_id"],
                "storage_v1": plan_v1["storage"],
                "target_v1": [list(row) for row in target1.target],
                "storage_v2": plan_v2["storage"],
                "target_v2": [list(row) for row in target2.target],
                "baseline": base.to_dict(),
                "baseline_review": review_grid(spec1, base.best),
                "raised": raised_report.to_dict(),
                "rack_capacity_one": rack1.to_dict(),
                "reserve_w_sticker": reserved.to_dict(),
            }
        )
        # Physically confirm both alternatives against separate sticker states.
        order_cheap = shop.confirm("closed-baseline", spec1)
        order_no_sticker = shop.confirm("closed-reserved", spec1, reserve=["S-W"])
        report["confirmed_orders"] = shop.list_orders()
        report["sticker_after"] = shop.list_stickers()
        report["recompute_cheap"] = shop.recompute_order(order_cheap.order_id)
        report["recompute_no_sticker"] = shop.recompute_order(order_no_sticker.order_id)
    finally:
        shop.close()
        publisher.close()
        catalog.close()
    return report


def run_multiedge(shop_db_dir: Path) -> dict[str, Any]:
    material = _load("multiedge1x3")
    catalog = Catalog(shop_db_dir / "catalog.db", material["height"], material["width"],
                      material["ceiling"])
    publisher = Publisher(shop_db_dir / "plans.db", catalog)
    shop = MoldShop(shop_db_dir / "shop.db")
    try:
        catalog.apply_batch(material["tiles"])
        plan = publisher.publish(material["requests"][0], default_outlets=[]).artifact
        mold = material["mold"]
        target = target_from_plan(plan, mold["blank_height"])
        spec = build_spec(target, mold["tools"], mold["strokes"], mold["rack_capacity"])
        shop.sync_inventory(spec)
        from .molding import analyze_stroke

        z = analyze_stroke(spec, "Z-0")
        search = optimize(spec)
        order = shop.confirm("multiedge", spec)
        return {
            "plan_id": plan["plan_id"],
            "storage": plan["storage"],
            "target": [list(row) for row in target.target],
            "z_stroke": z.to_dict(spec.target.height, spec.target.width),
            "optimize": search.to_dict(),
            "order": order.to_dict(),
            "sticker_after": shop.list_stickers(),
        }
    finally:
        shop.close()
        publisher.close()
        catalog.close()


def run_772(shop_db_dir: Path) -> dict[str, Any]:
    from .cli import run_material

    material_path = ROOT / "materials" / "772" / "input.json"
    run = run_material(material_path, shop_db_dir / "water")
    catalog = Catalog.open(shop_db_dir / "water" / "catalog.db")
    publisher = Publisher(shop_db_dir / "water" / "plans.db", catalog)
    shop = MoldShop(shop_db_dir / "shop.db")
    try:
        mold_cfg = json.loads((ROOT / "materials" / "772" / "mold.json").read_text(encoding="utf-8"))
        artifact = publisher.plan_for_request(mold_cfg["plan_request_id"])
        # Independent verification of the real published plan before molding.
        verification = publisher.verify(artifact["plan_id"]).to_dict()
        target = target_from_plan(artifact, mold_cfg["blank_height"])
        spec = build_spec(
            target, mold_cfg["tools"], mold_cfg["strokes"], mold_cfg["rack_capacity"]
        )
        shop.sync_inventory(spec)
        from .molding import analyze_stroke

        analyses = {sid: analyze_stroke(spec, sid).to_dict(spec.target.height, spec.target.width)
                    for sid in [s["id"] for s in mold_cfg["strokes"]]}
        cheapest = optimize(spec)
        quote = shop.quote("772-trial", spec)  # trial: must not peel anything
        order = shop.confirm("772-order", spec)
        later_blocked = shop.quote("772-later", spec)  # S-W now peeled
        return {
            "water_run_requests": [
                {"id": item["request"]["id"], "status": item["status"], "plan_id": item["plan_id"]}
                for item in run["requests"]
            ],
            "verification": {"ok": verification["ok"], "checks": verification["checks"]},
            "target": [list(row) for row in target.target],
            "stroke_analyses": analyses,
            "cheapest": cheapest.to_dict(),
            "trial_quote_consumed": quote.to_dict(),
            "confirmed_order": order.to_dict(),
            "later_quote_after_peel": later_blocked.to_dict(),
            "sticker_after": shop.list_stickers(),
            "recompute": shop.recompute_order(order.order_id),
        }
    finally:
        shop.close()
        publisher.close()
        catalog.close()


def main() -> int:
    import tempfile

    out: dict[str, Any] = {}
    for name, runner in (
        ("closed1x3", run_closed),
        ("multiedge1x3", run_multiedge),
        ("772", run_772),
    ):
        with tempfile.TemporaryDirectory(prefix=f"mold-{name}-") as tmp:
            out[name] = runner(Path(tmp))
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
