"""Overnight handover reconciliation -> frozen dawn handover ledger.

Completion rule (the only way a loan or a return is ever *completed*):

    same bike_id AND same real ride_no AND both sides really signed
    AND the event time chains onto the previous real custody location.

For an entry (return) there is one more gate: the bike must currently be an
*in-transit bike of the same ride number*.

A station-side ``station_sign`` that only exists because the reader contract
filled its declared default is provably not a real sign-off (the adapted
``sources`` say ``fill_default``); such an event is only a **one-sided
candidate**.  In-transit, unaccepted, and mutually contradicted bikes can
never count as dawn-lendable.

A corrected receipt (a higher revision accepted by the ingestion library)
rebuilds a second frozen ledger; both ledgers stay available.  A dock whose
entry was never confirmed is held as a *pending placeholder* and is never
promised to anyone else in the final report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .domain import BikeState, HandoverEvent, OpeningDock


@dataclass
class BikeVerdict:
    bike_id: str
    state: BikeState
    physical_location: str | None
    dock_idx: int | None
    ride_no: str | None
    missing_credentials: list[str] = field(default_factory=list)
    block_origin: str | None = None
    evidence_row_ids: list[str] = field(default_factory=list)

    @property
    def lendable_at_dawn(self) -> bool:
        return self.state is BikeState.READY


@dataclass
class StationLedgerEntry:
    station: str
    capacity: int
    # dock_idx -> bike_id or None; a pending placeholder is recorded separately
    occupied: dict[int, str]
    pending_placeholders: dict[int, str]
    available_docks: list[int]
    lendable_bikes: list[str]
    blocked_origins: list[str]


@dataclass(frozen=True)
class FrozenLedger:
    """An immutable handover snapshot the station master can hand over."""

    label: str
    bikes: tuple[BikeVerdict, ...]
    stations: tuple[StationLedgerEntry, ...]
    contradictions: tuple[str, ...]
    capacity_matches_opening: bool
    capacity_detail: str

    def bike(self, bike_id: str) -> BikeVerdict | None:
        for b in self.bikes:
            if b.bike_id == bike_id:
                return b
        return None

    def ready_bike_ids(self) -> frozenset[str]:
        return frozenset(b.bike_id for b in self.bikes if b.lendable_at_dawn)

    def pending_dock_keys(self) -> frozenset[tuple[str, int]]:
        out: set[tuple[str, int]] = set()
        for s in self.stations:
            for idx in s.pending_placeholders:
                out.add((s.station, idx))
        return frozenset(out)

    def to_json(self) -> dict:
        return {
            "label": self.label,
            "capacity_matches_opening": self.capacity_matches_opening,
            "capacity_detail": self.capacity_detail,
            "contradictions": list(self.contradictions),
            "bikes": [
                {
                    "bike_id": b.bike_id,
                    "state": b.state.value,
                    "physical_location": b.physical_location,
                    "dock_idx": b.dock_idx,
                    "ride_no": b.ride_no,
                    "lendable_at_dawn": b.lendable_at_dawn,
                    "missing_credentials": list(b.missing_credentials),
                    "block_origin": b.block_origin,
                    "evidence_row_ids": list(b.evidence_row_ids),
                }
                for b in self.bikes
            ],
            "stations": [
                {
                    "station": s.station,
                    "capacity": s.capacity,
                    "occupied": {str(k): v for k, v in sorted(s.occupied.items())},
                    "pending_placeholders": {
                        str(k): v for k, v in sorted(s.pending_placeholders.items())
                    },
                    "available_docks": list(s.available_docks),
                    "lendable_bikes": list(s.lendable_bikes),
                    "blocked_origins": list(s.blocked_origins),
                }
                for s in self.stations
            ],
        }


def _group(events: list[HandoverEvent]) -> dict[tuple, list[HandoverEvent]]:
    grouped: dict[tuple, list[HandoverEvent]] = {}
    for ev in events:
        grouped.setdefault(ev.key, []).append(ev)
    return grouped


def reconcile(
    label: str,
    opening_docks: list[OpeningDock],
    events: list[HandoverEvent],
) -> FrozenLedger:
    """Build one frozen handover ledger from the adapted opening master and
    the adapted station/bike event rows."""

    contradictions: list[str] = []

    # -- 1. opening capacity and physical starting positions ---------------
    capacity: dict[str, int] = {}
    opening_position: dict[str, tuple[str, int]] = {}
    dock_seen: dict[tuple[str, int], OpeningDock] = {}
    for d in opening_docks:
        capacity[d.station] = capacity.get(d.station, 0) + 1
        key = (d.station, d.dock_idx)
        if key in dock_seen:
            contradictions.append(f"duplicate opening dock {key}")
        dock_seen[key] = d
        if d.bike_id is not None:
            if d.bike_id in opening_position:
                contradictions.append(
                    f"bike {d.bike_id} listed at two opening docks"
                )
            opening_position[d.bike_id] = (d.station, d.dock_idx)

    # -- 2. pair station and bike sides on (bike, ride, event) -------------
    grouped = _group(events)
    # current custody per bike: (station, tick) of the last *completed* event
    custody: dict[str, tuple[str, int]] = {}
    # open rides: bike -> ride_no currently in transit
    open_ride: dict[str, str] = {}
    # docks already claimed by completed entries / opening master / pending
    claimed_docks: dict[str, set[int]] = {st: set() for st in capacity}
    for station, dock_idx in opening_position.values():
        claimed_docks[station].add(dock_idx)
    verdicts: dict[str, BikeVerdict] = {}

    def verdict(bike_id: str) -> BikeVerdict:
        if bike_id not in verdicts:
            verdicts[bike_id] = BikeVerdict(
                bike_id=bike_id,
                state=BikeState.BLOCKED,
                physical_location=None,
                dock_idx=None,
                ride_no=None,
                block_origin="never appears in a completed handover",
            )
        return verdicts[bike_id]

    # process in tick order so custody chaining is well defined
    for key in sorted(grouped, key=lambda k: min(e.tick for e in grouped[k])):
        sides = grouped[key]
        bike_id, ride_no, event = key
        v = verdict(bike_id)
        v.evidence_row_ids.extend(sorted({e.source_row_id for e in sides}))
        v.ride_no = ride_no

        # contradiction: multiple materially different rows on the same key
        stations = {e.station for e in sides}
        ticks = {e.tick for e in sides}
        if len(stations) > 1 or len(ticks) > 1:
            v.state = BikeState.CONTRADICTED
            v.block_origin = f"conflicting station/tick across {key}"
            contradictions.append(
                f"{key}: sides disagree on station {sorted(stations)} / tick {sorted(ticks)}"
            )
            continue

        ev = sides[0]
        # The two physical sides arrive as separate rows; merge them on the
        # business key.  A sign-off counts only if some side really carries
        # it (a reader-default fill is detectable and never counts).
        real_station = any(e.real_station_signed for e in sides)
        real_bike = any(e.real_bike_signed for e in sides)
        default_station = any(
            e.station_sign is not None and not e.real_station_signed for e in sides
        )
        known_dock = next((e.dock_idx for e in sides if e.dock_idx is not None), None)

        # -- 3. one-sided candidates never complete a handover -------------
        if not (real_station and real_bike):
            missing = []
            if not real_station:
                missing.append(
                    "real station sign-off"
                    + (" (reader default fill only)" if default_station else "")
                )
            if not real_bike:
                missing.append("real bike sign-off")
            v.missing_credentials = sorted(set(v.missing_credentials + missing))
            if event == "entry":
                # Physically present behind a one-sided receipt: hold a dock
                # as a pending placeholder (the lowest not otherwise claimed),
                # but never lendable.  The placeholder is never reassigned.
                dock = ev.dock_idx
                if dock is None:
                    free = [i for i in range(capacity[ev.station])
                            if i not in claimed_docks[ev.station]]
                    dock = free[0] if free else None
                if dock is not None:
                    claimed_docks[ev.station].add(dock)
                v.state = BikeState.PENDING_ACCEPTANCE
                v.physical_location = ev.station
                v.dock_idx = dock
                v.block_origin = "entry receipt only one-sided"
            else:
                # An exit (even a one-sided candidate) means the physical
                # bike has left its opening dock; that dock is free again.
                if bike_id in opening_position:
                    ost, odock = opening_position[bike_id]
                    claimed_docks[ost].discard(odock)
                # The bike-side trip number is a real fact: the ride is open
                # even though the loan itself is not a completed handover.
                open_ride[bike_id] = ride_no
                v.state = BikeState.IN_TRANSIT
                v.physical_location = None
                v.dock_idx = None
                v.block_origin = "exit receipt only one-sided"
            continue

        # -- 4. both sides really signed: enforce the custody chain --------
        prev = custody.get(bike_id) or (
            opening_position[bike_id] if bike_id in opening_position else None
        )

        if event == "entry":
            # Mandatory in-transit gate: the bike must currently be an
            # in-transit bike of this same ride.  The open ride is itself a
            # valid chain anchor for a bike returning from the road, so no
            # dock custody record is required for the return.
            if open_ride.get(bike_id) != ride_no:
                v.state = BikeState.BLOCKED
                v.block_origin = (
                    f"return for ride {ride_no} but bike is not an in-transit "
                    f"bike of that ride (open={open_ride.get(bike_id)})"
                )
                contradictions.append(f"{key}: return without matching open ride")
                continue
            if prev is not None and ev.tick < prev[1]:
                v.state = BikeState.BLOCKED
                v.block_origin = (
                    f"entry at tick {ev.tick} predates last custody tick {prev[1]}"
                )
                contradictions.append(f"{key}: non-chronological custody chain")
                continue
            v.state = BikeState.READY
            v.physical_location = ev.station
            v.dock_idx = known_dock
            if known_dock is not None:
                claimed_docks[ev.station].add(known_dock)
            v.missing_credentials = []
            v.block_origin = None
            open_ride.pop(bike_id, None)
            custody[bike_id] = (ev.station, ev.tick)
            continue

        # exit: must chain onto a previous real custody location
        if prev is None:
            v.state = BikeState.BLOCKED
            v.block_origin = (
                f"{event} {ride_no}: no previous real custody location to chain onto"
            )
            contradictions.append(
                f"{key}: no prior custody for bike {bike_id}"
            )
            continue
        prev_station, prev_tick = prev
        if ev.tick < prev_tick:
            v.state = BikeState.BLOCKED
            v.block_origin = (
                f"{event} at tick {ev.tick} predates last custody tick {prev_tick}"
            )
            contradictions.append(f"{key}: non-chronological custody chain")
            continue
        if prev_station != ev.station:
            v.state = BikeState.BLOCKED
            v.block_origin = (
                f"exit at {ev.station} but last real custody was {prev_station}"
            )
            contradictions.append(f"{key}: exit does not chain from custody")
            continue
        # completed loan: bike leaves the dock
        v.state = BikeState.IN_TRANSIT
        v.physical_location = None
        v.dock_idx = None
        v.missing_credentials = []
        open_ride[bike_id] = ride_no
        custody[bike_id] = (ev.station, ev.tick)

    # -- 5. bikes known only from the opening master -----------------------
    for bike_id, (station, dock_idx) in opening_position.items():
        if bike_id in verdicts:
            continue
        opening = dock_seen[(station, dock_idx)]
        if opening.status == "ready":
            verdicts[bike_id] = BikeVerdict(
                bike_id=bike_id,
                state=BikeState.READY,
                physical_location=station,
                dock_idx=dock_idx,
                ride_no=None,
                missing_credentials=[],
                block_origin=None,
                evidence_row_ids=[f"opening:{station}-{dock_idx}"],
            )
        else:
            # Physically listed but not accepted into the lendable pool
            # (e.g. still carrying an open ride or awaiting acceptance).
            verdicts[bike_id] = BikeVerdict(
                bike_id=bike_id,
                state=BikeState.IN_TRANSIT
                if opening.status == "in_transit"
                else BikeState.BLOCKED,
                physical_location=station,
                dock_idx=dock_idx,
                ride_no=None,
                missing_credentials=[],
                block_origin=f"opening master status is {opening.status!r}, not ready",
                evidence_row_ids=[f"opening:{station}-{dock_idx}"],
            )

    # bikes still carrying an open ride at dawn are not lendable
    for bike_id, ride_no in open_ride.items():
        v = verdict(bike_id)
        if v.state is BikeState.READY:
            v.state = BikeState.IN_TRANSIT
            v.block_origin = f"ride {ride_no} never closed"

    # -- 6. per-station frozen view for the station master -----------------
    station_entries: list[StationLedgerEntry] = []
    for station in sorted(capacity):
        cap = capacity[station]
        occupied: dict[int, str] = {}
        pending: dict[int, str] = {}
        blocked_origins: list[str] = []
        for b in verdicts.values():
            if b.physical_location != station:
                continue
            if b.state in (BikeState.READY,):
                if b.dock_idx is not None:
                    occupied[b.dock_idx] = b.bike_id
            elif b.state is BikeState.PENDING_ACCEPTANCE and b.dock_idx is not None:
                pending[b.dock_idx] = b.bike_id
                blocked_origins.append(
                    f"dock {b.dock_idx}: {b.bike_id} pending ({'; '.join(b.missing_credentials)})"
                )
            elif b.state in (BikeState.CONTRADICTED, BikeState.BLOCKED):
                blocked_origins.append(
                    f"{b.bike_id}: {b.block_origin or b.state.value}"
                )
        held = set(occupied) | set(pending)
        available = [idx for idx in range(cap) if idx not in held]
        lendable = sorted(
            b.bike_id
            for b in verdicts.values()
            if b.lendable_at_dawn and b.physical_location == station
        )
        station_entries.append(
            StationLedgerEntry(
                station=station,
                capacity=cap,
                occupied=occupied,
                pending_placeholders=pending,
                available_docks=available,
                lendable_bikes=lendable,
                blocked_origins=blocked_origins,
            )
        )

    # -- 7. the real old library: station capacities match opening rows ----
    master_cap = {s: sum(1 for d in opening_docks if d.station == s) for s in capacity}
    capacity_ok = master_cap == capacity
    capacity_detail = (
        "opening dock rows per station match declared capacities: "
        + ", ".join(f"{s}={master_cap[s]}" for s in sorted(master_cap))
    )

    return FrozenLedger(
        label=label,
        bikes=tuple(verdicts[b] for b in sorted(verdicts)),
        stations=tuple(station_entries),
        contradictions=tuple(contradictions),
        capacity_matches_opening=capacity_ok,
        capacity_detail=capacity_detail,
    )
