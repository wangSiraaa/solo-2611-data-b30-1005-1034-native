"""Distribution desk (发放台) for the joint-exhibition pairing service.

The desk may hand each branch exactly one file:

  * left branch:  rows of ``(组 group, 左成员 member, 章号 stamp)``
  * right branch: rows of ``(组 group, 右成员 member, 章号 stamp)``

Joining the two files on ``(group, stamp)`` — and nothing else — must rebuild
*exactly* the approved relation: duplicate hits merge (重复命中归一), no
approved pair may be missed, and no originally unpaired members may be
matched by sharing a stamp (额外错配).  There is no supplementary cross
table.

One stamp may be stamped for several left ends and several right ends (a
biclique); each stamp is carved for one group only and never prints as two
stamp numbers.  Every person receives a single paper card covering all
their exhibition groups, so the stamps one person carries are counted
*across* groups; the site also has a total budget of carved stamps.

The planner minimises the number of carved stamps.  Ties may be broken
toward either side (章记主要落在哪边), and either side's personal slot
limits may be tightened to trial an issue.  When no assignment works, the
plan names the pairs (搭档) and the extra mispairs (错配) that block it.

Documented envelope: at most 2 groups; per group at most 5 left ends,
5 right ends and 12 true pairs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable

from .contract import Contract
from .errors import ContractError, LimitError
from .types import IntType, TextType

# Documented envelope (see module docstring).
MAX_GROUPS = 2
MAX_SIDE = 5
MAX_PAIRS = 12
# Safety valve for the exact search; unreachable inside the envelope.
MAX_SEARCH_NODES = 2_000_000
# How many minimum-stamp covers are listed in an infeasible report.
MAX_ATTEMPTS = 16

_SIDE_LEFT = "left"
_SIDE_RIGHT = "right"


def _sort_key(value: Any) -> str:
    return f"{type(value).__name__}:{value!r}"


def _pair_sort_key(pair: tuple) -> str:
    return f"{_sort_key(pair[0])}|{_sort_key(pair[1])}"


# ---------------------------------------------------------------------------
# plan result
# ---------------------------------------------------------------------------


@dataclass
class Stamp:
    """One carved stamp: a biclique inside one group's approved relation."""

    group: Any
    stamp_no: int
    lefts: tuple
    rights: tuple

    def to_json(self) -> dict:
        return {
            "group": self.group,
            "stamp_no": self.stamp_no,
            "lefts": list(self.lefts),
            "rights": list(self.rights),
        }


@dataclass
class DistributionPlan:
    status: str  # "ISSUED" | "INFEASIBLE"
    stamps: list[Stamp] = field(default_factory=list)
    left_rows: list[tuple] = field(default_factory=list)  # (group, member, stamp_no)
    right_rows: list[tuple] = field(default_factory=list)
    stamp_count: int = 0
    loads: dict = field(default_factory=dict)  # {"left": {m: n}, "right": {m: n}}
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "stamp_count": self.stamp_count,
            "stamps": [s.to_json() for s in self.stamps],
            "left_rows": [list(r) for r in self.left_rows],
            "right_rows": [list(r) for r in self.right_rows],
            "loads": _json_copy(self.loads),
            "reasons": list(self.reasons),
            "details": _json_copy(self.details),
        }


def _json_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_copy(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# input normalisation
# ---------------------------------------------------------------------------


def _normalize_relation(relation: Any) -> dict[Any, set[tuple]]:
    if not isinstance(relation, dict):
        raise ContractError("relation must be a dict {group: iterable of (left, right)}")
    out: dict[Any, set[tuple]] = {}
    for group, pairs in relation.items():
        if not isinstance(pairs, Iterable):
            raise ContractError(f"group {group!r}: pairs must be an iterable")
        normalised = set()
        for pair in pairs:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise ContractError(
                    f"group {group!r}: pair {pair!r} is not a (left, right) pair"
                )
            normalised.add((pair[0], pair[1]))
        if normalised:
            out[group] = normalised
    return out


def _check_envelope(relation: dict[Any, set[tuple]]) -> None:
    if len(relation) > MAX_GROUPS:
        raise LimitError(
            f"at most {MAX_GROUPS} exhibition groups, got {len(relation)}"
        )
    for group, pairs in relation.items():
        lefts = {p[0] for p in pairs}
        rights = {p[1] for p in pairs}
        if len(lefts) > MAX_SIDE:
            raise LimitError(
                f"group {group!r}: at most {MAX_SIDE} left ends, got {len(lefts)}"
            )
        if len(rights) > MAX_SIDE:
            raise LimitError(
                f"group {group!r}: at most {MAX_SIDE} right ends, got {len(rights)}"
            )
        if len(pairs) > MAX_PAIRS:
            raise LimitError(
                f"group {group!r}: at most {MAX_PAIRS} true pairs, got {len(pairs)}"
            )


def _normalize_slots(slots: Any, members: set, side: str) -> dict:
    """Personal card slots: None (unlimited), an int for everyone, or a dict."""
    if slots is None:
        return {}
    if isinstance(slots, bool):
        raise ContractError(f"{side}_slots must be an int or a dict, got bool")
    if isinstance(slots, int):
        if slots < 0:
            raise ContractError(f"{side}_slots must be >= 0, got {slots}")
        return {member: slots for member in members}
    if isinstance(slots, dict):
        out = {}
        for member, limit in slots.items():
            if member not in members:
                raise ContractError(
                    f"{side}_slots names {member!r}, who has no approved pair"
                )
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ContractError(
                    f"{side}_slots for {member!r} must be an int >= 0, got {limit!r}"
                )
            out[member] = limit
        return out
    raise ContractError(f"{side}_slots must be None, an int, or a dict")


def _normalize_budget(total_stamps: Any) -> int | None:
    if total_stamps is None:
        return None
    if isinstance(total_stamps, bool) or not isinstance(total_stamps, int):
        raise ContractError("total_stamps must be None or an int")
    if total_stamps < 0:
        raise ContractError("total_stamps must be >= 0")
    return total_stamps


# ---------------------------------------------------------------------------
# exact biclique-cover search
# ---------------------------------------------------------------------------


class _GroupState:
    __slots__ = ("group", "pairs", "lefts", "rights", "cands", "containing", "full_mask")

    def __init__(self, group: Any, pairs: set[tuple]):
        self.group = group
        self.pairs = sorted(pairs, key=_pair_sort_key)
        self.lefts = sorted({p[0] for p in pairs}, key=_sort_key)
        self.rights = sorted({p[1] for p in pairs}, key=_sort_key)
        index = {pair: i for i, pair in enumerate(self.pairs)}
        edges = set(pairs)
        cands: list[tuple[int, tuple, tuple]] = []
        for lmask in range(1, 1 << len(self.lefts)):
            ls = tuple(
                self.lefts[i] for i in range(len(self.lefts)) if lmask >> i & 1
            )
            for rmask in range(1, 1 << len(self.rights)):
                rs = tuple(
                    self.rights[j] for j in range(len(self.rights)) if rmask >> j & 1
                )
                if all((l, r) in edges for l in ls for r in rs):
                    mask = 0
                    for l in ls:
                        for r in rs:
                            mask |= 1 << index[(l, r)]
                    cands.append((mask, ls, rs))
        # Larger bicliques first: good covers are found early.
        cands.sort(key=lambda c: (-(len(c[1]) * len(c[2])), repr(c[1]), repr(c[2])))
        self.cands = cands
        self.containing = [[] for _ in self.pairs]
        for ci, (mask, _, _) in enumerate(cands):
            m = mask
            while m:
                bit = m & -m
                self.containing[bit.bit_length() - 1].append(ci)
                m ^= bit
        self.full_mask = (1 << len(self.pairs)) - 1


def _person(side: str, member: Any) -> tuple:
    return (side, member)


def _plan_key(chosen: list, loads: dict, prefer: str) -> tuple:
    left_total = sum(n for (side, _), n in loads.items() if side == _SIDE_LEFT)
    right_total = sum(n for (side, _), n in loads.items() if side == _SIDE_RIGHT)
    # 章记主要落在哪边: minimise the records landing on the *other* side.
    other = right_total if prefer == _SIDE_LEFT else left_total
    structure = repr(sorted(repr(c) for c in chosen))
    return (len(chosen), other, structure)


class _Search:
    """Exact joint search over all groups: fewest stamps, slot limits kept."""

    def __init__(
        self,
        groups: list[_GroupState],
        limits: dict,
        count_limit: int,
        prefer: str,
        node_cap: int,
    ):
        self.groups = groups
        self.limits = limits  # (side, member) -> max stamps carried
        self.count_limit = count_limit
        self.prefer = prefer
        self.node_cap = node_cap
        self.nodes = 0
        self.best_key: tuple | None = None
        self.best_chosen: list | None = None
        # Upper bound on remaining pairs per group, for the count prune.
        self.max_area = [
            max((len(c[1]) * len(c[2])) for c in g.cands) for g in groups
        ]

    def run(self) -> list | None:
        covered = [0] * len(self.groups)
        self._dfs(0, covered, {}, [], 0)
        return self.best_chosen

    def _dfs(self, gi: int, covered: list, loads: dict, chosen: list, count: int) -> None:
        self.nodes += 1
        if self.nodes > self.node_cap:
            raise LimitError(
                f"stamp search exceeded {self.node_cap} nodes; the documented "
                f"envelope (<= {MAX_GROUPS} groups, <= {MAX_SIDE}+{MAX_SIDE} ends, "
                f"<= {MAX_PAIRS} pairs) never reaches this"
            )
        while gi < len(self.groups) and covered[gi] == self.groups[gi].full_mask:
            gi += 1
        if gi == len(self.groups):
            key = _plan_key(chosen, loads, self.prefer)
            if self.best_key is None or key < self.best_key:
                self.best_key = key
                self.best_chosen = list(chosen)
            return
        best_count = self.best_key[0] if self.best_key else self.count_limit + 1
        remaining = 0
        for g in range(gi, len(self.groups)):
            left_over = self.groups[g].full_mask & ~covered[g]
            remaining += -(-bin(left_over).count("1") // self.max_area[g])
        if count + remaining > min(self.count_limit, best_count):
            return
        state = self.groups[gi]
        uncovered = state.full_mask & ~covered[gi]
        first = (uncovered & -uncovered).bit_length() - 1
        for ci in state.containing[first]:
            if count + 1 > self.count_limit or count + 1 > best_count:
                break
            mask, ls, rs = state.cands[ci]
            new_loads = loads
            ok = True
            for member in ls:
                person = _person(_SIDE_LEFT, member)
                n = new_loads.get(person, 0) + 1
                if n > self.limits.get(person, n):
                    ok = False
                    break
                if new_loads is loads:
                    new_loads = dict(loads)
                new_loads[person] = n
            if not ok:
                continue
            for member in rs:
                person = _person(_SIDE_RIGHT, member)
                n = new_loads.get(person, 0) + 1
                if n > self.limits.get(person, n):
                    ok = False
                    break
                if new_loads is loads:
                    new_loads = dict(loads)
                new_loads[person] = n
            if not ok:
                continue
            old = covered[gi]
            covered[gi] = old | mask
            chosen.append((gi, ci))
            self._dfs(gi, covered, new_loads, chosen, count + 1)
            chosen.pop()
            covered[gi] = old


def _build_groups(relation: dict[Any, set[tuple]]) -> list[_GroupState]:
    return [
        _GroupState(group, pairs)
        for group, pairs in sorted(relation.items(), key=lambda kv: _sort_key(kv[0]))
    ]


def _solve(
    groups: list[_GroupState],
    limits: dict,
    count_limit: int,
    prefer: str,
) -> list | None:
    search = _Search(groups, limits, count_limit, prefer, MAX_SEARCH_NODES)
    return search.run()


def _chosen_to_stamps(groups: list[_GroupState], chosen: list) -> list[Stamp]:
    per_group: dict[int, list] = {}
    for gi, ci in chosen:
        per_group.setdefault(gi, []).append(groups[gi].cands[ci])
    stamps: list[Stamp] = []
    for gi in sorted(per_group):
        cands = sorted(
            per_group[gi], key=lambda c: (repr(c[1]), repr(c[2]))
        )
        for stamp_no, (_, ls, rs) in enumerate(cands, start=1):
            stamps.append(
                Stamp(group=groups[gi].group, stamp_no=stamp_no, lefts=ls, rights=rs)
            )
    return stamps


def _rows_and_loads(stamps: list[Stamp]) -> tuple[list, list, dict]:
    left_rows: list[tuple] = []
    right_rows: list[tuple] = []
    loads: dict[str, dict] = {_SIDE_LEFT: {}, _SIDE_RIGHT: {}}
    for stamp in stamps:
        for member in stamp.lefts:
            left_rows.append((stamp.group, member, stamp.stamp_no))
            loads[_SIDE_LEFT][member] = loads[_SIDE_LEFT].get(member, 0) + 1
        for member in stamp.rights:
            right_rows.append((stamp.group, member, stamp.stamp_no))
            loads[_SIDE_RIGHT][member] = loads[_SIDE_RIGHT].get(member, 0) + 1
    left_rows.sort(key=lambda r: (repr(r[0]), repr(r[1]), r[2]))
    right_rows.sort(key=lambda r: (repr(r[0]), repr(r[1]), r[2]))
    return left_rows, right_rows, loads


# ---------------------------------------------------------------------------
# infeasibility explanations
# ---------------------------------------------------------------------------


def _incompatibility_clique(state: _GroupState) -> list[int]:
    """A set of pairs (indexes) that can never share one stamp.

    ``p`` and ``q`` are incompatible iff putting them under one stamp would
    add a cross pair outside the approved relation.  A clique of size k here
    certifies that the group needs at least k stamps.
    """
    pairs = state.pairs
    edges = set(pairs)
    n = len(pairs)
    incompatible = [[False] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            l1, r1 = pairs[i]
            l2, r2 = pairs[j]
            if (l1, r2) not in edges or (l2, r1) not in edges:
                incompatible[i][j] = incompatible[j][i] = True

    best: list[int] = []

    def extend(clique: list[int], candidates: list[int]) -> None:
        nonlocal best
        if len(clique) + len(candidates) <= len(best):
            return
        if not candidates:
            if len(clique) > len(best):
                best = list(clique)
            return
        v = candidates[0]
        extend(clique + [v], [w for w in candidates[1:] if incompatible[v][w]])
        extend(clique, candidates[1:])

    extend([], list(range(n)))
    return best


def _conflict_witnesses(state: _GroupState, clique: list[int]) -> list[dict]:
    """Human-checkable reasons why clique pairs cannot share a stamp."""
    pairs = state.pairs
    edges = set(pairs)
    witnesses = []
    for x in range(len(clique)):
        for y in range(x + 1, len(clique)):
            l1, r1 = pairs[clique[x]]
            l2, r2 = pairs[clique[y]]
            would_add = sorted(
                {p for p in ((l1, r2), (l2, r1)) if p not in edges},
                key=_pair_sort_key,
            )
            if would_add:
                witnesses.append(
                    {
                        "group": state.group,
                        "pairs": [(l1, r1), (l2, r2)],
                        "would_add": would_add,
                        "explanation": (
                            f"pairs {(l1, r1)!r} and {(l2, r2)!r} cannot share a "
                            f"stamp: one stamp would then also pair "
                            + ", ".join(f"{p!r}" for p in would_add)
                            + ", which is not approved"
                        ),
                    }
                )
    return witnesses


def _enumerate_min_covers(
    groups: list[_GroupState], count: int, cap: int
) -> list[list]:
    """Up to ``cap`` distinct covers using exactly ``count`` stamps."""
    found: list[list] = []
    covered = [0] * len(groups)

    def dfs(gi: int, chosen: list) -> None:
        if len(found) >= cap:
            return
        while gi < len(groups) and covered[gi] == groups[gi].full_mask:
            gi += 1
        if gi == len(groups):
            found.append(list(chosen))
            return
        if len(chosen) >= count:
            return
        state = groups[gi]
        uncovered = state.full_mask & ~covered[gi]
        first = (uncovered & -uncovered).bit_length() - 1
        for ci in state.containing[first]:
            mask, _, _ = state.cands[ci]
            old = covered[gi]
            covered[gi] = old | mask
            chosen.append((gi, ci))
            dfs(gi, chosen)
            chosen.pop()
            covered[gi] = old
            if len(found) >= cap:
                return

    dfs(0, [])
    return found


def _violations(loads: dict, limits: dict) -> list[str]:
    out = []
    for (side, member), n in sorted(loads.items(), key=lambda kv: _sort_key(kv[0])):
        cap = limits.get((side, member))
        if cap is not None and n > cap:
            who = "left member" if side == _SIDE_LEFT else "right member"
            out.append(f"{who} {member!r} carries {n} stamps (card allows {cap})")
    return out


# ---------------------------------------------------------------------------
# the planner
# ---------------------------------------------------------------------------


def plan_distribution(
    relation: Any,
    *,
    left_slots: Any = None,
    right_slots: Any = None,
    total_stamps: Any = None,
    prefer: str = _SIDE_LEFT,
) -> DistributionPlan:
    """Plan the two stamp lists for an approved relation.

    ``relation`` is ``{group: iterable of (left_member, right_member)}`` as
    signed out by the registration desk.  ``left_slots`` / ``right_slots``
    are ``None`` (unlimited), one int for every person, or a per-person dict;
    a person's stamps are counted across all groups (one paper card each).
    ``total_stamps`` caps the site-wide number of carved stamps.  ``prefer``
    (``"left"`` / ``"right"``) decides on which side the stamp records
    mainly land when several minimum-stamp plans exist.
    """
    if prefer not in (_SIDE_LEFT, _SIDE_RIGHT):
        raise ContractError(f"prefer must be 'left' or 'right', got {prefer!r}")
    normalised = _normalize_relation(relation)
    _check_envelope(normalised)
    budget = _normalize_budget(total_stamps)

    left_members = {p[0] for pairs in normalised.values() for p in pairs}
    right_members = {p[1] for pairs in normalised.values() for p in pairs}
    limits: dict = {}
    for member, cap in _normalize_slots(left_slots, left_members, "left").items():
        limits[_person(_SIDE_LEFT, member)] = cap
    for member, cap in _normalize_slots(right_slots, right_members, "right").items():
        limits[_person(_SIDE_RIGHT, member)] = cap

    groups = _build_groups(normalised)
    total_pairs = sum(len(g.pairs) for g in groups)
    if total_pairs == 0:
        return DistributionPlan(status="ISSUED", reasons=["empty relation: no stamps needed"])

    # 1. Fewest stamps ignoring personal slot limits (always possible:
    #    one stamp per pair is a valid cover).
    free_count_limit = total_pairs if budget is None else min(budget, total_pairs)
    free_chosen = _solve(groups, {}, free_count_limit, prefer)
    if free_chosen is None:
        # The budget itself is too small even before personal slot limits.
        min_chosen = _solve(groups, {}, total_pairs, prefer)
        assert min_chosen is not None  # singletons always cover
        return _infeasible_budget(groups, min_chosen, budget)

    # 2. Same objective with the personal slot limits enforced.
    chosen = _solve(groups, limits, free_count_limit, prefer)
    if chosen is None:
        return _infeasible_slots(groups, free_chosen, limits, budget)

    stamps = _chosen_to_stamps(groups, chosen)
    left_rows, right_rows, loads = _rows_and_loads(stamps)
    witness = verify_rejoin(
        left_rows,
        right_rows,
        normalised,
        left_slots=left_slots,
        right_slots=right_slots,
        total_stamps=budget,
    )
    assert witness["equal"], "internal: planned lists must rejoin to the relation"
    return DistributionPlan(
        status="ISSUED",
        stamps=stamps,
        left_rows=left_rows,
        right_rows=right_rows,
        stamp_count=len(stamps),
        loads=loads,
        reasons=[],
        details={
            "min_stamps": len(free_chosen),
            "budget": budget,
            "prefer": prefer,
            "witness": witness,
        },
    )


def _infeasible_budget(
    groups: list[_GroupState], min_chosen: list, budget: int
) -> DistributionPlan:
    min_stamps = len(min_chosen)
    conflicts: list[dict] = []
    clique_sizes: dict[Any, int] = {}
    for state in groups:
        clique = _incompatibility_clique(state)
        clique_sizes[state.group] = len(clique)
        conflicts.extend(_conflict_witnesses(state, clique))
    reasons = [
        f"cannot issue: the relation needs at least {min_stamps} carved stamps "
        f"but the site budget is {budget}"
    ]
    for witness in conflicts:
        reasons.append(witness["explanation"])
    return DistributionPlan(
        status="INFEASIBLE",
        reasons=reasons,
        details={
            "kind": "stamp_budget",
            "min_stamps": min_stamps,
            "budget": budget,
            "pairwise_conflicts": conflicts,
            "pairwise_unshareable_per_group": clique_sizes,
            "note": "minimum proved by exhaustive exact search; the listed "
            "pairs are pairwise unshareable (each shared stamp would add the "
            "named unapproved mispairs)",
        },
    )


def _infeasible_slots(
    groups: list[_GroupState],
    free_chosen: list,
    limits: dict,
    budget: int | None,
) -> DistributionPlan:
    min_stamps = len(free_chosen)
    attempts = []
    for chosen in _enumerate_min_covers(groups, min_stamps, MAX_ATTEMPTS):
        stamps = _chosen_to_stamps(groups, chosen)
        _, _, loads = _rows_and_loads(stamps)
        flat_loads = {
            (side, member): n
            for side, side_loads in loads.items()
            for member, n in side_loads.items()
        }
        attempts.append(
            {
                "stamps": [s.to_json() for s in stamps],
                "loads": loads,
                "violations": _violations(flat_loads, limits),
            }
        )
    reasons = [
        f"cannot issue: no stamp assignment satisfies the personal card "
        f"limits (minimum-stamp plans need {min_stamps} stamps; the limits "
        f"cannot be met at any stamp count)"
    ]
    for attempt in attempts[:3]:
        reasons.extend(attempt["violations"])
    if budget is None:
        note = (
            "exhaustive: every cover (up to one stamp per pair) was checked; "
            "none satisfies the personal slot limits"
        )
    else:
        note = (
            f"exhaustive within the site budget: every cover of up to "
            f"{budget} stamps was checked; none satisfies the personal slot limits"
        )
    return DistributionPlan(
        status="INFEASIBLE",
        reasons=reasons,
        details={
            "kind": "slot_limits",
            "min_stamps": min_stamps,
            "budget": budget,
            "attempts": attempts,
            "attempts_capped": len(attempts) >= MAX_ATTEMPTS,
            "note": note,
        },
    )


# ---------------------------------------------------------------------------
# witness: rejoin the two lists independently
# ---------------------------------------------------------------------------


def _as_triples(rows: Iterable[tuple], side: str) -> list[tuple]:
    out = []
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 3:
            raise ContractError(
                f"{side} row {row!r} is not a (group, member, stamp) triple"
            )
        out.append((row[0], row[1], row[2]))
    return out


def verify_rejoin(
    left_rows: Iterable[tuple],
    right_rows: Iterable[tuple],
    relation: Any,
    *,
    left_slots: Any = None,
    right_slots: Any = None,
    total_stamps: Any = None,
) -> dict:
    """Rejoin the two issued lists on (group, stamp) and compare to the target.

    This is the 确能复联的见证: it consumes only the two files' rows and the
    approved relation, merges duplicate hits, and reports missing pairs,
    extra mispairs, per-person loads against the cards, and the stamp count
    against the site budget.
    """
    target = _normalize_relation(relation)
    left_map: dict[tuple, set] = {}
    right_map: dict[tuple, set] = {}
    duplicates = 0
    for group, member, stamp in _as_triples(left_rows, "left"):
        key = (group, stamp)
        before = len(left_map.setdefault(key, set()))
        left_map[key].add(member)
        duplicates += len(left_map[key]) == before
    for group, member, stamp in _as_triples(right_rows, "right"):
        key = (group, stamp)
        before = len(right_map.setdefault(key, set()))
        right_map[key].add(member)
        duplicates += len(right_map[key]) == before

    rejoined: dict[Any, set] = {}
    for key in sorted(set(left_map) & set(right_map), key=repr):
        group, _ = key
        for left_member in left_map[key]:
            for right_member in right_map[key]:
                rejoined.setdefault(group, set()).add((left_member, right_member))

    all_groups = sorted(set(target) | set(rejoined), key=_sort_key)
    missing: list[tuple] = []
    extra: list[tuple] = []
    for group in all_groups:
        want = target.get(group, set())
        got = rejoined.get(group, set())
        missing.extend((group, l, r) for l, r in want - got)
        extra.extend((group, l, r) for l, r in got - want)
    missing.sort(key=lambda t: (repr(t[0]), repr(t[1]), repr(t[2])))
    extra.sort(key=lambda t: (repr(t[0]), repr(t[1]), repr(t[2])))

    loads: dict[str, dict] = {_SIDE_LEFT: {}, _SIDE_RIGHT: {}}
    for (group, stamp), members in left_map.items():
        for member in members:
            loads[_SIDE_LEFT][member] = loads[_SIDE_LEFT].get(member, 0) + 1
    for (group, stamp), members in right_map.items():
        for member in members:
            loads[_SIDE_RIGHT][member] = loads[_SIDE_RIGHT].get(member, 0) + 1

    left_members = {p[0] for pairs in target.values() for p in pairs}
    right_members = {p[1] for pairs in target.values() for p in pairs}
    limits: dict = {}
    for member, cap in _normalize_slots(left_slots, left_members, "left").items():
        limits[_person(_SIDE_LEFT, member)] = cap
    for member, cap in _normalize_slots(right_slots, right_members, "right").items():
        limits[_person(_SIDE_RIGHT, member)] = cap
    flat_loads = {
        (side, member): n
        for side, side_loads in loads.items()
        for member, n in side_loads.items()
    }
    slot_violations = _violations(flat_loads, limits)

    stamp_keys = set(left_map) | set(right_map)
    stamp_count = len(stamp_keys)
    budget_ok = total_stamps is None or stamp_count <= total_stamps

    # One stamp, one number: two stamps in a group with identical member
    # sets would be one stamp virtually printed as two numbers.
    seen_shapes: dict[tuple, tuple] = {}
    duplicate_stamps: list[tuple] = []
    for key in sorted(stamp_keys, key=repr):
        shape = (
            key[0],
            tuple(sorted(left_map.get(key, set()), key=_sort_key)),
            tuple(sorted(right_map.get(key, set()), key=_sort_key)),
        )
        if shape in seen_shapes:
            duplicate_stamps.append(key)
        else:
            seen_shapes[shape] = key

    equal = not missing and not extra
    ok = equal and not slot_violations and budget_ok and not duplicate_stamps
    return {
        "ok": ok,
        "equal": equal,
        "missing_pairs": [list(t) for t in missing],
        "extra_pairs": [list(t) for t in extra],
        "rejoined": {
            repr(group): sorted(pairs, key=_pair_sort_key)
            for group, pairs in sorted(rejoined.items(), key=lambda kv: _sort_key(kv[0]))
        },
        "stamp_count": stamp_count,
        "budget": total_stamps,
        "budget_ok": budget_ok,
        "loads": loads,
        "slot_violations": slot_violations,
        "duplicate_rows_merged": duplicates,
        "duplicate_stamps": [list(k) for k in duplicate_stamps],
    }


# ---------------------------------------------------------------------------
# issuing the two files through real writer/reader contracts
# ---------------------------------------------------------------------------


def issue_file_documents(
    plan: DistributionPlan,
    *,
    left_writer: Contract,
    left_reader: Contract,
    right_writer: Contract,
    right_reader: Contract,
    left_file_id: str = "issue-left-1",
    right_file_id: str = "issue-right-1",
) -> tuple[dict, dict]:
    """Build the two issue file documents (desk as writer, branch as reader).

    The left file carries only ``(group_no, member_no, stamp_no)`` for left
    members, the right file only the right members' triples; there is no
    supplementary cross table.
    """
    if plan.status != "ISSUED":
        raise ContractError("cannot issue files for an infeasible plan")
    for name, contract, role in (
        ("left_writer", left_writer, "writer"),
        ("left_reader", left_reader, "reader"),
        ("right_writer", right_writer, "writer"),
        ("right_reader", right_reader, "reader"),
    ):
        if contract.role != role:
            raise ContractError(f"{name} must be a {role} contract")

    def row(group, member, stamp):
        return {
            # json encoding keeps the triple unambiguous for any member text.
            "row_id": json.dumps([group, member, stamp], ensure_ascii=False),
            "revision": 1,
            "values": {"group_no": group, "member_no": member, "stamp_no": stamp},
        }

    left_doc = {
        "file_id": left_file_id,
        "writer_version": left_writer.version,
        "reader_version": left_reader.version,
        "rows": [row(g, m, s) for g, m, s in plan.left_rows],
    }
    right_doc = {
        "file_id": right_file_id,
        "writer_version": right_writer.version,
        "reader_version": right_reader.version,
        "rows": [row(g, m, s) for g, m, s in plan.right_rows],
    }
    return left_doc, right_doc


def _decode_issue_rows(records: dict, reader: Contract, side: str) -> list[tuple]:
    """Decode a branch's reader-records/v1 file back to (group, member, stamp)."""
    if records.get("format") != "reader-records/v1":
        raise ContractError(f"{side} file: not reader-records/v1")
    if records.get("reader_version") != reader.version:
        raise ContractError(
            f"{side} file declares reader {records.get('reader_version')!r}, "
            f"expected {reader.version!r}"
        )
    field_types = {f.field_id: f.type for f in reader.fields}
    rows = []
    for raw in records.get("rows", []):
        values = raw.get("values", {})
        decoded = {}
        for field_id in ("group_no", "member_no", "stamp_no"):
            t = field_types.get(field_id)
            if t is None:
                raise ContractError(f"{side} reader lacks field {field_id!r}")
            if field_id not in values:
                raise ContractError(
                    f"{side} file row {raw.get('row_id')!r} lacks {field_id!r}"
                )
            encoded = values[field_id]
            if isinstance(t, TextType):
                decoded[field_id] = encoded
            elif isinstance(t, IntType):
                number = Fraction(encoded)
                if number.denominator != 1:
                    raise ContractError(
                        f"{side} file: {field_id} value {encoded!r} is not an integer"
                    )
                decoded[field_id] = number.numerator
            else:
                decoded[field_id] = Fraction(encoded)
        rows.append((decoded["group_no"], decoded["member_no"], decoded["stamp_no"]))
    return rows


def verify_issued_files(
    left_records: dict,
    right_records: dict,
    left_reader: Contract,
    right_reader: Contract,
    relation: Any,
    *,
    left_slots: Any = None,
    right_slots: Any = None,
    total_stamps: Any = None,
) -> dict:
    """Witness over the two emitted reader-records/v1 documents themselves."""
    left_rows = _decode_issue_rows(left_records, left_reader, _SIDE_LEFT)
    right_rows = _decode_issue_rows(right_records, right_reader, _SIDE_RIGHT)
    witness = verify_rejoin(
        left_rows,
        right_rows,
        relation,
        left_slots=left_slots,
        right_slots=right_slots,
        total_stamps=total_stamps,
    )
    witness["left_row_count"] = len(left_rows)
    witness["right_row_count"] = len(right_rows)
    return witness
