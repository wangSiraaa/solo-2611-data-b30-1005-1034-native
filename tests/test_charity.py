"""Scenario tests for charity matching (配捐) on settled auctions."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from sealed_auction import (
    AuctionService,
    CharityService,
    ClearingService,
    Database,
    MarginLedger,
)
from sealed_auction.core import DomainError, InvalidState, LimitExceeded, NotFound
from sealed_auction.reconcile import assert_reconciled


class Harness:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "charity.sqlite3")
        self.margin = MarginLedger(self.db)
        self.auctions = AuctionService(self.db, self.margin)
        self.clearing = ClearingService(self.db, self.margin, self.auctions)
        self.charity = CharityService(self.db)
        self._req = 0

    def req(self, prefix="req"):
        self._req += 1
        return f"{prefix}-{self._req:04d}"

    def deposit(self, buyer, amount):
        return self.margin.deposit(self.req("dep"), buyer, amount)

    def create(self, auction_id, units):
        return self.auctions.create_auction(self.req("new"), auction_id, units)

    def bid(self, bid_id, auction_id, buyer, qty, price, charity=None):
        hold = self.margin.freeze(self.req("frz"), buyer, auction_id, qty * price)
        return self.auctions.place_bid(
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

    def close(self):
        self._tmp.cleanup()


def scenario_one_receipts(h: Harness, auction_id="auc-1"):
    """The prompt's first board: A 1@8 (rebate 2), B 1@6 (rebate 4), price 6."""
    h.deposit("A", 1_000)
    h.deposit("B", 1_000)
    h.create(auction_id, 2)
    h.bid("bid-A", auction_id, "A", 1, 8,
          charity=[{"rebate_cents": 2, "allow": ["F", "G"]}])
    h.bid("bid-B", auction_id, "B", 1, 6,
          charity=[{"rebate_cents": 4, "allow": ["F", "G"]}])
    frozen = h.auctions.freeze_auction(h.req("close"), auction_id)
    assert frozen["clearing_price_cents"] == 6
    h.clearing.settle(h.req("settle"), auction_id)
    issued = h.issue(auction_id)
    by_donor = {r["donor"]: r for r in issued["receipts"]}
    return by_donor


class CharityScenarioTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    # ------------------------------------- prompt scenario 1: F takes both

    def test_scenario_one_f_takes_both_receipts(self):
        h = self.h
        by_donor = scenario_one_receipts(h)
        # Hammer 6, rebates deducted per unit first: 6-2=4 and 6-4=2.
        self.assertEqual(by_donor["A"]["donation_cents"], 4)
        self.assertEqual(by_donor["B"]["donation_cents"], 2)
        self.assertEqual(by_donor["A"]["unit_price_cents"], 6)
        self.assertEqual(by_donor["A"]["allow"], ["F", "G"])
        self.assertIsNone(by_donor["A"]["zero_reason"])

        h.invite("inv-F", "round-1", "F", 6, 2, 9)   # 6 cents, 2 donors, +9
        h.invite("inv-G", "round-1", "G", 4, 1, 6)   # 4 cents, 1 donor, +6

        plan = h.charity.plan_round("round-1")
        self.assertEqual(plan["state"], "PLANNED")
        self.assertEqual(plan["total_match_cents"], 9)
        self.assertEqual(plan["receipts_used"], 2)
        self.assertEqual(len(plan["claims"]), 1)
        claim = plan["claims"][0]
        self.assertEqual(claim["foundation"], "F")
        self.assertEqual(claim["donation_cents"], 6)
        self.assertEqual(claim["donors"], ["A", "B"])
        self.assertEqual(plan["unclaimed_invitations"], ["inv-G"])

        # Planning is a non-binding trial: receipts stay ISSUED.
        self.assertEqual(
            {r["status"] for r in h.charity.list_receipts()}, {"ISSUED"}
        )

        confirmed = h.charity.confirm_round(
            h.req("confirm"), "round-1",
            [
                {
                    "invitation_id": c["invitation_id"],
                    "receipt_ids": [r["receipt_id"] for r in c["receipts"]],
                }
                for c in plan["claims"]
            ],
        )
        self.assertEqual(confirmed["state"], "CONFIRMED")
        self.assertEqual(confirmed["total_match_cents"], 9)

        # Donor-traceable list for the foundation.
        report = h.charity.round_report("round-1")
        self.assertEqual(report["total_match_cents"], 9)
        f_claim = report["claims"][0]
        self.assertEqual(f_claim["foundation"], "F")
        self.assertEqual(f_claim["donors"], ["A", "B"])
        self.assertEqual(
            {r["donor"] for r in f_claim["receipts"]}, {"A", "B"}
        )
        for r in h.charity.list_receipts():
            self.assertEqual(r["status"], "COMMITTED")
            self.assertEqual(r["committed_to"], "F")
        assert_reconciled(h.db)

    # ------------------- prompt scenario 2: independent board, lower price

    def test_scenario_two_lower_price_only_g_can_claim(self):
        h = self.h
        h.deposit("A", 1_000)
        h.deposit("B", 1_000)
        h.create("auc-2", 2)
        # Only B's real bid differs from scenario 1 (6 -> 5): price becomes 5.
        h.bid("bid-A", "auc-2", "A", 1, 8,
              charity=[{"rebate_cents": 2, "allow": ["F", "G"]}])
        h.bid("bid-B", "auc-2", "B", 1, 5,
              charity=[{"rebate_cents": 4, "allow": ["F", "G"]}])
        frozen = h.auctions.freeze_auction(h.req("close"), "auc-2")
        self.assertEqual(frozen["clearing_price_cents"], 5)
        h.clearing.settle(h.req("settle"), "auc-2")
        issued = h.issue("auc-2")
        donations = {r["donor"]: r["donation_cents"] for r in issued["receipts"]}
        self.assertEqual(donations, {"A": 3, "B": 1})  # 5-2, 5-4

        h.invite("inv-F", "round-2", "F", 6, 2, 9)
        h.invite("inv-G", "round-2", "G", 4, 1, 6)
        plan = h.charity.plan_round("round-2")
        # F needs 6 cents but the board only holds 4: F cannot claim.
        self.assertEqual(plan["total_match_cents"], 6)
        self.assertEqual(len(plan["claims"]), 1)
        claim = plan["claims"][0]
        self.assertEqual(claim["foundation"], "G")
        self.assertEqual(claim["donation_cents"], 4)
        self.assertEqual(claim["donor_count"], 2)
        self.assertEqual(plan["unclaimed_invitations"], ["inv-F"])
        assert_reconciled(h.db)

    # ------------- prompt scenario 3: raised match, one big receipt decides

    def test_scenario_three_raised_match_single_receipt_to_g(self):
        h = self.h
        by_donor = scenario_one_receipts(h)  # original receipts 4 and 2
        h.invite("inv-F", "round-3", "F", 6, 2, 9)
        h.invite("inv-G", "round-3", "G", 4, 1, 10)  # match raised to 10

        plan = h.charity.plan_round("round-3")
        self.assertEqual(plan["total_match_cents"], 10)
        # Same match with fewer receipts wins the tie-break: only A's 4 goes.
        self.assertEqual(plan["receipts_used"], 1)
        claim = plan["claims"][0]
        self.assertEqual(claim["foundation"], "G")
        self.assertEqual(claim["donation_cents"], 4)
        self.assertEqual(claim["donors"], ["A"])
        # B's receipt stays unused (留用).
        self.assertEqual(plan["unused_receipts"],
                         [by_donor["B"]["receipt_id"]])

        h.charity.confirm_round(
            h.req("confirm"), "round-3",
            [
                {
                    "invitation_id": claim["invitation_id"],
                    "receipt_ids": [r["receipt_id"] for r in claim["receipts"]],
                }
            ],
        )
        b_receipt = h.charity.list_receipts(status="ISSUED")
        self.assertEqual(
            [r["receipt_id"] for r in b_receipt], [by_donor["B"]["receipt_id"]]
        )
        assert_reconciled(h.db)

    # ----------------- same donor, enough money but not enough headcount

    def test_same_donor_two_receipts_fail_the_donor_headcount(self):
        h = self.h
        h.deposit("A", 10_000)
        h.create("auc-solo", 2)
        h.bid("bid-A", "auc-solo", "A", 2, 6,
              charity=[
                  {"rebate_cents": 2, "allow": ["F", "G"]},
                  {"rebate_cents": 3, "allow": ["F", "G"]},
              ])
        h.settle("auc-solo")
        issued = h.issue("auc-solo")
        self.assertEqual(
            sorted(r["donation_cents"] for r in issued["receipts"]), [3, 4]
        )
        self.assertEqual({r["donor"] for r in issued["receipts"]}, {"A"})

        h.invite("inv-F", "round-solo", "F", 6, 2, 9)  # needs 2 donors
        h.invite("inv-G", "round-solo", "G", 4, 1, 5)  # needs 1 donor
        plan = h.charity.plan_round("round-solo")
        # A's two receipts sum to 7 >= 6, but the donor count stays 1:
        # F is not claimable.  G takes the single 4-cent receipt (tie-break:
        # one receipt suffices, the second would only add cost).
        self.assertEqual(plan["total_match_cents"], 5)
        self.assertEqual(len(plan["claims"]), 1)
        claim = plan["claims"][0]
        self.assertEqual(claim["foundation"], "G")
        self.assertEqual(claim["donor_count"], 1)
        self.assertEqual(claim["donation_cents"], 4)
        self.assertEqual(plan["receipts_used"], 1)
        self.assertEqual(plan["unclaimed_invitations"], ["inv-F"])

        # Even a hand-rolled claim cannot talk its way past the headcount.
        both = [r["receipt_id"] for r in issued["receipts"]]
        with self.assertRaises(DomainError):
            h.charity.confirm_round(
                h.req("confirm"), "round-solo",
                [{"invitation_id": "inv-F", "receipt_ids": both}],
            )
        assert_reconciled(h.db)

    # ------------------------- partial fill: only the allocated prefix

    def test_partial_fill_only_allocated_units_get_receipts(self):
        h = self.h
        h.deposit("A", 10_000)
        h.create("auc-part", 2)  # only 2 units on the block
        h.bid("bid-A", "auc-part", "A", 3, 5,
              charity=[
                  {"rebate_cents": 1, "allow": ["F"]},
                  {"rebate_cents": 2, "allow": ["F"]},
                  {"rebate_cents": 3, "allow": ["F"]},
              ])
        frozen = h.auctions.freeze_auction(h.req("close"), "auc-part")
        self.assertEqual(frozen["allocations"][0]["allocated_qty"], 2)  # of 3
        h.clearing.settle(h.req("settle"), "auc-part")
        issued = h.issue("auc-part")
        # The third registered unit never traded: no receipt for it.
        self.assertEqual(len(issued["receipts"]), 2)
        self.assertEqual(
            [(r["unit_index"], r["donation_cents"]) for r in issued["receipts"]],
            [(1, 4), (2, 3)],  # price 5 minus rebates 1 and 2
        )
        self.assertEqual(
            [r["receipt_id"] for r in issued["receipts"]],
            ["rcpt:auc-part:bid-A:1", "rcpt:auc-part:bid-A:2"],
        )
        assert_reconciled(h.db)

    # -------- preview, then another seat occupies the receipt for real

    def test_preview_then_other_seat_occupies_receipt(self):
        h = self.h
        by_donor = scenario_one_receipts(h)
        h.invite("inv-F", "round-x", "F", 6, 2, 9)
        h.invite("inv-G", "round-x", "G", 4, 1, 6)

        plan = h.charity.plan_round("round-x")
        self.assertEqual(plan["claims"][0]["foundation"], "F")
        f_receipts = [r["receipt_id"] for r in plan["claims"][0]["receipts"]]

        # Another seat moves first and commits A's receipt to G.
        other = h.charity.confirm_round(
            h.req("confirm"), "round-x",
            [{"invitation_id": "inv-G",
              "receipt_ids": [by_donor["A"]["receipt_id"]]}],
        )
        self.assertEqual(other["total_match_cents"], 6)

        # The stale plan cannot be confirmed: the receipt is already taken.
        with self.assertRaises(DomainError):
            h.charity.confirm_round(
                h.req("confirm"), "round-x",
                [{"invitation_id": "inv-F", "receipt_ids": f_receipts}],
            )

        # A fresh trial of the same round never recounts the committed
        # receipt: only B's 2 cents remain, so nothing more can be claimed.
        replan = h.charity.plan_round("round-x")
        self.assertEqual(replan["total_match_cents"], 0)
        self.assertEqual(replan["claims"], [])
        self.assertEqual(
            replan["unused_receipts"], [by_donor["B"]["receipt_id"]]
        )
        # The committed receipt still shows where it went.
        committed = h.charity.list_receipts(status="COMMITTED")
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0]["committed_to"], "G")
        self.assertEqual(committed[0]["donor"], "A")
        assert_reconciled(h.db)

    # ------------- ranked but not yet charged: preview only, never real

    def test_ranked_not_charged_tickets_are_preview_only(self):
        h = self.h
        h.deposit("A", 1_000)
        h.deposit("B", 1_000)
        h.create("auc-pre", 2)
        h.bid("bid-A", "auc-pre", "A", 1, 8,
              charity=[{"rebate_cents": 2, "allow": ["F"]}])
        h.bid("bid-B", "auc-pre", "B", 1, 6,
              charity=[{"rebate_cents": 4, "allow": ["F"]}])
        h.auctions.freeze_auction(h.req("close"), "auc-pre")

        preview = h.charity.preview_receipts("auc-pre")
        self.assertEqual(preview["settlement_status"], "FROZEN")
        self.assertEqual(
            {r["state"] for r in preview["receipts"]}, {"PREVIEW"}
        )
        self.assertEqual(
            sorted(r["donation_cents"] for r in preview["receipts"]), [2, 4]
        )
        # Preview rows are not real receipts yet.
        self.assertEqual(h.charity.list_receipts(), [])
        with self.assertRaises(InvalidState):
            h.issue("auc-pre")
        h.invite("inv-F", "round-pre", "F", 4, 1, 5)
        with self.assertRaises(NotFound):
            h.charity.confirm_round(
                h.req("confirm"), "round-pre",
                [{"invitation_id": "inv-F",
                  "receipt_ids": ["rcpt:auc-pre:bid-A:1"]}],
            )
        self.assertEqual(h.charity.plan_round("round-pre")["total_match_cents"], 0)

        # Once cleared, the same tickets become real, claimable receipts.
        h.clearing.settle(h.req("settle"), "auc-pre")
        issued = h.issue("auc-pre")
        self.assertEqual(len(issued["receipts"]), 2)
        after = h.charity.preview_receipts("auc-pre")
        self.assertEqual({r["state"] for r in after["receipts"]}, {"ISSUED"})
        plan = h.charity.plan_round("round-pre")
        self.assertEqual(plan["total_match_cents"], 5)
        assert_reconciled(h.db)

    # ----------------------- two agents race one real receipt

    def test_concurrent_confirms_only_one_claim_wins_the_receipt(self):
        h = self.h
        by_donor = scenario_one_receipts(h)
        h.invite("inv-F", "round-c", "F", 6, 2, 9)
        h.invite("inv-G", "round-c", "G", 4, 1, 6)
        both = sorted(r["receipt_id"] for r in by_donor.values())
        a_only = [by_donor["A"]["receipt_id"]]

        outcomes = []
        barrier = threading.Barrier(2)

        def agent(tag, claims):
            barrier.wait()
            try:
                res = h.charity.confirm_round(f"race-{tag}", "round-c", claims)
                outcomes.append(("ok", tag, res["total_match_cents"]))
            except DomainError:
                outcomes.append(("rejected", tag, None))

        threads = [
            threading.Thread(
                target=agent,
                args=("F", [{"invitation_id": "inv-F", "receipt_ids": both}]),
            ),
            threading.Thread(
                target=agent,
                args=("G", [{"invitation_id": "inv-G", "receipt_ids": a_only}]),
            ),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        oks = [o for o in outcomes if o[0] == "ok"]
        rejected = [o for o in outcomes if o[0] == "rejected"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(rejected), 1)
        # Exactly one confirmed claim exists and each receipt supports it once.
        report = h.charity.round_report("round-c")
        self.assertEqual(len(report["claims"]), 1)
        with h.db.read() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM claim_receipts"
            ).fetchone()["n"]
        committed = h.charity.list_receipts(status="COMMITTED")
        self.assertEqual(n, len(committed))
        self.assertIn(len(committed), (1, 2))
        assert_reconciled(h.db)

    # --------------------------- zero donation keeps its reason

    def test_zero_donation_receipt_keeps_reason_and_is_unclaimable(self):
        h = self.h
        h.deposit("A", 1_000)
        h.create("auc-zero", 1)
        h.bid("bid-A", "auc-zero", "A", 1, 6,
              charity=[{"rebate_cents": 6, "allow": ["G"]}])  # rebate >= price
        h.settle("auc-zero")
        issued = h.issue("auc-zero")
        receipt = issued["receipts"][0]
        self.assertEqual(receipt["donation_cents"], 0)
        self.assertEqual(receipt["zero_reason"], "rebate_covers_price")
        self.assertEqual(receipt["unit_price_cents"], 6)
        self.assertEqual(receipt["rebate_cents"], 6)

        h.invite("inv-G", "round-z", "G", 1, 1, 5)
        plan = h.charity.plan_round("round-z")
        self.assertEqual(plan["total_match_cents"], 0)
        with self.assertRaises(DomainError):
            h.charity.confirm_round(
                h.req("confirm"), "round-z",
                [{"invitation_id": "inv-G",
                  "receipt_ids": [receipt["receipt_id"]]}],
            )
        assert_reconciled(h.db)

    # ------------------------- permissions are honoured to the letter

    def test_receipt_without_permission_cannot_be_submitted(self):
        h = self.h
        h.deposit("A", 1_000)
        h.create("auc-perm", 1)
        h.bid("bid-A", "auc-perm", "A", 1, 8,
              charity=[{"rebate_cents": 2, "allow": ["F"]}])  # F only
        h.settle("auc-perm")
        issued = h.issue("auc-perm")
        rid = issued["receipts"][0]["receipt_id"]

        h.invite("inv-G", "round-p", "G", 4, 1, 6)
        plan = h.charity.plan_round("round-p")
        self.assertEqual(plan["total_match_cents"], 0)  # planner never offers it
        with self.assertRaises(DomainError):
            h.charity.confirm_round(
                h.req("confirm"), "round-p",
                [{"invitation_id": "inv-G", "receipt_ids": [rid]}],
            )
        assert_reconciled(h.db)

    def test_receipt_cannot_be_split_or_committed_twice(self):
        h = self.h
        by_donor = scenario_one_receipts(h)
        h.invite("inv-F", "round-s", "F", 6, 2, 9)
        h.invite("inv-G", "round-s", "G", 4, 1, 6)
        a_rid = by_donor["A"]["receipt_id"]
        b_rid = by_donor["B"]["receipt_id"]

        # One receipt inside two claims of the same request: rejected.
        with self.assertRaises(DomainError):
            h.charity.confirm_round(
                h.req("confirm"), "round-s",
                [
                    {"invitation_id": "inv-F", "receipt_ids": [a_rid, b_rid]},
                    {"invitation_id": "inv-G", "receipt_ids": [a_rid]},
                ],
            )
        # Properly committed to F; a second commitment to G is rejected.
        h.charity.confirm_round(
            h.req("confirm"), "round-s",
            [{"invitation_id": "inv-F", "receipt_ids": [a_rid, b_rid]}],
        )
        with self.assertRaises(DomainError):
            h.charity.confirm_round(
                h.req("confirm"), "round-s",
                [{"invitation_id": "inv-G", "receipt_ids": [a_rid]}],
            )
        report = h.charity.round_report("round-s")
        self.assertEqual(report["total_match_cents"], 9)
        self.assertEqual(len(report["claims"]), 1)
        assert_reconciled(h.db)

    # ------------------------------------- idempotent retries

    def test_issue_and_confirm_are_idempotent(self):
        h = self.h
        by_donor = scenario_one_receipts(h)
        h.invite("inv-G", "round-i", "G", 4, 1, 6)
        rid = by_donor["A"]["receipt_id"]

        issue_again = h.issue("auc-1")
        self.assertEqual(len(issue_again["receipts"]), 2)  # not duplicated

        confirm_id = h.req("confirm")
        claims = [{"invitation_id": "inv-G", "receipt_ids": [rid]}]
        first = h.charity.confirm_round(confirm_id, "round-i", claims)
        second = h.charity.confirm_round(confirm_id, "round-i", claims)
        self.assertEqual(first, second)
        report = h.charity.round_report("round-i")
        self.assertEqual(len(report["claims"]), 1)
        self.assertEqual(report["total_match_cents"], 6)
        assert_reconciled(h.db)

    # ------------------------------------------------- limits

    def test_at_most_four_foundations_per_round(self):
        h = self.h
        for i in range(4):
            h.invite(f"inv-{i}", "round-lim", f"F{i}", 1, 1, 1)
        with self.assertRaises(LimitExceeded):
            h.invite("inv-5", "round-lim", "F5", 1, 1, 1)
        # A fifth invitation for an already-present foundation is fine.
        h.invite("inv-4b", "round-lim", "F0", 2, 1, 3)

    def test_at_most_twenty_receipts_per_round(self):
        h = self.h
        h.deposit("A", 1_000_000)
        # Three auctions (deployment cap) of 10 + 10 + 1 units -> 21 receipts.
        for aid, units in (("auc-n1", 10), ("auc-n2", 10), ("auc-n3", 1)):
            h.create(aid, units)
            h.bid(f"bid-{aid}", aid, "A", units, 5,
                  charity=[{"rebate_cents": 1, "allow": ["F"]}] * units)
            h.settle(aid)
            h.issue(aid)
        self.assertEqual(len(h.charity.list_receipts()), 21)
        h.invite("inv-F", "round-n", "F", 1, 1, 1)
        with self.assertRaises(LimitExceeded):
            h.charity.plan_round("round-n")
        assert_reconciled(h.db)

    # --------------------- registrations travel with revisions

    def test_revision_replaces_charity_registration(self):
        h = self.h
        h.deposit("A", 10_000)
        h.create("auc-rev", 1)
        first = h.bid("bid-A", "auc-rev", "A", 1, 8,
                      charity=[{"rebate_cents": 5, "allow": ["F"]}])
        new_hold = h.margin.freeze(h.req("frz"), "A", "auc-rev", 9)
        h.auctions.revise_bid(
            h.req("rev"), "bid-A", "A", 1, 9, new_hold["hold_id"],
            charity=[{"rebate_cents": 1, "allow": ["G"]}],
        )
        h.settle("auc-rev")
        issued = h.issue("auc-rev")
        receipt = issued["receipts"][0]
        self.assertEqual(receipt["rebate_cents"], 1)
        self.assertEqual(receipt["allow"], ["G"])
        self.assertEqual(receipt["donation_cents"], 8)  # price 9 - rebate 1
        self.assertEqual(
            h.margin.get_hold(first["hold_id"])["status"], "RELEASED"
        )
        assert_reconciled(h.db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
