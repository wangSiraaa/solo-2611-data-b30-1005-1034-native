"""Assemble a :class:`~extcontract.bike.domain.Scenario` from a frozen
handover ledger plus the ingested reservations, consents and street graph.

Only bikes the ledger proves dawn-lendable are placed in the ride pool.
Docks held by unconfirmed entries reduce the usable station capacity, so the
planner can never park a returned bike into a pending placeholder.
"""

from __future__ import annotations

from .domain import Scenario, StreetEdge
from .reconcile import FrozenLedger


def build_scenario(
    ledger: FrozenLedger,
    reservations,
    consents: dict,
    edges: list[StreetEdge],
    truck_start: str,
    truck_capacity: int,
    horizon: int,
) -> Scenario:
    opening_bikes: dict[str, str] = {}
    for bike in ledger.bikes:
        if bike.lendable_at_dawn and bike.physical_location is not None:
            opening_bikes[bike.bike_id] = bike.physical_location

    capacity = {st.station: st.capacity for st in ledger.stations}
    stations = tuple(st.station for st in ledger.stations)

    return Scenario(
        horizon=horizon,
        stations=stations,
        capacity=capacity,
        opening_bikes=opening_bikes,
        ready_bikes=frozenset(opening_bikes),
        pending_docks=ledger.pending_dock_keys(),
        reservations=tuple(reservations),
        consents=dict(consents),
        edges=tuple(edges),
        truck_start=truck_start,
        truck_capacity=truck_capacity,
    )
