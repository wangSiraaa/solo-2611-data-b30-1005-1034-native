"""盲听试验统筹：把比较项数据、人员空闲表与耳机容量接成端到端流程。

流程：
1. 逐项枚举合规增益组合（blindstim.build_combos），无合规组合的项标记不可制作；
2. 每项任选一组合规组合封成匿名实包 + 封存追溯表（多项组合时任选，确定性取第一组）；
3. 按人员/时段/耳机约束求解两序排期（blindschedule.solve）；
4. 输出逐人逐时段匿名播放单，并由持封存表的复核函数验证两序确实相反。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .blindstim import (
    ItemStimulusSet, StimulusCombo, StimulusError,
    build_combos, make_item_packages, verify_bundle_integrity, write_stimulus_bundle,
)
from .blindschedule import ItemSpec, Problem, Schedule, verify_schedule


class TrialError(ValueError):
    pass


@dataclass
class ItemInput:
    item_id: str
    pcm_a: list[int]
    pcm_b: list[int]
    gains_a: list[int]
    gains_b: list[int]
    weight: int
    deadline: int
    forbidden_listeners: list[str] = field(default_factory=list)


@dataclass
class TrialResult:
    stimulus_sets: dict[str, ItemStimulusSet]
    rejected: dict[str, str]              # 不可制作项 -> 原因
    schedule: Schedule
    problem: Problem

    def completed(self) -> list[str]:
        return sorted(self.schedule.completed_items)

    def zero_items(self) -> list[str]:
        """得 0 的项：要么刺激做不出，要么两序排不齐。"""
        return sorted(set(it.item_id for it in self.problem.items)
                      - self.schedule.completed_items)


def build_problem(inputs: list[ItemInput], listeners: list[str], n_slots: int,
                  headphone_capacity: list[int],
                  availability: dict[str, list[int]],
                  cross_person_quota: int | None = None) -> tuple[Problem, dict[str, list[StimulusCombo]]]:
    """制作刺激并构造排期问题；返回问题与每项的全部合规组合。"""
    all_combos: dict[str, list[StimulusCombo]] = {}
    specs: list[ItemSpec] = []
    for data in inputs:
        combos = build_combos(data.item_id, data.pcm_a, data.pcm_b,
                              data.gains_a, data.gains_b)
        all_combos[data.item_id] = combos
        specs.append(ItemSpec(
            item_id=data.item_id, weight=data.weight, deadline=data.deadline,
            forbidden_listeners=frozenset(data.forbidden_listeners),
            feasible=bool(combos)))
    prob = Problem(
        items=specs, listeners=list(listeners), n_slots=n_slots,
        headphone_capacity=list(headphone_capacity),
        availability={p: frozenset(availability.get(p, [])) for p in listeners},
        cross_person_quota=cross_person_quota)
    prob.validate()
    return prob, all_combos


def run_trial(inputs: list[ItemInput], listeners: list[str], n_slots: int,
              headphone_capacity: list[int], availability: dict[str, list[int]],
              cross_person_quota: int | None = None,
              rng: Any | None = None) -> TrialResult:
    """端到端：制作 -> 封包 -> 排期 -> 独立校验。"""
    prob, all_combos = build_problem(
        inputs, listeners, n_slots, headphone_capacity, availability,
        cross_person_quota)
    sets: dict[str, ItemStimulusSet] = {}
    rejected: dict[str, str] = {}
    for data in inputs:
        combos = all_combos[data.item_id]
        if not combos:
            rejected[data.item_id] = _reject_reason(data)
            continue
        sets[data.item_id] = make_item_packages(combos[0], rng=rng)
    schedule = _solve_checked(prob, sets)
    return TrialResult(stimulus_sets=sets, rejected=rejected,
                       schedule=schedule, problem=prob)


def _reject_reason(data: ItemInput) -> str:
    """把“无合规组合”细分为越界退回 / 配不平 / 全相同，便于报告。"""
    from .blindstim import _scale_or_none, sum_of_squares
    reasons = []
    for ga in sorted(set(data.gains_a)):
        if _scale_or_none(data.pcm_a, ga) is None:
            reasons.append(f"A×{ga} 超 16 位范围整组退回")
    for gb in sorted(set(data.gains_b)):
        if _scale_or_none(data.pcm_b, gb) is None:
            reasons.append(f"B×{gb} 超 16 位范围整组退回")
    if reasons:
        return "；".join(reasons)
    pairs = []
    for ga in sorted(set(data.gains_a)):
        sa = _scale_or_none(data.pcm_a, ga)
        for gb in sorted(set(data.gains_b)):
            sb = _scale_or_none(data.pcm_b, gb)
            if sa is None or sb is None:
                continue
            if sum_of_squares(sa) != sum_of_squares(sb):
                pairs.append(f"({ga},{gb}) 平方和 {sum_of_squares(sa)}≠{sum_of_squares(sb)}")
            elif all(x == y for x, y in zip(sa, sb)):
                pairs.append(f"({ga},{gb}) 两份字节完全相同")
    return "无合规增益组合：" + ("；".join(pairs) if pairs else "区间不等长或无增益档")


def _solve_checked(prob: Problem,
                   sets: dict[str, ItemStimulusSet]) -> Schedule:
    from .blindschedule import solve
    sch = solve(prob)
    errors = verify_schedule(sch, prob)
    if errors:
        raise TrialError("内部错误：求解器产出的排期未通过独立校验: "
                         + " | ".join(errors))
    for iid in sch.completed_items:
        verify_bundle_integrity(sets[iid])
    return sch


# 持封存表的最终复核见 verify_trial_playlists（排期硬约束 + 两序相反 + 实包可追溯）
def verify_trial_playlists(result: TrialResult) -> list[str]:
    """持封存表的最终复核：排期硬约束 + 每个完成项两序相反 + 实包可追溯。"""
    errors = verify_schedule(result.schedule, result.problem)
    for iid in result.schedule.completed_items:
        stim = result.stimulus_sets.get(iid)
        if stim is None:
            errors.append(f"项 {iid} 已计完成但缺少匿名实包")
            continue
        try:
            verify_bundle_integrity(stim)
        except StimulusError as exc:
            errors.append(f"项 {iid} 实包复核失败: {exc}")
        sessions = result.schedule.sessions_of(iid)
        seq = [tuple(stim.order_for(s.first_render)) for s in sessions]
        if len(seq) == 2 and (seq[0] == seq[1] or seq[0][0] != seq[1][1]):
            errors.append(f"项 {iid} 两场播放序不是严格相反: {seq}")
    return errors


def anonymous_play_sheets(result: TrialResult) -> dict[str, list[dict[str, Any]]]:
    """逐人逐时段的匿名播放单：只出现匿名号，不出现 A/B 源身份。"""
    sheets: dict[str, list[dict[str, Any]]] = {p: [] for p in result.problem.listeners}
    for s in sorted(result.schedule.sessions, key=lambda x: (x.listener, x.slot)):
        stim = result.stimulus_sets[s.item_id]
        order = stim.order_for(s.first_render)
        sheets[s.listener].append({
            "slot": s.slot,
            "item_id": s.item_id,
            "headphone": s.headphone,
            "sequence": [{"anon_id": order[0], "play": "FIRST"},
                         {"anon_id": order[1], "play": "SECOND"}],
        })
    return sheets


def sealed_trace_report(result: TrialResult) -> dict[str, Any]:
    """封存追溯报告：持表人凭此把匿名号映射回源 render/片段/增益。"""
    out: dict[str, Any] = {}
    for iid, stim in sorted(result.stimulus_sets.items()):
        out[iid] = {
            "sum_sq": stim.combo.sum_sq,
            "differ_positions": stim.combo.differ_positions,
            "entries": [
                {"anon_id": t.anon_id, "side": t.side,
                 "source_render": t.source_render, "gain": t.gain,
                 "sample_digest": t.sample_digest}
                for t in stim.traces],
            "orders": {"AB": list(stim.order_ab), "BA": list(stim.order_ba)},
        }
    return out


def write_trial_bundle(result: TrialResult, out_dir: str) -> dict[str, str]:
    """落盘：每项公开实包/封存表 + 全局匿名播放单 + 排期摘要。"""
    os.makedirs(out_dir, exist_ok=True)
    paths: list[str] = []
    for iid, stim in result.stimulus_sets.items():
        paths.append(write_stimulus_bundle(stim, os.path.join(out_dir, "stimuli")))

    sheets_path = os.path.join(out_dir, "play_sheets.json")
    with open(sheets_path, "w", encoding="utf-8") as fh:
        json.dump(anonymous_play_sheets(result), fh, ensure_ascii=False, indent=2)

    sealed_path = os.path.join(out_dir, "sealed_trace_report.json")
    with open(sealed_path, "w", encoding="utf-8") as fh:
        json.dump(sealed_trace_report(result), fh, ensure_ascii=False, indent=2)

    summary_path = os.path.join(out_dir, "schedule_summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(schedule_summary(result), fh, ensure_ascii=False, indent=2)
    return {"play_sheets": sheets_path, "sealed_report": sealed_path,
            "summary": summary_path,
            "stimulus_dirs": [p["public_dir"] for p in paths]}


def schedule_summary(result: TrialResult) -> dict[str, Any]:
    sch = result.schedule
    return {
        "completed_items": sorted(sch.completed_items),
        "zero_items": result.zero_items(),
        "rejected_stimuli": result.rejected,
        "total_weight": sch.total_weight,
        "cross_person_items": sorted(sch.cross_person_items),
        "cross_person_count": len(sch.cross_person_items),
        "finish_slot": sch.finish_slot,
        "sessions": [
            {"item_id": s.item_id, "slot": s.slot, "listener": s.listener,
             "headphone": s.headphone, "order": s.order_key()}
            for s in sorted(sch.sessions, key=lambda x: (x.slot, x.headphone))],
    }
