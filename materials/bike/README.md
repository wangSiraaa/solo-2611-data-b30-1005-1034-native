# Bike-show overnight handover and next-day group ride

This directory holds the **original files and contracts** for the fictional
bike-show scenario.  Every business fact is ingested through the exact
`extcontract` toolkit; nothing here is parsed by hand.

## Contracts (`contracts/`)

| Reader contract | Paired writer contracts | Notes |
|---|---|---|
| `reader-handover.json` | `writer-bike-trip.json` (bike side), `writer-station-full.json` (station side, signed), `writer-station-half.json` (station side, signature column omitted) | The reader declares an in-domain default `"good"` for `station_sign`. A row that omits the column is adapted with `fill_default`, and the ledger uses that provenance to treat it as a **one-sided candidate only**. |
| `reader-consent.json` | `writer-consent.json` | Signed reward consents (rider, ride, fixed points, alternative station). |
| `reader-reservation.json` | `writer-reservation.json` | The fixed one-day reservations. |
| `reader-station-master.json` | `writer-station-master.json` | Real old-library station capacities and opening bikes. |

`bike_sign` and `dock_idx` are nullable and have **no** default: a stream that
does not know them must send an explicit `null`, matching the toolkit rule
that a nullable-without-default column cannot be silently invented.

## Original files (`../../fixtures/bike/`)

| File | Stream | Meaning |
|---|---|---|
| `F-BOOT-master.json` | station master | A/B, two docks each; opening bikes and statuses. |
| `F-HALF-bike.json` | bike trip | B2's R9 exit and B-side-signed return to B. |
| `F-HALF-station.json` | station, half | B2's return with **no station sign-off** (reader fills `good`). |
| `F-TRANSIT-bike.json` / `F-TRANSIT-station.json` | bike / half station | B3's half-signed R8 exit: a genuinely in-transit bike. |
| `F-CORRECT-station.json` | station, full | Revision 2 of B2's return: supplies the real station sign-off and dock 1. |
| `F-CONSENT.json` | consent | Rider jia accepts 2 points to re-dock at B instead of A. |
| `F-RESERVATIONS.json` | reservation | jia (pick A@0, return A@2) and yi (pick B@3, return B@4). |

## Reconciliation rules (see `extcontract/bike/reconcile.py`)

A loan or return is **completed** only when the same bike, the same real ride
number, and **both real sign-offs** are present and the event time chains onto
the previous real custody location.  A return additionally requires the bike
to be an in-transit bike of that same ride.  Everything else is one of
`ready`, `in_transit`, `pending_acceptance`, `contradicted`, or `blocked`;
only `ready` bikes are dawn-lendable.  A half-signed entry holds a **pending
placeholder dock** that the final report never grants to anyone else.

A corrected receipt (higher revision) rebuilds a second `FrozenLedger`; the
first frozen account stays available.

## Ride planning (see `extcontract/bike/ride.py`)

At every tick: reservation returns → truck load/unload → reservation pickups
→ at most one priced edge move.  Capacity is checked on each stage's combined
total; a bike that only frees up later can never be used early.  The search is
exhaustive, so the minimum transport fee (then minimum reward on ties, or
minimum reward under a fee limit) is a proved optimum.  `estimate()` returns a
plan tagged `estimated` that is never recorded as a real dispatch.

Run the worked demonstration:

```bash
PYTHONPATH=. python3.11 examples/run_bike_demo.py
```
