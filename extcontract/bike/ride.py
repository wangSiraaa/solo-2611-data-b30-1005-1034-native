"""Fixed one-day reservations, optional reward re-docking, and the truck.

Timing at every tick is fixed by the brief and enforced in this exact order:

    1. normal reservation returns (all returns at the tick are counted
       together against dock capacity);
    2. transport loading / unloading at the truck's current station (the
       whole stage is checked against truck and station capacity);
    3. reservation pickups (a bike that only frees up later in the day can
       never be used here).

The truck traverses at most one street edge per beat; every traversal pays
the edge price, empty or loaded alike.  Only dawn-READY bikes ever enter the
ride pool — pending, in-transit, unaccepted or contradicted bikes do not
exist as far as the planner is concerned.

The search is exhaustive over the tiny bounded space (<=3 stations, 4 bikes,
4 reservations, 8 ticks, one truck): reward choices, dock operations, edge
moves and bike-to-reservation assignments are all enumerated, so the minimum
transport fee (and the minimum reward on ties / under a fee budget) is a
proved optimum rather than a heuristic.  Each DFS node is an immutable state,
so there is no backtracking bookkeeping to get wrong.  Plans built by
:meth:`estimate` are tagged ``estimated`` and are never recorded as real
dispatches.
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import (
    Fulfillment,
    LoadUnload,
    Plan,
    Reservation,
    Scenario,
    TruckMove,
)

TRUCK = "truck"


@dataclass(frozen=True)
class _State:
    tick: int
    truck_loc: str
    truck_bikes: frozenset[str]
    # bike_id -> station name (docked) or TRUCK (carried); riding bikes are
    # mapped to the pickup station key with a separate riding set
    location: dict[str, str]
    riding: frozenset[str]
    docked_count: dict[str, int]
    done: frozenset[str]
    fee: int
    moves: tuple[TruckMove, ...]
    ops: tuple[LoadUnload, ...]
    fulfillments: tuple[Fulfillment, ...]


@dataclass
class _Ride:
    reservation: Reservation
    return_station: str
    rewarded: bool
    points: int
    bike_id: str = ""

    def key(self) -> str:
        return self.reservation.ride_no


class RidePlanner:
    def __init__(self, scenario: Scenario):
        self.s = scenario
        self.adj = self._adjacency()

    def _adjacency(self) -> dict[str, dict[str, int]]:
        adj: dict[str, dict[str, int]] = {st: {} for st in self.s.stations}
        for e in self.s.edges:
            adj.setdefault(e.a, {})[e.b] = e.cost
            adj.setdefault(e.b, {})[e.a] = e.cost
        return adj

    # -- public entry points ------------------------------------------------

    def best_plan(self, reward_cap: int | None) -> Plan:
        """Cheapest transport fee with reward total <= cap; ties break on
        fewer reward points."""
        cap = self.s.reward_cap if reward_cap is None else reward_cap
        return self._search(cap=cap, fee_limit=None, primary="fee", estimate=False)

    def min_reward_plan(self, fee_limit: int) -> Plan:
        """Under a transport-fee limit, spend as few reward points as possible."""
        return self._search(cap=None, fee_limit=fee_limit, primary="reward", estimate=False)

    def estimate(self, reward_cap: int | None) -> Plan:
        """An estimated plan: never to be recorded as an actual dispatch."""
        return self._search(cap=reward_cap, fee_limit=None, primary="fee", estimate=True)

    # -- exhaustive search --------------------------------------------------

    def _search(self, *, cap, fee_limit, primary: str, estimate: bool) -> Plan:
        s = self.s
        reservations = sorted(
            s.reservations, key=lambda r: (r.pickup_tick, r.ride_no)
        )

        options: list[list[tuple[str, int, bool]]] = []
        for r in reservations:
            consent = s.consents.get(r.ride_no)
            if consent is None or consent.alt_station == r.return_station:
                options.append([(r.return_station, 0, False)])
            else:
                options.append(
                    [
                        (r.return_station, 0, False),
                        (consent.alt_station, consent.points, True),
                    ]
                )

        best: Plan | None = None
        for combo in self._combos(options):
            reward_total = sum(c[1] for c in combo)
            if cap is not None and reward_total > cap:
                continue
            rides = [
                _Ride(
                    reservation=r,
                    return_station=combo[i][0],
                    rewarded=combo[i][2],
                    points=combo[i][1],
                )
                for i, r in enumerate(reservations)
            ]
            plan = self._solve_combo(rides, reward_total)
            if plan is None or not plan.feasible:
                continue
            if fee_limit is not None and plan.transport_cost > fee_limit:
                continue
            best = self._prefer(best, plan, primary)

        if best is None:
            reason = getattr(self, "_infeasible_reason", None) or (
                "no feasible dispatch satisfies every reservation under the limits"
            )
            return Plan(
                feasible=False,
                transport_cost=0,
                reward_points=0,
                fulfillments=(),
                truck_moves=(),
                dock_ops=(),
                reason=reason,
                estimated=estimate,
            )
        return Plan(**{**best.__dict__, "estimated": estimate})

    @staticmethod
    def _combos(options):
        def rec(i, acc):
            if i == len(options):
                yield tuple(acc)
                return
            for choice in options[i]:
                acc.append(choice)
                yield from rec(i + 1, acc)
                acc.pop()

        yield from rec(0, [])

    @staticmethod
    def _prefer(best: Plan | None, cand: Plan, primary: str) -> Plan:
        if best is None:
            return cand
        if primary == "fee":
            kb = (best.transport_cost, best.reward_points)
            kc = (cand.transport_cost, cand.reward_points)
        else:
            kb = (best.reward_points, best.transport_cost)
            kc = (cand.reward_points, cand.transport_cost)
        return cand if kc < kb else best

    def _solve_combo(self, rides: list[_Ride], reward_total: int) -> Plan | None:
        s = self.s
        usable = {
            st: s.capacity[st] - len([p for p in s.pending_docks if p[0] == st])
            for st in s.stations
        }
        location = dict(s.opening_bikes)
        docked = {st: 0 for st in s.stations}
        for st in s.opening_bikes.values():
            docked[st] += 1

        due_pickup: dict[int, list[_Ride]] = {}
        due_return: dict[int, list[_Ride]] = {}
        for rd in rides:
            due_pickup.setdefault(rd.reservation.pickup_tick, []).append(rd)
            due_return.setdefault(rd.reservation.return_tick, []).append(rd)

        start = _State(
            tick=0,
            truck_loc=s.truck_start,
            truck_bikes=frozenset(),
            location=location,
            riding=frozenset(),
            docked_count=docked,
            done=frozenset(),
            fee=0,
            moves=(),
            ops=(),
            fulfillments=(),
        )
        self._solve_combo_bind(rides, due_pickup, due_return, usable)
        self._blockers: list[str] = []
        best: Plan | None = None
        for final_state in self._dfs(start, rides, due_pickup, due_return, usable):
            plan = Plan(
                feasible=True,
                transport_cost=final_state.fee,
                reward_points=reward_total,
                fulfillments=final_state.fulfillments,
                truck_moves=final_state.moves,
                dock_ops=final_state.ops,
            )
            if best is None or (plan.transport_cost, plan.reward_points) < (
                best.transport_cost,
                best.reward_points,
            ):
                best = plan
        if best is None and self._blockers:
            # Surface the earliest concrete refusal for the final report.
            self._infeasible_reason = self._blockers[0]
        return best

    # -- DFS over ticks: returns -> dock ops -> pickups -> edge move -------

    def _dfs(self, state: _State, rides, due_pickup, due_return, usable):
        if state.tick > self.s.horizon:
            if len(state.done) == len(rides):
                yield state
            return

        loc = dict(state.location)
        docked = dict(state.docked_count)

        # ---- stage 1: reservation returns (counted together) --------------
        returns = due_return.get(state.tick, [])
        need: dict[str, int] = {}
        for rd in returns:
            if rd.key() not in state.done and rd.bike_id == "":
                return
            need[rd.return_station] = need.get(rd.return_station, 0) + 1
        for st, n in need.items():
            if docked[st] + n > usable[st]:
                self._blockers.append(
                    f"tick {state.tick}: station {st} would hold "
                    f"{docked[st] + n} bikes against usable capacity {usable[st]} "
                    f"(full station; no dock frees at this stage)"
                )
                return  # full station; no later stage this tick frees a dock
        new_fulfillments = list(state.fulfillments)
        newly_done: set[str] = set()
        riding = set(state.riding)
        for rd in returns:
            loc[rd.bike_id] = rd.return_station
            docked[rd.return_station] += 1
            riding.discard(rd.bike_id)
            newly_done.add(rd.key())
            new_fulfillments.append(
                Fulfillment(
                    ride_no=rd.reservation.ride_no,
                    bike_id=rd.bike_id,
                    pickup_tick=rd.reservation.pickup_tick,
                    pickup_station=rd.reservation.pickup_station,
                    return_tick=rd.reservation.return_tick,
                    return_station=rd.return_station,
                    rewarded=rd.rewarded,
                    points=rd.points,
                )
            )
        after_returns = _State(
            tick=state.tick,
            truck_loc=state.truck_loc,
            truck_bikes=state.truck_bikes,
            location=loc,
            riding=frozenset(riding),
            docked_count=docked,
            done=state.done | frozenset(newly_done),
            fee=state.fee,
            moves=state.moves,
            ops=state.ops,
            fulfillments=tuple(new_fulfillments),
        )
        yield from self._stage_dock(after_returns, rides, due_pickup, usable)

    def _stage_dock(self, state: _State, rides, due_pickup, usable):
        station = state.truck_loc
        docked_here = sorted(
            b
            for b, st in state.location.items()
            if st == station and b not in state.riding and b in self.s.ready_bikes
        )
        for load_set, unload_set in self._dock_choices(state.truck_bikes, docked_here):
            load, unload = frozenset(load_set), frozenset(unload_set)
            new_truck = (state.truck_bikes - unload) | load
            if len(new_truck) > self.s.truck_capacity:
                continue
            new_docked_here = (
                state.docked_count[station] - len(load) + len(unload)
            )
            if new_docked_here > usable[station]:
                continue
            loc = dict(state.location)
            docked = dict(state.docked_count)
            for b in unload:
                loc[b] = station
            for b in load:
                loc[b] = TRUCK
            docked[station] = new_docked_here
            ops = state.ops
            if load or unload:
                ops = ops + (
                    LoadUnload(
                        tick=state.tick,
                        station=station,
                        loaded=tuple(sorted(load)),
                        unloaded=tuple(sorted(unload)),
                    ),
                )
            after_dock = _State(
                tick=state.tick,
                truck_loc=station,
                truck_bikes=frozenset(new_truck),
                location=loc,
                riding=state.riding,
                docked_count=docked,
                done=state.done,
                fee=state.fee,
                moves=state.moves,
                ops=ops,
                fulfillments=state.fulfillments,
            )
            yield from self._stage_pickup(after_dock, rides, due_pickup, usable)

    def _stage_pickup(self, state: _State, rides, due_pickup, usable):
        pickups = due_pickup.get(state.tick, [])
        if not pickups:
            yield from self._stage_move(state)
            return
        for assignment in self._assignments(pickups, state.location, state.riding):
            loc = dict(state.location)
            docked = dict(state.docked_count)
            riding = set(state.riding)
            for rd, bike in assignment:
                rd.bike_id = bike
                riding.add(bike)
                docked[rd.reservation.pickup_station] -= 1
            after_pickup = _State(
                tick=state.tick,
                truck_loc=state.truck_loc,
                truck_bikes=state.truck_bikes,
                location=loc,
                riding=frozenset(riding),
                docked_count=docked,
                done=state.done,
                fee=state.fee,
                moves=state.moves,
                ops=state.ops,
                fulfillments=state.fulfillments,
            )
            yield from self._stage_move(after_pickup)
            for rd, _bike in assignment:
                rd.bike_id = ""

    def _stage_move(self, state: _State):
        # stay for the beat (no fee), or traverse exactly one priced edge
        yield from self._advance(state, state.truck_loc, 0)
        for nxt, cost in self.adj.get(state.truck_loc, {}).items():
            yield from self._advance(state, nxt, cost)

    def _advance(self, state: _State, nxt: str, cost: int):
        moves = state.moves
        if nxt != state.truck_loc:
            moves = moves + (
                TruckMove(
                    tick=state.tick,
                    frm=state.truck_loc,
                    to=nxt,
                    cost=cost,
                    loaded=tuple(sorted(state.truck_bikes)),
                ),
            )
        nxt_state = _State(
            tick=state.tick + 1,
            truck_loc=nxt,
            truck_bikes=state.truck_bikes,
            location=dict(state.location),
            riding=state.riding,
            docked_count=dict(state.docked_count),
            done=state.done,
            fee=state.fee + cost,
            moves=moves,
            ops=state.ops,
            fulfillments=state.fulfillments,
        )
        yield from self._dfs(
            nxt_state,
            self._rides,
            self._due_pickup,
            self._due_return,
            self._usable,
        )

    # -- choice helpers -----------------------------------------------------

    def _dock_choices(self, truck_bikes: frozenset, docked_here: list[str]):
        choices = []
        for unload in self._powerset(tuple(sorted(truck_bikes))):
            for load in self._powerset(tuple(docked_here)):
                choices.append((load, unload))
        return choices

    def _assignments(self, pickups, location, riding):
        def rec(i, used):
            if i == len(pickups):
                yield tuple(zip(pickups, used))
                return
            rd = pickups[i]
            st = rd.reservation.pickup_station
            candidates = sorted(
                b
                for b, where in location.items()
                if where == st and b not in riding and b in self.s.ready_bikes
            )
            for bike in candidates:
                if bike in used:
                    continue
                used.append(bike)
                yield from rec(i + 1, used)
                used.pop()

        return list(rec(0, []))

    @staticmethod
    def _powerset(items):
        out = [()]
        for x in items:
            out += [acc + (x,) for acc in out]
        return out

    # combo context used by the generator pipeline across ticks
    _rides: list = []
    _due_pickup: dict = {}
    _due_return: dict = {}
    _usable: dict = {}
    _blockers: list = []
    _infeasible_reason: str | None = None

    def _solve_combo_bind(self, rides, due_pickup, due_return, usable):
        self._rides = rides
        self._due_pickup = due_pickup
        self._due_return = due_return
        self._usable = usable
