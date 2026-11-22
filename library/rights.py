"""权利室：副本印次/版税权段登记、取件认领与借次认列。

与目录、台账的边界：
- 实际取走的是“某一册的某一印次”，不是当前规范作品名。权利室只按副本与印次
  登记当时适用的内容清单（每段含具名作者、合同版号、权段号），不读目录别名。
- 借次只认台账 reservations 中 status='fulfilled' 的成功取件；失败、过期、
  同操作重放都不可能产生认领，因此不可能生出借次。
- 认领时逐段复制当时内容清单快照；后续原书目合并（目录修订）不回写真实借出的版。
- 去重口径：同一读者、同一业务日、同一作者、同一合同版的同一权段，只计一次；
  合订本内不同权段可分别计。被去重的重复取件在
  recognized_loan_claims 中仍可逐条复核。
- 馆报数字（library_reports）只是对账材料，表内没有真实取件支撑的数字绝不付款。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import db
from .errors import Rejection


class RightsService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ================================================================ 基础资料

    def register_author(
        self, author_id: str, name: str, op_id: str
    ) -> dict[str, Any]:
        payload = {"author_id": author_id, "name": name}
        fp = db.fingerprint("rights_author", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "rights_author", fp)
            if prior is not None:
                conn.commit()
                return prior
            conn.execute(
                "INSERT INTO rights_authors(author_id, name) VALUES(?, ?)",
                (author_id, name),
            )
            result = {"status": "ok", "author_id": author_id, "name": name}
            db.record_operation(conn, op_id, "rights_author", fp)
            db.add_receipt(conn, "rights_author", "ok", result, op_id)
            conn.commit()
            return result
        except sqlite3.IntegrityError:
            conn.rollback()
            raise Rejection("author_exists", author=author_id)
        except Exception:
            conn.rollback()
            raise

    def register_edition(
        self, edition_code: str, author_id: str, title: str, op_id: str
    ) -> dict[str, Any]:
        payload = {
            "edition_code": edition_code,
            "author_id": author_id,
            "title": title,
        }
        fp = db.fingerprint("rights_edition", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "rights_edition", fp)
            if prior is not None:
                conn.commit()
                return prior
            if not conn.execute(
                "SELECT 1 FROM rights_authors WHERE author_id=?", (author_id,)
            ).fetchone():
                raise Rejection("unknown_author", author=author_id)
            conn.execute(
                "INSERT INTO rights_editions(edition_code, author_id, title) "
                "VALUES(?, ?, ?)",
                (edition_code, author_id, title),
            )
            result = {
                "status": "ok",
                "edition_code": edition_code,
                "author_id": author_id,
                "title": title,
            }
            db.record_operation(conn, op_id, "rights_edition", fp)
            db.add_receipt(conn, "rights_edition", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("rights_edition", op_id, fp, rej)
        except sqlite3.IntegrityError:
            conn.rollback()
            raise Rejection("edition_exists", edition=edition_code)
        except Exception:
            conn.rollback()
            raise

    def register_impression(
        self,
        impression_id: str,
        copy_id: str,
        impression_seq: int,
        segments: list[dict[str, str]],
        op_id: str,
    ) -> dict[str, Any]:
        """登记某副本某印次当时适用的内容清单。

        segments: [{author_id, edition_code, segment_code, segment_no?}, ...]
        同一合同版可在多册的印次中出现；权段号缺省按给定顺序从 1 起。
        印次一经写入不可变（同 ID 重放只返回原回执，异文冲突）。
        """
        norm = []
        for i, seg in enumerate(segments, start=1):
            norm.append(
                {
                    "segment_no": int(seg.get("segment_no", i)),
                    "author_id": seg["author_id"],
                    "edition_code": seg["edition_code"],
                    "segment_code": seg["segment_code"],
                }
            )
        payload = {
            "impression_id": impression_id,
            "copy_id": copy_id,
            "impression_seq": impression_seq,
            "segments": sorted(norm, key=lambda s: s["segment_no"]),
        }
        fp = db.fingerprint("rights_impression", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "rights_impression", fp)
            if prior is not None:
                conn.commit()
                return prior
            if not norm:
                raise Rejection("empty_impression_segments", impression=impression_id)
            for seg in norm:
                row = conn.execute(
                    "SELECT author_id FROM rights_editions WHERE edition_code=?",
                    (seg["edition_code"],),
                ).fetchone()
                if row is None:
                    raise Rejection(
                        "unknown_edition", edition=seg["edition_code"]
                    )
                if row["author_id"] != seg["author_id"]:
                    raise Rejection(
                        "edition_author_mismatch",
                        edition=seg["edition_code"],
                        expected=row["author_id"],
                        given=seg["author_id"],
                    )
            ts = db.now_iso()
            conn.execute(
                "INSERT INTO rights_impressions(impression_id, copy_id, "
                "impression_seq, created_at) VALUES(?, ?, ?, ?)",
                (impression_id, copy_id, impression_seq, ts),
            )
            conn.executemany(
                "INSERT INTO rights_segments(impression_id, segment_no, "
                "author_id, edition_code, segment_code) VALUES(?, ?, ?, ?, ?)",
                [
                    (
                        impression_id,
                        seg["segment_no"],
                        seg["author_id"],
                        seg["edition_code"],
                        seg["segment_code"],
                    )
                    for seg in norm
                ],
            )
            result = {
                "status": "ok",
                "impression_id": impression_id,
                "copy_id": copy_id,
                "impression_seq": impression_seq,
                "segments": norm,
            }
            db.record_operation(conn, op_id, "rights_impression", fp)
            db.add_receipt(conn, "rights_impression", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("rights_impression", op_id, fp, rej)
        except sqlite3.IntegrityError:
            conn.rollback()
            raise Rejection(
                "impression_exists",
                impression=impression_id,
                copy=copy_id,
                seq=impression_seq,
            )
        except Exception:
            conn.rollback()
            raise

    def register_library_report(
        self,
        report_id: str,
        business_day: str,
        edition_code: str,
        reported_count: int,
        op_id: str,
    ) -> dict[str, Any]:
        """登记馆报数字。仅留痕对账，结算绝不读取此表。"""
        if reported_count < 0:
            raise Rejection("negative_report_count", count=reported_count)
        payload = {
            "report_id": report_id,
            "business_day": business_day,
            "edition_code": edition_code,
            "reported_count": reported_count,
        }
        fp = db.fingerprint("library_report", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "library_report", fp)
            if prior is not None:
                conn.commit()
                return prior
            conn.execute(
                "INSERT INTO library_reports(report_id, business_day, "
                "edition_code, reported_count) VALUES(?, ?, ?, ?)",
                (report_id, business_day, edition_code, reported_count),
            )
            result = {"status": "ok", **payload}
            db.record_operation(conn, op_id, "library_report", fp)
            db.add_receipt(conn, "library_report", "ok", result, op_id)
            conn.commit()
            return result
        except sqlite3.IntegrityError:
            conn.rollback()
            raise Rejection("report_exists", report=report_id)
        except Exception:
            conn.rollback()
            raise

    # ================================================================ 取件认领

    def claim_pickup(
        self,
        op_id: str,
        hold_id: str,
        business_day: str,
    ) -> dict[str, Any]:
        """以一次成功取件（fulfilled 保留）为凭据，按取件时锁定的原副本与印次
        认领借次。失败/过期/未兑现的保留一律拒绝；同文重放原样返回，不生新借次。
        """
        payload = {"hold_id": hold_id, "business_day": business_day}
        fp = db.fingerprint("claim_pickup", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "claim_pickup", fp)
            if prior is not None:
                conn.commit()
                return prior

            hold = conn.execute(
                "SELECT * FROM reservations WHERE hold_id=?", (hold_id,)
            ).fetchone()
            if hold is None:
                raise Rejection("unknown_hold", hold=hold_id)
            if hold["status"] != "fulfilled":
                # 失败、过期、仅保留未取件：没有真实取件，不能生出借次
                raise Rejection(
                    "hold_not_fulfilled",
                    hold=hold_id,
                    status=hold["status"],
                )
            if conn.execute(
                "SELECT 1 FROM loan_claims WHERE hold_id=?", (hold_id,)
            ).fetchone():
                raise Rejection("hold_already_claimed", hold=hold_id)

            copy_id = hold["copy_id"]
            impression = self._impression_for_copy(conn, copy_id)
            segments = conn.execute(
                "SELECT segment_id, author_id, edition_code, segment_code "
                "FROM rights_segments WHERE impression_id=? ORDER BY segment_no",
                (impression["impression_id"],),
            ).fetchall()
            if not segments:
                raise Rejection(
                    "impression_without_segments",
                    impression=impression["impression_id"],
                )

            claim_n = int(db.get_meta(conn, "claim_counter", "0")) + 1
            db.set_meta(conn, "claim_counter", str(claim_n))
            claim_id = f"CL{claim_n}"
            ts = db.now_iso()
            conn.execute(
                "INSERT INTO loan_claims(claim_id, op_id, hold_id, copy_id, "
                "reader_id, business_day, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    claim_id,
                    op_id,
                    hold_id,
                    copy_id,
                    hold["reader_id"],
                    business_day,
                    ts,
                ),
            )
            conn.executemany(
                "INSERT INTO claim_segments(claim_id, segment_id, author_id, "
                "edition_code, segment_code) VALUES(?, ?, ?, ?, ?)",
                [
                    (claim_id, s["segment_id"], s["author_id"],
                     s["edition_code"], s["segment_code"])
                    for s in segments
                ],
            )

            recognized: list[dict[str, Any]] = []
            loan_n = int(db.get_meta(conn, "loan_counter", "0"))
            for seg in segments:
                existing = conn.execute(
                    "SELECT loan_id FROM recognized_loans WHERE reader_id=? "
                    "AND business_day=? AND author_id=? AND edition_code=? "
                    "AND segment_code=?",
                    (
                        hold["reader_id"],
                        business_day,
                        seg["author_id"],
                        seg["edition_code"],
                        seg["segment_code"],
                    ),
                ).fetchone()
                if existing is None:
                    loan_n += 1
                    loan_id = f"L{loan_n}"
                    conn.execute(
                        "INSERT INTO recognized_loans(loan_id, author_id, "
                        "edition_code, segment_code, reader_id, business_day, "
                        "first_claim_id) VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (
                            loan_id,
                            seg["author_id"],
                            seg["edition_code"],
                            seg["segment_code"],
                            hold["reader_id"],
                            business_day,
                            claim_id,
                        ),
                    )
                    recognized.append(
                        {
                            "loan_id": loan_id,
                            "edition_code": seg["edition_code"],
                            "segment_code": seg["segment_code"],
                            "new": True,
                        }
                    )
                    loan_id_for_link = loan_id
                else:
                    recognized.append(
                        {
                            "loan_id": existing["loan_id"],
                            "edition_code": seg["edition_code"],
                            "segment_code": seg["segment_code"],
                            "new": False,
                        }
                    )
                    loan_id_for_link = existing["loan_id"]
                conn.execute(
                    "INSERT OR IGNORE INTO recognized_loan_claims(loan_id, claim_id) "
                    "VALUES(?, ?)",
                    (loan_id_for_link, claim_id),
                )
            db.set_meta(conn, "loan_counter", str(loan_n))

            result = {
                "status": "ok",
                "claim_id": claim_id,
                "hold_id": hold_id,
                "copy_id": copy_id,
                "reader_id": hold["reader_id"],
                "business_day": business_day,
                "impression_id": impression["impression_id"],
                "recognized": recognized,
            }
            db.record_operation(conn, op_id, "claim_pickup", fp)
            db.add_receipt(conn, "claim_pickup", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("claim_pickup", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    # ================================================================ 复核查询

    def edition_real_counts(
        self, start_day: str, end_day: str
    ) -> dict[str, int]:
        """窗口内各合同版的真实认列借次（只数 recognized_loans，不读馆报）。"""
        rows = self.conn.execute(
            "SELECT edition_code, COUNT(*) AS n FROM recognized_loans "
            "WHERE business_day BETWEEN ? AND ? GROUP BY edition_code",
            (start_day, end_day),
        ).fetchall()
        return {r["edition_code"]: r["n"] for r in rows}

    def audit_loan(self, loan_id: str) -> dict[str, Any]:
        """权利室独立复核：一条已计借次对应哪些真实取件（含被去重的重复件）。"""
        loan = self.conn.execute(
            "SELECT * FROM recognized_loans WHERE loan_id=?", (loan_id,)
        ).fetchone()
        if loan is None:
            raise Rejection("unknown_loan", loan=loan_id)
        claims = self.conn.execute(
            "SELECT lc.claim_id, lc.hold_id, lc.copy_id, lc.reader_id, "
            "lc.business_day, lc.created_at "
            "FROM recognized_loan_claims rlc "
            "JOIN loan_claims lc ON lc.claim_id = rlc.claim_id "
            "WHERE rlc.loan_id=? ORDER BY lc.created_at, lc.claim_id",
            (loan_id,),
        ).fetchall()
        return {
            "loan": dict(loan),
            "duplicate_count": len(claims) - 1,
            "pickups": [dict(c) for c in claims],
        }

    def list_recognized_loans(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM recognized_loans ORDER BY loan_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_claims(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM loan_claims ORDER BY claim_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_impression(self, impression_id: str) -> dict[str, Any]:
        imp = self.conn.execute(
            "SELECT * FROM rights_impressions WHERE impression_id=?",
            (impression_id,),
        ).fetchone()
        if imp is None:
            raise Rejection("unknown_impression", impression=impression_id)
        segs = self.conn.execute(
            "SELECT segment_no, author_id, edition_code, segment_code "
            "FROM rights_segments WHERE impression_id=? ORDER BY segment_no",
            (impression_id,),
        ).fetchall()
        return {"impression": dict(imp), "segments": [dict(s) for s in segs]}

    # ================================================================ 内部

    @staticmethod
    def _impression_for_copy(
        conn: sqlite3.Connection, copy_id: str
    ) -> sqlite3.Row:
        """取该副本当前登记的最新印次；未登记的副本不能认领。"""
        row = conn.execute(
            "SELECT * FROM rights_impressions WHERE copy_id=? "
            "ORDER BY impression_seq DESC, impression_id DESC LIMIT 1",
            (copy_id,),
        ).fetchone()
        if row is None:
            raise Rejection("copy_without_impression", copy=copy_id)
        return row

    def _reject(self, kind: str, op_id: str, fp: str, rej: Rejection) -> dict:
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
