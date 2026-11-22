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

-- ===========================================================================
-- 权利室：副本印次/版税权段、取件认领与认列借次（与目录、台账物理分表）
-- ===========================================================================

-- 具名作者
CREATE TABLE IF NOT EXISTS rights_authors (
    author_id TEXT PRIMARY KEY,
    name      TEXT NOT NULL
);

-- 合同版：归属明确到具名作者；同一合同版可在多册副本的印次中出现
CREATE TABLE IF NOT EXISTS rights_editions (
    edition_code TEXT PRIMARY KEY,
    author_id    TEXT NOT NULL REFERENCES rights_authors(author_id),
    title        TEXT NOT NULL
);

-- 权利室为副本登记的印次（不可变；同一副本可有先后多个印次）
CREATE TABLE IF NOT EXISTS rights_impressions (
    impression_id  TEXT PRIMARY KEY,
    copy_id        TEXT NOT NULL,
    impression_seq INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE(copy_id, impression_seq)
);

-- 该印次当时适用的内容清单：一行是一段版税权（具名作者+合同版+权段号）
CREATE TABLE IF NOT EXISTS rights_segments (
    segment_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    impression_id TEXT NOT NULL REFERENCES rights_impressions(impression_id),
    segment_no   INTEGER NOT NULL,
    author_id    TEXT NOT NULL,
    edition_code TEXT NOT NULL REFERENCES rights_editions(edition_code),
    segment_code TEXT NOT NULL,
    UNIQUE(impression_id, segment_no)
);

-- 取件业务日认领：只有台账中 fulfilled 的成功取件可被认领；
-- 失败、过期、重放都不可能生出认领（hold_id 唯一，一取件至多认领一次）
CREATE TABLE IF NOT EXISTS loan_claims (
    claim_id     TEXT PRIMARY KEY,
    op_id        TEXT NOT NULL UNIQUE,
    hold_id      TEXT NOT NULL UNIQUE,
    copy_id      TEXT NOT NULL,
    reader_id    TEXT NOT NULL,
    business_day TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- 认领快照：认领时该印次内容清单逐段留痕，之后目录合并不改写
CREATE TABLE IF NOT EXISTS claim_segments (
    claim_id     TEXT NOT NULL REFERENCES loan_claims(claim_id),
    segment_id   INTEGER NOT NULL,
    author_id    TEXT NOT NULL,
    edition_code TEXT NOT NULL,
    segment_code TEXT NOT NULL,
    PRIMARY KEY (claim_id, segment_id)
);

-- 认列借次：按 (读者, 业务日, 作者, 合同版, 权段) 去重后的真实借次
CREATE TABLE IF NOT EXISTS recognized_loans (
    loan_id        TEXT PRIMARY KEY,
    author_id      TEXT NOT NULL,
    edition_code   TEXT NOT NULL,
    segment_code   TEXT NOT NULL,
    reader_id      TEXT NOT NULL,
    business_day   TEXT NOT NULL,
    first_claim_id TEXT NOT NULL,
    UNIQUE(reader_id, business_day, author_id, edition_code, segment_code)
);

-- 一条已计借次对应哪些真实取件（含被去重的重复取件），供权利室独立复核
CREATE TABLE IF NOT EXISTS recognized_loan_claims (
    loan_id  TEXT NOT NULL REFERENCES recognized_loans(loan_id),
    claim_id TEXT NOT NULL REFERENCES loan_claims(claim_id),
    PRIMARY KEY (loan_id, claim_id)
);

-- 馆报数字：没有真实取件支撑，仅供核对，结算一律不读取
CREATE TABLE IF NOT EXISTS library_reports (
    report_id      TEXT PRIMARY KEY,
    business_day   TEXT NOT NULL,
    edition_code   TEXT NOT NULL,
    reported_count INTEGER NOT NULL CHECK (reported_count >= 0)
);

-- ===========================================================================
-- 结算：期间、费率条款、共池协议与幂等过账
-- ===========================================================================

CREATE TABLE IF NOT EXISTS settle_periods (
    period_id  TEXT PRIMARY KEY,
    author_id  TEXT NOT NULL REFERENCES rights_authors(author_id),
    start_day  TEXT NOT NULL,
    end_day    TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('open', 'posted')),
    created_at TEXT NOT NULL
);

-- 各版每期期初预付（开账时由前期实过账余额结转，首期可显式注入）
CREATE TABLE IF NOT EXISTS edition_period_openings (
    period_id      TEXT NOT NULL REFERENCES settle_periods(period_id),
    edition_code   TEXT NOT NULL,
    opening_prepay INTEGER NOT NULL CHECK (opening_prepay >= 0),
    PRIMARY KEY (period_id, edition_code)
);

-- 各版单开条款：门槛与两档单价（达到门槛则全期每次改用新费）
CREATE TABLE IF NOT EXISTS edition_period_terms (
    period_id    TEXT NOT NULL REFERENCES settle_periods(period_id),
    edition_code TEXT NOT NULL,
    threshold    INTEGER NOT NULL CHECK (threshold > 0),
    low_rate     INTEGER NOT NULL CHECK (low_rate >= 0),
    high_rate    INTEGER NOT NULL CHECK (high_rate >= 0),
    PRIMARY KEY (period_id, edition_code)
);

-- 共池协议：自己的门槛、两档单价与办理费（办理费不可用预付抵）
CREATE TABLE IF NOT EXISTS pool_agreements (
    pool_id      TEXT PRIMARY KEY,
    period_id    TEXT NOT NULL REFERENCES settle_periods(period_id),
    threshold    INTEGER NOT NULL CHECK (threshold > 0),
    low_rate     INTEGER NOT NULL CHECK (low_rate >= 0),
    high_rate    INTEGER NOT NULL CHECK (high_rate >= 0),
    handling_fee INTEGER NOT NULL CHECK (handling_fee >= 0)
);

-- 共池成员：必须整组参加；offset_order 规定组内余额抵款先后
CREATE TABLE IF NOT EXISTS pool_members (
    pool_id      TEXT NOT NULL REFERENCES pool_agreements(pool_id),
    edition_code TEXT NOT NULL,
    offset_order INTEGER NOT NULL,
    PRIMARY KEY (pool_id, edition_code),
    UNIQUE (pool_id, offset_order)
);

-- 过账头：每期至多一条，只按选定方案真正过账一次
CREATE TABLE IF NOT EXISTS posted_settlements (
    period_id        TEXT PRIMARY KEY REFERENCES settle_periods(period_id),
    op_id            TEXT NOT NULL UNIQUE,
    total_author_cash INTEGER NOT NULL CHECK (total_author_cash >= 0),
    total_handling_fee INTEGER NOT NULL CHECK (total_handling_fee >= 0),
    total_new_cash   INTEGER NOT NULL CHECK (total_new_cash >= 0),
    plan_json        TEXT NOT NULL,
    posted_at        TEXT NOT NULL
);

-- 过账的各版清算行：预付实际扣多少、剩多少逐版留痕
CREATE TABLE IF NOT EXISTS posted_edition_lines (
    period_id      TEXT NOT NULL REFERENCES settle_periods(period_id),
    edition_code   TEXT NOT NULL,
    mode           TEXT NOT NULL CHECK (mode IN ('standalone', 'pooled')),
    pool_id        TEXT,
    real_count     INTEGER NOT NULL CHECK (real_count >= 0),
    applied_rate   INTEGER NOT NULL CHECK (applied_rate >= 0),
    gross_payable  INTEGER NOT NULL CHECK (gross_payable >= 0),
    prepay_before  INTEGER NOT NULL CHECK (prepay_before >= 0),
    prepay_deducted INTEGER NOT NULL CHECK (prepay_deducted >= 0),
    prepay_after   INTEGER NOT NULL CHECK (prepay_after >= 0),
    author_cash    INTEGER NOT NULL CHECK (author_cash >= 0),
    PRIMARY KEY (period_id, edition_code)
);

-- 过账的共池清算行
CREATE TABLE IF NOT EXISTS posted_pool_lines (
    period_id     TEXT NOT NULL REFERENCES settle_periods(period_id),
    pool_id       TEXT NOT NULL,
    total_count   INTEGER NOT NULL CHECK (total_count >= 0),
    applied_rate  INTEGER NOT NULL CHECK (applied_rate >= 0),
    gross_payable INTEGER NOT NULL CHECK (gross_payable >= 0),
    handling_fee  INTEGER NOT NULL CHECK (handling_fee >= 0),
    author_cash   INTEGER NOT NULL CHECK (author_cash >= 0),
    PRIMARY KEY (period_id, pool_id)
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
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('claim_counter', '0')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('loan_counter', '0')"
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
