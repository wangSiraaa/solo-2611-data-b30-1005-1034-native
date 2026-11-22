"""保存员（逐册受光履历）与策展人（最低费用轮展班表）测试。

上限按题面：最多 6 册、10 日。数值样例严格对题：
- 两部合格本 A/B，双限 3/3、暗复 2、两天最低均 1、换本费 2、遮光费 3；
- 只给 A 补一笔真实取件照 3 后归还 -> A 永久额度尽，只能用 B，一天 1 一天 2，费 3；
- 换本费提到 5 -> 固定同册两天、其中一天照 1，最低仍 3；
- 另用长期受光真归还例（A 的 P 快用尽、R 已暗藏恢复）核出必须换册的场次，
  并以换本费 2 / 5 重开两版班表。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from library import IdempotencyConflict, LibrarySystem
from library.errors import Rejection


def _seed_work(system: LibrarySystem, copies: list[str], bib: str = "W") -> None:
    cat_copies = [
        {"id": c, "book": bib, "branch": "north", "format": "hardcover"}
        for c in copies
    ]
    system.catalog.submit_revision(
        "v1", [bib], [], cat_copies, op_id="cat-v1"
    )


def _profile(system, cid, p_cap, r_cap, rec, ip=0, ir=0, op=None):
    return system.conservation.register_profile(
        cid, p_cap, r_cap, rec, ip, ir, op_id=op or f"prof-{cid}"
    )


def _payload(plan, entries):
    """从规划结果构造登记 payload，含开展日历日。"""
    return {
        "revision": plan["revision"],
        "canonical_bib": plan["canonical_bib"],
        "min_levels": plan["min_levels"],
        "swap_fee": plan["swap_fee"],
        "shade_fee": plan["shade_fee"],
        "start_day": plan["start_day"],
        "entries": entries,
    }


def _real_loan(system, tid, reader, copy, hold_op, pickup_op):
    """经原入口走完整真实领用：票据 -> 保留 -> 取件（返回 hold_id）。"""
    system.ledger.register_ticket(
        tid, reader, "W", int(tid[1:]), ["north"], ["hardcover"],
        op_id=f"tk-{tid}",
    )
    hold = system.ledger.reserve(hold_op, "v1", copy)
    system.ledger.pickup(pickup_op, hold["hold_id"], reader, copy)
    return hold["hold_id"]


class ConservationProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.system = LibrarySystem(":memory:")
        _seed_work(self.system, ["C1"])

    def tearDown(self) -> None:
        self.system.close()

    def test_profile_seed_and_duplicate(self) -> None:
        r = _profile(self.system, "C1", 3, 3, 2)
        self.assertEqual(r["status"], "ok")
        again = self.system.conservation.register_profile(
            "C1", 3, 3, 2, op_id="dup"
        )
        self.assertEqual(again["status"], "rejected")
        self.assertEqual(again["error"]["reason"], "profile_exists")

    def test_initial_state_cannot_exceed_cap(self) -> None:
        r = self.system.conservation.register_profile(
            "C1", 2, 2, 1, initial_p=3, op_id="bad"
        )
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "initial_exceeds_cap")
        # 失败不建档
        with self.assertRaises(Exception):
            self.system.conservation.get_profile("C1")

    def test_idempotent_same_payload_replays(self) -> None:
        r1 = self.system.conservation.register_profile(
            "C1", 3, 3, 2, op_id="op1"
        )
        r2 = self.system.conservation.register_profile(
            "C1", 3, 3, 2, op_id="op1"
        )
        self.assertEqual(r1, r2)

    def test_idempotent_diff_payload_conflicts(self) -> None:
        self.system.conservation.register_profile(
            "C1", 3, 3, 2, op_id="op1"
        )
        with self.assertRaises(IdempotencyConflict):
            self.system.conservation.register_profile(
                "C1", 4, 3, 2, op_id="op1"
            )


class DaylightAttributionTest(unittest.TestCase):
    """只有真实取件能产生曝光；拒绝回执、未取保留、重复附件都不记账。"""

    def setUp(self) -> None:
        self.system = LibrarySystem(":memory:")
        _seed_work(self.system, ["C1", "C2"])
        _profile(self.system, "C1", 5, 5, 2)
        _profile(self.system, "C2", 5, 5, 2)

    def tearDown(self) -> None:
        self.system.close()

    def test_held_reservation_cannot_carry_daylight(self) -> None:
        self.system.ledger.register_ticket(
            "T1", "R1", "W", 1, ["north"], ["hardcover"], op_id="tk1"
        )
        hold = self.system.ledger.reserve("h1", "v1", "C1")
        # 未取件的保留：挂日光拒绝，不产生任何读数
        r = self.system.conservation.attach_daylight(
            "a1", hold["hold_id"], 0, 2, True
        )
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "no_real_pickup")
        self.assertEqual(self.system.conservation.light_provenance("C1"), [])

    def test_wrong_reader_receipt_carries_no_light(self) -> None:
        hid = _real_loan(self.system, "T1", "R1", "C1", "h1", "p1")
        # 另一笔保留给 C2，错误读者取件被拒
        self.system.ledger.register_ticket(
            "T2", "R2", "W", 2, ["north"], ["hardcover"], op_id="tk2"
        )
        h2 = self.system.ledger.reserve("h2", "v1", "C2")
        bad = self.system.ledger.pickup("p2", h2["hold_id"], "R9", "C2")
        self.assertEqual(bad["status"], "rejected")
        # 被拒的取件没有 fulfilled 行，无笔可挂日光
        r = self.system.conservation.attach_daylight("a2", h2["hold_id"], 0, 1, True)
        self.assertEqual(r["error"]["reason"], "no_real_pickup")
        self.assertEqual(self.system.conservation.light_provenance("C2"), [])
        # 真实读者的那笔仍可正常挂账
        ok = self.system.conservation.attach_daylight("a3", hid, 0, 2, True)
        self.assertEqual(ok["status"], "ok")

    def test_rejected_pickup_keeps_hold_for_real_reader(self) -> None:
        self.system.ledger.register_ticket(
            "T1", "R1", "W", 1, ["north"], ["hardcover"], op_id="tk1"
        )
        hold = self.system.ledger.reserve("h1", "v1", "C1")
        bad = self.system.ledger.pickup("px", hold["hold_id"], "R9", "C1")
        self.assertEqual(bad["status"], "rejected")
        # 拒绝不消耗保留；此时挂日光同样被拒
        r = self.system.conservation.attach_daylight(
            "ax", hold["hold_id"], 0, 2, True
        )
        self.assertEqual(r["error"]["reason"], "no_real_pickup")
        self.assertEqual(
            self.system.ledger.get_hold(hold["hold_id"])["status"], "held"
        )
        # 真实读者成功取件后即可挂账
        self.system.ledger.pickup("p1", hold["hold_id"], "R1", "C1")
        ok = self.system.conservation.attach_daylight(
            "a1", hold["hold_id"], 0, 2, True
        )
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(self.system.conservation.day_value("C1", 0)["p"], 2)

    def test_multiple_pending_coexist_but_only_one_signed_per_day(self) -> None:
        hid = _real_loan(self.system, "T1", "R1", "C1", "h1", "p1")
        pn1 = self.system.conservation.attach_daylight(
            "w1", hid, 1, 1, signed=False
        )
        pn2 = self.system.conservation.attach_daylight(
            "w2", hid, 1, 2, signed=False
        )
        self.assertEqual(pn1["state"], "pending")
        self.assertEqual(pn2["state"], "pending")
        self.assertEqual(len(self.system.conservation.pending_readings()), 2)
        # 待核不占名额：同日仍可签认一份
        signed = self.system.conservation.attach_daylight(
            "sg", hid, 1, 1, signed=True
        )
        self.assertEqual(signed["state"], "signed")
        # 两份待核再签认都撞同日已签认，且原样保留
        for reading in (pn1["reading_id"], pn2["reading_id"]):
            v = self.system.conservation.verify_reading(f"v{reading}", reading)
            self.assertEqual(v["error"]["reason"], "duplicate_daily_reading")
        self.assertEqual(len(self.system.conservation.pending_readings()), 2)

    def test_duplicate_attachment_same_day_rejected(self) -> None:
        hid = _real_loan(self.system, "T1", "R1", "C1", "h1", "p1")
        first = self.system.conservation.attach_daylight("a1", hid, 3, 2, True)
        self.assertEqual(first["status"], "ok")
        # 同册同日重复附件（哪怕同一笔借阅、同文之外的新请求）
        dup = self.system.conservation.attach_daylight("a2", hid, 3, 2, True)
        self.assertEqual(dup["status"], "rejected")
        self.assertEqual(dup["error"]["reason"], "duplicate_daily_reading")
        # 同文重放原样返回，不新增读数
        replay = self.system.conservation.attach_daylight("a1", hid, 3, 2, True)
        self.assertEqual(replay, first)
        self.assertEqual(len(self.system.conservation.daily_exposure("C1")), 1)

    def test_second_real_loan_same_day_also_rejected(self) -> None:
        hid1 = _real_loan(self.system, "T1", "R1", "C1", "h1", "p1")
        self.system.conservation.attach_daylight("a1", hid1, 5, 1, True)
        # 归还后再借第二笔，同一天也不能再记一份
        self.system.conservation.return_loan("r1", hid1)
        self.system.ledger.register_ticket(
            "T2", "R2", "W", 2, ["north"], ["hardcover"], op_id="tk2"
        )
        h2 = self.system.ledger.reserve("h2", "v1", "C1")
        hid2 = self.system.ledger.pickup("p2", h2["hold_id"], "R2", "C1")["hold_id"]
        r = self.system.conservation.attach_daylight("a2", hid2, 5, 1, True)
        self.assertEqual(r["error"]["reason"], "duplicate_daily_reading")

    def test_signed_reading_exceeding_cap_rejected(self) -> None:
        hid = _real_loan(self.system, "T1", "R1", "C1", "h1", "p1")
        r = self.system.conservation.attach_daylight("a1", hid, 0, 6, True)
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "light_exceeds_cap")
        self.assertEqual(self.system.conservation.daily_exposure("C1"), [])

    def test_pending_reading_kept_unverified_and_not_counted(self) -> None:
        hid = _real_loan(self.system, "T1", "R1", "C1", "h1", "p1")
        pending = self.system.conservation.attach_daylight(
            "a1", hid, 0, 2, signed=False
        )
        self.assertEqual(pending["state"], "pending")
        # 待核不进状态：日 0 的 P/R 仍按初始 0（全天暗藏）
        self.assertEqual(
            (self.system.conservation.day_value("C1", 0)["p"],
             self.system.conservation.day_value("C1", 0)["r"]),
            (0, 0),
        )
        # 待核不占当日签认名额：同日可再签一份
        signed = self.system.conservation.attach_daylight(
            "a2", hid, 0, 1, signed=True
        )
        self.assertEqual(signed["state"], "signed")
        self.assertEqual(self.system.conservation.day_value("C1", 0)["p"], 1)
        # 待核读数签认时发现同日已有生效读数 -> 拒，待核原样保留
        verify = self.system.conservation.verify_reading("v1", pending["reading_id"])
        self.assertEqual(verify["status"], "rejected")
        self.assertEqual(verify["error"]["reason"], "duplicate_daily_reading")
        still = self.system.conservation.pending_readings()
        self.assertEqual([x["reading_id"] for x in still], [pending["reading_id"]])

    def test_verify_pending_then_enters_ledger(self) -> None:
        hid = _real_loan(self.system, "T1", "R1", "C1", "h1", "p1")
        pending = self.system.conservation.attach_daylight(
            "a1", hid, 0, 2, signed=False
        )
        verified = self.system.conservation.verify_reading(
            "v1", pending["reading_id"]
        )
        self.assertEqual(verified["state"], "signed")
        self.assertEqual(self.system.conservation.day_value("C1", 0)["p"], 2)

    def test_correction_keeps_original_version(self) -> None:
        hid = _real_loan(self.system, "T1", "R1", "C1", "h1", "p1")
        old = self.system.conservation.attach_daylight("a1", hid, 0, 2, True)
        corrected = self.system.conservation.correct_reading(
            "c1", old["reading_id"], 1
        )
        self.assertEqual(corrected["supersedes"], old["reading_id"])
        self.assertEqual(corrected["exposure"], 1)
        prov = {x["reading_id"]: x for x in
                self.system.conservation.light_provenance("C1")}
        self.assertEqual(prov[old["reading_id"]]["state"], "superseded")
        self.assertEqual(prov[corrected["reading_id"]]["state"], "signed")
        # 状态按新值重放
        self.assertEqual(self.system.conservation.day_value("C1", 0)["p"], 1)

    def test_correction_to_over_cap_rejected_and_original_restored(self) -> None:
        hid = _real_loan(self.system, "T1", "R1", "C1", "h1", "p1")
        old = self.system.conservation.attach_daylight("a1", hid, 0, 2, True)
        r = self.system.conservation.correct_reading("c1", old["reading_id"], 6)
        self.assertEqual(r["status"], "rejected")
        # 原版仍是生效的 signed
        prov = {x["reading_id"]: x for x in
                self.system.conservation.light_provenance("C1")}
        self.assertEqual(prov[old["reading_id"]]["state"], "signed")


class StateReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.system = LibrarySystem(":memory:")
        _seed_work(self.system, ["C1"])
        _profile(self.system, "C1", 5, 5, 2)

    def tearDown(self) -> None:
        self.system.close()

    def test_dark_days_recover_r_and_never_touch_p(self) -> None:
        # 无任何读数：连续暗藏，R 已在 0 则保持 0，P 永远 0
        for day in range(4):
            v = self.system.conservation.day_value("C1", day)
            self.assertEqual((v["p"], v["r"]), (0, 0))

    def test_light_then_dark_recovery_sequence(self) -> None:
        hid = _real_loan(self.system, "T1", "R1", "C1", "h1", "p1")
        self.system.conservation.attach_daylight("a1", hid, 0, 3, True)
        # 日0：P=R=3
        self.assertEqual(
            (self.system.conservation.day_value("C1", 0)["p"],
             self.system.conservation.day_value("C1", 0)["r"]), (3, 3)
        )
        # 日1 暗藏：P 不动 3，R 减 2 -> 1
        v1 = self.system.conservation.day_value("C1", 1)
        self.assertEqual((v1["p"], v1["r"]), (3, 1))
        # 日2 暗藏：R -> 0
        v2 = self.system.conservation.day_value("C1", 2)
        self.assertEqual((v2["p"], v2["r"]), (3, 0))

    def test_provenance_tracks_loan_and_expo_sources(self) -> None:
        hid = _real_loan(self.system, "T1", "R1", "C1", "h1", "p1")
        self.system.conservation.attach_daylight("a1", hid, 0, 2, True)
        prov = self.system.conservation.light_provenance("C1")
        self.assertEqual(len(prov), 1)
        self.assertEqual(prov[0]["source"], f"loan:{hid}")


class AliasDoesNotMergeHistoryTest(unittest.TestCase):
    """作品别名合并不合并实物受光历史。"""

    def setUp(self) -> None:
        self.system = LibrarySystem(":memory:")
        copies = [
            {"id": "C1", "book": "WA", "branch": "north", "format": "hardcover"},
            {"id": "C2", "book": "WB", "branch": "north", "format": "hardcover"},
        ]
        self.system.catalog.submit_revision(
            "v1", ["WA", "WB"], [], copies, op_id="cat1"
        )
        _profile(self.system, "C1", 5, 5, 2)
        _profile(self.system, "C2", 5, 5, 2)

    def tearDown(self) -> None:
        self.system.close()

    def test_merge_keeps_per_copy_ledger_separate(self) -> None:
        self.system.ledger.register_ticket(
            "T1", "R1", "WA", 1, ["north"], ["hardcover"], op_id="tk1"
        )
        hold = self.system.ledger.reserve("h1", "v1", "C1")
        self.assertEqual(hold["status"], "ok")
        self.system.ledger.pickup("p1", hold["hold_id"], "R1", "C1")
        self.system.conservation.attach_daylight(
            "a1", hold["hold_id"], 0, 3, True
        )
        # 提交合并修订 WB -> WA
        merged_copies = [
            {"id": "C1", "book": "WA", "branch": "north", "format": "hardcover"},
            {"id": "C2", "book": "WB", "branch": "north", "format": "hardcover"},
        ]
        self.system.catalog.submit_revision(
            "v2", ["WA", "WB"], [{"from": "WB", "to": "WA"}],
            merged_copies, op_id="cat2",
        )
        self.system.catalog.activate_revision("v2")
        # 合并后 C2 的履历仍是空的，没有被 C1 的光照污染
        self.assertEqual(self.system.conservation.day_value("C2", 0)["p"], 0)
        self.assertEqual(self.system.conservation.light_provenance("C2"), [])
        # C1 的账原样可查
        self.assertEqual(self.system.conservation.day_value("C1", 0)["p"], 3)
        # 归还 C1 后，两册都作为同一规范作品 WA 的候选，但双限各自独立
        self.system.conservation.return_loan("r1", hold["hold_id"])
        plan = self.system.curator.plan_exhibition("WA", [1, 1], 2, 3)
        self.assertEqual(sorted(plan["candidate_copies"]), ["C1", "C2"])


class ReturnFreesCopyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.system = LibrarySystem(":memory:")
        _seed_work(self.system, ["C1"])
        _profile(self.system, "C1", 5, 5, 2)

    def tearDown(self) -> None:
        self.system.close()

    def test_unreturned_copy_blocked_then_return_opens_it(self) -> None:
        hid = _real_loan(self.system, "T1", "R1", "C1", "h1", "p1")
        # 实际未还：唯一副本 C1 被台账挡住，无册可展
        self.assertEqual(
            [c["copy_id"] for c in
             self.system.conservation.available_copies("v1")],
            [],
        )
        with self.assertRaises(Rejection) as ctx:
            self.system.curator.plan_exhibition("W", [1], 2, 3)
        self.assertEqual(ctx.exception.reason, "no_exhibitable_copy")
        # 馆内归还后开放
        r = self.system.conservation.return_loan("r1", hid)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(
            [c["copy_id"] for c in
             self.system.conservation.available_copies("v1")],
            ["C1"],
        )
        plan2 = self.system.curator.plan_exhibition("W", [1], 2, 3)
        self.assertEqual(plan2["minimum_cost"], 0)

    def test_return_requires_fulfilled_hold(self) -> None:
        self.system.ledger.register_ticket(
            "T1", "R1", "W", 1, ["north"], ["hardcover"], op_id="tk1"
        )
        hold = self.system.ledger.reserve("h1", "v1", "C1")
        r = self.system.conservation.return_loan("r1", hold["hold_id"])
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "hold_not_fulfilled")

    def test_return_is_idempotent(self) -> None:
        hid = _real_loan(self.system, "T1", "R1", "C1", "h1", "p1")
        r1 = self.system.conservation.return_loan("r1", hid)
        r2 = self.system.conservation.return_loan("r1", hid)
        self.assertEqual(r1, r2)
        # 再次归还已归还的笔 -> 拒绝
        r3 = self.system.conservation.return_loan("r2", hid)
        self.assertEqual(r3["error"]["reason"], "hold_not_fulfilled")


class WorkedExampleTest(unittest.TestCase):
    """题面三个数值样例的严格复刻。"""

    def _two_copy_system(self):
        system = LibrarySystem(":memory:")
        _seed_work(system, ["A", "B"])
        _profile(system, "A", 3, 3, 2)
        _profile(system, "B", 3, 3, 2)
        return system

    def test_example_one_a_burned_then_only_b_runs(self) -> None:
        system = self._two_copy_system()
        # 只给 A 补前一天真实取件、照 3 后归还；B 全程冷藏
        hid = _real_loan(system, "T1", "R1", "A", "h1", "p1")
        at = system.conservation.attach_daylight("a1", hid, 0, 3, True)
        self.assertEqual(at["status"], "ok")
        # A 的永久额度已用尽：无论是否归还，双限上都再受不下任何一度光
        self.assertEqual(system.conservation.day_value("A", 1)["p_headroom"], 0)
        # 实际未还期间：A 被台账占用挡住，候选只剩 B
        self.assertEqual(
            sorted(c["copy_id"] for c in
                   system.conservation.available_copies("v1")),
            ["B"],
        )
        # 归还 A：台账放行，但双限仍把 A 逐出一切可行受光动作，班表只会用 B
        system.conservation.return_loan("r1", hid)
        plan = system.curator.plan_exhibition("W", [1, 1], 2, 3)
        # 只能用 B：一天照 1、一天照 2，遮光费 3（换本无意义——A 受光即超限）
        self.assertEqual(plan["minimum_cost"], 3)
        for sch in plan["schedules"]:
            self.assertTrue(all(e["copy"] == "B" for e in sch["entries"]))
            lights = sorted(e["light"] for e in sch["entries"])
            self.assertEqual(lights, [1, 2])
        system.close()

    def test_example_two_swap_fee_five_keeps_single_copy(self) -> None:
        system = self._two_copy_system()
        # 不带任何借阅：两册全新；恢复基准，仅把换本费提到 5
        plan = system.curator.plan_exhibition("W", [1, 1], 5, 3)
        # 固定同册两天、其中一天照 1，最低 3（遮光 3 < 换本 5）
        self.assertEqual(plan["minimum_cost"], 3)
        for sch in plan["schedules"]:
            copies = {e["copy"] for e in sch["entries"]}
            self.assertEqual(len(copies), 1)
            self.assertEqual(sorted(e["light"] for e in sch["entries"]), [1, 2])
        system.close()

    def test_baseline_swap_fee_two_allows_two_optimal_forms(self) -> None:
        system = self._two_copy_system()
        plan = system.curator.plan_exhibition("W", [1, 1], 2, 3)
        # 基准：每天亮 2 换一次本，费 2；两版先后顺序并列最优
        self.assertEqual(plan["minimum_cost"], 2)
        sequences = [
            tuple((e["copy"], e["light"]) for e in sch["entries"])
            for sch in plan["schedules"]
        ]
        self.assertIn((("A", 2), ("B", 2)), sequences)
        self.assertIn((("B", 2), ("A", 2)), sequences)
        system.close()


class MandatorySwapExampleTest(unittest.TestCase):
    """长期受光、R 已恢复但 P 快用尽的真归还例：核出必须换册的场次，两版班表。"""

    def _system(self):
        system = LibrarySystem(":memory:")
        _seed_work(system, ["A", "B"])
        # 双限 3/3、暗复 2。
        # A：长期受光真归还，P=1/3（只剩最后一次全亮的余度），R 已暗藏恢复到满 3。
        _profile(system, "A", 3, 3, 2, ip=1, ir=3)
        _profile(system, "B", 3, 3, 2)
        # A 的初始状态以一笔已归还的旧借阅 + 馆藏初始状态接入（无附加光照记录）
        hid = _real_loan(system, "T1", "R1", "A", "h1", "p1")
        system.conservation.return_loan("r1", hid)
        return system

    def test_single_copy_cannot_cover_two_full_bright_days(self) -> None:
        system = self._system()
        # 两天最低均 2：B 全新独自两天全亮 P 累计 4>3，且最低 2 不许遮光
        b_only = {
            tuple(e["light"] for e in sch["entries"])
            for sch in system.curator.plan_exhibition("W", [2, 2], 2, 3)["schedules"]
            if all(e["copy"] == "B" for e in sch["entries"])
        }
        self.assertEqual(b_only, set())
        system.close()

    def test_swap_fee_two_forces_swap_at_cost_two(self) -> None:
        system = self._system()
        plan = system.curator.plan_exhibition("W", [2, 2], 2, 3)
        self.assertEqual(plan["minimum_cost"], 2)
        self.assertEqual(plan["schedule_count"], 1)
        entries = plan["schedules"][0]["entries"]
        self.assertEqual([e["copy"] for e in entries], ["B", "A"])
        self.assertEqual([e["light"] for e in entries], [2, 2])
        self.assertTrue(entries[1]["swapped"])
        # 换册让 A 只吃最后一度全亮，展后 A 双限到顶、不可再展
        end = plan["schedules"][0]["end_state"]["A"]
        self.assertEqual((end["p"], end["r"]), (3, 3))
        self.assertEqual((end["p_headroom"], end["r_headroom"]), (0, 0))
        system.close()

    def test_swap_fee_five_reopens_second_version(self) -> None:
        system = self._system()
        plan = system.curator.plan_exhibition("W", [2, 2], 5, 3)
        # 结构上仍必须换册（无遮光可走），费用随换本费变为 5
        self.assertEqual(plan["minimum_cost"], 5)
        entries = plan["schedules"][0]["entries"]
        self.assertEqual([e["copy"] for e in entries], ["B", "A"])
        system.close()

    def test_when_a_p_cap_exhausted_no_schedule_exists(self) -> None:
        system = LibrarySystem(":memory:")
        _seed_work(system, ["A", "B"])
        _profile(system, "A", 3, 3, 2, ip=3, ir=3)  # A 永久额度尽
        _profile(system, "B", 3, 3, 2)
        with self.assertRaises(Rejection) as ctx:
            system.curator.plan_exhibition("W", [2, 2], 2, 3)
        self.assertEqual(ctx.exception.reason, "no_feasible_schedule")
        system.close()


class RegisterExhibitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.system = LibrarySystem(":memory:")
        _seed_work(self.system, ["A", "B"])
        _profile(self.system, "A", 3, 3, 2)
        _profile(self.system, "B", 3, 3, 2)

    def tearDown(self) -> None:
        self.system.close()

    def _plan(self, swap=2, shade=3, levels=(1, 1)):
        return self.system.curator.plan_exhibition(
            "W", list(levels), swap, shade
        )

    def test_register_writes_occupancy_and_light_history(self) -> None:
        plan = self._plan()
        chosen = plan["schedules"][0]
        r = self.system.curator.register_exhibition(
            "reg1", "E1", _payload(plan, chosen["entries"]),
        )
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["total_cost"], plan["minimum_cost"])
        expo = self.system.curator.get_exhibition("E1")
        self.assertEqual(len(expo["entries"]), 2)
        # 实际受光进入保存员履历，来源标注本场展出
        for cid in ("A", "B"):
            prov = self.system.conservation.light_provenance(cid)
            self.assertTrue(any(x["source"] == "expo:E1" for x in prov))

    def test_duplicate_registration_does_not_double_spend_light(self) -> None:
        plan = self._plan()
        payload = _payload(plan, plan["schedules"][0]["entries"])
        first = self.system.curator.register_exhibition("reg1", "E1", payload)
        replay = self.system.curator.register_exhibition("reg1", "E1", payload)
        self.assertEqual(first, replay)
        # 每册每日仍只有一份读数
        for cid in ("A", "B"):
            self.assertEqual(
                len(self.system.conservation.daily_exposure(cid)), 1
            )

    def test_copy_taken_by_ledger_cannot_be_signed_exhibitable(self) -> None:
        plan = self._plan()
        # 登记前，旧入口真正占走 A（held 保留）
        self.system.ledger.register_ticket(
            "T1", "R1", "W", 1, ["north"], ["hardcover"], op_id="tk1"
        )
        self.system.ledger.reserve("h9", "v1", "A")
        payload = _payload(plan, plan["schedules"][0]["entries"])
        r = self.system.curator.register_exhibition("reg1", "E1", payload)
        self.assertEqual(r["status"], "rejected")
        self.assertIn(r["error"]["reason"],
                      ("copy_not_exhibitable", "schedule_not_optimal_or_invalid"))
        self.assertEqual(self.system.curator.list_exhibitions(), [])

    def test_non_optimal_schedule_rejected(self) -> None:
        plan = self._plan(swap=2, shade=3)
        # 手工构造一个更贵的班表（两天都遮光），最低亮度允许但非最优
        expensive = [
            {"day": 0, "copy": "A", "light": 1, "shaded": True},
            {"day": 1, "copy": "B", "light": 1, "shaded": True},
        ]
        payload = _payload(plan, expensive)
        r = self.system.curator.register_exhibition("reg1", "E1", payload)
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "schedule_not_optimal_or_invalid")


class BoundAndScaleTest(unittest.TestCase):
    """题面上限：最多 6 册、10 日；余度不能跨册相加。"""

    def test_six_copies_ten_days_plans_and_replays(self) -> None:
        system = LibrarySystem(":memory:")
        ids = [f"C{i}" for i in range(6)]
        _seed_work(system, ids)
        for cid in ids:
            _profile(system, cid, 10, 10, 2)
        levels = [1] * 10
        plan = system.curator.plan_exhibition("W", levels, 2, 3)
        self.assertEqual(plan["days"], 10)
        # 每个在册册子的双限独立核算：抽查任意展本日终不超限
        for sch in plan["schedules"]:
            for e in sch["entries"]:
                self.assertLessEqual(e["end_p"], 10)
                self.assertLessEqual(e["end_r"], 10)
        system.close()

    def test_headrooms_are_never_summed_across_copies(self) -> None:
        system = LibrarySystem(":memory:")
        _seed_work(system, ["A", "B"])
        # 每册 P 上限 1：两册各只能吃一度；两天最低均 2（每天两度）-> 无解，
        # 即便两册余度相加为 2，也不能当作一册的两度
        _profile(system, "A", 1, 5, 1)
        _profile(system, "B", 1, 5, 1)
        with self.assertRaises(Rejection) as ctx:
            system.curator.plan_exhibition("W", [2, 2], 2, 3)
        self.assertEqual(ctx.exception.reason, "no_feasible_schedule")
        system.close()


class PersistenceTest(unittest.TestCase):
    def test_reopen_keeps_light_ledger_and_exhibitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "lib.db")
            system = LibrarySystem(path)
            _seed_work(system, ["A", "B"])
            _profile(system, "A", 3, 3, 2)
            _profile(system, "B", 3, 3, 2)
            plan = system.curator.plan_exhibition("W", [1, 1], 2, 3)
            chosen = plan["schedules"][0]
            system.curator.register_exhibition(
                "reg1", "E1", _payload(plan, chosen["entries"]),
            )
            system.close()

            reopened = LibrarySystem.reopen(path)
            expo = reopened.curator.get_exhibition("E1")
            self.assertEqual(len(expo["entries"]), 2)
            self.assertEqual(expo["start_day"], plan["start_day"])
            # 光照履历与逐册状态原样可查（落在真实日历日）
            a_entry = next(e for e in expo["entries"] if e["copy"] == "A")
            self.assertEqual(
                len(reopened.conservation.daily_exposure("A")), 1
            )
            self.assertEqual(
                reopened.conservation.day_value(
                    "A", a_entry["calendar_day"]
                )["p"],
                a_entry["light"],
            )
            reopened.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
