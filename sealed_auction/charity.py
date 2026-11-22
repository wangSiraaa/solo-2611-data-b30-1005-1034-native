"""Charity matching (配捐) on top of settled auctions.

Auction, margin and ordinary clearing are untouched.  This layer turns
*settled* allocations into named donation receipts and then plans which
foundation invitations enter a claim round:

* Every allocated unit gets one named receipt, but only after the auction is
  CLEARED — donations recognise settled funds only.  Ranked-but-uncharged
  tickets can merely be previewed.
* Per unit the pre-registered non-donation rebate (回赠估值) is deducted from
  the uniform hammer price first; a negative remainder floors at zero and the
  receipt keeps the reason.  Partial fills only cover the allocated prefix of
  the bid's registered units.
* A receipt is atomic (its amount is never split) and may be formally
  committed to at most one foundation.  Two agents racing the same real
  receipt confirm exactly one claim; a committed receipt is excluded from
  every later trial of the round.
* Each invitation states a minimum donation, a minimum number of *distinct*
  donors, and the fixed integer cents the foundation adds on success.  The
  planner picks the invitations and the truly permitted receipts that
  maximise the total match; ties use as few receipts as possible.  At most
  four foundations and twenty receipts per round keep the search exact.
"""

from __future__ import annotations

import json
import sqlite3
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

MAX_FOUNDATIONS_PER_ROUND = 4
MAX_RECEIPTS_PER_ROUND = 20

ZERO_REBATE_COVERS_PRICE = "rebate_covers_price"


def _receipt_id(auction_id: str, bid_id: str, unit_index: int) -> str:
    return f"rcpt:{auction_id}:{bid_id}:{unit_index}"


class CharityService:
    def __init__(self, db):
        self.db = db

    # ---------------------------------------------------------- derivation

    @staticmethod
    def _derive(snapshot: dict) -> list[dict[str, Any]]:
        """Would-be receipts from the immutable frozen result.

        Only the actually allocated prefix of each bid's registered units
        produces a receipt; the donation is the hammer price minus the unit's
        rebate, floored at zero with the reason preserved.
        """
        price = snapshot["clearing_price_cents"]
        derived: list[dict[str, Any]] = []
        if price is None:
            return derived
        for alloc in snapshot["allocations"]:
            registered = {c["unit_index"]: c for c in alloc.get("charity", [])}
            for unit_index in range(1, alloc["allocated_qty"] + 1):
                reg = registered.get(
                    unit_index,
                    {"unit_index": unit_index, "rebate_cents": 0, "allow": []},
                )
                donation = max(0, price - reg["rebate_cents"])
                derived.append(
                    {
                        "receipt_id": _receipt_id(
                            snapshot["auction_id"], alloc["bid_id"], unit_index
                        ),
                        "auction_id": snapshot["auction_id"],
                        "bid_id": alloc["bid_id"],
                        "donor": alloc["buyer"],
                        "unit_index": unit_index,
                        "unit_price_cents": price,
                        "rebate_cents": reg["rebate_cents"],
                        "donation_cents": donation,
                        "zero_reason": ZERO_REBATE_COVERS_PRICE
                        if donation == 0
                        else None,
                        "allow": list(reg["allow"]),
                    }
                )
        return derived

    @staticmethod
    def _shape(row: sqlite3.Row | dict) -> dict[str, Any]:
        r = dict(row)
        return {
            "receipt_id": r["receipt_id"],
            "auction_id": r["auction_id"],
            "bid_id": r["bid_id"],
            "donor": r["donor"],
            "unit_index": r["unit_index"],
            "unit_price_cents": r["unit_price_cents"],
            "rebate_cents": r["rebate_cents"],
            "donation_cents": r["donation_cents"],
            "zero_reason": r["zero_reason"],
            "allow": json.loads(r["allow_json"])
            if isinstance(r.get("allow_json"), str)
            else list(r.get("allow", [])),
            "status": r["status"],
            "committed_to": r["committed_to"],
            "claim_id": r["claim_id"],
        }

    # -------------------------------------------------------------- reads

    def preview_receipts(self, auction_id: str) -> dict[str, Any]:
        """Pre-query the tickets of a ranked auction.

        Works once the ranking is fixed.  Until the auction is CLEARED and
        the receipts are issued, every line is preview-only and can never
        enter a claim.
        """
        require_nonempty(auction_id, "auction_id")
        with self.db.read() as conn:
            auction = conn.execute(
                "SELECT status FROM auctions WHERE auction_id=?", (auction_id,)
            ).fetchone()
            if auction is None:
                raise NotFound(f"auction {auction_id!r} not found")
            if auction["status"] == "OPEN":
                raise InvalidState(
                    f"auction {auction_id} is still OPEN; no ranked tickets to preview"
                )
            row = conn.execute(
                "SELECT allocations_json, status FROM settlements WHERE auction_id=?",
                (auction_id,),
            ).fetchone()
            snapshot = json.loads(row["allocations_json"])
            settlement_status = row["status"]
            issued = {
                r["receipt_id"]: r["status"]
                for r in conn.execute(
                    "SELECT receipt_id, status FROM donation_receipts "
                    "WHERE auction_id=?",
                    (auction_id,),
                )
            }
        items = []
        for d in self._derive(snapshot):
            items.append(
                {
                    **d,
                    "state": issued.get(d["receipt_id"], "PREVIEW"),
                }
            )
        return {
            "auction_id": auction_id,
            "settlement_status": settlement_status,
            "clearing_price_cents": snapshot["clearing_price_cents"],
            "note": (
                "ranked but not charged; preview only"
                if settlement_status != "CLEARED"
                else "settled; issued rows are the real receipts"
            ),
            "receipts": items,
        }

    def list_receipts(
        self, auction_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """Real issued receipts, optionally filtered."""
        sql = "SELECT * FROM donation_receipts"
        clauses, args = [], []
        if auction_id is not None:
            clauses.append("auction_id=?")
            args.append(auction_id)
        if status is not None:
            clauses.append("status=?")
            args.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY receipt_id"
        with self.db.read() as conn:
            return [self._shape(r) for r in conn.execute(sql, args)]

    # ------------------------------------------------------------- issue

    def issue_receipts(self, request_id: str, auction_id: str) -> dict[str, Any]:
        """Open one named receipt per allocated unit of a cleared auction.

        Idempotent: receipt ids are deterministic, so a retried or repeated
        issuance replays the same rows and never duplicates a receipt.
        """
        require_nonempty(request_id, "request_id")
        require_nonempty(auction_id, "auction_id")
        payload = {"auction_id": auction_id}
        operation = "charity.issue_receipts"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            auction = conn.execute(
                "SELECT status FROM auctions WHERE auction_id=?", (auction_id,)
            ).fetchone()
            if auction is None:
                raise NotFound(f"auction {auction_id!r} not found")
            if auction["status"] == "OPEN":
                raise InvalidState(
                    f"auction {auction_id} is still OPEN; freeze and settle it first"
                )
            settlement = conn.execute(
                "SELECT allocations_json, status FROM settlements WHERE auction_id=?",
                (auction_id,),
            ).fetchone()
            if settlement["status"] != "CLEARED":
                raise InvalidState(
                    f"auction {auction_id} is {settlement['status']}; donation "
                    "receipts recognise cleared funds only (preview is available)"
                )
            snapshot = json.loads(settlement["allocations_json"])
            at = now_utc()
            for d in self._derive(snapshot):
                conn.execute(
                    "INSERT OR IGNORE INTO donation_receipts("
                    "receipt_id, auction_id, bid_id, donor, unit_index, "
                    "unit_price_cents, rebate_cents, donation_cents, zero_reason, "
                    "allow_json, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ISSUED', ?)",
                    (
                        d["receipt_id"],
                        d["auction_id"],
                        d["bid_id"],
                        d["donor"],
                        d["unit_index"],
                        d["unit_price_cents"],
                        d["rebate_cents"],
                        d["donation_cents"],
                        d["zero_reason"],
                        json.dumps(d["allow"], ensure_ascii=False),
                        at,
                    ),
                )
            rows = conn.execute(
                "SELECT * FROM donation_receipts WHERE auction_id=? "
                "ORDER BY bid_id, unit_index",
                (auction_id,),
            ).fetchall()
            response = {
                "request_id": request_id,
                "auction_id": auction_id,
                "status": "ISSUED",
                "receipts": [self._shape(r) for r in rows],
            }
            put_receipt(conn, request_id, operation, payload, response)
        return response

    # -------------------------------------------------------- invitations

    def register_invitation(
        self,
        request_id: str,
        invitation_id: str,
        round_id: str,
        foundation: str,
        min_donation_cents: int,
        min_donors: int,
        match_cents: int,
    ) -> dict[str, Any]:
        """Register a foundation's invitation for one claim round."""
        require_nonempty(request_id, "request_id")
        require_nonempty(invitation_id, "invitation_id")
        require_nonempty(round_id, "round_id")
        require_nonempty(foundation, "foundation")
        require_int(min_donation_cents, "min_donation_cents", minimum=0)
        require_int(min_donors, "min_donors", minimum=1)
        require_int(match_cents, "match_cents", minimum=1)
        payload = {
            "invitation_id": invitation_id,
            "round_id": round_id,
            "foundation": foundation,
            "min_donation_cents": min_donation_cents,
            "min_donors": min_donors,
            "match_cents": match_cents,
        }
        operation = "charity.register_invitation"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            existing = conn.execute(
                "SELECT * FROM match_invitations WHERE invitation_id=?",
                (invitation_id,),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["round_id"] == round_id
                    and existing["foundation"] == foundation
                    and existing["min_donation_cents"] == min_donation_cents
                    and existing["min_donors"] == min_donors
                    and existing["match_cents"] == match_cents
                )
                if not same:
                    raise InvalidState(
                        f"invitation {invitation_id} already registered with "
                        "different terms"
                    )
                response = self._invitation_response(request_id, existing)
                put_receipt(conn, request_id, operation, payload, response)
                return response
            foundations = {
                r["foundation"]
                for r in conn.execute(
                    "SELECT DISTINCT foundation FROM match_invitations "
                    "WHERE round_id=?",
                    (round_id,),
                )
            }
            if foundation not in foundations and len(foundations) >= MAX_FOUNDATIONS_PER_ROUND:
                raise LimitExceeded(
                    f"at most {MAX_FOUNDATIONS_PER_ROUND} foundations per round"
                )
            at = now_utc()
            conn.execute(
                "INSERT INTO match_invitations(invitation_id, round_id, foundation, "
                "min_donation_cents, min_donors, match_cents, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?)",
                (invitation_id, round_id, foundation, min_donation_cents,
                 min_donors, match_cents, at),
            )
            row = conn.execute(
                "SELECT * FROM match_invitations WHERE invitation_id=?",
                (invitation_id,),
            ).fetchone()
            response = self._invitation_response(request_id, row)
            put_receipt(conn, request_id, operation, payload, response)
        return response

    @staticmethod
    def _invitation_response(request_id: str, row) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "invitation_id": row["invitation_id"],
            "round_id": row["round_id"],
            "foundation": row["foundation"],
            "min_donation_cents": row["min_donation_cents"],
            "min_donors": row["min_donors"],
            "match_cents": row["match_cents"],
            "status": row["status"],
        }

    # ----------------------------------------------------------- planning

    def plan_round(self, round_id: str) -> dict[str, Any]:
        """Trial calculation (试算/预查): the optimal claim arrangement.

        Read-only and non-binding.  Only OPEN invitations and still-ISSUED
        real receipts take part; a receipt committed by an earlier confirmed
        claim of the same round is never recounted.
        """
        require_nonempty(round_id, "round_id")
        with self.db.read() as conn:
            invitations = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM match_invitations WHERE round_id=? "
                    "AND status='OPEN' ORDER BY invitation_id",
                    (round_id,),
                )
            ]
            receipts = [
                self._shape(r)
                for r in conn.execute(
                    "SELECT * FROM donation_receipts WHERE status='ISSUED' "
                    "ORDER BY receipt_id"
                )
            ]
        if len(receipts) > MAX_RECEIPTS_PER_ROUND:
            raise LimitExceeded(
                f"at most {MAX_RECEIPTS_PER_ROUND} receipts per round, "
                f"got {len(receipts)}"
            )
        plan = self._optimize(invitations, receipts)
        return self._plan_response(round_id, invitations, receipts, plan)

    @staticmethod
    def _plan_response(
        round_id: str,
        invitations: list[dict],
        receipts: list[dict],
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        claims = []
        used_receipt_ids: set[str] = set()
        claimed_invitation_ids: set[str] = set()
        for inv, subset in zip(plan["invitations"], plan["assignment"]):
            if subset is None:
                continue
            claimed_invitation_ids.add(inv["invitation_id"])
            chosen = sorted(
                (receipts[i] for i in subset), key=lambda r: r["receipt_id"]
            )
            for r in chosen:
                used_receipt_ids.add(r["receipt_id"])
            donors = sorted({r["donor"] for r in chosen})
            claims.append(
                {
                    "invitation_id": inv["invitation_id"],
                    "foundation": inv["foundation"],
                    "matched_cents": inv["match_cents"],
                    "donation_cents": sum(r["donation_cents"] for r in chosen),
                    "donor_count": len(donors),
                    "donors": donors,
                    "receipts": [
                        {
                            "receipt_id": r["receipt_id"],
                            "donor": r["donor"],
                            "donation_cents": r["donation_cents"],
                            "auction_id": r["auction_id"],
                            "bid_id": r["bid_id"],
                            "unit_index": r["unit_index"],
                        }
                        for r in chosen
                    ],
                }
            )
        claims.sort(key=lambda c: c["invitation_id"])
        return {
            "round_id": round_id,
            "state": "PLANNED",
            "total_match_cents": plan["total_match"],
            "receipts_used": len(used_receipt_ids),
            "claims": claims,
            "unclaimed_invitations": [
                inv["invitation_id"]
                for inv in invitations
                if inv["invitation_id"] not in claimed_invitation_ids
            ],
            "unused_receipts": [
                r["receipt_id"] for r in receipts
                if r["receipt_id"] not in used_receipt_ids
            ],
        }

    @staticmethod
    def _optimize(
        invitations: list[dict], receipts: list[dict]
    ) -> dict[str, Any]:
        """Exact branch-and-bound over invitations x receipt subsets.

        A receipt's amount is never split and never serves two foundations.
        Only inclusion-minimal feasible subsets are considered for an
        invitation: a superset buys the same fixed match with strictly more
        receipts, so it can never improve the (match, receipt-count) order.
        Deterministic traversal makes the tie-break stable.
        """
        ordered = sorted(
            invitations, key=lambda i: (-i["match_cents"], i["invitation_id"])
        )
        n_inv = len(ordered)
        donations = [r["donation_cents"] for r in receipts]
        donors = [r["donor"] for r in receipts]
        allows = [set(r["allow"]) for r in receipts]
        eligible = [
            [
                i
                for i in range(len(receipts))
                if donations[i] > 0 and inv["foundation"] in allows[i]
            ]
            for inv in ordered
        ]

        used = [False] * len(receipts)
        chosen: list[tuple[int, ...] | None] = [None] * n_inv
        best = {"total_match": 0, "receipt_count": 0, "assignment": [None] * n_inv}
        subset_cache: dict[tuple[int, tuple[int, ...]], list[frozenset]] = {}

        def minimal_subsets(idx: int) -> list[frozenset]:
            key = (idx, tuple(i for i, u in enumerate(used) if u))
            if key in subset_cache:
                return subset_cache[key]
            inv = ordered[idx]
            cand = [i for i in eligible[idx] if not used[i]]
            cand.sort(key=lambda i: (-donations[i], receipts[i]["receipt_id"]))
            m = len(cand)
            suffix_sum = [0] * (m + 1)
            for k in range(m - 1, -1, -1):
                suffix_sum[k] = suffix_sum[k + 1] + donations[cand[k]]
            found: list[frozenset] = []

            def dfs(pos: int, picked: list[int], amount: int, dset: frozenset):
                if (
                    amount >= inv["min_donation_cents"]
                    and len(dset) >= inv["min_donors"]
                ):
                    found.append(frozenset(picked))
                    return  # minimal on this path: extending only adds cost
                if pos >= m or amount + suffix_sum[pos] < inv["min_donation_cents"]:
                    return
                reachable = set(dset)
                for k in range(pos, m):
                    reachable.add(donors[cand[k]])
                if len(reachable) < inv["min_donors"]:
                    return
                for k in range(pos, m):
                    i = cand[k]
                    dfs(
                        k + 1,
                        picked + [i],
                        amount + donations[i],
                        dset | {donors[i]},
                    )

            dfs(0, [], 0, frozenset())
            minimal = [s for s in found if not any(o < s for o in found)]
            minimal.sort(
                key=lambda s: (len(s), tuple(sorted(receipts[i]["receipt_id"] for i in s)))
            )
            subset_cache[key] = minimal
            return minimal

        def feasible_remaining(idx: int) -> bool:
            """Could invitation idx still be met by the unused receipts?"""
            inv = ordered[idx]
            amount = 0
            dset = set()
            for i in eligible[idx]:
                if not used[i]:
                    amount += donations[i]
                    dset.add(donors[i])
            return (
                amount >= inv["min_donation_cents"] and len(dset) >= inv["min_donors"]
            )

        # Smallest feasible subset per invitation on the full pool (nothing
        # used yet): a lower bound on what satisfying it must cost.
        min_size = [
            min((len(s) for s in minimal_subsets(j)), default=0)
            for j in range(n_inv)
        ]

        def search(idx: int, match_so_far: int, count_so_far: int):
            if idx == n_inv:
                if match_so_far > best["total_match"] or (
                    match_so_far == best["total_match"]
                    and count_so_far < best["receipt_count"]
                ):
                    best["total_match"] = match_so_far
                    best["receipt_count"] = count_so_far
                    best["assignment"] = list(chosen)
                return
            bound = match_so_far
            min_extra = 0
            for j in range(idx, n_inv):
                if feasible_remaining(j):
                    bound += ordered[j]["match_cents"]
                    min_extra += min_size[j]
            if bound < best["total_match"]:
                return
            if bound == best["total_match"]:
                # Reaching the bound needs every still-feasible invitation,
                # i.e. at least min_extra more receipts; ties never update.
                if count_so_far + min_extra >= best["receipt_count"]:
                    return
            # Satisfying branches first: strong matches are found early and
            # sharpen the bound for everything after them.
            for subset in minimal_subsets(idx):
                for i in subset:
                    used[i] = True
                chosen[idx] = tuple(sorted(subset))
                search(
                    idx + 1,
                    match_so_far + ordered[idx]["match_cents"],
                    count_so_far + len(subset),
                )
                chosen[idx] = None
                for i in subset:
                    used[i] = False
            search(idx + 1, match_so_far, count_so_far)  # invitation sits out

        search(0, 0, 0)
        return {
            "invitations": ordered,
            "assignment": best["assignment"],
            "total_match": best["total_match"],
        }

    # --------------------------------------------------------- confirming

    def confirm_round(
        self, request_id: str, round_id: str, claims: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Confirm (请款) a planned arrangement atomically.

        Every receipt must still be ISSUED, truly permitted for the
        foundation, positive, and unsplit; every invitation must still be
        OPEN and its thresholds met.  All claims commit in one transaction —
        two agents racing the same real receipt confirm exactly one claim,
        and a committed receipt is never recounted by a later trial.
        """
        require_nonempty(request_id, "request_id")
        require_nonempty(round_id, "round_id")
        if not isinstance(claims, list):
            raise DomainError("claims must be a list")
        normalized = []
        for claim in claims:
            if not isinstance(claim, dict):
                raise DomainError("each claim must be an object")
            invitation_id = require_nonempty(
                claim.get("invitation_id"), "claim.invitation_id"
            )
            receipt_ids = claim.get("receipt_ids")
            if not isinstance(receipt_ids, list) or not receipt_ids:
                raise DomainError("claim.receipt_ids must be a non-empty list")
            for rid in receipt_ids:
                require_nonempty(rid, "receipt_id")
            normalized.append(
                {
                    "invitation_id": invitation_id,
                    "receipt_ids": sorted(set(receipt_ids)),
                }
            )
        normalized.sort(key=lambda c: c["invitation_id"])
        payload = {"round_id": round_id, "claims": normalized}
        operation = "charity.confirm_round"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            seen_invitations: set[str] = set()
            seen_receipts: set[str] = set()
            prepared = []
            for claim in normalized:
                inv_id = claim["invitation_id"]
                if inv_id in seen_invitations:
                    raise DomainError(f"invitation {inv_id} claimed twice")
                seen_invitations.add(inv_id)
                inv = conn.execute(
                    "SELECT * FROM match_invitations WHERE invitation_id=?",
                    (inv_id,),
                ).fetchone()
                if inv is None:
                    raise NotFound(f"invitation {inv_id!r} not found")
                if inv["round_id"] != round_id:
                    raise DomainError(
                        f"invitation {inv_id} belongs to round "
                        f"{inv['round_id']}, not {round_id}"
                    )
                if inv["status"] != "OPEN":
                    raise InvalidState(
                        f"invitation {inv_id} is {inv['status']}; cannot claim again"
                    )
                rows = []
                for rid in claim["receipt_ids"]:
                    if rid in seen_receipts:
                        raise DomainError(
                            f"receipt {rid} appears in two claims; a receipt's "
                            "amount is never split"
                        )
                    seen_receipts.add(rid)
                    row = conn.execute(
                        "SELECT * FROM donation_receipts WHERE receipt_id=?", (rid,)
                    ).fetchone()
                    if row is None:
                        raise NotFound(
                            f"receipt {rid!r} not found; previewed tickets are "
                            "not real receipts until the auction is cleared"
                        )
                    receipt = self._shape(row)
                    if receipt["status"] != "ISSUED":
                        raise DomainError(
                            f"receipt {rid} is {receipt['status']}; already "
                            "committed receipts cannot be recounted"
                        )
                    if receipt["donation_cents"] <= 0:
                        raise DomainError(
                            f"receipt {rid} has zero donation "
                            f"({receipt['zero_reason']}); nothing to match"
                        )
                    if inv["foundation"] not in receipt["allow"]:
                        raise DomainError(
                            f"receipt {rid} does not permit foundation "
                            f"{inv['foundation']}"
                        )
                    rows.append(receipt)
                donation_total = sum(r["donation_cents"] for r in rows)
                donors = sorted({r["donor"] for r in rows})
                if donation_total < inv["min_donation_cents"]:
                    raise DomainError(
                        f"claim for {inv['foundation']} totals {donation_total} "
                        f"< minimum {inv['min_donation_cents']} cents"
                    )
                if len(donors) < inv["min_donors"]:
                    raise DomainError(
                        f"claim for {inv['foundation']} has {len(donors)} distinct "
                        f"donors < minimum {inv['min_donors']}"
                    )
                prepared.append((inv, rows, donation_total, donors))

            at = now_utc()
            confirmed = []
            for inv, rows, donation_total, donors in prepared:
                claim_id = f"claim:{inv['invitation_id']}"
                try:
                    conn.execute(
                        "INSERT INTO match_claims(claim_id, round_id, invitation_id, "
                        "foundation, matched_cents, donation_cents, donor_count, "
                        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            claim_id,
                            round_id,
                            inv["invitation_id"],
                            inv["foundation"],
                            inv["match_cents"],
                            donation_total,
                            len(donors),
                            at,
                        ),
                    )
                    for r in rows:
                        conn.execute(
                            "INSERT INTO claim_receipts(claim_id, receipt_id) "
                            "VALUES (?, ?)",
                            (claim_id, r["receipt_id"]),
                        )
                except sqlite3.IntegrityError as exc:
                    raise DomainError(
                        "a receipt or invitation is already committed to another "
                        "confirmed claim; only one claim wins the race"
                    ) from exc
                conn.execute(
                    "UPDATE match_invitations SET status='CLAIMED' "
                    "WHERE invitation_id=?",
                    (inv["invitation_id"],),
                )
                for r in rows:
                    conn.execute(
                        "UPDATE donation_receipts SET status='COMMITTED', "
                        "committed_to=?, claim_id=?, committed_at=? "
                        "WHERE receipt_id=?",
                        (inv["foundation"], claim_id, at, r["receipt_id"]),
                    )
                confirmed.append(
                    {
                        "claim_id": claim_id,
                        "invitation_id": inv["invitation_id"],
                        "foundation": inv["foundation"],
                        "matched_cents": inv["match_cents"],
                        "donation_cents": donation_total,
                        "donor_count": len(donors),
                        "donors": donors,
                        "receipts": [
                            {
                                "receipt_id": r["receipt_id"],
                                "donor": r["donor"],
                                "donation_cents": r["donation_cents"],
                                "auction_id": r["auction_id"],
                                "bid_id": r["bid_id"],
                                "unit_index": r["unit_index"],
                            }
                            for r in rows
                        ],
                    }
                )
            response = {
                "request_id": request_id,
                "round_id": round_id,
                "state": "CONFIRMED",
                "total_match_cents": sum(c["matched_cents"] for c in confirmed),
                "receipts_used": sum(len(c["receipts"]) for c in confirmed),
                "claims": confirmed,
                "confirmed_at": at,
            }
            put_receipt(conn, request_id, operation, payload, response)
        return response

    # ------------------------------------------------------------- audit

    def round_report(self, round_id: str) -> dict[str, Any]:
        """Confirmed claims of a round, each traceable to its donors."""
        require_nonempty(round_id, "round_id")
        with self.db.read() as conn:
            invitations = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM match_invitations WHERE round_id=? "
                    "ORDER BY invitation_id",
                    (round_id,),
                )
            ]
            claims = []
            for claim in conn.execute(
                "SELECT * FROM match_claims WHERE round_id=? ORDER BY claim_id",
                (round_id,),
            ):
                receipts = [
                    self._shape(r)
                    for r in conn.execute(
                        "SELECT dr.* FROM claim_receipts cr JOIN donation_receipts "
                        "dr ON dr.receipt_id = cr.receipt_id WHERE cr.claim_id=? "
                        "ORDER BY cr.receipt_id",
                        (claim["claim_id"],),
                    )
                ]
                claims.append(
                    {
                        "claim_id": claim["claim_id"],
                        "invitation_id": claim["invitation_id"],
                        "foundation": claim["foundation"],
                        "matched_cents": claim["matched_cents"],
                        "donation_cents": claim["donation_cents"],
                        "donor_count": claim["donor_count"],
                        "donors": sorted({r["donor"] for r in receipts}),
                        "receipts": receipts,
                    }
                )
        return {
            "round_id": round_id,
            "invitations": invitations,
            "claims": claims,
            "total_match_cents": sum(c["matched_cents"] for c in claims),
        }
