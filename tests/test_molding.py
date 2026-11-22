"""Tests for the foam negative-mold layer built on published slabwater plans.

Covers the rule set in the task statement:

* the target grid is derived from a *published* plan's real grid/outlets/
  elevation revision, never from a theoretical knife depth;
* minimum safe holder height, per-cell cut-to thickness, exact-reach cells and
  the cell that limits a whole stroke;
* one cell grazed by several edges (different lifts) takes the lowest edge, and
  one cell covered by several strokes takes the minimum thickness -- depths
  never stack, overcutting is impossible at a minimum-safe height;
* mount fee once per knife, per-stroke labor, rack capacity, one physical
  positioning sticker per sticker group, <= 18 strokes / <= 6 knives;
* quotes do not peel stickers, confirmation does, later orders cannot reuse a
  peeled sticker, and a frozen order still recomputes after the catalog moves.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from slabwater import (
    Catalog,
    Edge,
    MoldError,
    MoldShop,
    Publisher,
    StrokeSpec,
    Tool,
    analyze_stroke,
    build_spec,
    evaluate_selection,
    optimize,
    review_grid,
    target_from_plan,
)

ROOT = Path(__file__).resolve().parent.parent


def load(name: str) -> dict:
    return json.loads((ROOT / "materials" / name / "input.json").read_text(encoding="utf-8"))


class ClosedMoldBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.material = load("closed1x3")
        self.catalog = Catalog(
            self.dir / "catalog.db",
            self.material["height"],
            self.material["width"],
            self.material["ceiling"],
        )
        self.publisher = Publisher(self.dir / "plans.db", self.catalog)
        self.shop = MoldShop(self.dir / "shop.db")
        self.catalog.apply_batch(self.material["tiles_v1"])
        self.catalog.apply_batch(self.material["tiles_v2_raised"])
        self.plan_v1 = self.publisher.publish(
            self.material["requests"][0], default_outlets=[]
        ).artifact
        self.plan_v2 = self.publisher.publish(
            self.material["requests"][1], default_outlets=[]
        ).artifact
        self.cfg = self.material["mold"]

    def tearDown(self) -> None:
        self.shop.close()
        self.publisher.close()
        self.catalog.close()
        self.tmp.cleanup()

    def spec(self, plan, rack: int):
        target = target_from_plan(plan, self.cfg["blank_height"])
        return build_spec(target, self.cfg["tools"], self.cfg["strokes"], rack)


class TargetDerivationTests(ClosedMoldBase):
    def test_target_comes_from_the_real_published_plan(self):
        target = target_from_plan(self.plan_v1, blank_height=2)
        # Closed ceiling C=2, no outlets: storage is C minus each cell height.
        self.assertEqual(self.plan_v1["opened"], [])
        self.assertEqual([list(r) for r in target.storage], [[2, 2, 1]])
        self.assertEqual([list(r) for r in target.target], [[0, 0, 1]])
        self.assertEqual(target.plan_id, self.plan_v1["plan_id"])
        self.assertEqual(target.elevation_hash, self.plan_v1["elevation_hash"])
        self.assertEqual(target.catalog_version, self.plan_v1["catalog_version"])

    def test_blank_height_must_cover_max_depth(self):
        with self.assertRaises(MoldError):
            target_from_plan(self.plan_v1, blank_height=1)  # max storage is 2

    def test_raised_revision_targets(self):
        target = target_from_plan(self.plan_v2, blank_height=2)
        self.assertEqual([list(r) for r in target.storage], [[2, 1, 1]])
        self.assertEqual([list(r) for r in target.target], [[0, 1, 1]])


class StrokeSafetyTests(ClosedMoldBase):
    def test_wide_plane_min_safe_height_and_limiting_cell(self):
        spec = self.spec(self.plan_v1, rack=2)
        w = analyze_stroke(spec, "W-01")
        # Target [0,0,1]; both edges lift 0, holder at col 0 -> cells 0,1.
        self.assertEqual(w.min_safe_height, 0)
        self.assertEqual(w.cut_to[(0, 0)], 0)
        self.assertEqual(w.cut_to[(0, 1)], 0)
        self.assertEqual(w.cut_depth[(0, 0)], 2)
        self.assertEqual(w.exact_cells, frozenset({(0, 0), (0, 1)}))
        self.assertEqual(w.short_cells, frozenset())
        self.assertEqual(w.limiting_cells, ((0, 0), (0, 1)))

    def test_raised_cell_lifts_the_whole_wide_stroke(self):
        spec = self.spec(self.plan_v2, rack=2)  # target [0,1,1]
        w = analyze_stroke(spec, "W-01")
        # Cell 1 now needs thickness 1, so the holder must rise to 1; cell 0 is
        # then left at 1 instead of its target 0 -> the wide plane cannot finish.
        self.assertEqual(w.min_safe_height, 1)
        self.assertEqual(w.limiting_cells, ((0, 1),))
        self.assertEqual(w.exact_cells, frozenset({(0, 1)}))
        self.assertEqual(w.short_cells, frozenset({(0, 0)}))

    def test_point_strokes_reach_single_cells(self):
        spec = self.spec(self.plan_v1, rack=2)
        p3 = analyze_stroke(spec, "P3")
        self.assertEqual(p3.min_safe_height, 1)
        self.assertEqual(p3.exact_cells, frozenset({(0, 2)}))
        self.assertEqual(p3.cut_to[(0, 2)], 1)
        self.assertEqual(p3.cut_depth[(0, 2)], 1)

    def test_edge_falling_outside_board_is_rejected(self):
        target = target_from_plan(self.plan_v1, blank_height=2)
        bad_stroke = StrokeSpec(
            id="W-EDGE", tool_id="W", path=((0, 2),), labor_fee=1, sticker="S-W"
        )
        with self.assertRaises(MoldError):
            build_spec(target, self.cfg["tools"], [bad_stroke], rack_capacity=2)

    def test_non_adjacent_holder_path_is_rejected(self):
        target = target_from_plan(self.plan_v1, blank_height=2)
        # A holder path must walk four-neighbour adjacent positions; skipping
        # col 1 is not an approved contiguous stroke, and this is rejected at
        # StrokeSpec construction regardless of how it is built.
        with self.assertRaises(MoldError):
            StrokeSpec(
                id="JUMP", tool_id="P", path=((0, 0), (0, 2)), labor_fee=1
            )
        # The same cells with the intermediate holder position are accepted.
        contiguous = StrokeSpec(
            id="WALK", tool_id="P", path=((0, 0), (0, 1), (0, 2)), labor_fee=1
        )
        ok_spec = build_spec(target, self.cfg["tools"], [contiguous], rack_capacity=2)
        self.assertIn("WALK", {s.id for s in ok_spec.strokes})

    def test_lift_above_target_cannot_reach_and_never_overcuts(self):
        # A knife whose only edge sits 2 above the holder on a target-0 cell can
        # only reach thickness 2 at holder height 0: it must not plane at all.
        target = target_from_plan(self.plan_v1, blank_height=2)
        high = Tool(id="H", edges=(Edge(0, 0, 2),), mount_fee=0)
        stroke = StrokeSpec(id="H1", tool_id="H", path=((0, 0),), labor_fee=1)
        spec = build_spec(target, [high], [stroke], rack_capacity=1)
        analysis = analyze_stroke(spec, "H1")
        self.assertEqual(analysis.min_safe_height, 0)
        self.assertEqual(analysis.cut_to[(0, 0)], 2)
        self.assertEqual(analysis.cut_depth[(0, 0)], 0)
        self.assertEqual(analysis.exact_cells, frozenset())
        self.assertEqual(analysis.short_cells, frozenset({(0, 0)}))


class OptimizationTests(ClosedMoldBase):
    def test_baseline_cheapest_is_wide_plane_plus_one_point(self):
        spec = self.spec(self.plan_v1, rack=2)
        report = optimize(spec)
        self.assertTrue(report.ok)
        self.assertEqual(report.best.stroke_ids, ("P3", "W-01"))
        self.assertEqual(report.best.total_fee, 3)  # labor 1 + 2, mounts free
        self.assertEqual(report.best.tool_ids, ("P", "W"))
        self.assertEqual([list(r) for r in report.best.result_grid], [[0, 0, 1]])
        self.assertEqual(report.best.missing_cells, frozenset())
        self.assertEqual(report.best.overcut_cells, frozenset())

    def test_raised_grid_cheapest_is_five(self):
        spec = self.spec(self.plan_v2, rack=2)
        report = optimize(spec)
        self.assertTrue(report.ok)
        self.assertEqual(report.best.stroke_ids, ("P1", "P3", "W-01"))
        self.assertEqual(report.best.total_fee, 5)
        # All-point alternative is 6.
        all_points = evaluate_selection(spec, ["P1", "P2", "P3"])
        self.assertEqual(all_points.total_fee, 6)
        self.assertTrue(all_points.feasible)

    def test_rack_capacity_one_forces_three_point_strokes(self):
        spec = self.spec(self.plan_v1, rack=1)
        report = optimize(spec)
        self.assertTrue(report.ok)
        self.assertEqual(report.best.stroke_ids, ("P1", "P2", "P3"))
        self.assertEqual(report.best.tool_ids, ("P",))
        self.assertEqual(report.best.total_fee, 6)

    def test_cover_does_not_stack_and_takes_minimum_thickness(self):
        # W-01 plus P1 both touch cell 0: result is the minimum of the two
        # cut-to values, never the sum of depths, and still exactly the target.
        spec = self.spec(self.plan_v1, rack=2)
        selection = evaluate_selection(spec, ["W-01", "P1", "P3"])
        self.assertTrue(selection.feasible)
        self.assertEqual(selection.result_grid[0][0], 0)
        review = review_grid(spec, selection)[0][0]
        self.assertEqual({c["stroke_id"] for c in review["contributors"]}, {"W-01", "P1"})
        self.assertTrue(review["matches_target"])

    def test_sticker_group_allows_at_most_one_stroke(self):
        spec = self.spec(self.plan_v1, rack=2)
        # Only one W stroke exists here, but add a second one sharing S-W.
        extra = StrokeSpec(id="W-X", tool_id="W", path=((0, 1),), labor_fee=1,
                           sticker="S-W")
        spec2 = build_spec(spec.target, spec.tools, (*spec.strokes, extra), rack_capacity=2)
        bad = evaluate_selection(spec2, ["W-01", "W-X"])
        self.assertFalse(bad.feasible)
        self.assertIn("S-W", bad.sticker_conflicts)
        # The search itself never picks two strokes of one sticker group.
        report = optimize(spec2)
        self.assertTrue(report.ok)
        chosen_stickers = [
            s.sticker for s in spec2.strokes if s.id in report.best.stroke_ids
        ]
        self.assertEqual(chosen_stickers.count("S-W"), 1)

    def test_candidate_limits_are_enforced(self):
        target = target_from_plan(self.plan_v1, blank_height=2)
        strokes = [
            StrokeSpec(id=f"P{i}", tool_id="P", path=((0, i),), labor_fee=1)
            for i in range(3)
        ]
        with self.assertRaises(MoldError):
            build_spec(target, self.cfg["tools"], strokes, rack_capacity=2,
                       max_strokes=2)
        many_tools = [
            Tool(id=f"T{i}", edges=(Edge(0, 0, 0),), mount_fee=0) for i in range(7)
        ]
        with self.assertRaises(MoldError):
            build_spec(target, many_tools, strokes[:1], rack_capacity=8, max_tools=6)


class MultiEdgeTests(unittest.TestCase):
    """The dedicated case: one cell grazed by two same-offset edges of one knife."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.material = load("multiedge1x3")
        self.catalog = Catalog(
            self.dir / "catalog.db", 1, 3, self.material["ceiling"]
        )
        self.publisher = Publisher(self.dir / "plans.db", self.catalog)
        self.catalog.apply_batch(self.material["tiles"])
        self.plan = self.publisher.publish(
            self.material["requests"][0], default_outlets=[]
        ).artifact
        cfg = self.material["mold"]
        target = target_from_plan(self.plan, cfg["blank_height"])
        self.spec = build_spec(target, cfg["tools"], cfg["strokes"], cfg["rack_capacity"])

    def tearDown(self) -> None:
        self.publisher.close()
        self.catalog.close()
        self.tmp.cleanup()

    def test_lowest_of_two_edges_decides_and_naive_height_overcuts(self):
        # Target is [1,1,1]. Knife Z at holder col 0 grazes cell 0 with a lift-0
        # edge AND a lift-2 edge, and cell 1 with one lift-0 edge.
        z = analyze_stroke(self.spec, "Z-0")
        self.assertEqual(z.min_safe_height, 1)
        # Cell 0: min(h+0, h+2) = h -> the low edge wins over the high edge.
        self.assertEqual(z.cut_to[(0, 0)], 1)
        self.assertEqual(z.cut_to[(0, 1)], 1)
        self.assertEqual(z.exact_cells, frozenset({(0, 0), (0, 1)}))
        self.assertEqual(z.limiting_cells, ((0, 0), (0, 1)))
        # The high edge alone would suggest h = target - lift = -1, clamped to
        # holder height 0; at h=0 the low edge cuts cell 0 to thickness 0,
        # which is below its target 1 -- an overcut.  Taking the minimum over
        # every grazing edge is therefore what makes h_safe=1 the only safe
        # height, and that height still reaches the target exactly.
        naive_height = max(0, self.spec.target.at((0, 0)) - 2)  # trust high edge only
        low_edge_bottom = naive_height + 0
        self.assertLess(low_edge_bottom, self.spec.target.at((0, 0)))
        self.assertEqual(z.min_safe_height, 1)
        self.assertFalse(z.short_cells & {(0, 0)})

    def test_full_order_needs_the_point_stroke_for_cell_two(self):
        report = optimize(self.spec)
        self.assertTrue(report.ok)
        self.assertEqual(report.best.stroke_ids, ("P3", "Z-0"))
        self.assertEqual([list(r) for r in report.best.result_grid], [[1, 1, 1]])


class ShopLedgerTests(ClosedMoldBase):
    def test_quote_does_not_peel_but_confirmation_does(self):
        spec = self.spec(self.plan_v1, rack=2)
        self.shop.sync_inventory(spec)
        quote = self.shop.quote("trial-1", spec)
        self.assertFalse(quote.consumed_inventory)
        self.assertEqual(self.shop.sticker_status("S-W"), "on_roll")
        order = self.shop.confirm("real-1", spec)
        self.assertEqual(order.peeled, ("S-W",))
        self.assertEqual(self.shop.sticker_status("S-W"), "peeled")
        self.assertEqual(set(order.tools), {"P", "W"})

    def test_later_order_cannot_reuse_peeled_sticker(self):
        spec = self.spec(self.plan_v1, rack=2)
        self.shop.confirm("first", spec)
        # The still-cheapest unconstrained set needs S-W, which is now gone.
        report = optimize(spec, consumed_stickers=["S-W"])
        self.assertTrue(report.ok)
        self.assertEqual(report.best.stroke_ids, ("P1", "P2", "P3"))
        self.assertEqual(report.best.total_fee, 6)
        # A self-chosen order trying to use the peeled sticker is rejected and
        # leaves inventory untouched.
        forbidden = evaluate_selection(spec, ["W-01", "P3"],
                                       consumed_stickers=frozenset({"S-W"}))
        self.assertFalse(forbidden.feasible)
        with self.assertRaises(MoldError):
            self.shop.confirm("greedy", spec, forbidden)
        self.assertEqual(self.shop.sticker_status("S-W"), "peeled")

    def test_reserving_the_sticker_approves_the_six_fee_alternative(self):
        spec = self.spec(self.plan_v1, rack=2)
        self.shop.sync_inventory(spec)
        quote = self.shop.quote("keep-sticker", spec, reserve=["S-W"])
        self.assertTrue(quote.report.ok)
        self.assertEqual(quote.report.best.stroke_ids, ("P1", "P2", "P3"))
        self.assertEqual(quote.report.best.total_fee, 6)
        # Reserving is not consuming: the sticker is still physically on the roll.
        self.assertEqual(self.shop.sticker_status("S-W"), "on_roll")
        order = self.shop.confirm("kept", spec, reserve=["S-W"])
        self.assertEqual(order.peeled, ())
        self.assertEqual(self.shop.sticker_status("S-W"), "on_roll")

    def test_two_orders_leave_different_physical_records(self):
        spec = self.spec(self.plan_v1, rack=2)
        self.shop.sync_inventory(spec)
        cheap = self.shop.confirm("with-sticker", spec)
        no_sticker = self.shop.confirm("without-sticker", spec, reserve=["S-W"])
        self.assertEqual(cheap.peeled, ("S-W",))
        self.assertEqual(no_sticker.peeled, ())
        self.assertNotEqual(cheap.order_id, no_sticker.order_id)
        self.assertEqual(len(self.shop.list_orders()), 2)

    def test_confirmation_is_idempotent_and_does_not_double_peel(self):
        spec = self.spec(self.plan_v1, rack=2)
        first = self.shop.confirm("idem", spec)
        second = self.shop.confirm("idem", spec)
        self.assertEqual(first.order_id, second.order_id)
        self.assertEqual(len(self.shop.list_orders()), 1)

    def test_request_key_reused_with_other_input_conflicts(self):
        spec = self.spec(self.plan_v1, rack=2)
        self.shop.confirm("same-key", spec)
        other = self.spec(self.plan_v2, rack=2)  # different frozen target/input
        with self.assertRaises(MoldError):
            self.shop.confirm("same-key", other)

    def test_recompute_after_catalog_moves_on(self):
        spec = self.spec(self.plan_v1, rack=2)
        order = self.shop.confirm("frozen", spec)
        # A brand-new elevation revision for a different source drawing must not
        # disturb the stored order; recomputation uses the frozen spec.
        self.catalog.apply_batch([
            {"tile_id": "T", "revision": 9, "row": 0, "col": 0,
             "values": [[2, 2, 2]]}
        ])
        recomputed = self.shop.recompute_order(order.order_id)
        self.assertTrue(recomputed["recomputed_matches_stored"])
        self.assertEqual(recomputed["frozen_plan_id"], self.plan_v1["plan_id"])
        self.assertEqual(
            recomputed["stored_selection"]["stroke_ids"], ["P3", "W-01"]
        )
        # The per-cell review reproduces the target on every touched cell.
        for line in recomputed["review"]:
            for cell in line:
                self.assertTrue(cell["matches_target"])


class SevenSeventyTwoMoldTests(unittest.TestCase):
    """The mold built on the real published A2 plan in materials/772."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.water = json.loads(
            (ROOT / "materials" / "772" / "input.json").read_text(encoding="utf-8")
        )
        self.mold_cfg = json.loads(
            (ROOT / "materials" / "772" / "mold.json").read_text(encoding="utf-8")
        )
        self.catalog = Catalog(
            self.dir / "catalog.db",
            self.water["height"],
            self.water["width"],
            self.water["ceiling"],
        )
        self.publisher = Publisher(self.dir / "plans.db", self.catalog)
        self.catalog.apply_batch(self.water["initial_tiles"])
        self.catalog.apply_batch([self.water["correction"]])
        for request in self.water["requests"]:
            self.publisher.publish(request, default_outlets=self.water["outlets"])
        self.artifact = self.publisher.plan_for_request("A2")
        target = target_from_plan(self.artifact, self.mold_cfg["blank_height"])
        self.spec = build_spec(
            target,
            self.mold_cfg["tools"],
            self.mold_cfg["strokes"],
            self.mold_cfg["rack_capacity"],
        )
        self.shop = MoldShop(self.dir / "shop.db")

    def tearDown(self) -> None:
        self.shop.close()
        self.publisher.close()
        self.catalog.close()
        self.tmp.cleanup()

    def test_published_plan_verifies_before_molding(self):
        result = self.publisher.verify(self.artifact["plan_id"])
        self.assertTrue(result.ok, result.messages)
        self.assertEqual(self.artifact["opened"], ["L", "R"])
        self.assertEqual(self.artifact["storage"], [[1, 0, 2, 1, 3]])

    def test_target_grid_matches_published_storage(self):
        self.assertEqual(
            [list(r) for r in self.spec.target.target], [[3, 4, 2, 3, 1]]
        )

    def test_full_wide_sweep_cannot_finish_in_one_pass(self):
        full = analyze_stroke(self.spec, "W-FULL")
        self.assertEqual(full.min_safe_height, 4)
        self.assertEqual(full.limiting_cells, ((0, 1),))
        # At the forced height only cell 1 is exact; every deep cell stays short.
        self.assertEqual(full.exact_cells, frozenset({(0, 1)}))
        self.assertEqual(
            full.short_cells, frozenset({(0, 0), (0, 2), (0, 3), (0, 4)})
        )

    def test_short_wide_stroke_hits_two_cells_together(self):
        # Holder at col 2: the lift-0 edge lands on cell 2 (target 2) and the
        # lift-1 edge on cell 3 (target 3); at holder height 2 both bind, so one
        # stroke reaches two different target thicknesses exactly.
        w23 = analyze_stroke(self.spec, "W-23")
        self.assertEqual(w23.min_safe_height, 2)
        self.assertEqual(w23.exact_cells, frozenset({(0, 2), (0, 3)}))
        self.assertEqual(w23.limiting_cells, ((0, 2), (0, 3)))

    def test_cheapest_order_mixes_knives_and_beats_all_points(self):
        report = optimize(self.spec)
        self.assertTrue(report.ok)
        expected = self.mold_cfg["expected"]
        self.assertEqual(list(report.best.stroke_ids), expected["cheapest_stroke_ids"])
        self.assertEqual(report.best.total_fee, expected["cheapest_total_fee"])
        all_points = evaluate_selection(
            self.spec, expected["all_point_stroke_ids"]
        )
        self.assertEqual(all_points.total_fee, expected["all_point_fee"])
        self.assertGreater(all_points.total_fee, report.best.total_fee)
        self.assertEqual(
            [list(r) for r in report.best.result_grid], [[3, 4, 2, 3, 1]]
        )
    def test_shop_flow_quote_confirm_then_blocked_later_order(self):
        self.shop.sync_inventory(self.spec)
        trial = self.shop.quote("772-trial", self.spec)
        self.assertFalse(trial.consumed_inventory)
        self.assertEqual(self.shop.sticker_status("S-W"), "on_roll")
        order = self.shop.confirm("772-real", self.spec)
        self.assertEqual(order.peeled, ("S-W",))
        # A later order under the same opening conditions loses the sticker.
        later = self.shop.quote("772-later", self.spec)
        self.assertTrue(later.report.ok)
        self.assertNotIn("W-23", later.report.best.stroke_ids)
        self.assertEqual(self.shop.sticker_status("S-W"), "peeled")
        recomputed = self.shop.recompute_order(order.order_id)
        self.assertTrue(recomputed["recomputed_matches_stored"])


if __name__ == "__main__":
    unittest.main()
