"""Re-runnable demo: settled auction units -> a singable two-voice edition.

Run from the repository root:

    python3 -m examples.run_edition

The numbers mirror the brief: H won one unit for 8, L won one for 9, the two
allowed vertical intervals are 7 and 8, and the manuscript moves up two
consecutive fifths.  Only one of the two proposed changes can be printed.
The same auction + margin engine that clears real bids supplies the winning
units, so a lost seat or an unsettled bid really does remove a signature.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from sealed_auction import (
    Allocation,
    AuctionService,
    Database,
    MarginLedger,
    Permissions,
    Proposal,
    Score,
    build_edition,
)
from sealed_auction.edition import allocations_from_auction


def show(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def dump(label: str, obj) -> None:
    print(f"\n[{label}]")
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def brief_edition(label: str, allocations, score) -> None:
    show(label)
    result = build_edition(
        score,
        Permissions.from_pairs([("H", "high", 0), ("L", "low", 0)]),
        allocations,
        [
            Proposal("H1", "H", "high", (1,), 10, 5),
            Proposal("L1", "L", "low", (1,), 1, 3),
        ],
    )
    print(
        f"  feasible={result['feasible']} value={result['total_value']} "
        f"changed_notes={result['changed_notes']}"
    )
    for beat in result["beats"] or []:
        print(
            f"  beat {beat['beat']}: high={beat['high']} low={beat['low']} "
            f"interval={beat['interval']}"
        )
    print("  signed:", [s["proposal_id"] for s in result["selections"]])
    for receipt in result["receipts"]:
        print(
            f"  receipt {receipt['proposal_id']}: bid={receipt['bid_id']} "
            f"units {receipt['units_before']}->{receipt['units_after']} "
            f"beats={receipt['beats']} {receipt['original_pitches']}->"
            f"{receipt['new_pitch']}"
        )
    for item in result["unselected_proposals"]:
        print(f"  not printed {item['proposal_id']}: {item['reason']}")


def main() -> int:
    tmp = tempfile.TemporaryDirectory()
    db = Database(Path(tmp.name) / "edition.sqlite3")
    margin = MarginLedger(db)
    auctions = AuctionService(db, margin)

    base = Score(
        high=(7, 9),
        low=(0, 2),
        measures=(0, 0),
        allowed_intervals=frozenset({7, 8}),
        high_range=(0, 12),
        low_range=(0, 12),
    )

    def req(name):
        return f"demo-{name}"

    def bid(bid_id, auction, buyer, qty, price):
        hold = margin.freeze(req(f"frz-{bid_id}"), buyer, auction, qty * price)
        auctions.place_bid(
            req(f"bid-{bid_id}"), bid_id, auction, buyer, qty, price,
            hold["hold_id"],
        )

    try:
        for buyer in ("H", "L", "X"):
            margin.deposit(req(f"dep-{buyer}"), buyer, 10_000)

        show("1. initial two seats: H @ 8, L @ 9")
        auctions.create_auction(req("new-a"), "proof-a", 2)
        bid("h-bid", "proof-a", "H", 1, 800)
        bid("l-bid", "proof-a", "L", 1, 900)
        auctions.freeze_auction(req("close-a"), "proof-a")
        auctions.settle_auction(req("settle-a"), "proof-a")
        allocations = allocations_from_auction(auctions, "proof-a")
        brief_edition("edition: H's +1 (value 5) beats L's -1 (value 3)", allocations, base)

        show("2. a late bidder X @ 10 takes one seat before the old close")
        # A fresh auction represents the re-cut: X outranks H, who is now out.
        auctions.create_auction(req("new-b"), "proof-b", 2)
        bid("h-bid-b", "proof-b", "H", 1, 800)
        bid("l-bid-b", "proof-b", "L", 1, 900)
        bid("x-bid-b", "proof-b", "X", 1, 1_000)
        auctions.freeze_auction(req("close-b"), "proof-b")
        auctions.settle_auction(req("settle-b"), "proof-b")
        allocations = allocations_from_auction(auctions, "proof-b")
        print("  winners:", sorted(allocations))
        brief_edition("edition after the late bid: only L remains", allocations, base)

        show("3. H keeps the seat but the printed high voice is capped at 9")
        auctions.create_auction(req("new-c"), "proof-c", 2)
        bid("h-bid-c", "proof-c", "H", 1, 800)
        bid("l-bid-c", "proof-c", "L", 1, 900)
        auctions.freeze_auction(req("close-c"), "proof-c")
        auctions.settle_auction(req("settle-c"), "proof-c")
        allocations = allocations_from_auction(auctions, "proof-c")
        capped = Score(
            high=base.high,
            low=base.low,
            measures=base.measures,
            allowed_intervals=base.allowed_intervals,
            high_range=(0, 9),
            low_range=(0, 12),
        )
        brief_edition("edition with high<=9: H is out of range", allocations, capped)

        show("4. a held note across a barline costs one unit but moves both beats")
        tied = Score(
            high=(5, 5, 9),
            low=(0, 0, 2),
            measures=(0, 1, 1),
            allowed_intervals=frozenset({5, 6, 7}),
            high_ties=((0, 1),),
        )
        held = build_edition(
            tied,
            Permissions.from_pairs(
                [("H", "high", 0), ("H", "high", 1)]
            ),
            {"H": Allocation("H", 1, bid_id="h-bid")},
            [Proposal("T1", "H", "high", (0, 1), 6, 4)],
        )
        print("  tied beats ->", [b["high"] for b in held["beats"]],
              "changed_notes:", held["changed_notes"])
        half = build_edition(
            tied,
            Permissions.from_pairs([("H", "high", 0), ("H", "high", 1)]),
            {"H": Allocation("H", 1, bid_id="h-bid")},
            [Proposal("T1", "H", "high", (1,), 6, 4)],
        )
        print("  half a tie ->", [r["reason"] for r in half["rejected_proposals"]])
        untied = Score(
            high=(5, 5, 9),
            low=(0, 0, 2),
            measures=(0, 1, 1),
            allowed_intervals=frozenset({5, 6, 7}),
        )
        same = build_edition(
            untied,
            Permissions.from_pairs([("H", "high", 0), ("H", "high", 1)]),
            {"H": Allocation("H", 1, bid_id="h-bid")},
            [Proposal("T1", "H", "high", (0, 1), 6, 4)],
        )
        print("  same pitches, no tie ->",
              [r["reason"] for r in same["rejected_proposals"]])
        return 0
    finally:
        tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
