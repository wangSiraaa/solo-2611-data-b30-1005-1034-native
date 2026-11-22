"""Tests for rigid alignment, provenance-preserving diffs, and sand transport.

Run with::

    python -m unittest discover -s tests -v

The worked 1x3 sample is in ``materials/renovation/input.json``; its expected
costs (4, 0, 6) and the insufficient-marker ambiguity are asserted here so the
tests double as a regression of the stated example.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from slabwater.align import AlignmentError, align_slices, compute_difference
from slabwater.catalog import Catalog
from slabwater.renovation import analyze, run_material, run_material_file
from slabwater.transport import TransportError, plan_transport

ROOT = Path(__file__).resolve().parent.parent
MATERIAL = ROOT / "materials" / "renovation" / "input.json"


def load_material():
    return json.loads(MATERIAL.read_text(encoding="utf-8"))


def make_catalog(tmp, height, width, ceiling, tiles, name="c.db"):
    catalog = Catalog(Path(tmp) / name, height, width, ceiling)
    result = catalog.apply_batch(tiles)
    assert result.ok, result.errors
    return catalog, catalog.get_slice(result.version)


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def slices(self, old_tiles, target_tiles, height=1, width=3, ceiling=9):
        old_cat, old = make_catalog(self.dir, height, width, ceiling, old_tiles, "old.db")
        tgt_cat, target = make_catalog(self.dir, height, width, ceiling, target_tiles, "tgt.db")
        self.addCleanup(old_cat.close)
        self.addCleanup(tgt_cat.close)
        return old, target

    def test_same_direction_two_scoops(self):
        old, target = self.slices(
            [{"tile_id": "O", "revision": 1, "row": 0, "col": 0, "values": [[2, 0, 0]]}],
            [{"tile_id": "N", "revision": 1, "row": 0, "col": 0, "values": [[0, 0, 2]]}],
        )
        result = align_slices(
            old, target,
            {"L": [0, 0], "M": [0, 1], "R": [0, 2]},
            {"L": [0, 0], "M": [0, 1], "R": [0, 2]},
            [{"old": "L", "targets": ["L"]}, {"old": "R", "targets": ["R"]}],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "aligned")
        self.assertEqual(result.maps[0].delta, ((2, 0, -2),))
        self.assertEqual(result.maps[0].positive_total, 2)
        self.assertEqual(result.maps[0].negative_total, 2)

    def test_flipped_endpoints_zero_difference(self):
        old, target = self.slices(
            [{"tile_id": "O", "revision": 1, "row": 0, "col": 0, "values": [[2, 0, 0]]}],
            [{"tile_id": "N", "revision": 1, "row": 0, "col": 0, "values": [[0, 0, 2]]}],
        )
        result = align_slices(
            old, target,
            {"L": [0, 0], "R": [0, 2]},
            {"L": [0, 0], "R": [0, 2]},
            [{"old": "L", "targets": ["R"]}, {"old": "R", "targets": ["L"]}],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.maps[0].delta, ((0, 0, 0),))
        self.assertTrue(result.maps[0].transform.mirror)

    def test_insufficient_markers_is_ambiguous_not_cheapest(self):
        old, target = self.slices(
            [{"tile_id": "O", "revision": 1, "row": 0, "col": 0, "values": [[2, 0, 0]]}],
            [{"tile_id": "N", "revision": 1, "row": 0, "col": 0, "values": [[0, 0, 2]]}],
        )
        # Only the middle marker is pinned: both the identity and the mirror
        # satisfy it, yet they yield different full-plate differences.
        result = align_slices(
            old, target,
            {"M": [0, 1]},
            {"M": [0, 1]},
            [{"old": "M", "targets": ["M"]}],
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "ambiguous")
        signatures = {difference.signature() for difference in result.maps}
        self.assertGreater(len(signatures), 1)
        self.assertEqual(set(result.disagreement_cells), {(0, 0), (0, 2)})

    def test_different_origin_is_not_index_subtraction(self):
        # Old plate lives at its own origin; the target plate is installed under
        # a different one on the same 2x3 surface.  Both are fully covered, so a
        # naive same-index subtraction would silently produce a wrong diff; the
        # diff must come from the aligned (rotated/translated) placement instead.
        old_cat, old = make_catalog(
            self.dir,
            2,
            3,
            9,
            [
                {"tile_id": "A", "revision": 1, "row": 0, "col": 0, "values": [[1, 0, 0]]},
                {"tile_id": "B", "revision": 1, "row": 1, "col": 0, "values": [[0, 2, 0]]},
            ],
            "old2.db",
        )
        # Target is the 180-degree rotation of the old heights: [[0,0,1],[0,2,0]]
        # -> rotated -> [[0,2,0],[1,0,0]].  One tile, installed at origin (0,0).
        tgt_cat, target = make_catalog(
            self.dir,
            2,
            3,
            9,
            [{"tile_id": "C", "revision": 1, "row": 0, "col": 0, "values": [[0, 2, 0], [1, 0, 0]]}],
            "tgt2.db",
        )
        self.addCleanup(old_cat.close)
        self.addCleanup(tgt_cat.close)
        result = align_slices(
            old,
            target,
            {"P": [0, 0]},
            {"P": [1, 0]},  # old top-left corresponds to target bottom-left
            [{"old": "P", "targets": ["P"]}],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.maps[0].transform.rotation, 180)
        self.assertEqual(result.maps[0].delta, ((0, 0, 0), (0, 0, 0)))
        # Same-index subtraction would have been far from zero here.
        naive = [
            [old.heights[r][c] - target.heights[r][c] for c in range(3)] for r in range(2)
        ]
        self.assertNotEqual(naive, [[0, 0, 0], [0, 0, 0]])

    def test_provenance_points_at_original_tiles(self):
        old, target = self.slices(
            [
                {"tile_id": "OA", "revision": 1, "row": 0, "col": 0, "values": [[5, 0, 0]]},
            ],
            [
                {"tile_id": "NB", "revision": 2, "row": 0, "col": 0, "values": [[0, 0, 5]]},
            ],
        )
        result = align_slices(
            old, target, {"L": [0, 0]}, {"L": [0, 0]},
            [{"old": "L", "targets": ["L"]}],
        )
        self.assertTrue(result.ok)
        entries = {entry.old_cell: entry for entry in result.maps[0].deltas}
        surplus = entries[(0, 0)]
        self.assertEqual(surplus.delta, 5)
        self.assertEqual([ref.tile_id for ref in surplus.old_sources], ["OA"])
        self.assertEqual([ref.tile_id for ref in surplus.target_sources], ["NB"])
        self.assertEqual(surplus.old_sources[0].local_col, 0)
        deficit = entries[(0, 2)]
        self.assertEqual(deficit.delta, -5)
        self.assertEqual([ref.tile_id for ref in deficit.target_sources], ["NB"])
        self.assertEqual(deficit.target_sources[0].local_col, 2)

    def test_shifted_target_uses_translation(self):
        # 1x3 old; target is the same plate shifted out of the origin.  A tile
        # may sit at different columns; the rigid placement must translate it.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old_cat = Catalog(Path(tmp.name) / "o.db", 2, 3, 9)
        tgt_cat = Catalog(Path(tmp.name) / "t.db", 2, 3, 9)
        self.addCleanup(old_cat.close)
        self.addCleanup(tgt_cat.close)
        old_cat.apply_batch(
            [
                {"tile_id": "A", "revision": 1, "row": 0, "col": 0, "values": [[5, 0, 0]]},
                {"tile_id": "B", "revision": 1, "row": 1, "col": 0, "values": [[0, 0, 0]]},
            ]
        )
        # Target has the 5 in the bottom-right after a 180 degree rotation.
        tgt_cat.apply_batch(
            [
                {"tile_id": "C", "revision": 1, "row": 0, "col": 0, "values": [[0, 0, 0]]},
                {"tile_id": "D", "revision": 1, "row": 1, "col": 0, "values": [[0, 0, 5]]},
            ]
        )
        old = old_cat.get_slice(1)
        target = tgt_cat.get_slice(1)
        result = align_slices(
            old, target,
            {"P": [0, 0]},
            {"P": [1, 2]},
            [{"old": "P", "targets": ["P"]}],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.maps[0].transform.rotation, 180)
        self.assertEqual(result.maps[0].delta[0][0], 0)


class TransportTests(unittest.TestCase):
    def test_two_lanes_cost_four(self):
        result = plan_transport(
            [[2, 0, -2]],
            [
                {"from": [0, 0], "to": [0, 1], "capacity": 2, "cost": 1},
                {"from": [0, 1], "to": [0, 2], "capacity": 2, "cost": 1},
            ],
            outsource_price=1,
            buy_price=3,
        )
        self.assertEqual(result.total_cost, 4)
        self.assertEqual(result.freight_cost, 4)
        self.assertEqual([lane.flow for lane in result.lanes], [2, 2])
        self.assertEqual(result.outsourced_total, 0)
        self.assertEqual(result.bought_total, 0)

    def test_zero_difference_costs_zero(self):
        result = plan_transport(
            [[0, 0, 0]],
            [
                {"from": [0, 0], "to": [0, 1], "capacity": 2, "cost": 1},
                {"from": [0, 1], "to": [0, 2], "capacity": 2, "cost": 1},
            ],
            outsource_price=1,
            buy_price=3,
        )
        self.assertEqual(result.total_cost, 0)
        self.assertEqual(result.lane_total, 0)

    def test_capacity_one_moves_one_and_trades_rest(self):
        result = plan_transport(
            [[2, 0, -2]],
            [
                {"from": [0, 0], "to": [0, 1], "capacity": 1, "cost": 1},
                {"from": [0, 1], "to": [0, 2], "capacity": 2, "cost": 1},
            ],
            outsource_price=1,
            buy_price=3,
        )
        # move 1 (freight 2) + outsource 1 (1) + buy 1 (3) = 6
        self.assertEqual(result.total_cost, 6)
        self.assertEqual(result.lane_total, 2)
        self.assertEqual(result.outsourced_total, 1)
        self.assertEqual(result.bought_total, 1)
        self.assertEqual(result.outsource_cost, 1)
        self.assertEqual(result.buy_cost, 3)

    def test_two_orders_share_one_lane_capacity(self):
        # Two surplus cells (0 and 1) both sit upstream of one bottleneck lane
        # (1)->(2).  The lane's capacity is shared across shipments, never
        # duplicated per order, and the reported flow never exceeds it.
        result = plan_transport(
            [[3, 3, -2]],
            [
                {"from": [0, 0], "to": [0, 1], "capacity": 6, "cost": 0},
                {"from": [0, 1], "to": [0, 2], "capacity": 1, "cost": 1},
            ],
            outsource_price=9,
            buy_price=9,
        )
        bottleneck = next(l for l in result.lanes if l.target == (0, 2))
        self.assertEqual(bottleneck.capacity, 1)
        self.assertEqual(bottleneck.flow, 1)  # only one spoon fits, not two
        # The remaining spoon of fill is bought; leftover surplus (3 + 2) is
        # outsourced.  Total surplus 6 = 1 shipped in + 5 outsourced.
        self.assertEqual(result.bought_total, 1)
        self.assertEqual(result.outsourced_total, 5)
        for lane in result.lanes:
            self.assertLessEqual(lane.flow, lane.capacity)

    def test_no_closed_loop_production(self):
        # A zero-cost cycle must not create sand; the only real flows are from
        # the surplus to the deficit.
        result = plan_transport(
            [[2, 0, 0]],
            [
                {"from": [0, 0], "to": [0, 1], "capacity": 2, "cost": 0},
                {"from": [0, 1], "to": [0, 2], "capacity": 2, "cost": 0},
                {"from": [0, 2], "to": [0, 0], "capacity": 2, "cost": 0},
            ],
            outsource_price=5,
            buy_price=5,
        )
        loop = next(l for l in result.lanes if l.source == (0, 2))
        self.assertEqual(loop.flow, 0)
        # Surplus 2 with no deficit: outsource all of it, no loop pumping.
        self.assertEqual(result.outsourced_total, 2)
        self.assertEqual(result.total_cost, 10)

    def test_conservation_every_cell(self):
        result = plan_transport(
            [[3, -1, -2]],
            [
                {"from": [0, 0], "to": [0, 1], "capacity": 1, "cost": 2},
                {"from": [0, 1], "to": [0, 2], "capacity": 2, "cost": 1},
            ],
            outsource_price=1,
            buy_price=2,
        )
        for balance in result.balances:
            self.assertEqual(balance.residual, 0)
            self.assertTrue(balance.to_dict()["conserved"])

    def test_lane_conflicting_duplicate_rejected(self):
        with self.assertRaises(TransportError):
            plan_transport(
                [[1, -1]],
                [
                    {"from": [0, 0], "to": [0, 1], "capacity": 1, "cost": 1},
                    {"from": [0, 0], "to": [0, 1], "capacity": 2, "cost": 1},
                ],
                outsource_price=1,
                buy_price=1,
            )

    def test_outsource_needs_a_surplus_and_buy_needs_a_short(self):
        # With no prices at all there is no way to move the imbalance, so the
        # problem is infeasible rather than inventing sand.
        with self.assertRaises(TransportError):
            plan_transport([[1, -1]], [])
        # A surplus-only plate can be cleared by sending the excess away, but
        # never by buying (buy applies only to short cells).
        result = plan_transport([[3, 0]], [], outsource_price=2)
        self.assertEqual(result.outsourced_total, 3)
        self.assertEqual(result.bought_total, 0)
        self.assertEqual(result.total_cost, 6)
        # A short-only plate can buy its deficit, never outsource.
        result = plan_transport([[0, -2]], [], buy_price=4)
        self.assertEqual(result.bought_total, 2)
        self.assertEqual(result.outsourced_total, 0)
        self.assertEqual(result.total_cost, 8)


class RenovationTests(unittest.TestCase):
    def test_worked_material_matches_all_three_costs(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_material_file(MATERIAL, Path(tmp) / "db")
        by_id = {s["id"]: s for s in report["scenarios"]}
        self.assertEqual(by_id["same_direction"]["transport"]["total_cost"], 4)
        self.assertEqual(by_id["same_direction"]["transport"]["moved_total"], 2)
        self.assertEqual(by_id["flipped"]["transport"]["total_cost"], 0)
        self.assertEqual(by_id["flipped"]["difference"]["delta"], [[0, 0, 0]])
        reduced = by_id["reduced_first_lane"]["transport"]
        self.assertEqual(reduced["total_cost"], 6)
        self.assertEqual(reduced["lane_total"], 2)
        self.assertEqual(reduced["outsourced_total"], 1)
        self.assertEqual(reduced["bought_total"], 1)
        missing = by_id["insufficient_markers"]
        self.assertEqual(missing["status"], "ambiguous")
        self.assertEqual(set(map(tuple, missing["disagreement_cells"])), {(0, 0), (0, 2)})
        self.assertEqual(len(missing["maps"]), 2)

    def test_both_plates_go_through_real_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_material_file(MATERIAL, Path(tmp) / "db")
            self.assertEqual(report["versions"]["old"], [1])
            self.assertEqual(report["versions"]["target"], [1])
        # The old and target slices really are distinct frozen surfaces.
        self.assertNotEqual(report["old"]["elevation_hash"], report["target"]["elevation_hash"])
        self.assertEqual(report["old"]["heights"], [[2, 0, 0]])
        self.assertEqual(report["target"]["heights"], [[0, 0, 2]])

    def test_aligned_map_retains_both_versions_and_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_material_file(MATERIAL, Path(tmp) / "db")
        same = next(s for s in report["scenarios"] if s["id"] == "same_direction")
        deltas = same["difference"]["deltas"]
        self.assertEqual(len(deltas), 2)
        positive = next(d for d in deltas if d["delta"] > 0)
        self.assertEqual(positive["old_cell"], [0, 0])
        self.assertEqual(positive["target_cell"], [0, 0])
        self.assertEqual(positive["old_sources"][0]["tile_id"], "OLD")
        self.assertEqual(positive["target_sources"][0]["tile_id"], "NEW")
        negative = next(d for d in deltas if d["delta"] < 0)
        self.assertEqual(negative["target_cell"], [0, 2])
        self.assertEqual(negative["target_fill"], 2)

    def test_coverage_gap_target_rejected_at_catalog(self):
        from slabwater.renovation import RenovationError

        with tempfile.TemporaryDirectory() as tmp:
            material = load_material()
            material["target_tiles"] = [
                {"tile_id": "N", "revision": 1, "row": 0, "col": 0, "values": [[0, 0]]}
            ]
            with self.assertRaises(RenovationError) as ctx:
                run_material(material, Path(tmp) / "db")
            self.assertEqual(ctx.exception.code, "batch_rejected")
            codes = {e["code"] for e in ctx.exception.details["errors"]}
            self.assertIn("coverage_gap", codes)

    def test_malformed_ticket_is_reported_not_crashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            material = load_material()
            material["scenarios"] = [
                {"id": "bad", "tickets": [{"old": "NOPE", "targets": ["L"]}]}
            ]
            report = run_material(material, Path(tmp) / "db")
        self.assertEqual(report["scenarios"][0]["status"], "malformed")

    def test_analyze_direct_on_slices(self):
        material = load_material()
        with tempfile.TemporaryDirectory() as tmp:
            old_cat = Catalog(Path(tmp) / "o.db", 1, 3, 9)
            tgt_cat = Catalog(Path(tmp) / "t.db", 1, 3, 9)
            try:
                old_cat.apply_batch(material["old_tiles"])
                tgt_cat.apply_batch(material["target_tiles"])
                report = analyze(
                    old_cat.get_slice(1),
                    tgt_cat.get_slice(1),
                    material["old_markers"],
                    material["target_markers"],
                    [{"id": "only", "tickets": material["scenarios"][0]["tickets"]}],
                    defaults={
                        "lanes": material["lanes"],
                        "outsource_price": 1,
                        "buy_price": 3,
                    },
                )
            finally:
                old_cat.close()
                tgt_cat.close()
        self.assertEqual(report["scenarios"][0]["transport"]["total_cost"], 4)


if __name__ == "__main__":
    unittest.main()
