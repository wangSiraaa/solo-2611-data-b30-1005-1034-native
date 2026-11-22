"""Exact balance planning for up to three calibrated display trays.

Inputs per planning run:

* the calibrated :class:`~slabwater.tray.Pan` objects (each frozen to one
  published plan and catalog slice),
* a starting water volume per tray,
* at most three optional balance weights, each with a signed pivot moment and a
  hanging fee,
* the target total moment.

Water may only be *transferred* between trays: the total volume is fixed, no
tray may exceed its calibrated capacity, and every unit actually moved from one
tray to another costs exactly 1 (the same water is never billed twice, so the
transfer cost is ``sum(max(0, start_i - final_i))``).  Hanging a weight adds
its fee.  The answer minimizes transfer cost + hanging fees exactly, using
:class:`fractions.Fraction` throughout.

Why the search looks the way it does
------------------------------------

On one linear piece of a tray's calibration, both its water volume interval and
its moment are affine in the same local parameter, so a fixed choice of
"piece per tray + weight subset" turns every requirement into linear
constraints:

* box bounds ``w_lo <= w_i <= w_hi`` (capacity is included),
* volume conservation ``sum w_i = total``,
* moment target ``sum T_i(w_i) + weight moments = target``.

Minimizing a piecewise-linear convex transfer cost over a bounded polytope
attains its optimum at a polytope vertex.  We therefore enumerate, for every
combination, all intersections of ``n`` independent active rows of the
constraint matrix (``n`` = number of trays, at most 3) and solve the square
systems with exact Gauss-Jordan elimination.  No single linear interpolation is
assumed to suffice -- a tray whose mark line crosses a high ground contributes
an extra kink, and every kink gets its own piece here.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations, product
from typing import Sequence

from .tray import Pan, WSegment

__all__ = ["Weight", "LoadingPlan", "InfeasibleBalance", "plan_balance"]


@dataclass(frozen=True)
class Weight:
    label: str
    moment: Fraction
    fee: Fraction

    def __post_init__(self) -> None:
        if self.fee < 0:
            raise ValueError("hanging fee must be non-negative")


@dataclass(frozen=True)
class TrayFinal:
    name: str
    start: Fraction
    water: Fraction
    z_lo: Fraction
    z_hi: Fraction
    moment: Fraction
    segment_index: int


@dataclass(frozen=True)
class LoadingPlan:
    trays: tuple[TrayFinal, ...]
    hung: tuple[Weight, ...]
    transfers: tuple[tuple[str, str, Fraction], ...]
    transfer_cost: Fraction
    weight_fee: Fraction
    total_cost: Fraction
    target_moment: Fraction
    positive_moment: Fraction
    negative_moment: Fraction
    net_moment: Fraction

    def as_dict(self) -> dict:
        return {
            "trays": [
                {
                    "name": t.name,
                    "start": str(t.start),
                    "water": str(t.water),
                    "z_range": [str(t.z_lo), str(t.z_hi)],
                    "moment": str(t.moment),
                    "segment": t.segment_index,
                }
                for t in self.trays
            ],
            "hung": [{"label": w.label, "moment": str(w.moment), "fee": str(w.fee)} for w in self.hung],
            "transfers": [
                {"from": a, "to": b, "amount": str(x)} for a, b, x in self.transfers
            ],
            "transfer_cost": str(self.transfer_cost),
            "weight_fee": str(self.weight_fee),
            "total_cost": str(self.total_cost),
            "target_moment": str(self.target_moment),
            "two_side_check": {
                "positive": str(self.positive_moment),
                "negative": str(self.negative_moment),
                "net": str(self.net_moment),
            },
        }


class InfeasibleBalance(Exception):
    pass


# -- exact linear algebra ----------------------------------------------------

def _gauss_jordan(matrix: list[list[Fraction]], rhs: list[Fraction]) -> list[Fraction] | None:
    """Solve a square rational system; return None if singular."""

    n = len(matrix)
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    row = 0
    for col in range(n):
        pivot = next((r for r in range(row, n) if a[r][col] != 0), None)
        if pivot is None:
            return None
        a[row], a[pivot] = a[pivot], a[row]
        div = a[row][col]
        a[row] = [v / div for v in a[row]]
        for r in range(n):
            if r != row and a[r][col] != 0:
                factor = a[r][col]
                a[r] = [v - factor * a[row][j] for j, v in enumerate(a[r])]
        row += 1
    return [a[i][n] for i in range(n)]


def _rank_and_independent(
    rows: list[list[Fraction]], n: int
) -> tuple[int, list[int]]:
    """Rank of ``rows`` (length n each) and indices of an independent basis."""

    basis: list[list[Fraction]] = []
    chosen: list[int] = []
    for idx, row in enumerate(rows):
        trial = basis + [row]
        m = [r[:] for r in trial]
        rank = 0
        for col in range(n):
            pivot = next((r for r in range(rank, len(m)) if m[r][col] != 0), None)
            if pivot is None:
                continue
            m[rank], m[pivot] = m[pivot], m[rank]
            div = m[rank][col]
            m[rank] = [v / div for v in m[rank]]
            for r in range(len(m)):
                if r != rank and m[r][col] != 0:
                    factor = m[r][col]
                    m[r] = [v - factor * m[rank][j] for j, v in enumerate(m[r])]
            rank += 1
        if rank > len(basis):
            basis.append(row)
            chosen.append(idx)
    return len(basis), chosen


def _polytope_vertices(
    n: int,
    bounds: list[tuple[Fraction, Fraction]],
    equality_rows: list[list[Fraction]],
    equality_rhs: list[Fraction],
) -> list[tuple[Fraction, ...]]:
    """All vertices of the bounded polytope ``bounds`` + equalities.

    A vertex is any feasible point at which n independent constraint rows are
    active; bounds contribute two candidate rows (w = lo, w = hi) each.
    """

    eq_rank, eq_idx = _rank_and_independent(equality_rows, n)
    eq_rows = [equality_rows[i] for i in eq_idx]
    eq_rhs = [equality_rhs[i] for i in eq_idx]
    bound_rows: list[list[Fraction]] = []
    bound_rhs: list[Fraction] = []
    bound_tag: list[tuple[int, str]] = []
    for i, (lo, hi) in enumerate(bounds):
        row = [Fraction(0)] * n
        row[i] = Fraction(1)
        bound_rows.append(row)
        bound_rhs.append(lo)
        bound_tag.append((i, "lo"))
        bound_rows.append(row[:])
        bound_rhs.append(hi)
        bound_tag.append((i, "hi"))

    vertices: list[tuple[Fraction, ...]] = []
    seen: set[tuple[Fraction, ...]] = set()
    need = n - eq_rank
    if need < 0:
        return vertices
    for chosen in combinations(range(len(bound_rows)), need):
        mat = eq_rows + [bound_rows[k] for k in chosen]
        rhs = eq_rhs + [bound_rhs[k] for k in chosen]
        sol = _gauss_jordan(mat, rhs)
        if sol is None:
            continue
        point = tuple(sol)
        if point in seen:
            continue
        if all(bounds[i][0] <= point[i] <= bounds[i][1] for i in range(n)):
            for row, value in zip(equality_rows, equality_rhs):
                if sum(a * b for a, b in zip(row, point)) != value:
                    break
            else:
                seen.add(point)
                vertices.append(point)
    return vertices


# -- planning ----------------------------------------------------------------

def _transfer_list(
    trays: Sequence[Pan], starts: Sequence[Fraction], finals: Sequence[Fraction]
) -> tuple[tuple[tuple[str, str, Fraction], ...], Fraction]:
    """Greedy, exact settlement of who sends water to whom (bills each unit once)."""

    donors = [
        (trays[i].name, starts[i] - finals[i])
        for i in range(len(trays))
        if starts[i] - finals[i] > 0
    ]
    receivers = [
        [trays[i].name, finals[i] - starts[i]]
        for i in range(len(trays))
        if finals[i] - starts[i] > 0
    ]
    moves: list[tuple[str, str, Fraction]] = []
    cost = Fraction(0)
    for name, surplus in donors:
        remaining = surplus
        for rec in receivers:
            if rec[1] <= 0:
                continue
            take = min(remaining, rec[1])
            if take > 0:
                moves.append((name, rec[0], take))
                rec[1] -= take
                remaining -= take
                cost += take
        if remaining != 0:  # pragma: no cover - conservation guarantees this
            raise RuntimeError("water did not balance")
    return tuple(moves), cost


def plan_balance(
    trays: Sequence[Pan],
    starts: Sequence[Fraction | int],
    weights: Sequence[Weight],
    target_moment: Fraction | int,
) -> LoadingPlan:
    n = len(trays)
    if not 1 <= n <= 3:
        raise ValueError("between one and three trays are supported")
    if len(weights) > 3:
        raise ValueError("at most three balance weights")
    starts_f = tuple(Fraction(s) for s in starts)
    target = Fraction(target_moment)
    for pan, start in zip(trays, starts_f):
        if not (0 <= start <= pan.capacity):
            raise ValueError(f"start volume of {pan.name} exceeds its capacity")
    if sum(starts_f, Fraction(0)) > sum(p.capacity for p in trays):
        raise ValueError("total starting water exceeds total capacity")

    best: tuple | None = None  # (total_cost, deterministic tie tuple, payload)

    for mask in range(1 << len(weights)):
        hung = tuple(weights[i] for i in range(len(weights)) if mask & (1 << i))
        weight_moment = sum((w.moment for w in hung), Fraction(0))
        weight_fee = sum((w.fee for w in hung), Fraction(0))
        moment_target = target - weight_moment

        segment_lists = [list(enumerate(pan.w_segments)) for pan in trays]
        for picked in product(*segment_lists):
            seg_index = [idx for idx, _ in picked]
            segs: list[WSegment] = [seg for _, seg in picked]
            bounds = [(seg.w_lo, seg.w_hi) for seg in segs]

            # Conservation row and the single affine moment equality.  Pieces
            # with a fixed volume contribute a constant instead of a slope.
            ones = [Fraction(1)] * n
            total_water = sum(starts_f, Fraction(0))
            m_row = [Fraction(0)] * n
            m_const = Fraction(0)
            for i, seg in enumerate(segs):
                if seg.w_lo == seg.w_hi:
                    m_const += seg.t_lo
                else:
                    slope = (seg.t_hi - seg.t_lo) / (seg.w_hi - seg.w_lo)
                    m_row[i] = slope
                    m_const += seg.t_lo - slope * seg.w_lo
            eq_rows = [ones, m_row]
            eq_rhs = [total_water, moment_target - m_const]

            for point in _polytope_vertices(n, bounds, eq_rows, eq_rhs):
                # Exact feasibility re-check (also covers plateaus' moments).
                moments = []
                for i, (w, seg) in enumerate(zip(point, segs)):
                    if not (seg.w_lo <= w <= seg.w_hi):
                        break
                    moments.append(seg.moment(w))
                else:
                    if sum(moments, Fraction(0)) != moment_target:
                        continue
                    _, transfer_cost = _transfer_list(trays, starts_f, point)
                    total_cost = transfer_cost + weight_fee
                    tie = (
                        tuple(point),
                        tuple(sorted(w.label for w in hung)),
                        tuple(seg_index),
                    )
                    if best is None or (total_cost, tie) < (best[0], best[1]):
                        best = (total_cost, tie, (point, tuple(hung), transfer_cost, weight_fee,
                                                  tuple(seg_index), tuple(moments)))

    if best is None:
        raise InfeasibleBalance(
            "no legal transfer/weight combination reaches the target moment"
        )

    total_cost, _, payload = best
    point, hung, transfer_cost, weight_fee, seg_index, moments = payload
    transfers, _ = _transfer_list(trays, starts_f, point)
    finals: list[TrayFinal] = []
    positive = negative = Fraction(0)
    for i, pan in enumerate(trays):
        lookup = pan.lookup_water(point[i])
        finals.append(
            TrayFinal(
                name=pan.name,
                start=starts_f[i],
                water=point[i],
                z_lo=lookup.z_lo,
                z_hi=lookup.z_hi,
                moment=moments[i],
                segment_index=seg_index[i],
            )
        )
        if moments[i] >= 0:
            positive += moments[i]
        else:
            negative += moments[i]
    weight_signed = sum((w.moment for w in hung), Fraction(0))
    net = sum(moments, Fraction(0)) + weight_signed
    return LoadingPlan(
        trays=tuple(finals),
        hung=hung,
        transfers=transfers,
        transfer_cost=transfer_cost,
        weight_fee=weight_fee,
        total_cost=total_cost,
        target_moment=target,
        positive_moment=positive,
        negative_moment=negative,
        net_moment=net,
    )
