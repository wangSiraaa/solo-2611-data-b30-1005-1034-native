"""Crossword board drafts built on SOLO versions, with cell-identity rules.

A board draft is a decoded SOLO :class:`~solo.revision.Version` whose text
is a rectangular grid of rows separated by ``\\n``:

* ``#`` is a black cell,
* ``.`` is an empty white cell,
* every other character is a pre-printed letter cell.

A *cell identity* is the decoded character of a specific version, located
by its half-open byte range in that version's body.  Overlays (transparent
re-letterings) and separators (bars between two horizontally adjacent
cells) are pinned to identities of a designated original version.  When a
later revision is used as the board draft, identities are followed through
the revision library's patch records:

* a character only ever *copied* by kept runs keeps its identity, even
  across many revisions;
* a character touched by any replacement loses its identity -- even when
  the replacement writes the identical character (and therefore identical
  bytes); identity follows the patch records, not a byte comparison;
* an overlay whose byte range does not coincide with one complete
  character of its origin version is rejected; a range that lands inside a
  two-byte PAIR character is reported specifically as a half-PAIR cell;
* an overlay may only cover a cell that is white (``.``) in its origin;
* surviving overlays fighting over one cell with different values are all
  rejected as conflicts;
* a separator is mounted only when both side identities survive and the
  two cells are still neighbours in the draft grid.

Rejected items are returned in structured reports and are never mounted.
Board making itself only derives word slots from the mounted grid; it
never consults any puzzle-club word list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .codec import MODE_PAIR, decode_document
from .revision import RevisionLibrary, RevisionError
__all__ = [
    "MAX_ROWS",
    "MAX_COLS",
    "MAX_SLOTS",
    "BoardError",
    "Cell",
    "Overlay",
    "OverlayRejection",
    "Separator",
    "SeparatorRejection",
    "Slot",
    "BoardDraft",
    "MadeBoard",
    "make_board",
    "HORIZONTAL",
    "VERTICAL",
]

MAX_ROWS = 4
MAX_COLS = 4
MAX_SLOTS = 10

HORIZONTAL = "H"
VERTICAL = "V"

BLACK = "#"
WHITE = "."
NEWLINE = "\n"


class BoardError(Exception):
    """A board draft, tag table or showing violates the stated limits."""


# ---------------------------------------------------------------------------
# Grid value types


@dataclass(frozen=True)
class Cell:
    """One grid cell, pinned to one decoded character of one version."""

    row: int
    col: int
    kind: str  # "black" | "white" | "letter"
    char: str  # "#" / "." / the printed letter
    char_index: int  # character index in the version text
    byte_start: int
    byte_end: int

    @property
    def byte_range(self) -> tuple[int, int]:
        return self.byte_start, self.byte_end

    @property
    def is_white(self) -> bool:
        return self.kind == "white"


@dataclass(frozen=True)
class Overlay:
    """A transparent re-lettering pinned to an origin-version byte range."""

    value: str
    start_byte: int
    end_byte: int
    origin_version: int | None = None  # defaults to the draft version
    label: str = ""

    @property
    def byte_range(self) -> tuple[int, int]:
        return self.start_byte, self.end_byte


@dataclass(frozen=True)
class OverlayRejection:
    overlay: Overlay
    code: str  # bad_range | half_pair | partial_character | not_white |
    #          identity_rewritten | conflict
    reason: str


@dataclass(frozen=True)
class Separator:
    """A bar between ``(row, col)`` and ``(row, col + 1)`` in the origin."""

    row: int
    col: int
    origin_version: int | None = None


@dataclass(frozen=True)
class SeparatorRejection:
    separator: Separator
    code: str  # bad_origin | side_rewritten | not_adjacent
    reason: str


@dataclass(frozen=True)
class Slot:
    """A word slot: direction, start cell and per-position fixed letters."""

    id: str
    direction: str
    start_row: int
    start_col: int
    length: int
    fixed: tuple[str | None, ...]
    cells: tuple[tuple[int, int], ...]

    def fits(self, word: str) -> bool:
        if len(word) != self.length:
            return False
        return all(got is None or got == want for got, want in zip(self.fixed, word))


# ---------------------------------------------------------------------------
# Identity traversal


def _replace_records(version) -> list:
    return [r for r in version.records if r.kind == "replace"]


def _map_index_through(records: Sequence, old_index: int) -> int | None:
    """Map a base-version character index across one patch batch.

    Returns the new index, or ``None`` when the character was inside a
    replacement (its identity is dead).  Note that a replacement writing
    the identical character -- and therefore identical bytes -- still
    kills the identity: the decision follows the patch records, not a
    byte comparison.
    """
    delta = 0
    for rec in records:
        if rec.old_char_start <= old_index < rec.old_char_end:
            return None
        if old_index >= rec.old_char_end:
            delta += (rec.new_char_end - rec.new_char_start) - (
                rec.old_char_end - rec.old_char_start
            )
    return old_index + delta


# ---------------------------------------------------------------------------
# Draft


@dataclass
class BoardDraft:
    """A revision version interpreted as a rectangular crossword grid."""

    library: RevisionLibrary
    version_id: int
    rows: tuple[str, ...]
    cells: tuple[Cell, ...]
    _doc: object = field(repr=False)
    _coord_by_index: dict[int, tuple[int, int]] = field(repr=False)
    _drafts: dict[int, "BoardDraft"] = field(repr=False, default_factory=dict)

    # -- construction -----------------------------------------------------

    @classmethod
    def from_version(cls, library: RevisionLibrary, version_id: int) -> "BoardDraft":
        version = library.get(version_id)
        doc = decode_document(version.body)
        if doc.starts is None or doc.ends is None:
            raise BoardError("version decode lacks byte ranges")
        rows = tuple(doc.text.split(NEWLINE))
        if not rows or any(not row for row in rows):
            raise BoardError("board draft must have non-empty rows")
        if len(rows) > MAX_ROWS:
            raise BoardError(f"board has {len(rows)} rows, limit is {MAX_ROWS}")
        width = len(rows[0])
        if width > MAX_COLS:
            raise BoardError(f"board has {width} columns, limit is {MAX_COLS}")
        if any(len(row) != width for row in rows):
            raise BoardError("board draft rows must form a rectangle")

        cells: list[Cell] = []
        coord_by_index: dict[int, tuple[int, int]] = {}
        char_index = 0
        for r, row in enumerate(rows):
            for c, ch in enumerate(row):
                start = int(doc.starts[char_index])
                end = int(doc.ends[char_index])
                if ch == BLACK:
                    kind = "black"
                elif ch == WHITE:
                    kind = "white"
                else:
                    kind = "letter"
                cells.append(Cell(r, c, kind, ch, char_index, start, end))
                coord_by_index[char_index] = (r, c)
                char_index += 1
            # The newline itself is a decoded character but not a cell.
            if r < len(rows) - 1:
                char_index += 1
        if char_index != len(doc.text):
            raise BoardError("internal error: cell scan missed characters")

        draft = cls(
            library=library,
            version_id=version_id,
            rows=rows,
            cells=tuple(cells),
            _doc=doc,
            _coord_by_index=coord_by_index,
        )
        draft._drafts[version_id] = draft
        return draft

    # -- accessors --------------------------------------------------------

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return len(self.rows[0])

    def cell_at(self, row: int, col: int) -> Cell:
        if not (0 <= row < self.n_rows and 0 <= col < self.n_cols):
            raise BoardError(f"cell ({row},{col}) is outside the grid")
        return self.cells[row * self.n_cols + col]

    def _origin_draft(self, origin_version: int) -> "BoardDraft":
        if origin_version == self.version_id:
            return self
        if origin_version in self._drafts:
            return self._drafts[origin_version]
        origin = BoardDraft.from_version(self.library, origin_version)
        # Share the identity-cache across drafts of one library.
        origin._drafts = self._drafts
        self._drafts[origin_version] = origin
        return origin

    def _version_chain(self, origin_version: int) -> list[tuple[int, int]]:
        """Return ``(base_id, new_id)`` hops from origin to this version."""
        hops: list[tuple[int, int]] = []
        current = self.version_id
        seen: set[int] = set()
        while current != origin_version:
            if current in seen:
                raise BoardError("version chain contains a cycle")
            seen.add(current)
            version = self.library.get(current)
            if version.parent_version is None:
                raise BoardError(
                    f"origin version {origin_version} is not an ancestor of "
                    f"draft version {self.version_id}"
                )
            hops.append((version.parent_version, current))
            current = version.parent_version
        hops.reverse()
        return hops

    def map_identity(self, origin_version: int, origin_char_index: int) -> int | None:
        """Follow one cell identity from an origin version to the draft.

        Returns the draft character index, or ``None`` if a replacement
        killed the identity along the way.  Patch records are stored on the
        new version produced by each batch.
        """
        index: int | None = origin_char_index
        for _base_id, new_id in self._version_chain(origin_version):
            new_version = self.library.get(new_id)
            index = _map_index_through(_replace_records(new_version), index)  # type: ignore[arg-type]
            if index is None:
                return None
        return index

    # -- overlays ---------------------------------------------------------

    def evaluate_overlays(
        self, overlays: Sequence[Overlay]
    ) -> tuple[dict[tuple[int, int], str], list[OverlayRejection]]:
        """Validate overlays; return ``{(row, col): value}`` and rejections."""
        effective: dict[tuple[int, int], list[Overlay]] = {}
        rejections: list[OverlayRejection] = []

        for overlay in overlays:
            origin_id = (
                overlay.origin_version
                if overlay.origin_version is not None
                else self.version_id
            )
            try:
                origin = self._origin_draft(origin_id)
            except (BoardError, RevisionError) as exc:
                rejections.append(
                    OverlayRejection(overlay, "bad_origin", str(exc))
                )
                continue

            located = self._locate_origin_range(origin, overlay)
            if located is None:
                continue
            origin_cell, rejection = located
            if rejection is not None:
                rejections.append(rejection)
                continue

            target_index = self.map_identity(origin_id, origin_cell.char_index)
            if target_index is None:
                rejections.append(
                    OverlayRejection(
                        overlay,
                        "identity_rewritten",
                        f"cell ({origin_cell.row},{origin_cell.col}) of v{origin_id} "
                        "was touched by a replacement; its identity is dead even if "
                        "the rewritten character has the same value and bytes",
                    )
                )
                continue
            coord = self._coord_by_index.get(target_index)
            if coord is None:
                rejections.append(
                    OverlayRejection(
                        overlay,
                        "identity_rewritten",
                        f"mapped character index {target_index} is not a grid cell",
                    )
                )
                continue
            effective.setdefault(coord, []).append(overlay)

        mounted: dict[tuple[int, int], str] = {}
        for coord, group in effective.items():
            values = {o.value for o in group}
            if len(values) > 1:
                for overlay in group:
                    rejections.append(
                        OverlayRejection(
                            overlay,
                            "conflict",
                            f"competing overlays on cell {coord}: "
                            + ", ".join(sorted(values)),
                        )
                    )
                continue
            mounted[coord] = next(iter(values))
        return mounted, rejections

    def _locate_origin_range(
        self, origin: "BoardDraft", overlay: Overlay
    ) -> tuple[Cell, OverlayRejection | None] | None:
        """Find the origin cell a byte range points at, or why it cannot."""
        s, e = overlay.start_byte, overlay.end_byte
        if s >= e:
            return None, OverlayRejection(
                overlay, "bad_range", f"empty or inverted byte range [{s},{e})"
            )
        match: Cell | None = None
        container: Cell | None = None
        for cell in origin.cells:
            if cell.byte_start == s and cell.byte_end == e:
                match = cell
                break
            if cell.byte_start <= s < cell.byte_end and e <= cell.byte_end:
                container = cell
        if match is None:
            if container is not None:
                mode = origin._doc.modes[container.char_index]
                if mode == MODE_PAIR and container.byte_end - container.byte_start == 2:
                    return None, OverlayRejection(
                        overlay,
                        "half_pair",
                        f"range [{s},{e}) lands inside two-byte PAIR character "
                        f"{container.char!r} at cell ({container.row},{container.col}) "
                        f"[{container.byte_start},{container.byte_end}); a half PAIR "
                        "cell cannot carry an overlay",
                    )
                return None, OverlayRejection(
                    overlay,
                    "partial_character",
                    f"range [{s},{e}) is only part of character {container.char!r} "
                    f"[{container.byte_start},{container.byte_end})",
                )
            return None, OverlayRejection(
                overlay,
                "bad_range",
                f"range [{s},{e}) does not coincide with one complete character "
                "(it may span several characters or land on switch-only bytes)",
            )
        if not match.is_white:
            return None, OverlayRejection(
                overlay,
                "not_white",
                f"cell ({match.row},{match.col}) of the origin is {match.kind} "
                f"({match.char!r}); overlays may only cover white '.' cells",
            )
        return match, None

    # -- separators -------------------------------------------------------

    def evaluate_separators(
        self, separators: Sequence[Separator]
    ) -> tuple[set[tuple[int, int]], list[SeparatorRejection]]:
        """Validate separators; return mounted edge set ``{(row, col)}``.

        An edge ``(row, col)`` sits between columns ``col`` and ``col + 1``.
        """
        mounted: set[tuple[int, int]] = set()
        rejections: list[SeparatorRejection] = []
        for sep in separators:
            origin_id = (
                sep.origin_version if sep.origin_version is not None else self.version_id
            )
            try:
                origin = self._origin_draft(origin_id)
            except (BoardError, RevisionError) as exc:
                rejections.append(SeparatorRejection(sep, "bad_origin", str(exc)))
                continue
            if not (0 <= sep.row < origin.n_rows and 0 <= sep.col < origin.n_cols - 1):
                rejections.append(
                    SeparatorRejection(
                        sep,
                        "bad_origin",
                        f"edge ({sep.row},{sep.col}) is not between two adjacent "
                        "origin cells",
                    )
                )
                continue
            left = origin.cell_at(sep.row, sep.col)
            right = origin.cell_at(sep.row, sep.col + 1)
            left_index = self.map_identity(origin_id, left.char_index)
            right_index = self.map_identity(origin_id, right.char_index)
            if left_index is None or right_index is None:
                rejections.append(
                    SeparatorRejection(
                        sep,
                        "side_rewritten",
                        "a cell on one side was replaced; its identity is dead",
                    )
                )
                continue
            left_coord = self._coord_by_index.get(left_index)
            right_coord = self._coord_by_index.get(right_index)
            if left_coord is None or right_coord is None:
                rejections.append(
                    SeparatorRejection(
                        sep, "side_rewritten", "a side identity left the grid"
                    )
                )
                continue
            if (
                left_coord[0] != right_coord[0]
                or right_coord[1] != left_coord[1] + 1
            ):
                rejections.append(
                    SeparatorRejection(
                        sep,
                        "not_adjacent",
                        f"the two sides survive at {left_coord} and {right_coord} "
                        "but are no longer adjacent",
                    )
                )
                continue
            mounted.add(left_coord)
        return mounted, rejections


# ---------------------------------------------------------------------------
# Made board / slots


@dataclass(frozen=True)
class MadeBoard:
    """The physical board: fixed letters, mounted items and word slots."""

    draft: BoardDraft
    overlays: Mapping[tuple[int, int], str]
    separators: frozenset[tuple[int, int]]
    slots: tuple[Slot, ...]
    overlay_rejections: tuple[OverlayRejection, ...]
    separator_rejections: tuple[SeparatorRejection, ...]

    def fixed_at(self, row: int, col: int) -> str | None:
        cell = self.draft.cell_at(row, col)
        if cell.kind == "black":
            return BLACK
        if cell.kind == "letter":
            return cell.char
        return self.overlays.get((row, col))

    def slot_by_id(self, slot_id: str) -> Slot:
        for slot in self.slots:
            if slot.id == slot_id:
                return slot
        raise BoardError(f"unknown slot {slot_id}")

    def crossings(self) -> dict[tuple[int, int], list[tuple[str, int]]]:
        """Cell coordinate -> every ``(slot id, position)`` covering it."""
        out: dict[tuple[int, int], list[tuple[str, int]]] = {}
        for slot in self.slots:
            for pos, coord in enumerate(slot.cells):
                out.setdefault(coord, []).append((slot.id, pos))
        return out

    def render(self, words: Mapping[str, str] | None = None) -> str:
        """Render the board face; rows joined by '/'."""
        lines: list[str] = []
        for r in range(self.draft.n_rows):
            line = []
            for c in range(self.draft.n_cols):
                cell = self.draft.cell_at(r, c)
                if cell.kind == "black":
                    line.append(BLACK)
                    continue
                fixed = self.overlays.get((r, c))
                if fixed is None and cell.kind == "letter":
                    fixed = cell.char
                line.append(fixed if fixed is not None else WHITE)
            lines.append("".join(line))
        face = "/".join(lines)
        if not words:
            return face
        filled = [list(row) for row in face.split("/")]
        for slot in self.slots:
            word = words.get(slot.id)
            if word is None:
                continue
            for pos, (r, c) in enumerate(slot.cells):
                filled[r][c] = word[pos]
        return "/".join("".join(row) for row in filled)


def _build_slots(
    draft: BoardDraft,
    overlays: Mapping[tuple[int, int], str],
    separators: frozenset[tuple[int, int]],
) -> tuple[Slot, ...]:
    slots: list[Slot] = []

    def fixed_letter(row: int, col: int) -> str | None:
        cell = draft.cell_at(row, col)
        if cell.kind == "letter":
            return cell.char
        return overlays.get((row, col))

    def emit(direction: str, run_cells: list[tuple[int, int]]) -> None:
        start_row, start_col = run_cells[0]
        slot_id = f"{direction}({start_row},{start_col})"
        fixed = tuple(fixed_letter(r, c) for r, c in run_cells)
        slots.append(
            Slot(
                id=slot_id,
                direction=direction,
                start_row=start_row,
                start_col=start_col,
                length=len(run_cells),
                fixed=fixed,
                cells=tuple(run_cells),
            )
        )

    # Horizontal runs, split at black cells and mounted separators.
    for r in range(draft.n_rows):
        run: list[tuple[int, int]] = []
        for c in range(draft.n_cols):
            cell = draft.cell_at(r, c)
            if cell.kind == "black":
                if run:
                    emit(HORIZONTAL, run)
                    run = []
                continue
            if run and (r, c - 1) in separators:
                emit(HORIZONTAL, run)
                run = []
            run.append((r, c))
        if run:
            emit(HORIZONTAL, run)

    # Vertical runs, split only at black cells; separators are horizontal.
    for c in range(draft.n_cols):
        run = []
        for r in range(draft.n_rows):
            cell = draft.cell_at(r, c)
            if cell.kind == "black":
                if run:
                    emit(VERTICAL, run)
                    run = []
                continue
            run.append((r, c))
        if run:
            emit(VERTICAL, run)

    if len(slots) > MAX_SLOTS:
        raise BoardError(f"board has {len(slots)} slots, limit is {MAX_SLOTS}")
    return tuple(slots)


def make_board(
    draft: BoardDraft,
    overlays: Sequence[Overlay] = (),
    separators: Sequence[Separator] = (),
) -> MadeBoard:
    """Validate every mounted item and derive the word slots.

    Invalid overlays and separators are reported item by item and left off
    the board.  No word list is consulted: a slot is any maximal run the
    grid geometry defines, including runs of length one.
    """
    mounted_overlays, overlay_rejections = draft.evaluate_overlays(overlays)
    mounted_separators, separator_rejections = draft.evaluate_separators(separators)
    slots = _build_slots(draft, mounted_overlays, frozenset(mounted_separators))
    return MadeBoard(
        draft=draft,
        overlays=mounted_overlays,
        separators=frozenset(mounted_separators),
        slots=slots,
        overlay_rejections=tuple(overlay_rejections),
        separator_rejections=tuple(separator_rejections),
    )
