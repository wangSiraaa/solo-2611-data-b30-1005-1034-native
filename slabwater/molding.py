"""Foam negative-mold planning on top of one *published* slabwater plan.

The published plan already fixed the grid, the opened outlets and the elevation
revision.  This module never re-decides any of that: it reads the stored water
grid and turns each cell's storage depth into a target foam thickness

    target[cell] = blank_height M - storage[cell],   M >= max(storage).

A small plane knife is described by its edges: for every edge a grid offset from
the holder position and a *lift* -- how much higher that edge's bottom sits than
the holder reference.  With the holder fixed at height ``h`` over a board of
blank height ``M``, an edge at lift ``b`` leaves cell ``c`` at thickness
``h + b`` (the holder reference points downward, so a higher/lifted edge cuts
less deep).  A stroke is a sequence of adjacent holder positions walked with one
fixed holder height; every edge must land inside the board at every position.

At one holder position a single cell can be grazed by several edges of the same
knife (the multi-edge case the task statement calls out).  The lowest edge
decides the cut: ``cut_to(c) = min(h + b)`` over every edge landing on ``c``.
A stroke at height ``h`` is safe only when ``cut_to(c) >= target(c)`` for every
cell it touches -- it must never plane a cell thinner than its target.

The *minimum safe holder height* of a stroke is therefore::

    h_safe = max over every touch (cell c, edge lift b) of (target[c] - b).

At exactly ``h_safe`` every touched cell has a binding touch (an edge whose
bottom reaches its target); the cell(s) attaining the maximum are precisely the
ones that limit the whole stroke.  A cell is "exactly reachable" by the stroke
only when its lowest grazing edge actually lands on its target.

Everything here is integer and purely a synthetic rule verifier -- not a real
machining model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .model import Cell, canonical_json, is_int, sha256_hex

_NEIGHBOURS = frozenset(((1, 0), (-1, 0), (0, 1), (0, -1)))


class MoldError(ValueError):
    """Raised when a mold specification is structurally invalid."""


@dataclass(frozen=True)
class Edge:
    """One cutting edge: offset from the holder and lift above its reference."""

    dr: int
    dc: int
    lift: int

    def __post_init__(self) -> None:
        if not (is_int(self.dr) and is_int(self.dc) and is_int(self.lift)):
            raise MoldError("edge dr, dc and lift must be integers")
        if self.lift < 0:
            raise MoldError("edge lift must be non-negative (edge bottom above holder reference)")

    def to_dict(self) -> dict[str, Any]:
        return {"dr": self.dr, "dc": self.dc, "lift": self.lift}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Edge":
        if not isinstance(raw, Mapping):
            raise MoldError("edge must be an object")
        return cls(dr=raw.get("dr"), dc=raw.get("dc"), lift=raw.get("lift"))


@dataclass(frozen=True)
class Tool:
    """A plane knife type: fixed edge geometry, mounted once for any fee."""

    id: str
    edges: tuple[Edge, ...]
    mount_fee: int

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise MoldError("tool id must be a non-empty string")
        if not self.edges:
            raise MoldError(f"tool {self.id} must define at least one edge")
        # Two distinct edges may occupy the same grid offset at different lifts
        # (the multi-edge case: one cell is grazed by a high and a low edge of
        # the same knife); only an exactly duplicated edge is invalid.
        signatures = [(edge.dr, edge.dc, edge.lift) for edge in self.edges]
        if len(set(signatures)) != len(signatures):
            raise MoldError(f"tool {self.id} has two identical edges")
        if not is_int(self.mount_fee) or self.mount_fee < 0:
            raise MoldError(f"tool {self.id} mount_fee must be a non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "edges": [edge.to_dict() for edge in self.edges],
            "mount_fee": self.mount_fee,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Tool":
        if not isinstance(raw, Mapping):
            raise MoldError("tool must be an object")
        tool_id = raw.get("id")
        if not isinstance(tool_id, str) or not tool_id:
            raise MoldError("tool id must be a non-empty string")
        edges_raw = raw.get("edges")
        if not isinstance(edges_raw, (list, tuple)) or not edges_raw:
            raise MoldError(f"tool {tool_id} must define at least one edge")
        edges = tuple(Edge.from_dict(item) for item in edges_raw)
        mount_fee = raw.get("mount_fee")
        if not is_int(mount_fee) or mount_fee < 0:
            raise MoldError(f"tool {tool_id} mount_fee must be a non-negative integer")
        return cls(id=tool_id, edges=edges, mount_fee=mount_fee)


@dataclass(frozen=True)
class StrokeSpec:
    """One approved stroke: a contiguous path of holder positions for one tool.

    ``sticker`` is the physical one-time positioning sticker group this stroke
    peels if used; ``None`` means no sticker is needed.  Strokes sharing one
    sticker id compete for that single physical sticker -- at most one of them
    may be selected in one work order.
    """

    id: str
    tool_id: str
    path: tuple[Cell, ...]
    labor_fee: int
    sticker: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise MoldError("stroke id must be a non-empty string")
        if not isinstance(self.tool_id, str) or not self.tool_id:
            raise MoldError(f"stroke {self.id} tool_id must be a non-empty string")
        if not self.path or not all(
            isinstance(cell, tuple) and len(cell) == 2
            and is_int(cell[0]) and is_int(cell[1])
            for cell in self.path
        ):
            raise MoldError(f"stroke {self.id} path must be non-empty [row, col] positions")
        if len(set(self.path)) != len(self.path):
            raise MoldError(f"stroke {self.id} revisits a holder position")
        for prev, nxt in zip(self.path, self.path[1:]):
            if (nxt[0] - prev[0], nxt[1] - prev[1]) not in _NEIGHBOURS:
                raise MoldError(
                    f"stroke {self.id} holder positions must be four-neighbour adjacent"
                )
        if not is_int(self.labor_fee) or self.labor_fee < 0:
            raise MoldError(f"stroke {self.id} labor_fee must be a non-negative integer")
        if self.sticker is not None and (not isinstance(self.sticker, str) or not self.sticker):
            raise MoldError(f"stroke {self.id} sticker must be null or a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool_id": self.tool_id,
            "path": [[r, c] for r, c in self.path],
            "labor_fee": self.labor_fee,
            "sticker": self.sticker,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StrokeSpec":
        if not isinstance(raw, Mapping):
            raise MoldError("stroke must be an object")
        stroke_id = raw.get("id")
        tool_id = raw.get("tool_id")
        path_raw = raw.get("path")
        if not isinstance(path_raw, (list, tuple)) or not path_raw:
            raise MoldError(f"stroke {stroke_id} path must be a non-empty list of [row, col]")
        path = [(item[0], item[1]) for item in path_raw]
        return cls(
            id=stroke_id,
            tool_id=tool_id,
            path=tuple(path),
            labor_fee=raw.get("labor_fee"),
            sticker=raw.get("sticker"),
        )


@dataclass(frozen=True)
class Touch:
    """One edge landing on one cell at one holder position of a stroke."""

    cell: Cell
    holder: Cell
    edge: Edge


@dataclass(frozen=True)
class StrokeAnalysis:
    """Safety analysis of one stroke at its minimum safe holder height."""

    spec: StrokeSpec
    tool: Tool
    touches: tuple[Touch, ...]
    min_safe_height: int
    # Per touched cell: the thickness the lowest grazing edge leaves (== target
    # minus nothing; may still sit above the target), and the cut depth.
    cut_to: Mapping[Cell, int]
    cut_depth: Mapping[Cell, int]
    # Cells whose lowest edge reaches exactly their target.
    exact_cells: frozenset[Cell]
    # Cells touched but still thicker than target after this stroke.
    short_cells: frozenset[Cell]
    # Touch(es) attaining the global max(target - lift): what limits the height.
    limiting: tuple[Touch, ...]

    @property
    def limiting_cells(self) -> tuple[Cell, ...]:
        seen: dict[Cell, None] = {}
        for touch in self.limiting:
            seen.setdefault(touch.cell, None)
        return tuple(seen)

    def to_dict(self, height: int, width: int) -> dict[str, Any]:
        grid_cut = [[None] * width for _ in range(height)]
        grid_depth = [[0] * width for _ in range(height)]
        for (r, c), value in self.cut_to.items():
            grid_cut[r][c] = value
            grid_depth[r][c] = self.cut_depth[(r, c)]
        return {
            "stroke_id": self.spec.id,
            "tool_id": self.tool.id,
            "sticker": self.spec.sticker,
            "path": [[r, c] for r, c in self.spec.path],
            "labor_fee": self.spec.labor_fee,
            "min_safe_height": self.min_safe_height,
            "cut_to_grid": grid_cut,
            "cut_depth_grid": grid_depth,
            "exact_cells": [[r, c] for r, c in sorted(self.exact_cells)],
            "short_cells": [[r, c] for r, c in sorted(self.short_cells)],
            "limiting_cells": [[r, c] for r, c in self.limiting_cells],
            "limiting_touches": [
                {
                    "cell": [t.cell[0], t.cell[1]],
                    "holder": [t.holder[0], t.holder[1]],
                    "edge": t.edge.to_dict(),
                    "required_holder_height": self.min_safe_height,
                }
                for t in self.limiting
            ],
        }


@dataclass(frozen=True)
class MoldTarget:
    """The frozen target surface derived from one published plan."""

    height: int
    width: int
    blank_height: int
    storage: tuple[tuple[int, ...], ...]
    target: tuple[tuple[int, ...], ...]
    plan_id: str
    catalog_version: int
    elevation_hash: str
    opened: tuple[str, ...]

    def at(self, cell: Cell) -> int:
        return self.target[cell[0]][cell[1]]

    def in_board(self, cell: Cell) -> bool:
        r, c = cell
        return 0 <= r < self.height and 0 <= c < self.width

    def to_dict(self) -> dict[str, Any]:
        return {
            "height": self.height,
            "width": self.width,
            "blank_height": self.blank_height,
            "plan_id": self.plan_id,
            "catalog_version": self.catalog_version,
            "elevation_hash": self.elevation_hash,
            "opened": list(self.opened),
            "storage": [list(row) for row in self.storage],
            "target": [list(row) for row in self.target],
        }


@dataclass(frozen=True)
class MoldSpec:
    """A complete mold production order specification (knives + approved strokes)."""

    target: MoldTarget
    tools: tuple[Tool, ...]
    strokes: tuple[StrokeSpec, ...]
    rack_capacity: int
    max_strokes: int = 18
    max_tools: int = 6

    def tool(self, tool_id: str) -> Tool:
        for tool in self.tools:
            if tool.id == tool_id:
                return tool
        raise MoldError(f"unknown tool: {tool_id}")

    def sticker_ids(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for stroke in self.strokes:
            if stroke.sticker is not None:
                seen.setdefault(stroke.sticker, None)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "tools": [tool.to_dict() for tool in self.tools],
            "strokes": [stroke.to_dict() for stroke in self.strokes],
            "rack_capacity": self.rack_capacity,
            "max_strokes": self.max_strokes,
            "max_tools": self.max_tools,
        }


@dataclass(frozen=True)
class Selection:
    """One evaluated set of strokes run at their minimum safe heights."""

    stroke_ids: tuple[str, ...]
    tool_ids: tuple[str, ...]
    feasible: bool
    total_fee: int
    mount_fee: int
    labor_fee: int
    # Resulting per-cell thickness; blank where no selected stroke touches.
    result_grid: tuple[tuple[int, ...], ...]
    target_grid: tuple[tuple[int, ...], ...]
    exact_cells: frozenset[Cell]
    missing_cells: frozenset[Cell]
    overcut_cells: frozenset[Cell]
    sticker_used: str | None
    sticker_conflicts: tuple[str, ...]
    rack_ok: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "stroke_ids": list(self.stroke_ids),
            "tool_ids": list(self.tool_ids),
            "feasible": self.feasible,
            "total_fee": self.total_fee,
            "mount_fee": self.mount_fee,
            "labor_fee": self.labor_fee,
            "result_grid": [list(row) for row in self.result_grid],
            "target_grid": [list(row) for row in self.target_grid],
            "exact_cells": [[r, c] for r, c in sorted(self.exact_cells)],
            "missing_cells": [[r, c] for r, c in sorted(self.missing_cells)],
            "overcut_cells": [[r, c] for r, c in sorted(self.overcut_cells)],
            "sticker_used": self.sticker_used,
            "sticker_conflicts": list(self.sticker_conflicts),
            "rack_ok": self.rack_ok,
        }


@dataclass(frozen=True)
class SelectionReport:
    """Outcome of the minimum-fee search over the approved strokes."""

    ok: bool
    reason: str | None
    best: Selection | None
    candidates_evaluated: int
    forbidden_stickers: frozenset[str]
    consumed_stickers: frozenset[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "best": self.best.to_dict() if self.best else None,
            "candidates_evaluated": self.candidates_evaluated,
            "forbidden_stickers": sorted(self.forbidden_stickers),
            "consumed_stickers": sorted(self.consumed_stickers),
        }


# ---------------------------------------------------------------------------
# Construction / validation
# ---------------------------------------------------------------------------

def target_from_plan(artifact: Mapping[str, Any], blank_height: int) -> MoldTarget:
    """Derive the frozen mold target from a *published* plan artifact.

    The plan's own grid, opened outlets and elevation version are taken as the
    source of truth; this function refuses anything that does not look like a
    stored successful plan rather than recomputing hydraulics itself.
    """

    if not isinstance(artifact, Mapping):
        raise MoldError("plan artifact must be an object")
    storage_raw = artifact.get("storage")
    if not isinstance(storage_raw, (list, tuple)) or not storage_raw:
        raise MoldError("plan artifact has no storage grid")
    storage_rows: list[tuple[int, ...]] = []
    for line in storage_raw:
        if not isinstance(line, (list, tuple)) or not line:
            raise MoldError("plan storage grid must be a non-empty matrix")
        if not all(is_int(v) and v >= 0 for v in line):
            raise MoldError("plan storage values must be non-negative integers")
        storage_rows.append(tuple(int(v) for v in line))
    width = len(storage_rows[0])
    if any(len(row) != width for row in storage_rows):
        raise MoldError("plan storage grid must be rectangular")
    storage = tuple(storage_rows)
    max_depth = max(max(row) for row in storage)
    if not is_int(blank_height) or blank_height < max_depth:
        raise MoldError(
            f"blank height M must be an integer >= max storage depth {max_depth}"
        )
    target = tuple(
        tuple(blank_height - storage[r][c] for c in range(width))
        for r in range(len(storage))
    )
    opened_raw = artifact.get("opened", [])
    if not isinstance(opened_raw, (list, tuple)) or not all(isinstance(v, str) for v in opened_raw):
        raise MoldError("plan artifact opened outlets must be a list of ids")
    return MoldTarget(
        height=len(storage),
        width=width,
        blank_height=blank_height,
        storage=storage,
        target=target,
        plan_id=str(artifact.get("plan_id", "")),
        catalog_version=int(artifact.get("catalog_version", -1)),
        elevation_hash=str(artifact.get("elevation_hash", "")),
        opened=tuple(opened_raw),
    )


def build_spec(
    target: MoldTarget,
    tools: Sequence[Mapping[str, Any] | Tool],
    strokes: Sequence[Mapping[str, Any] | StrokeSpec],
    rack_capacity: int,
    max_strokes: int = 18,
    max_tools: int = 6,
) -> MoldSpec:
    """Validate knives and approved strokes against the frozen target board."""

    if not is_int(rack_capacity) or rack_capacity <= 0:
        raise MoldError("rack capacity must be a positive integer")
    if not is_int(max_strokes) or not is_int(max_tools):
        raise MoldError("candidate limits must be integers")
    parsed_tools: list[Tool] = []
    tool_ids: set[str] = set()
    for raw in tools:
        tool = raw if isinstance(raw, Tool) else Tool.from_dict(raw)
        if tool.id in tool_ids:
            raise MoldError(f"duplicate tool id: {tool.id}")
        tool_ids.add(tool.id)
        parsed_tools.append(tool)
    if len(parsed_tools) > max_tools:
        raise MoldError(f"at most {max_tools} knife types are allowed, got {len(parsed_tools)}")
    parsed_strokes: list[StrokeSpec] = []
    stroke_ids: set[str] = set()
    for raw in strokes:
        stroke = raw if isinstance(raw, StrokeSpec) else StrokeSpec.from_dict(raw)
        if stroke.id in stroke_ids:
            raise MoldError(f"duplicate stroke id: {stroke.id}")
        stroke_ids.add(stroke.id)
        if stroke.tool_id not in tool_ids:
            raise MoldError(f"stroke {stroke.id} references unknown tool {stroke.tool_id}")
        parsed_strokes.append(stroke)
    if len(parsed_strokes) > max_strokes:
        raise MoldError(
            f"at most {max_strokes} candidate strokes are allowed, got {len(parsed_strokes)}"
        )
    spec = MoldSpec(
        target=target,
        tools=tuple(parsed_tools),
        strokes=tuple(parsed_strokes),
        rack_capacity=rack_capacity,
        max_strokes=max_strokes,
        max_tools=max_tools,
    )
    # Geometry is validated per stroke now so an unusable order is rejected up
    # front rather than silently dropped from the search.
    for stroke in spec.strokes:
        analyze_stroke(spec, stroke.id)
    return spec


def spec_from_dict(raw: Mapping[str, Any]) -> MoldSpec:
    if not isinstance(raw, Mapping):
        raise MoldError("mold spec must be an object")
    target_raw = raw.get("target")
    if not isinstance(target_raw, Mapping):
        raise MoldError("mold spec needs a target object")
    target = MoldTarget(
        height=int(target_raw["height"]),
        width=int(target_raw["width"]),
        blank_height=int(target_raw["blank_height"]),
        storage=tuple(tuple(v) for v in target_raw["storage"]),
        target=tuple(tuple(v) for v in target_raw["target"]),
        plan_id=str(target_raw.get("plan_id", "")),
        catalog_version=int(target_raw.get("catalog_version", -1)),
        elevation_hash=str(target_raw.get("elevation_hash", "")),
        opened=tuple(target_raw.get("opened", ())),
    )
    return build_spec(
        target=target,
        tools=list(raw["tools"]),
        strokes=list(raw["strokes"]),
        rack_capacity=int(raw["rack_capacity"]),
        max_strokes=int(raw.get("max_strokes", 18)),
        max_tools=int(raw.get("max_tools", 6)),
    )


# ---------------------------------------------------------------------------
# Stroke geometry and safety analysis
# ---------------------------------------------------------------------------

def _touches(spec: MoldSpec, stroke: StrokeSpec) -> tuple[Touch, ...]:
    tool = spec.tool(stroke.tool_id)
    touches: list[Touch] = []
    for holder in stroke.path:
        for edge in tool.edges:
            cell = (holder[0] + edge.dr, holder[1] + edge.dc)
            if not spec.target.in_board(cell):
                raise MoldError(
                    f"stroke {stroke.id} at holder {list(holder)} lands edge "
                    f"{edge.to_dict()} outside the {spec.target.height}x{spec.target.width} board"
                )
            touches.append(Touch(cell=cell, holder=holder, edge=edge))
    return tuple(touches)


def analyze_stroke(spec: MoldSpec, stroke_id: str) -> StrokeAnalysis:
    """Compute the minimum safe holder height and the per-cell cut record."""

    stroke = next((s for s in spec.strokes if s.id == stroke_id), None)
    if stroke is None:
        raise MoldError(f"unknown stroke: {stroke_id}")
    tool = spec.tool(stroke.tool_id)
    touches = _touches(spec, stroke)
    # h_safe = max(target[c] - lift) over every touch; every other height is
    # either unsafe (overcut) or leaves every binding cell above target.
    required = [(spec.target.at(t.cell) - t.edge.lift, t) for t in touches]
    raw_max = max(value for value, _ in required)
    # The holder reference cannot go below the board bottom; when every edge
    # sits above a zero target the stroke simply cannot reach that cell.
    h_safe = max(0, raw_max)
    limiting = tuple(t for value, t in required if value == raw_max)
    # Lowest edge per cell decides: cut_to = min(h_safe + lift).
    lowest: dict[Cell, int] = {}
    for touch in touches:
        bottom = h_safe + touch.edge.lift
        if touch.cell not in lowest or bottom < lowest[touch.cell]:
            lowest[touch.cell] = bottom
    cut_to = dict(sorted(lowest.items()))
    cut_depth = {
        cell: spec.target.blank_height - value for cell, value in cut_to.items()
    }
    exact = frozenset(cell for cell, value in cut_to.items() if value == spec.target.at(cell))
    short = frozenset(cell for cell, value in cut_to.items() if value > spec.target.at(cell))
    # Safety invariant: a stroke at its own minimum safe height never overcuts.
    assert not any(value < spec.target.at(cell) for cell, value in cut_to.items())
    return StrokeAnalysis(
        spec=stroke,
        tool=tool,
        touches=touches,
        min_safe_height=h_safe,
        cut_to=cut_to,
        cut_depth=cut_depth,
        exact_cells=exact,
        short_cells=short,
        limiting=limiting,
    )


def analyze_all(spec: MoldSpec) -> tuple[StrokeAnalysis, ...]:
    return tuple(analyze_stroke(spec, stroke.id) for stroke in spec.strokes)


def evaluate_selection(
    spec: MoldSpec,
    stroke_ids: Sequence[str],
    forbidden_stickers: frozenset[str] = frozenset(),
    consumed_stickers: frozenset[str] = frozenset(),
) -> Selection:
    """Evaluate one concrete set of strokes at their minimum safe heights.

    Multiple strokes grazing one cell do not stack: the resulting thickness is
    the minimum of the per-stroke ``cut_to`` values, and the blank survives
    wherever no selected edge lands.
    """

    wanted = list(stroke_ids)
    if len(set(wanted)) != len(wanted):
        raise MoldError("stroke list contains duplicates")
    by_id = {stroke.id: stroke for stroke in spec.strokes}
    unknown = [sid for sid in wanted if sid not in by_id]
    if unknown:
        raise MoldError(f"unknown strokes: {unknown}")
    t = spec.target
    result = [[t.blank_height] * t.width for _ in range(t.height)]
    exact: set[Cell] = set()
    overcut: set[Cell] = set()
    tool_set: set[str] = set()
    labor = 0
    stickers: list[str] = []
    analyses = [analyze_stroke(spec, sid) for sid in wanted]
    for analysis in analyses:
        labor += analysis.spec.labor_fee
        tool_set.add(analysis.tool.id)
        if analysis.spec.sticker is not None:
            stickers.append(analysis.spec.sticker)
        for (r, c), value in analysis.cut_to.items():
            if value < result[r][c]:
                result[r][c] = value
    for r in range(t.height):
        for c in range(t.width):
            if result[r][c] == t.target[r][c]:
                exact.add((r, c))
            elif result[r][c] < t.target[r][c]:
                overcut.add((r, c))
    missing = frozenset(
        (r, c)
        for r in range(t.height)
        for c in range(t.width)
        if result[r][c] > t.target[r][c]
    )
    duplicate_stickers = tuple(sorted({s for s in stickers if stickers.count(s) > 1}))
    sticker_blocked = tuple(sorted(set(stickers) & (forbidden_stickers | consumed_stickers)))
    sticker_used = stickers[0] if stickers else None
    rack_ok = len(tool_set) <= spec.rack_capacity
    feasible = (
        not missing
        and not overcut
        and not duplicate_stickers
        and not sticker_blocked
        and rack_ok
    )
    mount = sum(spec.tool(tool_id).mount_fee for tool_id in tool_set)
    return Selection(
        stroke_ids=tuple(sorted(wanted)),
        tool_ids=tuple(sorted(tool_set)),
        feasible=feasible,
        total_fee=mount + labor,
        mount_fee=mount,
        labor_fee=labor,
        result_grid=tuple(tuple(row) for row in result),
        target_grid=t.target,
        exact_cells=frozenset(exact),
        missing_cells=missing,
        overcut_cells=frozenset(overcut),
        sticker_used=sticker_used,
        sticker_conflicts=tuple(sorted(set(duplicate_stickers) | set(sticker_blocked))),
        rack_ok=rack_ok,
    )


def optimize(
    spec: MoldSpec,
    forbidden_stickers: Sequence[str] = (),
    consumed_stickers: Sequence[str] = (),
) -> SelectionReport:
    """Search every subset of the <= 18 approved strokes for the cheapest order.

    Only minimum-safe-height executions are considered: any higher holder cannot
    make a stroke cut deeper, so it can never help reach a target and is never
    part of an optimum.  Ties break deterministically on (fewest strokes, then
    lexicographic stroke id tuple), giving a stable work order.
    """

    forbidden = frozenset(forbidden_stickers)
    consumed = frozenset(consumed_stickers)
    blocked = forbidden | consumed
    analyses = analyze_all(spec)
    by_id = {a.spec.id: a for a in analyses}
    ids = tuple(a.spec.id for a in analyses)
    best: Selection | None = None
    best_key: tuple[int, int, tuple[str, ...]] | None = None
    evaluated = 0

    def consider(chosen: list[str]) -> None:
        nonlocal best, best_key, evaluated
        evaluated += 1
        selection = evaluate_selection(spec, chosen, forbidden_stickers=forbidden,
                                       consumed_stickers=consumed)
        if not selection.feasible:
            return
        key = (selection.total_fee, len(selection.stroke_ids), selection.stroke_ids)
        if best_key is None or key < best_key:
            best_key = key
            best = selection

    # DFS in stroke-id order; prune sticker groups (one physical sticker means
    # at most one stroke per group) and the rack (distinct mounted knives).
    def dfs(index: int, chosen: list[str], stickers_used: set[str], tools_used: set[str]) -> None:
        if index == len(ids):
            consider(chosen)
            return
        stroke = by_id[ids[index]].spec
        # Branch 1: skip this stroke.
        dfs(index + 1, chosen, stickers_used, tools_used)
        # Branch 2: take it, if no physical constraint is violated already.
        if stroke.sticker is not None:
            if stroke.sticker in stickers_used or stroke.sticker in blocked:
                return
        new_tool = stroke.tool_id not in tools_used
        if new_tool and len(tools_used) + 1 > spec.rack_capacity:
            return
        chosen.append(stroke.id)
        if stroke.sticker is not None:
            stickers_used.add(stroke.sticker)
        tools_used.add(stroke.tool_id)
        dfs(index + 1, chosen, stickers_used, tools_used)
        chosen.pop()
        if stroke.sticker is not None:
            stickers_used.discard(stroke.sticker)
        if new_tool:
            tools_used.discard(stroke.tool_id)

    dfs(0, [], set(), set())
    if best is None:
        reason = "no_feasible_stroke_set"
        if blocked:
            reason = "no_feasible_stroke_set_with_sticker_constraints"
        return SelectionReport(
            ok=False, reason=reason, best=None, candidates_evaluated=evaluated,
            forbidden_stickers=forbidden, consumed_stickers=consumed,
        )
    return SelectionReport(
        ok=True, reason=None, best=best, candidates_evaluated=evaluated,
        forbidden_stickers=forbidden, consumed_stickers=consumed,
    )


def review_grid(spec: MoldSpec, selection: Selection) -> list[list[dict[str, Any]]]:
    """One per-cell review row for the workshop drawing."""

    t = spec.target
    rows: list[list[dict[str, Any]]] = []
    analyses = {sid: analyze_stroke(spec, sid) for sid in selection.stroke_ids}
    for r in range(t.height):
        line: list[dict[str, Any]] = []
        for c in range(t.width):
            contributors = []
            for sid in selection.stroke_ids:
                analysis = analyses[sid]
                if (r, c) in analysis.cut_to:
                    contributors.append(
                        {
                            "stroke_id": sid,
                            "cut_to": analysis.cut_to[(r, c)],
                            "cut_depth": analysis.cut_depth[(r, c)],
                        }
                    )
            final = selection.result_grid[r][c]
            line.append(
                {
                    "cell": [r, c],
                    "target": t.target[r][c],
                    "storage_depth": t.storage[r][c],
                    "blank": t.blank_height,
                    "final": final,
                    "touched": bool(contributors),
                    "matches_target": final == t.target[r][c],
                    "contributors": sorted(contributors, key=lambda item: (item["cut_to"], item["stroke_id"])),
                }
            )
        rows.append(line)
    return rows


def spec_hash(spec: MoldSpec) -> str:
    return sha256_hex(canonical_json(spec.to_dict()))
