"""Sealed unit auction service: bids, freeze, cutoff and immutable results.

The service never touches cash balances.  It only verifies hold credentials
issued by the margin warehouse and, at settlement, asks the warehouse to
consume/release those holds.  The ranked result is computed once at cutoff
and persisted, so clearing cannot silently re-rank around a failed payment.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .core import (
    DataIntegrityError,
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
from .margin import MarginLedger

MAX_UNITS = 10
MAX_BUYERS_PER_AUCTION = 5
MAX_OPEN_AUCTIONS = 3  # total auctions in this synthetic deployment


def normalize_charity(charity: Any, qty: int) -> list[dict[str, Any]]:
    """Validate per-unit charity registrations given at bid time.

    ``charity`` is a list whose i-th entry describes the lot's (i+1)-th unit:
    the non-donation rebate valuation (``rebate_cents``) and the foundations
    allowed to use that unit's donation (``allow``).  Missing trailing units
    default to a zero rebate with no permissions.
    """
    if charity is None:
        return []
    if not isinstance(charity, list):
        raise DomainError("charity must be a list of per-unit registrations")
    if len(charity) > qty:
        raise DomainError(
            f"charity registrations ({len(charity)}) exceed bid qty ({qty})"
        )
    normalized = []
    for unit_index, entry in enumerate(charity, start=1):
        if not isinstance(entry, dict):
            raise DomainError(f"charity entry for unit {unit_index} must be an object")
        rebate = require_int(
            entry.get("rebate_cents"), f"charity[{unit_index}].rebate_cents", minimum=0
        )
        allow = entry.get("allow", [])
        if not isinstance(allow, list) or any(
            not isinstance(f, str) or not f.strip() for f in allow
        ):
            raise DomainError(
                f"charity[{unit_index}].allow must be a list of foundation names"
            )
        normalized.append(
            {
                "unit_index": unit_index,
                "rebate_cents": rebate,
                "allow": sorted(set(allow)),
            }
        )
    return normalized


class AuctionService:
    def __init__(self, db, margin: MarginLedger):
        self.db = db
        self.margin = margin

    # ------------------------------------------------------------- lifecycle

    def create_auction(
        self, request_id: str, auction_id: str, units: int
    ) -> dict[str, Any]:
        require_nonempty(request_id, "request_id")
        require_nonempty(auction_id, "auction_id")
        require_int(units, "units", minimum=1)
        if units > MAX_UNITS:
            raise LimitExceeded(f"at most {MAX_UNITS} units per auction")
        payload = {"auction_id": auction_id, "units": units}
        operation = "auction.create"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            existing = conn.execute(
                "SELECT * FROM auctions WHERE auction_id=?", (auction_id,)
            ).fetchone()
            if existing is not None:
                if existing["units"] != units:
                    raise InvalidState(
                        f"auction {auction_id} already exists with units="
                        f"{existing['units']}"
                    )
                response = {
                    "auction_id": auction_id,
                    "units": units,
                    "status": existing["status"],
                    "created_at": existing["created_at"],
                }
                put_receipt(conn, request_id, operation, payload, response)
                return response
            total = conn.execute("SELECT COUNT(*) AS n FROM auctions").fetchone()["n"]
            if total >= MAX_OPEN_AUCTIONS:
                raise LimitExceeded(
                    f"at most {MAX_OPEN_AUCTIONS} auctions may be created"
                )
            at = now_utc()
            conn.execute(
                "INSERT INTO auctions(auction_id, units, status, created_at) "
                "VALUES (?, ?, 'OPEN', ?)",
                (auction_id, units, at),
            )
            response = {"auction_id": auction_id, "units": units,
                        "status": "OPEN", "created_at": at}
            put_receipt(conn, request_id, operation, payload, response)
        return response

    def _auction(self, conn, auction_id: str, for_update: bool = False) -> dict:
        row = conn.execute(
            "SELECT * FROM auctions WHERE auction_id=?", (auction_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"auction {auction_id!r} not found")
        return dict(row)

    def _require_open(self, conn, auction_id: str) -> dict:
        auction = self._auction(conn, auction_id)
        if auction["status"] != "OPEN":
            raise InvalidState(
                f"auction {auction_id} is {auction['status']}; bids are closed"
            )
        return auction

    def _check_buyer_capacity(self, conn, auction_id: str, buyer: str) -> None:
        row = conn.execute(
            "SELECT DISTINCT buyer FROM bids WHERE auction_id=?", (auction_id,)
        ).fetchall()
        buyers = {r["buyer"] for r in row}
        if buyer not in buyers and len(buyers) >= MAX_BUYERS_PER_AUCTION:
            raise LimitExceeded(
                f"auction {auction_id} already has {MAX_BUYERS_PER_AUCTION} buyers"
            )

    # ------------------------------------------------------------- bids

    def place_bid(
        self,
        request_id: str,
        bid_id: str,
        auction_id: str,
        buyer: str,
        qty: int,
        unit_price_cents: int,
        hold_id: str,
        charity: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        require_nonempty(request_id, "request_id")
        require_nonempty(bid_id, "bid_id")
        require_nonempty(auction_id, "auction_id")
        require_nonempty(buyer, "buyer")
        require_nonempty(hold_id, "hold_id")
        require_int(qty, "qty", minimum=1)
        require_int(unit_price_cents, "unit_price_cents", minimum=1)
        charity_units = normalize_charity(charity, qty)
        payload = {"bid_id": bid_id, "auction_id": auction_id, "buyer": buyer,
                   "qty": qty, "unit_price_cents": unit_price_cents,
                   "hold_id": hold_id, "charity": charity_units}
        operation = "auction.place_bid"
        required = qty * unit_price_cents

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            self._require_open(conn, auction_id)
            self._check_buyer_capacity(conn, auction_id, buyer)
            existing = conn.execute(
                "SELECT * FROM bids WHERE bid_id=?", (bid_id,)
            ).fetchone()
            if existing is not None:
                raise InvalidState(
                    f"bid {bid_id} already exists ({existing['status']}); "
                    "use revise_bid for a live bid"
                )
            other = conn.execute(
                "SELECT 1 FROM bids WHERE auction_id=? AND buyer=? AND status='ACTIVE'",
                (auction_id, buyer),
            ).fetchone()
            if other is not None:
                raise InvalidState(
                    f"buyer {buyer} already has an active bid in {auction_id}"
                )
            # The auction itself validates the warehouse credential; caller
            # assertions such as "already frozen" are never trusted.
            self.margin.verify_credential(conn, hold_id, buyer, auction_id, required)
            at = now_utc()
            try:
                conn.execute(
                    "INSERT INTO bids(bid_id, auction_id, buyer, qty, unit_price_cents, "
                    "revision, status, hold_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, 'ACTIVE', ?, ?, ?)",
                    (bid_id, auction_id, buyer, qty, unit_price_cents, hold_id, at, at),
                )
            except sqlite3.IntegrityError as exc:
                # Unique indexes still hold under concurrent bid placement.
                raise DomainError(
                    f"bid rejected: {bid_id} already exists, or buyer {buyer} "
                    "already has an active bid, or the hold already backs one"
                ) from exc
            self._store_charity(conn, bid_id, charity_units, at)
            conn.execute(
                "INSERT INTO bid_events(request_id, bid_id, auction_id, buyer, action, "
                "qty, unit_price_cents, revision, hold_id, at) "
                "VALUES (?, ?, ?, ?, 'PLACE', ?, ?, 1, ?, ?)",
                (request_id, bid_id, auction_id, buyer, qty, unit_price_cents,
                 hold_id, at),
            )
            response = {
                "request_id": request_id,
                "bid_id": bid_id,
                "auction_id": auction_id,
                "buyer": buyer,
                "qty": qty,
                "unit_price_cents": unit_price_cents,
                "revision": 1,
                "status": "ACTIVE",
                "hold_id": hold_id,
                "required_cents": required,
                "charity": charity_units,
                "at": at,
            }
            put_receipt(conn, request_id, operation, payload, response)
        return response

    @staticmethod
    def _store_charity(conn, bid_id: str, charity_units: list[dict], at: str) -> None:
        """(Re)write a bid's per-unit charity registrations atomically."""
        conn.execute("DELETE FROM bid_charity WHERE bid_id=?", (bid_id,))
        for unit in charity_units:
            conn.execute(
                "INSERT INTO bid_charity(bid_id, unit_index, rebate_cents, "
                "allow_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (bid_id, unit["unit_index"], unit["rebate_cents"],
                 json.dumps(unit["allow"], ensure_ascii=False), at),
            )

    def revise_bid(
        self,
        request_id: str,
        bid_id: str,
        buyer: str,
        qty: int,
        unit_price_cents: int,
        new_hold_id: str,
        charity: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Replace a live bid wholesale with a higher revision.

        The replacement hold must already exist and cover the new notional.
        The old hold is released **in the same transaction** that installs the
        new bid/credential: there is no window where the old collateral is
        gone but the old bid could still be settled.  When ``charity`` is
        given, the per-unit charity registrations are replaced in the same
        commit; otherwise the existing registrations carry over.
        """
        require_nonempty(request_id, "request_id")
        require_nonempty(bid_id, "bid_id")
        require_nonempty(buyer, "buyer")
        require_nonempty(new_hold_id, "new_hold_id")
        require_int(qty, "qty", minimum=1)
        require_int(unit_price_cents, "unit_price_cents", minimum=1)
        charity_units = normalize_charity(charity, qty) if charity is not None else None
        payload = {"bid_id": bid_id, "buyer": buyer, "qty": qty,
                   "unit_price_cents": unit_price_cents, "new_hold_id": new_hold_id,
                   "charity": charity_units}
        operation = "auction.revise_bid"
        required = qty * unit_price_cents

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            bid = conn.execute("SELECT * FROM bids WHERE bid_id=?", (bid_id,)).fetchone()
            if bid is None:
                raise NotFound(f"bid {bid_id!r} not found")
            bid = dict(bid)
            if bid["status"] != "ACTIVE":
                raise InvalidState(f"bid {bid_id} is {bid['status']}")
            if bid["buyer"] != buyer:
                raise DomainError(f"bid {bid_id} belongs to {bid['buyer']}, not {buyer}")
            self._require_open(conn, bid["auction_id"])
            if new_hold_id == bid["hold_id"]:
                raise DomainError("revision requires a distinct hold credential")
            self.margin.verify_credential(
                conn, new_hold_id, buyer, bid["auction_id"], required
            )
            at = now_utc()
            old_hold_id = bid["hold_id"]
            revision = bid["revision"] + 1
            try:
                conn.execute(
                    "UPDATE bids SET qty=?, unit_price_cents=?, revision=?, hold_id=?, "
                    "updated_at=? WHERE bid_id=?",
                    (qty, unit_price_cents, revision, new_hold_id, at, bid_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError(
                    f"hold {new_hold_id} already backs another active bid"
                ) from exc
            # Atomic release of the superseded collateral (no independent receipt:
            # the outer revision receipt replays the whole swap).
            self.margin.release_in_tx(
                conn, old_hold_id, f"{request_id}:release", auction_id=bid["auction_id"]
            )
            if charity_units is not None:
                self._store_charity(conn, bid_id, charity_units, at)
            conn.execute(
                "INSERT INTO bid_events(request_id, bid_id, auction_id, buyer, action, "
                "qty, unit_price_cents, revision, hold_id, at) "
                "VALUES (?, ?, ?, ?, 'REVISE', ?, ?, ?, ?, ?)",
                (request_id, bid_id, bid["auction_id"], buyer, qty, unit_price_cents,
                 revision, new_hold_id, at),
            )
            response = {
                "request_id": request_id,
                "bid_id": bid_id,
                "auction_id": bid["auction_id"],
                "buyer": buyer,
                "qty": qty,
                "unit_price_cents": unit_price_cents,
                "revision": revision,
                "status": "ACTIVE",
                "hold_id": new_hold_id,
                "replaced_hold_id": old_hold_id,
                "required_cents": required,
                "charity": charity_units,
                "at": at,
            }
            put_receipt(conn, request_id, operation, payload, response)
        return response

    def cancel_bid(self, request_id: str, bid_id: str) -> dict[str, Any]:
        require_nonempty(request_id, "request_id")
        require_nonempty(bid_id, "bid_id")
        payload = {"bid_id": bid_id}
        operation = "auction.cancel_bid"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            bid = conn.execute("SELECT * FROM bids WHERE bid_id=?", (bid_id,)).fetchone()
            if bid is None:
                raise NotFound(f"bid {bid_id!r} not found")
            bid = dict(bid)
            if bid["status"] != "ACTIVE":
                raise InvalidState(f"bid {bid_id} is already {bid['status']}")
            self._require_open(conn, bid["auction_id"])
            at = now_utc()
            conn.execute(
                "UPDATE bids SET status='CANCELLED', updated_at=? WHERE bid_id=?",
                (at, bid_id),
            )
            self.margin.release_in_tx(
                conn, bid["hold_id"], f"{request_id}:release",
                auction_id=bid["auction_id"],
            )
            conn.execute(
                "INSERT INTO bid_events(request_id, bid_id, auction_id, buyer, action, "
                "qty, unit_price_cents, revision, hold_id, at) "
                "VALUES (?, ?, ?, ?, 'CANCEL', ?, ?, ?, ?, ?)",
                (request_id, bid_id, bid["auction_id"], bid["buyer"], bid["qty"],
                 bid["unit_price_cents"], bid["revision"], bid["hold_id"], at),
            )
            response = {
                "request_id": request_id,
                "bid_id": bid_id,
                "auction_id": bid["auction_id"],
                "buyer": bid["buyer"],
                "status": "CANCELLED",
                "released_hold_id": bid["hold_id"],
                "at": at,
            }
            put_receipt(conn, request_id, operation, payload, response)
        return response

    # ------------------------------------------------------------- cutoff

    def freeze_auction(self, request_id: str, auction_id: str) -> dict[str, Any]:
        """Close bidding and persist the immutable ranked result.

        No payments happen here.  Every valid bid's hold is re-verified, then
        the result is stored so settlement and any later re-open read the
        exact same allocation.
        """
        require_nonempty(request_id, "request_id")
        require_nonempty(auction_id, "auction_id")
        payload = {"auction_id": auction_id}
        operation = "auction.freeze"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            auction = self._auction(conn, auction_id)
            if auction["status"] in ("FROZEN", "SETTLED"):
                snapshot = self._load_snapshot(conn, auction_id)
                response = self._snapshot_response(
                    auction_id, auction["status"], snapshot
                )
                put_receipt(conn, request_id, operation, payload, response)
                return response

            bids = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM bids WHERE auction_id=? AND status='ACTIVE' "
                    "ORDER BY unit_price_cents DESC, bid_id ASC",
                    (auction_id,),
                )
            ]
            for bid in bids:
                required = bid["qty"] * bid["unit_price_cents"]
                self.margin.verify_credential(
                    conn, bid["hold_id"], bid["buyer"], auction_id, required
                )
            charity_regs = self._load_charity(conn, [b["bid_id"] for b in bids])

            snapshot = self._compute_result(auction, bids, charity_regs)
            at = now_utc()
            conn.execute(
                "UPDATE auctions SET status='FROZEN', frozen_at=? WHERE auction_id=?",
                (at, auction_id),
            )
            conn.execute(
                "INSERT INTO settlements(auction_id, clearing_price_cents, "
                "allocations_json, status, created_at) VALUES (?, ?, ?, 'FROZEN', ?)",
                (auction_id, snapshot["clearing_price_cents"],
                 json.dumps(snapshot, sort_keys=True, ensure_ascii=False), at),
            )
            response = self._snapshot_response(auction_id, "FROZEN", snapshot)
            put_receipt(conn, request_id, operation, payload, response)
        return response

    @staticmethod
    def _load_charity(conn, bid_ids: list[str]) -> dict[str, dict[int, dict]]:
        """Per-bid charity registrations, keyed by bid_id then unit_index."""
        regs: dict[str, dict[int, dict]] = {bid_id: {} for bid_id in bid_ids}
        if not bid_ids:
            return regs
        marks = ",".join("?" for _ in bid_ids)
        for row in conn.execute(
            f"SELECT bid_id, unit_index, rebate_cents, allow_json FROM bid_charity "
            f"WHERE bid_id IN ({marks}) ORDER BY bid_id, unit_index",
            bid_ids,
        ):
            regs[row["bid_id"]][row["unit_index"]] = {
                "unit_index": row["unit_index"],
                "rebate_cents": row["rebate_cents"],
                "allow": json.loads(row["allow_json"]),
            }
        return regs

    @staticmethod
    def _compute_result(
        auction: dict,
        bids: list[dict],
        charity_regs: dict[str, dict[int, dict]] | None = None,
    ) -> dict[str, Any]:
        """One deterministic pass: price desc, bid_id asc, partial last fill."""
        charity_regs = charity_regs or {}
        remaining = auction["units"]
        allocations: list[dict] = []
        clearing_price: int | None = None
        for bid in bids:  # already in deterministic priority order
            if remaining <= 0:
                allocated = 0
            else:
                allocated = min(bid["qty"], remaining)
                remaining -= allocated
            if allocated > 0:
                clearing_price = bid["unit_price_cents"]
            regs = charity_regs.get(bid["bid_id"], {})
            # The bid's registered charity terms travel with the frozen
            # result, so receipts are derived from immutable data.  Units
            # without a registration default to zero rebate, no permissions.
            charity = [
                regs.get(
                    unit_index,
                    {"unit_index": unit_index, "rebate_cents": 0, "allow": []},
                )
                for unit_index in range(1, bid["qty"] + 1)
            ]
            allocations.append(
                {
                    "bid_id": bid["bid_id"],
                    "buyer": bid["buyer"],
                    "qty": bid["qty"],
                    "unit_price_cents": bid["unit_price_cents"],
                    "allocated_qty": allocated,
                    "hold_id": bid["hold_id"],
                    "revision": bid["revision"],
                    "charity": charity,
                }
            )

        # Uniform price = lowest price among *allocated* units.  Bids that got
        # nothing never set the price.
        orders = []
        for a in allocations:
            if a["allocated_qty"] > 0:
                a["order_cents"] = a["allocated_qty"] * clearing_price  # type: ignore[operator]
                orders.append(
                    {
                        "buyer": a["buyer"],
                        "bid_id": a["bid_id"],
                        "allocated_qty": a["allocated_qty"],
                        "order_cents": a["order_cents"],
                        "hold_id": a["hold_id"],
                    }
                )
            else:
                a["order_cents"] = 0
        return {
            "auction_id": auction["auction_id"],
            "units": auction["units"],
            "clearing_price_cents": clearing_price,
            "units_allocated": auction["units"] - remaining,
            "allocations": allocations,
            "orders": orders,
        }

    def _load_snapshot(self, conn, auction_id: str) -> dict:
        row = conn.execute(
            "SELECT allocations_json FROM settlements WHERE auction_id=?",
            (auction_id,),
        ).fetchone()
        if row is None:
            raise DataIntegrityError(
                f"auction {auction_id} is frozen but has no stored result"
            )
        return json.loads(row["allocations_json"])

    @staticmethod
    def _snapshot_response(auction_id: str, status: str, snapshot: dict) -> dict:
        return {
            "auction_id": auction_id,
            "status": status,
            "units": snapshot["units"],
            "clearing_price_cents": snapshot["clearing_price_cents"],
            "units_allocated": snapshot["units_allocated"],
            "allocations": snapshot["allocations"],
            "orders": snapshot["orders"],
        }

    # ------------------------------------------------------------ settle

    def settle_auction(self, request_id: str, auction_id: str) -> dict[str, Any]:
        """Consume the frozen result against real margin holds.

        Each hold charge/release is an idempotent warehouse transaction with a
        deterministic request_id, so a lost response is resumed, not treated
        as failure.  The frozen ranking is never recomputed here.
        """
        require_nonempty(request_id, "request_id")
        require_nonempty(auction_id, "auction_id")
        payload = {"auction_id": auction_id}
        operation = "auction.settle"

        # Phase 1: fix the settlement intent (resumable).
        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            auction = self._auction(conn, auction_id)
            if auction["status"] == "OPEN":
                raise InvalidState(
                    f"auction {auction_id} is still OPEN; freeze it before settling"
                )
            snapshot = self._load_snapshot(conn, auction_id)
            settlement = conn.execute(
                "SELECT status FROM settlements WHERE auction_id=?", (auction_id,)
            ).fetchone()
            if settlement["status"] != "CLEARED":
                conn.execute(
                    "UPDATE settlements SET status='CLEARING' WHERE auction_id=?",
                    (auction_id,),
                )
                conn.execute(
                    "UPDATE auctions SET status='SETTLED' WHERE auction_id=?",
                    (auction_id,),
                )

        # Phase 2: idempotent warehouse side effects, in snapshot order.
        for item in snapshot["allocations"]:
            hold_id = item["hold_id"]
            if item["allocated_qty"] > 0:
                self.margin.charge_capped(
                    request_id=f"settle:{auction_id}:{item['bid_id']}",
                    hold_id=hold_id,
                    auction_id=auction_id,
                    charge_cents=item["order_cents"],
                )
            else:
                self.margin.release(
                    request_id=f"settle:{auction_id}:release:{item['bid_id']}",
                    hold_id=hold_id,
                )

        # Phase 3: mark cleared and record the receipt.
        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            at = now_utc()
            conn.execute(
                "UPDATE settlements SET status='CLEARED', cleared_at=? "
                "WHERE auction_id=?",
                (at, auction_id),
            )
            result = self._settlement_result(conn, auction_id, snapshot, at)
            put_receipt(conn, request_id, operation, payload, result)
        return result

    def _settlement_result(self, conn, auction_id, snapshot, cleared_at) -> dict:
        charges = []
        for item in snapshot["allocations"]:
            hold = conn.execute(
                "SELECT status, charged_cents FROM holds WHERE hold_id=?",
                (item["hold_id"],),
            ).fetchone()
            charges.append(
                {
                    "bid_id": item["bid_id"],
                    "buyer": item["buyer"],
                    "hold_id": item["hold_id"],
                    "allocated_qty": item["allocated_qty"],
                    "order_cents": item["order_cents"],
                    "hold_status": hold["status"] if hold else None,
                    "charged_cents": hold["charged_cents"] if hold else None,
                }
            )
        return {
            "auction_id": auction_id,
            "status": "SETTLED",
            "clearing_price_cents": snapshot["clearing_price_cents"],
            "units_allocated": snapshot["units_allocated"],
            "orders": snapshot["orders"],
            "settlement": charges,
            "settled_at": cleared_at,
        }

    # -------------------------------------------------------------- reads

    def read_result(self, auction_id: str) -> dict[str, Any]:
        """Re-open after cutoff/settlement: always the same frozen result."""
        with self.db.read() as conn:
            auction = self._auction(conn, auction_id)
            if auction["status"] == "OPEN":
                return {
                    "auction_id": auction_id,
                    "status": "OPEN",
                    "result": None,
                    "note": "bidding is open; no fixed result yet",
                }
            snapshot = self._load_snapshot(conn, auction_id)
            settlement = conn.execute(
                "SELECT status, cleared_at FROM settlements WHERE auction_id=?",
                (auction_id,),
            ).fetchone()
            holds = {
                r["hold_id"]: dict(r)
                for r in conn.execute(
                    "SELECT hold_id, status, charged_cents FROM holds WHERE auction_id=?",
                    (auction_id,),
                )
            }
        allocations = []
        for a in snapshot["allocations"]:
            h = holds.get(a["hold_id"], {})
            allocations.append(
                {
                    **a,
                    "hold_status": h.get("status"),
                    "charged_cents": h.get("charged_cents"),
                }
            )
        return {
            "auction_id": auction_id,
            "status": auction["status"],
            "units": snapshot["units"],
            "clearing_price_cents": snapshot["clearing_price_cents"],
            "units_allocated": snapshot["units_allocated"],
            "allocations": allocations,
            "orders": snapshot["orders"],
            "settlement_status": settlement["status"] if settlement else None,
            "cleared_at": settlement["cleared_at"] if settlement else None,
        }

    def explain_auction(self, auction_id: str) -> dict[str, Any]:
        """Human-readable derivation of the frozen cutoff."""
        with self.db.read() as conn:
            auction = self._auction(conn, auction_id)
            if auction["status"] == "OPEN":
                raise InvalidState("auction is still OPEN; freeze it first")
            snapshot = self._load_snapshot(conn, auction_id)
            events = [
                dict(r)
                for r in conn.execute(
                    "SELECT seq, action, bid_id, buyer, qty, unit_price_cents, "
                    "revision, hold_id, at FROM bid_events WHERE auction_id=? "
                    "ORDER BY seq",
                    (auction_id,),
                )
            ]

        ranked = []
        cumulative = 0
        for rank, a in enumerate(snapshot["allocations"], start=1):
            remaining = max(snapshot["units"] - cumulative, 0)
            cumulative += a["allocated_qty"]
            cue = "tie-break by bid_id asc"
            ranked.append(
                {
                    "rank": rank,
                    "bid_id": a["bid_id"],
                    "buyer": a["buyer"],
                    "qty": a["qty"],
                    "unit_price_cents": a["unit_price_cents"],
                    "allocated_qty": a["allocated_qty"],
                    "order_cents": a["order_cents"],
                    "cutoff": "partial fill (last unit)" if 0 < a["allocated_qty"] < a["qty"]
                    else ("out (units exhausted)" if a["allocated_qty"] == 0 else "filled"),
                    "explanation": (
                        f"price {a['unit_price_cents']} cents; {a['allocated_qty']} of "
                        f"{a['qty']} units allocated with {remaining} units left before it"
                    ),
                }
            )
        return {
            "auction_id": auction_id,
            "status": auction["status"],
            "units": snapshot["units"],
            "rule": (
                "sort by unit_price_cents DESC, bid_id ASC; allocate min(qty, remaining) "
                "until units exhausted; partial fill allowed on the last allocated bid"
            ),
            "clearing_rule": (
                "single uniform price = lowest unit price among allocated units; "
                "if none allocated there is no clearing price"
            ),
            "ranked": ranked,
            "clearing_price_cents": snapshot["clearing_price_cents"],
            "units_allocated": snapshot["units_allocated"],
            "bid_events": events,
        }
