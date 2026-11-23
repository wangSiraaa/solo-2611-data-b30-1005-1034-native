"""Optimality cross-check against an independent brute-force simulator.

The brute force below is deliberately written as a separate explicit state
trajectory enumerator; it shares no code with :class:`RidePlanner`.  Matching
on random bounded scenarios gives confidence the planner's pruning still
returns the true (minimum fee, then minimum reward) optimum.
"""

from __future__ import annotations

import itertools
import random

from extcontract.bike.domain import Reservation, Scenario, StreetEdge
from extcontract.bike.ride import RidePlanner


def _powerset(xs):
    out = [()]
    for x in xs:
        out += [a + (x,) for a in out]
    return out


def _brute_force(scn):
    edges: dict[str, dict[str, int]] = {}
    for e in scn.edges:
        edges.setdefault(e.a, {})[e.b] = e.cost
        edges.setdefault(e.b, {})[e.a] = e.cost
    res = sorted(scn.reservations, key=lambda r: r.pickup_tick)
    opts = []
    for r in res:
        c = scn.consents.get(r.ride_no)
        if c and c.alt_station != r.return_station:
            opts.append(
                [(r.return_station, 0, False), (c.alt_station, c.points, True)]
            )
        else:
            opts.append([(r.return_station, 0, False)])
    best = None
    usable = {
        st: scn.capacity[st] - len([p for p in scn.pending_docks if p[0] == st])
        for st in scn.stations
    }

    def assignments(pk, loc, riding):
        if not pk:
            return [()]

        def rec(i, used):
            if i == len(pk):
                return [tuple(used)]
            r = pk[i]
            out = []
            for b, w in loc.items():
                if (
                    w == r.pickup_station
                    and b in scn.ready_bikes
                    and b not in used
                    and b not in riding
                ):
                    used.append(b)
                    out += rec(i + 1, used)
                    used.pop()
            return out

        return rec(0, [])

    for combo in itertools.product(*opts):
        reward = sum(x[1] for x in combo)
        ret_station = {r.ride_no: combo[i][0] for i, r in enumerate(res)}

        def rec(tick, loc, tloc, tload, fee, picked, done):
            nonlocal best
            if tick > scn.horizon:
                if len(done) == len(res):
                    key = (fee, reward)
                    if best is None or key < best:
                        best = key
                return
            loc = dict(loc)
            docked = {st: 0 for st in scn.stations}
            riding = set()
            for b, w in loc.items():
                if isinstance(w, tuple):
                    riding.add(b)
                elif w != "truck":
                    docked[w] += 1
            rets = [
                r
                for r in res
                if r.return_tick == tick and r.ride_no in picked and r.ride_no not in done
            ]
            need: dict[str, int] = {}
            for r in rets:
                st = ret_station[r.ride_no]
                need[st] = need.get(st, 0) + 1
            if any(docked[st] + n > usable[st] for st, n in need.items()):
                return
            for r in rets:
                b = picked[r.ride_no]
                loc[b] = ret_station[r.ride_no]
                riding.discard(b)
            done2 = done | {r.ride_no for r in rets}
            here = [b for b, w in loc.items() if w == tloc and b in scn.ready_bikes]
            for load in _powerset(tuple(here)):
                for unload in _powerset(tuple(sorted(tload))):
                    nt = (set(tload) - set(unload)) | set(load)
                    if len(nt) > scn.truck_capacity:
                        continue
                    if docked[tloc] - len(load) + len(unload) > usable[tloc]:
                        continue
                    l2 = dict(loc)
                    for b in unload:
                        l2[b] = tloc
                    for b in load:
                        l2[b] = "truck"
                    pk = [
                        r
                        for r in res
                        if r.pickup_tick == tick and r.ride_no not in picked
                    ]
                    for bikes in assignments(pk, l2, riding):
                        l3, picked2, ok = dict(l2), dict(picked), True
                        for r, b in zip(pk, bikes):
                            if l3.get(b) != r.pickup_station:
                                ok = False
                                break
                            l3[b] = ("ride", r.ride_no)
                            picked2[r.ride_no] = b
                        if not ok:
                            continue
                        for nxt, cost in [(tloc, 0)] + list(edges.get(tloc, {}).items()):
                            rec(
                                tick + 1,
                                l3,
                                nxt,
                                frozenset(nt),
                                fee + cost,
                                picked2,
                                done2,
                            )

        rec(0, dict(scn.opening_bikes), scn.truck_start, frozenset(), 0, {}, set())
    return best


def _random_scenario(rng):
    nb = rng.randint(1, 3)
    bikes = [f"B{i}" for i in range(nb)]
    loc = {b: rng.choice(["A", "B"]) for b in bikes}
    nr = rng.randint(1, 3)
    res = []
    for i in range(nr):
        pt = rng.randint(0, 2)
        rt = pt + rng.randint(1, 2)
        res.append(
            Reservation(
                f"r{i}", f"d{i}", pt,
                rng.choice(["A", "B"]), rt, rng.choice(["A", "B"]),
            )
        )
    consents = {}
    if res and rng.random() < 0.6:
        r = res[0]
        alt = "B" if r.return_station == "A" else "A"
        consents[r.ride_no] = type(
            "C", (), {"ride_no": r.ride_no, "points": 2, "alt_station": alt}
        )()
    return Scenario(
        horizon=6,
        stations=("A", "B"),
        capacity={"A": nb + 2, "B": nb + 2},
        opening_bikes=loc,
        ready_bikes=frozenset(bikes),
        pending_docks=frozenset(),
        reservations=tuple(res),
        consents=consents,
        edges=(StreetEdge("A", "B", 3),),
        truck_start="A",
        truck_capacity=1,
    )


def test_planner_matches_independent_brute_force():
    rng = random.Random(7)
    for _ in range(12):
        scn = _random_scenario(rng)
        plan = RidePlanner(scn).best_plan(reward_cap=2)
        brute = _brute_force(scn)
        if plan.feasible:
            assert brute == (plan.transport_cost, plan.reward_points)
        else:
            assert brute is None
