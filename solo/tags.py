"""Hang tags, tag tables, replacement tags and the two-show planner.

A physical :class:`Tag` carries an answer word, its puzzle club and whether
it is an old (borrowable) tag.  Old tags are handed over in whole
:class:`Box`es; new tags belong to the museum and simply hang when their
slot's per-show table says so.  :class:`ReplacementTag` items are limited
stock drawn against a global inventory; they are never club old tags.

Constraints within one showing:

* every slot hangs at most one tag and every tag occupies at most one slot;
* a tag is only eligible for slots listing it, for a word of matching
  length that respects every printed/overlay-fixed letter;
* one answer word appears at most once in a showing;
* crossings must spell the same letter;
* committee-mandated tags must be hung.

Across two showings, boxes borrowed for the first showing are either
returned (and become unavailable later) or renewed -- renewal only when
the box actually lent at least one hung tag, and never beyond the cabinet
box limit.  The same physical old tag hung in both showings counts twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Mapping, Sequence

from .board import BoardError, MadeBoard, Slot

__all__ = [
    "MAX_HANG_TAGS_PER_SLOT",
    "MAX_BOXES",
    "MAX_REPLACEMENTS",
    "Tag",
    "Box",
    "ReplacementTag",
    "TagTable",
    "Assignment",
    "Showing",
    "Plan",
    "InfeasibleReport",
    "PlanError",
    "feasible_assignments",
    "plan_single_showing",
    "plan_two_showings",
    "require_plan",
]

MAX_HANG_TAGS_PER_SLOT = 4
MAX_BOXES = 4
MAX_REPLACEMENTS = 3
# Bounds are tiny (4x4 board, 10 slots, 4 tags/slot); keep enumeration honest.
_ENUMERATION_CAP = 200_000


class PlanError(BoardError):
    """No assignment/plan satisfies the stated constraints."""


# ---------------------------------------------------------------------------
# Tag inventory


@dataclass(frozen=True)
class Tag:
    """One physical tag: answer word, owning club, old/new flag."""

    tag_id: str
    word: str
    club: str
    is_old: bool = False
    box_id: str | None = None

    def __post_init__(self) -> None:
        if self.is_old and not self.box_id:
            raise BoardError(f"old tag {self.tag_id} must name its box")
        if not self.is_old and self.box_id is not None:
            raise BoardError(f"new tag {self.tag_id} cannot belong to a box")


@dataclass(frozen=True)
class Box:
    """A whole box of old tags handed over and renewed as one unit."""

    box_id: str
    club: str
    tag_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.tag_ids:
            raise BoardError(f"box {self.box_id} is empty")


@dataclass(frozen=True)
class ReplacementTag:
    """A limited replacement tag: id, eligible slot ids and answer word."""

    tag_id: str
    word: str
    eligible_slots: frozenset[str]


@dataclass
class TagTable:
    """Per-show table of the tag ids each slot may hang, plus limits check."""

    board: MadeBoard
    entries: Mapping[str, frozenset[str]]
    tags: Mapping[str, Tag]
    boxes: Mapping[str, Box]
    replacements: Mapping[str, ReplacementTag] = field(default_factory=dict)
    replacement_stock: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.entries = {slot: frozenset(ids) for slot, ids in self.entries.items()}
        self.tags = dict(self.tags)
        self.boxes = dict(self.boxes)
        self.replacements = dict(self.replacements)
        self.replacement_stock = dict(self.replacement_stock)
        self._validate()

    def _validate(self) -> None:
        slot_ids = {slot.id for slot in self.board.slots}
        unknown = set(self.entries) - slot_ids
        if unknown:
            raise BoardError(f"tag table names unknown slots: {sorted(unknown)}")
        for slot in self.board.slots:
            ids = self.entries.get(slot.id, frozenset())
            if len(ids) > MAX_HANG_TAGS_PER_SLOT:
                raise BoardError(
                    f"slot {slot.id} lists {len(ids)} tags, limit is "
                    f"{MAX_HANG_TAGS_PER_SLOT}"
                )
            for tag_id in ids:
                tag = self._require_tag(tag_id)
                if len(tag.word) != slot.length:
                    raise BoardError(
                        f"tag {tag_id} word {tag.word!r} has length "
                        f"{len(tag.word)} but slot {slot.id} has length {slot.length}"
                    )
                # A word clashing with a printed/overlay-fixed letter is a
                # legal table entry but can never be hung; the solver's
                # candidates() filters it, so an overlay can make an old
                # tag unhangable without making the table invalid.
        if len(self.boxes) > MAX_BOXES:
            raise BoardError(
                f"{len(self.boxes)} borrow boxes, limit is {MAX_BOXES}"
            )
        if len(self.replacements) > MAX_REPLACEMENTS:
            raise BoardError(
                f"{len(self.replacements)} replacement kinds, limit is "
                f"{MAX_REPLACEMENTS}"
            )
        for rid, rep in self.replacements.items():
            bad = set(rep.eligible_slots) - slot_ids
            if bad:
                raise BoardError(f"replacement {rid} names unknown slots {sorted(bad)}")
            for slot_id in rep.eligible_slots:
                slot = self.board.slot_by_id(slot_id)
                if len(rep.word) != slot.length:
                    raise BoardError(
                        f"replacement {rid} word {rep.word!r} has the wrong length "
                        f"for slot {slot_id}"
                    )
            if self.replacement_stock.get(rid, 0) < 0:
                raise BoardError(f"replacement {rid} stock is negative")
        self._check_box_integrity()

    def _check_box_integrity(self) -> None:
        box_of: dict[str, str] = {}
        for tag_id, tag in self.tags.items():
            if tag.is_old:
                if tag.box_id not in self.boxes:
                    raise BoardError(f"old tag {tag_id} points at unknown box {tag.box_id}")
                box_of[tag_id] = tag.box_id
        for box in self.boxes.values():
            for tag_id in box.tag_ids:
                tag = self.tags.get(tag_id)
                if tag is None or not tag.is_old:
                    raise BoardError(f"box {box.box_id} must contain old tag {tag_id}")
                if box_of.get(tag_id) != box.box_id:
                    raise BoardError(f"tag {tag_id} is not recorded in box {box.box_id}")
                if tag.club != box.club:
                    raise BoardError(
                        f"tag {tag_id} club {tag.club} differs from box club {box.club}"
                    )

    def _require_tag(self, tag_id: str) -> Tag:
        tag = self.tags.get(tag_id)
        if tag is None:
            raise BoardError(f"unknown tag {tag_id}")
        return tag

    def candidates(
        self,
        slot: Slot,
        stock_override: Mapping[str, int] | None = None,
    ) -> tuple[Tag | ReplacementTag, ...]:
        """Everything the per-show table permits on ``slot`` right now.

        Entries whose word clashes with a printed or overlay-fixed letter
        are filtered here: the table itself stays valid, but the tag is not
        hangable on that board face.  ``stock_override`` carries the stock
        remaining after earlier draws (e.g. the first showing).
        """
        stock = stock_override if stock_override is not None else self.replacement_stock
        out: list[Tag | ReplacementTag] = []
        for tag_id in self.entries.get(slot.id, frozenset()):
            tag = self._require_tag(tag_id)
            if slot.fits(tag.word):
                out.append(tag)
        for rid, rep in self.replacements.items():
            if (
                slot.id in rep.eligible_slots
                and stock.get(rid, 0) > 0
                and slot.fits(rep.word)
            ):
                out.append(rep)
        return tuple(out)


# ---------------------------------------------------------------------------
# Assignments and showings


@dataclass(frozen=True)
class Assignment:
    """One filled showing: slot id -> hung entity id."""

    slot_tags: Mapping[str, str]
    replacement_ids: frozenset[str]
    hung_old_tags: frozenset[str]
    used_boxes: frozenset[str]
    club_old_hangs: Mapping[str, int]
    words: Mapping[str, str]

    def face(self, board: MadeBoard) -> str:
        return board.render(self.words)


@dataclass(frozen=True)
class Showing:
    index: int
    board: MadeBoard
    table: TagTable
    assignment: Assignment


@dataclass(frozen=True)
class Plan:
    showings: tuple[Showing, ...]
    renewed_boxes: frozenset[str]
    returned_boxes: frozenset[str]
    replacement_stock_left: Mapping[str, int]
    club_old_hangs: Mapping[str, int]
    feasible: bool = True

    def club_count(self, club: str) -> int:
        return self.club_old_hangs.get(club, 0)


@dataclass(frozen=True)
class InfeasibleReport:
    feasible: bool
    reasons: tuple[str, ...]
    blocking: Mapping[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Single-showing enumeration


def _word_of(entity: Tag | ReplacementTag) -> str:
    return entity.word


def _is_replacement(entity: Tag | ReplacementTag) -> bool:
    return isinstance(entity, ReplacementTag)


def feasible_assignments(
    table: TagTable,
    *,
    mandatory: Sequence[str] = (),
    available_boxes: frozenset[str] | None = None,
    available_old_tags: frozenset[str] | None = None,
    replacement_stock: Mapping[str, int] | None = None,
    collect_all: bool = False,
) -> list[Assignment]:
    """Enumerate feasible assignments of one showing.

    ``mandatory`` are tag ids (old or new) that must be hung.  The stock
    arguments encode what the second showing can still access after the
    first showing's renewals and replacement draws.
    """
    board = table.board
    stock = dict(replacement_stock if replacement_stock is not None else table.replacement_stock)
    mandatory = tuple(mandatory)

    mandatory_slots: dict[str, str] = {}
    for tag_id in mandatory:
        tag = table.tags.get(tag_id)
        if tag is None:
            raise PlanError(f"mandated tag {tag_id} does not exist")
        homes = [
            slot.id
            for slot in board.slots
            if tag_id in table.entries.get(slot.id, frozenset())
            and slot.fits(tag.word)
        ]
        if not homes:
            raise PlanError(
                f"mandated tag {tag_id} is not hangable in this showing's table "
                "(no eligible slot, or its word clashes with a fixed letter)"
            )
        if len(homes) > 1:
            raise PlanError(f"mandated tag {tag_id} is listed on multiple slots")
        mandatory_slots[homes[0]] = tag_id

    crossings = board.crossings()
    slots = list(board.slots)
    # Mandated slots first: fail early when a mandate cannot be satisfied.
    slots.sort(key=lambda s: 0 if s.id in mandatory_slots else 1)

    results: list[Assignment] = []
    state: dict[str, object] = {
        "slot_tags": {},
        "words": {},
        "used_tag_ids": set(),
        "used_words": set(),
        "reps": {},  # replacement id -> drawn count
    }

    def entity_allowed(entity: Tag | ReplacementTag, slot: Slot) -> bool:
        eid = entity.tag_id
        if eid in state["used_tag_ids"]:
            return False
        word = _word_of(entity)
        if word in state["used_words"]:
            return False
        if isinstance(entity, Tag):
            if entity.is_old:
                if available_boxes is not None and entity.box_id not in available_boxes:
                    return False
                if available_old_tags is not None and eid not in available_old_tags:
                    return False
            if slot.id in mandatory_slots and mandatory_slots[slot.id] != eid:
                return False
        else:
            if state["reps"].get(eid, 0) >= stock.get(eid, 0):
                return False
        # Crossings: every shared cell must agree with already-filled slots.
        for pos, coord in enumerate(slot.cells):
            for other_slot_id, other_pos in crossings.get(coord, ()):
                other_word = state["words"].get(other_slot_id)
                if other_word is not None and other_word[other_pos] != word[pos]:
                    return False
        return True

    def finish() -> Assignment | None:
        slot_tags = dict(state["slot_tags"])
        words = dict(state["words"])
        # Every mandate must actually be hung.
        for tag_id in mandatory:
            if tag_id not in slot_tags.values():
                return None
        hung_old = frozenset(
            eid
            for eid in slot_tags.values()
            if eid in table.tags and table.tags[eid].is_old
        )
        used_boxes = frozenset(table.tags[eid].box_id for eid in hung_old)
        club_hangs: dict[str, int] = {}
        for eid in hung_old:
            club = table.tags[eid].club
            club_hangs[club] = club_hangs.get(club, 0) + 1
        return Assignment(
            slot_tags=slot_tags,
            replacement_ids=frozenset(state["reps"]),
            hung_old_tags=hung_old,
            used_boxes=used_boxes,
            club_old_hangs=club_hangs,
            words=words,
        )

    def backtrack(i: int) -> None:
        if len(results) >= _ENUMERATION_CAP:
            raise PlanError("enumeration cap reached; board limits should prevent this")
        if i == len(slots):
            assignment = finish()
            if assignment is not None:
                results.append(assignment)
            return
        slot = slots[i]
        chosen: list[Tag | ReplacementTag] = []
        if slot.id in mandatory_slots:
            entity = table.tags[mandatory_slots[slot.id]]
            chosen = [entity] if entity_allowed(entity, slot) else []
        else:
            chosen = [e for e in table.candidates(slot, stock) if entity_allowed(e, slot)]
        for entity in chosen:
            eid = entity.tag_id
            word = _word_of(entity)
            state["slot_tags"][slot.id] = eid
            state["words"][slot.id] = word
            state["used_tag_ids"].add(eid)
            state["used_words"].add(word)
            drew_rep = False
            if _is_replacement(entity):
                state["reps"][eid] = state["reps"].get(eid, 0) + 1
                drew_rep = True
            backtrack(i + 1)
            if not collect_all and results:
                return
            del state["slot_tags"][slot.id]
            del state["words"][slot.id]
            state["used_tag_ids"].discard(eid)
            state["used_words"].discard(word)
            if drew_rep:
                left = state["reps"][eid] - 1
                if left:
                    state["reps"][eid] = left
                else:
                    del state["reps"][eid]

    backtrack(0)
    return results


# ---------------------------------------------------------------------------
# Single-showing convenience


def plan_single_showing(
    table: TagTable,
    *,
    mandatory: Sequence[str] = (),
    available_boxes: frozenset[str] | None = None,
    available_old_tags: frozenset[str] | None = None,
    replacement_stock: Mapping[str, int] | None = None,
) -> Assignment:
    results = feasible_assignments(
        table,
        mandatory=mandatory,
        available_boxes=available_boxes,
        available_old_tags=available_old_tags,
        replacement_stock=replacement_stock,
    )
    if not results:
        raise PlanError(_single_showing_diagnosis(table, mandatory, available_boxes))
    return results[0]


def _single_showing_diagnosis(
    table: TagTable,
    mandatory: Sequence[str],
    available_boxes: frozenset[str] | None,
) -> str:
    reasons = []
    for tag_id in mandatory:
        tag = table.tags.get(tag_id)
        if tag is None:
            reasons.append(f"mandated tag {tag_id} does not exist")
            continue
        homes = [
            s.id
            for s in table.board.slots
            if tag_id in table.entries.get(s.id, frozenset()) and s.fits(tag.word)
        ]
        if not homes:
            reasons.append(f"mandated tag {tag_id} cannot hang on any slot of this showing")
        elif tag.is_old and available_boxes is not None and tag.box_id not in available_boxes:
            reasons.append(
                f"mandated tag {tag_id} needs box {tag.box_id}, which is not available"
            )
    for slot in table.board.slots:
        if not table.candidates(slot):
            reasons.append(f"slot {slot.id} has no eligible tag with stock left")
    if not reasons:
        reasons.append("crossing letters, one-word-per-showing or stock constraints conflict")
    return "showing infeasible: " + "; ".join(reasons)


# ---------------------------------------------------------------------------
# Two-show planner


def _club_totals(first: Assignment, second: Assignment) -> dict[str, int]:
    totals: dict[str, int] = {}
    for assignment in (first, second):
        for club, count in assignment.club_old_hangs.items():
            totals[club] = totals.get(club, 0) + count
    return totals


def _plan_key(
    first: Assignment,
    second: Assignment,
    renewed: frozenset[str],
    objective_club: str | None,
    floor_club: str | None,
    floor: int,
):
    totals = _club_totals(first, second)
    # 1) maximize the objective club's old-tag hang count,
    # 2) then maximize the floored club (the "one more before" direction),
    # 3) then minimize replacement draws,
    # 4) then keep as few boxes in the cabinet as possible,
    # 5) deterministic tie break on the actual hung id tuples.
    rep_draws = len(first.replacement_ids) + len(second.replacement_ids)
    tie = (
        tuple(sorted(first.slot_tags.items())),
        tuple(sorted(second.slot_tags.items())),
        tuple(sorted(renewed)),
    )
    return (
        -(totals.get(objective_club, 0) if objective_club else 0),
        -(totals.get(floor_club, 0) if floor_club else 0),
        rep_draws,
        len(renewed),
        tie,
    )


def plan_two_showings(
    first_table: TagTable,
    second_table: TagTable,
    *,
    cabinet_limit: int,
    mandatory_first: Sequence[str] = (),
    mandatory_second: Sequence[str] = (),
    floor_club: str | None = None,
    floor_hangs: int = 0,
    objective_club: str | None = None,
) -> Plan | InfeasibleReport:
    """Plan two linked showings with box renewal and replacement stock.

    All boxes arrive before the first showing.  A box is renewed only when
    the first showing hung one of its tags; returned boxes are unavailable
    in the second showing.  Replacement stock is shared and decremented.
    """
    if not 0 <= cabinet_limit <= MAX_BOXES:
        raise BoardError(f"cabinet limit {cabinet_limit} outside 0..{MAX_BOXES}")

    first_all = _safe_enumerate(first_table, mandatory_first)
    if isinstance(first_all, InfeasibleReport):
        return first_all

    # Replacement tags come from one shared inventory.  Both per-show
    # tables must declare the same replacement kinds and stock.
    shared_stock = dict(first_table.replacement_stock)
    second_stock = {rid: second_table.replacement_stock.get(rid) for rid in first_table.replacements}
    if set(second_table.replacements) != set(first_table.replacements) or second_stock != shared_stock:
        raise BoardError(
            "the two showings must share one replacement inventory with identical "
            "replacement kinds and stock"
        )

    best = None
    blockers: list[str] = []
    for first in first_all:
        if len(first.used_boxes) > cabinet_limit:
            blockers.append(
                f"first showing needs {len(first.used_boxes)} boxes but the cabinet "
                f"holds {cabinet_limit}"
            )
            continue
        remaining_stock = dict(shared_stock)
        for rid in first.replacement_ids:
            remaining_stock[rid] -= 1
            if remaining_stock[rid] < 0:
                break
        # Only boxes that actually lent a hung tag may be renewed; the
        # museum may also return any of them.  Enumerate every renewal
        # subset that fits the cabinet, smallest first.
        lendable_boxes = sorted(first.used_boxes)
        renewal_options: list[frozenset[str]] = [frozenset()]
        for size in range(1, len(lendable_boxes) + 1):
            for combo in combinations(lendable_boxes, size):
                renewal_options.append(frozenset(combo))
        any_second = False
        for renewed in renewal_options:
            if len(renewed) > cabinet_limit:
                continue
            # A renewed box brings its whole set back: a physical old tag
            # already hung in show 1 may be hung again in show 2 (it counts
            # as a second hang); unused tags are now available as well.
            second_old = frozenset(
                tag_id
                for box_id in renewed
                for tag_id in first_table.boxes[box_id].tag_ids
            )
            try:
                second_list = feasible_assignments(
                    second_table,
                    mandatory=mandatory_second,
                    available_boxes=renewed,
                    available_old_tags=second_old,
                    replacement_stock=remaining_stock,
                    collect_all=True,
                )
            except PlanError as exc:
                blockers.append(str(exc))
                continue
            if not second_list:
                continue
            any_second = True
            for second in second_list:
                totals = _club_totals(first, second)
                if floor_club is not None and totals.get(floor_club, 0) < floor_hangs:
                    continue
                key = _plan_key(
                    first, second, renewed, objective_club, floor_club, floor_hangs
                )
                if best is None or key < best[0]:
                    best = (key, first, second, renewed, remaining_stock)
        if not any_second:
            blockers.append(
                "after a feasible first showing, the second showing has no tags left "
                f"for any renewal subset of {sorted(first.used_boxes)} "
                f"(stock {remaining_stock})"
            )

    if best is None:
        reasons = tuple(dict.fromkeys(blockers)) or (
            "the two showings cannot be satisfied together under the renewal rules",
        )
        return InfeasibleReport(
            feasible=False,
            reasons=reasons,
            blocking={
                "floor_club": floor_club,
                "floor_hangs": floor_hangs,
                "cabinet_limit": cabinet_limit,
            },
        )

    _key, first, second, renewed, remaining_stock = best
    final_stock = dict(remaining_stock)
    for rid in second.replacement_ids:
        final_stock[rid] -= 1
    all_boxes = frozenset(first_table.boxes)
    returned = frozenset(all_boxes - renewed)
    return Plan(
        showings=(
            Showing(0, first_table.board, first_table, first),
            Showing(1, second_table.board, second_table, second),
        ),
        renewed_boxes=renewed,
        returned_boxes=returned,
        replacement_stock_left=final_stock,
        club_old_hangs=_club_totals(first, second),
    )


def _safe_enumerate(table: TagTable, mandatory: Sequence[str]):
    try:
        return feasible_assignments(table, mandatory=mandatory, collect_all=True)
    except PlanError as exc:
        return InfeasibleReport(feasible=False, reasons=(str(exc),))


def require_plan(result: Plan | InfeasibleReport) -> Plan:
    if isinstance(result, InfeasibleReport):
        raise PlanError("; ".join(result.reasons))
    return result
