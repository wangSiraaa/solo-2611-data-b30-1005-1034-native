"""Charity matching (配捐) demo: from hammer price to matched donations.

Run from the repository root:

    python3 -m examples.run_charity_demo

Walks the three boards from the task plus the extra real-order cases:

1. A bids 1@8, B bids 1@6, uniform price 6; rebates 2 and 4 -> receipts 4
   and 2.  F wants 6 cents from >= 2 donors and adds 9; G wants 4 cents from
   >= 1 donor and adds 6.  Best: both receipts to F -> 9.
2. An independent board where only B's real bid is 5: price 5, receipts 3
   and 1.  F can no longer claim; both receipts go to G -> 6.
3. The original receipts, but G raises its match to 10: only A's receipt is
   submitted to G -> 10, B's receipt stays unused.
4. One donor, two receipts: enough money, not enough distinct donors.
5. Partial fill: only the allocated prefix of the registered units issues.
6. Preview, then another seat occupies the receipt for real.

All identities and rules are synthetic; nothing touches real payment rails.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sealed_auction import (
    AuctionService,
    CharityService,
    ClearingService,
    Database,
    MarginLedger,
)
from sealed_auction.core import DomainError, InvalidState


class Harness:
    """One isolated episode: fresh database, services, request ids."""

    def __init__(self, tmpdir: str, name: str):
        self.db = Database(Path(tmpdir) / f"{name}.sqlite3")
        self.margin = MarginLedger(self.db)
        self.auctions = AuctionService(self.db, self.margin)
        self.clearing = ClearingService(self.db, self.margin, self.auctions)
        self.charity = CharityService(self.db)
        self._req = 0

    def req(self, prefix: str) -> str:
        self._req += 1
        return f"demo-{prefix}-{self._req:04d}"

    def deposit(self, buyer: str, amount: int) -> None:
        self.margin.deposit(self.req("dep"), buyer, amount)

    def bid(self, auction_id, bid_id, buyer, qty, price, charity):
        hold = self.margin.freeze(self.req("frz"), buyer, auction_id, qty * price)
        self.auctions.place_bid(
            self.req("bid"), bid_id, auction_id, buyer, qty, price,
            hold["hold_id"], charity=charity,
        )

    def settle(self, auction_id):
        self.auctions.freeze_auction(self.req("close"), auction_id)
        return self.clearing.settle(self.req("settle"), auction_id)

    def issue(self, auction_id):
        return self.charity.issue_receipts(self.req("issue"), auction_id)

    def invite(self, invitation_id, round_id, foundation, minimum, donors, match):
        return self.charity.register_invitation(
            self.req("inv"), invitation_id, round_id, foundation,
            minimum, donors, match,
        )


def show(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def print_receipts(issued) -> None:
    for r in issued["receipts"]:
        reason = f"  (zero: {r['zero_reason']})" if r["zero_reason"] else ""
        print(
            f"    {r['receipt_id']}: donor={r['donor']} "
            f"hammer={r['unit_price_cents']} rebate={r['rebate_cents']} "
            f"donation={r['donation_cents']} allow={r['allow']}{reason}"
        )


def print_plan(plan) -> None:
    print(
        f"    plan: total match={plan['total_match_cents']} "
        f"receipts used={plan['receipts_used']}"
    )
    for c in plan["claims"]:
        print(
            f"      -> {c['foundation']} via {c['invitation_id']}: "
            f"donation={c['donation_cents']} donors={c['donors']} "
            f"match={c['matched_cents']}"
        )
        for r in c["receipts"]:
            print(f"         {r['receipt_id']} ({r['donor']}: {r['donation_cents']})")
    if plan["unclaimed_invitations"]:
        print(f"    invitations sitting out: {plan['unclaimed_invitations']}")
    if plan["unused_receipts"]:
        print(f"    receipts kept in reserve: {plan['unused_receipts']}")


def board_one(h: Harness, auction_id: str):
    """A 1@8 (rebate 2), B 1@6 (rebate 4); both permitted for F and G."""
    h.deposit("A", 1_000)
    h.deposit("B", 1_000)
    h.auctions.create_auction(h.req("new"), auction_id, 2)
    h.bid(auction_id, "bid-A", "A", 1, 8,
          [{"rebate_cents": 2, "allow": ["F", "G"]}])
    h.bid(auction_id, "bid-B", "B", 1, 6,
          [{"rebate_cents": 4, "allow": ["F", "G"]}])
    frozen = h.auctions.freeze_auction(h.req("close"), auction_id)
    print(f"    uniform hammer price: {frozen['clearing_price_cents']} cents")
    h.clearing.settle(h.req("settle"), auction_id)
    issued = h.issue(auction_id)
    print_receipts(issued)
    return issued


def main() -> int:
    tmp = tempfile.TemporaryDirectory()
    try:
        show("1. board one: receipts 4 and 2 -> F takes both, match 9")
        h = Harness(tmp.name, "s1")
        print("    A bids 1@8 (rebate 2), B bids 1@6 (rebate 4); both allow F, G")
        board_one(h, "auc-1")
        h.invite("inv-F", "round-1", "F", 6, 2, 9)
        h.invite("inv-G", "round-1", "G", 4, 1, 6)
        print("    F: min 6 cents, >=2 donors, adds 9 | G: min 4, >=1 donor, adds 6")
        plan = h.charity.plan_round("round-1")
        print_plan(plan)
        confirmed = h.charity.confirm_round(
            h.req("confirm"), "round-1",
            [{"invitation_id": c["invitation_id"],
              "receipt_ids": [r["receipt_id"] for r in c["receipts"]]}
             for c in plan["claims"]],
        )
        print(f"    confirmed total match: {confirmed['total_match_cents']}")

        show("2. independent board, B's real bid is 5: receipts 3 and 1 -> G")
        h = Harness(tmp.name, "s2")
        h.deposit("A", 1_000)
        h.deposit("B", 1_000)
        h.auctions.create_auction(h.req("new"), "auc-2", 2)
        h.bid("auc-2", "bid-A", "A", 1, 8,
              [{"rebate_cents": 2, "allow": ["F", "G"]}])
        h.bid("auc-2", "bid-B", "B", 1, 5,
              [{"rebate_cents": 4, "allow": ["F", "G"]}])
        frozen = h.auctions.freeze_auction(h.req("close"), "auc-2")
        print(f"    uniform hammer price: {frozen['clearing_price_cents']} cents")
        h.clearing.settle(h.req("settle"), "auc-2")
        print_receipts(h.issue("auc-2"))
        h.invite("inv-F", "round-2", "F", 6, 2, 9)
        h.invite("inv-G", "round-2", "G", 4, 1, 6)
        plan = h.charity.plan_round("round-2")
        print("    F needs 6 cents but the board holds only 4 -> F cannot claim")
        print_plan(plan)

        show("3. original receipts, G raises its match to 10 -> only A goes")
        h = Harness(tmp.name, "s3")
        board_one(h, "auc-3")
        h.invite("inv-F", "round-3", "F", 6, 2, 9)
        h.invite("inv-G", "round-3", "G", 4, 1, 10)
        print("    F: min 6, >=2 donors, adds 9 | G: min 4, >=1 donor, adds 10")
        plan = h.charity.plan_round("round-3")
        print_plan(plan)
        print("    same match with fewer receipts wins: B's receipt stays in reserve")

        show("4. one donor, two receipts: money suffices, headcount does not")
        h = Harness(tmp.name, "s4")
        h.deposit("A", 10_000)
        h.auctions.create_auction(h.req("new"), "auc-4", 2)
        h.bid("auc-4", "bid-A", "A", 2, 6,
              [{"rebate_cents": 2, "allow": ["F", "G"]},
               {"rebate_cents": 3, "allow": ["F", "G"]}])
        h.settle("auc-4")
        print_receipts(h.issue("auc-4"))
        h.invite("inv-F", "round-4", "F", 6, 2, 9)
        h.invite("inv-G", "round-4", "G", 4, 1, 5)
        print("    A's two receipts sum to 7 >= 6, but the donor count stays 1")
        print_plan(h.charity.plan_round("round-4"))

        show("5. partial fill: only the allocated prefix of units issues")
        h = Harness(tmp.name, "s5")
        h.deposit("A", 10_000)
        h.auctions.create_auction(h.req("new"), "auc-5", 2)
        h.bid("auc-5", "bid-A", "A", 3, 5,
              [{"rebate_cents": 1, "allow": ["F"]},
               {"rebate_cents": 2, "allow": ["F"]},
               {"rebate_cents": 3, "allow": ["F"]}])
        frozen = h.auctions.freeze_auction(h.req("close"), "auc-5")
        print(f"    bid qty 3, allocated {frozen['allocations'][0]['allocated_qty']}")
        h.clearing.settle(h.req("settle"), "auc-5")
        issued = h.issue("auc-5")
        print_receipts(issued)
        print("    the 3rd registered unit never traded: no receipt for it")

        show("6. preview, then another seat occupies the receipt for real")
        h = Harness(tmp.name, "s6")
        h.deposit("A", 1_000)
        h.deposit("B", 1_000)
        h.auctions.create_auction(h.req("new"), "auc-6", 2)
        h.bid("auc-6", "bid-A", "A", 1, 8,
              [{"rebate_cents": 2, "allow": ["F", "G"]}])
        h.bid("auc-6", "bid-B", "B", 1, 6,
              [{"rebate_cents": 4, "allow": ["F", "G"]}])
        h.auctions.freeze_auction(h.req("close"), "auc-6")
        preview = h.charity.preview_receipts("auc-6")
        print(f"    ranked, not yet charged: {preview['note']}")
        for r in preview["receipts"]:
            print(f"      {r['receipt_id']}: would donate {r['donation_cents']}")
        try:
            h.issue("auc-6")
        except InvalidState as exc:
            print(f"    issue refused before clearing: {exc}")
        h.settle("auc-6")
        issued = h.issue("auc-6")
        print_receipts(issued)
        h.invite("inv-F", "round-6", "F", 6, 2, 9)
        h.invite("inv-G", "round-6", "G", 4, 1, 6)
        plan = h.charity.plan_round("round-6")
        print("    our trial says F should take both:")
        print_plan(plan)
        a_rid = "rcpt:auc-6:bid-A:1"
        other = h.charity.confirm_round(
            h.req("confirm"), "round-6",
            [{"invitation_id": "inv-G", "receipt_ids": [a_rid]}],
        )
        print(f"    another seat confirms A's receipt to G first: "
              f"match {other['total_match_cents']}")
        try:
            h.charity.confirm_round(
                h.req("confirm"), "round-6",
                [{"invitation_id": "inv-F",
                  "receipt_ids": [a_rid, "rcpt:auc-6:bid-B:1"]}],
            )
        except DomainError as exc:
            print(f"    our stale plan is rejected: {exc}")
        replan = h.charity.plan_round("round-6")
        print("    fresh trial never recounts the committed receipt:")
        print_plan(replan)
        return 0
    finally:
        tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
