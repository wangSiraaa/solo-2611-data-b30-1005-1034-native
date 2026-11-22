"""Threshold-component lake ladders and old-name migration.

The atlas group builds, for a set of positive integer depth thresholds, the
four-neighbour connected components of "cells whose water depth is at least
this threshold".  Everything here is driven by *published per-cell water depth*
-- the ``storage`` grid of a signed plan -- never by raw cell height and never
by total storage.

Two properties matter and are enforced structurally:

* All cells at one threshold take effect simultaneously.  Components are the
  connected components of the whole threshold set, so a flat top can never be
  split into several spurious branches by iteration order.
* A deeper block belongs to exactly one shallower block.  Because the threshold
  cell sets are nested, every component at threshold ``t`` lies inside exactly
  one component at the next lower chosen threshold; the ladder records that
  unique parent and the matching children.

A block's identity is ``(threshold, cells)``: the same cell set appearing at a
different depth is still a distinct block ("different depth, same cell set").
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .model import Cell, SourceRef, is_int

_NEIGHBOURS = ((1, 0), (-1, 0), (0, 1), (0, -1))

#: An edition has at most this many blocks eligible to bear a name.  Since each
#: block takes at most one name, no migration can retain more names than this.
MAX_ELIGIBLE_BLOCKS_PER_EDITION = 18


class LakeError(Exception):
    """Raised for malformed depth grids, thresholds or old-name declarations."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


# ---------------------------------------------------------------------------
# Depth products (the only accepted naming source)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DepthProduct:
    """One official product's per-cell water depth, plus provenance.

    ``depths`` is the published ``storage`` matrix (``level - height``); it is
    the sole basis for naming.  ``sources`` maps each cell to the catalog tile
    contributions that produced the frozen surface, so every block can report
    where its cells came from.
    """

    version: int
    elevation_hash: str
    depths: tuple[tuple[int, ...], ...]
    sources: Mapping[Cell, tuple[SourceRef, ...]]

    @property
    def height(self) -> int:
        return len(self.depths)

    @property
    def width(self) -> int:
        return len(self.depths[0]) if self.depths else 0

    def depth_at(self, row: int, col: int) -> int:
        return self.depths[row][col]


def _normalize_grid(depths: Sequence[Sequence[int]]) -> tuple[tuple[int, ...], ...]:
    if not depths:
        raise LakeError("bad_depth_grid", "depth grid must have at least one row")
    width = None
    rows: list[tuple[int, ...]] = []
    for r, line in enumerate(depths):
        if not isinstance(line, (list, tuple)) or not line:
            raise LakeError("bad_depth_grid", f"depth row {r} is empty or not a sequence")
        if width is None:
            width = len(line)
        elif len(line) != width:
            raise LakeError("bad_depth_grid", "depth grid is ragged")
        parsed: list[int] = []
        for value in line:
            if not is_int(value):
                raise LakeError("bad_depth_grid", f"depth at row {r} is not an integer")
            if value < 0:
                raise LakeError("bad_depth_grid", f"depth at row {r} is negative")
            parsed.append(value)
        rows.append(tuple(parsed))
    return tuple(rows)


def depth_product_from_plan(publisher, plan_id: str, verify: bool = True) -> DepthProduct:
    """Read a signed plan and use its published per-cell depth as the source.

    ``storage`` in a signed artifact is the published water depth per cell.  We
    intentionally do not consult the elevation heights for naming.  Provenance
    is taken from the catalog version the plan froze.
    """

    artifact = publisher.get_plan(plan_id)
    if verify:
        check = publisher.verify(plan_id)
        if not check.ok:
            raise LakeError(
                "plan_not_verifiable",
                f"plan {plan_id} does not verify against the catalog",
                {"messages": list(check.messages)},
            )
    depths = _normalize_grid(artifact["storage"])
    surface = publisher.catalog.get_slice(artifact["catalog_version"])
    return DepthProduct(
        version=artifact["catalog_version"],
        elevation_hash=artifact["elevation_hash"],
        depths=depths,
        sources=dict(surface.sources),
    )


def depth_product_from_grid(
    depths: Sequence[Sequence[int]],
    version: int = 0,
    elevation_hash: str = "",
    sources: Mapping[Cell, tuple[SourceRef, ...]] | None = None,
) -> DepthProduct:
    """Build a depth product from an explicit published depth grid (testing aid)."""

    return DepthProduct(
        version=version,
        elevation_hash=elevation_hash,
        depths=_normalize_grid(depths),
        sources=dict(sources or {}),
    )


# ---------------------------------------------------------------------------
# Ladder of threshold components
# ---------------------------------------------------------------------------
def components_at(depths: Sequence[Sequence[int]], threshold: int) -> list[tuple[Cell, ...]]:
    """Four-connected components of cells with depth >= ``threshold``.

    The whole threshold is processed at once; component ordering and cell
    ordering are canonical, so a flat top yields exactly one component.
    """

    if not is_int(threshold) or threshold <= 0:
        raise LakeError("bad_threshold", "thresholds must be positive integers")
    grid = _normalize_grid(depths)
    height, width = len(grid), len(grid[0])
    active = {(r, c) for r in range(height) for c in range(width) if grid[r][c] >= threshold}
    seen: set[Cell] = set()
    comps: list[tuple[Cell, ...]] = []
    for start in sorted(active):
        if start in seen:
            continue
        seen.add(start)
        stack = [start]
        cells: list[Cell] = []
        while stack:
            r, c = stack.pop()
            cells.append((r, c))
            for dr, dc in _NEIGHBOURS:
                nr, nc = r + dr, c + dc
                if (nr, nc) in active and (nr, nc) not in seen:
                    seen.add((nr, nc))
                    stack.append((nr, nc))
        comps.append(tuple(sorted(cells)))
    comps.sort(key=lambda cells: cells[0])
    return comps


@dataclass(frozen=True)
class Block:
    block_id: str
    threshold: int
    cells: tuple[Cell, ...]

    @property
    def area(self) -> int:
        return len(self.cells)

    @property
    def cell_set(self) -> frozenset[Cell]:
        return frozenset(self.cells)


@dataclass(frozen=True)
class Ladder:
    thresholds: tuple[int, ...]
    blocks: Mapping[str, Block]
    by_threshold: Mapping[int, tuple[str, ...]]
    index: Mapping[int, Mapping[Cell, str]]
    parents: Mapping[str, str | None]
    children: Mapping[str, tuple[str, ...]]
    provenance: Mapping[Cell, tuple[SourceRef, ...]]
    depth_product: DepthProduct

    def block(self, block_id: str) -> Block:
        return self.blocks[block_id]

    def query(self, threshold: int, cell: Cell) -> str | None:
        """Which block at ``threshold`` contains ``cell`` (``None`` if dry)."""

        return self.index.get(threshold, {}).get((int(cell[0]), int(cell[1])))


def _thresholds(thresholds: Iterable[int]) -> tuple[int, ...]:
    values = sorted(set(thresholds))
    if not values:
        raise LakeError("bad_threshold", "at least one threshold is required")
    for value in values:
        if not is_int(value) or value <= 0:
            raise LakeError("bad_threshold", "thresholds must be positive integers")
    return tuple(values)


def build_ladder(product: DepthProduct, thresholds: Iterable[int]) -> Ladder:
    """Build the threshold-component ladder for one depth product."""

    levels = _thresholds(thresholds)
    blocks: dict[str, Block] = {}
    by_threshold: dict[int, tuple[str, ...]] = {}
    index: dict[int, dict[Cell, str]] = {}
    for level in levels:
        comps = components_at(product.depths, level)
        ids: list[str] = []
        cell_index: dict[Cell, str] = {}
        for k, cells in enumerate(comps):
            block_id = f"T{level}#{k}"
            blocks[block_id] = Block(block_id=block_id, threshold=level, cells=cells)
            ids.append(block_id)
            for cell in cells:
                cell_index[cell] = block_id
        by_threshold[level] = tuple(ids)
        index[level] = cell_index
    parents: dict[str, str | None] = {}
    children: dict[str, list[str]] = {block_id: [] for block_id in blocks}
    for position, level in enumerate(levels):
        for block_id in by_threshold[level]:
            if position == 0:
                parents[block_id] = None
                continue
            lower = levels[position - 1]
            block = blocks[block_id]
            parent = index[lower][block.cells[0]]
            parent_cells = blocks[parent].cell_set
            if not block.cell_set <= parent_cells:
                # The nested-threshold invariant guarantees this cannot happen.
                raise LakeError(
                    "broken_nesting",
                    f"block {block_id} is not contained in its shallower parent {parent}",
                    {"block": block_id, "parent": parent},
                )
            parents[block_id] = parent
            children[parent].append(block_id)
    return Ladder(
        thresholds=levels,
        blocks=blocks,
        by_threshold=by_threshold,
        index=index,
        parents=parents,
        children={block_id: tuple(v) for block_id, v in children.items()},
        provenance=dict(product.sources),
        depth_product=product,
    )


def _source_to_dict(ref: SourceRef) -> dict[str, Any]:
    return ref.to_dict()


def ladder_report(ladder: Ladder) -> dict[str, Any]:
    """Complete per-threshold delivery: cells, area, adjacent inclusion, provenance."""

    thresholds = []
    for level in ladder.thresholds:
        entries = []
        for block_id in ladder.by_threshold[level]:
            block = ladder.blocks[block_id]
            provenance = []
            for cell in block.cells:
                refs = ladder.provenance.get(cell, ())
                provenance.append(
                    {
                        "cell": [cell[0], cell[1]],
                        "sources": [_source_to_dict(ref) for ref in refs],
                    }
                )
            entries.append(
                {
                    "block_id": block_id,
                    "cells": [[r, c] for r, c in block.cells],
                    "area": block.area,
                    "parent": ladder.parents[block_id],
                    "children": list(ladder.children[block_id]),
                    "provenance": provenance,
                }
            )
        thresholds.append({"threshold": level, "blocks": entries})
    return {
        "depth_source": {
            "version": ladder.depth_product.version,
            "elevation_hash": ladder.depth_product.elevation_hash,
            "height": ladder.depth_product.height,
            "width": ladder.depth_product.width,
        },
        "thresholds": thresholds,
    }


# ---------------------------------------------------------------------------
# Old names and migration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OldName:
    """A name from the old edition.

    ``threshold`` and ``cells`` are the old edition's threshold and block; the
    block is validated to be a real component of the old ladder.  ``system``
    names the lake system; ``importance`` is the retention score.  The name may
    only appear at ``allowed_thresholds`` in the new edition (and, because of
    the same-threshold rule, that must include ``threshold``).
    """

    name: str
    importance: int
    system: str
    threshold: int
    cells: tuple[Cell, ...]
    allowed_thresholds: tuple[int, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OldName":
        cells_raw = raw.get("cells")
        if not isinstance(cells_raw, (list, tuple)) or not cells_raw:
            raise LakeError("bad_old_name", "old name needs a non-empty cells list")
        cells: list[Cell] = []
        for item in cells_raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise LakeError("bad_old_name", "old name cells must be [row, col] pairs")
            r, c = item
            if not is_int(r) or not is_int(c):
                raise LakeError("bad_old_name", "old name cells must be integer pairs")
            cells.append((r, c))
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise LakeError("bad_old_name", "old name must be a non-empty string")
        importance = raw.get("importance")
        if not is_int(importance) or importance < 0:
            raise LakeError("bad_old_name", f"old name {name} needs a non-negative importance")
        system = raw.get("system")
        if not isinstance(system, str) or not system:
            raise LakeError("bad_old_name", f"old name {name} needs a non-empty system")
        threshold = raw.get("threshold")
        if not is_int(threshold):
            raise LakeError("bad_old_name", f"old name {name} needs an integer threshold")
        allowed = raw.get("allowed_thresholds", [threshold])
        if not isinstance(allowed, (list, tuple)) or not allowed:
            raise LakeError("bad_old_name", f"old name {name} needs allowed_thresholds")
        allowed_t = []
        for value in allowed:
            if not is_int(value):
                raise LakeError("bad_old_name", "allowed_thresholds must be integers")
            allowed_t.append(value)
        return cls(
            name=name,
            importance=importance,
            system=system,
            threshold=threshold,
            cells=tuple(sorted(cells)),
            allowed_thresholds=tuple(sorted(set(allowed_t))),
        )


@dataclass(frozen=True)
class Eligible:
    block_id: str
    threshold: int
    overlap: int
    new_cells: tuple[Cell, ...]


@dataclass(frozen=True)
class PairRelation:
    shallow_name: str
    deep_name: str
    old_nested: bool
    new_nested: bool
    cross_system: bool

    @property
    def consistent(self) -> bool:
        return self.old_nested == self.new_nested

    @property
    def cross_system_ok(self) -> bool:
        return not (self.cross_system and self.new_nested)

    @property
    def ok(self) -> bool:
        return self.consistent and self.cross_system_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "shallow_name": self.shallow_name,
            "deep_name": self.deep_name,
            "old_nested": self.old_nested,
            "new_nested": self.new_nested,
            "cross_system": self.cross_system,
            "hierarchy_preserved": self.consistent,
            "cross_system_ok": self.cross_system_ok,
            "ok": self.ok,
        }


def _validate_old_name(name: OldName, old_ladder: Ladder) -> None:
    ids = old_ladder.by_threshold.get(name.threshold)
    if ids is None:
        raise LakeError(
            "old_threshold_missing",
            f"old name {name.name} uses threshold {name.threshold} absent from the old ladder",
        )
    target = frozenset(name.cells)
    for block_id in ids:
        if old_ladder.blocks[block_id].cell_set == target:
            return
    raise LakeError(
        "old_block_not_a_component",
        f"old name {name.name} block is not a threshold component of the old ladder",
        {"name": name.name, "threshold": name.threshold, "cells": [list(c) for c in name.cells]},
    )


def eligible_blocks(name: OldName, new_ladder: Ladder) -> list[Eligible]:
    """New blocks a name may take: same threshold, allowed, non-empty overlap."""

    if name.threshold not in name.allowed_thresholds:
        return []
    old_cells = set(name.cells)
    result: list[Eligible] = []
    for block_id in new_ladder.by_threshold.get(name.threshold, ()):  # same threshold only
        block = new_ladder.blocks[block_id]
        overlap = len(old_cells & block.cell_set)
        if overlap > 0:
            result.append(
                Eligible(block_id=block_id, threshold=block.threshold, overlap=overlap,
                         new_cells=block.cells)
            )
    result.sort(key=lambda e: (-e.overlap, e.block_id))
    return result


def _relation_specs(
    name_a: str, system_a: str, thr_a: int, old_a: frozenset[Cell], new_a: frozenset[Cell],
    name_b: str, system_b: str, thr_b: int, old_b: frozenset[Cell], new_b: frozenset[Cell],
) -> PairRelation | None:
    if thr_a == thr_b:
        return None  # same depth: neither is a parent of the other
    if thr_a < thr_b:
        shallow, deep = (name_a, old_a, new_a), (name_b, old_b, new_b)
    else:
        shallow, deep = (name_b, old_b, new_b), (name_a, old_a, new_a)
    old_nested = deep[1] <= shallow[1]
    new_nested = deep[2] <= shallow[2]
    return PairRelation(
        shallow_name=shallow[0],
        deep_name=deep[0],
        old_nested=old_nested,
        new_nested=new_nested,
        cross_system=(system_a != system_b),
    )


@dataclass(frozen=True)
class NameTag:
    name: str
    importance: int
    system: str
    old_threshold: int
    old_cells: tuple[Cell, ...]
    new_block: str
    new_threshold: int
    new_cells: tuple[Cell, ...]
    overlap: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "importance": self.importance,
            "system": self.system,
            "old_threshold": self.old_threshold,
            "old_block_cells": [[r, c] for r, c in self.old_cells],
            "new_block": self.new_block,
            "new_threshold": self.new_threshold,
            "new_block_cells": [[r, c] for r, c in self.new_cells],
            "overlap": self.overlap,
        }


@dataclass(frozen=True)
class LostName:
    name: str
    importance: int
    reason: str
    eligible_blocks: tuple[Eligible, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "importance": self.importance,
            "reason": self.reason,
            "eligible_blocks": [e.block_id for e in self.eligible_blocks],
        }


@dataclass(frozen=True)
class MigrationResult:
    tags: tuple[NameTag, ...]
    lost: tuple[LostName, ...]
    total_importance: int
    total_overlap: int
    max_names: int | None
    relations: tuple[PairRelation, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "new_name_tags": [tag.to_dict() for tag in self.tags],
            "lost_names": [lost.to_dict() for lost in self.lost],
            "total_importance": self.total_importance,
            "total_overlap": self.total_overlap,
            "max_names": self.max_names,
            "kinship": [relation.to_dict() for relation in self.relations],
        }


def migrate(
    old_ladder: Ladder,
    new_ladder: Ladder,
    names: Sequence[OldName],
    max_names: int | None = None,
) -> MigrationResult:
    """Maximise retained importance, then total overlap, under every constraint.

    Constraints enforced on a retained set:

    * each name maps to a new block at the *same threshold* with at least one
      overlapping cell, and that threshold is permitted for the name;
    * each new block takes at most one name, each name at most one block;
    * for a retained shallow/deep pair, "old deep block under old shallow block"
      holds iff "new deep block under new shallow block" holds;
    * names from different systems are never placed as mother/branch (no new
      containment between them).

    Ties (equal importance and equal overlap) may resolve to any optimum; the
    search is deterministic so a repeated call returns the same choice.
    """

    if max_names is not None and (not is_int(max_names) or max_names < 0):
        raise LakeError("bad_max_names", "max_names must be a non-negative integer")
    seen: set[str] = set()
    for name in names:
        if name.name in seen:
            raise LakeError("duplicate_name", f"old name {name.name} appears twice")
        seen.add(name.name)
        _validate_old_name(name, old_ladder)

    eligible: dict[str, list[Eligible]] = {}
    for name in names:
        eligible[name.name] = eligible_blocks(name, new_ladder)

    # Deterministic search order: higher importance first, then name.
    ordered = sorted(names, key=lambda n: (-n.importance, n.name))
    by_name = {n.name: n for n in names}
    cap = max_names if max_names is not None else len(names)
    # At most MAX_ELIGIBLE_BLOCKS_PER_EDITION blocks may bear a name in an edition.
    cap = min(cap, MAX_ELIGIBLE_BLOCKS_PER_EDITION)

    # Per-name maximum overlap, for an admissible overlap upper bound.
    max_overlap = {n.name: (max((e.overlap for e in eligible[n.name]), default=0)) for n in names}

    best: tuple[int, int, tuple[tuple[str, str], ...], dict[str, Eligible]] | None = None

    def compatible(chosen: dict[str, Eligible], name: OldName, cand: Eligible) -> bool:
        cand_old = frozenset(name.cells)
        cand_new = frozenset(cand.new_cells)
        for other_name, other in chosen.items():
            other_spec = by_name[other_name]
            rel = _relation_specs(
                other_spec.name, other_spec.system, other_spec.threshold,
                frozenset(other_spec.cells), frozenset(other.new_cells),
                name.name, name.system, name.threshold, cand_old, cand_new,
            )
            if rel is None:
                continue
            if not rel.ok:
                return False
        return True

    def signature(chosen: dict[str, Eligible]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((n, e.block_id) for n, e in chosen.items()))

    def dfs(i: int, chosen: dict[str, Eligible], used: set[str], imp: int, ov: int,
            remaining_imp: list[int], remaining_ov: list[int]) -> None:
        nonlocal best
        if i == len(ordered):
            sig = signature(chosen)
            if best is None or imp > best[0] or (imp == best[0] and (ov > best[1] or (ov == best[1] and sig < best[2]))):
                best = (imp, ov, sig, dict(chosen))
            return
        name = ordered[i]
        # Admissible pruning: cannot beat the incumbent even taking everything.
        if best is not None:
            if imp + remaining_imp[i] < best[0]:
                return
            if imp + remaining_imp[i] == best[0] and ov + remaining_ov[i] < best[1]:
                return
        # Option 1: skip this name.
        dfs(i + 1, chosen, used, imp, ov, remaining_imp, remaining_ov)
        # Option 2: assign to an eligible, free block if under the cap.
        if len(chosen) < cap:
            for cand in eligible[name.name]:
                if cand.block_id in used:
                    continue
                if not compatible(chosen, name, cand):
                    continue
                chosen[name.name] = cand
                used.add(cand.block_id)
                dfs(i + 1, chosen, used, imp + name.importance, ov + cand.overlap,
                    remaining_imp, remaining_ov)
                used.remove(cand.block_id)
                del chosen[name.name]

    rem_imp = [0] * (len(ordered) + 1)
    rem_ov = [0] * (len(ordered) + 1)
    for i in range(len(ordered) - 1, -1, -1):
        rem_imp[i] = rem_imp[i + 1] + ordered[i].importance
        rem_ov[i] = rem_ov[i + 1] + max_overlap[ordered[i].name]
    dfs(0, {}, set(), 0, 0, rem_imp, rem_ov)

    if best is None:  # only possible with max_names=0 and empty names
        best = (0, 0, (), {})

    chosen = best[3]
    tags: list[NameTag] = []
    for name in names:
        cand = chosen.get(name.name)
        if cand is None:
            continue
        tags.append(
            NameTag(
                name=name.name,
                importance=name.importance,
                system=name.system,
                old_threshold=name.threshold,
                old_cells=name.cells,
                new_block=cand.block_id,
                new_threshold=cand.threshold,
                new_cells=cand.new_cells,
                overlap=cand.overlap,
            )
        )
    tags.sort(key=lambda t: (t.old_threshold, t.name))

    lost: list[LostName] = []
    for name in sorted(names, key=lambda n: n.name):
        if name.name in chosen:
            continue
        if not eligible[name.name]:
            if name.threshold not in name.allowed_thresholds:
                reason = "threshold_not_allowed"
            else:
                reason = "no_overlapping_block_same_threshold"
        elif len(chosen) >= cap:
            reason = "name_cap"
        else:
            reason = "conflict_or_not_optimal"
        lost.append(
            LostName(
                name=name.name,
                importance=name.importance,
                reason=reason,
                eligible_blocks=tuple(eligible[name.name]),
            )
        )

    relations = _retained_relations(tags)
    return MigrationResult(
        tags=tuple(tags),
        lost=tuple(lost),
        total_importance=best[0],
        total_overlap=best[1],
        max_names=cap,
        relations=tuple(relations),
    )


def _retained_relations(tags: Sequence[NameTag]) -> list[PairRelation]:
    relations: list[PairRelation] = []
    ordered = sorted(tags, key=lambda t: (t.old_threshold, t.name))
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            a, b = ordered[i], ordered[j]
            rel = _relation_specs(
                a.name, a.system, a.old_threshold, frozenset(a.old_cells), frozenset(a.new_cells),
                b.name, b.system, b.old_threshold, frozenset(b.old_cells), frozenset(b.new_cells),
            )
            if rel is not None:
                relations.append(rel)
    return relations


def kinship_report(result: MigrationResult, new_ladder: Ladder) -> list[dict[str, Any]]:
    """Verifiable parent/branch relations among retained names.

    Each entry is derived purely from the frozen new depth product: the deeper
    block's cells are checked for containment in the shallower block's cells,
    and the pair is annotated with whether the old edition agreed and whether
    the two names share a system.  ``verified`` is true when the new-deck
    containment check passes on the issued atlas.
    """

    by_block = {tag.new_block: tag for tag in result.tags}
    out: list[dict[str, Any]] = []
    for rel in result.relations:
        shallow_tag = next(t for t in result.tags if t.name == rel.shallow_name)
        deep_tag = next(t for t in result.tags if t.name == rel.deep_name)
        new_shallow = frozenset(new_ladder.blocks[shallow_tag.new_block].cell_set)
        new_deep = frozenset(new_ladder.blocks[deep_tag.new_block].cell_set)
        verified = new_deep <= new_shallow
        out.append(
            {
                **rel.to_dict(),
                "shallow_new_block": shallow_tag.new_block,
                "deep_new_block": deep_tag.new_block,
                "shallow_threshold": shallow_tag.new_threshold,
                "deep_threshold": deep_tag.new_threshold,
                "verified_from_depth_product": verified,
                "depth_source_version": new_ladder.depth_product.version,
                "depth_source_hash": new_ladder.depth_product.elevation_hash,
            }
        )
    return out
