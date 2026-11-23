"""Bike-show overnight handover and next-day group-ride support, built on the
existing exact-contract ingestion toolkit.

Layers:

* :mod:`extcontract.bike.ingest_files` loads the real original station,
  per-bike, master, consent and reservation files through
  :class:`extcontract.IngestionLibrary` (atomic, revision-aware).
* :mod:`extcontract.bike.reconcile` pairs the two sides of every handover and
  freezes a dawn ledger; a corrected receipt rebuilds a second frozen ledger.
* :mod:`extcontract.bike.ride` exhaustively plans reservations, reward
  re-docking and the single transport truck under the strict stage ordering.
"""

from .domain import (
    BikeState,
    Consent,
    EventKind,
    Fulfillment,
    HandoverEvent,
    LoadUnload,
    OpeningDock,
    Plan,
    Reservation,
    Scenario,
    StreetEdge,
    TruckMove,
)
from .ingest_files import HandoverFiles
from .reconcile import BikeVerdict, FrozenLedger, StationLedgerEntry, reconcile
from .ride import RidePlanner
from .scenario import build_scenario

__all__ = [
    "BikeState",
    "EventKind",
    "Consent",
    "Reservation",
    "StreetEdge",
    "HandoverEvent",
    "OpeningDock",
    "Scenario",
    "Plan",
    "Fulfillment",
    "TruckMove",
    "LoadUnload",
    "HandoverFiles",
    "BikeVerdict",
    "FrozenLedger",
    "StationLedgerEntry",
    "reconcile",
    "RidePlanner",
    "build_scenario",
]
