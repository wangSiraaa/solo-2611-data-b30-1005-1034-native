"""Fictional astronomy exhibit: observation orders on settled auction units.

This module layers a synthetic observatory on top of the sealed auction.
Every **actually settled** unit (partial fills count by their real allocated
quantity) grants the buyer's team one telescope *opening* at any station.
One opening records three consecutive exposures and consumes exactly one
unit.  Bidding alone never signs a plate — only a committed night shift does.

Exhibit model (all synthetic, integer-only):

  * stations: fixed planar coordinates, a per-exposure duty roster, a mirror
    move limit (positions per fully idle exposure) and an optional allowed
    mirror range;
  * stars: an occultation circle radius, per-exposure circle centres, and
    the integer mirror position each station must hold to aim at the star.

A station is occulted when ``dist^2(station, circle centre) <= radius^2``.
A predicted plate is signed for ``(star, centre exposure, station)`` when the
three exposures ``centre-1 / centre / centre+1`` read bright/dark/bright, the
required mirror position is allowed at the station, and the station is on
duty for all three exposures.  Two plates prove the same crossing when they
share star and centre exposure and the two station ordinates lie strictly on
opposite sides of the centre exposure's circle ordinate.

The night plan is a small exact optimisation (<= 3 stations, <= 8 orders,
15 exposures): maximise total exhibit value, break ties towards fewer
openings.  Signed real exposures are persisted with a primary key on
(station, exposure), so a later night can never re-spend them, and spent
openings are ledgered per buyer.
"""

from __future__ import annotations

import json
from itertools import combinations
from typing import Any

from .core import (
    DomainError,
    InvalidState,
    LimitExceeded,
    NotFound,
    get_receipt,
    now_utc,
    put_receipt,
    require_int,
    require_nonempty,
)

MAX_STATIONS = 3        # 三站
MAX_ORDERS = 8          # 至多八个观测请求
NIGHT_EXPOSURES = 15    # 十五拍, exposures are numbered 1..15
OPENINGS_PER_ORDER = 2  # a scored order spends one opening at each of two stations


# ---------------------------------------------------------------------------
# pure geometry / prediction helpers
# ---------------------------------------------------------------------------

def _dist2(x: int, y: int, cx: int, cy: int) -> int:
    return (x - cx) ** 2 + (y - cy) ** 2


def evaluate_event(model: dict, star: str, center: int) -> dict[str, Any]:
    """Predicted plates and station pairs for one (star, centre exposure).

    Pure function of the exhibit model: it never looks at auctions, wallets
    or vouchers, so the forecast is identical no matter how few units a team
    actually settled.  Every number needed to re-check a plate by hand is
    included in the per-station triple substitutions.
    """
    star_model = model["stars"][star]
    radius = star_model["radius"]
    r2 = radius * radius
    centers = star_model["centers"]
    exposures = (center - 1, center, center + 1)

    stations_out: list[dict[str, Any]] = []
    plates: list[str] = []
    for name in sorted(model["stations"]):
        st = model["stations"][name]
        aim = star_model["aims"].get(name)
        substitutions: list[dict[str, Any]] = []
        lights: list[str | None] = []
        ephemeris_ok = True
        for exp in exposures:
            c = centers.get(exp)
            if c is None:
                ephemeris_ok = False
                lights.append(None)
                substitutions.append({"exposure": exp, "ephemeris": None})
                continue
            d2 = _dist2(st["x"], st["y"], c[0], c[1])
            blocked = d2 <= r2
            light = "dark" if blocked else "bright"
            lights.append(light)
            substitutions.append(
                {
                    "exposure": exp,
                    "station_xy": [st["x"], st["y"]],
                    "circle": [c[0], c[1]],
                    "dist2": d2,
                    "r2": r2,
                    "blocked": blocked,
                    "light": light,
                }
            )
        pattern_ok = lights == ["bright", "dark", "bright"]
        mirror_allowed = (
            aim is not None
            and (st["mirror_min"] is None or aim >= st["mirror_min"])
            and (st["mirror_max"] is None or aim <= st["mirror_max"])
        )
        duty = [bool(model["duty"][name][e]) for e in exposures]
        duty_ok = all(duty)
        plate = ephemeris_ok and pattern_ok and mirror_allowed and duty_ok
        if plate:
            plates.append(name)
        stations_out.append(
            {
                "station": name,
                "mirror": aim,
                "substitutions": substitutions,
                "checks": {
                    "ephemeris": ephemeris_ok,
                    "pattern_bright_dark_bright": pattern_ok,
                    "mirror_allowed": mirror_allowed,
                    "duty": duty,
                },
                "plate": plate,
            }
        )

    pairs: list[dict[str, Any]] = []
    centre_row = centers.get(center)
    if centre_row is not None:
        cy = centre_row[1]
        for s1, s2 in combinations(plates, 2):
            y1 = model["stations"][s1]["y"]
            y2 = model["stations"][s2]["y"]
            # Strictly one above and one below the centre circle's ordinate.
            if (y1 - cy) * (y2 - cy) < 0:
                pairs.append({"stations": [s1, s2], "y": [y1, y2], "center_cy": cy})

    return {
        "star": star,
        "center_exposure": center,
        "radius": radius,
        "stations": stations_out,
        "plates": plates,
        "pairs": pairs,
    }


# ---------------------------------------------------------------------------
# pure night-scheduling helpers
# ---------------------------------------------------------------------------

def _station_feasible(obs: list[tuple[int, int]], move_limit: int, duty: dict[int, bool]) -> bool:
    """Check one station's observation list ((centre, mirror) pairs).

    Observations may not overlap in exposures; between two observations the
    mirror may move by at most ``move_limit`` positions per fully idle,
    on-duty exposure.  Aiming before the first observation is free.
    """
    obs = sorted(obs)
    for (c1, m1), (c2, m2) in zip(obs, obs[1:]):
        if c2 - c1 < 3:
            return False  # the three-exposure windows overlap
        gap = [e for e in range(c1 + 2, c2 - 1) if duty.get(e, False)]
        dist = abs(m2 - m1)
        if dist == 0:
            continue
        if move_limit == 0:
            return False
        needed = -(-dist // move_limit)  # ceil
        if needed > len(gap):
            return False
    return True


def plan_assignment(
    orders: list[dict],
    options: dict[str, list[tuple[str, str]]],
    remaining: dict[str, int],
    stations: dict[str, dict],
    aims: dict[tuple[str, str], int],
    duty: dict[str, dict[int, bool]],
) -> list[tuple[str, str, str]]:
    """Exact search: maximise (total value, -openings), deterministic ties.

    ``orders`` are sorted by order_id; ``options`` maps an order id to its
    usable station pairs; ``remaining`` maps a buyer to openings left.
    Returns a list of (order_id, station1, station2).
    """
    best_score: tuple | None = None
    best_tie: tuple | None = None
    best_choice: list[tuple[str, str, str]] = []

    used: dict[str, int] = {}
    station_obs: dict[str, list[tuple[int, int]]] = {s: [] for s in stations}
    choice: list[tuple[str, str, str]] = []

    def rec(i: int, value: int) -> None:
        nonlocal best_score, best_tie, best_choice
        if i == len(orders):
            openings = OPENINGS_PER_ORDER * len(choice)
            score = (value, -openings)
            tie = tuple(sorted(choice))
            if (
                best_score is None
                or score > best_score
                or (score == best_score and tie < best_tie)
            ):
                best_score, best_tie = score, tie
                best_choice = list(choice)
            return
        order = orders[i]
        # Option 1: skip this order.
        rec(i + 1, value)
        # Option 2: score it through one of its station pairs.
        for s1, s2 in options.get(order["order_id"], ()):
            buyer = order["buyer"]
            if used.get(buyer, 0) + OPENINGS_PER_ORDER > remaining.get(buyer, 0):
                continue
            added = []
            feasible = True
            for s in (s1, s2):
                mirror = aims[(order["star"], s)]
                candidate = station_obs[s] + [(order["center_exposure"], mirror)]
                if not _station_feasible(
                    candidate, stations[s]["move_limit"], duty[s]
                ):
                    feasible = False
                    break
                added.append((s, order["center_exposure"], mirror))
            if not feasible:
                continue
            used[buyer] = used.get(buyer, 0) + OPENINGS_PER_ORDER
            for s, centre, mirror in added:
                station_obs[s].append((centre, mirror))
            choice.append((order["order_id"], s1, s2))
            rec(i + 1, value + order["value"])
            choice.pop()
            for s, centre, mirror in added:
                station_obs[s].pop()
            used[buyer] -= OPENINGS_PER_ORDER

    rec(0, 0)
    return best_choice


def build_sheet(
    station: str,
    obs: list[dict],
    duty: dict[int, bool],
    move_limit: int,
) -> list[dict[str, Any]]:
    """Per-exposure execution sheet for one station.

    ``obs`` is a centre-sorted list of dicts with keys centre, mirror, star,
    order_id and lights (exposure -> bright/dark).  The mirror may move by at
    most ``move_limit`` positions per fully idle on-duty exposure; the first
    aim of the night is free.
    """
    sheet: list[dict[str, Any]] = []
    mirror: int | None = None
    cursor = 1
    for o in obs:
        start = o["center"] - 1
        for e in range(cursor, start):
            if not duty.get(e, False):
                sheet.append({"exposure": e, "activity": "down", "mirror": mirror})
                continue
            if mirror is None:
                mirror = o["mirror"]  # 开场自由对准: first aim is free
                sheet.append({"exposure": e, "activity": "idle", "mirror": mirror})
                continue
            diff = o["mirror"] - mirror
            step = 0 if diff == 0 else max(-move_limit, min(move_limit, diff))
            if step:
                mirror += step
                sheet.append({"exposure": e, "activity": "move", "mirror": mirror})
            else:
                sheet.append({"exposure": e, "activity": "idle", "mirror": mirror})
        if mirror is None:
            mirror = o["mirror"]
        for e in (o["center"] - 1, o["center"], o["center"] + 1):
            sheet.append(
                {
                    "exposure": e,
                    "activity": "expose",
                    "star": o["star"],
                    "order_id": o["order_id"],
                    "mirror": o["mirror"],
                    "light": o["lights"][e],
                }
            )
        cursor = o["center"] + 2
    for e in range(cursor, NIGHT_EXPOSURES + 1):
        activity = "idle" if duty.get(e, False) else "down"
        sheet.append({"exposure": e, "activity": activity, "mirror": mirror})
    return sheet


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

class ExhibitService:
    """Observation orders, plate prediction and night shifts."""

    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------ model load

    @staticmethod
    def _load_model(conn) -> dict[str, Any]:
        stations: dict[str, dict] = {}
        for r in conn.execute(
            "SELECT station, x, y, move_limit, mirror_min, mirror_max "
            "FROM exhibit_stations ORDER BY station"
        ):
            stations[r["station"]] = {
                "x": r["x"],
                "y": r["y"],
                "move_limit": r["move_limit"],
                "mirror_min": r["mirror_min"],
                "mirror_max": r["mirror_max"],
            }
        duty: dict[str, dict[int, bool]] = {s: {} for s in stations}
        for r in conn.execute("SELECT station, exposure, available FROM exhibit_duty"):
            duty[r["station"]][r["exposure"]] = bool(r["available"])
        stars: dict[str, dict] = {}
        for r in conn.execute("SELECT star, radius FROM exhibit_stars ORDER BY star"):
            stars[r["star"]] = {"radius": r["radius"], "centers": {}, "aims": {}}
        for r in conn.execute("SELECT star, exposure, cx, cy FROM exhibit_star_centers"):
            stars[r["star"]]["centers"][r["exposure"]] = (r["cx"], r["cy"])
        for r in conn.execute("SELECT star, station, mirror FROM exhibit_star_aims"):
            stars[r["star"]]["aims"][r["station"]] = r["mirror"]
        orders = [
            dict(r)
            for r in conn.execute(
                "SELECT order_id, buyer, star, center_exposure, value, status "
                "FROM exhibit_orders ORDER BY order_id"
            )
        ]
        return {"stations": stations, "duty": duty, "stars": stars, "orders": orders}

    # -------------------------------------------------------- model registry

    def register_station(
        self,
        request_id: str,
        station: str,
        x: int,
        y: int,
        move_limit: int,
        mirror_min: int | None = None,
        mirror_max: int | None = None,
    ) -> dict[str, Any]:
        require_nonempty(request_id, "request_id")
        require_nonempty(station, "station")
        require_int(x, "x")
        require_int(y, "y")
        require_int(move_limit, "move_limit", minimum=0)
        if mirror_min is not None:
            require_int(mirror_min, "mirror_min")
        if mirror_max is not None:
            require_int(mirror_max, "mirror_max")
        if mirror_min is not None and mirror_max is not None and mirror_min > mirror_max:
            raise DomainError("mirror_min must not exceed mirror_max")
        payload = {"station": station, "x": x, "y": y, "move_limit": move_limit,
                   "mirror_min": mirror_min, "mirror_max": mirror_max}
        operation = "exhibit.register_station"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            existing = conn.execute(
                "SELECT * FROM exhibit_stations WHERE station=?", (station,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["x"] == x and existing["y"] == y
                    and existing["move_limit"] == move_limit
                    and existing["mirror_min"] == mirror_min
                    and existing["mirror_max"] == mirror_max
                )
                if not same:
                    raise InvalidState(
                        f"station {station} already registered with different parameters"
                    )
                response = self._station_response(existing)
                put_receipt(conn, request_id, operation, payload, response)
                return response
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM exhibit_stations"
            ).fetchone()["n"]
            if total >= MAX_STATIONS:
                raise LimitExceeded(f"at most {MAX_STATIONS} stations in this exhibit")
            at = now_utc()
            conn.execute(
                "INSERT INTO exhibit_stations(station, x, y, move_limit, mirror_min, "
                "mirror_max, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (station, x, y, move_limit, mirror_min, mirror_max, at),
            )
            for exposure in range(1, NIGHT_EXPOSURES + 1):
                conn.execute(
                    "INSERT INTO exhibit_duty(station, exposure, available) "
                    "VALUES (?, ?, 1)",
                    (station, exposure),
                )
            response = {
                "station": station, "x": x, "y": y, "move_limit": move_limit,
                "mirror_min": mirror_min, "mirror_max": mirror_max,
                "duty": "available all night (exposures 1-15)", "created_at": at,
            }
            put_receipt(conn, request_id, operation, payload, response)
        return response

    @staticmethod
    def _station_response(row) -> dict[str, Any]:
        return {
            "station": row["station"], "x": row["x"], "y": row["y"],
            "move_limit": row["move_limit"], "mirror_min": row["mirror_min"],
            "mirror_max": row["mirror_max"], "created_at": row["created_at"],
        }

    def set_duty(
        self, request_id: str, station: str, exposure: int, available: bool
    ) -> dict[str, Any]:
        """Mark one exposure at one station on/off duty (停机/恢复)."""
        require_nonempty(request_id, "request_id")
        require_nonempty(station, "station")
        require_int(exposure, "exposure", minimum=1)
        if exposure > NIGHT_EXPOSURES:
            raise DomainError(f"exposure must be <= {NIGHT_EXPOSURES}, got {exposure}")
        if not isinstance(available, bool):
            raise DomainError(f"available must be a boolean, got {available!r}")
        payload = {"station": station, "exposure": exposure, "available": available}
        operation = "exhibit.set_duty"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            row = conn.execute(
                "SELECT available FROM exhibit_duty WHERE station=? AND exposure=?",
                (station, exposure),
            ).fetchone()
            if row is None:
                raise NotFound(f"station {station!r} not registered")
            conn.execute(
                "UPDATE exhibit_duty SET available=? WHERE station=? AND exposure=?",
                (1 if available else 0, station, exposure),
            )
            response = {"station": station, "exposure": exposure, "available": available}
            put_receipt(conn, request_id, operation, payload, response)
        return response

    def register_star(
        self,
        request_id: str,
        star: str,
        radius: int,
        centers: dict[int, tuple[int, int]],
        aims: dict[str, int],
    ) -> dict[str, Any]:
        """Register a star's radius, per-exposure circle centres and aims.

        The ephemeris is immutable once registered: re-registering with the
        same parameters is idempotent, different parameters are rejected so a
        signed forecast can never shift under a committed night plan.
        """
        require_nonempty(request_id, "request_id")
        require_nonempty(star, "star")
        require_int(radius, "radius", minimum=1)
        if not isinstance(centers, dict) or not centers:
            raise DomainError("centers must be a non-empty {exposure: (cx, cy)} map")
        norm_centers: dict[int, tuple[int, int]] = {}
        for exposure, c in centers.items():
            require_int(exposure, "exposure", minimum=1)
            if exposure > NIGHT_EXPOSURES:
                raise DomainError(f"exposure must be <= {NIGHT_EXPOSURES}")
            if not isinstance(c, (list, tuple)) or len(c) != 2:
                raise DomainError(f"center for exposure {exposure} must be (cx, cy)")
            require_int(c[0], "cx")
            require_int(c[1], "cy")
            norm_centers[int(exposure)] = (int(c[0]), int(c[1]))
        if not isinstance(aims, dict) or not aims:
            raise DomainError("aims must be a non-empty {station: mirror} map")
        norm_aims: dict[str, int] = {}
        for station, mirror in aims.items():
            require_nonempty(station, "station")
            require_int(mirror, "mirror")
            norm_aims[station] = int(mirror)
        payload = {
            "star": star, "radius": radius,
            "centers": {str(k): list(v) for k, v in sorted(norm_centers.items())},
            "aims": dict(sorted(norm_aims.items())),
        }
        operation = "exhibit.register_star"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            for station in norm_aims:
                if station not in self._load_model(conn)["stations"]:
                    raise NotFound(f"station {station!r} not registered")
            existing = conn.execute(
                "SELECT radius FROM exhibit_stars WHERE star=?", (star,)
            ).fetchone()
            if existing is not None:
                model = self._load_model(conn)["stars"][star]
                same = (
                    existing["radius"] == radius
                    and model["centers"] == norm_centers
                    and model["aims"] == norm_aims
                )
                if not same:
                    raise InvalidState(
                        f"star {star} already registered with a different ephemeris"
                    )
                response = {"star": star, "radius": radius, "centers": norm_centers,
                            "aims": norm_aims, "note": "already registered"}
                put_receipt(conn, request_id, operation, payload, response)
                return response
            at = now_utc()
            conn.execute(
                "INSERT INTO exhibit_stars(star, radius, created_at) VALUES (?, ?, ?)",
                (star, radius, at),
            )
            for exposure, (cx, cy) in norm_centers.items():
                conn.execute(
                    "INSERT INTO exhibit_star_centers(star, exposure, cx, cy) "
                    "VALUES (?, ?, ?, ?)",
                    (star, exposure, cx, cy),
                )
            for station, mirror in norm_aims.items():
                conn.execute(
                    "INSERT INTO exhibit_star_aims(star, station, mirror) "
                    "VALUES (?, ?, ?)",
                    (star, station, mirror),
                )
            response = {"star": star, "radius": radius, "centers": norm_centers,
                        "aims": norm_aims, "created_at": at}
            put_receipt(conn, request_id, operation, payload, response)
        return response

    def register_order(
        self,
        request_id: str,
        order_id: str,
        buyer: str,
        star: str,
        center_exposure: int,
        value: int,
    ) -> dict[str, Any]:
        """Register one observation order (观测单): star, centre exposure, value."""
        require_nonempty(request_id, "request_id")
        require_nonempty(order_id, "order_id")
        require_nonempty(buyer, "buyer")
        require_nonempty(star, "star")
        require_int(center_exposure, "center_exposure", minimum=2)
        if center_exposure > NIGHT_EXPOSURES - 1:
            raise DomainError(
                f"center_exposure must be <= {NIGHT_EXPOSURES - 1} so the three "
                "exposures fit the night"
            )
        require_int(value, "value", minimum=1)
        payload = {"order_id": order_id, "buyer": buyer, "star": star,
                   "center_exposure": center_exposure, "value": value}
        operation = "exhibit.register_order"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            existing = conn.execute(
                "SELECT * FROM exhibit_orders WHERE order_id=?", (order_id,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["buyer"] == buyer and existing["star"] == star
                    and existing["center_exposure"] == center_exposure
                    and existing["value"] == value
                )
                if not same:
                    raise InvalidState(
                        f"order {order_id} already registered with different parameters"
                    )
                response = dict(existing)
                put_receipt(conn, request_id, operation, payload, response)
                return response
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM exhibit_orders"
            ).fetchone()["n"]
            if total >= MAX_ORDERS:
                raise LimitExceeded(f"at most {MAX_ORDERS} observation orders")
            if conn.execute(
                "SELECT 1 FROM exhibit_stars WHERE star=?", (star,)
            ).fetchone() is None:
                raise NotFound(f"star {star!r} not registered")
            at = now_utc()
            conn.execute(
                "INSERT INTO exhibit_orders(order_id, buyer, star, center_exposure, "
                "value, status, created_at) VALUES (?, ?, ?, ?, ?, 'PENDING', ?)",
                (order_id, buyer, star, center_exposure, value, at),
            )
            response = {"order_id": order_id, "buyer": buyer, "star": star,
                        "center_exposure": center_exposure, "value": value,
                        "status": "PENDING", "created_at": at}
            put_receipt(conn, request_id, operation, payload, response)
        return response

    # -------------------------------------------------------------- openings

    @staticmethod
    def _granted_openings(conn) -> dict[str, int]:
        """Openings earned per buyer: actually settled units, partials included."""
        granted: dict[str, int] = {}
        for row in conn.execute(
            "SELECT allocations_json FROM settlements WHERE status='CLEARED'"
        ):
            snapshot = json.loads(row["allocations_json"])
            for a in snapshot["allocations"]:
                if a["allocated_qty"] > 0:
                    granted[a["buyer"]] = granted.get(a["buyer"], 0) + a["allocated_qty"]
        return granted

    @staticmethod
    def _spent_openings(conn) -> dict[str, int]:
        spent: dict[str, int] = {}
        for row in conn.execute(
            "SELECT buyer, SUM(openings) AS n FROM exhibit_openings GROUP BY buyer"
        ):
            spent[row["buyer"]] = int(row["n"])
        return spent

    def openings(self, buyer: str) -> dict[str, Any]:
        """Buyer-by-buyer opening account (兑账): granted, spent, remaining."""
        require_nonempty(buyer, "buyer")
        with self.db.read() as conn:
            granted = self._granted_openings(conn).get(buyer, 0)
            spends = [
                dict(r)
                for r in conn.execute(
                    "SELECT night_id, order_id, station, openings FROM "
                    "exhibit_openings WHERE buyer=? ORDER BY night_id, order_id, station",
                    (buyer,),
                )
            ]
        spent = sum(s["openings"] for s in spends)
        return {
            "buyer": buyer,
            "granted_openings": granted,
            "spent_openings": spent,
            "remaining_openings": granted - spent,
            "spends": spends,
        }

    def signed_exposures(self) -> list[dict[str, Any]]:
        """All signed real exposures so far (empty until a night is planned)."""
        with self.db.read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT night_id, station, exposure, order_id, buyer, star, "
                    "center_exposure, mirror, light FROM exhibit_exposures "
                    "ORDER BY night_id, station, exposure"
                )
            ]

    # ------------------------------------------------------------- prediction

    def predict(self) -> dict[str, Any]:
        """Forecast plates and station pairs for every registered order.

        Pure read of the exhibit model: the forecast never depends on how
        many units any team actually settled (预报星历不因缺券改变).
        """
        with self.db.read() as conn:
            model = self._load_model(conn)
        events = []
        seen = set()
        for order in model["orders"]:
            key = (order["star"], order["center_exposure"])
            if key in seen:
                continue
            seen.add(key)
            events.append(evaluate_event(model, order["star"], order["center_exposure"]))
        return {
            "stations": model["stations"],
            "events": events,
        }

    # ------------------------------------------------------------ night shift

    def plan_night(self, request_id: str, night_id: str) -> dict[str, Any]:
        """Plan and commit one night shift.

        Selects the value-maximising set of orders (ties: fewer openings),
        signs the real exposures, and deducts two openings per scored order
        from its buyer.  Signed exposures and spent openings are persisted,
        so the next night can never re-spend them.
        """
        require_nonempty(request_id, "request_id")
        require_nonempty(night_id, "night_id")
        payload = {"night_id": night_id}
        operation = "exhibit.plan_night"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            if conn.execute(
                "SELECT 1 FROM exhibit_nights WHERE night_id=?", (night_id,)
            ).fetchone() is not None:
                raise InvalidState(f"night {night_id} is already planned")

            model = self._load_model(conn)
            granted = self._granted_openings(conn)
            spent = self._spent_openings(conn)
            remaining = {
                b: granted[b] - spent.get(b, 0) for b in granted
            }
            spent_exposures = {
                (r["station"], r["exposure"])
                for r in conn.execute("SELECT station, exposure FROM exhibit_exposures")
            }

            pending = [o for o in model["orders"] if o["status"] == "PENDING"]
            events: dict[tuple[str, int], dict] = {}
            for order in pending:
                key = (order["star"], order["center_exposure"])
                if key not in events:
                    events[key] = evaluate_event(model, order["star"], order["center_exposure"])

            # Usable station pairs per order: predicted pairs whose six real
            # exposures have not been spent by earlier nights.
            options: dict[str, list[tuple[str, str]]] = {}
            for order in pending:
                centre = order["center_exposure"]
                usable = []
                for pair in events[(order["star"], centre)]["pairs"]:
                    s1, s2 = pair["stations"]
                    exposures = [
                        (s, e)
                        for s in (s1, s2)
                        for e in (centre - 1, centre, centre + 1)
                    ]
                    if all(e not in spent_exposures for e in exposures):
                        usable.append((s1, s2))
                options[order["order_id"]] = sorted(usable)

            aims = {
                (star, station): mirror
                for star, sm in model["stars"].items()
                for station, mirror in sm["aims"].items()
            }
            choice = plan_assignment(
                pending, options, remaining, model["stations"], aims, model["duty"]
            )
            chosen = {c[0]: (c[1], c[2]) for c in choice}

            at = now_utc()
            by_id = {o["order_id"]: o for o in pending}
            total_value = sum(by_id[oid]["value"] for oid in chosen)
            openings_used = OPENINGS_PER_ORDER * len(chosen)
            conn.execute(
                "INSERT INTO exhibit_nights(night_id, total_value, openings_used, "
                "created_at) VALUES (?, ?, ?, ?)",
                (night_id, total_value, openings_used, at),
            )

            scored: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            station_obs: dict[str, list[dict]] = {s: [] for s in model["stations"]}
            for order in pending:
                pair = chosen.get(order["order_id"])
                if pair is None:
                    reason = (
                        "no_valid_pair"
                        if not options[order["order_id"]]
                        else "not_selected"
                    )
                    skipped.append(
                        {"order_id": order["order_id"], "buyer": order["buyer"],
                         "star": order["star"],
                         "center_exposure": order["center_exposure"],
                         "value": order["value"], "reason": reason}
                    )
                    continue
                s1, s2 = pair
                event = events[(order["star"], order["center_exposure"])]
                lights_by_station = {
                    block["station"]: {
                        s["exposure"]: s["light"]
                        for s in block["substitutions"]
                        if "light" in s
                    }
                    for block in event["stations"]
                }
                conn.execute(
                    "UPDATE exhibit_orders SET status='SCORED' WHERE order_id=?",
                    (order["order_id"],),
                )
                for s in (s1, s2):
                    mirror = aims[(order["star"], s)]
                    lights = lights_by_station[s]
                    station_obs[s].append(
                        {"center": order["center_exposure"], "mirror": mirror,
                         "star": order["star"], "order_id": order["order_id"],
                         "lights": lights}
                    )
                    for e in (order["center_exposure"] - 1, order["center_exposure"],
                              order["center_exposure"] + 1):
                        conn.execute(
                            "INSERT INTO exhibit_exposures(station, exposure, night_id, "
                            "order_id, buyer, star, center_exposure, mirror, light, "
                            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (s, e, night_id, order["order_id"], order["buyer"],
                             order["star"], order["center_exposure"], mirror,
                             lights[e], at),
                        )
                    conn.execute(
                        "INSERT INTO exhibit_openings(night_id, buyer, order_id, "
                        "station, openings, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                        (night_id, order["buyer"], order["order_id"], s, at),
                    )
                scored.append(
                    {"order_id": order["order_id"], "buyer": order["buyer"],
                     "star": order["star"], "center_exposure": order["center_exposure"],
                     "value": order["value"], "stations": [s1, s2]}
                )

            openings_used = OPENINGS_PER_ORDER * len(scored)
            sheets = {
                s: build_sheet(
                    s,
                    sorted(station_obs[s], key=lambda o: o["center"]),
                    model["duty"][s],
                    model["stations"][s]["move_limit"],
                )
                for s in sorted(model["stations"])
            }
            accounts = self._accounts(scored, skipped, granted, spent)
            response = {
                "night_id": night_id,
                "status": "PLANNED",
                "total_value": total_value,
                "openings_used": openings_used,
                "scored": scored,
                "skipped": skipped,
                "sheets": sheets,
                "accounts": accounts,
                "planned_at": at,
            }
            put_receipt(conn, request_id, operation, payload, response)
        return response

    def _accounts(self, scored, skipped, granted, spent_before):
        """Buyer-by-buyer opening settlement for the plan response."""
        scored_by_buyer: dict[str, list[dict]] = {}
        for s in scored:
            scored_by_buyer.setdefault(s["buyer"], []).append(s)
        skipped_by_buyer: dict[str, list[dict]] = {}
        for s in skipped:
            skipped_by_buyer.setdefault(s["buyer"], []).append(s)
        buyers = sorted(
            set(granted) | set(scored_by_buyer) | set(skipped_by_buyer)
        )
        accounts = []
        for buyer in buyers:
            spent_now = OPENINGS_PER_ORDER * len(scored_by_buyer.get(buyer, []))
            before = spent_before.get(buyer, 0)
            orders = [
                {**s, "status": "SCORED"} for s in scored_by_buyer.get(buyer, [])
            ] + [
                {**s, "status": "SKIPPED"} for s in skipped_by_buyer.get(buyer, [])
            ]
            orders.sort(key=lambda o: o["order_id"])
            accounts.append(
                {
                    "buyer": buyer,
                    "granted_openings": granted.get(buyer, 0),
                    "spent_before": before,
                    "spent_this_night": spent_now,
                    "remaining_openings": granted.get(buyer, 0) - before - spent_now,
                    "orders": orders,
                }
            )
        return accounts

    def read_night(self, night_id: str) -> dict[str, Any]:
        """Re-read a committed night: sheets and accounts from stored rows.

        Signed exposures are immutable rows; the idle/move/down cells of a
        sheet are re-derived from the *current* duty roster, so a later 停机
        edit can restyle those cells but never the signed exposures.
        """
        require_nonempty(night_id, "night_id")
        with self.db.read() as conn:
            night = conn.execute(
                "SELECT * FROM exhibit_nights WHERE night_id=?", (night_id,)
            ).fetchone()
            if night is None:
                raise NotFound(f"night {night_id!r} not found")
            model = self._load_model(conn)
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM exhibit_exposures WHERE night_id=? "
                    "ORDER BY station, exposure",
                    (night_id,),
                )
            ]
            opening_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT buyer, order_id, station, openings FROM exhibit_openings "
                    "WHERE night_id=? ORDER BY order_id, station",
                    (night_id,),
                )
            ]
            granted = self._granted_openings(conn)
            spent_total = self._spent_openings(conn)

        station_obs: dict[str, dict[int, dict]] = {}
        for r in rows:
            obs = station_obs.setdefault(r["station"], {}).setdefault(
                r["center_exposure"],
                {"center": r["center_exposure"], "mirror": r["mirror"],
                 "star": r["star"], "order_id": r["order_id"], "lights": {}},
            )
            obs["lights"][r["exposure"]] = r["light"]
        sheets = {}
        for s in sorted(model["stations"]):
            obs_list = sorted(
                station_obs.get(s, {}).values(), key=lambda o: o["center"]
            )
            sheets[s] = build_sheet(
                s, obs_list, model["duty"][s], model["stations"][s]["move_limit"]
            )

        scored: dict[str, dict] = {}
        spent_this_night: dict[str, int] = {}
        for r in opening_rows:
            spent_this_night[r["buyer"]] = (
                spent_this_night.get(r["buyer"], 0) + r["openings"]
            )
            entry = scored.setdefault(
                r["order_id"],
                {"order_id": r["order_id"], "buyer": r["buyer"], "stations": []},
            )
            entry["stations"].append(r["station"])
        accounts = []
        for buyer in sorted(spent_this_night):
            accounts.append(
                {
                    "buyer": buyer,
                    "granted_openings": granted.get(buyer, 0),
                    "spent_this_night": spent_this_night[buyer],
                    "spent_total": spent_total.get(buyer, 0),
                    "remaining_openings": granted.get(buyer, 0)
                    - spent_total.get(buyer, 0),
                }
            )
        return {
            "night_id": night_id,
            "status": "PLANNED",
            "total_value": night["total_value"],
            "openings_used": night["openings_used"],
            "scored": [scored[k] for k in sorted(scored)],
            "sheets": sheets,
            "accounts": accounts,
            "planned_at": night["created_at"],
        }
