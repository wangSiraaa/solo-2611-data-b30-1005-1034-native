"""Shared fixtures and builders for the bike-show tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from extcontract.bike import HandoverFiles, build_scenario, reconcile
from extcontract.bike.domain import StreetEdge
from extcontract.bike.ride import RidePlanner

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "bike"
EDGES = [StreetEdge("A", "B", 3)]


def load_files(*, with_correction: bool) -> HandoverFiles:
    files = HandoverFiles()
    assert files.ingest_master(FIX / "F-BOOT-master.json").accepted
    assert files.ingest_consent(FIX / "F-CONSENT.json").accepted
    assert files.ingest_reservation(FIX / "F-RESERVATIONS.json").accepted
    assert files.ingest_handover(
        "writer-bike-trip.json", FIX / "F-HALF-bike.json"
    ).accepted
    assert files.ingest_handover(
        "writer-bike-trip.json", FIX / "F-TRANSIT-bike.json"
    ).accepted
    assert files.ingest_handover(
        "writer-station-half.json", FIX / "F-TRANSIT-station.json"
    ).accepted
    half = files.ingest_handover(
        "writer-station-half.json", FIX / "F-HALF-station.json"
    )
    assert half.accepted, half.errors
    if with_correction:
        correction = files.ingest_handover(
            "writer-station-full.json", FIX / "F-CORRECT-station.json"
        )
        assert correction.accepted, correction.errors
    return files


@pytest.fixture()
def half_files() -> HandoverFiles:
    return load_files(with_correction=False)


@pytest.fixture()
def corrected_files() -> HandoverFiles:
    return load_files(with_correction=True)


@pytest.fixture()
def half_ledger(half_files):
    return reconcile("half", half_files.opening_docks(), half_files.handover_events())


@pytest.fixture()
def corrected_ledger(corrected_files):
    return reconcile(
        "corrected", corrected_files.opening_docks(), corrected_files.handover_events()
    )


def make_planner(ledger, files, horizon: int = 4) -> RidePlanner:
    scenario = build_scenario(
        ledger,
        files.reservation_list(),
        files.consent_map(),
        EDGES,
        truck_start="A",
        truck_capacity=1,
        horizon=horizon,
    )
    return RidePlanner(scenario)
