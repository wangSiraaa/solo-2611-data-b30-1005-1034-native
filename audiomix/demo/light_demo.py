"""展台灯箱声控走台演示：真实 WAV -> 实际混音 -> 逐样本检测 -> 自动门/手按派工。

全部按题面五个样例走真实数据，不把目标亮点照抄成结果：
  1. 基础：灯1 只许在 1、5 亮；PCM [0,3,0,3,0,3,0]，H=2,L=0,连续1。
     脉冲 1、3、5；一个门 -> 最少手按 1 次（长接必在 3 误亮，故不能一根到底）。
  2. 相消：保持灯班，原节目中间加一个真正 -3 的短 clip（第 3 拍 3+(-3)=0），
     重新渲染 -> 脉冲只剩 1、5；接一个长门 [1,5] -> 零手按。
  3. 固定原档（脉冲仍 1、3、5），自动门加到两个，分开两窗 [1,1]、[5,5] -> 零手按。
  4. 两灯同拍争一名值班员：两灯都要在第 3 拍亮，只有一名值班员；一盏靠门，
     另一盏手按（若零门则一亮点无人能补，必须显式报出，不得漏）。
  5. 一次持续高音跨原块边界：连续高样本跨越 1600 边界，启动计数不在跨块时重置。

运行：python3 -m demo.light_demo /tmp/audiomix_light_demo
"""
from __future__ import annotations

import os
import shutil
import sys
from fractions import Fraction as F

from audiomix.archive import ArchiveStore
from audiomix.assets import AssetRegistry, sequence
from audiomix.lightshow import (
    check_plan, detect_from_archive, plan_show,
)
from audiomix.timeline import TimelineEditor

RATE = 16000


def _print(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def _render(reg: AssetRegistry, name: str, base: str, specs, T):
    """建独立时间轴 -> 发布产品 -> 渲染入仓 -> 导出 WAV；返回 (store, render_id, wav)。"""
    ed = TimelineEditor(base, reg, name=name)
    ed.insert(specs)
    product = ed.publish()
    prod_path = os.path.join(base, f"product_{name}.json")
    ed.save_product(product, prod_path)
    store = ArchiveStore(os.path.join(base, "archive"))
    rid = f"r_{name}"
    store.render_from_files(prod_path, reg, T, render_id=rid)
    pcm = store.full_pcm(rid)
    wav = os.path.join(base, f"{name}.wav")
    store.export_wav(rid, wav)
    return store, rid, wav, pcm, product


def scenario_1(base, reg, a_beeps):
    _print("样例 1：基础 —— 灯1 只许在 1、5 亮，一个门，最少手按 1 次")
    T = F(7, RATE)
    store, rid, wav, pcm, product = _render(
        reg, "s1", base,
        [{"clip_id": "beeps", "asset_id": a_beeps, "a": 0, "b": 7, "start": "0"}], T)
    print("真实 PCM =", pcm.tolist(), "  WAV ->", wav)
    det = detect_from_archive(store, rid, H=2, L=0, n_on=1, n_off=1,
                              contact_id="pickup")
    print("检测脉冲 =", det.pulse_beats(), "（覆盖来源/连续范围/锁定原因）")
    assert det.pulse_beats() == [1, 3, 5], det.pulse_beats()

    # 一个门 + 一名值班员：长接（窗 [1,5]）会在第 3 拍误亮（3 不是灯1 必亮点），
    # 故合法自动窗只能覆盖 1 或 5 之一，另一点手按。自动派生候选只切合法窗。
    plan = plan_show(required=[("L1", 1), ("L1", 5)],
                     pulses_by_contact={"pickup": det.pulse_beats()},
                     gates_available=1, contacts=["pickup"],
                     roster={1: ["alice"], 5: ["alice"]}, beats_total=7)
    print("走台单：手按", plan.manual_count, "次；自动门", plan.gate_count,
          "个；未覆盖", len(plan.uncovered))
    print("  自动门:", [g.to_json() for g in plan.gates])
    print("  手按  :", plan.manual_presses)
    assert plan.manual_count == 1 and plan.gate_count == 1
    assert not plan.uncovered
    cc = check_plan(plan, roster={1: ["alice"], 5: ["alice"]})
    print("  独立核对:", cc)
    assert cc["valid"]
    # 窗内每个脉冲都必须是灯1 的必亮拍；3 不该出现在任何自动窗里
    for g in plan.gate_auto:
        assert all(b in (1, 5) for b in g["beats"]), g
    store.close()


def scenario_2_cancel(base, reg, a_beeps, a_neg3):
    _print("样例 2：中间加真正相消的 -3 短 clip，脉冲只剩 1、5，长门零手按")
    T = F(7, RATE)
    store, rid, wav, pcm, product = _render(
        reg, "s2", base,
        [
            {"clip_id": "beeps", "asset_id": a_beeps, "a": 0, "b": 7, "start": "0"},
            # 真正相消：在第 3 拍（样本下标 3）叠加 -3，使 3 + (-3) = 0
            {"clip_id": "cancel", "asset_id": a_neg3, "a": 0, "b": 1,
             "start": F(3, RATE)},
        ], T)
    print("相消后真实 PCM =", pcm.tolist(), "  WAV ->", wav)
    assert pcm.tolist() == [0, 3, 0, 0, 0, 3, 0], pcm.tolist()
    det = detect_from_archive(store, rid, H=2, L=0, n_on=1, n_off=1,
                              contact_id="pickup")
    print("检测脉冲 =", det.pulse_beats())
    assert det.pulse_beats() == [1, 5], det.pulse_beats()
    plan = plan_show(required=[("L1", 1), ("L1", 5)],
                     pulses_by_contact={"pickup": det.pulse_beats()},
                     gates_available=1, contacts=["pickup"],
                     roster={1: ["alice"], 5: ["alice"]}, beats_total=7)
    print("走台单：手按", plan.manual_count, "次；自动门",
          [(g.to_json()) for g in plan.gates])
    assert plan.manual_count == 0 and plan.gate_count == 1 and not plan.uncovered
    assert plan.gates[0].win_start == 1 and plan.gates[0].win_end == 5
    store.close()


def scenario_3_two_windows(base, reg, a_beeps):
    _print("样例 3：固定原档（脉冲 1、3、5），两个门，分开两窗 -> 零手按")
    T = F(7, RATE)
    store, rid, wav, pcm, product = _render(
        reg, "s3", base,
        [{"clip_id": "beeps", "asset_id": a_beeps, "a": 0, "b": 7, "start": "0"}], T)
    print("真实 PCM =", pcm.tolist())
    det = detect_from_archive(store, rid, H=2, L=0, n_on=1, n_off=1,
                              contact_id="pickup")
    assert det.pulse_beats() == [1, 3, 5]
    plan = plan_show(required=[("L1", 1), ("L1", 5)],
                     pulses_by_contact={"pickup": det.pulse_beats()},
                     gates_available=2, contacts=["pickup"],
                     roster={1: ["alice"], 5: ["alice"]}, beats_total=7)
    print("走台单：手按", plan.manual_count, "次；自动门",
          [g.to_json() for g in plan.gates])
    assert plan.manual_count == 0 and plan.gate_count == 2 and not plan.uncovered
    wins = sorted(tuple(g.to_json()["window"]) for g in plan.gates)
    assert wins == [(1, 1), (5, 5)], wins
    store.close()


def scenario_4_two_lights_one_duty(base, reg, a_two):
    _print("样例 4：两灯同拍争一名值班员（第 3 拍两灯都要亮）")
    T = F(7, RATE)
    store, rid, wav, pcm, product = _render(
        reg, "s4", base,
        [{"clip_id": "beeps", "asset_id": a_two, "a": 0, "b": 7, "start": "0"}], T)
    print("真实 PCM =", pcm.tolist(), "  WAV ->", wav)
    det = detect_from_archive(store, rid, H=2, L=0, n_on=1, n_off=1,
                              contact_id="pickup")
    print("检测脉冲 =", det.pulse_beats())
    req = [("L1", 3), ("L2", 3)]
    # 有一个门：一盏自动、另一盏手按（一人一拍只能按一盏）
    plan1 = plan_show(required=req, pulses_by_contact={"pickup": det.pulse_beats()},
                      gates_available=1, contacts=["pickup"],
                      roster={3: ["bob"]}, beats_total=7)
    print("可用 1 个门 -> 手按", plan1.manual_count, "自动",
          [g.to_json() for g in plan1.gates], "未覆盖", plan1.uncovered)
    assert plan1.manual_count == 1 and plan1.gate_count == 1 and not plan1.uncovered
    cc = check_plan(plan1, roster={3: ["bob"]})
    assert cc["valid"], cc
    # 零门：两盏都要手按，但只有 bob 一人 -> 必有一亮点无人能补，必须显式报出
    plan0 = plan_show(required=req, pulses_by_contact={"pickup": det.pulse_beats()},
                      gates_available=0, contacts=["pickup"],
                      roster={3: ["bob"]}, beats_total=7)
    print("可用 0 个门 -> 手按", plan0.manual_count, "未覆盖", plan0.uncovered)
    assert len(plan0.uncovered) == 1 and plan0.manual_count == 1
    store.close()


def scenario_5_cross_block(base, reg):
    _print("样例 5：一次持续高音跨原块边界（1600 样本）启动计数不重置")
    # 1900 个高样本(>=H) + 100 个低样本；H=50,L=10,n_on=1800,n_off=1。
    vals = [100] * 1900 + [0] * 100
    a_hold = reg.register(sequence(vals), RATE, note="sustained-cross-block").asset_id
    T = F(len(vals), RATE)
    store, rid, wav, pcm, product = _render(
        reg, "s5", base,
        [{"clip_id": "hold", "asset_id": a_hold, "a": 0, "b": len(vals), "start": "0"}], T)
    print("样本数 =", len(pcm), " 块数 =", store.render_meta(rid)["n_blocks"],
          " 块边界 = 1600")
    # 逐块手动喂，验证跨块不重置
    from audiomix.lightshow import ContactDetector, ContactSpec
    spec = ContactSpec("hold", rid, H=50, L=10, n_on=1800, n_off=1)
    det = ContactDetector(spec)
    n = len(pcm)
    for b0 in range(0, n, 1600):
        det.feed(pcm[b0:b0 + 1600])
    print("检测脉冲 =", det.pulse_beats(), "(应仅一个，且在第 1799 拍，位于第 2 块)")
    assert det.pulse_beats() == [1799], det.pulse_beats()
    # 若在块边界重置启动计数，则第 2 块只有 300 个高样本 < 1800，不会发脉冲
    det_reset = ContactDetector(spec)
    blk0 = pcm[:1600]
    det_reset.feed(blk0)
    det_reset._on_count = 0  # 人为模拟“跨块重置”
    det_reset.feed(pcm[1600:2000])
    print("若跨块重置 -> 脉冲 =", det_reset.pulse_beats(), "(无脉冲，证明原行为必要)")
    assert det_reset.pulse_beats() == []
    # 锁定原因可查：脉冲 1799 后上锁，高样本持续到 1899，此后 0 样本在 1900 复位
    assert det.lock_explanation(1798) is None
    assert det.locked_at(1800) and det.locked_at(1899)
    assert det.lock_explanation(1850) is not None
    assert not det.locked_at(1900)          # 第 1900 拍出现连续复位样本，解锁
    print("锁定原因（第 1850 拍仍锁）:", det.lock_explanation(1850))
    store.close()


def main(base: str) -> None:
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    reg = AssetRegistry(os.path.join(base, "registry"))
    a_beeps = reg.register(sequence([0, 3, 0, 3, 0, 3, 0]), RATE,
                           note="base-beeps").asset_id
    a_neg3 = reg.register(sequence([-3]), RATE, note="cancel-neg3").asset_id
    a_two = reg.register(sequence([0, 0, 0, 5, 0, 0, 0]), RATE,
                         note="single-at-3").asset_id
    print("素材登记：base-beeps, cancel-neg3, single-at-3")

    scenario_1(base, reg, a_beeps)
    scenario_2_cancel(base, reg, a_beeps, a_neg3)
    scenario_3_two_windows(base, reg, a_beeps)
    scenario_4_two_lights_one_duty(base, reg, a_two)
    scenario_5_cross_block(base, reg)

    print("\n全部声控走台样例通过（真实 WAV/实际混音/逐样本检测/派工）。")
    print("WAV 与产品位于:", base)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/audiomix_light_demo")
