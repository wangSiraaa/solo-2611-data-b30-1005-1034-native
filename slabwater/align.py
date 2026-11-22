"""Rigid alignment of two frozen catalog slices and their sand difference.

The old and target plates may have been installed under different coordinate
origins, so their arrays must never be subtracted index-for-index.  This module:

1. enumerates the eight dihedral orientations of a square grid (a multiple of
   90 degree rotation, optionally with one mirror), translating each result so
   it lands on the old plate;
2. keeps only orientation + translation pairs that map every target cell to a
   distinct old cell, i.e. a one-to-one cover of the whole old plate;
3. keeps only those that satisfy every alignment ticket (an old marker must
   coincide with one of the candidate target markers);
4. subtracts ``old - (aligned_target + correction)`` and retains, for every
   non-zero difference, both original cells and the frozen per-tile provenance
   from both slices.

If several surviving alignments produce the *same* complete difference map we
may hand that one map on.  If they produce different maps we return every
actual map plus the disagreement cells instead of silently picking a cheapest
one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .model import Cell, Slice, SourceRef, is_int

_ROTATIONS = (0, 90, 180, 270)


class AlignmentError(ValueError):
    """Malformed alignment material (as opposed to an unsatisfiable one)."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def _orient(
    rotation: int, mirror: bool, cell: Cell, height: int, width: int
) -> Cell:
    """Orient a target cell in its own plate, before translation."""

    r, c = cell
    if mirror:
        c = width - 1 - c
    if rotation == 0:
        return r, c
    if rotation == 90:
        return c, height - 1 - r
    if rotation == 180:
        return height - 1 - r, width - 1 - c
    if rotation == 270:
        return width - 1 - c, r
    raise AlignmentError("bad_rotation", f"rotation must be one of {_ROTATIONS}")


@dataclass(frozen=True)
class RigidTransform:
    """A square-grid isometry mapping *target* coordinates into *old* ones.

    ``mirror`` reflects the target columns first; ``rotation`` is a clockwise
    quarter-turn count; ``(dr, dc)`` is the final translation.  The translation
    is chosen by the enumerator so the transformed plate covers the old plate.
    """

    rotation: int
    mirror: bool
    dr: int
    dc: int

    def apply(self, cell: Cell, height: int, width: int) -> Cell:
        rr, cc = _orient(self.rotation, self.mirror, cell, height, width)
        return rr + self.dr, cc + self.dc

    def key(self) -> tuple[int, bool, int, int]:
        return (self.rotation, self.mirror, self.dr, self.dc)

    def label(self) -> str:
        mirror = "mirror+" if self.mirror else ""
        return f"rot{self.rotation:03d}/{mirror}dr{self.dr}/dc{self.dc}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rotation": self.rotation,
            "mirror": self.mirror,
            "dr": self.dr,
            "dc": self.dc,
            "label": self.label(),
        }


@dataclass(frozen=True)
class DeltaCell:
    """One non-zero sand difference with both original cells and provenance."""

    old_cell: Cell
    target_cell: Cell
    delta: int
    old_sources: tuple[SourceRef, ...]
    target_sources: tuple[SourceRef, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_cell": [self.old_cell[0], self.old_cell[1]],
            "target_cell": [self.target_cell[0], self.target_cell[1]],
            "delta": self.delta,
            "old_scoopable": self.delta if self.delta > 0 else 0,
            "target_fill": -self.delta if self.delta < 0 else 0,
            "old_sources": [ref.to_dict() for ref in self.old_sources],
            "target_sources": [ref.to_dict() for ref in self.target_sources],
        }


@dataclass(frozen=True)
class DifferenceMap:
    """One complete ``old - aligned_target - correction`` difference map."""

    delta: tuple[tuple[int, ...], ...]
    deltas: tuple[DeltaCell, ...]
    positive_total: int
    negative_total: int
    transform: RigidTransform
    alignments: tuple[RigidTransform, ...]

    def signature(self) -> tuple[tuple[int, ...], ...]:
        return self.delta

    def to_dict(self) -> dict[str, Any]:
        return {
            "delta": [list(row) for row in self.delta],
            "deltas": [cell.to_dict() for cell in self.deltas],
            "positive_total": self.positive_total,
            "negative_total": self.negative_total,
            "scoopable_total": self.positive_total,
            "fill_total": self.negative_total,
            "transform": self.transform.to_dict(),
            "alignments": [transform.to_dict() for transform in self.alignments],
        }


@dataclass(frozen=True)
class AlignmentResult:
    ok: bool
    status: str
    correction: int
    alignments: tuple[RigidTransform, ...]
    maps: tuple[DifferenceMap, ...]
    disagreement_cells: tuple[Cell, ...]
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "correction": self.correction,
            "reason": self.reason,
            "alignments": [transform.to_dict() for transform in self.alignments],
            "maps": [difference.to_dict() for difference in self.maps],
            "disagreement_cells": [
                [cell[0], cell[1]] for cell in self.disagreement_cells
            ],
        }


def _validate_marker_table(
    table: Mapping[str, Any], height: int, width: int, label: str
) -> dict[str, Cell]:
    markers: dict[str, Cell] = {}
    if not isinstance(table, Mapping):
        raise AlignmentError("bad_markers", f"{label} markers must be an object")
    for name, raw in table.items():
        if not isinstance(name, str) or not name:
            raise AlignmentError("bad_markers", f"{label} marker names must be non-empty strings")
        cell = _parse_cell(raw, f"{label} marker {name!r}")
        if not (0 <= cell[0] < height and 0 <= cell[1] < width):
            raise AlignmentError(
                "marker_out_of_bounds",
                f"{label} marker {name!r} cell {list(cell)} is outside the plate",
            )
        markers[name] = cell
    return markers


def _parse_cell(raw: Any, what: str) -> Cell:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise AlignmentError("bad_cell", f"{what} must be a [row, col] pair")
    r, c = raw
    if not is_int(r) or not is_int(c):
        raise AlignmentError("bad_cell", f"{what} coordinates must be integers")
    return int(r), int(c)


@dataclass(frozen=True)
class _Ticket:
    ticket_id: str
    old_marker: str
    target_markers: tuple[str, ...]


def _validate_tickets(
    raw_tickets: Sequence[Any],
    old_markers: Mapping[str, Cell],
    target_markers: Mapping[str, Cell],
) -> tuple[_Ticket, ...]:
    if raw_tickets is None:
        return ()
    if not isinstance(raw_tickets, (list, tuple)):
        raise AlignmentError("bad_tickets", "tickets must be a list")
    tickets: list[_Ticket] = []
    for index, raw in enumerate(raw_tickets):
        if not isinstance(raw, Mapping):
            raise AlignmentError("bad_tickets", f"ticket[{index}] must be an object")
        ticket_id = raw.get("id", f"ticket{index}")
        if not isinstance(ticket_id, str):
            raise AlignmentError("bad_tickets", f"ticket[{index}] id must be a string")
        old_marker = raw.get("old")
        if not isinstance(old_marker, str) or old_marker not in old_markers:
            raise AlignmentError(
                "bad_tickets",
                f"ticket {ticket_id!r} references unknown old marker {old_marker!r}",
            )
        targets = raw.get("targets")
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, (list, tuple)) or not targets:
            raise AlignmentError(
                "bad_tickets", f"ticket {ticket_id!r} must list at least one target marker"
            )
        resolved: list[str] = []
        for name in targets:
            if not isinstance(name, str) or name not in target_markers:
                raise AlignmentError(
                    "bad_tickets",
                    f"ticket {ticket_id!r} references unknown target marker {name!r}",
                )
            resolved.append(name)
        tickets.append(_Ticket(ticket_id, old_marker, tuple(resolved)))
    return tuple(tickets)


def _candidate_alignments(old: Slice, target: Slice) -> list[RigidTransform]:
    """All orientation+translation pairs that are a one-to-one cover of old."""

    candidates: list[RigidTransform] = []
    seen: set[tuple[Cell, ...]] = set()
    old_area = old.height * old.width
    if target.height * target.width != old_area:
        return candidates
    for rotation in _ROTATIONS:
        for mirror in (False, True):
            oriented: list[tuple[Cell, Cell]] = []
            for r in range(target.height):
                for c in range(target.width):
                    oriented.append(((r, c), _orient(rotation, mirror, (r, c), target.height, target.width)))
            min_r = min(cell[0] for _, cell in oriented)
            max_r = max(cell[0] for _, cell in oriented)
            min_c = min(cell[1] for _, cell in oriented)
            max_c = max(cell[1] for _, cell in oriented)
            for dr in range(-min_r, old.height - max_r):
                for dc in range(-min_c, old.width - max_c):
                    transform = RigidTransform(rotation, mirror, dr, dc)
                    mapped: dict[Cell, Cell] = {}
                    valid = True
                    for target_cell, oriented_cell in oriented:
                        old_cell = (oriented_cell[0] + dr, oriented_cell[1] + dc)
                        if not (0 <= old_cell[0] < old.height and 0 <= old_cell[1] < old.width):
                            valid = False
                            break
                        if old_cell in mapped:
                            valid = False
                            break
                        mapped[old_cell] = target_cell
                    if not valid or len(mapped) != old_area:
                        continue
                    signature = tuple(
                        mapped[(r, c)] for r in range(old.height) for c in range(old.width)
                    )
                    if signature in seen:
                        continue
                    seen.add(signature)
                    candidates.append(transform)
    candidates.sort(key=lambda transform: transform.key())
    return candidates


def _satisfies(
    transform: RigidTransform,
    old_markers: Mapping[str, Cell],
    target_markers: Mapping[str, Cell],
    tickets: Sequence[_Ticket],
    target_height: int,
    target_width: int,
) -> bool:
    for ticket in tickets:
        old_cell = old_markers[ticket.old_marker]
        if not any(
            transform.apply(target_markers[name], target_height, target_width) == old_cell
            for name in ticket.target_markers
        ):
            return False
    return True


def compute_difference(
    old: Slice, target: Slice, transform: RigidTransform, correction: int = 0
) -> DifferenceMap:
    """Subtract an aligned target plate from the old plate, keeping provenance."""

    old_to_target: dict[Cell, Cell] = {}
    for tr in range(target.height):
        for tc in range(target.width):
            old_cell = transform.apply((tr, tc), target.height, target.width)
            old_to_target[old_cell] = (tr, tc)
    if len(old_to_target) != old.height * old.width:
        raise AlignmentError(
            "not_one_to_one", "transform does not map the target onto the whole old plate"
        )

    delta: list[list[int]] = []
    deltas: list[DeltaCell] = []
    positive = 0
    negative = 0
    for r in range(old.height):
        row: list[int] = []
        for c in range(old.width):
            target_cell = old_to_target[(r, c)]
            value = old.heights[r][c] - (
                target.heights[target_cell[0]][target_cell[1]] + correction
            )
            row.append(value)
            if value:
                deltas.append(
                    DeltaCell(
                        old_cell=(r, c),
                        target_cell=target_cell,
                        delta=value,
                        old_sources=old.sources.get((r, c), ()),
                        target_sources=target.sources.get(target_cell, ()),
                    )
                )
                if value > 0:
                    positive += value
                else:
                    negative += -value
        delta.append(row)
    return DifferenceMap(
        delta=tuple(tuple(row) for row in delta),
        deltas=tuple(deltas),
        positive_total=positive,
        negative_total=negative,
        transform=transform,
        alignments=(transform,),
    )


def align_slices(
    old: Slice,
    target: Slice,
    old_markers: Mapping[str, Any] | None = None,
    target_markers: Mapping[str, Any] | None = None,
    tickets: Sequence[Any] | None = None,
    correction: int = 0,
) -> AlignmentResult:
    """Find the rigid placement(s) and the resulting sand difference map(s)."""

    if not is_int(correction):
        raise AlignmentError("bad_correction", "correction must be an integer")
    old_table = _validate_marker_table(
        old_markers or {}, old.height, old.width, "old"
    )
    target_table = _validate_marker_table(
        target_markers or {}, target.height, target.width, "target"
    )
    ticket_list = _validate_tickets(tickets or (), old_table, target_table)

    candidates = _candidate_alignments(old, target)
    if not candidates:
        return AlignmentResult(
            ok=False,
            status="no_candidate_transform",
            correction=correction,
            alignments=(),
            maps=(),
            disagreement_cells=(),
            reason="no rigid orientation/translation covers the old plate",
        )
    valid = [
        transform
        for transform in candidates
        if _satisfies(
            transform,
            old_table,
            target_table,
            ticket_list,
            target.height,
            target.width,
        )
    ]
    if not valid:
        return AlignmentResult(
            ok=False,
            status="no_alignment",
            correction=correction,
            alignments=(),
            maps=(),
            disagreement_cells=(),
            reason="every full-plate orientation violates at least one ticket",
        )

    computed = [
        (transform, compute_difference(old, target, transform, correction))
        for transform in valid
    ]
    # Group by the complete difference map: identical maps are interchangeable.
    groups: dict[tuple[tuple[int, ...], ...], list[tuple[RigidTransform, DifferenceMap]]] = {}
    for transform, difference in computed:
        groups.setdefault(difference.signature(), []).append((transform, difference))

    maps: list[DifferenceMap] = []
    for entries in groups.values():
        representative = entries[0][1]
        maps.append(
            DifferenceMap(
                delta=representative.delta,
                deltas=representative.deltas,
                positive_total=representative.positive_total,
                negative_total=representative.negative_total,
                transform=representative.transform,
                alignments=tuple(transform for transform, _ in entries),
            )
        )
    maps.sort(key=lambda difference: difference.transform.key())

    if len(maps) == 1:
        return AlignmentResult(
            ok=True,
            status="aligned",
            correction=correction,
            alignments=tuple(valid),
            maps=tuple(maps),
            disagreement_cells=(),
            reason=None,
        )

    disagreement: list[Cell] = []
    for r in range(old.height):
        for c in range(old.width):
            values = {difference.delta[r][c] for difference in maps}
            if len(values) > 1:
                disagreement.append((r, c))
    return AlignmentResult(
        ok=False,
        status="ambiguous",
        correction=correction,
        alignments=tuple(valid),
        maps=tuple(maps),
        disagreement_cells=tuple(disagreement),
        reason=(
            "markers/tickets leave more than one distinct difference map; "
            "returning every actual map instead of choosing one"
        ),
    )
