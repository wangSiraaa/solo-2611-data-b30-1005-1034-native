"""Scenario tests for the fictional astronomy exhibit layer.

The exhibit sits on top of the real sealed auction: only actually settled
units become telescope openings.  Covered here:

  * the reference field: 4 settled units, stations (2,1)/(2,-1), radius 1,
    circle track (1,0)->(2,0)->(3,0); F/G centre 2, H centre 5; F mirror 0
    value 5, G/H mirror 2 value 4  ->  G+H are chosen for 8;
  * F alone cannot be followed by H (no idle exposure to move 0 -> 2);
  * moving G's circle track to y=2 costs G its station pair -> F (5) wins;
  * shutting one station at exposure 6 breaks H's pair -> F (5) wins;
  * a three-station event reachable from more than one station pair;
  * a real partial fill: the forecast is unchanged, the signable night
    shortens;
  * persistence: signed exposures and spent openings cannot be re-spent by
    the next night.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sealed_auction import AuctionService, ClearingService, Database, ExhibitService, MarginLedger
from sealed_auction.core import DomainError, InvalidState, LimitExceeded, NotFound
from sealed_auction.reconcile import assert_reconciled

TRACK = {1: (1, 0), 2: (2, 0), 3: (3, 0)}  # circle centres around the centre exposure


class Harness:
    """Fresh database with the four services wired together."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "exhibit.sqlite3")
        self.margin = MarginLedger(self.db)
        self.auctions = AuctionService(self.db, self.margin)
        self.clearing = ClearingService(self.db, self.margin, self.auctions)
        self.exhibit = ExhibitService(self.db)
        self._req = 0

    def req(self, prefix="req"):
        self._req += 1
        return f"{prefix}-{self._req:04d}"

    def close(self):
        self._tmp.cleanup()

    # -- auction side -------------------------------------------------

    def settle_units(self, buyer, bid_qty, units):
        """Run a real auction and settle it; returns the frozen allocation."""
        auction_id = f"auc-{self._req}"
        self.margin.deposit(self.req("dep"), buyer, 1_000_000)
        self.auctions.create_auction(self.req("new"), auction_id, units)
        hold = self.margin.freeze(self.req("frz"), buyer, auction_id, bid_qty * 500)
        self.auctions.place_bid(
            self.req("bid"), f"bid-{self._req}", auction_id, buyer,
            bid_qty, 500, hold["hold_id"],
        )
        frozen = self.auctions.freeze_auction(self.req("cls"), auction_id)
        self.clearing.settle(self.req("stl"), auction_id)
        return frozen["allocations"][0]["allocated_qty"]

    # -- exhibit side -------------------------------------------------

    def register_reference_model(self, g_cy=0, stations=(("S1", 2, 1), ("S2", 2, -1))):
        """The reference two-station F/G/H model from the problem statement."""
        ex = self.exhibit
        for name, x, y in stations:
            ex.register_station(self.req("st"), name, x, y, 1)
        g_track = {e: (cx, g_cy) for e, (cx, _cy) in TRACK.items()}
        ex.register_star(self.req("sr"), "F", 1, TRACK, {"S1": 0, "S2": 0})
        ex.register_star(self.req("sr"), "G", 1, g_track, {"S1": 2, "S2": 2})
        ex.register_star(
            self.req("sr"), "H", 1,
            {4: (1, 0), 5: (2, 0), 6: (3, 0)}, {"S1": 2, "S2": 2},
        )
        ex.register_order(self.req("or"), "ord-F", "terra", "F", 2, 5)
        ex.register_order(self.req("or"), "ord-G", "terra", "G", 2, 4)
        ex.register_order(self.req("or"), "ord-H", "terra", "H", 5, 4)


class ExhibitTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    # ------------------------------------------------ reference field: G+H=8

    def test_reference_field_chooses_GH_for_8(self):
        h = self.h
        granted = h.settle_units("terra", bid_qty=4, units=4)
        self.assertEqual(granted, 4)
        h.register_reference_model()

        plan = h.exhibit.plan_night(h.req("night"), "night-1")
        self.assertEqual(plan["total_value"], 8)
        self.assertEqual(plan["openings_used"], 4)
        self.assertEqual(
            {s["order_id"] for s in plan["scored"]}, {"ord-G", "ord-H"}
        )
        self.assertEqual(plan["skipped"][0]["order_id"], "ord-F")

        # Both stations expose G at 1-3 and H at 4-6, mirror 2 throughout.
        for sheet in plan["sheets"].values():
            expose = [e for e in sheet if e["activity"] == "expose"]
            self.assertEqual([e["exposure"] for e in expose], [1, 2, 3, 4, 5, 6])
            self.assertEqual([e["star"] for e in expose], ["G", "G", "G", "H", "H", "H"])
            self.assertEqual(
                [e["light"] for e in expose],
                ["bright", "dark", "bright", "bright", "dark", "bright"],
            )
            self.assertTrue(all(e["mirror"] == 2 for e in expose))

        account = plan["accounts"][0]
        self.assertEqual(account["buyer"], "terra")
        self.assertEqual(account["granted_openings"], 4)
        self.assertEqual(account["spent_this_night"], 4)
        self.assertEqual(account["remaining_openings"], 0)

        openings = h.exhibit.openings("terra")
        self.assertEqual(openings["granted_openings"], 4)
        self.assertEqual(openings["spent_openings"], 4)
        self.assertEqual(len(openings["spends"]), 4)  # 2 orders x 2 stations
        assert_reconciled(h.db)

    # ------------------------------------- F finishes too late to move for H

    def test_F_blocks_H_because_mirror_cannot_move_in_time(self):
        h = self.h
        h.settle_units("terra", bid_qty=4, units=4)
        h.register_reference_model()
        # Drop G: only F (mirror 0, exposures 1-3) and H (mirror 2, 4-6).
        with h.db.tx() as conn:
            conn.execute("DELETE FROM exhibit_orders WHERE order_id='ord-G'")

        plan = h.exhibit.plan_night(h.req("night"), "night-1")
        # F+H would need the mirror 0 -> 2 with zero idle exposures: infeasible.
        self.assertEqual([s["order_id"] for s in plan["scored"]], ["ord-F"])
        self.assertEqual(plan["total_value"], 5)
        self.assertEqual(plan["openings_used"], 2)
        self.assertEqual(plan["skipped"][0]["order_id"], "ord-H")

    # --------------------------------- G's track moved to y=2: G loses its pair

    def test_G_track_at_y2_loses_pair_and_F_wins(self):
        h = self.h
        h.settle_units("terra", bid_qty=4, units=4)
        h.register_reference_model(g_cy=2)

        predict = h.exhibit.predict()
        events = {e["star"]: e for e in predict["events"]}
        # S1=(2,1) still sees bright/dark/bright for G; S2=(2,-1) never darkens.
        self.assertEqual(events["G"]["plates"], ["S1"])
        self.assertEqual(events["G"]["pairs"], [])
        # The other two events are untouched.
        self.assertEqual(events["F"]["pairs"][0]["stations"], ["S1", "S2"])
        self.assertEqual(events["H"]["pairs"][0]["stations"], ["S1", "S2"])

        plan = h.exhibit.plan_night(h.req("night"), "night-1")
        self.assertEqual([s["order_id"] for s in plan["scored"]], ["ord-F"])
        self.assertEqual(plan["total_value"], 5)
        reasons = {s["order_id"]: s["reason"] for s in plan["skipped"]}
        self.assertEqual(reasons["ord-G"], "no_valid_pair")
        self.assertEqual(reasons["ord-H"], "not_selected")

    # --------------------------- one station down at exposure 6: H cannot pair

    def test_station_shutdown_at_exposure_6_breaks_H(self):
        h = self.h
        h.settle_units("terra", bid_qty=4, units=4)
        h.register_reference_model()
        h.exhibit.set_duty(h.req("duty"), "S2", 6, False)

        predict = h.exhibit.predict()
        events = {e["star"]: e for e in predict["events"]}
        self.assertEqual(events["H"]["plates"], ["S1"])
        self.assertEqual(events["H"]["pairs"], [])

        plan = h.exhibit.plan_night(h.req("night"), "night-1")
        self.assertEqual([s["order_id"] for s in plan["scored"]], ["ord-F"])
        self.assertEqual(plan["total_value"], 5)
        # The shutdown exposure shows up on S2's sheet.
        s2 = plan["sheets"]["S2"]
        self.assertEqual(s2[5]["exposure"], 6)
        self.assertEqual(s2[5]["activity"], "down")

    # ------------------------------------------- checkable triple substitutions

    def test_prediction_substitutions_are_individually_checkable(self):
        h = self.h
        h.register_reference_model()
        predict = h.exhibit.predict()
        events = {e["star"]: e for e in predict["events"]}
        f_s1 = next(b for b in events["F"]["stations"] if b["station"] == "S1")
        subs = f_s1["substitutions"]
        # (2,1) vs (1,0),(2,0),(3,0), r^2 = 1: dist^2 = 2, 1, 2.
        self.assertEqual([s["dist2"] for s in subs], [2, 1, 2])
        self.assertEqual([s["r2"] for s in subs], [1, 1, 1])
        self.assertEqual([s["blocked"] for s in subs], [False, True, False])
        self.assertEqual([s["light"] for s in subs], ["bright", "dark", "bright"])
        self.assertEqual([s["circle"] for s in subs], [[1, 0], [2, 0], [3, 0]])
        self.assertTrue(f_s1["plate"])
        pair = events["F"]["pairs"][0]
        self.assertEqual(pair["stations"], ["S1", "S2"])
        self.assertEqual(pair["y"], [1, -1])
        self.assertEqual(pair["center_cy"], 0)

    # ----------------------- three stations: same event from different pairs

    def _three_station_model(self, h):
        ex = h.exhibit
        ex.register_station(h.req("st"), "A", 3, 2, 1)
        ex.register_station(h.req("st"), "B", 3, -2, 1)
        ex.register_station(h.req("st"), "C", 4, -1, 1)
        ex.register_star(
            h.req("sr"), "E", 2,
            {7: (0, 0), 8: (3, 0), 9: (6, 0)},
            {"A": 0, "B": 0, "C": 0},
        )
        ex.register_order(h.req("or"), "ord-E", "terra", "E", 8, 6)

    def test_same_event_obtainable_from_different_station_pairs(self):
        h = self.h
        h.settle_units("terra", bid_qty=2, units=2)
        self._three_station_model(h)

        predict = h.exhibit.predict()
        (event,) = predict["events"]
        self.assertEqual(event["plates"], ["A", "B", "C"])
        # A (y=2) is above the centre line y=0; B and C are below.
        self.assertEqual(
            [p["stations"] for p in event["pairs"]], [["A", "B"], ["A", "C"]]
        )

        plan = h.exhibit.plan_night(h.req("night"), "night-1")
        self.assertEqual(plan["total_value"], 6)
        self.assertEqual(plan["scored"][0]["stations"], ["A", "B"])

    def test_pair_fallback_when_one_station_is_down(self):
        h = self.h
        h.settle_units("terra", bid_qty=2, units=2)
        self._three_station_model(h)
        h.exhibit.set_duty(h.req("duty"), "B", 8, False)

        predict = h.exhibit.predict()
        (event,) = predict["events"]
        self.assertEqual(event["plates"], ["A", "C"])
        self.assertEqual([p["stations"] for p in event["pairs"]], [["A", "C"]])

        plan = h.exhibit.plan_night(h.req("night"), "night-1")
        self.assertEqual(plan["scored"][0]["stations"], ["A", "C"])
        self.assertEqual(plan["total_value"], 6)

    # ------------- partial fill: same forecast, shorter signable night

    def test_partial_fill_keeps_forecast_but_shortens_night(self):
        full = Harness()
        try:
            full.settle_units("terra", bid_qty=4, units=4)
            full.register_reference_model()
            forecast_full = full.exhibit.predict()
            plan_full = full.exhibit.plan_night(full.req("night"), "night-1")
        finally:
            full.close()

        h = self.h
        # Real partial fill: bids 4, only 2 units exist -> 2 openings.
        granted = h.settle_units("terra", bid_qty=4, units=2)
        self.assertEqual(granted, 2)
        h.register_reference_model()

        forecast_partial = h.exhibit.predict()
        # 预报星历不因缺券改变: the forecast is byte-identical.
        self.assertEqual(forecast_partial, forecast_full)

        plan = h.exhibit.plan_night(h.req("night"), "night-1")
        # ...but the signable night shortens: 2 openings buy one order only.
        self.assertEqual(plan["openings_used"], 2)
        self.assertEqual(plan["total_value"], 5)
        self.assertEqual([s["order_id"] for s in plan["scored"]], ["ord-F"])
        self.assertLess(plan["total_value"], plan_full["total_value"])
        account = plan["accounts"][0]
        self.assertEqual(account["granted_openings"], 2)
        self.assertEqual(account["remaining_openings"], 0)

    # -------------------- bidding alone never signs a plate

    def test_bidding_and_settling_alone_signs_no_plate(self):
        h = self.h
        h.register_reference_model()
        # Frozen but not settled: no openings yet.
        h.margin.deposit(h.req("dep"), "terra", 1_000_000)
        h.auctions.create_auction(h.req("new"), "auc-x", 4)
        hold = h.margin.freeze(h.req("frz"), "terra", "auc-x", 4 * 500)
        h.auctions.place_bid(h.req("bid"), "bid-x", "auc-x", "terra", 4, 500, hold["hold_id"])
        h.auctions.freeze_auction(h.req("cls"), "auc-x")
        self.assertEqual(h.exhibit.openings("terra")["granted_openings"], 0)

        h.clearing.settle(h.req("stl"), "auc-x")
        openings = h.exhibit.openings("terra")
        self.assertEqual(openings["granted_openings"], 4)
        self.assertEqual(openings["spent_openings"], 0)
        # No night planned -> no signed real exposures anywhere.
        self.assertEqual(h.exhibit.signed_exposures(), [])

    # -------------------- two teams share stations, one exposure one team

    def test_two_teams_share_the_night_per_exposure(self):
        h = self.h
        h.settle_units("terra", bid_qty=2, units=2)
        h.settle_units("nova", bid_qty=2, units=2)
        ex = h.exhibit
        ex.register_station(h.req("st"), "S1", 2, 1, 1)
        ex.register_station(h.req("st"), "S2", 2, -1, 1)
        ex.register_star(h.req("sr"), "F", 1, TRACK, {"S1": 0, "S2": 0})
        ex.register_star(
            h.req("sr"), "W", 1, {4: (1, 0), 5: (2, 0), 6: (3, 0)}, {"S1": 0, "S2": 0}
        )
        ex.register_order(h.req("or"), "ord-F", "terra", "F", 2, 5)
        ex.register_order(h.req("or"), "ord-W", "nova", "W", 5, 4)

        plan = ex.plan_night(h.req("night"), "night-1")
        # Same mirror 0 throughout: F at 1-3 for terra, W at 4-6 for nova.
        self.assertEqual(plan["total_value"], 9)
        self.assertEqual(plan["openings_used"], 4)
        accounts = {a["buyer"]: a for a in plan["accounts"]}
        self.assertEqual(accounts["terra"]["spent_this_night"], 2)
        self.assertEqual(accounts["nova"]["spent_this_night"], 2)
        self.assertEqual(accounts["terra"]["remaining_openings"], 0)
        self.assertEqual(accounts["nova"]["remaining_openings"], 0)
        for sheet in plan["sheets"].values():
            expose = [e for e in sheet if e["activity"] == "expose"]
            self.assertEqual([e["star"] for e in expose], ["F", "F", "F", "W", "W", "W"])

    # -------------------- signed exposures cannot be re-spent next night

    def test_next_night_cannot_respend_exposures_or_openings(self):
        h = self.h
        h.settle_units("terra", bid_qty=6, units=6)
        h.register_reference_model()
        before = h.exhibit.predict()

        night1 = h.exhibit.plan_night(h.req("night"), "night-1")
        self.assertEqual(night1["total_value"], 8)  # G+H
        self.assertEqual(h.exhibit.openings("terra")["remaining_openings"], 2)

        # New orders for night 2: J reuses F's geometry (exposures 1-3, spent),
        # K sits on free exposures 7-9 at mirror 2.
        h.exhibit.register_star(h.req("sr"), "J", 1, TRACK, {"S1": 0, "S2": 0})
        h.exhibit.register_order(h.req("or"), "ord-J", "terra", "J", 2, 3)
        h.exhibit.register_star(
            h.req("sr"), "K", 1, {7: (1, 0), 8: (2, 0), 9: (3, 0)}, {"S1": 2, "S2": 2}
        )
        h.exhibit.register_order(h.req("or"), "ord-K", "terra", "K", 8, 3)

        night2 = h.exhibit.plan_night(h.req("night"), "night-2")
        scored = {s["order_id"] for s in night2["scored"]}
        self.assertEqual(scored, {"ord-K"})
        skipped = {s["order_id"]: s["reason"] for s in night2["skipped"]}
        # F never scored but its exposures are spent by G; J needs the same
        # spent exposures.  Both are blocked: 签好的原曝光占额，下个夜班不能重复花.
        self.assertEqual(skipped["ord-F"], "no_valid_pair")
        self.assertEqual(skipped["ord-J"], "no_valid_pair")

        openings = h.exhibit.openings("terra")
        self.assertEqual(openings["spent_openings"], 6)
        self.assertEqual(openings["remaining_openings"], 0)

        # The forecast is a pure function of the model: planning changed nothing.
        after = h.exhibit.predict()
        self.assertEqual(
            [e for e in before["events"]],
            [e for e in after["events"] if (e["star"], e["center_exposure"])
             in {("F", 2), ("G", 2), ("H", 5)}],
        )
        # read_night reconstructs the same sheets from stored rows.
        reread = h.exhibit.read_night("night-1")
        self.assertEqual(reread["sheets"], night1["sheets"])
        self.assertEqual(reread["total_value"], 8)

    # ---------------------------------------------------------- idempotency

    def test_plan_night_is_idempotent_and_night_id_unique(self):
        h = self.h
        h.settle_units("terra", bid_qty=4, units=4)
        h.register_reference_model()

        rid = h.req("night")
        first = h.exhibit.plan_night(rid, "night-1")
        again = h.exhibit.plan_night(rid, "night-1")
        self.assertEqual(first, again)
        with self.assertRaises(InvalidState):
            h.exhibit.plan_night(h.req("night"), "night-1")
        self.assertEqual(len(h.exhibit.signed_exposures()), 12)  # 2 orders x 2 x 3
        with self.assertRaises(NotFound):
            h.exhibit.read_night("night-404")

    def test_registration_is_idempotent_and_immutable(self):
        h = self.h
        h.register_reference_model()
        rid = h.req("st")
        first = h.exhibit.register_station(rid, "S3", 0, 0, 1)
        second = h.exhibit.register_station(rid, "S3", 0, 0, 1)
        self.assertEqual(first, second)
        with self.assertRaises(InvalidState):
            h.exhibit.register_station(h.req("st"), "S3", 9, 9, 1)
        with self.assertRaises(InvalidState):
            h.exhibit.register_star(h.req("sr"), "F", 2, TRACK, {"S1": 0, "S2": 0})
        same = h.exhibit.register_star(h.req("sr"), "F", 1, TRACK, {"S1": 0, "S2": 0})
        self.assertEqual(same["star"], "F")

    # --------------------------------------------------------------- limits

    def test_exhibit_limits(self):
        h = self.h
        h.register_reference_model()
        h.exhibit.register_station(h.req("st"), "S3", 0, 0, 1)  # third and last
        with self.assertRaises(LimitExceeded):
            h.exhibit.register_station(h.req("st"), "S4", 0, 0, 1)
        with self.assertRaises(DomainError):
            h.exhibit.register_order(h.req("or"), "ord-c1", "terra", "F", 1, 1)
        with self.assertRaises(DomainError):
            h.exhibit.register_order(h.req("or"), "ord-c15", "terra", "F", 15, 1)
        with self.assertRaises(NotFound):
            h.exhibit.register_order(h.req("or"), "ord-nostar", "terra", "ZZ", 2, 1)
        with self.assertRaises(NotFound):
            h.exhibit.set_duty(h.req("duty"), "S404", 3, False)
        for i in range(5):  # 3 registered + 5 = 8 orders
            h.exhibit.register_order(h.req("or"), f"ord-x{i}", "terra", "F", 2, 1)
        with self.assertRaises(LimitExceeded):
            h.exhibit.register_order(h.req("or"), "ord-overflow", "terra", "F", 2, 1)

    # ------------------------------------------- openings follow real fills

    def test_openings_track_partial_fills_across_auctions(self):
        h = self.h
        # Two real auctions: 3 of 5 wanted, then 2 of 2 -> 5 openings total.
        self.assertEqual(h.settle_units("terra", bid_qty=5, units=3), 3)
        self.assertEqual(h.settle_units("nova", bid_qty=2, units=2), 2)
        self.assertEqual(h.exhibit.openings("terra")["granted_openings"], 3)
        self.assertEqual(h.exhibit.openings("nova")["granted_openings"], 2)
        assert_reconciled(h.db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
