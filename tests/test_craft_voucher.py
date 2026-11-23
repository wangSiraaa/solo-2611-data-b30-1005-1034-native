"""工艺凭单：候选比例筛选、同实样横纵联合、修订与退校。"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from craftbench import build_voucher, load_scenario, parse_scenario
from craftbench.errors import CraftError
from craftbench.ratio import fraction_text
from craftbench.voucher import (
    fired_dims,
    joint_dimensions,
    resolve_proof_revisions,
)
from craftbench.model import ProofObservation

FIX = Path("fixtures/craftbench")


def _proof(proof_id, sample, direction, green, observed, signed=True, revision=1):
    lo, hi = observed if observed is not None else (0, 0)
    return ProofObservation(proof_id, sample, direction, Fraction(green),
                            Fraction(lo), Fraction(hi), signed, revision)


def test_base_observation_keeps_both_pairs():
    sc = load_scenario(FIX / "two-sleeve-base.json")
    voucher = build_voucher(sc)
    assert voucher.accepted
    assert voucher.surviving_pairs("S") == [0, 1]
    sample = voucher.style_verdicts["S"].samples[0]
    assert {pv.pair_index: pv.status for pv in sample.pair_verdicts} == {0: "KEPT", 1: "KEPT"}


def test_point_observation_excludes_four_fifths_with_signed_reason():
    # 校样改为点区间 [5,5]：4/5 => 4 不在 [5,5]，组 1 被排除且有据可查。
    sc = load_scenario(FIX / "two-sleeve-base.json")
    small = sc.piece_by_id()["small"]
    new_proof = _proof("small|sm1|w", "sm1", "w", 5, (5, 5))
    small = type(small)(small.piece_id, small.style_id, small.green, (new_proof,),
                        small.allow_rotation)
    style = sc.styles[0]
    style = type(style)(style.style_id, style.candidate_ratios, style.allow_rotation,
                        (small, sc.piece_by_id()["large"]))
    sc2 = type(sc)(sc.scenario_id, (style,), sc.racks, sc.objective,
                   (small, sc.piece_by_id()["large"]))
    voucher = build_voucher(sc2)
    assert voucher.surviving_pairs("S") == [0]
    excluded = next(pv for pv in voucher.style_verdicts["S"].samples[0].pair_verdicts
                    if pv.pair_index == 1)
    assert excluded.status == "EXCLUDED"
    assert "4" in excluded.checks[0].reason and "5..5" in excluded.checks[0].reason


def test_sample_horizontal_and_vertical_act_on_same_whole_pair():
    # 同一样：横向只放行 pair0、纵向只放行 pair1 => 整组都不成立，退校。
    # 证明横纵不能跨组拼接：pair0 过不了纵，pair1 过不了横。
    raw = {
        "scenario_id": "joint",
        "styles": [{
            "style_id": "J", "allow_rotation": False,
            "candidate_ratios": [["1", "1/2"], ["1/2", "1"]],
            "pieces": [{
                "piece_id": "j1",
                "green": {"outer_w": "4", "outer_d": "4",
                          "inner_w": "1", "inner_d": "1"},
                "proofs": [
                    {"proof_id": "j1|s|w", "sample_id": "s", "direction": "w",
                     "green_dim": "4", "observed": ["4", "4"], "signed": True,
                     "revision": 1},
                    {"proof_id": "j1|s|d", "sample_id": "s", "direction": "d",
                     "green_dim": "4", "observed": ["4", "4"], "signed": True,
                     "revision": 1},
                ],
            }],
        }],
        "racks": [],
        "objective": {"mode": "min_boxes", "box_cap": None, "fee_budget": None},
    }
    voucher = build_voucher(parse_scenario(raw))
    assert not voucher.accepted
    assert voucher.style_verdicts["J"].samples[0].surviving_indices == []


def test_different_samples_may_establish_different_pairs():
    # 两个实样各印证一组，款级存活取并集 => 两组都活。
    raw = {
        "scenario_id": "multi-sample",
        "styles": [{
            "style_id": "M", "allow_rotation": False,
            "candidate_ratios": [["1", "1"], ["1/2", "1/2"]],
            "pieces": [{
                "piece_id": "m1",
                "green": {"outer_w": "4", "outer_d": "4",
                          "inner_w": "1", "inner_d": "1"},
                "proofs": [
                    {"proof_id": "m1|a|w", "sample_id": "a", "direction": "w",
                     "green_dim": "4", "observed": ["4", "4"], "signed": True,
                     "revision": 1},
                    {"proof_id": "m1|b|w", "sample_id": "b", "direction": "w",
                     "green_dim": "4", "observed": ["2", "2"], "signed": True,
                     "revision": 1},
                ],
            }],
        }],
        "racks": [],
        "objective": {"mode": "min_boxes", "box_cap": None, "fee_budget": None},
    }
    voucher = build_voucher(parse_scenario(raw))
    assert voucher.accepted
    assert voucher.surviving_pairs("M") == [0, 1]
    by_sample = {s.sample_id: s.surviving_indices
                 for s in voucher.style_verdicts["M"].samples}
    assert by_sample == {"a": [0], "b": [1]}


def test_horizontal_only_reply_keeps_multiple_pairs():
    raw = {
        "scenario_id": "h-only",
        "styles": [{
            "style_id": "H", "allow_rotation": False,
            "candidate_ratios": [["1", "1"], ["1/2", "1/2"]],
            "pieces": [{
                "piece_id": "h1",
                "green": {"outer_w": "4", "outer_d": "4",
                          "inner_w": "1", "inner_d": "1"},
                "proofs": [
                    {"proof_id": "h1|s|w", "sample_id": "s", "direction": "w",
                     "green_dim": "4", "observed": ["2", "4"], "signed": True,
                     "revision": 1},
                ],
            }],
        }],
        "racks": [],
        "objective": {"mode": "min_boxes", "box_cap": None, "fee_budget": None},
    }
    voucher = build_voucher(parse_scenario(raw))
    assert voucher.surviving_pairs("H") == [0, 1]  # 纵向无观察，不约束


def test_unsigned_placeholder_never_constrains():
    sc = load_scenario(FIX / "cross-axis-demo.json")
    voucher = build_voucher(sc)
    placeholders = voucher.style_verdicts["T"].ignored_placeholders
    assert any(p["proof_id"] == "outer|bmiss|d" for p in placeholders)
    # 缺测纵向没有把任何组排掉。
    assert voucher.surviving_pairs("T") == [0, 1]


def test_revision_resolution_highest_wins_conflict_rejects():
    good = _proof("p|s|w", "s", "w", 5, (5, 5), revision=2)
    old = _proof("p|s|w", "s", "w", 5, (4, 5), revision=1)
    winners = resolve_proof_revisions((old, good))
    assert len(winners) == 1 and winners[0].observed_lo == Fraction(5)

    clash = _proof("p|s|w", "s", "w", 5, (3, 3), revision=2)
    with pytest.raises(CraftError):
        resolve_proof_revisions((good, clash))


def test_joint_dimensions_same_and_separate_kiln():
    sc = load_scenario(FIX / "two-sleeve-base.json")
    voucher = build_voucher(sc)
    joint = joint_dimensions(sc, voucher, "small", "large")
    # 同炉锁同一组 => 2 个炉况；异炉 2x2 => 4 个。
    assert len(joint["same_kiln"]) == 2
    assert len(joint["separate_kiln"]) == 4
    styles = sc.style_by_id()
    # 同炉 pair1(4/5)：小外 4 < 大内 24/5。
    same1 = next(c for c in joint["same_kiln"] if c["pair_index"] == 1)
    assert same1["small"]["outer_w"] == "4"
    assert same1["large"]["inner_w"] == "24/5"
    # 点值精确，无浮点。
    dims = fired_dims(sc.piece_by_id()["large"], styles["S"], 1)
    assert fraction_text(dims.inner_w) == "24/5"
