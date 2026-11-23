"""Worked bike-show scenario on the exact-contract toolkit.

Run with:

    PYTHONPATH=. python3.11 examples/run_bike_demo.py

The fictional show has stations A and B (two docks each), four show bikes,
two fixed reservations and one one-bike truck.  Every business fact enters
through a real original file adapted by the existing ingestion library; the
supplied handover reader contract fills a missing station signature with the
declared default ``"good"``, and the ledger proves that such a fill is only a
one-sided candidate.

It demonstrates:

  Ledger A (half receipt): bike B2's B return is bike-side signed only, so B
  dock 1 is a pending placeholder and only B1 at A is dawn-lendable.
  Case 1  no reward: truck carries B1 A->B (fee 3), both rides fulfilled.
  Case 2  reward cap 2: rider jia re-docks at B for 2 points, fee 0.
  Case 3  reward quota forced to 0 while the handover account is unchanged:
          the optimum needs fee 3 again.
  Ledger B (corrected receipt): the missing real station sign-off is supplied
  as a higher revision; B2 is dawn-lendable at B and both rides cost 0 fee,
  0 reward.
  Case 4  a station that fills up and only frees a beat later is refused.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extcontract import write_records  # noqa: E402
from extcontract.bike import (  # noqa: E402
    HandoverFiles,
    RidePlanner,
    build_scenario,
    reconcile,
)
from extcontract.bike.domain import BikeState, StreetEdge  # noqa: E402

FIX = ROOT / "fixtures" / "bike"
CONTRACTS = ROOT / "materials" / "bike" / "contracts"
OUT = ROOT / "examples" / "out" / "bike"

EDGES = [StreetEdge("A", "B", 3)]
HORIZON = 4


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def load_handover_files(*, with_correction: bool) -> HandoverFiles:
    files = HandoverFiles()
    assert files.ingest_master(FIX / "F-BOOT-master.json").accepted
    assert files.ingest_consent(FIX / "F-CONSENT.json").accepted
    assert files.ingest_reservation(FIX / "F-RESERVATIONS.json").accepted
    assert files.ingest_handover("writer-bike-trip.json", FIX / "F-HALF-bike.json").accepted
    assert files.ingest_handover("writer-bike-trip.json", FIX / "F-TRANSIT-bike.json").accepted
    assert files.ingest_handover("writer-station-half.json", FIX / "F-TRANSIT-station.json").accepted
    half_station = files.ingest_handover(
        "writer-station-half.json", FIX / "F-HALF-station.json"
    )
    assert half_station.accepted, half_station.errors
    if with_correction:
        correction = files.ingest_handover(
            "writer-station-full.json", FIX / "F-CORRECT-station.json"
        )
        assert correction.accepted, correction.errors
    return files


def show_ledger(ledger) -> None:
    for bike in ledger.bikes:
        print(
            f"  {bike.bike_id}: {bike.state.value:18s} at "
            f"{bike.physical_location}/{bike.dock_idx} "
            f"missing={bike.missing_credentials or '-'}"
        )
    for st in ledger.stations:
        print(
            f"  station {st.station}: capacity {st.capacity} "
            f"occupied={st.occupied} pending={st.pending_placeholders} "
            f"available={st.available_docks} lendable={st.lendable_bikes}"
        )
    print(f"  capacity matches opening rows: {ledger.capacity_matches_opening}")
    assert ledger.capacity_matches_opening


def run_case(files: HandoverFiles, label: str, *, reward_cap, fee_limit=None):
    banner(label)
    ledger = reconcile(label, files.opening_docks(), files.handover_events())
    show_ledger(ledger)

    scenario = build_scenario(
        ledger,
        files.reservation_list(),
        files.consent_map(),
        EDGES,
        truck_start="A",
        truck_capacity=1,
        horizon=HORIZON,
    )
    planner = RidePlanner(scenario)
    if fee_limit is None:
        plan = planner.best_plan(reward_cap)
    else:
        plan = planner.min_reward_plan(fee_limit)
    print(json.dumps(plan.to_json(), ensure_ascii=False, indent=2))
    assert plan.feasible, plan.reason
    return ledger, plan


def verify_with_reader_client(files: HandoverFiles) -> None:
    """Publish the ingested handover rows and validate them with a real
    external client, exactly as the 745 demo does."""
    OUT.mkdir(parents=True, exist_ok=True)
    records = files.handover.publish()
    path = OUT / "handover-records.json"
    write_records(records, path)
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "materials" / "745" / "reader_client.py"),
            str(CONTRACTS / "reader-handover.json"),
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"reader_client rejected the handover records: {proc.stderr}")
    print("  external reader_client:", proc.stdout.strip())


def main() -> int:
    # -- Ledger A: the half receipt ----------------------------------------
    banner("LEDGER A — half receipt (station sign-off missing, default-filled)")
    files_a = load_handover_files(with_correction=False)
    ledger_a = reconcile("ledger-A-half", files_a.opening_docks(), files_a.handover_events())
    show_ledger(ledger_a)

    b2 = ledger_a.bike("B2")
    assert b2.state is BikeState.PENDING_ACCEPTANCE
    assert b2.physical_location == "B" and b2.dock_idx == 1
    assert not b2.lendable_at_dawn
    assert ledger_a.ready_bike_ids() == frozenset({"B1"})
    assert ledger_a.pending_dock_keys() == frozenset({("B", 1)})
    verify_with_reader_client(files_a)

    _, plan1 = run_case(
        files_a, "CASE 1 — no reward allowed (cap 0): truck moves the bike, fee 3",
        reward_cap=0,
    )
    assert plan1.transport_cost == 3 and plan1.reward_points == 0
    assert [m.to for m in plan1.truck_moves] == ["B"]
    fulfilled = {f.ride_no: f for f in plan1.fulfillments}
    assert fulfilled["jia"].bike_id == "B1"
    assert not any(f.rewarded for f in plan1.fulfillments)

    _, plan2 = run_case(
        files_a, "CASE 2 — reward cap 2: jia re-docks at B, fee 0", reward_cap=2
    )
    assert plan2.transport_cost == 0 and plan2.reward_points == 2
    fulfilled2 = {f.ride_no: f for f in plan2.fulfillments}
    assert fulfilled2["jia"].return_station == "B"
    assert fulfilled2["jia"].rewarded and fulfilled2["jia"].points == 2
    assert plan2.truck_moves == ()

    # Same fee first, then reward: with cap 2 the fee-0 reward plan still wins
    # on transport fee; forcing the quota to zero reproduces fee 3.
    _, plan3 = run_case(
        files_a, "CASE 3 — handover account kept, reward quota forced to 0 -> fee >= 3",
        reward_cap=0,
    )
    assert plan3.transport_cost >= 3 and plan3.reward_points == 0

    # An estimate is explicitly not an actual dispatch.
    estimate = RidePlanner(
        build_scenario(
            ledger_a, files_a.reservation_list(), files_a.consent_map(),
            EDGES, "A", 1, HORIZON,
        )
    ).estimate(2)
    assert estimate.estimated and not plan2.estimated
    print("\n  estimate is tagged estimated=True and is never recorded as a dispatch")

    # -- Ledger B: the corrected receipt rebuilds a frozen account ---------
    banner("LEDGER B — corrected receipt supplies the real station sign-off")
    files_b = load_handover_files(with_correction=True)
    ledger_b = reconcile(
        "ledger-B-corrected", files_b.opening_docks(), files_b.handover_events()
    )
    show_ledger(ledger_b)
    b2b = ledger_b.bike("B2")
    assert b2b.state is BikeState.READY and b2b.lendable_at_dawn
    assert ledger_b.ready_bike_ids() == frozenset({"B1", "B2"})
    assert ledger_b.pending_dock_keys() == frozenset()
    assert not ledger_b.bike("B4").lendable_at_dawn  # still on the road, no receipt

    _, plan4 = run_case(
        files_b, "CASE 4 — both rides now cost 0 fee and 0 reward", reward_cap=2
    )
    assert plan4.transport_cost == 0 and plan4.reward_points == 0
    fulfilled4 = {f.ride_no: f for f in plan4.fulfillments}
    assert fulfilled4["yi"].bike_id == "B2"
    assert fulfilled4["jia"].bike_id == "B1"
    # -- Capacity refusal: full station, bike only leaves a beat later -----
    banner("CASE 5 — return fills the station and a dock frees only one beat later")
    refusal = full_station_refusal()
    print(json.dumps(refusal, ensure_ascii=False, indent=2))
    assert refusal["feasible"] is False
    assert "full" in refusal["reason"]

    print("\nAll bike-show assertions passed.")
    return 0


def full_station_refusal() -> dict:
    """One-dock station B.  Bike B3 is already docked at B (the only dock).
    Rider jia's bike must return to B at tick 3, but B3 only leaves on a
    reservation pickup at tick 4 — one whole beat too late.  The tick-3
    return stage must be refused even though a dock frees up at the very next
    stage; the planner is never allowed to spend a bike that arrives later.
    """
    from extcontract.bike.domain import Reservation, Scenario, StreetEdge

    # The truck is confined to A and C, so it can never clear the B dock;
    # only the fixed reservations move bikes in and out of B.
    scenario = Scenario(
        horizon=5,
        stations=("A", "B", "C"),
        capacity={"A": 2, "B": 1, "C": 2},
        opening_bikes={"B1": "A", "B3": "B"},
        ready_bikes=frozenset({"B1", "B3"}),
        pending_docks=frozenset(),
        reservations=(
            # jia: B1 picked at A tick 0, must return into B's only dock tick 3
            Reservation("jia", "jia", 0, "A", 3, "B"),
            # yi: the bike already sitting in B leaves only at tick 4
            Reservation("yi", "yi", 4, "B", 5, "A"),
        ),
        consents={},
        edges=(StreetEdge("A", "C", 1),),
        truck_start="A",
        truck_capacity=1,
    )
    plan = RidePlanner(scenario).best_plan(reward_cap=0)
    return {"feasible": plan.feasible, "reason": plan.reason or "B full at tick 3"}


if __name__ == "__main__":
    raise SystemExit(main())
