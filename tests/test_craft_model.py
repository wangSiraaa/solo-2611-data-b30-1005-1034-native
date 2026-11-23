"""情景解析、硬上限与结构校验测试。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from craftbench.errors import CraftError
from craftbench.model import load_scenario, parse_scenario

FIX = Path("fixtures/craftbench")


def test_load_base_scenario():
    sc = load_scenario(FIX / "two-sleeve-base.json")
    assert sc.scenario_id == "two-sleeve-base"
    assert {p.piece_id for p in sc.pieces} == {"small", "large"}
    assert sc.racks[0].seats == 2 and sc.racks[0].fee == 2
    assert sc.racks[0].admits(sc.piece_by_id()["small"])


def test_too_many_pieces_rejected():
    raw = json.loads((FIX / "two-sleeve-base.json").read_text(encoding="utf-8"))
    style = raw["styles"][0]
    base_piece = copy.deepcopy(style["pieces"][0])
    style["pieces"] = []
    for i in range(7):  # 超过六件硬上限
        piece = copy.deepcopy(base_piece)
        piece["piece_id"] = f"p{i}"
        piece["proofs"] = []
        style["pieces"].append(piece)
    with pytest.raises(CraftError):
        parse_scenario(raw)


def test_too_many_ratios_rejected():
    raw = json.loads((FIX / "two-sleeve-base.json").read_text(encoding="utf-8"))
    raw["styles"][0]["candidate_ratios"] = [
        ["1", "1"], ["1/2", "1/2"], ["1/3", "1/3"], ["1/4", "1/4"]
    ]
    with pytest.raises(CraftError):
        parse_scenario(raw)


def test_rack_must_be_same_style_and_known_pieces():
    raw = json.loads((FIX / "two-sleeve-base.json").read_text(encoding="utf-8"))
    raw["racks"][0]["allowed_pieces"] = ["small", "ghost"]
    with pytest.raises(CraftError):
        parse_scenario(raw)


def test_unsigned_proof_is_parsed_but_marked_placeholder():
    raw = json.loads((FIX / "cross-axis-demo.json").read_text(encoding="utf-8"))
    sc = parse_scenario(raw)
    outer = sc.piece_by_id()["outer"]
    unsigned = [p for p in outer.proofs if not p.is_signed]
    assert len(unsigned) == 1 and unsigned[0].sample_id == "bmiss"


def test_negative_green_rejected():
    raw = json.loads((FIX / "two-sleeve-base.json").read_text(encoding="utf-8"))
    raw["styles"][0]["pieces"][0]["green"]["outer_w"] = "-5"
    with pytest.raises(CraftError):
        parse_scenario(raw)


def test_cross_style_rack_rejected():
    raw = {
        "scenario_id": "mix",
        "racks": [{"rack_id": "X", "style_id": "A", "seats": 2,
                   "allowed_pieces": ["a1", "b1"], "fee": 1}],
        "styles": [
            {"style_id": "A", "allow_rotation": False,
             "candidate_ratios": [["1", "1"]],
             "pieces": [{"piece_id": "a1",
                         "green": {"outer_w": "1", "outer_d": "1",
                                   "inner_w": "1", "inner_d": "1"}, "proofs": []}]},
            {"style_id": "B", "allow_rotation": False,
             "candidate_ratios": [["1", "1"]],
             "pieces": [{"piece_id": "b1",
                         "green": {"outer_w": "1", "outer_d": "1",
                                   "inner_w": "1", "inner_d": "1"}, "proofs": []}]},
        ],
        "objective": {"mode": "min_boxes", "box_cap": None, "fee_budget": None},
    }
    with pytest.raises(CraftError):
        parse_scenario(raw)


def test_too_many_racks_rejected():
    raw = json.loads((FIX / "two-sleeve-base.json").read_text(encoding="utf-8"))
    raw["racks"] = [
        {"rack_id": f"R{i}", "style_id": "S", "seats": 1,
         "allowed_pieces": ["small"], "fee": 1}
        for i in range(4)  # 超过三张付费承架硬上限
    ]
    with pytest.raises(CraftError):
        parse_scenario(raw)
