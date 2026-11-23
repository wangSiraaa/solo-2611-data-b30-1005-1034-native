"""Load the real original bike-show files through the existing
:class:`extcontract.IngestionLibrary`.

Nothing in this module parses a business row by hand.  Every file is checked
against its declared writer contract, adapted to the fixed reader contract,
and committed atomically with revision handling.  The published
``reader-records/v1`` rows are the only data the handover ledger ever reads.

The reader contract supplies ``station_sign`` with an in-domain default
``"good"``.  We keep the per-field ``sources`` provenance so the ledger can
tell a *real* station signature (``conversion != "fill_default"``) from a
manufactured one — a filled default is only ever a one-sided candidate.
"""

from __future__ import annotations

import json
from pathlib import Path

from extcontract import IngestionLibrary, load_contract

from .domain import Consent, HandoverEvent, OpeningDock, Reservation

_CONTRACTS = Path(__file__).resolve().parents[2] / "materials" / "bike" / "contracts"


def _contract(name: str, role: str):
    return load_contract(_CONTRACTS / name, role=role)


def _read_doc(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _is_fill_default(sources: dict, field_id: str) -> bool:
    src = sources.get(field_id) or {}
    return src.get("conversion") == "fill_default"


class HandoverFiles:
    """The three original streams of the overnight handover, each ingested
    under its own fixed reader contract."""

    def __init__(self) -> None:
        self.handover_reader = _contract("reader-handover.json", "reader")
        self.consent_reader = _contract("reader-consent.json", "reader")
        self.reservation_reader = _contract("reader-reservation.json", "reader")
        self.master_reader = _contract("reader-station-master.json", "reader")

        self.handover = IngestionLibrary(self.handover_reader)
        self.consents = IngestionLibrary(self.consent_reader)
        self.reservations = IngestionLibrary(self.reservation_reader)
        self.master = IngestionLibrary(self.master_reader)

    # -- ingest one original file ------------------------------------------

    def ingest_handover(self, writer_name: str, path: str | Path):
        writer = _contract(writer_name, "writer")
        return self.handover.ingest(writer, _read_doc(path))

    def ingest_consent(self, path: str | Path):
        writer = _contract("writer-consent.json", "writer")
        return self.consents.ingest(writer, _read_doc(path))

    def ingest_reservation(self, path: str | Path):
        writer = _contract("writer-reservation.json", "writer")
        return self.reservations.ingest(writer, _read_doc(path))

    def ingest_master(self, path: str | Path):
        writer = _contract("writer-station-master.json", "writer")
        return self.master.ingest(writer, _read_doc(path))

    # -- read adapted rows back out of the real target slice ---------------

    def handover_events(self) -> list[HandoverEvent]:
        events: list[HandoverEvent] = []
        for state in self.handover.snapshot():
            v, s = state.encoded, state.sources
            events.append(
                HandoverEvent(
                    bike_id=v["bike_id"],
                    ride_no=v["ride_no"],
                    event=v["event"],
                    station=v["station"],
                    tick=int(v["tick"]),
                    dock_idx=None if v["dock_idx"] is None else int(v["dock_idx"]),
                    bike_sign=v["bike_sign"],
                    station_sign=v["station_sign"],
                    station_sign_is_default=_is_fill_default(s, "station_sign"),
                    bike_sign_is_default=_is_fill_default(s, "bike_sign"),
                    source_row_id=state.row_id,
                )
            )
        return events

    def opening_docks(self) -> list[OpeningDock]:
        docks: list[OpeningDock] = []
        for state in self.master.snapshot():
            v = state.encoded
            docks.append(
                OpeningDock(
                    station=v["station"],
                    dock_idx=int(v["dock_idx"]),
                    bike_id=v["bike_id"],
                    status=v["status"],
                )
            )
        return docks

    def reservation_list(self) -> list[Reservation]:
        return [
            Reservation(
                ride_no=state.encoded["ride_no"],
                rider=state.encoded["rider"],
                pickup_tick=int(state.encoded["pickup_tick"]),
                pickup_station=state.encoded["pickup_station"],
                return_tick=int(state.encoded["return_tick"]),
                return_station=state.encoded["return_station"],
            )
            for state in self.reservations.snapshot()
        ]

    def consent_map(self) -> dict[str, Consent]:
        out: dict[str, Consent] = {}
        for state in self.consents.snapshot():
            v = state.encoded
            out[v["ride_no"]] = Consent(
                rider=v["rider"],
                ride_no=v["ride_no"],
                points=int(v["points"]),
                alt_station=v["alt_station"],
            )
        return out
