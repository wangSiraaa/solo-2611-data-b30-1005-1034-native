"""Pure imposition logic for the fictional print shop.

No SQLite access lives here so the rules can be unit-tested in isolation:

* a plate is one buyer's unique original, with allowed heads, a paper-edge
  registration-hole sequence and an optional flip;
* a reverse mount mirrors the hole sequence left-to-right;
* a two-up mount needs the two named plates on opposite heads with the
  effective hole sequences matching position by position (an empty head has
  no registration constraint);
* the planner minimises (torn labels, sheets pulled, plate changes) in that
  lexicographic order, subject to the paper and change limits.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

from .core import DomainError, PressInfeasible

LEFT = "L"
RIGHT = "R"
HEADS = (LEFT, RIGHT)
NORMAL = "NORMAL"
REVERSED = "REVERSED"
MAX_PLATES_PER_JOB = 6
MAX_SHEETS_PER_BATCH = 10


@dataclass(frozen=True)
class Plate:
    plate_id: str
    auction_id: str
    buyer: str
    qty: int
    allowed_heads: frozenset[str]
    holes: tuple[int, ...]
    flippable: bool

    def orientations(self) -> tuple[str, ...]:
        """Reverse mounting exists only when the plate is allowed to flip."""
        return (NORMAL, REVERSED) if self.flippable else (NORMAL,)


def effective_holes(holes: tuple[int, ...], orientation: str) -> tuple[int, ...]:
    """Hole order seen by the fixed pin row after mounting.

    Reversing a plate mirrors the paper edge, so the hole sequence is read
    right-to-left.
    """
    if orientation == NORMAL:
        return tuple(holes)
    if orientation == REVERSED:
        return tuple(reversed(holes))
    raise DomainError(f"unknown orientation {orientation!r}")


@dataclass(frozen=True)
class HeadMount:
    plate: Plate
    orientation: str

    def holes(self) -> tuple[int, ...]:
        return effective_holes(self.plate.holes, self.orientation)


@dataclass(frozen=True)
class Mount:
    """One locked-in press setup. ``None`` on a head means an empty head."""

    left: HeadMount | None
    right: HeadMount | None

    def key(self) -> tuple:
        def entry(head: HeadMount | None) -> tuple[str, str] | None:
            return None if head is None else (head.plate.plate_id, head.orientation)

        return (entry(self.left), entry(self.right))

    def plates(self) -> tuple[HeadMount, ...]:
        return tuple(h for h in (self.left, self.right) if h is not None)

    def is_two_up(self) -> bool:
        return self.left is not None and self.right is not None


@dataclass(frozen=True)
class BatchPlan:
    seq: int
    mount: Mount
    sheets: int
    is_change: bool
    # Per occupied plate: copies printed on these sheets and how many are torn.
    production: dict[str, dict[str, int]]
    torn: int


@dataclass(frozen=True)
class JobPlan:
    batches: tuple[BatchPlan, ...]
    paper_used: int
    changes_used: int
    torn_total: int
    delivered: dict[str, int]
    torn_by_plate: dict[str, int]


def prove_mount(
    plates_by_id: dict[str, Plate],
    left: dict[str, str] | None,
    right: dict[str, str] | None,
) -> dict[str, Any]:
    """Trial-imposition read used by the plate maker when opening a proof.

    Mirrors what the press can actually see on the pulled proof: real plate
    ids, head, orientation, effective hole sequences and exactly which pin
    positions fail to line up.  Nothing is written.
    """
    if left is None and right is None:
        raise DomainError("a mount must occupy at least one head")

    mounts: dict[str, HeadMount] = {}
    seen_plates: set[str] = set()
    for head, spec in ((LEFT, left), (RIGHT, right)):
        if spec is None:
            continue
        plate = plates_by_id.get(spec["plate_id"])
        if plate is None:
            raise DomainError(f"unknown plate {spec['plate_id']!r}")
        if plate.plate_id in seen_plates:
            raise DomainError(
                f"plate {plate.plate_id} cannot be duplicated onto both heads"
            )
        seen_plates.add(plate.plate_id)
        orientation = spec.get("orientation", NORMAL)
        if head not in plate.allowed_heads:
            raise DomainError(
                f"plate {plate.plate_id} is not allowed on head {head} "
                f"(allowed: {sorted(plate.allowed_heads)})"
            )
        if orientation == REVERSED and not plate.flippable:
            raise DomainError(f"plate {plate.plate_id} cannot be reversed (flip forbidden)")
        if orientation not in (NORMAL, REVERSED):
            raise DomainError(f"unknown orientation {orientation!r}")
        mounts[head] = HeadMount(plate, orientation)

    left_hm = mounts.get(LEFT)
    right_hm = mounts.get(RIGHT)
    mismatch: list[int] = []
    reason = "empty head has no registration constraint"
    registered = True

    if left_hm is not None and right_hm is not None:
        lh, rh = left_hm.holes(), right_hm.holes()
        if len(lh) != len(rh):
            registered = False
            reason = f"hole train length differs: {len(lh)} vs {len(rh)}"
            mismatch = sorted(
                set(range(len(lh))) ^ set(range(len(rh))),
            )
        else:
            mismatch = [i for i in range(len(lh)) if lh[i] != rh[i]]
            registered = not mismatch
            reason = (
                "pin positions line up one by one"
                if registered
                else f"pin mismatch at positions {mismatch}"
            )

    def describe(head: str, hm: HeadMount | None) -> dict[str, Any] | None:
        if hm is None:
            return None
        return {
            "head": head,
            "plate_id": hm.plate.plate_id,
            "buyer": hm.plate.buyer,
            "orientation": hm.orientation,
            "effective_holes": list(hm.holes()),
        }

    return {
        "left": describe(LEFT, left_hm),
        "right": describe(RIGHT, right_hm),
        "registered": registered,
        "mismatch_positions": mismatch,
        "reason": reason,
    }


def _single_mounts(plates: list[Plate]) -> list[Mount]:
    mounts: list[Mount] = []
    for plate in sorted(plates, key=lambda p: p.plate_id):
        for head in sorted(plate.allowed_heads):
            hm = HeadMount(plate, NORMAL)  # reversing a single head never helps
            if head == LEFT:
                mounts.append(Mount(hm, None))
            else:
                mounts.append(Mount(None, hm))
    return mounts


def _two_up_mounts(plates: list[Plate]) -> list[Mount]:
    """Every distinct feasible two-up setup.

    The two named plates sit on opposite heads, each in an allowed
    orientation; the fixed single pin row only accepts the setup when the
    effective hole trains are identical.
    """
    found: dict[tuple, Mount] = {}
    ordered = sorted(plates, key=lambda p: p.plate_id)
    for i, left_plate in enumerate(ordered):
        for right_plate in ordered[i + 1:]:
            for l_head in left_plate.allowed_heads:
                if l_head not in HEADS:
                    continue
                r_head = RIGHT if l_head == LEFT else LEFT
                if r_head not in right_plate.allowed_heads:
                    continue
                for l_orient in left_plate.orientations():
                    l_holes = effective_holes(left_plate.holes, l_orient)
                    for r_orient in right_plate.orientations():
                        if effective_holes(right_plate.holes, r_orient) != l_holes:
                            continue
                        left_hm = HeadMount(left_plate, l_orient)
                        right_hm = HeadMount(right_plate, r_orient)
                        mount = (
                            Mount(left_hm, right_hm)
                            if l_head == LEFT
                            else Mount(right_hm, left_hm)
                        )
                        found.setdefault(mount.key(), mount)
    return [found[k] for k in sorted(found, key=lambda k: repr(k))]


def candidate_mounts(plates: list[Plate]) -> list[Mount]:
    """Two-up setups first (they save paper), then deterministic single heads."""
    return _two_up_mounts(plates) + _single_mounts(plates)


def plan_job(
    plates: list[Plate],
    paper_limit: int,
    max_changes: int,
) -> JobPlan:
    """Find the lexicographically best feasible work order.

    Order of preference: fewest torn labels, then fewest sheets, then fewest
    plate changes (and, on a full tie, fewer, bigger batches).  Raises
    :class:`PressInfeasible` when no sequence of mounts can deliver every
    plate within the press limits.

    A fast deterministic zero-waste search is tried first; with no tearing
    allowed every mount has exactly one legal pull size, so the explored set is
    tiny.  If it fails, the minimum number of distinct mounts that jointly
    cover every plate (a set-cover lower bound on plate changes) decides whether
    the request is simply impossible, after which a bounded general search that
    permits tearing finds the minimum-waste plan.
    """
    if not plates:
        raise DomainError("a press job needs at least one plate")
    if len(plates) > MAX_PLATES_PER_JOB:
        raise DomainError(f"at most {MAX_PLATES_PER_JOB} plates per job")
    if paper_limit < 1:
        raise DomainError("paper_limit must be >= 1")
    if max_changes < 0:
        raise DomainError("max_changes must be >= 0")

    mounts = candidate_mounts(plates)
    index = {p.plate_id: i for i, p in enumerate(plates)}
    demands = tuple(p.qty for p in plates)
    target = tuple(0 for _ in plates)
    plate_ids = {p.plate_id for p in plates}

    def capped(remaining: tuple[int, ...]) -> tuple[int, ...]:
        return tuple(max(0, r) for r in remaining)

    def search(zero_waste_only: bool):
        # Budget-independent memo: (remaining demand, current mount) -> cheapest
        # suffix.  Budgets prune at the call site and are re-checked on a hit.
        memo: dict[tuple, tuple[tuple[int, int, int, int], list]] = {}

        def dfs(remaining, prev_idx, paper_left, changes_left):
            if remaining == target:
                return (0, 0, 0, 0), []
            key = (capped(remaining), prev_idx)
            if key in memo:
                score, steps = memo[key]
                _w, suffix_paper, suffix_changes, _b = score
                if (
                    suffix_paper <= paper_left
                    and suffix_changes <= changes_left
                    and (not zero_waste_only or score[0] == 0)
                ):
                    return score, steps
                return None

            best_score: tuple[int, int, int, int] | None = None
            best_steps: list[tuple[int, int]] | None = None
            for m_idx, mount in enumerate(mounts):
                occupied = [index[hm.plate.plate_id] for hm in mount.plates()]
                if all(remaining[i] <= 0 for i in occupied):
                    continue  # mount cannot make progress
                is_change = prev_idx is not None and prev_idx != m_idx
                next_changes_left = changes_left - (1 if is_change else 0)
                if next_changes_left < 0:
                    continue

                if zero_waste_only:
                    # Zero waste: every occupied plate must still owe labels and
                    # the unique non-dominated pull finishes the neediest-smallest
                    # occupied plate exactly (k = min remaining).  A smaller pull
                    # completes no plate and is dominated by pulling this amount
                    # on the same mount (same change count, zero waste, less
                    # paper); a larger pull would overshoot and tear.
                    if any(remaining[i] <= 0 for i in occupied):
                        continue
                    k = min(remaining[i] for i in occupied)
                    if k > MAX_SHEETS_PER_BATCH or k > paper_left:
                        continue
                    pull_sizes = (k,)
                else:
                    upper = min(
                        MAX_SHEETS_PER_BATCH,
                        paper_left,
                        max(1, max(remaining[i] for i in occupied)),
                    )
                    pull_sizes = range(1, upper + 1)

                for k in pull_sizes:
                    next_remaining = list(remaining)
                    waste = 0
                    for i in occupied:
                        if next_remaining[i] > 0:
                            waste += max(0, k - next_remaining[i])
                        else:
                            waste += k
                        next_remaining[i] -= k
                    if zero_waste_only and waste > 0:
                        continue
                    suffix = dfs(
                        capped(tuple(next_remaining)),
                        m_idx,
                        paper_left - k,
                        next_changes_left,
                    )
                    if suffix is None:
                        continue
                    (s_waste, s_paper, s_changes, s_batches), steps = suffix
                    candidate_score = (
                        waste + s_waste,
                        k + s_paper,
                        (1 if is_change else 0) + s_changes,
                        1 + s_batches,
                    )
                    candidate_steps = [(m_idx, k), *steps]
                    if best_score is None or candidate_score < best_score:
                        best_score = candidate_score
                        best_steps = candidate_steps

            if best_score is None:
                return None
            incumbent = memo.get(key)
            if incumbent is None or best_score < incumbent[0]:
                memo[key] = (best_score, best_steps)
            return memo[key]

        return dfs(demands, None, paper_limit, max_changes)

    def minimum_covering_mounts() -> int:
        """Smallest number of distinct mounts whose plate sets cover every
        plate; a hard lower bound on the plate changes any job needs."""
        cover_sets = [
            {hm.plate.plate_id for hm in m.plates()} for m in mounts
        ]
        for size in range(1, len(cover_sets) + 1):
            for combo in itertools.combinations(range(len(cover_sets)), size):
                covered: set[str] = set()
                for ci in combo:
                    covered |= cover_sets[ci]
                if covered >= plate_ids:
                    return size
        return len(cover_sets) + 1

    zero_waste = search(zero_waste_only=True)
    if zero_waste is not None:
        (torn_total, paper_used, changes_used, _b), steps = zero_waste
        return _build_plan(
            plates, mounts, demands, steps, torn_total, paper_used, changes_used
        )

    # Zero waste did not fit.  Two budget-independent lower bounds can prove the
    # request impossible without any expensive search:
    #   * paper: a sheet yields at most two owed labels (one per head), and each
    #     individual plate still needs its full awarded quantity;
    #   * changes: every distinct plate must be mounted at least once, so the
    #     smallest set of mounts covering all plates bounds the change count.
    has_two_up = any(m.is_two_up() for m in mounts)
    per_sheet_capacity = 2 if has_two_up else 1
    paper_lower_bound = max(
        max(demands),
        (sum(demands) + per_sheet_capacity - 1) // per_sheet_capacity,
    )
    if paper_lower_bound > paper_limit:
        raise PressInfeasible(
            "cannot deliver every plate within "
            f"paper_limit={paper_limit}, max_changes={max_changes}"
        )
    if minimum_covering_mounts() - 1 > max_changes:
        raise PressInfeasible(
            "cannot deliver every plate within "
            f"paper_limit={paper_limit}, max_changes={max_changes}"
        )

    # Both lower bounds are satisfied: a tearing plan may exist.  Run the
    # bounded general search to find the minimum-waste one.
    found = search(zero_waste_only=False)
    if found is None:
        raise PressInfeasible(
            "cannot deliver every plate within "
            f"paper_limit={paper_limit}, max_changes={max_changes}"
        )
    (torn_total, paper_used, changes_used, _b), steps = found
    return _build_plan(
        plates, mounts, demands, steps, torn_total, paper_used, changes_used
    )


def _build_plan(
    plates: list[Plate],
    mounts: list[Mount],
    demands: tuple[int, ...],
    steps: list[tuple[int, int]],
    torn_total: int,
    paper_used: int,
    changes_used: int,
) -> JobPlan:
    remaining = list(demands)
    index = {p.plate_id: i for i, p in enumerate(plates)}
    batches: list[BatchPlan] = []
    prev_key: tuple | None = None
    for seq, (m_idx, k) in enumerate(steps, start=1):
        mount = mounts[m_idx]
        is_change = prev_key is not None and mount.key() != prev_key
        production: dict[str, dict[str, int]] = {}
        batch_torn = 0
        for hm in mount.plates():
            i = index[hm.plate.plate_id]
            useful = max(0, min(k, remaining[i]))
            torn = k - useful
            batch_torn += torn
            production[hm.plate.plate_id] = {
                "head": LEFT if mount.left is hm else RIGHT,
                "orientation": hm.orientation,
                "printed": k,
                "delivered": useful,
                "torn": torn,
            }
            remaining[i] -= k
        batches.append(
            BatchPlan(
                seq=seq,
                mount=mount,
                sheets=k,
                is_change=is_change,
                production=production,
                torn=batch_torn,
            )
        )
        prev_key = mount.key()

    delivered = {p.plate_id: 0 for p in plates}
    torn_by_plate = {p.plate_id: 0 for p in plates}
    for batch in batches:
        for plate_id, acc in batch.production.items():
            delivered[plate_id] += acc["delivered"]
            torn_by_plate[plate_id] += acc["torn"]
    return JobPlan(
        batches=tuple(batches),
        paper_used=paper_used,
        changes_used=changes_used,
        torn_total=torn_total,
        delivered=delivered,
        torn_by_plate=torn_by_plate,
    )
