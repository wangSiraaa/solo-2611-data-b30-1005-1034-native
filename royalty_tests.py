"""权利室与版税结算的单元/集成测试。

运行：python3.11 -m unittest royalty_tests -v
覆盖：
- 只有 fulfilled 取件可认领；失败/过期/重放不生借次
- 同读者同日同作者同版同权段去重，不同权段/不同读者分别计
- 馆报数字不参与结算
- 单开门槛追溯、预付只抵本版且余额结转
- 共池整组、按序抵款、办理费不可抵、池间不得共用版、无借次版可携预付入池
- 外部分组核验、预算保版、过账幂等与次期结转、12 版/10 池上限
"""

from __future__ import annotations

import unittest

from library import LibrarySystem, Rejection
from library.royalties import MAX_EDITIONS, MAX_POOLS

TERMS = {"threshold": 3, "low_rate": 4, "high_rate": 6}
DAY = "2026-11-02"


class RoyaltyTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.s = LibrarySystem(":memory:")
        self.s.catalog.submit_revision(
            "v1", ["W"], [],
            [
                {"id": f"C{i}", "book": "W", "branch": "n",
                 "format": "paperback"}
                for i in range(1, 8)
            ],
            op_id="cat",
        )
        self.s.catalog.activate_revision("v1")
        self.s.rights.register_author("AU", "作者", op_id="au")
        for code in ("A", "B", "C"):
            self.s.rights.register_edition(
                code, "AU", f"版{code}", op_id=f"ed-{code}"
            )

    def tearDown(self) -> None:
        self.s.close()

    def _ticket(self, tid: str, reader: str, seq: int) -> None:
        self.s.ledger.register_ticket(
            tid, reader, "W", seq, ["n"], ["paperback"], op_id=f"tk-{tid}"
        )

    def _impression(self, iid: str, copy: str, editions: list[str]) -> None:
        self.s.rights.register_impression(
            iid, copy, 1,
            [
                {"author_id": "AU", "edition_code": e,
                 "segment_code": "s1"}
                for e in editions
            ],
            op_id=f"imp-{iid}",
        )

    def _real_pickup(self, op: str, copy: str, reader: str,
                     ticket: str, seq: int) -> str:
        self._ticket(ticket, reader, seq)
        hold = self.s.ledger.reserve(f"rsv-{op}", "v1", copy)
        self.assertEqual(hold["status"], "ok", hold)
        pk = self.s.ledger.pickup(
            f"pk-{op}", hold["hold_id"], reader, copy
        )
        self.assertEqual(pk["status"], "ok")
        return hold["hold_id"]


class FactLayerTest(RoyaltyTestBase):
    def test_only_fulfilled_pickup_recognizes(self) -> None:
        self._impression("I1", "C1", ["A"])
        self._ticket("T1", "R1", 1)
        hold = self.s.ledger.reserve("h", "v1", "C1")
        # 未取件的保留不能认领
        r = self.s.rights.claim_pickup("cl0", hold["hold_id"], DAY)
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "hold_not_fulfilled")

        # 错误读者取件失败，保留不被消耗，仍无借次
        bad = self.s.ledger.pickup("bad", hold["hold_id"], "RX", "C1")
        self.assertEqual(bad["status"], "rejected")
        self.assertEqual(
            self.s.ledger.get_hold(hold["hold_id"])["status"], "held"
        )
        # 过期释放后认领同样拒绝
        self.s.ledger.release("rel", hold["hold_id"], "expired")
        r2 = self.s.rights.claim_pickup("cl1", hold["hold_id"], DAY)
        self.assertEqual(r2["status"], "rejected")
        self.assertEqual(r2["error"]["reason"], "hold_not_fulfilled")
        self.assertEqual(self.s.rights.edition_real_counts(DAY, DAY), {})

    def test_same_reader_same_day_same_segment_counts_once(self) -> None:
        self._impression("I1", "C1", ["A", "B"])
        self._impression("I2", "C2", ["A", "B"])
        h1 = self._real_pickup("1", "C1", "R1", "T1", 1)
        h2 = self._real_pickup("2", "C2", "R1", "T2", 2)
        c1 = self.s.rights.claim_pickup("cl1", h1, DAY)
        c2 = self.s.rights.claim_pickup("cl2", h2, DAY)
        self.assertEqual(len(c1["recognized"]), 2)
        self.assertTrue(all(not x["new"] for x in c2["recognized"]))
        self.assertEqual(
            self.s.rights.edition_real_counts(DAY, DAY), {"A": 1, "B": 1}
        )
        # 不同权段分别计
        self.s.rights.register_impression(
            "I3", "C3", 1,
            [{"author_id": "AU", "edition_code": "A",
              "segment_code": "s2"}],
            op_id="imp-I3",
        )
        h3 = self._real_pickup("3", "C3", "R1", "T3", 3)
        c3 = self.s.rights.claim_pickup("cl3", h3, DAY)
        self.assertTrue(c3["recognized"][0]["new"])
        self.assertEqual(
            self.s.rights.edition_real_counts(DAY, DAY), {"A": 2, "B": 1}
        )

    def test_different_reader_same_segment_counts_separately(self) -> None:
        self._impression("I1", "C1", ["A"])
        self._impression("I2", "C2", ["A"])
        h1 = self._real_pickup("1", "C1", "R1", "T1", 1)
        h2 = self._real_pickup("2", "C2", "R2", "T2", 2)
        self.s.rights.claim_pickup("cl1", h1, DAY)
        self.s.rights.claim_pickup("cl2", h2, DAY)
        self.assertEqual(self.s.rights.edition_real_counts(DAY, DAY), {"A": 2})

    def test_replay_idempotent_and_conflict(self) -> None:
        self._impression("I1", "C1", ["A"])
        hold = self._real_pickup("1", "C1", "R1", "T1", 1)
        first = self.s.rights.claim_pickup("cl", hold, DAY)
        again = self.s.rights.claim_pickup("cl", hold, DAY)
        self.assertEqual(first, again)
        self.assertEqual(len(self.s.rights.list_recognized_loans()), 1)
        # 同一取件不能二次认领（不同 op_id）
        dup = self.s.rights.claim_pickup("cl2", hold, DAY)
        self.assertEqual(dup["status"], "rejected")
        self.assertEqual(dup["error"]["reason"], "hold_already_claimed")

    def test_audit_loan_lists_duplicate_pickups(self) -> None:
        self._impression("I1", "C1", ["A"])
        self._impression("I2", "C2", ["A"])
        h1 = self._real_pickup("1", "C1", "R1", "T1", 1)
        h2 = self._real_pickup("2", "C2", "R1", "T2", 2)
        self.s.rights.claim_pickup("cl1", h1, DAY)
        self.s.rights.claim_pickup("cl2", h2, DAY)
        audit = self.s.rights.audit_loan("L1")
        self.assertEqual(audit["duplicate_count"], 1)
        self.assertEqual(
            sorted(p["hold_id"] for p in audit["pickups"]),
            sorted([h1, h2]),
        )

    def test_library_report_never_pays(self) -> None:
        self._impression("I1", "C1", ["A"])
        hold = self._real_pickup("1", "C1", "R1", "T1", 1)
        self.s.rights.claim_pickup("cl", hold, DAY)
        self.s.rights.register_library_report(
            "G", DAY, "A", 999, op_id="rep"
        )
        self.s.royalties.open_period(
            "P", "AU", DAY, DAY,
            editions=[{"edition_code": "A", **TERMS}],
            op_id="open",
        )
        plan = self.s.royalties.evaluate("P")
        line = plan.edition_lines[0]
        self.assertEqual(line.real_count, 1)  # 不是 999
        self.assertEqual(line.gross_payable, 4)

    def test_catalog_merge_does_not_rewrite_loans(self) -> None:
        self._impression("I1", "C1", ["A", "B"])
        hold = self._real_pickup("1", "C1", "R1", "T1", 1)
        self.s.rights.claim_pickup("cl", hold, DAY)
        before = self.s.rights.edition_real_counts(DAY, DAY)
        self.s.catalog.submit_revision(
            "v2", ["W"], [],
            [{"id": "C1", "book": "W", "branch": "n",
              "format": "paperback"}],
            op_id="cat2",
        )
        self.s.catalog.activate_revision("v2")
        self.assertEqual(
            self.s.rights.edition_real_counts(DAY, DAY), before
        )


class StandaloneSettlementTest(RoyaltyTestBase):
    def _open(self, prepay: dict[str, int] | None = None) -> None:
        self.s.royalties.open_period(
            "P", "AU", DAY, DAY,
            editions=[
                {"edition_code": "A", **TERMS},
                {"edition_code": "B", **TERMS},
            ],
            extra_prepay=prepay or {},
            op_id="open",
        )

    def test_threshold_retroactive_rate(self) -> None:
        self._impression("I1", "C1", ["A"])
        for i in range(3):  # A 三次真实借次（不同读者，同日同段分别计）
            self.s.rights.register_impression(
                f"I{i+2}", f"C{i+2}", 1,
                [{"author_id": "AU", "edition_code": "A",
                  "segment_code": "s1"}],
                op_id=f"imp-I{i+2}",
            )
            h = self._real_pickup(
                f"a{i}", f"C{i+2}", f"R{i}", f"T{i}", i + 1
            )
            self.s.rights.claim_pickup(f"cl-a{i}", h, DAY)
        self._open()
        plan = self.s.royalties.evaluate("P")
        a = next(e for e in plan.edition_lines if e.edition_code == "A")
        self.assertEqual(a.real_count, 3)
        self.assertEqual(a.applied_rate, 6)  # 达门槛全期每次 6
        self.assertEqual(a.gross_payable, 18)

    def test_prepay_only_offsets_own_edition(self) -> None:
        # A、B 各 1 次；A 预付 8，B 无预付
        self._impression("I1", "C1", ["A"])
        self._impression("I2", "C2", ["B"])
        h1 = self._real_pickup("1", "C1", "R1", "T1", 1)
        h2 = self._real_pickup("2", "C2", "R2", "T2", 2)
        self.s.rights.claim_pickup("cl1", h1, DAY)
        self.s.rights.claim_pickup("cl2", h2, DAY)
        self._open(prepay={"A": 8, "B": 0})
        plan = self.s.royalties.evaluate_grouping("P", [])
        by = {e.edition_code: e for e in plan.edition_lines}
        self.assertEqual(by["A"].prepay_deducted, 4)
        self.assertEqual(by["A"].prepay_after, 4)
        self.assertEqual(by["A"].author_cash, 0)
        self.assertEqual(by["B"].author_cash, 4)  # A 的余额不能挪给 B
        self.assertEqual(by["B"].prepay_after, 0)
        self.assertEqual(plan.total_new_cash, 4)

    def test_zero_loan_edition_keeps_prepay(self) -> None:
        self._open(prepay={"A": 5, "B": 0})
        plan = self.s.royalties.evaluate("P")
        a = next(e for e in plan.edition_lines if e.edition_code == "A")
        self.assertEqual(a.real_count, 0)
        self.assertEqual(a.prepay_deducted, 0)
        self.assertEqual(a.prepay_after, 5)
        self.assertEqual(plan.total_new_cash, 0)


class PoolSettlementTest(RoyaltyTestBase):
    def _open_pool(
        self, fee: int = 1, prepay: dict[str, int] | None = None
    ) -> None:
        self.s.royalties.open_period(
            "P", "AU", DAY, DAY,
            editions=[
                {"edition_code": "A", **TERMS},
                {"edition_code": "B", **TERMS},
            ],
            pools=[
                {"pool_id": "POOL", **TERMS, "handling_fee": fee,
                 "members": ["A", "B"]}
            ],
            extra_prepay=prepay or {},
            op_id="open",
        )

    def test_pools_must_not_share_edition(self) -> None:
        self.s.royalties.open_period(
            "P2", "AU", DAY, DAY,
            editions=[
                {"edition_code": "A", **TERMS},
                {"edition_code": "B", **TERMS},
                {"edition_code": "C", **TERMS},
            ],
            pools=[
                {"pool_id": "P1", **TERMS, "handling_fee": 1,
                 "members": ["A", "B"]},
                {"pool_id": "P2", **TERMS, "handling_fee": 1,
                 "members": ["B", "C"]},
            ],
            op_id="open2",
        )
        with self.assertRaises(Rejection) as ctx:
            self.s.royalties.evaluate_grouping("P2", ["P1", "P2"])
        self.assertEqual(ctx.exception.reason, "pools_share_edition")

    def test_pool_offset_order_and_handling_fee(self) -> None:
        # A=1、B=2，池合计 3 -> 费率 6，应付 18；A 预付 8 先抵，费 1 另付
        self._impression("I1", "C1", ["A"])
        self._impression("I2", "C2", ["B"])
        h1 = self._real_pickup("1", "C1", "R1", "T1", 1)
        h2 = self._real_pickup("2", "C2", "R2", "T2", 2)
        self.s.rights.claim_pickup("cl1", h1, DAY)
        self.s.rights.claim_pickup("cl2", h2, DAY)
        self.s.rights.register_impression(
            "I3", "C3", 1,
            [{"author_id": "AU", "edition_code": "B",
              "segment_code": "s2"}],
            op_id="imp-I3",
        )
        h3 = self._real_pickup("3", "C3", "R3", "T3", 3)
        self.s.rights.claim_pickup("cl3", h3, DAY)
        self._open_pool(fee=1, prepay={"A": 8, "B": 0})
        plan = self.s.royalties.evaluate_grouping("P", ["POOL"])
        pool = plan.pool_lines[0]
        self.assertEqual((pool.total_count, pool.applied_rate), (3, 6))
        self.assertEqual(pool.gross_payable, 18)
        self.assertEqual(pool.author_cash, 10)   # 18-8
        self.assertEqual(pool.handling_fee, 1)   # 不可用预付抵
        self.assertEqual(plan.total_new_cash, 11)
        by = {e.edition_code: e for e in plan.edition_lines}
        self.assertEqual(by["A"].prepay_deducted, 8)
        self.assertEqual(by["A"].prepay_after, 0)
        self.assertEqual(by["B"].prepay_deducted, 0)

    def test_zero_loan_edition_brings_prepay_into_approved_pool(self) -> None:
        # B 1 次应付 4；A 无借次但带预付 8 入获准池，池应付 4 全由 A 预付抵
        self._impression("I2", "C2", ["B"])
        h = self._real_pickup("1", "C2", "R2", "T2", 2)
        self.s.rights.claim_pickup("cl", h, DAY)
        self._open_pool(fee=1, prepay={"A": 8, "B": 0})
        plan = self.s.royalties.evaluate_grouping("P", ["POOL"])
        self.assertEqual(plan.total_author_cash, 0)
        by = {e.edition_code: e for e in plan.edition_lines}
        self.assertEqual(by["A"].prepay_deducted, 4)
        self.assertEqual(by["A"].prepay_after, 4)
        self.assertEqual(plan.total_new_cash, 1)  # 只剩办理费

    def test_handling_fee_cannot_be_offset(self) -> None:
        self._open_pool(fee=6, prepay={"A": 8, "B": 0})  # 零借次
        plan = self.s.royalties.evaluate_grouping("P", ["POOL"])
        self.assertEqual(plan.total_author_cash, 0)
        self.assertEqual(plan.total_handling_fee, 6)
        self.assertEqual(plan.total_new_cash, 6)


class PostingAndRolloverTest(RoyaltyTestBase):
    def _one_loan_each(self) -> None:
        self.s.rights.register_impression(
            "I1", "C1", 1,
            [
                {"author_id": "AU", "edition_code": "A",
                 "segment_code": "s1"},
                {"author_id": "AU", "edition_code": "B",
                 "segment_code": "s1"},
            ],
            op_id="imp-I1",
        )
        hold = self._real_pickup("1", "C1", "R1", "T1", 1)
        self.s.rights.claim_pickup("cl", hold, DAY)

    def test_post_idempotent_and_rollover(self) -> None:
        self._one_loan_each()
        self.s.royalties.open_period(
            "P1", "AU", DAY, DAY,
            editions=[
                {"edition_code": "A", **TERMS},
                {"edition_code": "B", **TERMS},
            ],
            pools=[{"pool_id": "POOL1", **TERMS, "handling_fee": 1,
                    "members": ["A", "B"]}],
            extra_prepay={"A": 8},
            op_id="open1",
        )
        plan = self.s.royalties.evaluate("P1")
        posted = self.s.royalties.post("P1", plan, op_id="post1")
        self.assertEqual(posted["status"], "ok")
        # 同文重放不二次扣余额
        self.assertEqual(
            self.s.royalties.post("P1", plan, op_id="post1"), posted
        )
        stmt = self.s.royalties.liquidation_statement("P1")
        a = next(m for m in stmt["prepay_movement"] if m["edition_code"] == "A")
        self.assertEqual(a["after"], 0)

        # 次期开账沿用真实所余
        opened = self.s.royalties.open_period(
            "P2", "AU", DAY, DAY,
            editions=[
                {"edition_code": "A", **TERMS},
                {"edition_code": "B", **TERMS},
            ],
            op_id="open2",
        )
        self.assertEqual(opened["opening_prepay"], {"A": 0, "B": 0})

    def test_protect_edition_within_budget(self) -> None:
        self._one_loan_each()
        self.s.royalties.open_period(
            "P1", "AU", DAY, DAY,
            editions=[
                {"edition_code": "A", **TERMS},
                {"edition_code": "B", **TERMS},
            ],
            extra_prepay={"A": 8},
            op_id="open",
        )
        # 保 A：A 的 4 应付必须现付，B 的 4 也现付，共 8
        ok = self.s.royalties.evaluate_protecting("P1", ["A"], budget=8)
        self.assertIsNotNone(ok)
        a = next(e for e in ok.edition_lines if e.edition_code == "A")
        self.assertEqual((a.prepay_deducted, a.prepay_after), (0, 8))
        self.assertEqual(ok.total_new_cash, 8)
        self.assertIsNone(
            self.s.royalties.evaluate_protecting("P1", ["A"], budget=7)
        )

    def test_external_grouping_check(self) -> None:
        self._one_loan_each()
        self.s.royalties.open_period(
            "P1", "AU", DAY, DAY,
            editions=[
                {"edition_code": "A", **TERMS},
                {"edition_code": "B", **TERMS},
            ],
            pools=[{"pool_id": "POOL1", **TERMS, "handling_fee": 1,
                    "members": ["A", "B"]}],
            extra_prepay={"A": 8},
            op_id="open",
        )
        bad = self.s.royalties.check_grouping("P1", ["NOPE"])
        self.assertFalse(bad["feasible"])
        good = self.s.royalties.check_grouping("P1", ["POOL1"])
        self.assertTrue(good["feasible"])
        self.assertEqual(good["new_cash"], 1)


class LimitsTest(RoyaltyTestBase):
    def test_edition_and_pool_limits(self) -> None:
        editions = [
            {"edition_code": f"E{i}", **TERMS}
            for i in range(MAX_EDITIONS + 1)
        ]
        for i in range(MAX_EDITIONS + 1):
            self.s.rights.register_edition(
                f"E{i}", "AU", f"e{i}", op_id=f"ed-E{i}"
            )
        with self.assertRaises(Rejection) as ctx:
            self.s.royalties.open_period(
                "P", "AU", DAY, DAY, editions=editions, op_id="open"
            )
        self.assertEqual(ctx.exception.reason, "too_many_editions")

    def test_enumeration_feasible_at_max_pools(self) -> None:
        # 10 个互不重叠的池（每池 1 版），枚举 2^10 种组合并选最优
        codes = [f"E{i}" for i in range(MAX_POOLS)]
        for code in codes:
            self.s.rights.register_edition(
                code, "AU", code, op_id=f"ed-{code}"
            )
        pools = [
            {"pool_id": f"P{i}", **TERMS, "handling_fee": 1,
             "members": [codes[i]]}
            for i in range(MAX_POOLS)
        ]
        self.s.royalties.open_period(
            "P", "AU", DAY, DAY,
            editions=[{"edition_code": c, **TERMS} for c in codes],
            pools=pools,
            op_id="open",
        )
        plan = self.s.royalties.evaluate("P")
        # 零借次：单开现金 0，选池反而各费 1，最优是不选任何池
        self.assertEqual(plan.selected_pools, ())
        self.assertEqual(plan.total_new_cash, 0)

    def test_money_must_be_nonneg_integer(self) -> None:
        with self.assertRaises(Rejection):
            self.s.royalties.open_period(
                "P", "AU", DAY, DAY,
                editions=[{"edition_code": "A", "threshold": 3,
                           "low_rate": -1, "high_rate": 6}],
                op_id="open",
            )
        with self.assertRaises(Rejection):
            self.s.royalties.evaluate_protecting("P", [], budget=1.5)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
