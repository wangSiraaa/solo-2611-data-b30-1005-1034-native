"""声控接点检测与展台排班测试。"""
import shutil
from fractions import Fraction as F

import numpy as np

from audiomix.assets import AssetRegistry, sequence
from audiomix.engine import BLOCK_SAMPLES
from audiomix.lightshow import (
    ContactDetector, ContactSpec, check_plan, detect_over_pcm, plan_show,
)


def _det(H, L, n_on=1, n_off=1):
    return ContactDetector(ContactSpec("c", "r", H, L, n_on, n_off))


def test_single_sample_trigger():
    # H=2,L=0,n_on=n_off=1；|3|>=H 即触发，高样本后接 0 复位
    d = _det(2, 0)
    d.feed(np.array([0, 3, 0, 3, 0, 3, 0]))
    assert d.pulse_beats() == [1, 3, 5]
    assert d.n_scanned() == 7
    # 每脉冲的连续范围、来源、值都在
    p = d.pulses[0].to_json()
    assert p["beat"] == 1 and p["run_start"] == 1 and p["run_end"] == 1
    assert p["value"] == 3 and p["render_id"] == "r" and p["contact_id"] == "c"
    print("  [OK] 单样本触发：脉冲=1,3,5，来源/连续范围/值齐备")


def test_consecutive_start_count():
    # n_on=3：连续 3 个 |x|>=H 才在最后一拍发脉冲
    d = _det(5, 1, n_on=3, n_off=1)
    d.feed(np.array([6, 6, 0, 6, 6, 6, 0]))
    # 第一段只有 2 个（被 0 打断），第二段 3 个 -> 在第 5 拍触发
    assert d.pulse_beats() == [5], d.pulse_beats()
    print("  [OK] 连续启动数：长度 3 才触发，短段被打断")


def test_middle_value_interrupts_start():
    # 中间值 L<|x|<H 既不计启动也不计复位，并打断未完成启动段
    d = _det(5, 1, n_on=3, n_off=1)
    # [6,6,3,6,6,6] 中 3 打断前两个高样本，需重新累计 3 个 -> 第 5 拍
    d.feed(np.array([6, 6, 3, 6, 6, 6]))
    assert d.pulse_beats() == [5], d.pulse_beats()
    print("  [OK] 中间值打断未完成启动段")


def test_reset_count_and_middle_break():
    # n_off=2：上锁后需连续 2 个 |x|<=L 才复位；中间值打断复位计数
    d = _det(5, 1, n_on=1, n_off=2)
    d.feed(np.array([6, 3, 0, 0]))   # 6 触发锁；3 中间值打断；0,0 才复位
    assert d.pulse_beats() == [0]
    assert d.locked_at(0) and d.locked_at(1) and d.locked_at(2)
    assert not d.locked_at(3)        # 第 3 拍凑够 2 个 0 -> 复位
    # 上锁期间不会产生新脉冲
    assert d.pulse_beats() == [0]
    print("  [OK] 复位连续计数 + 中间值打断，锁定期间不发脉冲")


def test_lock_explanation_query():
    d = _det(5, 1, n_on=2, n_off=2)
    d.feed(np.array([0, 6, 6, 0, 0, 0]))
    # 第 2 拍触发锁，第 4 拍（两个 0 到齐）复位
    exp = d.lock_explanation(1)
    assert exp is None                      # 第 1 拍时尚未触发
    exp = d.lock_explanation(2)
    assert exp is not None and exp["beat"] == 2 and exp["unbeat"] == 4
    assert exp["run_start"] == 1 and exp["run_end"] == 2
    assert "连续 2 样本触发" in exp["reason"]
    assert d.state_at(3) == "locked" and d.state_at(4) == "can-start"
    print("  [OK] 指定拍查询锁定原因（来源连续段 + 恢复拍）")


def test_negative_abs_threshold():
    d = _det(5, 0, n_on=2, n_off=1)
    d.feed(np.array([-6, -7, 0, 4]))
    assert d.pulse_beats() == [1]     # |.|>=5 的两个样本触发
    print("  [OK] 阈值按绝对值判定（负样本同样触发）")


def test_cross_block_not_reset():
    # 启动段跨越 1600 样本块边界，逐块 feed 不得重置
    n_on = BLOCK_SAMPLES + 100
    vals = [10] * n_on + [0] * 5
    pcm = np.array(vals)
    d = _det(5, 0, n_on=n_on, n_off=1)
    for b0 in range(0, len(pcm), BLOCK_SAMPLES):
        d.feed(pcm[b0:b0 + BLOCK_SAMPLES])
    assert d.pulse_beats() == [n_on - 1]
    # 整体检测与逐块检测结果一致
    d2 = detect_over_pcm(ContactSpec("c", "r", 5, 0, n_on, 1), pcm)
    assert d2.pulse_beats() == d.pulse_beats()
    # 若在块边界重置启动计数，则第二块剩余样本 < n_on，不会触发
    d3 = _det(5, 0, n_on=n_on, n_off=1)
    d3.feed(pcm[:BLOCK_SAMPLES])
    d3._on_count = 0
    d3.feed(pcm[BLOCK_SAMPLES:n_on])
    assert d3.pulse_beats() == []
    print("  [OK] 跨块不重置启动计数（重置则漏触发）")


# ---------------------------- 排班测试 ---------------------------- #

def test_plan_base_one_gate_one_manual():
    plan = plan_show(required=[("L1", 1), ("L1", 5)],
                     pulses_by_contact={"p": [1, 3, 5]},
                     gates_available=1, contacts=["p"],
                     roster={1: ["a"], 5: ["a"]}, beats_total=7)
    assert plan.manual_count == 1 and plan.gate_count == 1
    assert not plan.uncovered
    # 合法窗只能含必亮拍；长窗 [1,5] 会误传 3，故不可用
    for g in plan.gates:
        for b in plan.gate_auto[0]["beats"]:
            assert b in (1, 5)
    assert check_plan(plan, {1: ["a"], 5: ["a"]})["valid"]
    print("  [OK] 基础：一门 + 一次手按，自动窗不含禁亮相")


def test_plan_cancel_long_gate():
    plan = plan_show(required=[("L1", 1), ("L1", 5)],
                     pulses_by_contact={"p": [1, 5]},
                     gates_available=1, contacts=["p"],
                     roster={1: ["a"], 5: ["a"]}, beats_total=7)
    assert plan.manual_count == 0 and plan.gate_count == 1 and not plan.uncovered
    assert [plan.gates[0].win_start, plan.gates[0].win_end] == [1, 5]
    assert check_plan(plan)["valid"]
    print("  [OK] 相消后脉冲 1,5：一个长门零手按")


def test_plan_two_windows_zero_manual():
    plan = plan_show(required=[("L1", 1), ("L1", 5)],
                     pulses_by_contact={"p": [1, 3, 5]},
                     gates_available=2, contacts=["p"],
                     roster={1: ["a"], 5: ["a"]}, beats_total=7)
    assert plan.manual_count == 0 and plan.gate_count == 2 and not plan.uncovered
    wins = sorted((g.win_start, g.win_end) for g in plan.gates)
    assert wins == [(1, 1), (5, 5)]
    # 两门不重叠，不会在同拍重复点亮
    assert check_plan(plan)["valid"]
    print("  [OK] 两个门分开两窗：零手按")


def test_plan_same_beat_two_lights_one_duty():
    req = [("L1", 3), ("L2", 3)]
    p1 = plan_show(required=req, pulses_by_contact={"p": [3]},
                   gates_available=1, contacts=["p"],
                   roster={3: ["b"]}, beats_total=7)
    assert p1.manual_count == 1 and p1.gate_count == 1 and not p1.uncovered
    assert check_plan(p1, {3: ["b"]})["valid"]
    # 无门：两盏都要手按，一人一拍只能按一盏 -> 必有一亮点无人能补，显式报出
    p0 = plan_show(required=req, pulses_by_contact={"p": [3]},
                   gates_available=0, contacts=["p"],
                   roster={3: ["b"]}, beats_total=7)
    assert p0.manual_count == 1 and len(p0.uncovered) == 1
    assert p0.uncovered[0]["beat"] == 3
    print("  [OK] 同拍两灯争一人：一盏手按；无门时显式报未覆盖")


def test_plan_no_duty_no_gate_uncovered():
    # 无值班员、无门：所有必亮点都无人能补，全部列出，不得漏
    req = [("L1", 2), ("L1", 4)]
    plan = plan_show(required=req, pulses_by_contact={"p": [2, 4]},
                     gates_available=0, contacts=["p"], roster={}, beats_total=6)
    assert plan.manual_count == 0
    assert {(u["light_id"], u["beat"]) for u in plan.uncovered} == {("L1", 2), ("L1", 4)}
    cc = check_plan(plan)
    assert not cc["valid"] and any("无人能补" in x for x in cc["problems"])
    print("  [OK] 无门无人：所有必亮点列入 uncovered，核对拒绝通过")


def test_plan_never_lights_forbidden_beat():
    # 给一个故意冲突的接点：窗若跨越禁亮相就非法；求解必须避开它
    req = [("L1", 1), ("L1", 5)]
    plan = plan_show(required=req, pulses_by_contact={"p": [1, 3, 5]},
                     gates_available=3, contacts=["p"],
                     roster={1: ["a"], 3: ["a"], 5: ["a"]}, beats_total=7)
    # 禁亮拍 3 不能由任何门点亮；逐拍核对都无 forbidden_on
    assert all(not row["forbidden_on"] for row in plan.beats)
    cc = check_plan(plan, {1: ["a"], 3: ["a"], 5: ["a"]})
    assert cc["valid"], cc
    print("  [OK] 任何门都不得点亮禁亮拍（逐拍核对通过）")


def test_plan_gate_used_once_per_show():
    plan = plan_show(required=[("L1", 1), ("L1", 5)],
                     pulses_by_contact={"p": [1, 5]},
                     gates_available=2, contacts=["p"], beats_total=7)
    # 一个门已足够覆盖两点，同分手按 0 时少用门 -> 只用 1 个
    assert plan.gate_count == 1
    assert len({g.gate_id for g in plan.gates}) == plan.gate_count
    print("  [OK] 同分少用门：零手按时只配一门")


def run_all(base="/tmp/audiomix_light_tests"):
    shutil.rmtree(base, ignore_errors=True)
    print("== 声控检测 ==")
    for fn in [test_single_sample_trigger, test_consecutive_start_count,
               test_middle_value_interrupts_start, test_reset_count_and_middle_break,
               test_lock_explanation_query, test_negative_abs_threshold,
               test_cross_block_not_reset]:
        fn()
    print("== 展台排班 ==")
    for fn in [test_plan_base_one_gate_one_manual, test_plan_cancel_long_gate,
               test_plan_two_windows_zero_manual, test_plan_same_beat_two_lights_one_duty,
               test_plan_no_duty_no_gate_uncovered,
               test_plan_never_lights_forbidden_beat,
               test_plan_gate_used_once_per_show]:
        fn()
    print("声控检测与排班测试全部通过")


if __name__ == "__main__":
    run_all()
