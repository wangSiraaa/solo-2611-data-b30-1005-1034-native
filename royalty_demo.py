"""端到端演示：从真实保留/取件到版税结算的完整链路。

运行：python3.11 royalty_demo.py
内置断言覆盖题目三个数字场景、同日同读者同权段去重复核、外部指定分组核验、
预算内保住某版预付、过账幂等与次期开账沿用真实所余预付。退出码 0 即全部兑现。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from library import LibrarySystem

DB_PATH = str(Path(__file__).parent / "run_royalty.db")

# 两档费率条款（各版与共池一致）：低于 3 次每次 4，达到 3 次全期每次 6
TERMS = {"threshold": 3, "low_rate": 4, "high_rate": 6}
DAY1, DAY2 = "2026-11-02", "2026-11-10"


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def pickup(system: LibrarySystem, op_id: str, hold: str,
           reader: str, copy: str) -> dict:
    return system.ledger.pickup(op_id, hold, reader, copy)


def build_facts(system: LibrarySystem) -> None:
    """目录 + 票据 + 保留 + 权利室印次/权段 + 真实取件与认领。"""
    # ------------------------------------------------------------ 目录与副本
    system.catalog.submit_revision(
        "v1",
        ["WA", "WB", "WC"],
        [],
        [
            {"id": "C1", "book": "WA", "branch": "north", "format": "hardcover"},
            {"id": "C2", "book": "WA", "branch": "north", "format": "paperback"},
            {"id": "C3", "book": "WA", "branch": "north", "format": "paperback"},
            {"id": "C4", "book": "WA", "branch": "north", "format": "hardcover"},
        ],
        op_id="cat-v1",
    )
    system.catalog.activate_revision("v1")

    # 读者票据（到达序号决定保留先后），允许馆/装帧覆盖全部副本。
    # R1 两张票据：供同日取两册合订本；R3 的票据留给场景②的临时库副本。
    for ticket, reader, seq in (
        ("T1", "R1", 1),
        ("T2", "R1", 2),
        ("T3", "R3", 3),
    ):
        system.ledger.register_ticket(
            ticket, reader, "WA", seq,
            ["north"], ["hardcover", "paperback"], op_id=f"tk-{ticket}",
        )

    # ----------------------------- 权利室：具名作者、合同版、各副本印次清单
    system.rights.register_author("AU", "一位作者", op_id="author-AU")
    system.rights.register_edition("A", "AU", "甲书合同版 A", op_id="ed-A")
    system.rights.register_edition("B", "AU", "乙书合同版 B", op_id="ed-B")

    # 两册合订本 C1/C4 的印次各含两段版税权：A 的 s1 与 B 的 s1
    # （同一合同版 A、B 跨多册出现；实际取走的是某册某印次，不是规范作品名）
    for imp, copy in (("I1", "C1"), ("I4", "C4")):
        system.rights.register_impression(
            imp, copy, 1,
            [
                {"author_id": "AU", "edition_code": "A", "segment_code": "s1"},
                {"author_id": "AU", "edition_code": "B", "segment_code": "s1"},
            ],
            op_id=f"imp-{imp}",
        )
    # C3 的印次只含 B 的 s1（场景②中由另一读者真实借出，给 B 添 1 次）
    system.rights.register_impression(
        "I3", "C3", 1,
        [{"author_id": "AU", "edition_code": "B", "segment_code": "s1"}],
        op_id="imp-I3",
    )

    # 馆报数字：没有真实取件支撑，只是对账材料，结算绝不读取
    system.rights.register_library_report("G1", DAY1, "A", 99, op_id="report-G1")

    # ----------------------------------------------- 失败取件：错误读者被拒
    h_bad = system.ledger.reserve("rsv-C3", "v1", "C3")  # 给最早的 R1
    bad = pickup(system, "pk-bad", h_bad["hold_id"], "R9", "C3")
    assert bad["status"] == "rejected"
    assert bad["error"]["reason"] == "reader_mismatch"
    assert system.ledger.get_hold(h_bad["hold_id"])["status"] == "held"  # 未消耗

    # ----------------------------------------------- 过期保留不生借次
    system.ledger.release("rel-C3", h_bad["hold_id"], "expired")
    assert system.ledger.get_hold(h_bad["hold_id"])["status"] == "expired"

    # ------------------------ R1 真实取走第一册合订本 C1（基线借次）
    h1 = system.ledger.reserve("rsv-C1", "v1", "C1")  # T1 回队后最早
    assert h1["reader"] == "R1"
    assert pickup(system, "pk-C1", h1["hold_id"], "R1", "C1")["status"] == "ok"
    c1 = system.rights.claim_pickup("cl-C1", h1["hold_id"], DAY1)
    assert {x["loan_id"] for x in c1["recognized"]} == {"L1", "L2"}

    # 基线：A、B 各 1 个真实权段借次，分别由实际出借的副本产生
    assert system.rights.edition_real_counts(DAY1, DAY1) == {"A": 1, "B": 1}

    # ------------- 关键复核：同一读者同日取两个合订本却含同一权段，只计一次
    h4 = system.ledger.reserve("rsv-C4", "v1", "C4")  # R1 的 T2
    assert h4["reader"] == "R1"
    assert pickup(system, "pk-C4", h4["hold_id"], "R1", "C4")["status"] == "ok"
    c4 = system.rights.claim_pickup("cl-C4", h4["hold_id"], DAY1)
    # R1 当日已计过 A/s1、B/s1：两段全部去重，不生新借次
    assert all(not x["new"] for x in c4["recognized"])
    assert {x["loan_id"] for x in c4["recognized"]} == {"L1", "L2"}
    assert system.rights.edition_real_counts(DAY1, DAY1) == {"A": 1, "B": 1}

    # 同操作重放：返回同一回执，不新增认领/借次
    assert system.rights.claim_pickup("cl-C4", h4["hold_id"], DAY1) == c4

    # 对过期保留认领必须拒绝：过期没有真实取件，不能生出借次
    rejected_claim = system.rights.claim_pickup(
        "cl-expired", h_bad["hold_id"], DAY1
    )
    assert rejected_claim["status"] == "rejected"
    assert rejected_claim["error"]["reason"] == "hold_not_fulfilled"


def open_period(system: LibrarySystem, period_id: str,
                handling_fee: int | None, pool_id: str = "P-AB") -> None:
    pools = None
    if handling_fee is not None:
        pools = [
            {
                "pool_id": pool_id,
                **TERMS,
                "handling_fee": handling_fee,
                "members": ["A", "B"],  # 整组参加，抵款先后：先 A 后 B
            }
        ]
    system.royalties.open_period(
        period_id, "AU", DAY1, DAY1,
        editions=[
            {"edition_code": "A", **TERMS},
            {"edition_code": "B", **TERMS},
        ],
        pools=pools,
        extra_prepay={"A": 8, "B": 0},
        op_id=f"open-{period_id}",
    )


def check_numbers(system: LibrarySystem, period_id: str,
                  standalone_cash: int, pool_cash: int | None,
                  best_cash: int, pool_id: str = "P-AB") -> None:
    standalone = system.royalties.evaluate_grouping(period_id, [])
    best = system.royalties.evaluate(period_id)
    print(f"[{period_id}] 单开: 补款 {standalone.total_author_cash} "
          f"费 {standalone.total_handling_fee} 现金 {standalone.total_new_cash}")
    assert standalone.total_new_cash == standalone_cash
    if pool_cash is not None:
        pool = system.royalties.evaluate_grouping(period_id, [pool_id])
        print(f"[{period_id}] 并池: 补款 {pool.total_author_cash} "
              f"费 {pool.total_handling_fee} 现金 {pool.total_new_cash}")
        assert pool.total_new_cash == pool_cash
    print(f"[{period_id}] 枚举最优现金: {best.total_new_cash} "
          f"(选池 {best.selected_pools or '无，全部单开'})")
    assert best.total_new_cash == best_cash


def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(DB_PATH + suffix)
        if p.exists():
            p.unlink()

    system = LibrarySystem(DB_PATH)

    # ============================================ 一、事实层
    banner("一、真实保留与取件 → 权利室按印次权段认列借次（基线 A=1、B=1）")
    build_facts(system)
    print("基线真实借次:", system.rights.edition_real_counts(DAY1, DAY1))
    assert system.rights.edition_real_counts(DAY1, DAY1) == {"A": 1, "B": 1}

    # 权利室独立复核：R1 的 A/s1 一条已计借次对应两册合订本的两次真实取件
    audit = system.rights.audit_loan("L1")
    print("\n复核 L1（R1/A/s1）对应的真实取件:")
    for pk in audit["pickups"]:
        print(" ", pk)
    assert audit["duplicate_count"] == 1
    assert {p["hold_id"] for p in audit["pickups"]} == {"H2", "H3"}

    # 原书目后来合并不改写真正借出的版
    system.catalog.submit_revision(
        "v3",
        ["WA", "WB", "WC"],
        [{"from": "WB", "to": "WA"}],
        [
            {"id": "C1", "book": "WA", "branch": "north", "format": "hardcover"},
            {"id": "C2", "book": "WA", "branch": "north", "format": "paperback"},
            {"id": "C3", "book": "WA", "branch": "north", "format": "paperback"},
            {"id": "C4", "book": "WA", "branch": "north", "format": "hardcover"},
        ],
        op_id="cat-v3",
    )
    system.catalog.activate_revision("v3")
    assert system.rights.edition_real_counts(DAY1, DAY1) == {"A": 1, "B": 1}
    print("WB→WA 合并后真实借次不变:",
          system.rights.edition_real_counts(DAY1, DAY1))

    # ============================================ 二、场景①（题给数字）
    banner("场景①：A=1、B=1；预付 A=8、B=0；池费 1")
    open_period(system, "P1", handling_fee=1)
    # 单开：A 应付4 由预付抵（8→4），B 应付4 无预付 -> 现金补 4，A 余 4
    # 并池：合计2次*4=8，A 预付抵 8（A→0），补款0，仅费 1 -> 现金 1（最优）
    check_numbers(system, "P1", standalone_cash=4, pool_cash=1, best_cash=1)
    s_alone = system.royalties.evaluate_grouping("P1", [])
    by_ed = {e.edition_code: e for e in s_alone.edition_lines}
    assert by_ed["A"].prepay_deducted == 4 and by_ed["A"].prepay_after == 4
    assert by_ed["B"].author_cash == 4 and by_ed["B"].prepay_after == 0
    p_pool = system.royalties.evaluate_grouping("P1", ["P-AB"])
    pa = {e.edition_code: e for e in p_pool.edition_lines}
    assert pa["A"].prepay_deducted == 8 and pa["A"].prepay_after == 0
    assert p_pool.pool_lines[0].author_cash == 0

    # ============================================ 三、场景②（临时库副本）
    banner("场景②：另一读者再真实借出含 B 权段一册（B 添 1 次，池合计 3）")
    with tempfile.TemporaryDirectory() as tmp:
        copy_path = str(Path(tmp) / "whatif.db")
        system.close()
        for suffix in ("", "-wal", "-shm"):
            src = Path(DB_PATH + suffix)
            if src.exists():
                shutil.copy(src, copy_path + suffix)
        whatif = LibrarySystem(copy_path)

        # 副本里让过期回队的 C3 被另一读者 R3 真实取走（只含 B/s1）。
        # R3 的 T3（seq 3）仍是 waiting，且是当前唯一等待票据。
        h_extra = whatif.ledger.reserve("rsv-C3x", "v3", "C3")
        assert h_extra["reader"] == "R3"
        assert pickup(whatif, "pk-C3x", h_extra["hold_id"], "R3", "C3")[
            "status"] == "ok"
        claim_b = whatif.rights.claim_pickup(
            "cl-C3x", h_extra["hold_id"], DAY1
        )
        assert claim_b["status"] == "ok"
        counts = whatif.rights.edition_real_counts(DAY1, DAY1)
        print("添 1 次后真实借次:", counts)
        assert counts == {"A": 1, "B": 2}

        open_period(whatif, "P2W", handling_fee=1, pool_id="P-AB2")
        # 并池：合计3次达门槛，全期每次6 -> 18；A 预付抵8，补作者10，费1 -> 11
        # 单开：A=1 仍4（抵4），B=2 未达3 仍每次4 -> B 补8，现金8（最优）
        check_numbers(whatif, "P2W", standalone_cash=8, pool_cash=11,
                      best_cash=8, pool_id="P-AB2")
        w_pool = whatif.royalties.evaluate_grouping("P2W", ["P-AB2"])
        assert w_pool.pool_lines[0].gross_payable == 18
        assert w_pool.pool_lines[0].applied_rate == 6
        wp = {e.edition_code: e for e in w_pool.edition_lines}
        assert wp["A"].prepay_deducted == 8 and wp["A"].prepay_after == 0
        whatif.close()
        system = LibrarySystem(DB_PATH)  # 主库基线不受影响

    # ============================================ 四、场景③
    banner("场景③：恢复原借次（A=1、B=1），仅把办理费调成 6")
    open_period(system, "P3W", handling_fee=6, pool_id="P-AB3")
    # 单开现金 4；并池应付8 抵8 补0，但费6 -> 现金6；最省单开补4
    check_numbers(system, "P3W", standalone_cash=4, pool_cash=6,
                  best_cash=4, pool_id="P-AB3")

    # ============================================ 五、外部指定分组核验
    banner("六、结算处核验外部指定分组")
    check_empty = system.royalties.check_grouping("P1", [])
    check_pool = system.royalties.check_grouping("P1", ["P-AB"])
    check_bad = system.royalties.check_grouping("P1", ["P-GHOST"])
    print("全单开分组可行:", check_empty["feasible"],
          "现金", check_empty["new_cash"])
    print("并池分组可行:", check_pool["feasible"],
          "现金", check_pool["new_cash"], "最优?", check_pool["is_optimal"])
    print("未知池分组:", check_bad)
    assert check_empty["feasible"] and check_pool["feasible"]
    assert check_pool["new_cash"] == 1 and check_pool["is_optimal"]
    assert not check_bad["feasible"]
    assert check_bad["error"]["reason"] == "unknown_pool"

    # ============================================ 六、预算内保住 A 的预付
    banner("七、现金预算内选择保住 A 的预付供下期使用")
    plan_keep = system.royalties.evaluate_protecting("P1", ["A"], budget=8)
    assert plan_keep is not None
    a_line = next(e for e in plan_keep.edition_lines if e.edition_code == "A")
    assert a_line.prepay_deducted == 0 and a_line.prepay_after == 8
    # 受保后 A 应付4 也要现付，B 无预付补4：共需 8 现金
    assert plan_keep.total_new_cash == 8
    print("预算 8 内保 A：A 预付 8 全额保留，方案现金",
          plan_keep.total_new_cash)
    no_way = system.royalties.evaluate_protecting("P1", ["A"], budget=7)
    assert no_way is None
    print("预算 7 内保 A：无解（受保后 A 应付4 现付，B 补4，共需 8 现金）")

    # ============================================ 七、按最优方案真正过账
    banner("八、只按选定方案过账（P1 最优=并池，现金 1），重放不再吃余额")
    best = system.royalties.evaluate("P1")
    posted = system.royalties.post("P1", best, op_id="post-P1")
    assert posted["status"] == "ok"
    statement = system.royalties.liquidation_statement("P1")
    print("真实清算单:")
    print("  作者补款:", statement["author_topup"])
    print("  办理费  :", statement["handling_fees"])
    print("  预付变动:")
    for mv in statement["prepay_movement"]:
        print("   ", mv)
    assert statement["author_topup"] == []  # 应付8 全被 A 预付抵掉
    assert statement["handling_fees"] == [{"pool_id": "P-AB", "fee": 1}]
    prep = {m["edition_code"]: m for m in statement["prepay_movement"]}
    assert prep["A"] == {"edition_code": "A", "before": 8,
                         "deducted": 8, "after": 0}
    assert prep["B"] == {"edition_code": "B", "before": 0,
                         "deducted": 0, "after": 0}

    # 重新读本期回执/同文重放：不再吃一次余额
    assert system.royalties.post("P1", best, op_id="post-P1") == posted
    reread = system.royalties.get_posted("P1")
    assert reread["total_new_cash"] == 1
    a_after = next(
        m["after"] for m in reread["liquidation"]["prepay_movement"]
        if m["edition_code"] == "A"
    )
    print("同文重放过账 -> 同一回执，A 余额仍为", a_after)
    assert a_after == 0

    # 已过账期拒绝换方案二次过账（业务拒绝落 rejected 回执，不再扣余额）
    cheat = system.royalties.post(
        "P1", system.royalties.evaluate_grouping("P1", []),
        op_id="post-P1-cheat",
    )
    assert cheat["status"] == "rejected"
    assert cheat["error"]["reason"] == "period_already_posted"
    print("异方案二次过账 ->", cheat["error"]["reason"])
    assert system.royalties.get_posted("P1")["total_new_cash"] == 1

    # ============================================ 八、次期开账沿用真实所余
    banner("九、次期开账：沿用 P1 真实所余（A=0、B=0），B 另注入新预付 10")
    system.catalog.submit_revision(
        "v5",
        ["WA", "WB", "WC"],
        [{"from": "WB", "to": "WA"}],
        [
            {"id": "C1", "book": "WA", "branch": "north", "format": "hardcover"},
            {"id": "C2", "book": "WA", "branch": "north", "format": "paperback"},
            {"id": "C3", "book": "WA", "branch": "north", "format": "paperback"},
            {"id": "C4", "book": "WA", "branch": "north", "format": "hardcover"},
            {"id": "C6", "book": "WC", "branch": "north", "format": "paperback"},
        ],
        op_id="cat-v5",
    )
    system.catalog.activate_revision("v5")
    system.rights.register_impression(
        "I6", "C6", 1,
        [{"author_id": "AU", "edition_code": "B", "segment_code": "s1"}],
        op_id="imp-I6",
    )
    system.ledger.register_ticket(
        "T7", "R6", "WC", 7,
        ["north"], ["hardcover", "paperback"], op_id="tk-R6",
    )
    h7 = system.ledger.reserve("rsv-C6", "v5", "C6")
    assert pickup(system, "pk-C6", h7["hold_id"], "R6", "C6")["status"] == "ok"
    system.rights.claim_pickup("cl-C6", h7["hold_id"], DAY2)

    opened = system.royalties.open_period(
        "P2", "AU", DAY2, DAY2,
        editions=[
            {"edition_code": "A", **TERMS},
            {"edition_code": "B", **TERMS},
        ],
        pools=None,  # 次期无共池协议：没有豁免的版只能单开
        extra_prepay={"B": 10},
        op_id="open-P2",
    )
    print("P2 期初预付:", opened["opening_prepay"])
    assert opened["opening_prepay"] == {"A": 0, "B": 10}

    plan2 = system.royalties.evaluate("P2")
    by2 = {e.edition_code: e for e in plan2.edition_lines}
    assert by2["A"].real_count == 0 and by2["B"].real_count == 1
    assert by2["B"].gross_payable == 4
    assert by2["B"].prepay_deducted == 4 and by2["B"].prepay_after == 6
    assert plan2.total_new_cash == 0
    print("P2 单开：B 当期 1 次应付 4，由注入预付抵扣，现金 0，B 余",
          by2["B"].prepay_after)
    system.royalties.post("P2", plan2, op_id="post-P2")

    # 第三期开账：只沿用过账后的真实余额
    opened3 = system.royalties.open_period(
        "P3", "AU", DAY2, DAY2,
        editions=[
            {"edition_code": "A", **TERMS},
            {"edition_code": "B", **TERMS},
        ],
        op_id="open-P3",
    )
    assert opened3["opening_prepay"] == {"A": 0, "B": 6}
    print("P3 期初自动结转:", opened3["opening_prepay"])

    # 重开持久化库后数据原样
    system.close()
    reopened = LibrarySystem.reopen(DB_PATH)
    assert reopened.royalties.get_posted("P1")["total_new_cash"] == 1
    assert reopened.rights.edition_real_counts(DAY1, DAY2) == {"A": 1, "B": 2}
    print("重开库复核：P1 现金 1；累计真实借次 A=1、B=2（馆报 99 从未参与结算）")
    reopened.close()

    banner("全部断言通过")


if __name__ == "__main__":
    main()
