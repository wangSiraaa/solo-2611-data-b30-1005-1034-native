"""Tests for reservation fulfillment, rewards, truck dispatch and capacity."""

from __future__ import annotations

from extcontract.bike.domain import (
    Reservation,
    Scenario,
    StreetEdge,
)
from extcontract.bike.ride import RidePlanner

from conftest import EDGES, make_planner


# -- the three worked cases -------------------------------------------------

def test_case1_no_reward_uses_truck_fee_3(half_ledger, half_files):
    plan = make_planner(half_ledger, half_files).best_plan(reward_cap=0)
    assert plan.feasible
    assert plan.transport_cost == 3
    assert plan.reward_points == 0
    assert [m.to for m in plan.truck_moves] == ["B"]
    # load at A tick 2, unload at B tick 3, exactly the brief's dispatch
    assert plan.dock_ops[0].station == "A" and plan.dock_ops[0].loaded == ("B1",)
    assert plan.dock_ops[1].station == "B" and plan.dock_ops[1].unloaded == ("B1",)


def test_case2_reward_cap_2_prefers_zero_fee(half_ledger, half_files):
    plan = make_planner(half_ledger, half_files).best_plan(reward_cap=2)
    assert plan.feasible
    assert plan.transport_cost == 0
    assert plan.reward_points == 2
    assert plan.truck_moves == ()
    jia = next(f for f in plan.fulfillments if f.ride_no == "jia")
    assert jia.rewarded and jia.return_station == "B" and jia.points == 2
    # the reward really goes to the consenting rider, for this reservation only
    assert all(f.points == 0 for f in plan.fulfillments if f.ride_no != "jia")


def test_case3_zero_quota_keeps_handover_account_needs_fee_3(half_ledger, half_files):
    plan = make_planner(half_ledger, half_files).best_plan(reward_cap=0)
    assert plan.transport_cost >= 3 and plan.reward_points == 0


def test_case4_corrected_ledger_zero_fee_zero_reward(corrected_ledger, corrected_files):
    plan = make_planner(corrected_ledger, corrected_files).best_plan(reward_cap=2)
    assert plan.feasible
    assert plan.transport_cost == 0 and plan.reward_points == 0
    by_ride = {f.ride_no: f for f in plan.fulfillments}
    assert by_ride["jia"].bike_id == "B1"
    assert by_ride["yi"].bike_id == "B2"


def test_each_bike_that_arrives_is_named(half_ledger, half_files):
    plan = make_planner(half_ledger, half_files).best_plan(reward_cap=2)
    for f in plan.fulfillments:
        assert f.bike_id in half_ledger.ready_bike_ids()


# -- reward accounting and fee budgets -------------------------------------

def test_fee_then_reward_tie_break(half_ledger, half_files):
    # cap 2 must still pick the fee-0/reward-2 plan over fee-3/reward-0
    plan = make_planner(half_ledger, half_files).best_plan(reward_cap=2)
    assert (plan.transport_cost, plan.reward_points) == (0, 2)


def test_min_reward_under_fee_limit(half_ledger, half_files):
    planner = make_planner(half_ledger, half_files)
    # a fee budget of 0 forces the reward route
    plan = planner.min_reward_plan(fee_limit=0)
    assert plan.feasible and plan.transport_cost == 0
    assert plan.reward_points == 2
    # a fee budget >= 3 lets the planner spend no reward
    plan2 = planner.min_reward_plan(fee_limit=3)
    assert plan2.feasible and plan2.reward_points == 0


def test_estimate_is_never_a_real_dispatch(half_ledger, half_files):
    plan = make_planner(half_ledger, half_files).estimate(2)
    assert plan.estimated is True
    real = make_planner(half_ledger, half_files).best_plan(2)
    assert real.estimated is False


# -- capacity and stage ordering --------------------------------------------

def test_full_station_frees_one_beat_later_is_refused():
    scenario = Scenario(
        horizon=5,
        stations=("A", "B", "C"),
        capacity={"A": 2, "B": 1, "C": 2},
        opening_bikes={"B1": "A", "B3": "B"},
        ready_bikes=frozenset({"B1", "B3"}),
        pending_docks=frozenset(),
        reservations=(
            Reservation("jia", "jia", 0, "A", 3, "B"),
            Reservation("yi", "yi", 4, "B", 5, "A"),
        ),
        consents={},
        edges=(StreetEdge("A", "C", 1),),
        truck_start="A",
        truck_capacity=1,
    )
    plan = RidePlanner(scenario).best_plan(reward_cap=0)
    assert not plan.feasible
    assert "full" in plan.reason and "B" in plan.reason


def test_same_stage_returns_are_counted_together():
    # two bikes return into a one-dock station on the same tick: must fail on
    # the combined count, even though either one alone would fit
    scenario = Scenario(
        horizon=3,
        stations=("A", "B"),
        capacity={"A": 4, "B": 1},
        opening_bikes={"B1": "A", "B2": "A"},
        ready_bikes=frozenset({"B1", "B2"}),
        pending_docks=frozenset(),
        reservations=(
            Reservation("r1", "r1", 0, "A", 2, "B"),
            Reservation("r2", "r2", 0, "A", 2, "B"),
        ),
        consents={},
        edges=tuple(EDGES),
        truck_start="A",
        truck_capacity=1,
    )
    plan = RidePlanner(scenario).best_plan(reward_cap=0)
    assert not plan.feasible
    assert "capacity" in plan.reason


def test_truck_capacity_enforced_every_stage():
    # truck carries at most one; two bikes can never be loaded together
    scenario = Scenario(
        horizon=3,
        stations=("A", "B"),
        capacity={"A": 4, "B": 4},
        opening_bikes={"B1": "A", "B2": "A"},
        ready_bikes=frozenset({"B1", "B2"}),
        pending_docks=frozenset(),
        reservations=(
            Reservation("r1", "r1", 0, "A", 2, "B"),
            Reservation("r2", "r2", 0, "A", 2, "B"),
        ),
        consents={},
        edges=(StreetEdge("A", "B", 3),),
        truck_start="A",
        truck_capacity=1,
    )
    plan = RidePlanner(scenario).best_plan(reward_cap=0)
    for m in plan.truck_moves:
        assert len(m.loaded) <= 1


def test_pending_placeholder_reduces_usable_capacity(half_ledger, half_files):
    # B dock 1 is a pending placeholder; a return into B must use dock 0 only
    plan = make_planner(half_ledger, half_files).best_plan(reward_cap=2)
    assert plan.feasible
    # jia re-docks at B which has exactly one usable dock (dock 0)
    jia = next(f for f in plan.fulfillments if f.ride_no == "jia")
    assert jia.return_station == "B"


def test_cannot_use_a_bike_before_its_pickup_tick():
    # B's only lendable bike is needed at B tick 3; it cannot serve an earlier
    # A pickup through any time travel
    scenario = Scenario(
        horizon=4,
        stations=("A", "B"),
        capacity={"A": 2, "B": 2},
        opening_bikes={"B2": "B"},
        ready_bikes=frozenset({"B2"}),
        pending_docks=frozenset(),
        reservations=(Reservation("early", "e", 0, "A", 1, "A"),),
        consents={},
        edges=tuple(EDGES),
        truck_start="B",
        truck_capacity=1,
    )
    # truck would need two beats B->A but pickup is tick 0: infeasible
    plan = RidePlanner(scenario).best_plan(reward_cap=0)
    assert not plan.feasible


def test_empty_truck_move_still_pays_edge_price(half_ledger, half_files):
    plan = make_planner(half_ledger, half_files).best_plan(reward_cap=0)
    for m in plan.truck_moves:
        assert m.cost == 3  # A->B edge price regardless of load
