"""Tests for the threshold-component ladder and old-name migration.

The example is the closed ``C=3`` one-row surface from the task: old
``[0,2,1,3,1]`` (depth ``[3,1,2,0,2]``) with names 青 (4) at (0,0) and 石 (3)
at (0,2), both at depth 2.  The counterexample is a cross-system trap.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from slabwater.catalog import Catalog
from slabwater.cli import run_lakes
from slabwater.lakes import (
    LakeError,
    OldName,
    build_ladder,
    components_at,
    depth_product_from_grid,
    depth_product_from_plan,
    eligible_blocks,
    kinship_report,
    ladder_report,
    migrate,
)
from slabwater.publisher import Publisher

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "materials" / "lakes" / "example.json"
CONFLICT = ROOT / "materials" / "lakes" / "conflict.json"


def product(heights, ceiling=3):
    """Depth product from terrain via the published rule depth = C - height."""

    return depth_product_from_grid([[ceiling - h for h in row] for row in heights])


def names_from(material):
    return [OldName.from_dict(raw) for raw in material["old_names"]]


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class LadderTests(unittest.TestCase):
    def test_depth_comes_from_published_storage(self):
        # A signed closed-basin plan publishes depth = ceiling - height.
        material = load(EXAMPLE)
        with tempfile.TemporaryDirectory() as tmp:
            report = run_lakes(EXAMPLE, tmp)
        self.assertEqual(report["editions"]["old"]["depth"], [[3, 1, 2, 0, 2]])
        self.assertEqual(report["editions"]["new"]["depth"], [[3, 2, 2, 0, 2]])
        # The old ladder's threshold-2 blocks match that depth, not the heights.
        blocks = report["old_ladder"]["thresholds"]
        t2 = next(t for t in blocks if t["threshold"] == 2)["blocks"]
        self.assertEqual(
            sorted(tuple(b["cells"][0]) for b in t2),
            [(0, 0), (0, 2), (0, 4)],
        )

    def test_flat_top_yields_single_component(self):
        grid = [[4, 4, 4, 4, 4]]
        comps = components_at(grid, 4)
        self.assertEqual(comps, [((0, 0), (0, 1), (0, 2), (0, 3), (0, 4))])

    def test_separated_peaks_same_depth_are_distinct(self):
        comps = components_at([[4, 0, 4]], 4)
        self.assertEqual(comps, [((0, 0),), ((0, 2),)])

    def test_nesting_is_unique_parent(self):
        ladder = build_ladder(product([[0, 1, 2, 3, 2, 1, 0]]), [1, 2, 3])
        for level_idx in range(1, len(ladder.thresholds)):
            level = ladder.thresholds[level_idx]
            lower = ladder.thresholds[level_idx - 1]
            for block_id in ladder.by_threshold[level]:
                block = ladder.blocks[block_id]
                parents = {
                    pid
                    for pid in ladder.by_threshold[lower]
                    if block.cell_set <= ladder.blocks[pid].cell_set
                }
                self.assertEqual(len(parents), 1, f"{block_id} has ambiguous parent")
                self.assertEqual(ladder.parents[block_id], next(iter(parents)))
        self.assertIsNone(ladder.parents[ladder.by_threshold[1][0]])

    def test_same_cells_different_depth_are_distinct_blocks(self):
        ladder = build_ladder(depth_product_from_grid([[3, 3, 3]]), [2, 3])
        self.assertNotEqual(ladder.by_threshold[2][0], ladder.by_threshold[3][0])
        self.assertEqual(ladder.query(2, (0, 1)), "T2#0")
        self.assertEqual(ladder.query(3, (0, 1)), "T3#0")

    def test_query_returns_block_per_threshold(self):
        ladder = build_ladder(product([[0, 2, 1, 3, 1]]), [1, 2])
        self.assertEqual(ladder.query(2, (0, 0)), "T2#0")
        self.assertEqual(ladder.query(2, (0, 2)), "T2#1")
        self.assertIsNone(ladder.query(2, (0, 3)))  # depth 0

    def test_report_has_area_ancestry_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_lakes(EXAMPLE, tmp)
        t2 = next(t for t in report["new_ladder"]["thresholds"] if t["threshold"] == 2)
        merged = next(b for b in t2["blocks"] if [0, 0] in b["cells"])
        self.assertEqual(merged["area"], 3)
        self.assertEqual(merged["cells"], [[0, 0], [0, 1], [0, 2]])
        # Every cell carries catalog provenance from the single RIDGE tile.
        self.assertTrue(merged["provenance"])
        for item in merged["provenance"]:
            self.assertEqual({s["tile_id"] for s in item["sources"]}, {"RIDGE"})

    def test_invalid_threshold_rejected(self):
        with self.assertRaises(LakeError) as ctx:
            components_at([[1, 1]], 0)
        self.assertEqual(ctx.exception.code, "bad_threshold")


class ExampleMigrationTests(unittest.TestCase):
    def setUp(self):
        material = load(EXAMPLE)
        self.thresholds = material["thresholds"]
        self.old_ladder = build_ladder(product([[0, 2, 1, 3, 1]]), self.thresholds)
        self.names = names_from(material)

    def test_lowered_channel_merges_into_one_block(self):
        new_ladder = build_ladder(product([[0, 1, 1, 3, 1]]), self.thresholds)
        t2 = [new_ladder.blocks[b].cells for b in new_ladder.by_threshold[2]]
        self.assertIn(((0, 0), (0, 1), (0, 2)), t2)
        result = migrate(self.old_ladder, new_ladder, self.names, max_names=2)
        self.assertEqual(result.total_importance, 4)
        self.assertEqual(result.total_overlap, 1)
        self.assertEqual([t.name for t in result.tags], ["青"])
        self.assertEqual([l.name for l in result.lost], ["石"])
        self.assertEqual(result.tags[0].new_block, "T2#0")

    def test_both_names_share_one_block_only_one_kept(self):
        new_ladder = build_ladder(product([[0, 1, 1, 3, 1]]), self.thresholds)
        qing, shi = self.names
        # Both old blocks overlap the single new threshold-2 block, so both are
        # individually eligible; only one may claim it.
        self.assertEqual([e.block_id for e in eligible_blocks(qing, new_ladder)], ["T2#0"])
        self.assertEqual([e.block_id for e in eligible_blocks(shi, new_ladder)], ["T2#0"])
        self.assertEqual(eligible_blocks(qing, new_ladder)[0].overlap, 1)
        self.assertEqual(eligible_blocks(shi, new_ladder)[0].overlap, 1)

    def test_restored_terrain_keeps_both(self):
        new_ladder = build_ladder(product([[0, 2, 1, 3, 1]]), self.thresholds)
        result = migrate(self.old_ladder, new_ladder, self.names, max_names=2)
        self.assertEqual(result.total_importance, 7)
        self.assertEqual(result.total_overlap, 2)
        placed = {t.name: t.new_block for t in result.tags}
        self.assertEqual(placed, {"青": "T2#0", "石": "T2#1"})
        self.assertNotEqual(placed["青"], placed["石"])

    def test_restored_terrain_cap_one_keeps_qing(self):
        new_ladder = build_ladder(product([[0, 2, 1, 3, 1]]), self.thresholds)
        result = migrate(self.old_ladder, new_ladder, self.names, max_names=1)
        self.assertEqual(result.total_importance, 4)
        self.assertEqual([t.name for t in result.tags], ["青"])
        # 石's block still exists independently; it is simply unnamed.
        placed = {b for b in new_ladder.by_threshold[2]}
        self.assertIn("T2#1", placed)

    def test_lowered_channel_honours_cap_one(self):
        new_ladder = build_ladder(product([[0, 1, 1, 3, 1]]), self.thresholds)
        result = migrate(self.old_ladder, new_ladder, self.names, max_names=1)
        self.assertEqual(result.total_importance, 4)
        self.assertEqual([l.name for l in result.lost], ["石"])


class CrossSystemConflictTests(unittest.TestCase):
    def setUp(self):
        material = load(CONFLICT)
        self.thresholds = material["thresholds"]
        # Old depth [3,2,0,2,2,2]; new depth [3,2,2,2,2,2].
        self.old_ladder = build_ladder(product([[1, 2, 4, 2, 2, 2]], 4), self.thresholds)
        self.new_ladder = build_ladder(product([[1, 2, 2, 2, 2, 2]], 4), self.thresholds)
        self.names = names_from(material)
        self.by_name = {n.name: n for n in self.names}

    def test_each_max_overlap_choice_is_individually_best(self):
        deep = self.by_name["深A"]
        shallow_b = self.by_name["浅B"]
        deep_best = eligible_blocks(deep, self.new_ladder)[0]
        shallow_best = eligible_blocks(shallow_b, self.new_ladder)[0]
        self.assertEqual((deep_best.block_id, deep_best.overlap), ("T3#0", 1))
        self.assertEqual((shallow_best.block_id, shallow_best.overlap), ("T2#0", 3))
        # Jointly the deep block would be nested inside shallow B's block.
        deep_cells = self.new_ladder.blocks[deep_best.block_id].cell_set
        shallow_cells = self.new_ladder.blocks[shallow_best.block_id].cell_set
        self.assertTrue(deep_cells <= shallow_cells)
        self.assertNotEqual(deep.system, shallow_b.system)

    def test_naive_greedy_scores_higher_but_is_refused(self):
        # Taking every name's max-overlap block would keep all three names for 11
        # points -- but then 深A (A湖系) would hang under 浅B (B湖系).  The
        # constraint-aware optimum keeps 浅A+深A for 7 points instead.
        result = migrate(self.old_ladder, self.new_ladder, self.names, max_names=3)
        self.assertEqual(result.total_importance, 7)
        self.assertEqual([t.name for t in result.tags], ["浅A", "深A"])
        self.assertEqual([l.name for l in result.lost], ["浅B"])

    def test_same_system_ancestry_is_preserved(self):
        result = migrate(self.old_ladder, self.new_ladder, self.names, max_names=3)
        kin = kinship_report(result, self.new_ladder)
        self.assertEqual(len(kin), 1)
        entry = kin[0]
        self.assertEqual((entry["shallow_name"], entry["deep_name"]), ("浅A", "深A"))
        self.assertTrue(entry["old_nested"] and entry["new_nested"])
        self.assertFalse(entry["cross_system"])
        self.assertTrue(entry["verified_from_depth_product"])

    def test_same_system_hierarchy_must_be_bidirectional(self):
        # All three in one system. 深A is an old branch of 浅A but NOT of 浅B;
        # in the new edition 深A nests inside the single depth-2 block, so pairing
        # it with 浅B (not its old parent) would newly create a hierarchy.  The
        # bidirectional rule refuses that even though they share a system, so the
        # optimum is the old ancestor 浅A + 深A (7), not 浅B + 深A (9).
        names = [
            OldName("浅A", 2, "同系", 2, ((0, 0), (0, 1)), (2,)),
            OldName("深A", 5, "同系", 3, ((0, 0),), (3,)),
            OldName("浅B", 4, "同系", 2, ((0, 3), (0, 4), (0, 5)), (2,)),
        ]
        result = migrate(self.old_ladder, self.new_ladder, names, max_names=3)
        self.assertEqual(result.total_importance, 7)
        self.assertEqual({t.name for t in result.tags}, {"深A", "浅A"})


class ConstraintTests(unittest.TestCase):
    def test_name_cannot_change_threshold(self):
        old = build_ladder(product([[0, 2, 1, 3, 1]]), [1, 2])
        new = build_ladder(product([[0, 1, 1, 3, 1]]), [1, 2])
        # Old 青 lives at threshold 2; forbid 2 and it may never move to 1.
        name = OldName("青", 4, "青系", 2, ((0, 0),), (1,))
        self.assertEqual(eligible_blocks(name, new), [])
        result = migrate(old, new, [name], max_names=1)
        self.assertEqual(result.tags, ())
        self.assertEqual(result.lost[0].reason, "threshold_not_allowed")

    def test_per_edition_block_cap_is_eighteen(self):
        # 19 isolated depth-2 cells -> 19 eligible blocks, but only 18 may bear
        # a name.  Names are top-importance first, so the lowest is dropped.
        row = []
        for _ in range(19):
            row.extend([2, 0])
        row = row[: 19 * 2 - 1]  # 19 peaks separated by depth 0
        old = build_ladder(depth_product_from_grid([row]), [2])
        new = build_ladder(depth_product_from_grid([row]), [2])
        self.assertEqual(len(new.by_threshold[2]), 19)
        names = [
            OldName(f"N{i:02d}", 100 - i, "系", 2, ((0, 2 * i),), (2,))
            for i in range(19)
        ]
        result = migrate(old, new, names)
        self.assertEqual(len(result.tags), 18)
        self.assertEqual(result.max_names, 18)
        self.assertEqual(len(result.lost), 1)

    def test_zero_overlap_is_ineligible(self):
        # depth [3,1,2,0,2]: threshold-2 blocks at {0} and {4}.
        old = build_ladder(product([[0, 2, 1, 3, 1]]), [2])
        # new depth [3,2,2,0,2]: threshold-2 block {0,1,2} and {4}.
        new = build_ladder(product([[0, 1, 1, 3, 1]]), [2])
        # A name whose old block is {(0,4)} may not take the {(0,0),(0,1),(0,2)}
        # block because they share no cell.
        far = OldName("远", 1, "青系", 2, ((0, 4),), (2,))
        eligible_far = eligible_blocks(far, new)
        self.assertEqual([e.block_id for e in eligible_far], ["T2#1"])
        self.assertTrue(all(e.overlap == 1 for e in eligible_far))
        self.assertTrue(old.by_threshold[2])
        self.assertTrue(new.by_threshold[2])

    def test_old_block_must_be_real_component(self):
        old = build_ladder(product([[0, 2, 1, 3, 1]]), [1, 2])
        new = build_ladder(product([[0, 1, 1, 3, 1]]), [1, 2])
        bogus = OldName("假", 1, "青系", 2, ((0, 0), (0, 4)), (2,))
        with self.assertRaises(LakeError) as ctx:
            migrate(old, new, [bogus], max_names=1)
        self.assertEqual(ctx.exception.code, "old_block_not_a_component")

    def test_duplicate_name_rejected(self):
        old = build_ladder(product([[0, 2, 1, 3, 1]]), [1, 2])
        new = build_ladder(product([[0, 1, 1, 3, 1]]), [1, 2])
        dup = OldName("青", 4, "青系", 2, ((0, 0),), (2,))
        with self.assertRaises(LakeError) as ctx:
            migrate(old, new, [dup, dup], max_names=2)
        self.assertEqual(ctx.exception.code, "duplicate_name")

    def test_maximises_importance_before_overlap(self):
        # Old depth [2,2,1,2,0]: threshold-2 comps {0,1} and {3}.
        old = build_ladder(product([[1, 1, 2, 1, 3]]), [2])
        # New depth [2,2,2,2,0]: both merge into one threshold-2 block {0,1,2,3}.
        new = build_ladder(product([[1, 1, 1, 1, 3]]), [2])
        # High-importance name with overlap 1 vs low-importance name with overlap 2.
        heavy = OldName("重", 9, "甲", 2, ((0, 3),), (2,))       # overlaps new block by 1
        light = OldName("轻", 1, "乙", 2, ((0, 0), (0, 1)), (2,))  # overlaps by 2
        result = migrate(old, new, [heavy, light], max_names=1)
        self.assertEqual([t.name for t in result.tags], ["重"])
        self.assertEqual(result.total_importance, 9)
        self.assertEqual(result.total_overlap, 1)
        # If overlap came first, the light name (overlap 2) would have won.
        self.assertEqual([l.name for l in result.lost], ["轻"])


class KinshipTests(unittest.TestCase):
    def test_ancestry_is_verifiable_from_depth(self):
        # depth [3,3,1,0]: threshold 2 = {(0,0),(0,1)}, nested in threshold 1.
        old = build_ladder(product([[0, 0, 2, 3]]), [1, 2])
        new = build_ladder(product([[0, 0, 2, 3]]), [1, 2])
        deep = OldName("深", 5, "系", 2, ((0, 0), (0, 1)), (2,))
        shallow = OldName("浅", 3, "系", 1, ((0, 0), (0, 1), (0, 2)), (1,))
        result = migrate(old, new, [deep, shallow], max_names=2)
        kin = kinship_report(result, new)
        self.assertEqual(len(kin), 1)
        self.assertEqual(kin[0]["shallow_name"], "浅")
        self.assertEqual(kin[0]["deep_name"], "深")
        self.assertTrue(kin[0]["old_nested"])
        self.assertTrue(kin[0]["new_nested"])
        self.assertTrue(kin[0]["verified_from_depth_product"])
        self.assertTrue(kin[0]["ok"])


class EndToEndTests(unittest.TestCase):
    def test_example_material_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_lakes(EXAMPLE, tmp)
        migration = report["migration"]
        self.assertEqual(migration["total_importance"], 4)
        self.assertEqual([t["name"] for t in migration["new_name_tags"]], ["青"])
        self.assertEqual([l["name"] for l in migration["lost_names"]], ["石"])

    def test_conflict_material_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_lakes(CONFLICT, tmp)
        migration = report["migration"]
        self.assertEqual(migration["total_importance"], 7)
        self.assertEqual([t["name"] for t in migration["new_name_tags"]], ["浅A", "深A"])
        self.assertEqual([l["name"] for l in migration["lost_names"]], ["浅B"])
        # The preserved A-system kinship is verifiable from the new depth grid.
        self.assertEqual(len(migration["kinship"]), 1)
        self.assertTrue(migration["kinship"][0]["verified_from_depth_product"])

    def test_depth_product_reads_signed_plan(self):
        material = load(EXAMPLE)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            catalog = Catalog(tmp / "c.db", 1, 5, 3)
            publisher = Publisher(tmp / "p.db", catalog)
            try:
                catalog.apply_batch(material["old"]["tiles"])
                plan = publisher.publish(
                    {"id": "x", "source_revision": 1, "opens": 0,
                     "total_max": 10**9, "cell_max": [], "outlets": []}
                )
                product = depth_product_from_plan(publisher, plan.plan_id)
                self.assertEqual([list(r) for r in product.depths], [[3, 1, 2, 0, 2]])
            finally:
                publisher.close()
                catalog.close()


class LadderReportShapeTests(unittest.TestCase):
    def test_report_is_complete_per_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_lakes(EXAMPLE, tmp)
        ladder = report["new_ladder"]
        self.assertEqual([t["threshold"] for t in ladder["thresholds"]], [1, 2])
        for entry in ladder["thresholds"]:
            for block in entry["blocks"]:
                self.assertEqual(block["area"], len(block["cells"]))
                self.assertIn("parent", block)
                self.assertIn("children", block)
                self.assertIn("provenance", block)
        # Deeper block's parent is a shallower block whose cells contain it.
        parents = {
            b["block_id"]: b["parent"]
            for t in ladder["thresholds"] for b in t["blocks"]
        }
        cells = {
            b["block_id"]: {tuple(c) for c in b["cells"]}
            for t in ladder["thresholds"] for b in t["blocks"]
        }
        for block_id, parent in parents.items():
            if parent is not None:
                self.assertLess(parent[:2], block_id[:2])  # shallower threshold
                self.assertTrue(cells[block_id] <= cells[parent])


if __name__ == "__main__":
    unittest.main()
