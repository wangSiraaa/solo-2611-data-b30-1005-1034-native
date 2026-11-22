"""End-to-end exhibit: cell-identity distinctions and the two-show story.

Run:  python3 -m solo.exhibit

Everything here runs on real SOLO bytes held in a
:class:`~solo.revision.RevisionLibrary`.  The first half pins down cell
identity with a genuine original, one same-value white-cell rewrite and a
bad annotation landing inside a PAIR character; the second half replays
the museum's two showings (boxes, overlays, replacement tags, mandates and
the cabinet limit).
"""

from __future__ import annotations

import hashlib
import json
import os

from .board import (
    BoardDraft,
    Overlay,
    make_board,
)
from .codec import MODE_PAIR, decode_document
from .revision import RevisionLibrary, replace
from .tags import (
    Box,
    InfeasibleReport,
    Plan,
    ReplacementTag,
    Tag,
    TagTable,
    feasible_assignments,
    plan_two_showings,
    require_plan,
)

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts")

# Grid "一.\n.D": one PAIR ideograph, three ASCII cells, one newline.
GRID_TEXT = "一.\n.D"
H1 = "H(0,0)"  # top horizontal
H2 = "H(1,0)"  # bottom horizontal
V1 = "V(0,0)"  # left vertical
V2 = "V(0,1)"  # right vertical
BOX_A = "甲盒"
BOX_B = "乙盒"
CLUB_A = "甲社"
CLUB_B = "乙社"


# ---------------------------------------------------------------------------
# Real SOLO original for the 2x2 board


def build_board_library() -> tuple[RevisionLibrary, int, dict]:
    """Create the real original body and return byte-level cell facts."""
    lib = RevisionLibrary()
    body = (
        bytes((0xFF, 0x01))       # SINGLE -> PAIR, emits no character
        + bytes((0x00, 0x00))     # n=0 -> U+4E00 "一" at bytes [2,4)
        + bytes((0xFF, 0x00))     # PAIR -> SINGLE
        + b"."                    # white cell (0,1) at [6,7)
        + b"\n"                   # row separator at [7,8)
        + b"."                    # white cell (1,0) at [8,9)
        + b"D"                    # printed letter (1,1) at [9,10)
    )
    version = lib.add_original(body)
    doc = decode_document(body)
    assert doc.text == GRID_TEXT
    facts = {}
    for index, ch in enumerate(doc.text):
        facts[index] = {
            "char": ch,
            "bytes": [int(doc.starts[index]), int(doc.ends[index])],
            "mode": doc.modes[index],
        }
    return lib, version.version_id, {
        "body": body.hex(),
        "sha256": hashlib.sha256(body).hexdigest(),
        "cells": facts,
    }


# ---------------------------------------------------------------------------
# Part 1: cell identity on the real original


def demonstrate_identity(log=print) -> dict:
    lib, v0, facts = build_board_library()
    draft0 = BoardDraft.from_version(lib, v0)

    # One same-value rewrite of a white cell: char index 1 is the '.' at
    # (0,1), replaced by the identical ".".  The bytes come out equal, but
    # the revision records kill the old cell identity.
    v1 = lib.apply(v0, [replace(1, 2, ".")]).version_id
    draft1 = BoardDraft.from_version(lib, v1)

    top_right_0 = draft0.cell_at(0, 1)
    top_right_1 = draft1.cell_at(0, 1)
    byte_equal = (
        lib.get(v0).body[top_right_0.byte_start : top_right_0.byte_end]
        == lib.get(v1).body[top_right_1.byte_start : top_right_1.byte_end]
    )

    overlay_pinned_old = Overlay("B", top_right_0.byte_start, top_right_0.byte_end, origin_version=v0)
    overlay_pinned_new = Overlay("B", top_right_1.byte_start, top_right_1.byte_end, origin_version=v1)
    half_pair = Overlay("X", 2, 3, origin_version=v0)  # first byte only of "一"
    on_letter = Overlay("Z", 9, 10, origin_version=v0)  # the printed D cell
    bottom_left = draft0.cell_at(1, 0)
    conflict_b = Overlay("B", bottom_left.byte_start, bottom_left.byte_end, origin_version=v0, label="B件")
    conflict_c = Overlay("C", bottom_left.byte_start, bottom_left.byte_end, origin_version=v0, label="C件")

    board1 = make_board(draft1, [overlay_pinned_old, overlay_pinned_new, half_pair, on_letter, conflict_b, conflict_c])

    log("== 格身份（真实 SOLO 原件 + 修订库）==")
    log(f"原件 v{v0} {len(bytes.fromhex(facts['body']))} 字节 sha256={facts['sha256'][:12]}")
    log(f"  正文十六进制: {facts['body']}")
    for index, fact in facts["cells"].items():
        log(f"  char[{index}] {fact['char']!r} bytes {fact['bytes']} mode={fact['mode']}")
    log(f"同文重写: v1 把白格 (0,1) 的 '.' 替换为同一个 '.'；两版该格字节相同={byte_equal}")
    log(f"  旧身份经补丁记录映射结果: {draft1.map_identity(v0, top_right_0.char_index)} (None=已失效)")
    mounted = ", ".join(f"{coord}->{value}" for coord, value in board1.overlays.items())
    log(f"  有效覆字: {mounted}")
    for rejection in board1.overlay_rejections:
        log(f"  拒收[{rejection.code}] {rejection.overlay.label or rejection.overlay.value}: {rejection.reason}")

    return {
        "original": facts,
        "same_value_rewrite": {
            "cell": [0, 1],
            "byte_equal_across_versions": byte_equal,
            "old_identity_maps_to": draft1.map_identity(v0, top_right_0.char_index),
            "mounted_overlay": {str(k): v for k, v in board1.overlays.items()},
        },
        "rejections": [
            {"code": r.code, "reason": r.reason, "overlay": r.overlay.label or r.overlay.value}
            for r in board1.overlay_rejections
        ],
        "pair_mode_value": MODE_PAIR,
    }


# ---------------------------------------------------------------------------
# Part 2: tag inventory for the two showings


def _new_tag(tag_id: str, word: str) -> Tag:
    return Tag(tag_id, word, "馆藏", is_old=False)


def build_inventory() -> tuple[dict[str, Tag], dict[str, Box]]:
    tags = {
        "P1": Tag("P1", "一B", CLUB_A, is_old=True, box_id=BOX_A),
        "Q1": Tag("Q1", "一B", CLUB_B, is_old=True, box_id=BOX_B),
        "Q2": Tag("Q2", "CD", CLUB_B, is_old=True, box_id=BOX_B),
        # Museum new tags: two word choices per slot, distinct physical ids.
        "N-H1-一B": _new_tag("N-H1-一B", "一B"),
        "N-H1-一C": _new_tag("N-H1-一C", "一C"),
        "N-V1-一B": _new_tag("N-V1-一B", "一B"),
        "N-V1-一C": _new_tag("N-V1-一C", "一C"),
        "N-H2-BD": _new_tag("N-H2-BD", "BD"),
        "N-H2-CD": _new_tag("N-H2-CD", "CD"),
        "N-V2-BD": _new_tag("N-V2-BD", "BD"),
        "N-V2-CD": _new_tag("N-V2-CD", "CD"),
    }
    boxes = {
        BOX_A: Box(BOX_A, CLUB_A, ("P1",)),
        BOX_B: Box(BOX_B, CLUB_B, ("Q1", "Q2")),
    }
    return tags, boxes


def _show1_entries() -> dict[str, frozenset[str]]:
    return {
        H1: frozenset(("P1", "N-H1-一B", "N-H1-一C")),
        V1: frozenset(("Q1", "N-V1-一B", "N-V1-一C")),
        H2: frozenset(("N-H2-BD", "N-H2-CD")),
        V2: frozenset(("N-V2-BD", "N-V2-CD")),
    }


def _show2_entries() -> dict[str, frozenset[str]]:
    return {
        H1: frozenset(("N-H1-一B",)),          # overlay B forces 一B
        V2: frozenset(("N-V2-BD",)),           # overlay B + printed D force BD
        V1: frozenset(("N-V1-一B", "N-V1-一C")),
        H2: frozenset(("Q2",)),                # old Q2, or replacement R
    }


def _replacement_r(stock: int) -> tuple[dict[str, ReplacementTag], dict[str, int]]:
    return (
        {"R": ReplacementTag("R", "CD", frozenset({H2}))},
        {"R": stock},
    )


def show_boards(lib: RevisionLibrary, v0: int, *, show1_overlay: bool):
    """Build the two boards; only show 2 carries the B overlay by default."""
    draft = BoardDraft.from_version(lib, v0)
    top_right = draft.cell_at(0, 1)
    overlay_b = Overlay("B", top_right.byte_start, top_right.byte_end)
    board1 = make_board(draft, [overlay_b] if show1_overlay else [])
    board2 = make_board(draft, [overlay_b])
    return board1, board2


# ---------------------------------------------------------------------------
# Reporting helpers


def _assignment_view(board, assignment) -> dict:
    return {
        "face": assignment.face(board),
        "slots": dict(sorted(assignment.slot_tags.items())),
        "words": dict(sorted(assignment.words.items())),
        "hung_old_tags": sorted(assignment.hung_old_tags),
        "used_boxes": sorted(assignment.used_boxes),
        "replacement_draws": sorted(assignment.replacement_ids),
        "club_old_hangs": dict(assignment.club_old_hangs),
    }


def _plan_view(result: Plan | InfeasibleReport) -> dict:
    if isinstance(result, InfeasibleReport):
        return {"feasible": False, "reasons": list(result.reasons), "blocking": dict(result.blocking)}
    return {
        "feasible": True,
        "showings": [
            {
                "showing": showing.index,
                **_assignment_view(showing.board, showing.assignment),
            }
            for showing in result.showings
        ],
        "renewed_boxes": sorted(result.renewed_boxes),
        "returned_boxes": sorted(result.returned_boxes),
        "replacement_stock_left": dict(result.replacement_stock_left),
        "club_old_hangs": dict(result.club_old_hangs),
    }


def _log_plan(title: str, result: Plan | InfeasibleReport, log) -> None:
    log(f"== {title} ==")
    view = _plan_view(result)
    if not view["feasible"]:
        for reason in view["reasons"]:
            log(f"  无解: {reason}")
        return
    for showing in view["showings"]:
        log(f"  第{showing['showing'] + 1}场 板面 {showing['face']}")
        log(f"    各槽签号 {showing['slots']}")
        log(f"    旧签 {showing['hung_old_tags']} 用盒 {showing['used_boxes']} 替签 {showing['replacement_draws']}")
    log(f"  续借 {view['renewed_boxes']}；退回 {view['returned_boxes']}")
    log(f"  替签余库 {view['replacement_stock_left']}；谜社旧签挂次 {view['club_old_hangs']}")


# ---------------------------------------------------------------------------
# The four museum cases


def run_cases(log=print) -> dict:
    lib, v0, _ = build_board_library()
    tags, boxes = build_inventory()

    def make_tables(stock_r: int, show1_overlay: bool):
        board1, board2 = show_boards(lib, v0, show1_overlay=show1_overlay)
        reps, stock = _replacement_r(stock_r)
        # Both showings see the same shared replacement inventory; stock 0
        # simply means the replacement is listed but cannot be drawn.
        table1 = TagTable(board1, _show1_entries(), tags, boxes, reps, stock)
        table2 = TagTable(board2, _show2_entries(), tags, boxes, reps, stock)
        return board1, board2, table1, table2

    output: dict = {}

    # Case A: no R issued; cabinet holds one box.
    _, _, table1, table2 = make_tables(stock_r=0, show1_overlay=False)
    case_a = plan_two_showings(
        table1, table2, cabinet_limit=1, objective_club=CLUB_B
    )
    _log_plan("案例A：库房不发 R，留柜限一盒", case_a, log)
    output["case_a_no_replacement"] = _plan_view(case_a)
    plan_a = require_plan(case_a)
    assert plan_a.club_count(CLUB_A) == 0 and plan_a.club_count(CLUB_B) == 2
    assert plan_a.showings[0].assignment.face(plan_a.showings[0].board) == "一C/BD"
    assert plan_a.showings[1].assignment.face(plan_a.showings[1].board) == "一B/CD"

    # First showing alone: the two boards the statement lists, via P1 / Q1.
    solo_p1 = feasible_assignments(table1, mandatory=("P1",))
    solo_q1 = feasible_assignments(table1, mandatory=("Q1",))
    faces = {a.face(table1.board) for a in (solo_p1[0], solo_q1[0])}
    assert faces == {"一B/CD", "一C/BD"}
    # P1 and Q1 can never hang together (one word per showing).
    together = [
        a for a in feasible_assignments(table1, collect_all=True)
        if {"P1", "Q1"} <= set(a.slot_tags.values())
    ]
    assert not together
    output["first_showing_faces"] = sorted(faces)

    # Case B: show 1 also gets the B overlay; Q1 becomes unhangable.
    _, board1_b, table1_b, table2_b = make_tables(stock_r=0, show1_overlay=True)
    q1_homes = [
        slot.id for slot in board1_b.slots
        if "Q1" in table1_b.entries.get(slot.id, frozenset())
    ]
    q1_feasible = any(
        "Q1" in a.slot_tags.values()
        for a in feasible_assignments(table1_b, collect_all=True)
    )
    case_b = plan_two_showings(
        table1_b, table2_b, cabinet_limit=1, objective_club=CLUB_B
    )
    _log_plan("案例B：首场右上也装 B 覆字，且无 R", case_b, log)
    log(f"  首场 Q1 可挂={q1_feasible}（Q1 的槽位 {q1_homes}，一词一场限制挡住）")
    assert q1_feasible is False
    assert isinstance(case_b, InfeasibleReport)
    output["case_b_first_overlay_no_replacement"] = {
        "q1_hangable": q1_feasible,
        **_plan_view(case_b),
    }

    # Case C: one R issued, committee mandates P1 in show 1.
    _, _, table1_c, table2_c = make_tables(stock_r=1, show1_overlay=False)
    case_c = plan_two_showings(
        table1_c,
        table2_c,
        cabinet_limit=1,
        mandatory_first=("P1",),
        objective_club=CLUB_A,
    )
    _log_plan("案例C：发一张 R，委员会要求首场保住 P1", case_c, log)
    plan_c = require_plan(case_c)
    assert plan_c.club_count(CLUB_A) == 1 and plan_c.club_count(CLUB_B) == 0
    assert "R" in plan_c.showings[1].assignment.replacement_ids
    assert plan_c.renewed_boxes == frozenset()
    output["case_c_mandate_p1_with_replacement"] = _plan_view(plan_c)

    # Case D: R issued, no mandate -- both borrowing styles work; the
    # objective picks the club whose count is being maximised.
    _, _, table1_d, table2_d = make_tables(stock_r=1, show1_overlay=False)
    via_b = require_plan(
        plan_two_showings(table1_d, table2_d, cabinet_limit=1, objective_club=CLUB_B)
    )
    via_a = require_plan(
        plan_two_showings(table1_d, table2_d, cabinet_limit=1, objective_club=CLUB_A)
    )
    _log_plan("案例D-1：无委托，最大化乙社", via_b, log)
    _log_plan("案例D-2：无委托，最大化甲社", via_a, log)
    assert via_b.club_count(CLUB_B) == 2 and "Q2" in via_b.showings[1].assignment.hung_old_tags
    assert via_a.club_count(CLUB_A) == 1 and "R" in via_a.showings[1].assignment.replacement_ids
    # A cross-show floor for club A of one hang rules out the Q1/Q2 route.
    floored = require_plan(
        plan_two_showings(
            table1_d, table2_d, cabinet_limit=1,
            floor_club=CLUB_A, floor_hangs=1, objective_club=CLUB_A,
        )
    )
    assert floored.club_count(CLUB_A) >= 1
    output["case_d_two_viable_styles"] = {
        "max_club_b": _plan_view(via_b),
        "max_club_a": _plan_view(via_a),
        "floor_club_a_one": _plan_view(floored),
    }
    return output


def exhibit(log=print, write_json: bool = True) -> dict:
    identity = demonstrate_identity(log)
    cases = run_cases(log)
    report = {"identity": identity, "cases": cases}
    if write_json:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, "exhibit_report.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        log(f"== 报告已写出 {path} ==")
    return report


def main() -> int:
    exhibit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
