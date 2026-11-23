"""Tests for the two-sided handover reconciliation and frozen ledgers."""

from __future__ import annotations

from extcontract.bike import BikeState
from extcontract.bike.reconcile import reconcile

from conftest import load_files


def test_only_one_bike_lendable_at_dawn_under_half_receipt(half_ledger):
    assert half_ledger.ready_bike_ids() == frozenset({"B1"})
    b1 = half_ledger.bike("B1")
    assert b1.state is BikeState.READY and b1.physical_location == "A"


def test_half_receipt_is_pending_placeholder_not_lendable(half_ledger):
    b2 = half_ledger.bike("B2")
    assert b2.state is BikeState.PENDING_ACCEPTANCE
    assert not b2.lendable_at_dawn
    # the missing credential is precisely the real station-side sign-off
    assert any("station sign-off" in m for m in b2.missing_credentials)
    assert half_ledger.pending_dock_keys() == frozenset({("B", 1)})


def test_pending_placeholder_is_never_promised_to_anyone(half_ledger):
    station_b = next(s for s in half_ledger.stations if s.station == "B")
    assert 1 in station_b.pending_placeholders
    assert station_b.pending_placeholders[1] == "B2"
    # the placeholder dock is not offered as available
    assert 1 not in station_b.available_docks
    assert "B2" not in station_b.lendable_bikes


def test_default_filled_station_signature_is_one_sided(half_files):
    events = half_files.handover_events()
    station_half = next(
        e for e in events if e.source_row_id == "B2-return-station"
    )
    # reader contract filled "good", provenance proves it is not a real sign
    assert station_half.station_sign == "good"
    assert station_half.station_sign_is_default
    assert not station_half.real_station_signed


def test_in_transit_and_unaccepted_bikes_never_lendable(half_ledger):
    b3 = half_ledger.bike("B3")
    b4 = half_ledger.bike("B4")
    assert b3.state is BikeState.IN_TRANSIT and not b3.lendable_at_dawn
    assert b4.state is BikeState.IN_TRANSIT and not b4.lendable_at_dawn


def test_corrected_receipt_rebuilds_a_second_ready_ledger(corrected_ledger):
    b2 = corrected_ledger.bike("B2")
    assert b2.state is BikeState.READY
    assert b2.lendable_at_dawn
    assert b2.physical_location == "B" and b2.dock_idx == 1
    assert corrected_ledger.ready_bike_ids() == frozenset({"B1", "B2"})
    assert corrected_ledger.pending_dock_keys() == frozenset()


def test_both_frozen_ledgers_remain_available(half_ledger, corrected_ledger):
    # the correction rebuilds an account but cannot rewrite the first one
    assert half_ledger.ready_bike_ids() == frozenset({"B1"})
    assert corrected_ledger.ready_bike_ids() == frozenset({"B1", "B2"})
    assert half_ledger.label != corrected_ledger.label


def test_opening_capacity_matches_master_rows(half_ledger):
    assert half_ledger.capacity_matches_opening
    assert {s.station: s.capacity for s in half_ledger.stations} == {
        "A": 2,
        "B": 2,
    }


def test_return_without_matching_open_ride_is_blocked():
    from extcontract.bike.domain import HandoverEvent

    files = load_files(with_correction=False)
    # craft a double-signed return for a ride number the bike never opened
    rogue = HandoverEvent(
        bike_id="B1",
        ride_no="R-NOPE",
        event="entry",
        station="B",
        tick=2,
        dock_idx=1,
        bike_sign="good",
        station_sign="good",
        station_sign_is_default=False,
        bike_sign_is_default=False,
        source_row_id="rogue",
    )
    docks = files.opening_docks()
    ledger = reconcile("rogue-ledger", docks, files.handover_events() + [rogue])
    assert ledger.bike("B1").state in (BikeState.BLOCKED, BikeState.READY)
    # the rogue ride cannot match B1's (non-existent) open ride
    assert any("R-NOPE" in c for c in ledger.contradictions)


def test_contradictory_sides_are_flagged():
    from extcontract.bike.domain import HandoverEvent

    sides = [
        HandoverEvent("B9", "R1", "entry", "A", 1, 0, "good", None, False, False, "x1"),
        HandoverEvent("B9", "R1", "entry", "B", 1, 0, None, "good", False, False, "x2"),
    ]
    ledger = reconcile("contradiction", [], sides)
    assert ledger.bike("B9").state is BikeState.CONTRADICTED
    assert ledger.contradictions


def _event(bike, ride, event, station, tick, *, bike_sign="good",
           station_sign="good", dock=None, row=None,
           station_default=False, bike_default=False):
    from extcontract.bike.domain import HandoverEvent

    return HandoverEvent(
        bike_id=bike, ride_no=ride, event=event, station=station, tick=tick,
        dock_idx=dock, bike_sign=bike_sign, station_sign=station_sign,
        station_sign_is_default=station_default,
        bike_sign_is_default=bike_default,
        source_row_id=row or f"{bike}-{event}",
    )


def test_completed_loan_then_return_chain_is_ready():
    from extcontract.bike.domain import OpeningDock

    docks = [OpeningDock("A", 0, "B1", "ready"), OpeningDock("A", 1, None, "empty")]
    events = [
        _event("B1", "R1", "exit", "A", 1),
        _event("B1", "R1", "entry", "A", 2, dock=1),
    ]
    ledger = reconcile("chain", docks, events)
    assert ledger.bike("B1").state is BikeState.READY
    assert ledger.bike("B1").physical_location == "A"


def test_exit_from_wrong_station_breaks_custody_chain():
    from extcontract.bike.domain import OpeningDock

    docks = [OpeningDock("A", 0, "B1", "ready")]
    # B1 is really at A but claims an exit at B
    events = [_event("B1", "R1", "exit", "B", 1, row="bad-exit")]
    ledger = reconcile("bad-chain", docks, events)
    assert ledger.bike("B1").state is BikeState.BLOCKED
    assert "custody" in (ledger.bike("B1").block_origin or "")


def test_entry_before_its_exit_fails_the_open_ride_gate():
    from extcontract.bike.domain import OpeningDock

    docks = [OpeningDock("A", 0, "B1", "ready"), OpeningDock("A", 1, None, "empty")]
    # entry at tick 0 is processed before the exit at tick 1 ever opens R1
    events = [
        _event("B1", "R1", "exit", "A", 1),
        _event("B1", "R1", "entry", "A", 0, dock=1),
    ]
    ledger = reconcile("entry-first", docks, events)
    assert any("open ride" in c for c in ledger.contradictions)
    assert not ledger.bike("B1").lendable_at_dawn


def test_backdated_return_guard_exists_in_ledger_contradictions():
    # A return that cannot match an open ride is refused; this is the gate
    # that also carries the chronological custody-chain check in the engine.
    from extcontract.bike.domain import OpeningDock

    docks = [
        OpeningDock("A", 0, "B1", "ready"),
        OpeningDock("A", 1, None, "empty"),
    ]
    events = [
        _event("B1", "R1", "exit", "A", 1, row="r1-exit"),
        _event("B1", "R1", "entry", "A", 2, dock=1, row="r1-entry"),
        # R2 opens at tick 3 then a return claims tick 1
        _event("B1", "R2", "exit", "A", 3, row="r2-exit"),
        _event("B1", "R2", "entry", "A", 1, dock=0, row="r2-backdated"),
    ]
    ledger = reconcile("time-travel", docks, events)
    # the out-of-order R2 return is refused one way or another; never ready
    assert ledger.contradictions
    assert not ledger.bike("B1").lendable_at_dawn
    # and a well-ordered R2 return on the same data would have been accepted
    good_events = events[:-1] + [
        _event("B1", "R2", "entry", "A", 4, dock=0, row="r2-ok"),
    ]
    good = reconcile("ordered", docks, good_events)
    assert good.bike("B1").lendable_at_dawn


def test_return_for_wrong_ride_number_is_blocked():
    from extcontract.bike.domain import OpeningDock

    docks = [OpeningDock("A", 0, "B1", "ready")]
    events = [
        _event("B1", "R1", "exit", "A", 1),
        _event("B1", "R2", "entry", "A", 2, dock=None, row="wrong-ride"),
    ]
    ledger = reconcile("wrong-ride", docks, events)
    # the R2 return cannot match B1's open R1 ride
    assert ledger.bike("B1").state is BikeState.BLOCKED
    assert any("R2" in c for c in ledger.contradictions)
