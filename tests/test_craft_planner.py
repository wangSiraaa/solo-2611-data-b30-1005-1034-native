"""装窑/装箱联合工单测试：分炉独立性、同炉锁组、严格擦边、朝向、目标函数。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from craftbench import build_voucher, load_scenario, parse_scenario, plan_work_order
from craftbench.errors import PlanningError
from craftbench.model import Objective
from craftbench.planner import (
    AS_IS,
    ROTATED,
    axiswise_worst_check,
    evaluate_edge,
    iter_kiln_assignments,
    joint_kiln_conditions,
)

FIX = Path("fixtures/craftbench")


def _revised_two_sleeve():
    """[5,5] 点实读：只剩比例 1；普通分炉即可一箱零架费。"""
    sc = load_scenario(FIX / "two-sleeve-base.json")
    from fractions import Fraction
    from craftbench.model import ProofObservation
    small = sc.piece_by_id()["small"]
    proof = ProofObservation("small|sm1|w", "sm1", "w", Fraction(5),
                             Fraction(5), Fraction(5), True, 2)
    small = type(small)(small.piece_id, small.style_id, small.green, (proof,),
                        small.allow_rotation)
    style = sc.styles[0]
    large = sc.piece_by_id()["large"]
    style = type(style)(style.style_id, style.candidate_ratios, style.allow_rotation,
                        (small, large))
    return type(sc)(sc.scenario_id, (style,), sc.racks, sc.objective, (small, large))


def test_base_order_prefers_one_box_with_rack_fee_two():
    sc = load_scenario(FIX / "two-sleeve-base.json")
    order = plan_work_order(sc, build_voucher(sc))
    assert (order.box_count, order.rack_fee) == (1, 2)
    assert order.chains == [["small", "large"]]          # 内 -> 外
    assert order.edges[0].same_kiln is True
    assert order.assignment.rack_uses[0].rack_id == "R1"


def test_zero_fee_separate_kilns_cannot_guarantee_one_box_at_base():
    sc = load_scenario(FIX / "two-sleeve-base.json")
    voucher = build_voucher(sc)
    solo = next(a for a in iter_kiln_assignments(sc) if not a.rack_uses)
    edge = evaluate_edge(sc, voucher, solo, "small", "large")
    # 分炉存在 small@1, large@4/5 => 5 > 24/5 的反例炉况。
    assert edge is None
    combos = joint_kiln_conditions(sc, voucher, solo, "small", "large")
    bad = next(c for c in combos if c.inner_pair == 0 and c.outer_pair == 1)
    assert bad.same_kiln is False


def test_revised_separate_kilns_give_one_box_zero_fee():
    sc = _revised_two_sleeve()
    order = plan_work_order(sc, build_voucher(sc))
    assert (order.box_count, order.rack_fee) == (1, 0)
    assert all(not g.is_rack for g in order.assignment.groups)
    assert order.edges[0].same_kiln is False


def test_strict_inequality_touching_is_rejected():
    # 让内件外口恰等于外件内口的一个炉况 => 擦边即不许，边不成立。
    raw = {
        "scenario_id": "touch",
        "styles": [{
            "style_id": "K", "allow_rotation": False,
            "candidate_ratios": [["1", "1"], ["3/4", "3/4"]],
            "pieces": [
                {"piece_id": "in",
                 "green": {"outer_w": "6", "outer_d": "6",
                           "inner_w": "1", "inner_d": "1"},
                 "proofs": []},
                {"piece_id": "out",
                 "green": {"outer_w": "10", "outer_d": "10",
                           "inner_w": "8", "inner_d": "8"},
                 "proofs": []},
            ],
        }],
        "racks": [],
        "objective": {"mode": "min_boxes", "box_cap": None, "fee_budget": None},
    }
    sc = parse_scenario(raw)
    voucher = build_voucher(sc)
    solo = next(iter_kiln_assignments(sc))
    # in@1 => 外口 6；out@3/4 => 内口 6：严格小于失败（6 < 6 为假）。
    assert evaluate_edge(sc, voucher, solo, "in", "out") is None


def test_rotation_orientation_options():
    raw = {
        "scenario_id": "rot",
        "styles": [{
            "style_id": "R", "allow_rotation": True,
            "candidate_ratios": [["1", "1"]],
            "pieces": [
                {"piece_id": "in",
                 "green": {"outer_w": "2", "outer_d": "3",
                           "inner_w": "1", "inner_d": "1"},
                 "proofs": []},
                {"piece_id": "out",
                 "green": {"outer_w": "9", "outer_d": "9",
                           "inner_w": "4", "inner_d": "4"},
                 "proofs": []},
            ],
        }],
        "racks": [],
        "objective": {"mode": "min_boxes", "box_cap": None, "fee_budget": None},
    }
    sc = parse_scenario(raw)
    voucher = build_voucher(sc)
    solo = next(iter_kiln_assignments(sc))
    edge = evaluate_edge(sc, voucher, solo, "in", "out")
    assert edge is not None
    # 原向：3 < 4 过；转 90 度后 w/d 互换也是候选朝向。
    assert (AS_IS, AS_IS) in edge.all_orientation_options
    assert any(ori == ROTATED for pair in edge.all_orientation_options for ori in pair)


def test_no_rotation_style_only_offers_as_is():
    sc = load_scenario(FIX / "two-sleeve-base.json")
    voucher = build_voucher(sc)
    rack = next(a for a in iter_kiln_assignments(sc) if a.rack_uses
                and len(a.rack_uses[0].seats_used) == 2)
    edge = evaluate_edge(sc, voucher, rack, "small", "large")
    assert edge.all_orientation_options == ((AS_IS, AS_IS),)


def test_min_fee_mode_under_box_cap():
    sc = load_scenario(FIX / "three-sleeve-cap.json")
    order = plan_work_order(sc, build_voucher(sc))
    assert order.box_count <= 1
    assert order.rack_fee == 1  # 免费分炉只给两箱，达不了 1 箱上限
    assert [u.rack_id for u in order.assignment.rack_uses] == ["RB"]
    assert order.chains == [["p1", "p2", "p3"]]


def test_min_boxes_then_fee_tie_break():
    sc = load_scenario(FIX / "three-sleeve-cap.json")
    obj = Objective(mode="min_boxes", box_cap=None, fee_budget=None)
    sc2 = type(sc)(sc.scenario_id, sc.styles, sc.racks, obj, sc.pieces)
    order = plan_work_order(sc2, build_voucher(sc2))
    assert order.box_count == 1
    assert order.rack_fee == 1  # 同为一箱，RB(1) 比 RA(3) 省


def test_fee_budget_filters_assignments():
    sc = load_scenario(FIX / "three-sleeve-cap.json")
    obj = Objective(mode="min_boxes", box_cap=None, fee_budget=0)
    sc2 = type(sc)(sc.scenario_id, sc.styles, sc.racks, obj, sc.pieces)
    order = plan_work_order(sc2, build_voucher(sc2))
    assert order.rack_fee == 0 and order.box_count == 2


def test_infeasible_box_cap_raises():
    sc = load_scenario(FIX / "three-sleeve-cap.json")
    obj = Objective(mode="min_fee", box_cap=0, fee_budget=None)  # 至少 1 箱
    sc2 = type(sc)(sc.scenario_id, sc.styles, sc.racks, obj, sc.pieces)
    with pytest.raises(PlanningError):
        plan_work_order(sc2, build_voucher(sc2))


def test_axiswise_worst_case_fictionalizes_shared_kiln_condition():
    sc = load_scenario(FIX / "cross-axis-demo.json")
    voucher = build_voucher(sc)
    # 正确的整组检查：共架同组两炉况都能套，给一箱。
    order = plan_work_order(sc, voucher)
    assert (order.box_count, order.rack_fee) == (1, 1)
    # 错误对照：横纵各取最坏拼出 (2,2) 的虚构内件开口，判不可套。
    assert axiswise_worst_check(sc, voucher, "inner", "outer") is False


def test_assignment_enumeration_is_deduped_and_covers_solo_and_rack():
    sc = load_scenario(FIX / "two-sleeve-base.json")
    assignments = list(iter_kiln_assignments(sc))
    # 4 个互异划分：全分炉；只 large 上架；只 small 上架；两件同架。
    assert len(assignments) == 4
    assert any(not a.rack_uses for a in assignments)
    assert any(
        a.rack_uses and len(a.rack_uses[0].seats_used) == 2
        for a in assignments
    )
    # 去重保证：同一 (件 -> 炉位) 划分不重复出现。
    signatures = [
        tuple(sorted((pid, a.group_of[pid]) for pid in a.group_of))
        for a in assignments
    ]
    assert len(signatures) == len(set(signatures))


def test_rack_fee_charged_once_even_with_empty_seats():
    sc = load_scenario(FIX / "two-sleeve-base.json")
    # 只把一件放上双位架：空一席，仍付整笔架费 2；一件只占一次。
    partial = next(
        a for a in iter_kiln_assignments(sc)
        if a.rack_uses and len(a.rack_uses[0].seats_used) == 1
    )
    assert partial.total_fee == 2
    seated = [pid for u in partial.rack_uses for pid in u.seats_used]
    assert len(seated) == len(set(seated))


def test_two_outer_pieces_cannot_share_one_inner():
    # 一个内件只能进一条链：森林度约束保证它不被两个外件同时占用。
    raw = {
        "scenario_id": "share",
        "styles": [{
            "style_id": "V", "allow_rotation": False,
            "candidate_ratios": [["1", "1"]],
            "pieces": [
                {"piece_id": "core",
                 "green": {"outer_w": "1", "outer_d": "1",
                           "inner_w": "1", "inner_d": "1"},
                 "proofs": []},
                {"piece_id": "o1",
                 "green": {"outer_w": "5", "outer_d": "5",
                           "inner_w": "3", "inner_d": "3"},
                 "proofs": []},
                {"piece_id": "o2",
                 "green": {"outer_w": "6", "outer_d": "6",
                           "inner_w": "4", "inner_d": "4"},
                 "proofs": []},
            ],
        }],
        "racks": [],
        "objective": {"mode": "min_boxes", "box_cap": None, "fee_budget": None},
    }
    sc = parse_scenario(raw)
    order = plan_work_order(sc, build_voucher(sc))
    inner_used = [e.inner_id for e in order.edges]
    assert sorted(inner_used) == sorted(set(inner_used))  # 无内件被复用
    assert order.box_count == 2  # core 套进一个外件，另一个外件独立成箱
