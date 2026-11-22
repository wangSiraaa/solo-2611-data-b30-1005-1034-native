"""Re-runnable print-shop demo: settled originals -> plates -> work orders.

Run from the repository root:

    python3 -m examples.press_demo            # fresh temporary database
    python3 -m examples.press_demo --db press.sqlite3

The fictional print shop only manufactures labels for originals that were
actually awarded and whose auction clearing is done.  The three scenarios from
the shop rules are reproduced end to end, including the trial proof that shows
exactly which registration pin positions fail when two plates cannot share a
sheet.  Nothing here touches a real press or payment system.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from sealed_auction import (
    AuctionService,
    ClearingService,
    Database,
    MarginLedger,
    PressShop,
)
from sealed_auction.core import PressInfeasible
from sealed_auction.press import reconcile_press


def show(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def dump(label: str, obj) -> None:
    print(f"\n[{label}]")
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="SQLite path (default: temp file)")
    args = parser.parse_args()

    tmp = None
    if args.db is None:
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "press.sqlite3"
    else:
        db_path = Path(args.db)

    print(f"database: {db_path}")
    db = Database(db_path)
    margin = MarginLedger(db)
    auctions = AuctionService(db, margin)
    clearing = ClearingService(db, margin, auctions)
    press = PressShop(db, auctions)
    counter = {"n": 0}

    def req(tag: str) -> str:
        counter["n"] += 1
        return f"press-demo-{tag}-{counter['n']:03d}"

    def settle(auction_id: str, units: int, bids) -> None:
        auctions.create_auction(req("new"), auction_id, units)
        for bid_id, buyer, qty, price in bids:
            hold = margin.freeze(req("frz"), buyer, auction_id, qty * price)
            auctions.place_bid(
                req("bid"), bid_id, auction_id, buyer, qty, price, hold["hold_id"]
            )
        auctions.freeze_auction(req("close"), auction_id)
        clearing.settle(req("settle"), auction_id)

    try:
        show("1. compatible hole trains share one two-up plate, zero waste")
        for buyer in ("A", "B"):
            margin.deposit(req("dep"), buyer, 10_000)
        settle("auc-1", 4, [("bid-a", "A", 2, 500), ("bid-b", "B", 2, 400)])
        press.register_plate(req("plate"), "plate-a", "auc-1", "A", ["L"], [1, 0], False)
        press.register_plate(req("plate"), "plate-b", "auc-1", "B", ["R"], [1, 0], False)
        proof = press.trial_proof(
            {"plate_id": "plate-a"}, {"plate_id": "plate-b"}
        )
        dump("trial proof (pins line up)", proof)
        job1 = press.plan_job(req("job"), "job-1",
                              ["plate-a", "plate-b"], paper_limit=4, max_changes=1)
        print(f"    paper={job1['paper_used']} changes={job1['changes_used']} "
              f"torn={job1['torn_total']}")
        press.pull_job(req("pull"), "job-1")

        show("2. B's signed plate changes to 01 and cannot flip")
        settle("auc-2", 4, [("bid-a2", "A", 2, 500), ("bid-b2", "B", 2, 400)])
        press.register_plate(req("plate"), "plate-a2", "auc-2", "A", ["L"], [1, 0], False)
        press.register_plate(req("plate"), "plate-b2", "auc-2", "B", ["R"], [0, 1], False)
        bad = press.trial_proof(
            {"plate_id": "plate-a2"}, {"plate_id": "plate-b2"}
        )
        dump("trial proof (registration mismatch)", bad)
        # Probe the infeasible one-sheet request before any of these plates are
        # actually pulled.
        try:
            press.plan_job(req("job"), "job-2-one-sheet",
                           ["plate-a2", "plate-b2"], paper_limit=1, max_changes=1)
        except PressInfeasible as exc:
            print(f"    one sheet only -> {type(exc).__name__}: {exc}")
        job2 = press.plan_job(req("job"), "job-2",
                              ["plate-a2", "plate-b2"], paper_limit=4, max_changes=1)
        print(f"    single-head runs: paper={job2['paper_used']} "
              f"changes={job2['changes_used']} torn={job2['torn_total']}")
        press.pull_job(req("pull"), "job-2")

        show("3. new room: A wins 3, B wins 1")
        settle("auc-3", 4, [("bid-a3", "A", 3, 500), ("bid-b3", "B", 1, 400)])
        press.register_plate(req("plate"), "plate-a3", "auc-3", "A", ["L"], [1, 0], False)
        press.register_plate(req("plate"), "plate-b3", "auc-3", "B", ["R"], [1, 0], False)
        job3a = press.plan_job(req("job"), "job-3a",
                               ["plate-a3", "plate-b3"], paper_limit=3, max_changes=0)
        print(f"    3 sheets, 0 changes: paper={job3a['paper_used']} "
              f"torn={job3a['torn_total']} (B tears {job3a['torn_by_plate']['plate-b3']})")
        job3b = press.plan_job(req("job"), "job-3b",
                               ["plate-a3", "plate-b3"], paper_limit=4, max_changes=1)
        print(f"    1 change allowed: paper={job3b['paper_used']} "
              f"changes={job3b['changes_used']} torn={job3b['torn_total']}")
        for batch in job3b["batches"]:
            heads = [h["plate_id"] for h in (batch["left"], batch["right"]) if h]
            print(f"      batch {batch['seq']}: {heads} x {batch['sheets']} sheet(s), "
                  f"change={batch['is_change']}")
        # The two plans compete for the same originals; only the zero-waste one
        # is actually pulled, so each buyer is delivered exactly their award.
        press.pull_job(req("pull"), "job-3b")

        show("4. per-buyer delivered / torn audit")
        for buyer in ("A", "B"):
            dump(f"buyer {buyer}", press.buyer_account(buyer))

        show("5. independent print-shop reconciliation")
        report = reconcile_press(db)
        dump("reconciliation", report)
        return 0 if report["ok"] else 1
    finally:
        if tmp is not None:
            tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
