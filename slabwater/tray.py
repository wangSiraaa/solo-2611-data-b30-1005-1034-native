"""Display-case shallow trays calibrated from a *frozen* slabwater water plan.

A tray is a non-overlapping subset of the cells of one catalog surface.  Each
cell carries a unit area (the problem fixes area = 1), a *signed* lever arm to
the pivot (negative arms are allowed), a ground height ``h`` and a water-level
ceiling ``l``.  Both ``h`` and ``l`` are frozen per cell: ``h`` comes from a
catalog slice, ``l`` from a plan artifact published against that exact slice.
Re-publishing a plan under a new request id / on a new slice never mutates an
existing tray -- a tray keeps the plan id, catalog version and elevation hash
it was calibrated from.

Given one common mark line ``z`` shared by every cell of a tray, the actual
water in a cell is::

    water_i(z) = max(0, min(z, l_i) - h_i)

and its contribution to the pivot moment is ``water_i(z) * arm_i``.  The tray
total is a continuous, piecewise-linear function of ``z`` with kinks only at
cell ground/ceiling heights (a tray has at most eight distinct kinks).  Because
arms are signed, total moment is NOT monotone in water volume: when the mark
line crosses a high ground a new cell starts drinking and the moment may jump
in slope or even flip sign -- the piecewise bookkeeping here is what makes the
moment exact instead of "split the volume evenly".

All quantities are exact :class:`fractions.Fraction` values (the catalog and
publisher store integer heights; tray geometry and balance weights may be
rational).  The standard library alone is sufficient.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping, Sequence

from .model import Cell, Slice, SourceRef

__all__ = ["PanCell", "ZSegment", "WSegment", "LookupResult", "Pan"]


def _F(value: int | Fraction) -> Fraction:
    return value if isinstance(value, Fraction) else Fraction(value)


@dataclass(frozen=True)
class PanCell:
    """One calibrated cell.  ``h`` and ``l`` are frozen from real artifacts."""

    cell: Cell
    area: Fraction
    arm: Fraction
    ground: Fraction          # h, from the catalog slice
    level_ceiling: Fraction   # l, from the published plan's per-cell level
    sources: tuple[SourceRef, ...]
    catalog_version: int
    elevation_hash: str
    plan_id: str

    def water_at(self, z: Fraction) -> Fraction:
        z = _F(z)
        clamped = min(max(z, Fraction(0)), self.level_ceiling)
        return max(Fraction(0), clamped - self.ground)

    def moment_at(self, z: Fraction) -> Fraction:
        return self.water_at(z) * self.arm


@dataclass(frozen=True)
class ZSegment:
    """One linear piece of the tray response over a mark-line interval."""

    z_lo: Fraction
    z_hi: Fraction
    w_lo: Fraction
    w_hi: Fraction
    t_lo: Fraction
    t_hi: Fraction
    active: tuple[Cell, ...]      # cells strictly between h and l inside the piece
    area_sum: Fraction
    arm_sum: Fraction             # sum(area_i * arm_i) over active cells

    @property
    def is_plateau(self) -> bool:
        return self.area_sum == 0

    @property
    def slope_t_over_w(self) -> Fraction | None:
        """dT/dW on the piece; None when the piece is a plateau (dW = 0)."""

        if self.area_sum == 0:
            return None
        return self.arm_sum / self.area_sum


@dataclass(frozen=True)
class WSegment:
    """The same piece parameterised by delivered water volume W."""

    w_lo: Fraction
    w_hi: Fraction
    z_lo: Fraction
    z_hi: Fraction
    t_lo: Fraction
    t_hi: Fraction
    area_sum: Fraction
    arm_sum: Fraction

    @property
    def is_fixed(self) -> bool:
        return self.w_lo == self.w_hi

    def moment(self, w: Fraction) -> Fraction:
        w = _F(w)
        if self.w_lo == self.w_hi:
            return self.t_lo
        ratio = (w - self.w_lo) / (self.w_hi - self.w_lo)
        return self.t_lo + (self.t_hi - self.t_lo) * ratio


@dataclass(frozen=True)
class LookupResult:
    """Reverse lookup of a legal water volume.

    A plateau (the mark line crossing only dry or already-full cells) means the
    same volume is produced by a whole interval of legal mark lines; the
    interval is reported in full via ``z_lo`` / ``z_hi``.
    """

    water: Fraction
    z_lo: Fraction
    z_hi: Fraction
    moment_lo: Fraction
    moment_hi: Fraction

    @property
    def unique_z(self) -> bool:
        return self.z_lo == self.z_hi


class Pan:
    """A calibrated shallow tray bound to one plan and one catalog slice."""

    MAX_KINKS = 8

    def __init__(
        self,
        name: str,
        cells: Sequence[PanCell],
        plan_id: str,
        catalog_version: int,
        elevation_hash: str,
    ) -> None:
        if not cells:
            raise ValueError("tray needs at least one cell")
        coords = [c.cell for c in cells]
        if len(set(coords)) != len(coords):
            raise ValueError("cells of a tray must not overlap")
        for c in cells:
            if c.area <= 0:
                raise ValueError("cell area must be positive")
            if c.ground < 0 or c.level_ceiling < c.ground:
                raise ValueError(f"cell {c.cell} has illegal h/l freeze")
            if c.catalog_version != catalog_version or c.elevation_hash != elevation_hash:
                raise ValueError("every cell must be frozen from the same catalog slice")
        self.name = name
        self.cells: tuple[PanCell, ...] = tuple(cells)
        self.plan_id = plan_id
        self.catalog_version = catalog_version
        self.elevation_hash = elevation_hash
        # The question only asks about mark lines between the lowest legal
        # ground and the highest legal water level of the tray.
        self.z_min = min(c.ground for c in self.cells)
        self.z_max = max(c.level_ceiling for c in self.cells)
        kinks = {c.ground for c in self.cells} | {c.level_ceiling for c in self.cells}
        self.kinks: tuple[Fraction, ...] = tuple(sorted(kinks))
        if len(self.kinks) - 1 > self.MAX_KINKS:
            raise ValueError(
                f"tray {name} has {len(self.kinks) - 1} kinks, at most {self.MAX_KINKS} allowed"
            )
        self._z_segments = self._build_z_segments()
        self._w_segments = self._build_w_segments()
        self._capacity = self._z_segments[-1].w_hi

    # -- construction from real artifacts ---------------------------------
    @classmethod
    def from_plan(
        cls,
        name: str,
        surface: Slice,
        plan_artifact: Mapping,
        cells: Sequence[tuple[Cell, Fraction | int]],
    ) -> "Pan":
        """Freeze h from ``surface`` and l from the plan's per-cell levels.

        ``cells`` lists ``(cell, signed_arm)`` pairs; area is fixed at 1.
        """

        specs = [(cell, arm, plan_artifact) for cell, arm in cells]
        return cls.from_cell_plans(name, surface, specs)

    @classmethod
    def from_cell_plans(
        cls,
        name: str,
        surface: Slice,
        specs: Sequence[tuple[Cell, Fraction | int, Mapping]],
    ) -> "Pan":
        """Freeze each cell's l from its own published plan artifact.

        Every artifact must be published against the supplied frozen slice;
        different cells of one tray may therefore cite different request ids
        while all share the same ground surface.
        """

        pan_cells: list[PanCell] = []
        seen: set[Cell] = set()
        plan_ids: set[str] = set()
        for cell, arm, artifact in specs:
            r, c = cell
            if not (0 <= r < surface.height and 0 <= c < surface.width):
                raise ValueError(f"cell {cell} outside the frozen surface")
            if cell in seen:
                raise ValueError(f"cell {cell} listed twice")
            seen.add(cell)
            if artifact["catalog_version"] != surface.version:
                raise ValueError("plan was not published against the supplied slice")
            if artifact["elevation_hash"] != surface.elevation_hash:
                raise ValueError("plan elevation hash does not match the frozen slice")
            h = Fraction(surface.at(r, c))
            l = Fraction(artifact["levels"][r][c])
            if l < h:
                raise ValueError(f"cell {cell}: published level below ground")
            plan_ids.add(artifact["plan_id"])
            pan_cells.append(
                PanCell(
                    cell=cell,
                    area=Fraction(1),
                    arm=_F(arm),
                    ground=h,
                    level_ceiling=l,
                    sources=tuple(surface.sources.get(cell, ())),
                    catalog_version=surface.version,
                    elevation_hash=surface.elevation_hash,
                    plan_id=artifact["plan_id"],
                )
            )
        plan_id = next(iter(plan_ids)) if len(plan_ids) == 1 else "+".join(sorted(plan_ids))
        return cls(
            name,
            pan_cells,
            plan_id=plan_id,
            catalog_version=surface.version,
            elevation_hash=surface.elevation_hash,
        )

    # -- basic response ----------------------------------------------------
    @property
    def capacity(self) -> Fraction:
        return self._capacity

    def water_at(self, z: Fraction) -> Fraction:
        z = _F(z)
        if not (self.z_min <= z <= self.z_max):
            raise ValueError(
                f"mark line {z} outside legal range [{self.z_min}, {self.z_max}]"
            )
        return sum((c.water_at(z) for c in self.cells), Fraction(0))

    def moment_at(self, z: Fraction) -> Fraction:
        z = _F(z)
        if not (self.z_min <= z <= self.z_max):
            raise ValueError(
                f"mark line {z} outside legal range [{self.z_min}, {self.z_max}]"
            )
        return sum((c.moment_at(z) for c in self.cells), Fraction(0))

    def cell_breakdown(self, z: Fraction) -> list[dict]:
        """Per-cell water and moment at ``z`` (used for the two-side check)."""

        z = _F(z)
        rows = []
        for c in self.cells:
            w = c.water_at(z)
            rows.append(
                {
                    "cell": c.cell,
                    "arm": c.arm,
                    "ground": c.ground,
                    "ceiling": c.level_ceiling,
                    "water": w,
                    "moment": w * c.arm,
                    "sources": [s.to_dict() for s in c.sources],
                }
            )
        return rows

    # -- piecewise structure ----------------------------------------------
    @property
    def z_segments(self) -> tuple[ZSegment, ...]:
        return self._z_segments

    @property
    def w_segments(self) -> tuple[WSegment, ...]:
        return self._w_segments

    def _active(self, z: Fraction) -> tuple[PanCell, ...]:
        return tuple(c for c in self.cells if c.ground < z < c.level_ceiling)

    def _build_z_segments(self) -> tuple[ZSegment, ...]:
        segments: list[ZSegment] = []
        w = t = Fraction(0)
        points = self.kinks
        for lo, hi in zip(points[:-1], points[1:]):
            active = self._active((lo + hi) / 2)
            area_sum = sum((c.area for c in active), Fraction(0))
            arm_sum = sum((c.area * c.arm for c in active), Fraction(0))
            w_hi = w + area_sum * (hi - lo)
            t_hi = t + arm_sum * (hi - lo)
            segments.append(
                ZSegment(
                    z_lo=lo,
                    z_hi=hi,
                    w_lo=w,
                    w_hi=w_hi,
                    t_lo=t,
                    t_hi=t_hi,
                    active=tuple(c.cell for c in active),
                    area_sum=area_sum,
                    arm_sum=arm_sum,
                )
            )
            w, t = w_hi, t_hi
        return tuple(segments)

    def _build_w_segments(self) -> tuple[WSegment, ...]:
        out: list[WSegment] = []
        for seg in self._z_segments:
            out.append(
                WSegment(
                    w_lo=seg.w_lo,
                    w_hi=seg.w_hi,
                    z_lo=seg.z_lo,
                    z_hi=seg.z_hi,
                    t_lo=seg.t_lo,
                    t_hi=seg.t_hi,
                    area_sum=seg.area_sum,
                    arm_sum=seg.arm_sum,
                )
            )
        return tuple(out)

    # -- reverse lookup ----------------------------------------------------
    def lookup_water(self, water: Fraction) -> LookupResult:
        """Return every legal mark line that yields exactly ``water``."""

        water = _F(water)
        if water < 0 or water > self._capacity:
            raise ValueError(
                f"water {water} outside tray capacity [0, {self._capacity}]"
            )
        matches = [
            seg
            for seg in self._w_segments
            if seg.w_lo <= water <= seg.w_hi
        ]
        if not matches:  # pragma: no cover - segments cover [0, capacity]
            raise RuntimeError("piecewise coverage broken")
        first, last = matches[0], matches[-1]

        def z_rising(seg: WSegment, w: Fraction) -> Fraction:
            """Lowest legal mark line producing ``w`` inside one piece."""

            if seg.w_lo == seg.w_hi:
                return seg.z_lo
            ratio = (w - seg.w_lo) / (seg.w_hi - seg.w_lo)
            return seg.z_lo + (seg.z_hi - seg.z_lo) * ratio

        z_lo = z_rising(first, water)
        if last.is_fixed and water == last.w_hi:
            z_hi = last.z_hi
        else:
            z_hi = z_rising(last, water)
        return LookupResult(
            water=water,
            z_lo=z_lo,
            z_hi=z_hi,
            moment_lo=self.moment_at(z_lo),
            moment_hi=self.moment_at(z_hi),
        )

    # -- provenance --------------------------------------------------------
    def provenance(self) -> dict:
        return {
            "name": self.name,
            "plan_id": self.plan_id,
            "catalog_version": self.catalog_version,
            "elevation_hash": self.elevation_hash,
            "z_range": [str(self.z_min), str(self.z_max)],
            "capacity": str(self.capacity),
            "kinks": [str(k) for k in self.kinks],
            "cells": [
                {
                    "cell": list(c.cell),
                    "area": str(c.area),
                    "arm": str(c.arm),
                    "h": str(c.ground),
                    "l": str(c.level_ceiling),
                    "sources": [s.to_dict() for s in c.sources],
                }
                for c in self.cells
            ],
        }

    def response_table(self) -> list[dict]:
        """Exact water-moment relation, one row per linear piece."""

        return [
            {
                "z": [str(seg.z_lo), str(seg.z_hi)],
                "water": [str(seg.w_lo), str(seg.w_hi)],
                "moment": [str(seg.t_lo), str(seg.t_hi)],
                "active_cells": [list(cell) for cell in seg.active],
                "plateau": seg.is_plateau,
            }
            for seg in self._z_segments
        ]
