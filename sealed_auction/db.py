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

-- ---------------------------------------------------------------------
-- Fictional astronomy exhibit (see exhibit.py).  All coordinates are
-- integers in the exhibit's own synthetic plane; a night has 15 exposures.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS exhibit_stations (
    station          TEXT PRIMARY KEY,
    x                INTEGER NOT NULL,
    y                INTEGER NOT NULL,
    move_limit       INTEGER NOT NULL CHECK (move_limit >= 0),
    mirror_min       INTEGER,
    mirror_max       INTEGER,
    created_at       TEXT NOT NULL
);

-- Per-station per-exposure duty roster; exposure numbers are 1..15.
CREATE TABLE IF NOT EXISTS exhibit_duty (
    station          TEXT NOT NULL REFERENCES exhibit_stations(station),
    exposure         INTEGER NOT NULL CHECK (exposure BETWEEN 1 AND 15),
    available        INTEGER NOT NULL CHECK (available IN (0, 1)),
    PRIMARY KEY (station, exposure)
);

CREATE TABLE IF NOT EXISTS exhibit_stars (
    star             TEXT PRIMARY KEY,
    radius           INTEGER NOT NULL CHECK (radius > 0),
    created_at       TEXT NOT NULL
);

-- Occultation circle centre of a star at a given exposure.
CREATE TABLE IF NOT EXISTS exhibit_star_centers (
    star             TEXT NOT NULL REFERENCES exhibit_stars(star),
    exposure         INTEGER NOT NULL CHECK (exposure BETWEEN 1 AND 15),
    cx               INTEGER NOT NULL,
    cy               INTEGER NOT NULL,
    PRIMARY KEY (star, exposure)
);

-- Integer mirror position a station must hold to aim at a star.
CREATE TABLE IF NOT EXISTS exhibit_star_aims (
    star             TEXT NOT NULL REFERENCES exhibit_stars(star),
    station          TEXT NOT NULL REFERENCES exhibit_stations(station),
    mirror           INTEGER NOT NULL,
    PRIMARY KEY (star, station)
);

-- Observation orders registered by bidding teams.
CREATE TABLE IF NOT EXISTS exhibit_orders (
    order_id         TEXT PRIMARY KEY,
    buyer            TEXT NOT NULL,
    star             TEXT NOT NULL REFERENCES exhibit_stars(star),
    center_exposure  INTEGER NOT NULL CHECK (center_exposure BETWEEN 2 AND 14),
    value            INTEGER NOT NULL CHECK (value > 0),
    status           TEXT NOT NULL CHECK (status IN ('PENDING','SCORED')),
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exhibit_orders_buyer ON exhibit_orders(buyer);

CREATE TABLE IF NOT EXISTS exhibit_nights (
    night_id         TEXT PRIMARY KEY,
    total_value      INTEGER NOT NULL,
    openings_used    INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);

-- Signed real exposures ("原曝光").  The primary key makes a station
-- exposure spendable at most once across all nights.
CREATE TABLE IF NOT EXISTS exhibit_exposures (
    station          TEXT NOT NULL REFERENCES exhibit_stations(station),
    exposure         INTEGER NOT NULL CHECK (exposure BETWEEN 1 AND 15),
    night_id         TEXT NOT NULL REFERENCES exhibit_nights(night_id),
    order_id         TEXT NOT NULL REFERENCES exhibit_orders(order_id),
    buyer            TEXT NOT NULL,
    star             TEXT NOT NULL,
    center_exposure  INTEGER NOT NULL,
    mirror           INTEGER NOT NULL,
    light            TEXT NOT NULL CHECK (light IN ('bright','dark')),
    created_at       TEXT NOT NULL,
    PRIMARY KEY (station, exposure)
);
CREATE INDEX IF NOT EXISTS idx_exhibit_exposures_night ON exhibit_exposures(night_id);

-- Openings spent per buyer per scored order (one row per station used).
CREATE TABLE IF NOT EXISTS exhibit_openings (
    night_id         TEXT NOT NULL REFERENCES exhibit_nights(night_id),
    buyer            TEXT NOT NULL,
    order_id         TEXT NOT NULL REFERENCES exhibit_orders(order_id),
    station          TEXT NOT NULL REFERENCES exhibit_stations(station),
    openings         INTEGER NOT NULL CHECK (openings > 0),
    created_at       TEXT NOT NULL,
    PRIMARY KEY (night_id, order_id, station)
);
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
