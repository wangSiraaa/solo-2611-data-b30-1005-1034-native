"""出版方逐作者、逐期的公共借阅结算。

事实来源只有权利室的 recognized_loans（真实认列借次）；馆报数字不参与任何计算。

单开（各版各自结算）：
- 按本版当期真实借次选费率：低于门槛每次普通费；达到门槛则**全期每次**改新费；
- 本版期初预付只抵本版当期应付，多余余额留到下期，预付不跨版挪借。

共池（已由相关版方同意的协议）：
- 每份协议列必须整组参加的版号，池有自己的门槛与两档单价；
- 入池各版披露本期真实借次并合计，共用该池费率；
- 按条款顺序用组内各版期初余额依次抵池总应付；
- 抵完仍欠的部分另付给作者；办理费另付且不可用预付抵；
- 选中的池彼此不能共用任何一版；无借次的版可带自己的预付入获准池，
  未入池、无豁免的版不能被擅自挪钱。

优化：每期 <=12 版、<=10 池，枚举全部互不重叠的池子集（<=1024 种），
其余版单开，取新增现金最低的确定性方案。过账幂等，重读过账回执不再吃一次余额；
下期开账只沿用前期真实所余预付。
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import db
from .errors import Rejection
from .rights import RightsService

MAX_EDITIONS = 12
MAX_POOLS = 10


# ---------------------------------------------------------------------------
# 方案数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditionLine:
    edition_code: str
    mode: str  # 'standalone' | 'pooled'
    pool_id: str | None
    real_count: int
    applied_rate: int
    gross_payable: int
    prepay_before: int
    prepay_deducted: int
    prepay_after: int
    author_cash: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "edition_code": self.edition_code,
            "mode": self.mode,
            "pool_id": self.pool_id,
            "real_count": self.real_count,
            "applied_rate": self.applied_rate,
            "gross_payable": self.gross_payable,
            "prepay_before": self.prepay_before,
            "prepay_deducted": self.prepay_deducted,
            "prepay_after": self.prepay_after,
            "author_cash": self.author_cash,
        }


@dataclass(frozen=True)
class PoolLine:
    pool_id: str
    members: tuple[str, ...]
    total_count: int
    applied_rate: int
    gross_payable: int
    handling_fee: int
    author_cash: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "members": list(self.members),
            "total_count": self.total_count,
            "applied_rate": self.applied_rate,
            "gross_payable": self.gross_payable,
            "handling_fee": self.handling_fee,
            "author_cash": self.author_cash,
        }


@dataclass(frozen=True)
class Plan:
    period_id: str
    selected_pools: tuple[str, ...]
    edition_lines: tuple[EditionLine, ...]
    pool_lines: tuple[PoolLine, ...]
    total_author_cash: int
    total_handling_fee: int
    total_new_cash: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "period_id": self.period_id,
            "selected_pools": list(self.selected_pools),
            "edition_lines": [e.to_dict() for e in self.edition_lines],
            "pool_lines": [p.to_dict() for p in self.pool_lines],
            "total_author_cash": self.total_author_cash,
            "total_handling_fee": self.total_handling_fee,
            "total_new_cash": self.total_new_cash,
        }


def _require_nonneg_int(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Rejection("bad_money_value", field=name, value=repr(value))
    return value


def _tier_rate(count: int, threshold: int, low_rate: int, high_rate: int) -> int:
    """低于门槛普通费；达到门槛（含）全期每次改新费。"""
    return high_rate if count >= threshold else low_rate


class RoyaltyService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ================================================================ 期间配置

    def open_period(
        self,
        period_id: str,
        author_id: str,
        start_day: str,
        end_day: str,
        editions: list[dict[str, Any]],
        pools: list[dict[str, Any]] | None = None,
        op_id: str = "",
        extra_prepay: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """开结算期。

        editions: [{edition_code, threshold, low_rate, high_rate}]，
                  期初预付默认由上一真实过账期的 prepay_after 结转（首期为 0），
                  extra_prepay 可显式追加注入（仅当该版没有任何前期过账时作期初值）。
        pools:    [{pool_id, threshold, low_rate, high_rate, handling_fee,
                    members: [edition_code, ...]（顺序即抵款先后）}]
        """
        if len(editions) > MAX_EDITIONS:
            raise Rejection("too_many_editions", limit=MAX_EDITIONS)
        if len(pools or []) > MAX_POOLS:
            raise Rejection("too_many_pools", limit=MAX_POOLS)
        for ed in editions:
            for key in ("threshold", "low_rate", "high_rate"):
                _require_nonneg_int(ed[key], f"{ed['edition_code']}.{key}")
            if ed["threshold"] <= 0:
                raise Rejection("bad_threshold", edition=ed["edition_code"])
        for pool in pools or []:
            for key in ("threshold", "low_rate", "high_rate", "handling_fee"):
                _require_nonneg_int(pool[key], f"{pool['pool_id']}.{key}")
            if pool["threshold"] <= 0:
                raise Rejection("bad_threshold", pool=pool["pool_id"])

        payload = {
            "period_id": period_id,
            "author_id": author_id,
            "start_day": start_day,
            "end_day": end_day,
            "editions": sorted(editions, key=lambda e: e["edition_code"]),
            "pools": sorted(pools or [], key=lambda p: p["pool_id"]),
        }
        fp = db.fingerprint("open_period", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            if op_id:
                prior = db.check_idempotent(conn, op_id, "open_period", fp)
                if prior is not None:
                    conn.commit()
                    return prior
            if conn.execute(
                "SELECT 1 FROM settle_periods WHERE period_id=?", (period_id,)
            ).fetchone():
                raise Rejection("period_exists", period=period_id)

            ts = db.now_iso()
            conn.execute(
                "INSERT INTO settle_periods(period_id, author_id, start_day, "
                "end_day, status, created_at) VALUES(?, ?, ?, ?, 'open', ?)",
                (period_id, author_id, start_day, end_day, ts),
            )

            # 期初 = 上一真实过账期所余（自动结转）+ 本期显式注入的新预付
            prior_balances = self._last_posted_balances(conn, author_id)
            extra_prepay = extra_prepay or {}
            opening_map: dict[str, int] = {}
            for ed in editions:
                code = ed["edition_code"]
                if not conn.execute(
                    "SELECT 1 FROM rights_editions re "
                    "JOIN rights_authors ra ON ra.author_id=re.author_id "
                    "WHERE re.edition_code=? AND re.author_id=?",
                    (code, author_id),
                ).fetchone():
                    raise Rejection(
                        "edition_not_owned_by_author",
                        edition=code,
                        author=author_id,
                    )
                injected = _require_nonneg_int(
                    extra_prepay.get(code, 0), f"extra_prepay.{code}"
                )
                opening = prior_balances.get(code, 0) + injected
                opening_map[code] = opening
                conn.execute(
                    "INSERT INTO edition_period_openings(period_id, edition_code, "
                    "opening_prepay) VALUES(?, ?, ?)",
                    (period_id, code, opening),
                )
                conn.execute(
                    "INSERT INTO edition_period_terms(period_id, edition_code, "
                    "threshold, low_rate, high_rate) VALUES(?, ?, ?, ?, ?)",
                    (
                        period_id,
                        code,
                        ed["threshold"],
                        ed["low_rate"],
                        ed["high_rate"],
                    ),
                )

            seen_pools: set[str] = set()
            for pool in pools or []:
                if pool["pool_id"] in seen_pools:
                    raise Rejection("duplicate_pool_id", pool=pool["pool_id"])
                seen_pools.add(pool["pool_id"])
                members = pool["members"]
                if not members:
                    raise Rejection("pool_without_members", pool=pool["pool_id"])
                if len(set(members)) != len(members):
                    raise Rejection("duplicate_pool_member", pool=pool["pool_id"])
                edition_codes = {e["edition_code"] for e in editions}
                unknown = [m for m in members if m not in edition_codes]
                if unknown:
                    raise Rejection(
                        "pool_member_not_in_period",
                        pool=pool["pool_id"],
                        unknown=unknown,
                    )
                conn.execute(
                    "INSERT INTO pool_agreements(pool_id, period_id, threshold, "
                    "low_rate, high_rate, handling_fee) VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        pool["pool_id"],
                        period_id,
                        pool["threshold"],
                        pool["low_rate"],
                        pool["high_rate"],
                        pool["handling_fee"],
                    ),
                )
                for order, code in enumerate(members):
                    conn.execute(
                        "INSERT INTO pool_members(pool_id, edition_code, "
                        "offset_order) VALUES(?, ?, ?)",
                        (pool["pool_id"], code, order),
                    )

            result = {
                "status": "ok",
                "period_id": period_id,
                "opening_prepay": opening_map,
            }
            if op_id:
                db.record_operation(conn, op_id, "open_period", fp)
                db.add_receipt(conn, "open_period", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            if not op_id:
                raise
            return self._reject("open_period", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    # ================================================================ 方案评估

    def evaluate(self, period_id: str) -> Plan:
        """枚举全部互不重叠的池子集，返回新增现金最低的确定性最优方案。"""
        cfg = self._load_period_config(period_id)
        plans = []
        pool_ids = list(cfg["pools"])
        for r in range(len(pool_ids) + 1):
            for combo in itertools.combinations(pool_ids, r):
                plan = self._build_plan(period_id, cfg, set(combo))
                plans.append(plan)
        return min(
            plans,
            key=lambda p: (
                p.total_new_cash,
                p.total_handling_fee,
                len(p.selected_pools),
                ",".join(p.selected_pools),
            ),
        )

    def evaluate_grouping(
        self, period_id: str, selected_pools: list[str]
    ) -> Plan:
        """按外部指定分组（选中的池）评估；池重叠/不存在直接拒绝。

        未列入任何池的版一律单开——没有共池豁免的版不能被擅自挪钱。
        """
        cfg = self._load_period_config(period_id)
        unknown = [p for p in selected_pools if p not in cfg["pools"]]
        if unknown:
            raise Rejection("unknown_pool", pools=unknown)
        used: set[str] = set()
        for pool_id in selected_pools:
            members = set(cfg["pools"][pool_id]["members"])
            overlap = sorted(used & members)
            if overlap:
                raise Rejection(
                    "pools_share_edition",
                    pools=selected_pools,
                    overlap=overlap,
                )
            used |= members
        return self._build_plan(period_id, cfg, set(selected_pools))

    def check_grouping(
        self, period_id: str, selected_pools: list[str]
    ) -> dict[str, Any]:
        """结算处核验外部指定分组：可行则给方案与现金，不可行给原因。"""
        try:
            plan = self.evaluate_grouping(period_id, selected_pools)
        except Rejection as rej:
            return {"feasible": False, "error": rej.as_dict()}
        best = self.evaluate(period_id)
        return {
            "feasible": True,
            "plan": plan.to_dict(),
            "new_cash": plan.total_new_cash,
            "optimal_new_cash": best.total_new_cash,
            "is_optimal": plan.total_new_cash == best.total_new_cash,
        }

    def evaluate_protecting(
        self, period_id: str, protected_editions: list[str], budget: int
    ) -> Plan | None:
        """在本期现金预算内保住指定版的期初预付供下期使用。

        受保版强制单开且预付一分不扣（应付全额现金），且不得加入任何池；
        返回满足预算的最低现金方案，预算内无解返回 None。
        """
        _require_nonneg_int(budget, "budget")
        cfg = self._load_period_config(period_id)
        protected = set(protected_editions)
        unknown = sorted(e for e in protected if e not in cfg["editions"])
        if unknown:
            raise Rejection("unknown_edition", editions=unknown)
        forbidden_pools = {
            pid
            for pid, pool in cfg["pools"].items()
            if protected & set(pool["members"])
        }
        candidates: list[Plan] = []
        allowed_pools = [p for p in cfg["pools"] if p not in forbidden_pools]
        for r in range(len(allowed_pools) + 1):
            for combo in itertools.combinations(allowed_pools, r):
                plan = self._build_plan(
                    period_id, cfg, set(combo), protected=protected
                )
                if plan.total_new_cash <= budget:
                    candidates.append(plan)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda p: (
                p.total_new_cash,
                p.total_handling_fee,
                len(p.selected_pools),
                ",".join(p.selected_pools),
            ),
        )

    # ================================================================ 过账

    def post(self, period_id: str, plan: Plan, op_id: str) -> dict[str, Any]:
        """只按选定方案真正过账；每期一次，重放同文不重复扣余额。"""
        payload = {
            "period_id": period_id,
            "plan_json": json.dumps(
                plan.to_dict(), sort_keys=True, ensure_ascii=False
            ),
        }
        fp = db.fingerprint("post_settlement", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 先查期间状态：已过账期的任何异 op_id/异方案请求都拒绝，
            # 只有同一 op_id 的同文重放才在下面原样返回、不再吃余额
            period = conn.execute(
                "SELECT * FROM settle_periods WHERE period_id=?", (period_id,)
            ).fetchone()
            if period is None:
                raise Rejection("unknown_period", period=period_id)
            if period["status"] == "posted":
                existing = conn.execute(
                    "SELECT * FROM posted_settlements WHERE period_id=?",
                    (period_id,),
                ).fetchone()
                prior_same_op = db.check_idempotent(
                    conn, op_id, "post_settlement", fp
                )
                if prior_same_op is not None:
                    conn.commit()
                    return prior_same_op
                raise Rejection(
                    "period_already_posted",
                    period=period_id,
                    posted_op=existing["op_id"],
                )
            prior = db.check_idempotent(conn, op_id, "post_settlement", fp)
            if prior is not None:
                conn.commit()
                return prior
            if plan.period_id != period_id:
                raise Rejection("plan_period_mismatch", plan=plan.period_id)

            # 过账前以库内真实余额与真实借次复核方案，防手工构造串改
            fresh = self.evaluate_grouping(period_id, list(plan.selected_pools))
            if fresh.to_dict() != plan.to_dict():
                raise Rejection("plan_not_reproducible", period=period_id)

            ts = db.now_iso()
            conn.execute(
                "INSERT INTO posted_settlements(period_id, op_id, "
                "total_author_cash, total_handling_fee, total_new_cash, "
                "plan_json, posted_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    period_id,
                    op_id,
                    plan.total_author_cash,
                    plan.total_handling_fee,
                    plan.total_new_cash,
                    json.dumps(plan.to_dict(), ensure_ascii=False),
                    ts,
                ),
            )
            for line in plan.edition_lines:
                conn.execute(
                    "INSERT INTO posted_edition_lines(period_id, edition_code, "
                    "mode, pool_id, real_count, applied_rate, gross_payable, "
                    "prepay_before, prepay_deducted, prepay_after, author_cash) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        period_id,
                        line.edition_code,
                        line.mode,
                        line.pool_id,
                        line.real_count,
                        line.applied_rate,
                        line.gross_payable,
                        line.prepay_before,
                        line.prepay_deducted,
                        line.prepay_after,
                        line.author_cash,
                    ),
                )
            for pool in plan.pool_lines:
                conn.execute(
                    "INSERT INTO posted_pool_lines(period_id, pool_id, "
                    "total_count, applied_rate, gross_payable, handling_fee, "
                    "author_cash) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        period_id,
                        pool.pool_id,
                        pool.total_count,
                        pool.applied_rate,
                        pool.gross_payable,
                        pool.handling_fee,
                        pool.author_cash,
                    ),
                )
            conn.execute(
                "UPDATE settle_periods SET status='posted' WHERE period_id=?",
                (period_id,),
            )
            result = {
                "status": "ok",
                "period_id": period_id,
                "op_id": op_id,
                "liquidation": self._liquidation_dict(plan),
            }
            db.record_operation(conn, op_id, "post_settlement", fp)
            db.add_receipt(conn, "post_settlement", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("post_settlement", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def get_posted(self, period_id: str) -> dict[str, Any]:
        """读取真实过账回执（重新读不会再吃一次余额）。"""
        row = self.conn.execute(
            "SELECT * FROM posted_settlements WHERE period_id=?", (period_id,)
        ).fetchone()
        if row is None:
            raise Rejection("period_not_posted", period=period_id)
        plan = json.loads(row["plan_json"])
        return {
            "period_id": period_id,
            "op_id": row["op_id"],
            "posted_at": row["posted_at"],
            "total_author_cash": row["total_author_cash"],
            "total_handling_fee": row["total_handling_fee"],
            "total_new_cash": row["total_new_cash"],
            "plan": plan,
            "liquidation": self._liquidation_from_rows(period_id),
        }

    def liquidation_statement(self, period_id: str) -> dict[str, Any]:
        """真实清算单：作者补款、办理费、各原版预付实扣/剩余分开列。"""
        self.get_posted(period_id)  # 未过账直接抛错
        return self._liquidation_from_rows(period_id)

    # ================================================================ 内部：配置与计算

    def _load_period_config(self, period_id: str) -> dict[str, Any]:
        period = self.conn.execute(
            "SELECT * FROM settle_periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if period is None:
            raise Rejection("unknown_period", period=period_id)
        editions: dict[str, dict[str, Any]] = {}
        for row in self.conn.execute(
            "SELECT t.edition_code, t.threshold, t.low_rate, t.high_rate, "
            "o.opening_prepay FROM edition_period_terms t "
            "JOIN edition_period_openings o "
            "ON o.period_id=t.period_id AND o.edition_code=t.edition_code "
            "WHERE t.period_id=?",
            (period_id,),
        ):
            editions[row["edition_code"]] = {
                "threshold": row["threshold"],
                "low_rate": row["low_rate"],
                "high_rate": row["high_rate"],
                "opening_prepay": row["opening_prepay"],
            }
        real = RightsService(self.conn).edition_real_counts(
            period["start_day"], period["end_day"]
        )
        for code in editions:
            editions[code]["real_count"] = real.get(code, 0)

        pools: dict[str, dict[str, Any]] = {}
        pool_rows = self.conn.execute(
            "SELECT * FROM pool_agreements WHERE period_id=?", (period_id,)
        ).fetchall()
        for prow in pool_rows:
            members = [
                r["edition_code"]
                for r in self.conn.execute(
                    "SELECT edition_code FROM pool_members WHERE pool_id=? "
                    "ORDER BY offset_order",
                    (prow["pool_id"],),
                )
            ]
            pools[prow["pool_id"]] = {
                "threshold": prow["threshold"],
                "low_rate": prow["low_rate"],
                "high_rate": prow["high_rate"],
                "handling_fee": prow["handling_fee"],
                "members": members,
            }
        return {"period": dict(period), "editions": editions, "pools": pools}

    def _build_plan(
        self,
        period_id: str,
        cfg: dict[str, Any],
        selected_pools: set[str],
        protected: set[str] | None = None,
    ) -> Plan:
        protected = protected or set()
        editions = cfg["editions"]
        pooled_editions: set[str] = set()
        for pid in selected_pools:
            pooled_editions.update(cfg["pools"][pid]["members"])

        edition_lines: list[EditionLine] = []
        pool_lines: list[PoolLine] = []
        total_author_cash = 0
        total_handling_fee = 0

        # ---- 入池各版：合计真实借次，共池费率，按序抵款
        for pid in sorted(selected_pools):
            pool = cfg["pools"][pid]
            members = tuple(pool["members"])
            total_count = sum(editions[m]["real_count"] for m in members)
            rate = _tier_rate(
                total_count,
                pool["threshold"],
                pool["low_rate"],
                pool["high_rate"],
            )
            gross = total_count * rate
            remaining = gross
            for code in members:
                before = editions[code]["opening_prepay"]
                deduct = min(before, remaining)
                remaining -= deduct
                edition_lines.append(
                    EditionLine(
                        edition_code=code,
                        mode="pooled",
                        pool_id=pid,
                        real_count=editions[code]["real_count"],
                        applied_rate=rate,
                        gross_payable=0,  # 应付在池行汇总，版行只记预付变动
                        prepay_before=before,
                        prepay_deducted=deduct,
                        prepay_after=before - deduct,
                        author_cash=0,
                    )
                )
            author_cash = remaining  # 抵完仍欠：另付作者
            pool_lines.append(
                PoolLine(
                    pool_id=pid,
                    members=members,
                    total_count=total_count,
                    applied_rate=rate,
                    gross_payable=gross,
                    handling_fee=pool["handling_fee"],
                    author_cash=author_cash,
                )
            )
            total_author_cash += author_cash
            total_handling_fee += pool["handling_fee"]

        # ---- 未入池各版单开；受保版预付一分不扣，应付全额现金
        for code in sorted(editions):
            if code in pooled_editions:
                continue
            ed = editions[code]
            rate = _tier_rate(
                ed["real_count"],
                ed["threshold"],
                ed["low_rate"],
                ed["high_rate"],
            )
            gross = ed["real_count"] * rate
            before = ed["opening_prepay"]
            if code in protected:
                deduct = 0
            else:
                deduct = min(before, gross)
            author_cash = gross - deduct
            edition_lines.append(
                EditionLine(
                    edition_code=code,
                    mode="standalone",
                    pool_id=None,
                    real_count=ed["real_count"],
                    applied_rate=rate,
                    gross_payable=gross,
                    prepay_before=before,
                    prepay_deducted=deduct,
                    prepay_after=before - deduct,
                    author_cash=author_cash,
                )
            )
            total_author_cash += author_cash

        edition_lines.sort(key=lambda e: e.edition_code)
        pool_lines.sort(key=lambda p: p.pool_id)
        return Plan(
            period_id=period_id,
            selected_pools=tuple(sorted(selected_pools)),
            edition_lines=tuple(edition_lines),
            pool_lines=tuple(pool_lines),
            total_author_cash=total_author_cash,
            total_handling_fee=total_handling_fee,
            total_new_cash=total_author_cash + total_handling_fee,
        )

    @staticmethod
    def _last_posted_balances(
        conn: sqlite3.Connection, author_id: str
    ) -> dict[str, int]:
        """该作者最近一个真实过账期各版所余预付——下期开账唯一允许的结转来源。"""
        period = conn.execute(
            "SELECT p.period_id FROM settle_periods p "
            "JOIN posted_settlements s ON s.period_id=p.period_id "
            "WHERE p.author_id=? ORDER BY p.created_at DESC, p.period_id DESC "
            "LIMIT 1",
            (author_id,),
        ).fetchone()
        if period is None:
            return {}
        return {
            r["edition_code"]: r["prepay_after"]
            for r in conn.execute(
                "SELECT edition_code, prepay_after FROM posted_edition_lines "
                "WHERE period_id=?",
                (period["period_id"],),
            )
        }

    def _liquidation_dict(self, plan: Plan) -> dict[str, Any]:
        return {
            "author_topup": [
                {
                    "edition_code": e.edition_code,
                    "mode": e.mode,
                    "pool_id": e.pool_id,
                    "cash": e.author_cash,
                }
                for e in plan.edition_lines
                if e.author_cash > 0
            ] + [
                {
                    "pool_id": p.pool_id,
                    "members": list(p.members),
                    "cash": p.author_cash,
                }
                for p in plan.pool_lines
                if p.author_cash > 0
            ],
            "handling_fees": [
                {"pool_id": p.pool_id, "fee": p.handling_fee}
                for p in plan.pool_lines
                if p.handling_fee > 0
            ],
            "prepay_movement": [
                {
                    "edition_code": e.edition_code,
                    "before": e.prepay_before,
                    "deducted": e.prepay_deducted,
                    "after": e.prepay_after,
                }
                for e in plan.edition_lines
            ],
            "total_new_cash": plan.total_new_cash,
        }

    def _liquidation_from_rows(self, period_id: str) -> dict[str, Any]:
        edition_rows = self.conn.execute(
            "SELECT * FROM posted_edition_lines WHERE period_id=? "
            "ORDER BY edition_code",
            (period_id,),
        ).fetchall()
        pool_rows = self.conn.execute(
            "SELECT * FROM posted_pool_lines WHERE period_id=? ORDER BY pool_id",
            (period_id,),
        ).fetchall()
        return {
            "author_topup": [
                {
                    "edition_code": r["edition_code"],
                    "mode": r["mode"],
                    "pool_id": r["pool_id"],
                    "cash": r["author_cash"],
                }
                for r in edition_rows
                if r["author_cash"] > 0
            ] + [
                {
                    "pool_id": r["pool_id"],
                    "cash": r["author_cash"],
                }
                for r in pool_rows
                if r["author_cash"] > 0
            ],
            "handling_fees": [
                {"pool_id": r["pool_id"], "fee": r["handling_fee"]}
                for r in pool_rows
                if r["handling_fee"] > 0
            ],
            "prepay_movement": [
                {
                    "edition_code": r["edition_code"],
                    "before": r["prepay_before"],
                    "deducted": r["prepay_deducted"],
                    "after": r["prepay_after"],
                }
                for r in edition_rows
            ],
        }

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
