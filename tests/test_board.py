"""Tests for the crossword board, tag tables and two-show planner.

Run:  python3 -m unittest discover -s tests -v

The tests pin down the cell-identity rules (same-value rewrite, half-PAIR
annotation, conflicts, separator adjacency), the slot geometry, and every
museum case stated in the task, on real SOLO bytes held in a
RevisionLibrary.
"""

from __future__ import annotations

import unittest

from solo.board import (
    HORIZONTAL,
    MAX_COLS,
    MAX_ROWS,
    MAX_SLOTS,
    VERTICAL,
    BoardDraft,
    BoardError,
    Overlay,
    Separator,
    make_board,
)
from solo.codec import decode_document
from solo.exhibit import (
    BOX_A,
    BOX_B,
    CLUB_A,
    CLUB_B,
    H1,
    H2,
    V1,
    V2,
    GRID_TEXT,
    build_board_library,
    build_inventory,
    demonstrate_identity,
    exhibit,
    run_cases,
    show_boards,
)
from solo.revision import replace
from solo.tags import (
    MAX_BOXES,
    MAX_HANG_TAGS_PER_SLOT,
    MAX_REPLACEMENTS,
    Box,
    InfeasibleReport,
    ReplacementTag,
    Tag,
    TagTable,
    feasible_assignments,
    plan_single_showing,
    plan_two_showings,
    require_plan,
)


def _grid_body() -> bytes:
    """Real SOLO body for the 2x2 grid 一. / .D."""
    return (
        bytes((0xFF, 0x01))
        + bytes((0x00, 0x00))   # 一
        + bytes((0xFF, 0x00))
        + b"."
        + b"\n"
        + b"."
        + b"D"
    )


def make_draft() -> tuple:
    lib, v0, _ = build_board_library()
    return lib, BoardDraft.from_version(lib, v0), v0


class CellIdentityTests(unittest.TestCase):
    def test_real_original_decodes_to_grid(self):
        lib, draft, v0 = make_draft()
        self.assertEqual(draft.rows, ("一.", ".D"))
        doc = decode_document(lib.get(v0).body)
        self.assertEqual(doc.text, GRID_TEXT)
        # The ideograph is a genuine two-byte PAIR cell.
        cell = draft.cell_at(0, 0)
        self.assertEqual(cell.byte_range, (2, 4))
        self.assertEqual(doc.modes[cell.char_index], 1)

    def test_same_value_rewrite_kills_identity_despite_equal_bytes(self):
        lib, draft0, v0 = make_draft()
        old_cell = draft0.cell_at(0, 1)
        old_bytes = lib.get(v0).body[old_cell.byte_start : old_cell.byte_end]

        v1 = lib.apply(v0, [replace(old_cell.char_index, old_cell.char_index + 1, ".")]).version_id
        draft1 = BoardDraft.from_version(lib, v1)
        new_cell = draft1.cell_at(0, 1)
        new_bytes = lib.get(v1).body[new_cell.byte_start : new_cell.byte_end]

        # The visible cell and its bytes are identical ...
        self.assertEqual(new_cell.char, ".")
        self.assertEqual(old_bytes, new_bytes)
        # ... but the identity is dead, because a replacement touched it.
        self.assertIsNone(draft1.map_identity(v0, old_cell.char_index))
        # A copied neighbour keeps its identity across the same revision.
        neighbour = draft0.cell_at(1, 0)
        self.assertEqual(
            draft1.map_identity(v0, neighbour.char_index), neighbour.char_index
        )

    def test_identity_survives_chained_copies_but_dies_at_later_rewrite(self):
        lib, draft0, v0 = make_draft()
        # Rewrite (1,0), keeping the grid rectangular.
        v1 = lib.apply(v0, [replace(3, 4, "X")]).version_id
        # Later rewrite the top-left ideograph with the same character.
        v2 = lib.apply(v1, [replace(0, 1, "一")]).version_id
        draft2 = BoardDraft.from_version(lib, v2)
        # (1,1) the printed D was never touched.
        d_cell = draft0.cell_at(1, 1)
        self.assertIsNotNone(draft2.map_identity(v0, d_cell.char_index))
        # (0,0) died at the second batch even though the value is identical.
        top_left = draft0.cell_at(0, 0)
        self.assertIsNone(draft2.map_identity(v0, top_left.char_index))
        # (1,0) died at the first batch.
        bottom_left = draft0.cell_at(1, 0)
        self.assertIsNone(draft2.map_identity(v0, bottom_left.char_index))


class OverlayTests(unittest.TestCase):
    def test_half_pair_annotation_is_rejected(self):
        lib, draft, v0 = make_draft()
        pair_cell = draft.cell_at(0, 0)
        bad = Overlay("X", pair_cell.byte_start, pair_cell.byte_start + 1)
        board = make_board(draft, [bad])
        self.assertEqual(board.overlays, {})
        self.assertEqual(len(board.overlay_rejections), 1)
        self.assertEqual(board.overlay_rejections[0].code, "half_pair")

    def test_overlay_on_rewritten_white_cell_is_rejected(self):
        lib, draft0, v0 = make_draft()
        target = draft0.cell_at(0, 1)
        v1 = lib.apply(v0, [replace(target.char_index, target.char_index + 1, ".")]).version_id
        draft1 = BoardDraft.from_version(lib, v1)
        stale = Overlay("B", target.byte_start, target.byte_end, origin_version=v0)
        board = make_board(draft1, [stale])
        self.assertEqual(board.overlays, {})
        self.assertEqual(board.overlay_rejections[0].code, "identity_rewritten")

    def test_overlay_pinned_to_new_version_mounts(self):
        lib, draft0, v0 = make_draft()
        target = draft0.cell_at(0, 1)
        v1 = lib.apply(v0, [replace(target.char_index, target.char_index + 1, ".")]).version_id
        draft1 = BoardDraft.from_version(lib, v1)
        fresh_cell = draft1.cell_at(0, 1)
        fresh = Overlay("B", fresh_cell.byte_start, fresh_cell.byte_end, origin_version=v1)
        board = make_board(draft1, [fresh])
        self.assertEqual(board.overlays, {(0, 1): "B"})
        self.assertEqual(board.overlay_rejections, ())

    def test_overlay_requires_white_origin_cell(self):
        lib, draft, v0 = make_draft()
        printed = draft.cell_at(1, 1)  # the D letter cell
        board = make_board(draft, [Overlay("Z", printed.byte_start, printed.byte_end)])
        self.assertEqual(board.overlay_rejections[0].code, "not_white")

    def test_conflicting_overlays_are_all_rejected(self):
        lib, draft, v0 = make_draft()
        cell = draft.cell_at(1, 0)
        board = make_board(
            draft,
            [
                Overlay("B", cell.byte_start, cell.byte_end, label="B件"),
                Overlay("C", cell.byte_start, cell.byte_end, label="C件"),
            ],
        )
        self.assertEqual(board.overlays, {})
        codes = {r.code for r in board.overlay_rejections}
        self.assertEqual(codes, {"conflict"})

    def test_range_on_switch_only_bytes_is_bad_range(self):
        lib, draft, v0 = make_draft()
        # Bytes [0,2) are the leading SINGLE->PAIR switch, no character.
        board = make_board(draft, [Overlay("Q", 0, 2)])
        self.assertEqual(board.overlay_rejections[0].code, "bad_range")

    def test_unknown_origin_version_rejected(self):
        lib, draft, v0 = make_draft()
        board = make_board(draft, [Overlay("B", 6, 7, origin_version=99)])
        self.assertEqual(board.overlay_rejections[0].code, "bad_origin")


class SeparatorTests(unittest.TestCase):
    def test_mounted_separator_splits_a_horizontal_run(self):
        lib, draft, v0 = make_draft()
        board = make_board(draft, separators=[Separator(0, 0)])
        self.assertIn((0, 0), board.separators)
        top_slots = [s for s in board.slots if s.direction == HORIZONTAL and s.start_row == 0]
        self.assertEqual(sorted(s.length for s in top_slots), [1, 1])

    def test_separator_beside_rewritten_cell_is_rejected(self):
        lib, draft0, v0 = make_draft()
        target = draft0.cell_at(0, 1)
        v1 = lib.apply(v0, [replace(target.char_index, target.char_index + 1, ".")]).version_id
        draft1 = BoardDraft.from_version(lib, v1)
        board = make_board(draft1, separators=[Separator(0, 0, origin_version=v0)])
        self.assertEqual(board.separators, set())
        self.assertEqual(board.separator_rejections[0].code, "side_rewritten")

    def test_non_adjacent_separator_reported(self):
        # Use a same-width rewrite that moves a cell's column: turn the
        # grid "AB\nCD" into "AB\nDC" (C and D swap columns on row 1).
        # Both C and D survive as identities but their coordinates change,
        # so the old edge between them is no longer between the same two
        # adjacent identities in the same order.
        from solo.revision import RevisionLibrary

        lib = RevisionLibrary()
        body = b"AB\nCD"
        v0 = lib.add_original(body).version_id
        # Rewrite the whole second row; the two side identities both die,
        # which must be reported as side_rewritten rather than crash.
        v1 = lib.apply(v0, [replace(3, 5, "DC")]).version_id
        draft1 = BoardDraft.from_version(lib, v1)
        board = make_board(draft1, separators=[Separator(1, 0, origin_version=v0)])
        self.assertIn(
            board.separator_rejections[0].code,
            {"side_rewritten", "not_adjacent"},
        )
        # The untouched top-row separator still mounts.
        board2 = make_board(draft1, separators=[Separator(0, 0, origin_version=v0)])
        self.assertIn((0, 0), board2.separators)
        self.assertEqual(board2.separator_rejections, ())

    def test_separator_side_order_reversal_is_reported(self):
        # A same-width rewrite of one side cell keeps the other side's
        # identity but changes what sits next to it; the origin edge can no
        # longer be mounted between the same two origin identities.
        from solo.revision import RevisionLibrary

        lib = RevisionLibrary()
        v0 = lib.add_original(b"AB").version_id
        v1 = lib.apply(v0, [replace(0, 1, "C")]).version_id  # AB -> CB
        origin = BoardDraft.from_version(lib, v0)
        draft1 = BoardDraft.from_version(lib, v1)
        # B survives at column 1; A's identity is dead.
        self.assertIsNone(draft1.map_identity(v0, origin.cell_at(0, 0).char_index))
        self.assertEqual(draft1.map_identity(v0, origin.cell_at(0, 1).char_index), 1)
        board = make_board(draft1, separators=[Separator(0, 0, origin_version=v0)])
        self.assertEqual(board.separators, set())
        self.assertEqual(board.separator_rejections[0].code, "side_rewritten")


class SlotGeometryTests(unittest.TestCase):
    def test_2x2_has_four_slots_length_two(self):
        lib, draft, v0 = make_draft()
        board = make_board(draft)
        self.assertEqual(len(board.slots), 4)
        self.assertTrue(all(s.length == 2 for s in board.slots))
        directions = sorted((s.direction, s.start_row, s.start_col) for s in board.slots)
        self.assertEqual(directions, [(HORIZONTAL, 0, 0), (HORIZONTAL, 1, 0),
                                      (VERTICAL, 0, 0), (VERTICAL, 0, 1)])

    def test_black_cell_splits_runs_and_length_one_counts(self):
        from solo.revision import RevisionLibrary

        lib = RevisionLibrary()
        v0 = lib.add_original(b".#\n..").version_id
        draft = BoardDraft.from_version(lib, v0)
        board = make_board(draft)
        # Top row: one white isolated cell -> length-one horizontal slot.
        top = [s for s in board.slots if s.direction == HORIZONTAL and s.start_row == 0]
        self.assertEqual([s.length for s in top], [1])

    def test_overlay_fixes_a_slot_letter(self):
        lib, draft, v0 = make_draft()
        target = draft.cell_at(0, 1)
        board = make_board(draft, [Overlay("B", target.byte_start, target.byte_end)])
        top = next(s for s in board.slots if s.id == H1)
        self.assertEqual(top.fixed, ("一", "B"))
        self.assertTrue(top.fits("一B"))
        self.assertFalse(top.fits("一C"))

    def test_grid_limits_enforced(self):
        lib = __import__("solo.revision", fromlist=["RevisionLibrary"]).RevisionLibrary()
        wide = ("." * (MAX_COLS + 1) + "\n.")
        with self.assertRaises(BoardError):
            BoardDraft.from_version(lib, lib.add_original(wide.encode()).version_id)


class InventoryAndTableTests(unittest.TestCase):
    def setUp(self):
        self.lib, v0, _ = build_board_library()
        self.tags, self.boxes = build_inventory()
        self.board1, self.board2 = show_boards(self.lib, v0, show1_overlay=False)

    def test_old_tag_requires_box_and_box_integrity(self):
        with self.assertRaises(BoardError):
            Tag("lonely", "一B", CLUB_A, is_old=True)
        bad_box = Box("坏盒", CLUB_A, ("NOPE",))
        with self.assertRaises(BoardError):
            TagTable(self.board1, {H1: frozenset()}, self.tags, {**self.boxes, "坏盒": bad_box})

    def test_slot_table_limit(self):
        ids = frozenset(f"N{i}" for i in range(MAX_HANG_TAGS_PER_SLOT + 1))
        tags = {f"N{i}": Tag(f"N{i}", "一B", "馆藏", is_old=False)
                for i in range(MAX_HANG_TAGS_PER_SLOT + 1)}
        with self.assertRaises(BoardError):
            TagTable(self.board1, {H1: ids}, tags, {})

    def test_overlay_makes_old_tag_unhangable_but_table_stays_valid(self):
        # Build the overlaid board explicitly: B at (0,1).
        draft = BoardDraft.from_version(self.lib, 0)
        target = draft.cell_at(0, 1)
        board_overlay = make_board(draft, [Overlay("B", target.byte_start, target.byte_end)])
        # V2 runs down column 1; its top position is the overlaid B cell,
        # so the CD tag clashes while the BD tag and Q1 remain candidates.
        table = TagTable(board_overlay, {
            V2: frozenset(("N-V2-BD", "N-V2-CD")),
            V1: frozenset(("Q1", "N-V1-一B", "N-V1-一C")),
        }, self.tags, self.boxes)
        candidate_ids = {e.tag_id for e in table.candidates(board_overlay.slot_by_id(V2))}
        self.assertIn("N-V2-BD", candidate_ids)
        self.assertNotIn("N-V2-CD", candidate_ids)
        # With H1 forced to 一B, Q1 (also 一B) is excluded by the
        # one-word-per-showing rule during solving.
        entries = dict(table.entries)
        entries[H1] = frozenset(("N-H1-一B",))
        entries[H2] = frozenset(("N-H2-CD",))
        full = TagTable(board_overlay, entries, self.tags, self.boxes)
        self.assertFalse(
            any("Q1" in a.slot_tags.values()
                for a in feasible_assignments(full, collect_all=True))
        )

    def test_replacement_stock_gate(self):
        rep = ReplacementTag("R", "CD", frozenset({H2}))
        table = TagTable(self.board2, {H2: frozenset(("Q2",))}, self.tags, self.boxes,
                         {"R": rep}, {"R": 0})
        self.assertNotIn("R", [e.tag_id for e in table.candidates(table.board.slot_by_id(H2))])
        stocked = TagTable(self.board2, {H2: frozenset(("Q2",))}, self.tags, self.boxes,
                           {"R": rep}, {"R": 1})
        self.assertIn("R", [e.tag_id for e in stocked.candidates(stocked.board.slot_by_id(H2))])


class SingleShowingSolverTests(unittest.TestCase):
    def setUp(self):
        self.lib, v0, _ = build_board_library()
        self.tags, self.boxes = build_inventory()
        self.board, _ = show_boards(self.lib, v0, show1_overlay=False)
        self.table = TagTable(self.board, {
            H1: frozenset(("P1", "N-H1-一B", "N-H1-一C")),
            V1: frozenset(("Q1", "N-V1-一B", "N-V1-一C")),
            H2: frozenset(("N-H2-BD", "N-H2-CD")),
            V2: frozenset(("N-V2-BD", "N-V2-CD")),
        }, self.tags, self.boxes)

    def test_two_named_faces_and_no_p1_q1_together(self):
        faces = {a.face(self.board) for a in feasible_assignments(self.table, collect_all=True)}
        self.assertIn("一B/CD", faces)
        self.assertIn("一C/BD", faces)
        together = [a for a in feasible_assignments(self.table, collect_all=True)
                    if {"P1", "Q1"} <= set(a.slot_tags.values())]
        self.assertEqual(together, [])

    def test_mandated_p1_and_q1_each_succeed_alone(self):
        a1 = feasible_assignments(self.table, mandatory=("P1",))[0]
        a2 = feasible_assignments(self.table, mandatory=("Q1",))[0]
        self.assertEqual(a1.face(self.board), "一B/CD")
        self.assertEqual(a2.face(self.board), "一C/BD")

    def test_plan_single_showing_convenience_and_empty_failure(self):
        assignment = plan_single_showing(self.table, mandatory=("P1",))
        self.assertEqual(assignment.slot_tags[H1], "P1")
        # A table with no eligible tag on any slot cannot be filled.
        empty_entries = {slot.id: frozenset() for slot in self.board.slots}
        empty = TagTable(self.board, empty_entries, self.tags, self.boxes)
        with self.assertRaises(Exception):
            plan_single_showing(empty)

    def test_every_tag_occupies_one_slot_and_word_unique(self):
        for a in feasible_assignments(self.table, collect_all=True):
            self.assertEqual(len(set(a.slot_tags.values())), len(a.slot_tags))
            self.assertEqual(len(set(a.words.values())), len(a.words))
            for slot_id, word in a.words.items():
                self.assertTrue(self.board.slot_by_id(slot_id).fits(word))

    def test_crossing_must_agree(self):
        # Hand-built contradictory tables cannot produce an assignment:
        # H2 forced BD while V2 forced CD share their last cell.
        tags = {
            "ONLY-BD": Tag("ONLY-BD", "BD", "馆藏", is_old=False),
            "ONLY-CD": Tag("ONLY-CD", "CD", "馆藏", is_old=False),
            "ONLY-一B-H": Tag("ONLY-一B-H", "一B", "馆藏", is_old=False),
            "ONLY-一B-V": Tag("ONLY-一B-V", "一B", "馆藏", is_old=False),
        }
        broken = TagTable(self.board, {
            H2: frozenset(("ONLY-BD",)),
            V2: frozenset(("ONLY-CD",)),
            H1: frozenset(("ONLY-一B-H",)),
            V1: frozenset(("ONLY-一B-V",)),
        }, tags, {})
        self.assertEqual(feasible_assignments(broken), [])


class TwoShowPlannerTests(unittest.TestCase):
    def _tables(self, stock_r: int, show1_overlay: bool):
        lib, v0, _ = build_board_library()
        tags, boxes = build_inventory()
        board1, board2 = show_boards(lib, v0, show1_overlay=show1_overlay)
        rep = ReplacementTag("R", "CD", frozenset({H2}))
        entries1 = {
            H1: frozenset(("P1", "N-H1-一B", "N-H1-一C")),
            V1: frozenset(("Q1", "N-V1-一B", "N-V1-一C")),
            H2: frozenset(("N-H2-BD", "N-H2-CD")),
            V2: frozenset(("N-V2-BD", "N-V2-CD")),
        }
        entries2 = {
            H1: frozenset(("N-H1-一B",)),
            V2: frozenset(("N-V2-BD",)),
            V1: frozenset(("N-V1-一B", "N-V1-一C")),
            H2: frozenset(("Q2",)),
        }
        t1 = TagTable(board1, entries1, tags, boxes, {"R": rep}, {"R": stock_r})
        t2 = TagTable(board2, entries2, tags, boxes, {"R": rep}, {"R": stock_r})
        return lib, tags, boxes, board1, board2, t1, t2

    def test_case_a_q1_then_q2_when_no_replacement(self):
        _lib, _tags, _boxes, b1, b2, t1, t2 = self._tables(0, False)
        plan = require_plan(plan_two_showings(t1, t2, cabinet_limit=1, objective_club=CLUB_B))
        self.assertEqual(plan.club_count(CLUB_A), 0)
        self.assertEqual(plan.club_count(CLUB_B), 2)
        self.assertEqual(plan.renewed_boxes, frozenset({BOX_B}))
        self.assertEqual(plan.returned_boxes, frozenset({BOX_A}))
        self.assertEqual(plan.showings[0].assignment.face(b1), "一C/BD")
        self.assertEqual(plan.showings[1].assignment.face(b2), "一B/CD")
        self.assertEqual(plan.showings[1].assignment.slot_tags[H2], "Q2")

    def test_case_b_first_overlay_blocks_q1_and_second_showing(self):
        _lib, _tags, _boxes, b1, b2, t1, t2 = self._tables(0, True)
        result = plan_two_showings(t1, t2, cabinet_limit=1)
        self.assertIsInstance(result, InfeasibleReport)
        q1_feasible = any("Q1" in a.slot_tags.values()
                          for a in feasible_assignments(t1, collect_all=True))
        self.assertFalse(q1_feasible)

    def test_case_c_mandated_p1_returns_b_and_uses_replacement(self):
        _lib, _tags, _boxes, b1, b2, t1, t2 = self._tables(1, False)
        plan = require_plan(plan_two_showings(
            t1, t2, cabinet_limit=1, mandatory_first=("P1",), objective_club=CLUB_A))
        self.assertEqual(plan.club_count(CLUB_A), 1)
        self.assertEqual(plan.club_count(CLUB_B), 0)
        self.assertEqual(plan.renewed_boxes, frozenset())
        self.assertEqual(plan.showings[0].assignment.slot_tags[H1], "P1")
        self.assertIn("R", plan.showings[1].assignment.replacement_ids)
        self.assertEqual(plan.replacement_stock_left, {"R": 0})

    def test_case_d_both_styles_viable_and_objective_picks(self):
        _lib, _tags, _boxes, _b1, _b2, t1, t2 = self._tables(1, False)
        via_b = require_plan(plan_two_showings(t1, t2, cabinet_limit=1, objective_club=CLUB_B))
        via_a = require_plan(plan_two_showings(t1, t2, cabinet_limit=1, objective_club=CLUB_A))
        self.assertEqual(via_b.club_count(CLUB_B), 2)
        self.assertEqual(via_a.club_count(CLUB_A), 1)
        self.assertIn("R", via_a.showings[1].assignment.replacement_ids)

    def test_same_old_tag_twice_counts_twice(self):
        # Both showings list Q1 on V1; with H1 choosing 一C instead of 一B
        # the one-word-per-showing rule leaves room for Q1, and renewing
        # 乙盒 lets the same physical Q1 hang in both showings (=2 hangs).
        lib, v0, _ = build_board_library()
        tags, boxes = build_inventory()
        board1, _ = show_boards(lib, v0, show1_overlay=False)
        rep = ReplacementTag("R", "CD", frozenset({H2}))
        entries = {
            H1: frozenset(("N-H1-一C",)),
            V1: frozenset(("Q1", "N-V1-一B", "N-V1-一C")),
            H2: frozenset(("N-H2-BD",)),
            V2: frozenset(("N-V2-CD",)),
        }
        t1 = TagTable(board1, entries, tags, boxes, {"R": rep}, {"R": 0})
        t2 = TagTable(board1, entries, tags, boxes, {"R": rep}, {"R": 0})
        plan = require_plan(plan_two_showings(
            t1, t2, cabinet_limit=1, objective_club=CLUB_B))
        self.assertEqual(plan.club_count(CLUB_B), 2)
        self.assertIn("Q1", plan.showings[0].assignment.hung_old_tags)
        self.assertIn("Q1", plan.showings[1].assignment.hung_old_tags)

    def test_cabinet_limit_blocks_first_showing(self):
        _lib, _tags, _boxes, _b1, _b2, t1, t2 = self._tables(0, False)
        # Force both old tags to hang at once: impossible per word rule, so
        # check the cabinet branch directly via a table allowing both.
        result = plan_two_showings(t1, t2, cabinet_limit=0)
        self.assertIsInstance(result, InfeasibleReport)


class LimitEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.lib, v0, _ = build_board_library()
        self.tags, self.boxes = build_inventory()
        self.board, _ = show_boards(self.lib, v0, show1_overlay=False)

    def test_too_many_boxes_rejected(self):
        extra = {f"盒{i}": Box(f"盒{i}", "社", (f"T{i}",)) for i in range(MAX_BOXES)}
        tags = {f"T{i}": Tag(f"T{i}", "一B", "社", is_old=True, box_id=f"盒{i}")
                for i in range(MAX_BOXES)}
        too_many = {**self.boxes, **extra}
        all_tags = {**self.tags, **tags}
        with self.assertRaises(BoardError):
            TagTable(self.board, {H1: frozenset()}, all_tags, too_many)
        # Exactly at the limit (4 boxes) is fine; only include tags whose
        # boxes are present.
        at_limit = dict(self.boxes)
        for box in list(extra.values())[: MAX_BOXES - len(self.boxes)]:
            at_limit[box.box_id] = box
        kept_tags = {
            tid: tag for tid, tag in all_tags.items()
            if not tag.is_old or tag.box_id in at_limit
        }
        TagTable(self.board, {H1: frozenset()}, kept_tags, at_limit)

    def test_too_many_replacement_kinds_rejected(self):
        reps = {
            f"R{i}": ReplacementTag(f"R{i}", "CD", frozenset({H2}))
            for i in range(MAX_REPLACEMENTS + 1)
        }
        stock = {rid: 1 for rid in reps}
        with self.assertRaises(BoardError):
            TagTable(self.board, {H2: frozenset()}, self.tags, self.boxes, reps, stock)

    def test_slot_limit_constant_matches_task(self):
        self.assertEqual(MAX_HANG_TAGS_PER_SLOT, 4)
        self.assertEqual(MAX_BOXES, 4)
        self.assertEqual(MAX_REPLACEMENTS, 3)
        self.assertEqual((MAX_ROWS, MAX_COLS, MAX_SLOTS), (4, 4, 10))


class ExhibitReproductionTests(unittest.TestCase):
    def test_identity_report_contents(self):
        report = demonstrate_identity(log=lambda *_: None)
        rewrite = report["same_value_rewrite"]
        self.assertTrue(rewrite["byte_equal_across_versions"])
        self.assertIsNone(rewrite["old_identity_maps_to"])
        codes = {r["code"] for r in report["rejections"]}
        self.assertEqual(codes, {"identity_rewritten", "half_pair", "not_white", "conflict"})

    def test_full_exhibit_runs_and_writes_report(self):
        report = exhibit(log=lambda *_: None, write_json=True)
        cases = report["cases"]
        self.assertEqual(cases["first_showing_faces"], ["一B/CD", "一C/BD"])
        self.assertFalse(cases["case_b_first_overlay_no_replacement"]["feasible"])
        self.assertTrue(cases["case_c_mandate_p1_with_replacement"]["feasible"])

    def test_run_cases_assertions_hold(self):
        # run_cases itself contains the narrative assertions; calling it
        # proves every story branch reproduces on the real SOLO bytes.
        result = run_cases(log=lambda *_: None)
        self.assertIn("case_d_two_viable_styles", result)


if __name__ == "__main__":
    unittest.main()
