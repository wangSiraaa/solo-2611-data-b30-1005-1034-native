"""读者口述凭单：真实取件、阅读次序、同意公开与相邻过渡凭单。

规则边界
--------
1. 只有**真实成功的 pick-up**（保留状态 fulfilled）才能登记为“读过”。
   仍在保留、过期、取消、或错人取件的保留都不是候选，登记一律拒绝。
2. 每条阅读绑定唯一的 hold_id，记录读者、副本、原始书目、读者自己的次序 order_no
   以及是否同意公开 citable。同一读者同一次序冲突必须核正（撤回错误条后再登记）。
3. 出具凭单时，用**选定目录修订**把每条阅读的“原副本作品”归一（canonical）。
   连续读到同一规范作品的多次阅读合为一段；只在不同规范段之间形成有向过渡。
4. 相邻两段都必须各自落在**获准引用**的真实取件上，且中间没有未同意的整段，
   才交出一条有向过渡凭单。凭单给出两端真实取件（hold/copy）、受访人与
   两端所读的原始书目。旧版凭单永久保留，目录拆并后仍能回答“是哪两本”。
5. 出具是“快照 + epoch”：补录或撤回后必须重新出具，排展才看得到变化；
   旧 epoch 的凭单原样保留供追溯。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db
from .errors import Rejection

MAX_WORKS = 9


class OralService:
    def __init__(self, conn: sqlite3.Connection, catalog: Any, ledger: Any) -> None:
        self.conn = conn
        self.catalog = catalog
        self.ledger = ledger

    # ================================================================ 阅读登记

    def register_reading(
        self,
        op_id: str,
        hold_id: str,
        order_no: int,
        citable: bool,
        reader_id: str | None = None,
    ) -> dict[str, Any]:
        """把一次真实成功取件登记为读者的阅读。

        读者取自保留本身；若显式给出 reader_id 则必须一致。
        citable 为读者是否同意在书展上公开引用。
        """
        payload = {
            "hold_id": hold_id,
            "order_no": order_no,
            "citable": bool(citable),
            "reader_id": reader_id,
        }
        fp = db.fingerprint("register_reading", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "register_reading", fp)
            if prior is not None:
                conn.commit()
                return prior
            hold = conn.execute(
                "SELECT * FROM reservations WHERE hold_id=?", (hold_id,)
            ).fetchone()
            if hold is None:
                raise Rejection("unknown_hold", hold=hold_id)
            # 只有真实成功的取件才是“读过”的候选
            if hold["status"] != "fulfilled":
                raise Rejection(
                    "pickup_not_fulfilled",
                    hold=hold_id,
                    status=hold["status"],
                )
            if reader_id is not None and reader_id != hold["reader_id"]:
                raise Rejection(
                    "reader_mismatch",
                    hold=hold_id,
                    expected=hold["reader_id"],
                    given=reader_id,
                )
            reader = hold["reader_id"]
            if order_no < 0:
                raise Rejection("bad_order_no", order=order_no)
            if conn.execute(
                "SELECT 1 FROM readings WHERE hold_id=? AND retracted=0",
                (hold_id,),
            ).fetchone():
                raise Rejection("reading_exists", hold=hold_id)
            copy = conn.execute(
                "SELECT original_bib FROM revision_copies "
                "WHERE revision=? AND copy_id=?",
                (hold["revision"], hold["copy_id"]),
            ).fetchone()
            if copy is None:
                raise Rejection(
                    "copy_not_in_pickup_revision",
                    hold=hold_id,
                    revision=hold["revision"],
                )
            # 同一读者同一次序冲突：必须撤回旧条核正后才能登记
            conflict = conn.execute(
                "SELECT reading_id, hold_id FROM readings "
                "WHERE reader_id=? AND order_no=? AND retracted=0",
                (reader, order_no),
            ).fetchone()
            if conflict is not None:
                raise Rejection(
                    "order_conflict",
                    reader=reader,
                    order_no=order_no,
                    existing_reading=conflict["reading_id"],
                    existing_hold=conflict["hold_id"],
                )
            cur = conn.execute(
                "INSERT INTO readings(hold_id, ticket_id, reader_id, copy_id, "
                "original_bib, order_no, citable, retracted, pickup_revision, "
                "created_at) VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    hold_id,
                    hold["ticket_id"],
                    reader,
                    hold["copy_id"],
                    copy["original_bib"],
                    order_no,
                    1 if citable else 0,
                    hold["revision"],
                    db.now_iso(),
                ),
            )
            reading_id = cur.lastrowid
            result = {
                "status": "ok",
                "reading_id": reading_id,
                "hold_id": hold_id,
                "ticket": hold["ticket_id"],
                "reader": reader,
                "copy": hold["copy_id"],
                "original_bib": copy["original_bib"],
                "order_no": order_no,
                "citable": bool(citable),
                "pickup_revision": hold["revision"],
            }
            db.record_operation(conn, op_id, "register_reading", fp)
            db.add_receipt(conn, "register_reading", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("register_reading", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def retract_reading(self, op_id: str, reading_id: int) -> dict[str, Any]:
        """撤回一条阅读（核正或改写口述时的唯一合法入口）。旧行保留可审计。"""
        payload = {"reading_id": reading_id}
        fp = db.fingerprint("retract_reading", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "retract_reading", fp)
            if prior is not None:
                conn.commit()
                return prior
            row = conn.execute(
                "SELECT * FROM readings WHERE reading_id=?", (reading_id,)
            ).fetchone()
            if row is None:
                raise Rejection("unknown_reading", reading=reading_id)
            if row["retracted"]:
                raise Rejection("reading_already_retracted", reading=reading_id)
            conn.execute(
                "UPDATE readings SET retracted=1 WHERE reading_id=?", (reading_id,)
            )
            result = {
                "status": "ok",
                "reading_id": reading_id,
                "reader": row["reader_id"],
                "original_bib": row["original_bib"],
                "retracted": True,
            }
            db.record_operation(conn, op_id, "retract_reading", fp)
            db.add_receipt(conn, "retract_reading", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("retract_reading", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    # ================================================================ 出具凭单

    def issue_vouchers(self, op_id: str, revision: str) -> dict[str, Any]:
        """用选定目录版把所有真实阅读归一并出具相邻过渡凭单（新 epoch）。

        先从每位读者自己的次序里把“连续读同一规范作品”合为一段，
        再只在不同段之间、且两端阅读都获准引用时出具凭单。
        中间若夹有未同意公开的整段，则两侧不会被拼接。
        """
        payload = {"revision": revision}
        fp = db.fingerprint("issue_vouchers", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "issue_vouchers", fp)
            if prior is not None:
                conn.commit()
                return prior
            # 修订必须存在
            if not conn.execute(
                "SELECT 1 FROM revisions WHERE revision=?", (revision,)
            ).fetchone():
                raise Rejection("unknown_revision", revision=revision)
            canon = self.catalog.canonical_map(revision)
            rows = conn.execute(
                "SELECT * FROM readings WHERE retracted=0 "
                "ORDER BY reader_id, order_no, reading_id"
            ).fetchall()

            epoch_row = conn.execute(
                "SELECT COALESCE(MAX(epoch), 0) AS e FROM vouchers WHERE revision=?",
                (revision,),
            ).fetchone()
            epoch = epoch_row["e"] + 1
            ts = db.now_iso()
            vouchers: list[dict[str, Any]] = []
            seq = 0
            by_reader: dict[str, list[sqlite3.Row]] = {}
            for r in rows:
                by_reader.setdefault(r["reader_id"], []).append(r)

            for reader in sorted(by_reader):
                segments = self._segments(by_reader[reader], canon)
                for left, right in zip(segments, segments[1:]):
                    # 相邻两段都各有一条获准引用的真实取件才可交出版
                    if not (left["last"]["citable"] and right["first"]["citable"]):
                        continue
                    seq += 1
                    vid = f"V-{revision}-{epoch}-{seq:03d}"
                    lr, rr = left["last"], right["first"]
                    conn.execute(
                        "INSERT INTO vouchers(voucher_id, revision, epoch, "
                        "reader_id, from_canonical, to_canonical, from_reading, "
                        "to_reading, from_book, to_book, from_hold, to_hold, "
                        "from_copy, to_copy, created_at) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            vid,
                            revision,
                            epoch,
                            reader,
                            left["canonical"],
                            right["canonical"],
                            lr["reading_id"],
                            rr["reading_id"],
                            lr["original_bib"],
                            rr["original_bib"],
                            lr["hold_id"],
                            rr["hold_id"],
                            lr["copy_id"],
                            rr["copy_id"],
                            ts,
                        ),
                    )
                    vouchers.append(
                        {
                            "voucher_id": vid,
                            "reader": reader,
                            "from_canonical": left["canonical"],
                            "to_canonical": right["canonical"],
                            "from_reading": lr["reading_id"],
                            "to_reading": rr["reading_id"],
                            "from_book": lr["original_bib"],
                            "to_book": rr["original_bib"],
                            "from_hold": lr["hold_id"],
                            "to_hold": rr["hold_id"],
                            "from_copy": lr["copy_id"],
                            "to_copy": rr["copy_id"],
                        }
                    )
            result = {
                "status": "ok",
                "revision": revision,
                "epoch": epoch,
                "vouchers": vouchers,
            }
            db.record_operation(conn, op_id, "issue_vouchers", fp)
            db.add_receipt(conn, "issue_vouchers", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("issue_vouchers", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _segments(
        readings: list[sqlite3.Row], canon: dict[str, str]
    ) -> list[dict[str, Any]]:
        """按读者自己的次序，把连续同一规范作品合为一段。

        返回段列表，每段保留 canonical 与首/末两条阅读，供相邻过渡取端点。
        原书目不在该修订目录内的阅读会造成断段（不会跨过它拼接）。
        """
        segments: list[dict[str, Any]] = []
        cur: dict[str, Any] | None = None
        for r in readings:
            c = canon.get(r["original_bib"])
            if c is None:
                # 该修订不认识这本原始书目：强制断段，避免误拼
                cur = None
                continue
            if cur is not None and cur["canonical"] == c:
                cur["last"] = r
                continue
            cur = {"canonical": c, "first": r, "last": r}
            segments.append(cur)
        return segments

    # ================================================================ 采访场次

    def register_session(
        self, op_id: str, session_id: str, capacity: int, fee: int
    ) -> dict[str, Any]:
        payload = {"session_id": session_id, "capacity": capacity, "fee": fee}
        fp = db.fingerprint("register_session", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "register_session", fp)
            if prior is not None:
                conn.commit()
                return prior
            if capacity < 0 or fee < 0:
                raise Rejection(
                    "bad_session_terms",
                    session=session_id,
                    capacity=capacity,
                    fee=fee,
                )
            if conn.execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone():
                raise Rejection("session_exists", session=session_id)
            count = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            if count >= 6:
                raise Rejection("too_many_sessions", limit=6)
            conn.execute(
                "INSERT INTO sessions(session_id, capacity, fee, created_at) "
                "VALUES(?, ?, ?, ?)",
                (session_id, capacity, fee, db.now_iso()),
            )
            result = {
                "status": "ok",
                "session_id": session_id,
                "capacity": capacity,
                "fee": fee,
            }
            db.record_operation(conn, op_id, "register_session", fp)
            db.add_receipt(conn, "register_session", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("register_session", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def set_session_fee(
        self, op_id: str, session_id: str, fee: int
    ) -> dict[str, Any]:
        """仅调整某场开场费（其余条款不变）。已落定计划的费用不改写。"""
        payload = {"session_id": session_id, "fee": fee}
        fp = db.fingerprint("set_session_fee", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "set_session_fee", fp)
            if prior is not None:
                conn.commit()
                return prior
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise Rejection("unknown_session", session=session_id)
            if fee < 0:
                raise Rejection("bad_session_terms", session=session_id, fee=fee)
            conn.execute(
                "UPDATE sessions SET fee=? WHERE session_id=?", (fee, session_id)
            )
            result = {
                "status": "ok",
                "session_id": session_id,
                "old_fee": row["fee"],
                "fee": fee,
            }
            db.record_operation(conn, op_id, "set_session_fee", fp)
            db.add_receipt(conn, "set_session_fee", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("set_session_fee", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def signup(self, op_id: str, reader_id: str, session_id: str) -> dict[str, Any]:
        """读者签认某采访场次。只可在自己签过的场次出席。"""
        payload = {"reader_id": reader_id, "session_id": session_id}
        fp = db.fingerprint("signup", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "signup", fp)
            if prior is not None:
                conn.commit()
                return prior
            if not conn.execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone():
                raise Rejection("unknown_session", session=session_id)
            if conn.execute(
                "SELECT 1 FROM session_signups WHERE reader_id=? AND session_id=?",
                (reader_id, session_id),
            ).fetchone():
                conn.execute("DELETE FROM session_signups "
                             "WHERE reader_id=? AND session_id=?",
                             (reader_id, session_id))
                state = "removed"
            else:
                conn.execute(
                    "INSERT INTO session_signups(reader_id, session_id) "
                    "VALUES(?, ?)",
                    (reader_id, session_id),
                )
                state = "added"
            result = {
                "status": "ok",
                "reader": reader_id,
                "session_id": session_id,
                "state": state,
            }
            db.record_operation(conn, op_id, "signup", fp)
            db.add_receipt(conn, "signup", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("signup", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    # ================================================================ 查询

    def get_readings(self, reader_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM readings WHERE retracted=0"
        params: tuple[Any, ...] = ()
        if reader_id is not None:
            sql += " AND reader_id=?"
            params = (reader_id,)
        sql += " ORDER BY reader_id, order_no, reading_id"
        return [
            {
                "reading_id": r["reading_id"],
                "hold_id": r["hold_id"],
                "ticket_id": r["ticket_id"],
                "reader_id": r["reader_id"],
                "copy_id": r["copy_id"],
                "original_bib": r["original_bib"],
                "order_no": r["order_no"],
                "citable": bool(r["citable"]),
                "pickup_revision": r["pickup_revision"],
            }
            for r in self.conn.execute(sql, params)
        ]

    def reader_trail(self, reader_id: str, revision: str) -> dict[str, Any]:
        """从一位读者的票据追查其真实相邻段与凭单（默认最新 epoch）。"""
        canon = self.catalog.canonical_map(revision)
        rows = self.conn.execute(
            "SELECT * FROM readings WHERE reader_id=? AND retracted=0 "
            "ORDER BY order_no, reading_id",
            (reader_id,),
        ).fetchall()
        segments = [
            {
                "canonical": s["canonical"],
                "from_reading": s["first"]["reading_id"],
                "to_reading": s["last"]["reading_id"],
                "from_book": s["first"]["original_bib"],
                "to_book": s["last"]["original_bib"],
                "citable": bool(s["first"]["citable"] and s["last"]["citable"]),
            }
            for s in self._segments(rows, canon)
        ]
        vouchers = [
            v
            for v in self.get_vouchers(revision)
            if v["reader"] == reader_id
        ]
        return {
            "reader": reader_id,
            "revision": revision,
            "segments": segments,
            "vouchers": vouchers,
        }

    def get_vouchers(
        self, revision: str | None = None, epoch: int | None = None
    ) -> list[dict[str, Any]]:
        """凭单查询。默认返回该修订的最新 epoch；指定 epoch/无参数可查旧版。"""
        sql = "SELECT * FROM vouchers"
        clauses: list[str] = []
        params: list[Any] = []
        if revision is not None:
            clauses.append("revision=?")
            params.append(revision)
        if epoch is None and revision is not None:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(epoch), 0) AS e FROM vouchers WHERE revision=?",
                (revision,),
            ).fetchone()
            epoch = row["e"]
        if epoch is not None:
            clauses.append("epoch=?")
            params.append(epoch)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY revision, epoch, voucher_id"
        return [
            {
                "voucher_id": r["voucher_id"],
                "revision": r["revision"],
                "epoch": r["epoch"],
                "reader": r["reader_id"],
                "from_canonical": r["from_canonical"],
                "to_canonical": r["to_canonical"],
                "from_reading": r["from_reading"],
                "to_reading": r["to_reading"],
                "from_book": r["from_book"],
                "to_book": r["to_book"],
                "from_hold": r["from_hold"],
                "to_hold": r["to_hold"],
                "from_copy": r["from_copy"],
                "to_copy": r["to_copy"],
            }
            for r in self.conn.execute(sql, params)
        ]

    def get_voucher(self, voucher_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM vouchers WHERE voucher_id=?", (voucher_id,)
        ).fetchone()
        if row is None:
            raise Rejection("unknown_voucher", voucher=voucher_id)
        return {
            "voucher_id": row["voucher_id"],
            "revision": row["revision"],
            "epoch": row["epoch"],
            "reader": row["reader_id"],
            "from_canonical": row["from_canonical"],
            "to_canonical": row["to_canonical"],
            "from_reading": row["from_reading"],
            "to_reading": row["to_reading"],
            "from_book": row["from_book"],
            "to_book": row["to_book"],
            "from_hold": row["from_hold"],
            "to_hold": row["to_hold"],
            "from_copy": row["from_copy"],
            "to_copy": row["to_copy"],
        }

    def latest_epoch(self, revision: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(epoch), 0) AS e FROM vouchers WHERE revision=?",
            (revision,),
        ).fetchone()
        return row["e"]

    def get_sessions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM sessions ORDER BY session_id"
        ).fetchall()
        out = []
        for r in rows:
            signups = [
                x["reader_id"]
                for x in self.conn.execute(
                    "SELECT reader_id FROM session_signups WHERE session_id=? "
                    "ORDER BY reader_id",
                    (r["session_id"],),
                )
            ]
            out.append(
                {
                    "session_id": r["session_id"],
                    "capacity": r["capacity"],
                    "fee": r["fee"],
                    "signups": signups,
                }
            )
        return out

    # ================================================================ 内部

    def _reject(self, kind: str, op_id: str, fp: str, rej: Rejection) -> dict[str, Any]:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            db.record_operation(conn, op_id, kind, fp)
            result = {"status": "rejected", "error": rej.as_dict()}
            db.add_receipt(conn, kind, "rejected", result, op_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result
