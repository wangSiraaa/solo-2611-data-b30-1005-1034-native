"""保存员：逐册受光履历与光照状态重建。

边界（与目录、台账的分工）：
- 履历按实物 copy_id 建账；作品别名合并不合并任何一册的实物受光历史。
- 光照只从“真实领用”进入：必须存在 fulfilled 的取件；错误读者的拒绝回执、
  从未取件的保留、同一借阅的重复附件都不能多记展读曝光。
- 旧借阅没有附加光照记录时，按 light_profiles 里明示的馆藏初始状态接入；
- 有实际未还（fulfilled 未归还）或有效保留（held）的副本不能擅入展，
  这条由策展服务在挑册时向台账查询，保存员只负责如实出账。

按日整数记账规则：
- 当日有光照 d>0：P 与 R 都加 d；全天在暗处：P 不动，R 按该册暗藏恢复量
  下降到最低 0。每日日终 P、R 分别不能超各自上限（超上限的签认读数拒绝）。
- 同册每日只有一份合计曝光（部分唯一索引）；未签读数 pending 保留待核，
  不占名额、不进状态；旧记录勘误另开新行并把原版置为 superseded，原版不删除。
"""

from __future__ import annotations

from typing import Any

from . import db
from .errors import Rejection


class ConservationService:
    def __init__(self, conn, catalog, ledger) -> None:
        self.conn = conn
        self.catalog = catalog
        self.ledger = ledger

    # ================================================================ 建档

    def register_profile(
        self,
        copy_id: str,
        p_cap: int,
        r_cap: int,
        dark_recovery: int,
        initial_p: int = 0,
        initial_r: int = 0,
        anchor_day: int = 0,
        op_id: str | None = None,
    ) -> dict[str, Any]:
        """为一册建立光照账，并声明馆藏初始状态（旧借阅无附加记录时的接入点）。"""
        payload = {
            "copy_id": copy_id,
            "p_cap": p_cap,
            "r_cap": r_cap,
            "dark_recovery": dark_recovery,
            "initial_p": initial_p,
            "initial_r": initial_r,
            "anchor_day": anchor_day,
        }
        conn = self.conn
        if op_id is None:
            # 种子路径（材料初态）：无幂等包装，校验失败直接抛
            for name, val in (
                ("p_cap", p_cap), ("r_cap", r_cap),
                ("dark_recovery", dark_recovery),
                ("initial_p", initial_p), ("initial_r", initial_r),
            ):
                if not isinstance(val, int) or val < 0:
                    raise Rejection("bad_light_param", param=name, value=val)
            if initial_p > p_cap or initial_r > r_cap:
                raise Rejection("initial_exceeds_cap", **payload)
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute(
                    "SELECT 1 FROM light_profiles WHERE copy_id=?", (copy_id,)
                ).fetchone():
                    raise Rejection("profile_exists", copy=copy_id)
                conn.execute(
                    "INSERT INTO light_profiles(copy_id, p_cap, r_cap, "
                    "dark_recovery, initial_p, initial_r, anchor_day, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (copy_id, p_cap, r_cap, dark_recovery, initial_p,
                     initial_r, anchor_day, db.now_iso()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return {"status": "ok", "seeded": True, **payload}

        fp = db.fingerprint("register_profile", payload)
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "register_profile", fp)
            if prior is not None:
                conn.commit()
                return prior
            for name, val in (
                ("p_cap", p_cap), ("r_cap", r_cap),
                ("dark_recovery", dark_recovery),
                ("initial_p", initial_p), ("initial_r", initial_r),
            ):
                if not isinstance(val, int) or val < 0:
                    raise Rejection("bad_light_param", param=name, value=val)
            if initial_p > p_cap or initial_r > r_cap:
                raise Rejection("initial_exceeds_cap", **payload)
            if conn.execute(
                "SELECT 1 FROM light_profiles WHERE copy_id=?", (copy_id,)
            ).fetchone():
                raise Rejection("profile_exists", copy=copy_id)
            conn.execute(
                "INSERT INTO light_profiles(copy_id, p_cap, r_cap, "
                "dark_recovery, initial_p, initial_r, anchor_day, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (copy_id, p_cap, r_cap, dark_recovery, initial_p,
                 initial_r, anchor_day, db.now_iso()),
            )
            result = {"status": "ok", **payload}
            db.record_operation(conn, op_id, "register_profile", fp)
            db.add_receipt(conn, "register_profile", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("register_profile", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def get_profile(self, copy_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM light_profiles WHERE copy_id=?", (copy_id,)
        ).fetchone()
        if row is None:
            raise Rejection("unknown_profile", copy=copy_id)
        return dict(row)

    # ================================================================ 归还

    def return_loan(self, op_id: str, hold_id: str) -> dict[str, Any]:
        """闭展前的馆内归还：只认确实成功取走（fulfilled）的那一笔。

        归还后 hold 置 returned，副本解除占用、可被策展挑选；票据保持 fulfilled。
        错误读者的拒绝回执从未产生 fulfilled 保留，这里同样无笔可还。
        """
        payload = {"hold_id": hold_id}
        fp = db.fingerprint("return_loan", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "return_loan", fp)
            if prior is not None:
                conn.commit()
                return prior
            hold = conn.execute(
                "SELECT * FROM reservations WHERE hold_id=?", (hold_id,)
            ).fetchone()
            if hold is None:
                raise Rejection("unknown_hold", hold=hold_id)
            if hold["status"] != "fulfilled":
                # held（未取的保留）/ expired / cancelled / 已归还 都不能再走归还
                raise Rejection(
                    "hold_not_fulfilled", hold=hold_id, status=hold["status"]
                )
            ts = db.now_iso()
            conn.execute(
                "UPDATE reservations SET status='returned', returned_at=? "
                "WHERE hold_id=?",
                (ts, hold_id),
            )
            result = {
                "status": "ok",
                "hold_id": hold_id,
                "copy": hold["copy_id"],
                "ticket": hold["ticket_id"],
                "returned_at": ts,
            }
            db.record_operation(conn, op_id, "return_loan", fp)
            db.add_receipt(conn, "return_loan", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("return_loan", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    # ================================================================ 日光附件

    def attach_daylight(
        self,
        op_id: str,
        hold_id: str,
        day: int,
        exposure: int,
        signed: bool,
    ) -> dict[str, Any]:
        """把一次真实取件到归还期间的签认日光记录挂到该笔借阅上。

        - hold 必须 fulfilled/returned（确有成功取件）；held/expired/cancelled
          与不存在的 hold 一律拒绝，拒绝回执不产生任何曝光。
        - 同册每日只允许一份生效读数：重复附件（无论来自同笔还是另一笔真实借阅）
          都拒绝，不重复记账。
        - signed=False 时读数 pending 保留待核，不进状态、不做上限校验。
        - signed=True 时按日序重放校验双上限，超限拒绝。
        """
        payload = {
            "hold_id": hold_id, "day": day, "exposure": exposure, "signed": signed,
        }
        fp = db.fingerprint("attach_daylight", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "attach_daylight", fp)
            if prior is not None:
                conn.commit()
                return prior
            hold = conn.execute(
                "SELECT * FROM reservations WHERE hold_id=?", (hold_id,)
            ).fetchone()
            if hold is None:
                raise Rejection("unknown_hold", hold=hold_id)
            if hold["status"] not in ("fulfilled", "returned"):
                # 未取的保留、过期/取消、错误读者被拒（根本没有 fulfilled 行）
                raise Rejection(
                    "no_real_pickup", hold=hold_id, status=hold["status"]
                )
            copy_id = hold["copy_id"]
            profile = self._profile_row(copy_id)
            if not isinstance(day, int) or day < profile["anchor_day"]:
                raise Rejection(
                    "day_before_anchor", day=day, anchor=profile["anchor_day"]
                )
            if not isinstance(exposure, int) or exposure <= 0:
                raise Rejection("bad_exposure", exposure=exposure)
            dup = conn.execute(
                "SELECT 1 FROM light_readings WHERE copy_id=? AND day=? "
                "AND state='signed'",
                (copy_id, day),
            ).fetchone()
            if dup:
                # 同册每日只允许一份已签认合计曝光：重复附件（无论同笔还是
                # 另一笔真实借阅）都拒绝，不重复记账；待核读数不占此名额
                raise Rejection("duplicate_daily_reading", copy=copy_id, day=day)
            state = "signed" if signed else "pending"
            if signed:
                # 连同本读数一起按日序重放，日终双限都必须守住
                self._assert_reading_fits(profile, day, exposure)
            cur = conn.execute(
                "INSERT INTO light_readings(copy_id, day, exposure, source, "
                "state, op_id, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (copy_id, day, exposure, f"loan:{hold_id}", state,
                 op_id, db.now_iso()),
            )
            result = {
                "status": "ok",
                "reading_id": cur.lastrowid,
                "hold_id": hold_id,
                "copy": copy_id,
                "day": day,
                "exposure": exposure,
                "state": state,
            }
            db.record_operation(conn, op_id, "attach_daylight", fp)
            db.add_receipt(conn, "attach_daylight", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("attach_daylight", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def verify_reading(self, op_id: str, reading_id: int) -> dict[str, Any]:
        """把待核读数签认入账；签认时补做双上限重放校验。"""
        payload = {"reading_id": reading_id}
        fp = db.fingerprint("verify_reading", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "verify_reading", fp)
            if prior is not None:
                conn.commit()
                return prior
            row = conn.execute(
                "SELECT * FROM light_readings WHERE reading_id=?", (reading_id,)
            ).fetchone()
            if row is None:
                raise Rejection("unknown_reading", reading=reading_id)
            if row["state"] != "pending":
                raise Rejection(
                    "reading_not_pending", reading=reading_id, state=row["state"]
                )
            dup = conn.execute(
                "SELECT 1 FROM light_readings WHERE copy_id=? AND day=? "
                "AND state='signed'",
                (row["copy_id"], row["day"]),
            ).fetchone()
            if dup:
                # 待核期间该日已另有签认合计曝光：待核保留，不能再入账
                raise Rejection(
                    "duplicate_daily_reading",
                    copy=row["copy_id"], day=row["day"],
                )
            profile = self._profile_row(row["copy_id"])
            self._assert_reading_fits(profile, row["day"], row["exposure"])
            conn.execute(
                "UPDATE light_readings SET state='signed' WHERE reading_id=?",
                (reading_id,),
            )
            result = {
                "status": "ok", "reading_id": reading_id,
                "copy": row["copy_id"], "day": row["day"],
                "exposure": row["exposure"], "state": "signed",
            }
            db.record_operation(conn, op_id, "verify_reading", fp)
            db.add_receipt(conn, "verify_reading", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("verify_reading", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def correct_reading(
        self, op_id: str, reading_id: int, new_exposure: int
    ) -> dict[str, Any]:
        """旧记录勘误：原版置 superseded 永久保留，另开一份签认新读数。"""
        payload = {"reading_id": reading_id, "new_exposure": new_exposure}
        fp = db.fingerprint("correct_reading", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "correct_reading", fp)
            if prior is not None:
                conn.commit()
                return prior
            old = conn.execute(
                "SELECT * FROM light_readings WHERE reading_id=?", (reading_id,)
            ).fetchone()
            if old is None:
                raise Rejection("unknown_reading", reading=reading_id)
            if old["state"] == "superseded":
                raise Rejection("reading_already_superseded", reading=reading_id)
            if not isinstance(new_exposure, int) or new_exposure <= 0:
                raise Rejection("bad_exposure", exposure=new_exposure)
            profile = self._profile_row(old["copy_id"])
            # 先让旧版退场，再以新值重放校验
            conn.execute(
                "UPDATE light_readings SET state='superseded' WHERE reading_id=?",
                (reading_id,),
            )
            try:
                self._assert_reading_fits(profile, old["day"], new_exposure)
            except Rejection:
                # 新值不合法：恢复原版生效状态，整笔勘误拒绝
                conn.execute(
                    "UPDATE light_readings SET state=? WHERE reading_id=?",
                    (old["state"], reading_id),
                )
                raise
            cur = conn.execute(
                "INSERT INTO light_readings(copy_id, day, exposure, source, "
                "state, op_id, supersedes_id, created_at)"
                " VALUES(?, ?, ?, ?, 'signed', ?, ?, ?)",
                (old["copy_id"], old["day"], new_exposure, old["source"],
                 op_id, reading_id, db.now_iso()),
            )
            result = {
                "status": "ok",
                "reading_id": cur.lastrowid,
                "supersedes": reading_id,
                "copy": old["copy_id"],
                "day": old["day"],
                "exposure": new_exposure,
                "original": {"exposure": old["exposure"], "state": old["state"]},
            }
            db.record_operation(conn, op_id, "correct_reading", fp)
            db.add_receipt(conn, "correct_reading", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("correct_reading", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    # ================================================================ 查询与重建

    def day_value(self, copy_id: str, day: int) -> dict[str, Any]:
        """独查某册某日的日终 P/R（含该日光照与暗处恢复后的结果）。"""
        profile = self._profile_row(copy_id)
        p, r = self._replay_until(profile, day)
        return {
            "copy": copy_id, "day": day,
            "p": p, "r": r,
            "p_cap": profile["p_cap"], "r_cap": profile["r_cap"],
            "p_headroom": profile["p_cap"] - p,
            "r_headroom": profile["r_cap"] - r,
        }

    def light_provenance(self, copy_id: str) -> list[dict[str, Any]]:
        """哪些光来自哪一册、哪一次真实领用（或哪一场展），按日列示。"""
        self._profile_row(copy_id)
        rows = self.conn.execute(
            "SELECT reading_id, day, exposure, source, state, supersedes_id "
            "FROM light_readings WHERE copy_id=? ORDER BY day, reading_id",
            (copy_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def daily_exposure(self, copy_id: str) -> list[dict[str, Any]]:
        """同册每日一份合计曝光：只列生效中的读数（pending 单独标注待核）。"""
        self._profile_row(copy_id)
        rows = self.conn.execute(
            "SELECT day, exposure, state, source FROM light_readings "
            "WHERE copy_id=? AND state IN ('signed','pending') "
            "ORDER BY day",
            (copy_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def pending_readings(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT reading_id, copy_id, day, exposure, source "
            "FROM light_readings WHERE state='pending' ORDER BY copy_id, day"
        ).fetchall()
        return [dict(r) for r in rows]

    def available_copies(self, revision: str | None = None) -> list[dict[str, Any]]:
        """可展副本：无有效保留（held）且无实际未还借阅（fulfilled）。

        returned 的历史借阅不挡展；别名只用于策展按规范作品挑册，
        每册的光照履历始终独立。
        """
        revision = revision or self.catalog.head_revision
        statuses = self.ledger.get_copy_statuses(revision)
        out = []
        for st in statuses:
            if not st["occupied"]:
                out.append({"copy_id": st["copy_id"], "original_bib": st["original_bib"]})
        return out

    # ================================================================ 重放引擎

    def replay_day(
        self, profile: dict[str, Any], p: int, r: int, day: int,
        light: int | None,
    ) -> tuple[int, int]:
        """单日状态推进，供本服务与策展服务共用。

        light>0：P、R 各加 light（上限由调用方/建档约束保证）；
        light 为 None 或 0：全天暗处，P 不动，R 减暗藏恢复量，最低 0。
        """
        if light:
            p += light
            r += light
            if p > profile["p_cap"] or r > profile["r_cap"]:
                raise Rejection(
                    "light_exceeds_cap",
                    copy=profile["copy_id"], day=day,
                    end_p=p, end_r=r,
                    p_cap=profile["p_cap"], r_cap=profile["r_cap"],
                )
        else:
            r = max(0, r - profile["dark_recovery"])
        return p, r

    def initial_state(self, profile: dict[str, Any]) -> tuple[int, int]:
        return profile["initial_p"], profile["initial_r"]

    def signed_readings(self, copy_id: str) -> list[dict[str, int]]:
        rows = self.conn.execute(
            "SELECT day, exposure FROM light_readings "
            "WHERE copy_id=? AND state='signed' ORDER BY day",
            (copy_id,),
        ).fetchall()
        return [{"day": r["day"], "exposure": r["exposure"]} for r in rows]

    def _replay_until(
        self, profile: dict[str, Any], until_day: int
    ) -> tuple[int, int]:
        """从锚点日的馆藏初始状态重放到 until_day 日终；闲置日照暗藏计。"""
        p, r = self.initial_state(profile)
        readings = {
            x["day"]: x["exposure"]
            for x in self.signed_readings(profile["copy_id"])
            if x["day"] <= until_day
        }
        for day in range(profile["anchor_day"], until_day + 1):
            p, r = self.replay_day(profile, p, r, day, readings.get(day))
        return p, r

    def _assert_reading_fits(
        self, profile: dict[str, Any], day: int, exposure: int
    ) -> None:
        p, r = self.initial_state(profile)
        readings = {
            x["day"]: x["exposure"]
            for x in self.signed_readings(profile["copy_id"])
        }
        readings[day] = exposure  # 含待插入/勘误后的新值
        last_day = max(readings)
        for d in range(profile["anchor_day"], last_day + 1):
            p, r = self.replay_day(profile, p, r, d, readings.get(d))

    def _profile_row(self, copy_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM light_profiles WHERE copy_id=?", (copy_id,)
        ).fetchone()
        if row is None:
            raise Rejection("unknown_profile", copy=copy_id)
        return dict(row)

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
