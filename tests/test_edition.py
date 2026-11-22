"""Scenario tests for turning settled auction units into a sung edition."""

from __future__ import annotations

import unittest

from sealed_auction import (
    Allocation,
    AuctionService,
    Database,
    MarginLedger,
    Permissions,
    Proposal,
    Score,
    build_edition,
    check_proposals,
    collation_table,
)
from sealed_auction.edition import allocations_from_auction
from sealed_auction.core import DomainError


def perm(*entries):
    return Permissions.from_pairs(entries)


def alloc(*entries):
    """alloc(("H", 1, "h-bid"), ...) -> winning units keyed by buyer."""

    return {buyer: Allocation(buyer, units, bid_id=bid) for buyer, units, bid in entries}


# The base example from the brief: two beats, one unit each, default ranges
# cover every pitch used, and the original manuscript moves up by two fifths.
BASE = Score(
    high=(7, 9),
    low=(0, 2),
    measures=(0, 0),
    allowed_intervals=frozenset({7, 8}),
    high_range=(0, 12),
    low_range=(0, 12),
)


class BaseScenarioTest(unittest.TestCase):
    def setUp(self):
        self.permissions = perm(
            ("H", "high", 0),
            ("L", "low", 0),
        )
        self.allocations = alloc(("H", 1, "h-bid"), ("L", 1, "l-bid"))
        self.h = Proposal("H1", "H", "high", (1,), 10, 5)
        self.l = Proposal("L1", "L", "low", (1,), 1, 3)

    def test_only_one_change_can_be_printed_and_h_wins(self):
        result = build_edition(
            BASE, self.permissions, self.allocations, [self.h, self.l]
        )
        self.assertTrue(result["feasible"])
        self.assertEqual(result["total_value"], 5)
        self.assertEqual(
            [s["proposal_id"] for s in result["selections"]], ["H1"]
        )
        # H is written into the beat it names; the interval stays allowed.
        self.assertEqual(result["beats"][1]["high"], 10)
        self.assertEqual(result["beats"][1]["low"], 2)
        self.assertEqual(result["beats"][1]["interval"], 8)
        # L was rejected at the table: both changes would make 9 semitones.
        reasons = {u["proposal_id"]: u["reason"] for u in result["unselected_proposals"]}
        self.assertIn("9", reasons["L1"])

    def test_every_beat_and_signed_receipt_is_reported(self):
        result = build_edition(
            BASE, self.permissions, self.allocations, [self.h, self.l]
        )
        self.assertEqual(len(result["beats"]), 2)
        self.assertEqual([b["interval"] for b in result["beats"]], [7, 8])
        self.assertEqual(result["changed_notes"], 1)
        receipt = result["receipts"][0]
        self.assertEqual(receipt["bid_id"], "h-bid")
        self.assertEqual(receipt["hold_id"], None)  # synthetic allocation
        self.assertEqual(receipt["original_pitches"], [9])
        self.assertEqual(receipt["new_pitch"], 10)
        self.assertEqual(result["buyer_usage"]["H"]["written_items"], 1)
        self.assertEqual(result["buyer_usage"]["H"]["remaining_units"], 0)

    def test_seat_lost_to_late_bidder_leaves_no_signed_change(self):
        # A late bidder wins H's only unit, so H signs nothing at all.
        allocations = {"L": Allocation("L", 1, bid_id="l-bid")}
        result = build_edition(BASE, self.permissions, allocations, [self.h, self.l])
        self.assertTrue(result["feasible"])
        self.assertEqual(result["total_value"], 3)
        self.assertEqual([s["proposal_id"] for s in result["selections"]], ["L1"])
        self.assertEqual(result["beats"][1]["low"], 1)
        rejected = {r["proposal_id"]: r["reason"] for r in result["rejected_proposals"]}
        self.assertIn("no completed transaction", rejected["H1"])

    def test_keeping_seat_but_tightening_high_ceiling_forces_l(self):
        # H still owns the unit, but the printed high voice may not exceed 9.
        tight = Score(
            high=BASE.high,
            low=BASE.low,
            measures=BASE.measures,
            allowed_intervals=BASE.allowed_intervals,
            high_range=(0, 9),
            low_range=(0, 12),
        )
        result = build_edition(
            tight, self.permissions, self.allocations, [self.h, self.l]
        )
        self.assertEqual([s["proposal_id"] for s in result["selections"]], ["L1"])
        self.assertEqual(result["total_value"], 3)

    def test_unchanged_note_cannot_be_signed(self):
        # A singable manuscript, so "feasible" isolates the no-op rejection.
        singable = Score(
            high=(7, 9),
            low=(0, 4),
            measures=(0, 0),
            allowed_intervals=frozenset({5, 7}),
        )
        noop = Proposal("H0", "H", "high", (1,), 9, 5)
        result = build_edition(
            singable, self.permissions, self.allocations, [noop]
        )
        self.assertTrue(result["feasible"])
        self.assertEqual(result["selections"], [])
        rejected = {r["proposal_id"]: r["reason"] for r in result["rejected_proposals"]}
        self.assertIn("unchanged", rejected["H0"])


class PermissionsAndTableTest(unittest.TestCase):
    def setUp(self):
        self.allocations = alloc(("H", 1, "h-bid"), ("L", 1, "l-bid"))

    def test_half_a_tie_is_not_admitted(self):
        score = Score(
            high=(5, 5, 9),
            low=(0, 0, 2),
            measures=(0, 1, 1),
            allowed_intervals=frozenset({5, 6, 7}),
            high_ties=((0, 1),),
        )
        permissions = perm(("H", "high", 1))
        half = Proposal("P1", "H", "high", (1,), 6, 4)
        check = check_proposals(score, permissions, self.allocations, [half])[0]
        self.assertFalse(check.ok)
        self.assertIn("half of a tie", check.reason)

    def test_full_tie_must_be_authorised_in_every_measure(self):
        score = Score(
            high=(5, 5, 9),
            low=(0, 0, 2),
            measures=(0, 1, 1),
            allowed_intervals=frozenset({5, 6, 7}),
            high_ties=((0, 1),),
        )
        full = Proposal("P1", "H", "high", (0, 1), 6, 4)
        only_one = perm(("H", "high", 0))
        check = check_proposals(score, only_one, self.allocations, [full])[0]
        self.assertFalse(check.ok)
        self.assertIn("measure", check.reason)

        both = perm(("H", "high", 0), ("H", "high", 1))
        check = check_proposals(score, both, self.allocations, [full])[0]
        self.assertTrue(check.ok)
        self.assertEqual(check.item.beats, (0, 1))

    def test_untied_equal_pitches_are_two_items(self):
        score = Score(
            high=(5, 5, 9),
            low=(0, 0, 2),
            measures=(0, 1, 1),
            allowed_intervals=frozenset({5, 6, 7}),
        )
        permissions = perm(("H", "high", 0), ("H", "high", 1))
        first = Proposal("P1", "H", "high", (0,), 6, 4)
        second = Proposal("P2", "H", "high", (1,), 6, 4)
        checks = check_proposals(score, permissions, self.allocations, [first, second])
        self.assertTrue(all(c.ok for c in checks))
        # One tied span would have been one item; without a tie these are two.
        self.assertEqual({c.item.tied for c in checks}, {False})

    def test_unsettled_buyer_signature_is_not_admitted(self):
        # Winner won 0 units (bid allocated nothing), so no signature.
        allocations = {"H": Allocation("H", 0), "L": Allocation("L", 1)}
        proposal = Proposal("P1", "H", "high", (1,), 10, 5)
        check = check_proposals(
            BASE, perm(("H", "high", 0)), allocations, [proposal]
        )[0]
        self.assertFalse(check.ok)
        self.assertIn("no completed transaction", check.reason)

    def test_second_change_on_same_note_is_not_admitted(self):
        allocations = alloc(("H", 2, "h-bid"), ("L", 1, "l-bid"))
        first = Proposal("P1", "H", "high", (1,), 10, 5)
        second = Proposal("P2", "H", "high", (1,), 11, 4)
        result = build_edition(
            BASE, perm(("H", "high", 0), ("L", "low", 0)), allocations, [first, second]
        )
        self.assertEqual(len(result["conflicting_proposals"]), 1)
        self.assertEqual(len(result["selections"]), 1)
        self.assertEqual(result["selections"][0]["new_pitch"], 10)


class TieAcrossMeasuresTest(unittest.TestCase):
    """A held note over a barline costs one unit but touches every beat."""

    def setUp(self):
        self.score = Score(
            high=(5, 5, 9),
            low=(0, 0, 2),
            measures=(0, 1, 1),
            allowed_intervals=frozenset({5, 6, 7}),
            high_ties=((0, 1),),
        )
        self.proposal = Proposal("T1", "H", "high", (0, 1), 6, 4)

    def test_half_unit_is_rejected(self):
        half = Proposal("T1", "H", "high", (1,), 6, 4)
        check = check_proposals(
            self.score, perm(("H", "high", 0), ("H", "high", 1)),
            alloc(("H", 1, "h")), [half],
        )[0]
        self.assertFalse(check.ok)

    def test_authorised_in_only_one_measure_is_rejected(self):
        check = check_proposals(
            self.score, perm(("H", "high", 1)),
            alloc(("H", 1, "h")), [self.proposal],
        )[0]
        self.assertFalse(check.ok)
        self.assertIn("measure", check.reason)

    def test_one_unit_changes_both_beats(self):
        result = build_edition(
            self.score,
            perm(("H", "high", 0), ("H", "high", 1)),
            alloc(("H", 1, "h")),
            [self.proposal],
        )
        self.assertTrue(result["feasible"])
        self.assertEqual([b["high"] for b in result["beats"]], [6, 6, 9])
        self.assertEqual(result["changed_notes"], 2)
        receipt = result["receipts"][0]
        self.assertEqual(receipt["beats"], [0, 1])
        self.assertEqual(receipt["measures"], [0, 1])
        self.assertTrue(receipt["tied"])
        self.assertEqual(result["buyer_usage"]["H"]["written_items"], 1)

    def test_same_pitches_without_a_tie_need_two_units(self):
        untied = Score(
            high=(5, 5, 9),
            low=(0, 0, 2),
            measures=(0, 1, 1),
            allowed_intervals=frozenset({5, 6, 7}),
        )
        whole = Proposal("T1", "H", "high", (0, 1), 6, 4)
        check = check_proposals(
            untied, perm(("H", "high", 0), ("H", "high", 1)),
            alloc(("H", 2, "h")), [whole],
        )[0]
        self.assertFalse(check.ok)
        self.assertIn("several original notes", check.reason)

        one = Proposal("T1", "H", "high", (0,), 6, 4)
        two = Proposal("T2", "H", "high", (1,), 6, 4)
        result = build_edition(
            untied, perm(("H", "high", 0), ("H", "high", 1)),
            alloc(("H", 2, "h")), [one, two],
        )
        self.assertEqual(len(result["selections"]), 2)
        self.assertEqual(result["buyer_usage"]["H"]["written_items"], 2)


class UnitAndRuleLimitsTest(unittest.TestCase):
    def test_buyer_unit_cap_is_enforced(self):
        # Two separately writable notes, but only one settled unit.
        score = Score(
            high=(5, 7, 9, 11),
            low=(0, 1, 2, 3),
            measures=(0, 0, 1, 1),
            allowed_intervals=frozenset({5, 6, 7, 8}),
        )
        p1 = Proposal("P1", "H", "high", (0,), 6, 4)
        p2 = Proposal("P2", "H", "high", (3,), 10, 4)
        result = build_edition(
            score, perm(("H", "high", 0), ("H", "high", 1)),
            alloc(("H", 1, "h")), [p1, p2],
        )
        self.assertEqual(len(result["selections"]), 1)
        self.assertEqual(result["changed_notes"], 1)
        self.assertEqual(result["buyer_usage"]["H"]["remaining_units"], 0)

    def test_parallel_fifths_are_refused(self):
        # 7->9 over 0->2 keeps a fifth and both voices rise: unsingable.
        score = Score(
            high=(7, 9),
            low=(0, 2),
            measures=(0, 0),
            allowed_intervals=frozenset({7, 8}),
        )
        result = build_edition(score, perm(), alloc(), [])
        self.assertFalse(result["feasible"])
        self.assertIn("parallel", result["reason"])

    def test_parallel_octaves_are_refused(self):
        score = Score(
            high=(12, 14),
            low=(0, 2),
            measures=(0, 0),
            allowed_intervals=frozenset({12}),
            high_range=(0, 20),
        )
        result = build_edition(score, perm(), alloc(), [])
        self.assertFalse(result["feasible"])
        self.assertIn("parallel octave", result["reason"])

    def test_out_of_range_is_refused(self):
        score = Score(
            high=(5, 9),
            low=(0, 2),
            measures=(0, 0),
            allowed_intervals=frozenset({5, 7}),
            high_range=(0, 8),
        )
        result = build_edition(score, perm(), alloc(), [])
        self.assertFalse(result["feasible"])
        self.assertIn("range", result["reason"])

    def test_no_signable_version_when_seat_is_missing(self):
        # The manuscript's parallel fifth is only cured by H's +1, but H lost
        # the seat, so there is no allowed complete version.
        score = Score(
            high=(7, 9),
            low=(0, 2),
            measures=(0, 0),
            allowed_intervals=frozenset({7, 8}),
        )
        hh = Proposal("H1", "H", "high", (1,), 10, 5)
        result = build_edition(
            score, perm(("H", "high", 0)), alloc(), [hh],
        )
        self.assertFalse(result["feasible"])
        self.assertIsNone(result["total_value"])
        self.assertIn("parallel", result["reason"])

    def test_genuinely_unsingable_manuscript_has_no_version(self):
        bad = Score(
            high=(7, 9),
            low=(5, 7),
            measures=(0, 0),
            allowed_intervals=frozenset({18}),
        )
        result = build_edition(bad, perm(), alloc(), [])
        self.assertFalse(result["feasible"])
        self.assertIsNone(result["total_value"])
        self.assertIn("not singable", result["reason"])


class CollationTableTest(unittest.TestCase):
    def test_table_reports_per_person_strokes_and_rejections(self):
        allocations = alloc(("H", 1, "h-bid"), ("L", 1, "l-bid"))
        proposals = [
            Proposal("H1", "H", "high", (1,), 10, 5),
            Proposal("L1", "L", "low", (1,), 1, 3),
            Proposal("L2", "L", "low", (0,), 0, 2),  # unchanged pitch
        ]
        table = collation_table(
            BASE, perm(("H", "high", 0), ("L", "low", 0)), allocations, proposals
        )
        rows = {row["proposal_id"]: row for row in table["rows"]}
        self.assertTrue(rows["H1"]["admitted"])
        self.assertEqual(rows["H1"]["bid_id"], "h-bid")
        self.assertEqual(rows["H1"]["original_pitches"], [9])
        self.assertFalse(rows["L2"]["admitted"])
        self.assertIn("unchanged", rows["L2"]["reason"])
        self.assertEqual(table["per_buyer"]["H"]["allocated_units"], 1)
        self.assertEqual(table["per_buyer"]["L"]["admitted_items"], 1)
        self.assertTrue(table["per_buyer"]["L"]["within_units"])
        self.assertFalse(table["all_admitted"])


class RealAuctionIntegrationTest(unittest.TestCase):
    """The same numbers driven through the actual sealed auction + margin."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "edition.sqlite3")
        self.margin = MarginLedger(self.db)
        self.auctions = AuctionService(self.db, self.margin)

    def tearDown(self):
        self._tmp.cleanup()

    def _req(self, name):
        return f"req-{name}"

    def _bid(self, bid_id, auction, buyer, qty, price):
        hold = self.margin.freeze(
            self._req(f"frz-{bid_id}"), buyer, auction, qty * price
        )
        return self.auctions.place_bid(
            self._req(f"bid-{bid_id}"), bid_id, auction, buyer, qty, price,
            hold["hold_id"],
        )

    def test_settled_units_become_one_signature_each(self):
        for buyer in ("H", "L"):
            self.margin.deposit(self._req(f"dep-{buyer}"), buyer, 10_000)
        self.auctions.create_auction(self._req("new"), "proof", 2)
        self._bid("h-bid", "proof", "H", 1, 800)
        self._bid("l-bid", "proof", "L", 1, 900)
        self.auctions.freeze_auction(self._req("close"), "proof")
        # Before settlement the units are not real, so they cannot be signed.
        with self.assertRaises(DomainError):
            allocations_from_auction(self.auctions, "proof")
        self.auctions.settle_auction(self._req("settle"), "proof")

        allocations = allocations_from_auction(self.auctions, "proof")
        self.assertEqual(allocations["H"].bid_id, "h-bid")
        self.assertEqual(allocations["H"].units, 1)
        self.assertEqual(allocations["H"].unit_price_cents, 800)

        result = build_edition(
            BASE,
            perm(("H", "high", 0), ("L", "low", 0)),
            allocations,
            [
                Proposal("H1", "H", "high", (1,), 10, 5),
                Proposal("L1", "L", "low", (1,), 1, 3),
            ],
        )
        self.assertEqual(result["total_value"], 5)
        self.assertEqual(result["receipts"][0]["bid_id"], "h-bid")
        self.assertEqual(result["receipts"][0]["hold_id"], allocations["H"].hold_id)

    def test_lost_seat_to_late_high_bidder(self):
        for buyer in ("H", "L", "X"):
            self.margin.deposit(self._req(f"dep-{buyer}"), buyer, 10_000)
        self.auctions.create_auction(self._req("new"), "proof", 2)
        self._bid("h-bid", "proof", "H", 1, 800)
        self._bid("l-bid", "proof", "L", 1, 900)
        # Late higher bid takes one seat; H's cheaper bid is out entirely.
        self._bid("x-bid", "proof", "X", 1, 1_000)
        self.auctions.freeze_auction(self._req("close"), "proof")
        self.auctions.settle_auction(self._req("settle"), "proof")

        allocations = allocations_from_auction(self.auctions, "proof")
        self.assertNotIn("H", allocations)
        self.assertEqual(allocations["L"].units, 1)
        result = build_edition(
            BASE,
            perm(("H", "high", 0), ("L", "low", 0)),
            allocations,
            [
                Proposal("H1", "H", "high", (1,), 10, 5),
                Proposal("L1", "L", "low", (1,), 1, 3),
            ],
        )
        self.assertEqual(result["total_value"], 3)
        self.assertEqual([s["proposal_id"] for s in result["selections"]], ["L1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
