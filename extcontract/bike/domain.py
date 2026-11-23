"""Domain objects for the fictional bike-show overnight handover and next-day
group ride.

The domain deliberately keeps the two concerns of the brief apart:

* :mod:`extcontract.bike.reconcile` decides which physical bikes are really
  available at dawn from the *ingested* original files (station docking log,
  per-bike trip log, opening station master).  Nothing here invents a sign-off.
* :mod:`extcontract.bike.ride` plans the fixed one-day reservations, the
  optional reward re-docking, and the single transport truck.

All objects are plain frozen dataclasses so they hash cleanly inside the
exhaustive planner.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BikeState(str, Enum):
    """Authoritative dawn classification of a physical bike."""

    READY = "ready"
    # A real, double-signed handover placed the bike at a dock.
    IN_TRANSIT = "in_transit"
    # One side only, never accepted, or the open ride was never closed.
    PENDING_ACCEPTANCE = "pending_acceptance"
    # Physically present behind a one-sided receipt: holds a dock as a
    # pending placeholder, never lendable at dawn.
    CONTRADICTED = "contradicted"
    # The two sides disagree about the same (bike, ride, event).
    BLOCKED = "blocked"
    # Not lendable because an earlier custody rule failed.


class EventKind(str, Enum):
    EXIT = "exit"
    ENTRY = "entry"


@dataclass(frozen=True)
class HandoverEvent:
    """One adapted reader row, with the provenance needed to tell a real
    sign-off apart from a reader-contract default fill."""

    bike_id: str
    ride_no: str
    event: str
    station: str
    tick: int
    dock_idx: int | None
    bike_sign: str | None
    station_sign: str | None
    station_sign_is_default: bool
    bike_sign_is_default: bool
    source_row_id: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.bike_id, self.ride_no, self.event)

    @property
    def real_bike_signed(self) -> bool:
        return self.bike_sign is not None and not self.bike_sign_is_default

    @property
    def real_station_signed(self) -> bool:
        return self.station_sign is not None and not self.station_sign_is_default


@dataclass(frozen=True)
class OpeningDock:
    station: str
    dock_idx: int
    bike_id: str | None
    status: str


@dataclass(frozen=True)
class Reservation:
    ride_no: str
    rider: str
    pickup_tick: int
    pickup_station: str
    return_tick: int
    return_station: str


@dataclass(frozen=True)
class Consent:
    rider: str
    ride_no: str
    points: int
    alt_station: str


@dataclass(frozen=True)
class StreetEdge:
    a: str
    b: str
    cost: int


@dataclass(frozen=True)
class Scenario:
    """Everything the ride planner needs, independent of how files arrived."""

    horizon: int
    stations: tuple[str, ...]
    capacity: dict[str, int]
    # bike_id -> opening station (only READY bikes are lendable at dawn)
    opening_bikes: dict[str, str]
    ready_bikes: frozenset[str]
    pending_docks: frozenset[tuple[str, int]]
    reservations: tuple[Reservation, ...]
    consents: dict[str, Consent]
    edges: tuple[StreetEdge, ...]
    truck_start: str
    truck_capacity: int
    reward_cap: int | None = None


@dataclass(frozen=True)
class TruckMove:
    tick: int
    frm: str
    to: str
    cost: int
    loaded: tuple[str, ...]


@dataclass(frozen=True)
class LoadUnload:
    tick: int
    station: str
    loaded: tuple[str, ...] = ()
    unloaded: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fulfillment:
    ride_no: str
    bike_id: str
    pickup_tick: int
    pickup_station: str
    return_tick: str | int
    return_station: str
    rewarded: bool
    points: int


@dataclass(frozen=True)
class Plan:
    feasible: bool
    transport_cost: int
    reward_points: int
    fulfillments: tuple[Fulfillment, ...]
    truck_moves: tuple[TruckMove, ...]
    dock_ops: tuple[LoadUnload, ...]
    reason: str | None = None
    estimated: bool = False

    def to_json(self) -> dict:
        return {
            "feasible": self.feasible,
            "estimated": self.estimated,
            "transport_cost": self.transport_cost,
            "reward_points": self.reward_points,
            "fulfillments": [
                {
                    "ride_no": f.ride_no,
                    "bike_id": f.bike_id,
                    "pickup_tick": f.pickup_tick,
                    "pickup_station": f.pickup_station,
                    "return_tick": f.return_tick,
                    "return_station": f.return_station,
                    "rewarded": f.rewarded,
                    "points": f.points,
                }
                for f in self.fulfillments
            ],
            "truck_moves": [
                {
                    "tick": m.tick,
                    "from": m.frm,
                    "to": m.to,
                    "cost": m.cost,
                    "carrying": list(m.loaded),
                }
                for m in self.truck_moves
            ],
            "dock_ops": [
                {
                    "tick": o.tick,
                    "station": o.station,
                    "loaded": list(o.loaded),
                    "unloaded": list(o.unloaded),
                }
                for o in self.dock_ops
            ],
            "reason": self.reason,
        }
