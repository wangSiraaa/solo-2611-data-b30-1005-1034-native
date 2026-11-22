"""Tests for the display-case tray calibration and exact balance planner.

The calibration tests build frozen PanCell values directly (the demo in
``examples/display_balance.py`` covers the full Catalog -> Publisher -> Pan
chain against real artifacts); the planner tests enumerate enough geometry to
prove optimality, plateau handling, signed arms and infeasibility, all with
exact rational arithmetic.
"""

from __future__ import annotations

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from slabwater import Catalog, Publisher
from slabwater.balance import InfeasibleBalance, Weight, plan_balance
from slabwater.tray import Pan, PanCell


def cell(cid, h, l, arm, plan_id="P-test", version=1, eh="hash"):
    return PanCell(
        cell=cid,
        area=Fraction(1),
        arm=Fraction(arm),
        ground=Fraction(h),
        level_ceiling=Fraction(l),
        sources=(),
        catalog_version=version,
        elevation_hash=eh,
        plan_id=plan_id,
    )


def make_pan(name, specs, plan_id="P-test"):
    return Pan(name, [cell(cid, h, l, arm, plan_id=plan_id) for cid, h, l, arm in specs],
               plan_id=plan_id, catalog_version=1, elevation_hash="hash")


class TrayTests(unittest.TestCase):
    def test_single_cell_water_and_moment(self):
        pan = make_pan("A", [((0, 0), 0, 3, 2)])
        self.assertEqual(pan.capacity, 3)
        self.assertEqual(pan.water_at(Fraction(3, 2)), Fraction(3, 2))
        self.assertEqual(pan.moment_at(Fraction(3, 2)), 3)
        self.assertEqual(len(pan.z_segments), 1)

    def test_ground_above_zero_dry_below_ground(self):
        pan = make_pan("A", [((0, 0), 1, 3, 2)])
        self.assertEqual(pan.z_min, 1)
        self.assertEqual(pan.water_at(1), 0)
        self.assertEqual(pan.capacity, 2)
        self.assertEqual(pan.moment_at(3), 4)

    def test_sequential_fill_piecewise_moment(self):
        # 低格 h=0 力臂 3；高格 h=2 力臂 -1；同 l=3。
        pan = make_pan("seq", [((0, 0), 0, 3, 3), ((0, 1), 2, 3, -1)])
        self.assertEqual(len(pan.z_segments), 2)
        # W=2 时高格仍干，力矩全来自低格。
        r = pan.lookup_water(2)
        self.assertEqual((r.z_lo, r.z_hi), (2, 2))
        self.assertEqual(pan.moment_at(r.z_lo), 6)
        # 按总量均分会得到 2，与精确值 6 不符。
        self.assertEqual(Fraction(2, 2) * 3 + Fraction(2, 2) * -1, 2)
        # 高格进水后力矩斜率变为两臂之和。
        r2 = pan.lookup_water(3)
        self.assertEqual(r2.z_lo, Fraction(5, 2))
        self.assertEqual(pan.moment_at(r2.z_lo), 7)

    def test_internal_dry_plateau_returns_z_interval(self):
        # 低格 z∈[0,1] 吃水即满；高格 h=2 才进水；z∈[1,2] 是干台。
        pan = make_pan("plateau", [((0, 0), 0, 1, 2), ((0, 1), 2, 3, 5)])
        plateau_segments = [s for s in pan.z_segments if s.is_plateau]
        self.assertTrue(plateau_segments)
        r = pan.lookup_water(1)
        self.assertEqual((r.z_lo, r.z_hi), (1, 2))
        self.assertEqual(r.moment_lo, r.moment_hi)
        # 平台上任意刻线水量与力矩都相同。
        for z in (1, Fraction(3, 2), 2):
            self.assertEqual(pan.water_at(z), 1)
            self.assertEqual(pan.moment_at(z), 2)

    def test_lookup_outside_capacity_rejected(self):
        pan = make_pan("A", [((0, 0), 0, 3, 2)])
        with self.assertRaises(ValueError):
            pan.lookup_water(4)
        with self.assertRaises(ValueError):
            pan.lookup_water(-1)

    def test_mark_line_outside_legal_range_rejected(self):
        pan = make_pan("A", [((0, 0), 1, 3, 2)])
        with self.assertRaises(ValueError):
            pan.water_at(Fraction(1, 2))

    def test_more_than_eight_kinks_rejected(self):
        specs = [((0, i), i, i + 11, 1) for i in range(9)]  # 9 格 -> 9 个转折
        with self.assertRaises(ValueError):
            make_pan("too-many", specs)

    def test_overlapping_cells_rejected(self):
        with self.assertRaises(ValueError):
            make_pan("dup", [((0, 0), 0, 1, 1), ((0, 0), 0, 1, 2)])

    def test_negative_arm_moment_sign(self):
        pan = make_pan("neg", [((0, 0), 0, 3, -4)])
        self.assertEqual(pan.moment_at(Fraction(1, 2)), -2)


class BalanceTests(unittest.TestCase):
    def setUp(self):
        # 左臂 0（h=0,l=3），右臂 2（h=1,l=3，容量 2）——题面工作例的几何。
        self.left = make_pan("左", [((0, 0), 0, 3, 0)], plan_id="P1")
        self.right = make_pan("右", [((0, 1), 1, 3, 2)], plan_id="P1")

    def test_scenario_one_move_two_no_weight(self):
        plan = plan_balance([self.left, self.right], [3, 0],
                            [Weight("W", 2, 2)], target_moment=4)
        self.assertEqual(plan.total_cost, 2)
        self.assertEqual(plan.hung, ())
        self.assertEqual((plan.trays[0].water, plan.trays[1].water), (1, 2))
        self.assertEqual(plan.net_moment, 4)
        self.assertEqual(plan.transfer_cost, 2)

    def test_scenario_two_capacity_one_forces_weight(self):
        # 右盘地面抬到 2：容量 1。
        right2 = make_pan("右2", [((0, 1), 2, 3, 2)], plan_id="P2")
        plan = plan_balance([self.left, right2], [3, 0],
                            [Weight("W", 2, 2)], target_moment=4)
        self.assertEqual(plan.total_cost, 3)
        self.assertEqual(len(plan.hung), 1)
        self.assertEqual((plan.trays[0].water, plan.trays[1].water), (2, 1))

    def test_scenario_three_half_fee_weight_wins(self):
        plan = plan_balance([self.left, self.right], [3, 0],
                            [Weight("W", 2, Fraction(1, 2))], target_moment=4)
        self.assertEqual(plan.total_cost, Fraction(3, 2))
        self.assertEqual(len(plan.hung), 1)
        self.assertEqual(plan.weight_fee, Fraction(1, 2))

    def test_infeasible_target_raises(self):
        with self.assertRaises(InfeasibleBalance):
            plan_balance([self.left, self.right], [3, 0], [], target_moment=10)

    def test_total_water_conserved_and_capacity_respected(self):
        plan = plan_balance([self.left, self.right], [3, 0],
                            [Weight("W", 2, 2)], target_moment=4)
        total = sum(t.water for t in plan.trays)
        self.assertEqual(total, 3)
        for tray, final in zip((self.left, self.right), plan.trays):
            self.assertLessEqual(final.water, tray.capacity)

    def test_transfer_cost_bills_each_unit_once(self):
        # 三盘：A、B 力臂 1，C 力臂 0；总量 4 守恒，目标力矩 2 要求带臂盘
        # 共留 2 单位，即向 C 移 2 单位，费用只计这 2 个单位一次。
        a = make_pan("A", [((0, 0), 0, 4, 1)], plan_id="P3")
        b = make_pan("B", [((0, 1), 0, 4, 1)], plan_id="P3")
        c = make_pan("C", [((0, 2), 0, 4, 0)], plan_id="P3")
        plan = plan_balance([a, b, c], [2, 2, 0], [], target_moment=2)
        moved = sum(x for _, _, x in plan.transfers)
        self.assertEqual(moved, plan.transfer_cost)
        self.assertEqual(plan.transfer_cost, 2)
        self.assertEqual(sum(t.water for t in plan.trays), 4)
        self.assertEqual(plan.trays[2].water, 2)

    def test_signed_arms_and_negative_weight(self):
        pan = make_pan("N", [((0, 0), 0, 3, -2)], plan_id="P4")
        # 起始 2 单位、力矩 -4；目标 -2 需要水量 1（排出 1，但只有一盘时
        # 总量必须守恒——改用零费用保持并挂负砝码达到目标）。
        plan = plan_balance([pan], [2], [Weight("down", 2, Fraction(1, 3))],
                            target_moment=-2)
        self.assertEqual(plan.trays[0].water, 2)
        self.assertEqual(plan.net_moment, -2)
        self.assertEqual(plan.total_cost, Fraction(1, 3))

    def test_kink_crossing_needs_no_single_interpolation_assumption(self):
        # 盘内两格先后进水（h=0 臂 1，h=2 臂 3），目标力矩落在第二段。
        pan = make_pan("K", [((0, 0), 0, 3, 1), ((0, 1), 2, 3, 3)], plan_id="P5")
        partner = make_pan("Q", [((0, 2), 0, 3, 0)], plan_id="P5")
        # 总水 4：K 取 3（刻线 5/2，力矩 = 2.5*1 + 0.5*3 = 4），Q 取 1。
        plan = plan_balance([pan, partner], [1, 3], [], target_moment=4)
        self.assertEqual(plan.trays[0].water, 3)
        self.assertEqual(plan.trays[0].z_lo, Fraction(5, 2))
        self.assertEqual(plan.trays[0].moment, 4)

    def test_plateau_volume_reaches_balance_with_z_choice(self):
        # 干台盘 W=1 对应整段 z∈[1,2]；配平结果应返回区间而非单点。
        pan = make_pan("P", [((0, 0), 0, 1, 2), ((0, 1), 2, 3, 5)], plan_id="P6")
        partner = make_pan("Q", [((0, 2), 0, 3, 0)], plan_id="P6")
        plan = plan_balance([pan, partner], [1, 2], [], target_moment=2)
        final_pan = plan.trays[0]
        self.assertEqual(final_pan.water, 1)
        self.assertEqual((final_pan.z_lo, final_pan.z_hi), (1, 2))


class CatalogPublisherFreezeTests(unittest.TestCase):
    def test_pans_bind_to_frozen_plan_and_survive_republish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            catalog = Catalog(tmp / "c.db", 1, 2, 3)
            publisher = Publisher(tmp / "p.db", catalog)
            catalog.apply_batch(
                [{"tile_id": "g", "revision": 1, "row": 0, "col": 0,
                  "values": [[0, 1]]}]
            )
            artifact = publisher.publish({
                "id": "R1", "source_revision": 1, "opens": 0, "total_max": 99,
                "cell_max": [], "outlets": [{"id": "E", "cell": [0, 0], "sill": 0}],
            }).artifact
            surface = catalog.get_slice(1)
            pan = Pan.from_plan("冻结盘", surface, artifact, [((0, 1), 2)])
            self.assertEqual(pan.cells[0].ground, 1)
            self.assertEqual(pan.cells[0].level_ceiling, 3)
            # 新切面与重发不改旧盘标定。
            catalog.apply_batch(
                [{"tile_id": "g", "revision": 2, "row": 0, "col": 0,
                  "values": [[0, 2]]}]
            )
            publisher.publish({
                "id": "R2", "source_revision": 2, "opens": 0, "total_max": 99,
                "cell_max": [], "outlets": [{"id": "E", "cell": [0, 0], "sill": 0}],
            })
            self.assertEqual(pan.cells[0].ground, 1)
            self.assertEqual(pan.catalog_version, 1)
            reopened = publisher.reopen(artifact["plan_id"]).artifact
            self.assertEqual(reopened["levels"], artifact["levels"])
            publisher.close()
            catalog.close()


if __name__ == "__main__":
    unittest.main()
