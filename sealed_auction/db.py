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

-- ---------------------------------------------------------------------------
-- Fictional print shop: one unique type plate per settled winning original,
-- press jobs composed of mounted batches, and every printed label tracked as
-- either delivered to its own buyer or torn on the spot (never transferred).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS press_plates (
    plate_id       TEXT PRIMARY KEY,
    auction_id     TEXT NOT NULL,
    buyer          TEXT NOT NULL,
    qty            INTEGER NOT NULL CHECK (qty > 0),
    allowed_heads  TEXT NOT NULL,            -- JSON array, subset of ["L","R"]
    holes          TEXT NOT NULL,            -- JSON array of 0/1, paper-edge order
    flippable      INTEGER NOT NULL CHECK (flippable IN (0, 1)),
    created_at     TEXT NOT NULL
);
-- A buyer's original maps to a single unique plate; it can never be duplicated
-- into two heads, so (auction, buyer) is unique.
CREATE UNIQUE INDEX IF NOT EXISTS ux_plate_original
    ON press_plates(auction_id, buyer);

CREATE TABLE IF NOT EXISTS press_jobs (
    job_id              TEXT PRIMARY KEY,
    paper_limit         INTEGER NOT NULL CHECK (paper_limit > 0),
    max_changes         INTEGER NOT NULL CHECK (max_changes >= 0),
    paper_used          INTEGER NOT NULL DEFAULT 0,
    changes_used        INTEGER NOT NULL DEFAULT 0,
    torn_total          INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL CHECK (status IN ('PLANNED','PULLED')),
    plan_json           TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    pulled_at           TEXT
);

CREATE TABLE IF NOT EXISTS press_batches (
    batch_seq       INTEGER NOT NULL,
    job_id          TEXT NOT NULL REFERENCES press_jobs(job_id),
    head_left       TEXT,                    -- plate_id or NULL (empty head)
    head_right      TEXT,
    orient_left     TEXT CHECK (orient_left IS NULL OR orient_left IN ('NORMAL','REVERSED')),
    orient_right    TEXT CHECK (orient_right IS NULL OR orient_right IN ('NORMAL','REVERSED')),
    sheets          INTEGER NOT NULL CHECK (sheets BETWEEN 1 AND 10),
    is_change       INTEGER NOT NULL CHECK (is_change IN (0, 1)),
    PRIMARY KEY (job_id, batch_seq)
);

CREATE TABLE IF NOT EXISTS press_labels (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL REFERENCES press_jobs(job_id),
    batch_seq     INTEGER NOT NULL,
    plate_id      TEXT NOT NULL REFERENCES press_plates(plate_id),
    head          TEXT NOT NULL CHECK (head IN ('L','R')),
    buyer         TEXT NOT NULL,
    outcome       TEXT NOT NULL CHECK (outcome IN ('DELIVERED','TORN')),
    request_id    TEXT NOT NULL,
    at            TEXT NOT NULL,
    FOREIGN KEY (job_id, batch_seq) REFERENCES press_batches(job_id, batch_seq)
);
CREATE INDEX IF NOT EXISTS idx_labels_job ON press_labels(job_id);
CREATE INDEX IF NOT EXISTS idx_labels_plate ON press_labels(plate_id);
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
