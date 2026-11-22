"""End-to-end tests for the fictional print shop (imposition work orders).

The shop sits behind the auction: plates can only be registered for settled
winning originals, and every pulled label is accounted as delivered to its
own buyer or torn on the spot.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sealed_auction import (
    AuctionService,
    ClearingService,
    Database,
    MarginLedger,
    PressShop,
)
from sealed_auction.core import DomainError, InvalidState, PressInfeasible
from sealed_auction.press_plan import (
    NORMAL,
    REVERSED,
    effective_holes,
    plan_job,
    prove_mount,
)
from sealed_auction.press import reconcile_press
from sealed_auction.press_plan import Plate
from sealed_auction.reconcile import assert_reconciled


class PressHarness:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "auction.sqlite3")
        self.margin = MarginLedger(self.db)
        self.auctions = AuctionService(self.db, self.margin)
        self.clearing = ClearingService(self.db, self.margin, self.auctions)
        self.press = PressShop(self.db, self.auctions)
        self._req = 0

    def req(self, prefix="req"):
        self._req += 1
        return f"{prefix}-{self._req:04d}"

    def deposit(self, buyer, amount):
        return self.margin.deposit(self.req("dep"), buyer, amount)

    def settle_auction(self, auction_id, units, bids):
        """bids: list of (bid_id, buyer, qty, price). Runs the full auction."""
        self.auctions.create_auction(self.req("new"), auction_id, units)
        for bid_id, buyer, qty, price in bids:
            hold = self.margin.freeze(
                self.req("frz"), buyer, auction_id, qty * price
            )
            self.auctions.place_bid(
                self.req("bid"), bid_id, auction_id, buyer, qty, price, hold["hold_id"]
            )
        self.auctions.freeze_auction(self.req("close"), auction_id)
        self.clearing.settle(self.req("settle"), auction_id)

    def plate(self, plate_id, auction_id, buyer, heads, holes, flippable=False):
        return self.press.register_plate(
            self.req("plate"), plate_id, auction_id, buyer, heads, holes, flippable
        )

    def close(self):
        self._tmp.cleanup()


def plate(plate_id, qty, heads, holes, flippable=False, auction="auc", buyer=None):
    return Plate(
        plate_id=plate_id,
        auction_id=auction,
        buyer=buyer or plate_id,
        qty=qty,
        allowed_heads=frozenset(heads),
        holes=tuple(holes),
        flippable=flippable,
    )


class PressScenarioTest(unittest.TestCase):
    def setUp(self):
        self.h = PressHarness()

    def tearDown(self):
        self.h.close()

    # ------------------------------ 1. compatible two-up, one work order

    def test_matching_hole_trains_mount_two_up_zero_waste(self):
        h = self.h
        h.deposit("A", 10_000)
        h.deposit("B", 10_000)
        h.settle_auction("a1", 4, [("b-a", "A", 2, 500), ("b-b", "B", 2, 400)])
        h.plate("plate-a", "a1", "A", ["L"], [1, 0])
        h.plate("plate-b", "a1", "B", ["R"], [1, 0])

        job = h.press.plan_job(self.h.req("job"), "job-1",
                               ["plate-a", "plate-b"], paper_limit=4, max_changes=1)
        self.assertEqual(job["paper_used"], 2)
        self.assertEqual(job["changes_used"], 0)
        self.assertEqual(job["torn_total"], 0)
        self.assertEqual(len(job["batches"]), 1)
        batch = job["batches"][0]
        self.assertEqual(batch["sheets"], 2)
        self.assertEqual(batch["left"]["plate_id"], "plate-a")
        self.assertEqual(batch["right"]["plate_id"], "plate-b")
        # Actual mounting directions are kept on the work order.
        self.assertEqual(batch["left"]["orientation"], NORMAL)
        self.assertEqual(batch["right"]["orientation"], NORMAL)
        self.assertEqual(batch["production"]["plate-a"]["delivered"], 2)
        self.assertEqual(batch["production"]["plate-b"]["delivered"], 2)

        pulled = h.press.pull_job(self.h.req("pull"), "job-1")
        self.assertEqual(pulled["status"], "PULLED")
        self.assertEqual(h.press.buyer_account("A")["delivered_labels"], 2)
        self.assertEqual(h.press.buyer_account("B")["delivered_labels"], 2)
        self.assertEqual(reconcile_press(h.db)["ok"], True)
        assert_reconciled(h.db)

    # ------------------------------ 2. hole mismatch forces single heads

    def test_changed_plate_hole_train_blocks_two_up(self):
        h = self.h
        h.deposit("A", 10_000)
        h.deposit("B", 10_000)
        h.settle_auction("a2", 4, [("b-a", "A", 2, 500), ("b-b", "B", 2, 400)])
        h.plate("plate-a", "a2", "A", ["L"], [1, 0])
        # B's signed plate is changed to 01 and cannot flip.
        h.plate("plate-b", "a2", "B", ["R"], [0, 1], flippable=False)

        proof = h.press.trial_proof(
            {"plate_id": "plate-a", "orientation": NORMAL},
            {"plate_id": "plate-b", "orientation": NORMAL},
        )
        self.assertFalse(proof["registered"])
        self.assertEqual(proof["mismatch_positions"], [0, 1])

        job = h.press.plan_job(self.h.req("job"), "job-2",
                               ["plate-a", "plate-b"], paper_limit=4, max_changes=1)
        self.assertEqual(job["torn_total"], 0)
        self.assertEqual(job["paper_used"], 4)
        # Every batch is a single-head mount.
        for b in job["batches"]:
            self.assertIsNone(b["left"] if b["right"] else b["right"])
        h.press.pull_job(self.h.req("pull"), "job-2")
        self.assertEqual(h.press.buyer_account("A")["delivered_labels"], 2)
        self.assertEqual(h.press.buyer_account("B")["delivered_labels"], 2)
        self.assertEqual(reconcile_press(h.db)["ok"], True)

    def test_single_sheet_cannot_deliver_incompatible_pair(self):
        a = plate("A", 2, ["L"], [1, 0])
        b = plate("B", 2, ["R"], [0, 1])
        with self.assertRaises(PressInfeasible):
            plan_job([a, b], paper_limit=1, max_changes=1)

    # ------------------------------ 3. zero-change waste vs one-change split

    def test_zero_change_overprints_and_tears(self):
        h = self.h
        h.deposit("A", 10_000)
        h.deposit("B", 10_000)
        h.settle_auction("a3", 4, [("b-a", "A", 3, 500), ("b-b", "B", 1, 400)])
        h.plate("plate-a", "a3", "A", ["L"], [1, 0])
        h.plate("plate-b", "a3", "B", ["R"], [1, 0])

        job = h.press.plan_job(self.h.req("job"), "job-3a",
                               ["plate-a", "plate-b"], paper_limit=3, max_changes=0)
        self.assertEqual(job["paper_used"], 3)
        self.assertEqual(job["changes_used"], 0)
        self.assertEqual(job["torn_total"], 2)
        self.assertEqual(job["torn_by_plate"]["plate-b"], 2)
        h.press.pull_job(self.h.req("pull"), "job-3a")
        # The torn labels stay with B; they are never transferred to A.
        account_b = h.press.buyer_account("B")
        self.assertEqual(account_b["delivered_labels"], 1)
        self.assertEqual(account_b["torn_labels"], 2)
        self.assertEqual(h.press.buyer_account("A")["torn_labels"], 0)
        self.assertEqual(reconcile_press(h.db)["ok"], True)

    def test_one_change_splits_two_up_then_single_head_without_waste(self):
        a = plate("A", 3, ["L"], [1, 0])
        b = plate("B", 1, ["R"], [1, 0])
        plan = plan_job([a, b], paper_limit=4, max_changes=1)
        self.assertEqual(plan.torn_total, 0)
        self.assertEqual(plan.paper_used, 3)
        self.assertEqual(plan.changes_used, 1)
        self.assertEqual(len(plan.batches), 2)
        first, second = plan.batches
        self.assertTrue(first.mount.is_two_up())
        self.assertEqual(first.sheets, 1)
        # The identical single-head setup is kept for two sheets in one batch;
        # repeating the same mount is not another plate change.
        self.assertFalse(second.mount.is_two_up())
        self.assertEqual(second.sheets, 2)
        self.assertTrue(second.is_change)

    # ------------------------------ 4. flip / reverse registration

    def test_reverse_mount_mirrors_hole_train_and_requires_flippable(self):
        self.assertEqual(effective_holes((1, 0, 1), REVERSED), (1, 0, 1))
        self.assertEqual(effective_holes((1, 1, 0), REVERSED), (0, 1, 1))
        a = plate("A", 2, ["L"], [1, 0], flippable=True)
        b = plate("B", 2, ["R"], [0, 1], flippable=True)
        plates = {p.plate_id: p for p in (a, b)}
        # A normal vs B normal mismatches; flipping B mirrors 01 -> 10.
        bad = prove_mount(plates,
                          {"plate_id": "A", "orientation": NORMAL},
                          {"plate_id": "B", "orientation": NORMAL})
        self.assertFalse(bad["registered"])
        good = prove_mount(plates,
                           {"plate_id": "A", "orientation": NORMAL},
                           {"plate_id": "B", "orientation": REVERSED})
        self.assertTrue(good["registered"])
        self.assertEqual(good["right"]["effective_holes"], [1, 0])

        locked = plate("C", 1, ["L"], [0, 1], flippable=False)
        with self.assertRaises(DomainError):
            prove_mount({**plates, "C": locked},
                        {"plate_id": "C", "orientation": REVERSED}, None)

    # ------------------------------ 5. registration gates

    def test_plate_requires_settled_winning_original(self):
        h = self.h
        h.deposit("A", 10_000)
        h.deposit("B", 10_000)
        h.auctions.create_auction(h.req("new"), "a4", 2)
        hold = h.margin.freeze(h.req("frz"), "A", "a4", 2 * 100)
        h.auctions.place_bid(h.req("bid"), "b-a", "a4", "A", 2, 100, hold["hold_id"])
        # Auction still open: no plate can be registered.
        with self.assertRaises(InvalidState):
            h.plate("plate-a", "a4", "A", ["L"], [1, 0])
        h.auctions.freeze_auction(h.req("close"), "a4")
        # Frozen but the original clearing is not done yet.
        with self.assertRaises(InvalidState):
            h.plate("plate-a", "a4", "A", ["L"], [1, 0])
        h.clearing.settle(h.req("settle"), "a4")
        h.plate("plate-a", "a4", "A", ["L"], [1, 0])

        # A buyer with no allocation cannot get a plate.
        h.deposit("C", 10_000)
        h.auctions.create_auction(h.req("new"), "a4b", 1)
        hold_b = h.margin.freeze(h.req("frz"), "B", "a4b", 100)
        h.auctions.place_bid(h.req("bid"), "b-b", "a4b", "B", 1, 100, hold_b["hold_id"])
        hold_c = h.margin.freeze(h.req("frz"), "C", "a4b", 100)
        h.auctions.place_bid(h.req("bid"), "b-c", "a4b", "C", 1, 100, hold_c["hold_id"])
        h.auctions.freeze_auction(h.req("close"), "a4b")
        h.clearing.settle(h.req("settle"), "a4b")
        # C loses on the bid_id tie-break and wins nothing.
        with self.assertRaises(DomainError):
            h.plate("plate-c", "a4b", "C", ["R"], [1, 0])

    def test_duplicate_plate_and_head_direction_gates(self):
        h = self.h
        h.deposit("A", 10_000)
        h.settle_auction("a5", 2, [("b-a", "A", 2, 100)])
        h.plate("plate-a", "a5", "A", ["L"], [1, 0])
        # Re-registering the same original returns the same unique plate.
        again = h.plate("plate-a", "a5", "A", ["L"], [1, 0])
        self.assertEqual(again["qty"], 2)
        with self.assertRaises(DomainError):
            h.plate("plate-a2", "a5", "A", ["R"], [1, 0])
        # A left-only plate cannot be imposed on the right head.
        with self.assertRaises(DomainError):
            h.press.trial_proof(None, {"plate_id": "plate-a", "orientation": NORMAL})

    # ------------------------------ 6. limits, idempotency, cross-auction

    def test_press_limits_and_idempotent_replay(self):
        h = self.h
        h.deposit("A", 10_000)
        h.settle_auction("a6", 2, [("b-a", "A", 2, 100)])
        h.plate("plate-a", "a6", "A", ["L"], [1, 0])

        with self.assertRaises(DomainError):
            h.press.plan_job(h.req("job"), "bad", ["plate-a"], paper_limit=0, max_changes=0)
        with self.assertRaises(DomainError):
            h.press.plan_job(h.req("job"), "bad2", ["plate-a", "plate-a"], 4, 1)

        rid = h.req("job")
        first = h.press.plan_job(rid, "job-6", ["plate-a"], paper_limit=4, max_changes=1)
        second = h.press.plan_job(rid, "job-6", ["plate-a"], paper_limit=4, max_changes=1)
        self.assertEqual(first, second)

        pull_rid = h.req("pull")
        pulled = h.press.pull_job(pull_rid, "job-6")
        replay = h.press.pull_job(pull_rid, "job-6")
        self.assertEqual(pulled, replay)
        with h.db.read() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM press_labels WHERE job_id='job-6'"
            ).fetchone()["n"]
        self.assertEqual(n, 2)  # replay must not double-print
        self.assertEqual(reconcile_press(h.db)["ok"], True)

    def test_batches_pool_plates_from_different_auctions(self):
        h = self.h
        h.deposit("A", 10_000)
        h.deposit("B", 10_000)
        h.settle_auction("x1", 2, [("b-a", "A", 2, 100)])
        h.settle_auction("x2", 2, [("b-b", "B", 2, 100)])
        h.plate("plate-a", "x1", "A", ["L"], [1, 0])
        h.plate("plate-b", "x2", "B", ["R"], [1, 0])
        job = h.press.plan_job(self.h.req("job"), "job-x",
                               ["plate-a", "plate-b"], paper_limit=4, max_changes=1)
        self.assertEqual(job["paper_used"], 2)
        self.assertEqual(job["torn_total"], 0)
        self.assertEqual(job["batches"][0]["left"]["plate_id"], "plate-a")
        self.assertEqual(job["batches"][0]["right"]["plate_id"], "plate-b")
        h.press.pull_job(self.h.req("pull"), "job-x")
        self.assertEqual(reconcile_press(h.db)["ok"], True)
        assert_reconciled(h.db)

    def test_pulled_plate_cannot_be_re_imposed_in_another_job(self):
        h = self.h
        h.deposit("A", 10_000)
        h.deposit("B", 10_000)
        h.settle_auction("a8", 4, [("b-a", "A", 3, 100), ("b-b", "B", 1, 100)])
        h.plate("plate-a", "a8", "A", ["L"], [1, 0])
        h.plate("plate-b", "a8", "B", ["R"], [1, 0])
        h.press.plan_job(self.h.req("job"), "job-8a",
                         ["plate-a", "plate-b"], paper_limit=3, max_changes=0)
        h.press.pull_job(self.h.req("pull"), "job-8a")
        # The same originals cannot feed a second work order.
        with self.assertRaises(InvalidState):
            h.press.plan_job(self.h.req("job"), "job-8b",
                             ["plate-a"], paper_limit=4, max_changes=1)

    def test_reconcile_detects_torn_label_reassigned_to_other_buyer(self):
        h = self.h
        h.deposit("A", 10_000)
        h.deposit("B", 10_000)
        h.settle_auction("a7", 4, [("b-a", "A", 3, 100), ("b-b", "B", 1, 100)])
        h.plate("plate-a", "a7", "A", ["L"], [1, 0])
        h.plate("plate-b", "a7", "B", ["R"], [1, 0])
        job = h.press.plan_job(self.h.req("job"), "job-7",
                               ["plate-a", "plate-b"], paper_limit=3, max_changes=0)
        h.press.pull_job(self.h.req("pull"), "job-7")
        self.assertTrue(reconcile_press(h.db)["ok"])

        with h.db.tx() as conn:
            conn.execute(
                "UPDATE press_labels SET buyer='A' WHERE buyer='B' AND outcome='TORN'"
            )
        report = reconcile_press(h.db)
        self.assertFalse(report["ok"])
        self.assertTrue(any("reassigned" in v for v in report["violations"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
