"""SQLite storage shared by the two services.

The auction service and margin warehouse are separate Python services, but
they may use a shared transaction (explicitly allowed by the problem) so a
bid revision and its collateral adjustment commit atomically.  Durable
intents/receipts are still used at the settlement boundary, where each
margin operation is its own idempotent transaction.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS auctions (
    auction_id       TEXT PRIMARY KEY,
    units            INTEGER NOT NULL CHECK (units BETWEEN 1 AND 10),
    status           TEXT NOT NULL CHECK (status IN ('OPEN','FROZEN','SETTLED')),
    created_at       TEXT NOT NULL,
    frozen_at        TEXT,
    settled_at       TEXT
);

CREATE TABLE IF NOT EXISTS holds (
    hold_id          TEXT PRIMARY KEY,
    buyer            TEXT NOT NULL,
    auction_id       TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL CHECK (amount_cents >= 0),
    status           TEXT NOT NULL CHECK (status IN ('ACTIVE','RELEASED','CONSUMED')),
    charged_cents    INTEGER NOT NULL DEFAULT 0 CHECK (charged_cents >= 0),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_holds_buyer ON holds(buyer);
CREATE INDEX IF NOT EXISTS idx_holds_auction ON holds(auction_id);

CREATE TABLE IF NOT EXISTS bids (
    bid_id            TEXT PRIMARY KEY,
    auction_id        TEXT NOT NULL REFERENCES auctions(auction_id),
    buyer             TEXT NOT NULL,
    qty               INTEGER NOT NULL CHECK (qty > 0),
    unit_price_cents  INTEGER NOT NULL CHECK (unit_price_cents > 0),
    revision          INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    status            TEXT NOT NULL CHECK (status IN ('ACTIVE','CANCELLED')),
    hold_id           TEXT NOT NULL REFERENCES holds(hold_id),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
-- At most one valid bid per buyer per auction.
CREATE UNIQUE INDEX IF NOT EXISTS ux_bid_active_buyer
    ON bids(auction_id, buyer) WHERE status = 'ACTIVE';
-- A hold can back at most one valid bid at a time.
CREATE UNIQUE INDEX IF NOT EXISTS ux_bid_active_hold
    ON bids(hold_id) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS idx_bids_auction ON bids(auction_id);

CREATE TABLE IF NOT EXISTS bid_events (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        TEXT NOT NULL,
    bid_id            TEXT NOT NULL,
    auction_id        TEXT NOT NULL,
    buyer             TEXT NOT NULL,
    action            TEXT NOT NULL CHECK (action IN ('PLACE','REVISE','CANCEL')),
    qty               INTEGER NOT NULL,
    unit_price_cents  INTEGER NOT NULL,
    revision          INTEGER NOT NULL,
    hold_id           TEXT NOT NULL,
    at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bid_events_auction ON bid_events(auction_id);

CREATE TABLE IF NOT EXISTS ledger_events (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer             TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('DEPOSIT','FREEZE','RELEASE','CHARGE')),
    amount_cents      INTEGER NOT NULL CHECK (amount_cents > 0),
    hold_id           TEXT,
    auction_id        TEXT,
    request_id        TEXT NOT NULL,
    at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_buyer ON ledger_events(buyer);
CREATE INDEX IF NOT EXISTS idx_ledger_hold ON ledger_events(hold_id);
CREATE INDEX IF NOT EXISTS idx_ledger_auction ON ledger_events(auction_id);

CREATE TABLE IF NOT EXISTS receipts (
    request_id        TEXT PRIMARY KEY,
    operation         TEXT NOT NULL,
    fingerprint       TEXT NOT NULL,
    response_json     TEXT NOT NULL,
    at                TEXT NOT NULL
);

-- One hold may be charged at most once per auction, independent of retries.
CREATE UNIQUE INDEX IF NOT EXISTS ux_charge_once
    ON ledger_events(auction_id, hold_id) WHERE kind = 'CHARGE';

CREATE TABLE IF NOT EXISTS settlements (
    auction_id             TEXT PRIMARY KEY REFERENCES auctions(auction_id),
    clearing_price_cents   INTEGER,
    allocations_json       TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('FROZEN','CLEARING','CLEARED')),
    created_at             TEXT NOT NULL,
    cleared_at             TEXT
);

-- Charity registration captured at bid time: for the lot's 1st, 2nd, ... unit
-- the buyer records the non-donation rebate valuation and which foundations
-- may use that unit's donation.  Replaced wholesale on bid revision.
CREATE TABLE IF NOT EXISTS bid_charity (
    bid_id            TEXT NOT NULL REFERENCES bids(bid_id),
    unit_index        INTEGER NOT NULL CHECK (unit_index >= 1),
    rebate_cents      INTEGER NOT NULL CHECK (rebate_cents >= 0),
    allow_json        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (bid_id, unit_index)
);

-- One named receipt per actually allocated unit, issued only after the
-- auction is CLEARED (donations recognise settled funds only).
CREATE TABLE IF NOT EXISTS donation_receipts (
    receipt_id        TEXT PRIMARY KEY,
    auction_id        TEXT NOT NULL,
    bid_id            TEXT NOT NULL,
    donor             TEXT NOT NULL,
    unit_index        INTEGER NOT NULL,
    unit_price_cents  INTEGER NOT NULL,
    rebate_cents      INTEGER NOT NULL,
    donation_cents    INTEGER NOT NULL CHECK (donation_cents >= 0),
    zero_reason       TEXT,
    allow_json        TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('ISSUED','COMMITTED')),
    committed_to      TEXT,
    claim_id          TEXT,
    created_at        TEXT NOT NULL,
    committed_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_receipt_unit
    ON donation_receipts(auction_id, bid_id, unit_index);
CREATE INDEX IF NOT EXISTS idx_receipts_status ON donation_receipts(status);

CREATE TABLE IF NOT EXISTS match_invitations (
    invitation_id       TEXT PRIMARY KEY,
    round_id            TEXT NOT NULL,
    foundation          TEXT NOT NULL,
    min_donation_cents  INTEGER NOT NULL CHECK (min_donation_cents >= 0),
    min_donors          INTEGER NOT NULL CHECK (min_donors >= 1),
    match_cents         INTEGER NOT NULL CHECK (match_cents >= 1),
    status              TEXT NOT NULL CHECK (status IN ('OPEN','CLAIMED')),
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invitations_round ON match_invitations(round_id);

CREATE TABLE IF NOT EXISTS match_claims (
    claim_id          TEXT PRIMARY KEY,
    round_id          TEXT NOT NULL,
    invitation_id     TEXT NOT NULL REFERENCES match_invitations(invitation_id),
    foundation        TEXT NOT NULL,
    matched_cents     INTEGER NOT NULL,
    donation_cents    INTEGER NOT NULL,
    donor_count       INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);
-- An invitation enters at most one confirmed claim.
CREATE UNIQUE INDEX IF NOT EXISTS ux_claim_invitation
    ON match_claims(invitation_id);

CREATE TABLE IF NOT EXISTS claim_receipts (
    claim_id          TEXT NOT NULL REFERENCES match_claims(claim_id),
    receipt_id        TEXT NOT NULL REFERENCES donation_receipts(receipt_id),
    PRIMARY KEY (claim_id, receipt_id)
);
-- Last line of defence: two agents racing the same real receipt can confirm
-- only one claim; a committed receipt is never recounted by a later trial.
CREATE UNIQUE INDEX IF NOT EXISTS ux_claim_receipt_once
    ON claim_receipts(receipt_id);
"""


class Database:
    """Thin SQLite wrapper using one connection per transaction/read.

    BEGIN IMMEDIATE serialises writers.  Two concurrent freeze attempts for
    the last available cent therefore cannot both observe the same balance.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_schema(self) -> None:
        conn = self.connect()
        try:
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    @contextlib.contextmanager
    def tx(self):
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def read(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()
