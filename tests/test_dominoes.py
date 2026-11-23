"""Tests for rubbing ingestion, whole-tile restoration, and track planning."""

from __future__ import annotations

import pytest

from extcontract import (
    Bridge,
    FixedRubbingContract,
    RubbingLibrary,
    TileEdge,
    minimum_bridges_for_tracks,
    minimize_tracks,
    possible_directed_end_pairs,
    reconstruct_all,
    tile_edges_from_reconstructions,
)

DICTIONARY = ("A", "B", "C", "D", "E", "F")


def contract(writer="dw1", reader="dr1"):
    return FixedRubbingContract(writer, reader, DICTIONARY)


def doc(file_id, rows, writer="dw1", reader="dr1"):
    return {
        "file_id": file_id,
        "writer_version": writer,
        "reader_version": reader,
        "rubbings": rows,
    }


def rubbing(tile, rid, left, right, rivet=None, revision=1):
    return {
        "tile_id": tile,
        "rubbing_id": rid,
        "revision": revision,
        "left_marks": left,
        "right_marks": right,
        "rivet_side": rivet,
    }


def assert_chain(result, expected):
    assert result.status == "OPTIMAL"
    chains = [
        "–".join([p.head_mark for p in track.placements] + [
            track.placements[-1].tail_mark
        ])
        for track in result.tracks
    ]
    assert sorted(chains) == sorted(expected)


# ---------------------------------------------------------------------------
# Ingestion and evidence/default separation
# ---------------------------------------------------------------------------

def test_display_default_rivet_is_not_physical_evidence():
    library = RubbingLibrary(contract())
    document = doc("scan-1", [rubbing("T1", "T1-a", ["A"], ["B"], rivet=None)])
    result = library.ingest(document)
    assert result.accepted, result.errors
    canonical = library.get_rubbing("T1-a")
    assert canonical.rivet_side is None
    published = library.publish()["rows"][0]
    assert published["rivet_side"] == "left"
    assert published["sources"]["rivet_side"] == {
        "default": "left",
        "conversion": "display_default",
        "usable_for_orientation": False,
    }

    replay = library.ingest(document)
    assert replay.accepted
    assert replay.receipt["replay"] is True
    assert replay.replayed_rubbings == ["T1-a"]


def test_unknown_dictionary_candidate_rejects_whole_file():
    library = RubbingLibrary(contract())
    result = library.ingest(
        doc(
            "bad",
            [
                rubbing("T1", "ok", ["A"], ["B"], "left"),
                rubbing("T2", "bad-mark", ["Z"], ["B"], "left"),
            ],
        )
    )
    assert not result.accepted
    assert library.snapshot() == []


def test_rubbing_with_no_written_end_evidence_is_allowed_and_adds_no_constraint():
    library = RubbingLibrary(contract())
    result = library.ingest(
        doc(
            "unobserved",
            [
                rubbing("T1", "r1", ["A"], ["B"], "left"),
                rubbing("T1", "r2", None, None, "left"),
            ],
        )
    )
    assert result.accepted, result.errors
    reconstruction = reconstruct_all(library.freeze(), DICTIONARY)["T1"]
    assert reconstruction.directed_pairs == (("A", "B"),)


def test_atomic_revision_conflict_and_independent_frozen_libraries():
    # First frozen hypothesis: the old B label is still current at revision 1.
    old_library = RubbingLibrary(contract())
    assert old_library.ingest(
        doc("old", [rubbing("T3", "T3-r1", ["B"], ["D"], "left", 1)])
    ).accepted
    old = reconstruct_all(old_library.freeze(), DICTIONARY)["T3"]
    assert old.unique_unordered_pair == ("B", "D")

    # Illegal low-revision evidence is validated even when a higher revision wins.
    bad_library = RubbingLibrary(contract())
    bad_file = doc(
        "bad-low",
        [
            rubbing("T3", "T3-r1", ["Z"], ["D"], "left", 1),
            rubbing("T3", "T3-r1", ["C"], ["D"], "left", 2),
        ],
    )
    assert not bad_library.ingest(bad_file).accepted
    assert bad_library.snapshot() == []

    # A second, independent frozen library receives a superseding C label.
    new_library = RubbingLibrary(contract())
    assert new_library.ingest(
        doc("old", [rubbing("T3", "T3-r1", ["B"], ["D"], "left", 1)])
    ).accepted
    revision = new_library.ingest(
        doc("new", [rubbing("T3", "T3-r1", ["C"], ["D"], "left", 2)])
    )
    assert revision.accepted, revision.errors
    new = reconstruct_all(new_library.freeze(), DICTIONARY)["T3"]
    assert new.unique_unordered_pair == ("C", "D")

    # Same revision with both old and new labels still effective is a conflict.
    conflict_library = RubbingLibrary(contract())
    assert conflict_library.ingest(
        doc("old", [rubbing("T3", "T3-r1", ["B"], ["D"], "left", 1)])
    ).accepted
    conflict = conflict_library.ingest(
        doc("conflict", [rubbing("T3", "T3-r1", ["C"], ["D"], "left", 1)])
    )
    assert not conflict.accepted
    assert conflict_library.get_rubbing("T3-r1").left_marks == ("B",)


def test_lower_revision_is_validated_then_ignored():
    library = RubbingLibrary(contract())
    assert library.ingest(
        doc("high", [rubbing("T", "r", ["C"], ["D"], "left", revision=2)])
    ).accepted
    lower = library.ingest(
        doc("low", [rubbing("T", "r", ["B"], ["D"], "left", revision=1)])
    )
    assert lower.accepted
    assert lower.ignored[0]["action"] == "ignored_lower_revision"
    assert library.get_rubbing("r").left_marks == ("C",)

    illegal = library.ingest(
        doc("illegal-low", [rubbing("T", "r", ["Z"], ["D"], "left", revision=1)])
    )
    assert not illegal.accepted
    assert library.get_rubbing("r").left_marks == ("C",)


# ---------------------------------------------------------------------------
# Restoration: whole-tile flips, intersections, and non-Cartesian real pairs
# ---------------------------------------------------------------------------

def test_rivet_unknown_keeps_both_whole_tile_orientations():
    library = RubbingLibrary(contract())
    assert library.ingest(
        doc("scan", [rubbing("T1", "r", ["A"], ["B"], rivet=None)])
    ).accepted
    reconstruction = reconstruct_all(library.freeze(), DICTIONARY)["T1"]
    assert set(reconstruction.directed_pairs) == {("A", "B"), ("B", "A")}
    assert reconstruction.unordered_pairs == (("A", "B"),)
    assert reconstruction.exhibit_ready
    assert not reconstruction.rivet_orientation_determined


def test_observed_rivet_pins_directed_pair():
    library = RubbingLibrary(contract())
    assert library.ingest(
        doc("scan", [rubbing("T1", "r", ["A"], ["B"], rivet="left")])
    ).accepted
    assert possible_directed_end_pairs("T1", library.freeze(), DICTIONARY) == (("A", "B"),)


def test_empty_compatible_set_is_retained_for_review():
    library = RubbingLibrary(contract())
    assert library.ingest(
        doc(
            "scans",
            [
                rubbing("T", "r1", ["A"], ["B"], "left"),
                rubbing("T", "r2", ["A"], ["C"], "left"),
            ],
        )
    ).accepted
    reconstruction = reconstruct_all(library.freeze(), DICTIONARY)["T"]
    assert reconstruction.empty
    assert not reconstruction.exhibit_ready
    with pytest.raises(ValueError):
        tile_edges_from_reconstructions({"T": reconstruction})


def test_marginal_candidates_intersect_to_one_unordered_pair_not_cartesian():
    # Each individual witness admits several physical directed pairs.  Their
    # intersection is exactly the real C–F tile; e.g. C–E is never a real tile.
    library = RubbingLibrary(contract())
    result = library.ingest(
        doc(
            "ambiguous",
            [
                rubbing("M", "m1", ["C", "E"], ["F", "D"], rivet=None),
                rubbing("M", "m2", ["C", "A"], ["F", "B"], rivet=None),
            ],
        )
    )
    assert result.accepted, result.errors
    reconstruction = reconstruct_all(library.freeze(), DICTIONARY)["M"]
    assert set(reconstruction.directed_pairs) == {("C", "F"), ("F", "C")}
    assert reconstruction.unordered_pairs == (("C", "F"),)
    assert reconstruction.exhibit_ready
    assert not reconstruction.rivet_orientation_determined


def test_ambiguous_tile_with_two_unordered_pairs_is_not_exhibit_ready():
    library = RubbingLibrary(contract())
    assert library.ingest(
        doc("scan", [rubbing("T", "r", ["A", "C"], ["B", "D"], rivet=None)])
    ).accepted
    reconstruction = reconstruct_all(library.freeze(), DICTIONARY)["T"]
    assert len(reconstruction.unordered_pairs) > 1
    assert not reconstruction.exhibit_ready


# ---------------------------------------------------------------------------
# Exhibition examples from the brief
# ---------------------------------------------------------------------------

def edges_ab_bc_bd():
    return (
        TileEdge("T1", "A", "B"),
        TileEdge("T2", "B", "C"),
        TileEdge("T3", "B", "D"),
    )


def test_three_tiles_need_two_tracks_without_bridge():
    result = minimize_tracks(edges_ab_bc_bd(), (), max_borrowed_bridges=0)
    assert result.status == "OPTIMAL"
    assert result.track_count == 2
    assert result.borrowed_bridges == ()


def test_bridge_allows_one_track_and_borrowed_bridge_is_placed():
    bridge = (Bridge("Q", "C", "D"),)
    result = minimize_tracks(edges_ab_bc_bd(), bridge, max_borrowed_bridges=1)
    assert_chain(result, ["A–B–C–D–B"])
    assert result.track_count == 1
    assert result.borrowed_bridges == ("Q",)
    assert [p.edge_kind for p in result.tracks[0].placements].count("bridge") == 1


def test_revision_to_c_d_allows_one_track_without_bridge():
    revised = (
        TileEdge("T1", "A", "B"),
        TileEdge("T2", "B", "C"),
        TileEdge("T3", "C", "D"),
    )
    result = minimize_tracks(revised, (Bridge("Q", "C", "D"),), max_borrowed_bridges=0)
    assert_chain(result, ["A–B–C–D"])
    assert result.borrowed_bridges == ()


def test_one_track_without_needed_bridge_is_infeasible():
    result = minimum_bridges_for_tracks(
        edges_ab_bc_bd(), (Bridge("Q", "C", "D"),), available_tracks=1
    )
    assert result.status == "OPTIMAL"
    assert result.borrowed_bridges == ("Q",)

    stopped = minimum_bridges_for_tracks(
        edges_ab_bc_bd(), (), available_tracks=1
    )
    assert stopped.status == "INFEASIBLE"


def test_x_y_z_forbidden_adjacency_forces_two_tracks_without_ending_changes():
    edges = (
        TileEdge("X", "A", "B"),
        TileEdge("Y", "B", "C"),
        TileEdge("Z", "C", "D"),
    )
    no_forbidden = minimize_tracks(edges, (), max_borrowed_bridges=0)
    assert no_forbidden.track_count == 1
    forbidden = minimize_tracks(
        edges,
        (),
        max_borrowed_bridges=0,
        forbidden_adjacent={frozenset(("X", "Y"))},
    )
    assert forbidden.track_count == 2


def test_duplicate_same_marked_tiles_retain_multiplicity_and_identity():
    edges = (
        TileEdge("P1", "A", "B"),
        TileEdge("P2", "A", "B"),
    )
    result = minimize_tracks(edges, (Bridge("Q", "B", "A"),), max_borrowed_bridges=1)
    assert result.status == "OPTIMAL"
    used_real = [
        placement.edge_id
        for track in result.tracks
        for placement in track.placements
        if placement.edge_kind == "real"
    ]
    assert sorted(used_real) == ["P1", "P2"]


def test_fixed_track_budget_minimizes_bridge_count():
    result = minimum_bridges_for_tracks(
        edges_ab_bc_bd(), (Bridge("Q", "C", "D"),), available_tracks=2
    )
    assert result.status == "OPTIMAL"
    assert result.borrowed_bridges == ()
    assert result.track_count <= 2


def test_all_real_tiles_used_once_and_adjacencies_match():
    result = minimize_tracks(
        edges_ab_bc_bd(), (Bridge("Q", "C", "D"),), max_borrowed_bridges=1
    )
    placements = [p for track in result.tracks for p in track.placements]
    real_ids = sorted(p.edge_id for p in placements if p.edge_kind == "real")
    assert real_ids == ["T1", "T2", "T3"]
    for track in result.tracks:
        for left, right in zip(track.placements, track.placements[1:]):
            assert left.tail_mark == right.head_mark


def test_evidenced_rivet_side_is_reported_with_each_placement_direction():
    forward_case = (
        TileEdge(
            "X",
            "A",
            "B",
            rivet_orientation_determined=True,
            possible_directed_pairs=(("A", "B"),),
        ),
        TileEdge("Y", "B", "C"),
    )
    forward = minimize_tracks(forward_case, (), max_borrowed_bridges=0)
    x_forward = next(
        placement
        for track in forward.tracks
        for placement in track.placements
        if placement.edge_id == "X"
    )
    assert x_forward.directed_pair == ("A", "B")
    assert x_forward.rivet_side == "left"

    # A tile may be reversed for the track even when the rivet pins the
    # physical direction.  Reversing moves that rivet to the reader's right.
    reverse_case = (
        TileEdge("Y", "C", "B"),
        TileEdge(
            "X",
            "A",
            "B",
            rivet_orientation_determined=True,
            possible_directed_pairs=(("A", "B"),),
        ),
    )
    reversed_result = minimize_tracks(reverse_case, (), max_borrowed_bridges=0)
    x_reverse = next(
        placement
        for track in reversed_result.tracks
        for placement in track.placements
        if placement.edge_id == "X"
    )
    assert x_reverse.directed_pair == ("B", "A")
    assert x_reverse.rivet_side == "right"

    # Without rivet evidence, the same tile may be turned but the output must
    # not claim a reader side.
    undirected_case = (
        TileEdge("Y", "C", "B"),
        TileEdge(
            "X",
            "A",
            "B",
            rivet_orientation_determined=False,
            possible_directed_pairs=(("A", "B"), ("B", "A")),
        ),
    )
    undirected = minimize_tracks(undirected_case, (), max_borrowed_bridges=0)
    x_undirected = next(
        placement
        for track in undirected.tracks
        for placement in track.placements
        if placement.edge_id == "X"
    )
    assert x_undirected.directed_pair == ("B", "A")
    assert x_undirected.rivet_side is None


def test_double_tile_has_two_indistinguishable_orientations():
    library = RubbingLibrary(contract())
    assert library.ingest(
        doc("double", [rubbing("D", "d", ["A"], ["A"], rivet="left")])
    ).accepted
    reconstruction = reconstruct_all(library.freeze(), DICTIONARY)["D"]
    assert reconstruction.unique_unordered_pair == ("A", "A")
    assert reconstruction.exhibit_ready
    assert not reconstruction.rivet_orientation_determined


def test_forbidden_pair_can_be_separated_by_a_bridge_on_same_track():
    edges = (
        TileEdge("X", "A", "B"),
        TileEdge("Y", "B", "C"),
    )
    result = minimize_tracks(
        edges,
        (Bridge("Q", "B", "B"),),
        max_borrowed_bridges=1,
        forbidden_adjacent={frozenset(("X", "Y"))},
    )
    assert result.track_count == 1
    ids = [p.edge_id for p in result.tracks[0].placements]
    assert ids == ["X", "Q", "Y"]


def test_invalid_inputs_are_results_not_silent_substitutions():
    too_many = tuple(TileEdge(f"T{i}", "A", "B") for i in range(16))
    result = minimize_tracks(too_many, (), max_borrowed_bridges=0)
    assert result.status == "INVALID"
    assert any("15 real tiles" in error for error in result.errors)

    duplicate = (TileEdge("P", "A", "B"), TileEdge("P", "A", "B"))
    duplicate_result = minimize_tracks(duplicate, (), max_borrowed_bridges=0)
    assert duplicate_result.status == "INVALID"
    assert duplicate_result.errors

    inconsistent = (TileEdge("P", "A", "B", True, (("A", "C"),)),)
    inconsistent_result = minimize_tracks(inconsistent, (), max_borrowed_bridges=0)
    assert inconsistent_result.status == "INVALID"
    assert any("does not match" in error for error in inconsistent_result.errors)
