"""Choir audition evidence and whole-piece scoring tests.

These tests drive real files through :class:`IngestionLibrary` and only read
evidence from the published snapshot, matching the demo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from extcontract import (
    IngestionLibrary,
    coach_query,
    load_contract,
    parse_piece,
    parse_scores,
    parse_singers,
    proven_regions,
    solve,
)
from extcontract.choir import Phrase, Piece, Voice, merge_regions, Region

ROOT = Path(__file__).resolve().parents[1]
CHOIR = ROOT / "materials" / "choir"


def _doc(name):
    return json.loads((CHOIR / name).read_text(encoding="utf-8"))


WRITER = load_contract(CHOIR / "writer-contract.json", role="writer")
WRITER_NO_END = load_contract(CHOIR / "writer-contract-omit-ending.json", role="writer")
READER = load_contract(CHOIR / "reader-contract.json", role="reader")
PIECE = parse_piece(_doc("piece.json"))
SCORES = parse_scores(_doc("scores.json"))
SINGERS = parse_singers(_doc("singers.json"))
BAND_7 = set(_doc("band-only-7.json")["available_overpass_pitches"])


def _library(*names, no_ending=False):
    library = IngestionLibrary(READER)
    results = []
    for name in names:
        writer = WRITER_NO_END if (no_ending and "ending-omitted" in name) else WRITER
        results.append(library.ingest(writer, _doc(name)))
    return library, results


def _original():
    library, results = _library("auditions-original.json")
    (result,) = results
    assert result.accepted, result.errors
    return library.publish()


def _regions(published):
    return {s: [[r.low, r.high] for r in rs] for s, rs in proven_regions(published).items()}


# -- evidence ---------------------------------------------------------------

def test_original_auditions_prove_expected_ranges():
    published = _original()
    assert _regions(published) == {
        "甲": [[5, 6], [9, 9]],
        "乙": [[8, 8]],
        "丙": [[5, 8]],
    }


def test_adjacent_but_disjoint_ranges_do_not_merge():
    # 5..6 and 7..8 are adjacent but share no pitch.
    merged = merge_regions([Region(5, 6, ("a",)), Region(7, 8, ("b",))])
    assert [[r.low, r.high] for r in merged] == [[5, 6], [7, 8]]
    # 5..6 and 6..8 share pitch 6 and merge into 5..8.
    merged = merge_regions([Region(5, 6, ("a",)), Region(6, 8, ("b",))])
    assert [[r.low, r.high] for r in merged] == [[5, 8]]
    assert merged[0].sources == ("a", "b")


def test_coach_cannot_paint_isolated_notes():
    published = _original()
    regions = proven_regions(published)
    # 6 and 9 are both 甲's, but no region covers the span between them.
    assert all(not (r.low <= 6 and 9 <= r.high) for r in regions["甲"])
    query = coach_query(published)
    audit = {a["audition"]: a for a in query["auditions"]}
    assert audit["A-JIA-9"]["status"] == "proven_point"


def test_default_ending_is_not_a_signature():
    library, (result,) = _library("auditions-ending-omitted.json", no_ending=True)
    assert result.accepted, result.errors
    published = library.publish()
    ending_source = published["rows"][-1]["sources"]["ending"]
    assert ending_source.get("conversion") == "fill_default"
    assert proven_regions(published) == {}
    assert coach_query(published)["auditions"][0]["status"] == "no_evidence"


def test_actual_ending_is_a_signature():
    library, (result,) = _library("auditions-ending-signed.json")
    assert result.accepted, result.errors
    published = library.publish()
    assert "default" not in published["rows"][-1]["sources"]["ending"]
    assert _regions(published) == {"甲": [[5, 6]]}


@pytest.mark.parametrize("audition", [
    "A-JIA-LEAP", "A-JIA-GAP", "A-JIA-UNSIGNED",
])
def test_defects_sign_nothing(audition):
    library, (result,) = _library("auditions-defects.json")
    assert result.accepted, result.errors
    query = coach_query(library.publish())
    audit = {a["audition"]: a for a in query["auditions"]}[audition]
    assert audit["status"] == "no_evidence"
    assert proven_regions(library.publish()) == {}


def test_same_audition_same_position_conflict_rejected_atomically():
    library, (result,) = _library("auditions-conflict.json")
    assert not result.accepted
    assert any("same revision" in e for e in result.errors)
    assert library.row_count() == 0


def test_same_audition_same_position_conflict_across_row_ids_signs_nothing():
    # Distinct row ids, same audition id and seq, different measured pitches:
    # the ingestion layer accepts it, but the evidence layer must still refuse.
    library, (result,) = _library("auditions-conflict-seq.json")
    assert result.accepted, result.errors
    query = coach_query(library.publish())
    audit = {a["audition"]: a for a in query["auditions"]}["A-JIA-SEQ-CLASH"]
    assert audit["status"] == "no_evidence"
    assert any("同号同序冲突" in r for r in audit["reasons"])
    assert proven_regions(library.publish()) == {}


# -- performance ------------------------------------------------------------

def test_original_key_switch_handover_at_rest():
    regions = proven_regions(_original())
    result = solve(PIECE, [s for s in SCORES if s.score_id == "S0"], SINGERS, regions,
                   mode="switch", cap=1)
    assert result["feasible"] and result["score_id"] == "S0"
    assert result["switches"] == 1 and result["cost"] == 0
    assert [p["singer"] for p in result["phrases"]] == ["甲", "乙"]


def test_no_switch_original_key_requires_paid_guest():
    regions = proven_regions(_original())
    result = solve(PIECE, [s for s in SCORES if s.score_id == "S0"], SINGERS, regions,
                   mode="switch", cap=0)
    assert result["feasible"] and result["switches"] == 0
    assert result["cost"] == 3
    assert {p["singer"] for p in result["phrases"]} == {"丙"}


def test_budget_minimises_switches_then_cost():
    regions = proven_regions(_original())
    result = solve(PIECE, SCORES, SINGERS, regions, mode="budget", cap=1)
    assert result["feasible"] and result["score_id"] == "S1"
    assert result["switches"] == 0 and result["cost"] == 1
    assert [p["actual"] for p in result["phrases"]] == [[6, 6], [9, 9]]


def test_guest_pays_once_for_whole_piece():
    regions = proven_regions(_original())
    result = solve(PIECE, [s for s in SCORES if s.score_id == "S0"], SINGERS, regions,
                   mode="switch", cap=0)
    # 丙 sings both phrases but is charged its single fixed fee once.
    assert sum(result["guest_fees"].values()) == 3
    assert result["cost"] == 3


def test_supplemented_auditions_allow_free_original():
    library, results = _library("auditions-original.json", "auditions-jia-supplement.json")
    assert all(r.accepted for r in results)
    regions = proven_regions(library.publish())
    assert [[r.low, r.high] for r in regions["甲"]] == [[5, 8], [9, 9]]
    result = solve(PIECE, [s for s in SCORES if s.score_id == "S0"], SINGERS, regions,
                   mode="switch", cap=0)
    assert result["feasible"] and result["cost"] == 0 and result["switches"] == 0
    assert {p["singer"] for p in result["phrases"]} == {"甲"}


def test_band_restriction_forces_original_guest():
    regions = proven_regions(_original())
    result = solve(PIECE, SCORES, SINGERS, regions, band=BAND_7, mode="switch", cap=0)
    assert result["feasible"] and result["score_id"] == "S0"
    assert result["cost"] == 3 and result["switches"] == 0
    assert result["overpasses"][0]["actual"] == 7


def test_one_long_phrase_6_to_9_is_not_bluffable():
    one_long = Piece(
        piece_id="one-long", title="连续6-9单句",
        voices=(Voice("V-SOLO", "独唱", (Phrase("V-SOLO", "P1", 6, 9, 0, False),)),),
        overpasses=(),
    )
    regions = proven_regions(_original())
    result = solve(one_long, [s for s in SCORES if s.score_id == "S1"], SINGERS, regions,
                   mode="switch", cap=0)
    assert not result["feasible"]


def test_isolated_5_and_9_have_no_whole_piece_score():
    library, (result,) = _library("auditions-wu-59.json")
    assert result.accepted, result.errors
    regions = proven_regions(library.publish())
    assert [[r.low, r.high] for r in regions["戊"]] == [[5, 5], [9, 9]]
    performance = solve(PIECE, SCORES, SINGERS, regions, available=["戊"],
                        mode="switch", cap=0)
    assert not performance["feasible"]
