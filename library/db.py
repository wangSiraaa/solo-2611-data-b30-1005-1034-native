"""SQLite 连接、schema、元数据与幂等基础设施。

设计要点：
- 目录修订是全量快照，写入即不可变；alias_edges 是单跳、单向边，
  多跳规范书目的解析在读时进行（见 catalog.CatalogService.canonical_map）。
- 副本身份（copy_id）跨修订稳定；每修订单独保存其原始书目 ID、取件馆、装帧，
  后续修订拆别名也不会揉掉原始身份。
- 台账与目录物理上分表，台账不定义读者顺序，只保存票据上声明的到达序号；
  保留表用部分唯一索引保证“一票据/一副本同时至多一条未释放保留”。
- 所有写操作带操作 ID，operations 表保存请求指纹，重放/异文在事务开头识别。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 单行元数据：当前头修订与全局计数器
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 目录修订（不可变快照）
CREATE TABLE IF NOT EXISTS revisions (
    revision   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

-- 修订内声明的全部书目（稳定书目 ID）
CREATE TABLE IF NOT EXISTS revision_bibs (
    revision TEXT NOT NULL,
    bib_id   TEXT NOT NULL,
    PRIMARY KEY (revision, bib_id),
    FOREIGN KEY (revision) REFERENCES revisions(revision)
);

-- 单向别名边：单跳，from -> to；同一修订内每个 from 只允许一条出边
CREATE TABLE IF NOT EXISTS alias_edges (
    revision TEXT NOT NULL,
    from_bib TEXT NOT NULL,
    to_bib   TEXT NOT NULL,
    PRIMARY KEY (revision, from_bib),
    FOREIGN KEY (revision) REFERENCES revisions(revision)
);

-- 各副本在该修订下的原始身份与属性（同一副本跨修订保留同一 copy_id）
CREATE TABLE IF NOT EXISTS revision_copies (
    revision      TEXT NOT NULL,
    copy_id       TEXT NOT NULL,
    original_bib  TEXT NOT NULL,
    branch        TEXT NOT NULL,
    format        TEXT NOT NULL,
    PRIMARY KEY (revision, copy_id),
    FOREIGN KEY (revision) REFERENCES revisions(revision)
);

-- 等待票据：原书目身份、到达序号、允许的馆/装帧集合（JSON 数组）
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id        TEXT PRIMARY KEY,
    reader_id        TEXT NOT NULL,
    original_bib     TEXT NOT NULL,
    arrival_seq      INTEGER NOT NULL,
    allowed_branches TEXT NOT NULL,
    allowed_formats  TEXT NOT NULL,
    status           TEXT NOT NULL
        CHECK (status IN ('waiting', 'held', 'fulfilled', 'cancelled')),
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_waiting
    ON tickets(arrival_seq, ticket_id) WHERE status = 'waiting';

-- 保留（含已兑现的历史保留，永不物理删除）。
-- 锁定当时的票据、读者、馆、副本与目录修订；之后目录更正不影响已保留副本。
CREATE TABLE IF NOT EXISTS reservations (
    hold_id           TEXT PRIMARY KEY,
    revision          TEXT NOT NULL,
    ticket_id         TEXT NOT NULL,
    reader_id         TEXT NOT NULL,
    copy_id           TEXT NOT NULL,
    branch            TEXT NOT NULL,
    status            TEXT NOT NULL
        CHECK (status IN ('held', 'fulfilled', 'expired', 'cancelled')),
    created_at        TEXT NOT NULL,
    fulfilled_at      TEXT,
    released_at       TEXT,
    release_reason    TEXT
);
-- 未释放（held/fulfilled）的保留对票据、副本分别唯一
CREATE UNIQUE INDEX IF NOT EXISTS idx_resv_open_ticket
    ON reservations(ticket_id) WHERE status IN ('held', 'fulfilled');
CREATE UNIQUE INDEX IF NOT EXISTS idx_resv_open_copy
    ON reservations(copy_id) WHERE status IN ('held', 'fulfilled');

-- 柜台准备（两步式 prepare/commit），过期准备保留为记录可审计
CREATE TABLE IF NOT EXISTS preparations (
    prep_id        TEXT PRIMARY KEY,
    op_id          TEXT,
    revision       TEXT NOT NULL,
    copy_id        TEXT NOT NULL,
    wait_version   INTEGER NOT NULL,
    selected       TEXT,
    reader_id      TEXT,
    branch         TEXT,
    state          TEXT NOT NULL
        CHECK (state IN ('prepared', 'committed', 'rejected', 'stale')),
    created_at     TEXT NOT NULL
);

-- 操作幂等记录：操作 ID 唯一，保存请求指纹
CREATE TABLE IF NOT EXISTS operations (
    op_id      TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 全部回执（成功与拒绝都留痕；准备也产生回执）
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id       TEXT,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'rejected', 'conflict')),
    result      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_op ON receipts(op_id);

-- ------------------------------------------------------------------ 口述凭单
-- 读者签认的真实阅读：每条只挂在一次真实成功的取件（fulfilled 保留）上。
-- 保留/过期/取消/错人取件都不满足，因而无法登记。
-- order_no 是读者自己的阅读次序；同一读者同一次序冲突，须核正后才可登记。
CREATE TABLE IF NOT EXISTS readings (
    reading_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    hold_id         TEXT NOT NULL,               -- 一次取件最多一条未撤回的阅读
    ticket_id       TEXT NOT NULL,
    reader_id       TEXT NOT NULL,
    copy_id         TEXT NOT NULL,
    original_bib    TEXT NOT NULL,               -- 取件时副本的原始书目（不随目录改）
    order_no        INTEGER NOT NULL,
    citable         INTEGER NOT NULL CHECK (citable IN (0, 1)),
    retracted       INTEGER NOT NULL DEFAULT 0 CHECK (retracted IN (0, 1)),
    pickup_revision TEXT NOT NULL,               -- 取件当时锁定的目录修订
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_reader ON readings(reader_id, order_no);
-- 一次取件只允许一条未撤回的阅读；撤回的旧行保留供审计，可重新登记核正
CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_hold_active
    ON readings(hold_id) WHERE retracted = 0;
-- 同一读者同一次序只允许一条未撤回的阅读
CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_reader_order_active
    ON readings(reader_id, order_no) WHERE retracted = 0;

-- 采访场次：有限席位与一次性开场费
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    capacity   INTEGER NOT NULL CHECK (capacity >= 0),
    fee        INTEGER NOT NULL CHECK (fee >= 0),
    created_at TEXT NOT NULL
);

-- 读者签过名的采访场次（只在自己签过的场次出席）
CREATE TABLE IF NOT EXISTS session_signups (
    reader_id  TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (reader_id, session_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

-- 已交出版的口述凭单：记录当时所用的目录修订、规范两端与两端真实取件，
-- 并额外保存原始书目，使目录后来拆并时旧凭单仍能回答“是哪两本”。
-- 每次重新出具产生新 epoch，旧 epoch 全部保留可查；排展只取最新 epoch。
CREATE TABLE IF NOT EXISTS vouchers (
    voucher_id     TEXT PRIMARY KEY,
    revision       TEXT NOT NULL,
    epoch          INTEGER NOT NULL,
    reader_id      TEXT NOT NULL,
    from_canonical TEXT NOT NULL,
    to_canonical   TEXT NOT NULL,
    from_reading   INTEGER NOT NULL,
    to_reading     INTEGER NOT NULL,
    from_book      TEXT NOT NULL,
    to_book        TEXT NOT NULL,
    from_hold      TEXT NOT NULL,
    to_hold        TEXT NOT NULL,
    from_copy      TEXT NOT NULL,
    to_copy        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (revision, epoch, reader_id, from_reading, to_reading)
);
CREATE INDEX IF NOT EXISTS idx_vouchers_rev_epoch ON vouchers(revision, epoch);
CREATE INDEX IF NOT EXISTS idx_vouchers_edge
    ON vouchers(revision, epoch, from_canonical, to_canonical);

-- 落定的排展计划：逐段受访人、场次与席位（同场同席不能再签给另一人）
CREATE TABLE IF NOT EXISTS plans (
    plan_id    TEXT PRIMARY KEY,
    revision   TEXT NOT NULL,
    chain      TEXT NOT NULL,
    total_fee  INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_sessions (
    plan_id    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    fee        INTEGER NOT NULL,
    PRIMARY KEY (plan_id, session_id),
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
);
CREATE TABLE IF NOT EXISTS plan_edges (
    plan_id    TEXT NOT NULL,
    edge_index INTEGER NOT NULL,
    from_work  TEXT NOT NULL,
    to_work    TEXT NOT NULL,
    reader_id  TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seat_no    INTEGER NOT NULL,
    voucher    TEXT,
    PRIMARY KEY (plan_id, edge_index),
    UNIQUE (plan_id, reader_id),              -- 一人最多讲一次
    UNIQUE (plan_id, session_id, seat_no),    -- 同一席不能签给另一人
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
);
"""


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------

def connect(db_path: str) -> sqlite3.Connection:
    """打开（或新建）数据库并确保 schema 就位。

    db_path=":memory:" 时每次调用是独立内存库，测试中由系统对象持有同一连接。
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # isolation_level=None：事务完全由服务层显式 BEGIN/COMMIT 控制，
    # 避免驱动隐式事务与 BEGIN IMMEDIATE 叠加
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('head_revision', '')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('wait_version', '0')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('hold_counter', '0')"
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 时间 / 指纹 / 元数据
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def fingerprint(kind: str, payload: dict[str, Any]) -> str:
    """请求内容指纹：同一操作 ID 同内容可重放，异文拒绝。

    sort_keys 保证字典键序不影响指纹；分隔符固定，避免字段拼接歧义。
    """
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{kind}|{body}".encode("utf-8")).hexdigest()


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def bump_wait_version(conn: sqlite3.Connection) -> int:
    """等待状态变化（新票据/提交/释放）时自增，使旧准备整体失效。"""
    nxt = int(get_meta(conn, "wait_version", "0")) + 1
    set_meta(conn, "wait_version", str(nxt))
    return nxt


def next_hold_id(conn: sqlite3.Connection) -> str:
    nxt = int(get_meta(conn, "hold_counter", "0")) + 1
    set_meta(conn, "hold_counter", str(nxt))
    return f"H{nxt}"


# ---------------------------------------------------------------------------
# 幂等与回执
# ---------------------------------------------------------------------------

def check_idempotent(
    conn: sqlite3.Connection,
    op_id: str,
    kind: str,
    fp: str,
) -> dict | None:
    """事务开头调用。

    - 操作 ID 不存在：返回 None，继续正常处理（调用方随后登记操作）。
    - 同 ID 同指纹：返回上次回执 result（重放，调用方原样返回，不再写库）。
    - 同 ID 异指纹：抛 IdempotencyConflict，事务回滚，不留任何占位。
    """
    row = conn.execute(
        "SELECT fingerprint FROM operations WHERE op_id=?", (op_id,)
    ).fetchone()
    if row is None:
        return None
    if row["fingerprint"] != fp:
        from .errors import IdempotencyConflict

        raise IdempotencyConflict(op_id, kind)
    receipt = conn.execute(
        "SELECT result FROM receipts WHERE op_id=? ORDER BY receipt_id LIMIT 1",
        (op_id,),
    ).fetchone()
    return json.loads(receipt["result"]) if receipt is not None else {}


def record_operation(
    conn: sqlite3.Connection, op_id: str, kind: str, fp: str
) -> None:
    conn.execute(
        "INSERT INTO operations(op_id, kind, fingerprint, created_at) "
        "VALUES(?, ?, ?, ?)",
        (op_id, kind, fp, now_iso()),
    )


def add_receipt(
    conn: sqlite3.Connection,
    kind: str,
    status: str,
    result: dict[str, Any],
    op_id: str | None = None,
) -> dict[str, Any]:
    conn.execute(
        "INSERT INTO receipts(op_id, kind, status, result, created_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (op_id, kind, status, json.dumps(result, ensure_ascii=False), now_iso()),
    )
    return result
