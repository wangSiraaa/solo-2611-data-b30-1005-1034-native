"""声控接点检测与展台灯箱排班。

本模块只消费“真实混音后 PCM”（来自 audiomix 数据仓的逐块实际样本），
不读取任何外部设备，也不把目标亮点照抄成结果。

一、接点检测（ContactDetector）
    接点参数：整数启动阈 H、复位阈 L（0 <= L < H）、连续启动样本数 n_on、
    连续复位样本数 n_off。逐拍（= 输出样本序号）推进：
      * 初态“可启动”；
      * 未锁时累计连续 |x| >= H 的样本，达到 n_on 时在最后一拍发一个脉冲，随后上锁；
      * 上锁后累计连续 |x| <= L 的样本，达到 n_off 才复位（供后拍再触发）；
      * 中间值（L < |x| < H）既不计启动也不计复位，并打断未完成的连续段；
      * 跨渲染块边界不重置任何状态（由调用方逐块 feed，状态保留）。
    每个脉冲保留：来源（contact_id / render_id）、连续范围 [run_start, run_end]、
    触发值，以及锁定原因（LockEvent）。可对任意拍查询“为何仍被锁住”。

二、排班（plan_show）
    自动门把一只实际接点接到一盏灯，并给定一个连续闭区间；窗口内该接点的
    全部脉冲照传（空档可以保持接线）。因此一条接线合法的充要条件是：
    窗口内该接点的每一个脉冲，都恰好是该灯当拍的必亮点——否则会在禁亮拍误亮。
    同灯同拍最多真正点亮一次（多条接线命中同一点只算一次）。
    余下必亮点由当拍有空的值班员手按，一人一拍最多按一盏。
    在“可用自动门数量”约束下，求（未覆盖率, 手按次数, 用门数）字典序最小的
    走台单；无人能补的必亮点会在结果中显式列出，绝不漏报。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .engine import BLOCK_SAMPLES


# ============================== 接点检测 ============================== #

@dataclass(frozen=True)
class ContactSpec:
    """一只拾音接点的参数。"""
    contact_id: str
    render_id: str
    H: int          # 整数启动阈
    L: int          # 整数复位阈，0 <= L < H
    n_on: int       # 连续启动样本数
    n_off: int      # 连续复位样本数

    def __post_init__(self) -> None:
        if not (0 <= self.L < self.H):
            raise ValueError(
                f"接点 {self.contact_id}: 需 0 <= L < H，收到 L={self.L}, H={self.H}")
        if self.n_on < 1 or self.n_off < 1:
            raise ValueError(f"接点 {self.contact_id}: 连续样本数必须 >= 1")


@dataclass(frozen=True)
class Pulse:
    """一次触发。beat 是发脉冲的那一拍（连续段的最后一拍）。"""
    contact_id: str
    render_id: str
    beat: int
    run_start: int
    run_end: int
    value: int

    def to_json(self) -> dict[str, Any]:
        return {
            "contact_id": self.contact_id,
            "render_id": self.render_id,
            "beat": self.beat,
            "run_start": self.run_start,
            "run_end": self.run_end,
            "value": self.value,
        }


@dataclass(frozen=True)
class LockEvent:
    """一次上锁：原因 + 发脉冲的连续段；恢复于 unbeat（复位那拍，None 表示仍锁）。"""
    contact_id: str
    beat: int
    reason: str
    run_start: int
    run_end: int
    value: int
    unbeat: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "contact_id": self.contact_id,
            "beat": self.beat,
            "reason": self.reason,
            "run_start": self.run_start,
            "run_end": self.run_end,
            "value": self.value,
            "unbeat": self.unbeat,
        }


class ContactDetector:
    """逐样本状态机；可对真实 PCM 逐块 feed，状态跨块保留。"""

    def __init__(self, spec: ContactSpec):
        self.spec = spec
        self.locked = False
        self._on_count = 0
        self._on_start = 0
        self._off_count = 0
        self._off_start = 0
        self._last_value = 0
        self._next_index = 0          # 已处理到的下一个样本下标
        self.pulses: list[Pulse] = []
        self.locks: list[LockEvent] = []

    # ---- 核心状态机 ---- #
    def feed(self, samples: np.ndarray) -> None:
        """喂入一段连续样本（通常为一个渲染块）；不重置任何跨块状态。"""
        for raw in np.asarray(samples, dtype=np.int64):
            self._step(self._next_index, int(raw))
            self._next_index += 1

    def _step(self, n: int, v: int) -> None:
        self._last_value = v
        a = abs(v)
        if not self.locked:
            if a >= self.spec.H:
                if self._on_count == 0:
                    self._on_start = n
                self._on_count += 1
                if self._on_count >= self.spec.n_on:
                    run_end = n
                    run_start = run_end - self.spec.n_on + 1
                    self.pulses.append(Pulse(
                        self.spec.contact_id, self.spec.render_id,
                        run_end, run_start, run_end, v))
                    self.locks.append(LockEvent(
                        self.spec.contact_id, run_end,
                        f"|x|>={self.spec.H} 连续 {self.spec.n_on} 样本触发",
                        run_start, run_end, v))
                    self.locked = True
                    self._on_count = 0
                    self._off_count = 0
            else:
                self._on_count = 0      # 中间值或低值都会打断未完成的启动段
        else:
            if a <= self.spec.L:
                if self._off_count == 0:
                    self._off_start = n
                self._off_count += 1
                if self._off_count >= self.spec.n_off:
                    ev = self.locks[-1]
                    self.locks[-1] = LockEvent(
                        ev.contact_id, ev.beat, ev.reason, ev.run_start,
                        ev.run_end, ev.value, unbeat=n)
                    self.locked = False
                    self._off_count = 0
                    self._on_count = 0
            else:
                self._off_count = 0     # 中间值或高值都会打断未完成的复位段

    # ---- 查询 ---- #
    def pulse_beats(self) -> list[int]:
        return [p.beat for p in self.pulses]

    def n_scanned(self) -> int:
        return self._next_index

    def state_at(self, n: int) -> str:
        """返回判定第 n 拍之后的状态：'can-start' 或 'locked'。"""
        if n >= self._next_index:
            raise IndexError(f"拍 {n} 尚未检测到（已处理 {self._next_index} 拍）")
        return "locked" if self.lock_explanation(n) is not None else "can-start"

    def locked_at(self, n: int) -> bool:
        return self.state_at(n) == "locked"

    def lock_explanation(self, n: int) -> dict[str, Any] | None:
        """查询第 n 拍时若仍被锁住，返回导致该锁的脉冲连续段与原因。"""
        for ev in reversed(self.locks):
            if ev.beat <= n and (ev.unbeat is None or ev.unbeat > n):
                return ev.to_json()
        return None

    def result(self) -> dict[str, Any]:
        return {
            "contact": {
                "contact_id": self.spec.contact_id,
                "render_id": self.spec.render_id,
                "H": self.spec.H, "L": self.spec.L,
                "n_on": self.spec.n_on, "n_off": self.spec.n_off,
            },
            "pulses": [p.to_json() for p in self.pulses],
            "pulse_beats": self.pulse_beats(),
            "locks": [ev.to_json() for ev in self.locks],
            "final_state": "locked" if self.locked else "can-start",
            "samples_scanned": self._next_index,
        }


# ============================== 真实 PCM 检测 ============================== #

def detect_over_pcm(spec: ContactSpec, pcm: np.ndarray) -> ContactDetector:
    """对一整段真实 PCM 检测（内部仍逐块 feed，以证明跨块不重置）。"""
    det = ContactDetector(spec)
    n = len(pcm)
    for b0 in range(0, n, BLOCK_SAMPLES):
        det.feed(np.asarray(pcm[b0:b0 + BLOCK_SAMPLES], dtype=np.int64))
    return det


def detect_from_archive(store, render_id: str, H: int, L: int,
                        n_on: int = 1, n_off: int = 1,
                        contact_id: str = "c") -> ContactDetector:
    """从实际混音数据仓逐块读取 PCM 并检测。

    这是“根据真实声音重新点灯”的唯一数据入口：只用 render 的实际样本块，
    不读取时间轴意图，也不把目标亮点当作输入。
    """
    spec = ContactSpec(contact_id, render_id, int(H), int(L), int(n_on), int(n_off))
    det = ContactDetector(spec)
    meta = store.render_meta(render_id)
    n_samples = meta["n_samples"]
    for b0 in range(0, n_samples, BLOCK_SAMPLES):
        chunk = store.read_samples(render_id, b0, min(b0 + BLOCK_SAMPLES, n_samples))
        det.feed(chunk)
    return det


# ============================== 排班模型 ============================== #

@dataclass(frozen=True)
class Gate:
    """自动门：把一只接点接到一盏灯，在闭区间 [win_start, win_end] 内照传全部脉冲。"""
    gate_id: str
    contact_id: str
    light_id: str
    win_start: int
    win_end: int

    def in_window(self, beat: int) -> bool:
        return self.win_start <= beat <= self.win_end

    def to_json(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "contact_id": self.contact_id,
            "light_id": self.light_id,
            "window": [self.win_start, self.win_end],
        }


@dataclass
class PlanResult:
    """走台单：自动门接线、实际手按、未覆盖的必亮点、逐拍核对。"""
    required: list[tuple[str, int]]
    gates: list[Gate]
    gate_auto: list[dict[str, Any]]
    manual_presses: list[dict[str, Any]]
    uncovered: list[dict[str, Any]]
    beats: list[dict[str, Any]]
    manual_count: int
    gate_count: int

    def to_json(self) -> dict[str, Any]:
        return {
            "required": [{"light_id": l, "beat": b} for l, b in self.required],
            "gate_count": self.gate_count,
            "manual_count": self.manual_count,
            "gates": [g.to_json() for g in self.gates],
            "gate_auto": self.gate_auto,
            "manual_presses": self.manual_presses,
            "uncovered": self.uncovered,
            "beats": self.beats,
        }


@dataclass(frozen=True)
class _Beam:
    """一盏灯的一条合法接线候选：窗口内脉冲全部命中该灯必亮点，且非空。"""
    light_id: str
    contact_id: str
    win_start: int
    win_end: int
    beats: frozenset


def _candidate_beams(required: list[tuple[str, int]],
                     pulses_by_contact: Mapping[str, Iterable[int]],
                     contacts: Sequence[str]) -> list[_Beam]:
    """枚举全部合法接线候选（含子窗口）。

    一条接线 (contact, light, 窗口) 合法 <=> 窗口内该接点的每个脉冲都是该灯
    必亮点（否则会照传禁亮脉冲）。把某接点的脉冲按拍排序，找所有“连续一段都
    命中该灯必亮点”的子段 [p_i, p_j]，以 [p_i, p_j] 为窗口即得一条合法接线。
    子段（而非仅极大段）是必要的：同灯两门若在某一拍重叠，会重复点亮该拍；
    必要时改用互不重叠的子窗口即可各点亮各自部分。
    """
    req_by_light: dict[str, set[int]] = defaultdict(set)
    for light_id, beat in required:
        req_by_light[light_id].add(beat)

    beams: list[_Beam] = []
    for contact_id in contacts:
        beats = sorted(set(int(b) for b in pulses_by_contact.get(contact_id, ())))
        for light_id, req_set in req_by_light.items():
            # 在脉冲序列上切出所有“每拍都命中”的连续子段
            i = 0
            n = len(beats)
            while i < n:
                if beats[i] not in req_set:
                    i += 1
                    continue
                j = i
                while j + 1 < n and beats[j + 1] in req_set:
                    j += 1
                # [i, j] 是一个极大段；枚举其全部子段 [a, b]
                for a in range(i, j + 1):
                    for b in range(a, j + 1):
                        sub = frozenset(beats[a:b + 1])
                        beams.append(_Beam(light_id, contact_id,
                                           beats[a], beats[b], sub))
                i = j + 1
    return beams


def _manual_need(remaining: list[tuple[str, int]],
                 roster: Mapping[int, list[str]]) -> tuple[int, list[dict], list[dict]]:
    """按拍分配手按：一人一拍至多一盏；返回 (手按数, 手按明细, 无人能补明细)。"""
    by_beat: dict[int, list[str]] = defaultdict(list)
    for light_id, beat in remaining:
        by_beat[beat].append(light_id)
    manual: list[dict[str, Any]] = []
    uncovered: list[dict[str, Any]] = []
    for beat in sorted(by_beat):
        lights = sorted(by_beat[beat])
        duties = list(roster.get(beat, []))
        for i, light_id in enumerate(lights):
            if i < len(duties):
                manual.append({"light_id": light_id, "beat": beat,
                               "by": duties[i]})
            else:
                uncovered.append({
                    "light_id": light_id, "beat": beat,
                    "reason": "该拍无空闲值班员且无合法自动接线"})
    return len(manual), manual, uncovered


def _evaluate(required: list[tuple[str, int]],
              covered: set[tuple[str, int]],
              roster: Mapping[int, list[str]]) -> tuple[int, int, list, list]:
    """给定已覆盖点集，返回 (未覆盖数, 手按数, 手按明细, 未覆盖明细)。"""
    remaining = [pt for pt in required if pt not in covered]
    manual_count, manual, uncovered = _manual_need(remaining, roster)
    return len(uncovered), manual_count, manual, uncovered


def _light_options(light_id: str, req_beats: list[int],
                   beams: Sequence[_Beam]) -> dict[int, tuple[int, tuple[_Beam, ...]]]:
    """单盏灯：在不重叠前提下，求每个可达覆盖掩码所需的最少门数与接线组合。

    同灯同拍只许点亮一次，故两门不得在该灯的同一拍上重叠。返回
    {mask: (门数, 接线元组)}，mask 的位对应 req_beats 的下标。
    """
    bit = {b: i for i, b in enumerate(req_beats)}
    lb = [bm for bm in beams if bm.light_id == light_id]
    entries: list[tuple[_Beam, int]] = []
    for bm in lb:
        m = 0
        for b in bm.beats:
            m |= 1 << bit[b]
        if m:
            entries.append((bm, m))

    # 0/1 背包式 DP：{mask: (门数, 接线元组)}，每门至多用一次，且同灯不重叠
    dp: dict[int, tuple[int, tuple[_Beam, ...]]] = {0: (0, ())}
    for bm, m in entries:
        additions: dict[int, tuple[int, tuple[_Beam, ...]]] = {}
        for mask, (g, sel) in dp.items():
            if mask & m:
                continue                     # 同一灯同一拍重叠 -> 该组合非法
            nm = mask | m
            cand = (g + 1, sel + (bm,))
            prev = additions.get(nm)
            if prev is None or cand[0] < prev[0]:
                additions[nm] = cand
        for nm, cand in additions.items():
            prev = dp.get(nm)
            if prev is None or cand[0] < prev[0]:
                dp[nm] = cand
    return dp


def plan_show(required: Iterable[tuple[str, int]],
              pulses_by_contact: Mapping[str, Iterable[int]],
              gates_available: int,
              contacts: Sequence[str],
              roster: Mapping[int, Iterable[str]] | None = None,
              beats_total: int | None = None,
              candidate_beams: Sequence[_Beam] | None = None) -> PlanResult:
    """生成走台单。

    参数：
      required          逐灯必亮拍 [(light_id, beat), ...]（其余拍一律禁亮）
      pulses_by_contact 真实检测得到的每接点脉冲拍 {contact_id: [beat,...]}
      gates_available   可用自动门数量（每配一门整场最多用一次）
      contacts          可接的接点 id 列表
      roster            逐拍值班员 {beat: [name, ...]}
      beats_total       全场拍数（用于逐拍核对；默认可由数据推得）
      candidate_beams   可选：手工指定的接线候选（一般由 pulses 自动推导）

    目标按 (未覆盖数, 手按数, 用门数) 字典序最小。未覆盖=必亮点既无合法自动
    接线、当拍又无空闲值班员；它不会被悄悄丢掉，而是逐条列在 uncovered。
    """
    required = sorted(set((str(l), int(b)) for l, b in required))
    roster = {int(k): list(v) for k, v in (roster or {}).items()}
    contacts = list(contacts)
    gates_available = max(0, int(gates_available))

    beams = (list(candidate_beams) if candidate_beams is not None
             else _candidate_beams(required, pulses_by_contact, contacts))
    # 去重（同灯/接点/窗口）
    seen = set()
    uniq: list[_Beam] = []
    for bm in beams:
        sig = (bm.light_id, bm.contact_id, bm.win_start, bm.win_end)
        if sig not in seen:
            seen.add(sig)
            uniq.append(bm)
    beams = uniq

    req_by_light: dict[str, set[int]] = defaultdict(set)
    for light_id, beat in required:
        req_by_light[light_id].add(beat)
    # 每盏灯的必亮拍（排序），其掩码位序与之一致
    light_beats = {L: sorted(bs) for L, bs in req_by_light.items()}

    # ---- 每盏灯在“不重叠”下可达的覆盖掩码 -> 最少门数与接线组合 ----
    options: dict[str, dict[int, tuple[int, tuple[_Beam, ...]]]] = {}
    for L, bs in light_beats.items():
        options[L] = _light_options(L, bs, beams)

    # ---- 跨灯合并，门总数受 gates_available 限制 ----
    # 全局状态：{覆盖点集: (门数, 接线元组)}，逐灯卷积后保留各覆盖的最少门数
    global_states: dict[frozenset, tuple[int, tuple[_Beam, ...]]] = {
        frozenset(): (0, ())}
    for L, bs in light_beats.items():
        nxt: dict[frozenset, tuple[int, tuple[_Beam, ...]]] = {}
        for gmask, (g, sel) in global_states.items():
            for mask, (lg, lsel) in options[L].items():
                if g + lg > gates_available:
                    continue
                newcov = set(gmask)
                for i, b in enumerate(bs):
                    if mask >> i & 1:
                        newcov.add((L, b))
                key = frozenset(newcov)
                cand = (g + lg, sel + lsel)
                prev = nxt.get(key)
                if prev is None or cand[0] < prev[0]:
                    nxt[key] = cand
        global_states = nxt

    # ---- 选目标最优 ----
    best_key = None
    best_sel: tuple[_Beam, ...] = ()
    best_payload = None
    for cov, (g, sel) in global_states.items():
        unc, man, manual, uncovered = _evaluate(required, set(cov), roster)
        key = (unc, man, g)
        if best_key is None or key < best_key:
            best_key = key
            best_sel = sel
            best_payload = (manual, uncovered)
    assert best_key is not None
    _unc_count, manual_count, gate_count = best_key
    manual, uncovered = best_payload

    chosen = list(best_sel)

    # ---- 物化结果 ---- #
    gate_auto: list[dict[str, Any]] = []
    for idx, bm in enumerate(chosen):
        gate_auto.append({
            "gate_id": f"G{idx + 1}",
            "contact_id": bm.contact_id,
            "light_id": bm.light_id,
            "window": [bm.win_start, bm.win_end],
            "beats": sorted(bm.beats),
        })
    gates = [Gate(g["gate_id"], g["contact_id"], g["light_id"],
                  g["window"][0], g["window"][1]) for g in gate_auto]

    beats = _build_beat_table(required, gate_auto, manual, beats_total)

    return PlanResult(
        required=required,
        gates=gates,
        gate_auto=gate_auto,
        manual_presses=manual,
        uncovered=uncovered,
        beats=beats,
        manual_count=manual_count,
        gate_count=gate_count,
    )


def _build_beat_table(required, gate_auto, manual, beats_total) -> list[dict[str, Any]]:
    """逐拍对照：该拍每盏灯是否亮、由自动门还是手按负责；并标记禁亮违规。"""
    req_by_light: dict[str, set[int]] = defaultdict(set)
    for light_id, beat in required:
        req_by_light[light_id].add(beat)
    hi = beats_total
    if hi is None:
        all_beats = [b for _, b in required]
        for g in gate_auto:
            all_beats.extend(g["beats"])
        all_beats.extend(m["beat"] for m in manual)
        hi = (max(all_beats) + 1) if all_beats else 0

    auto_by_point: dict[tuple[str, int], list[str]] = defaultdict(list)
    for g in gate_auto:
        for b in g["beats"]:
            auto_by_point[(g["light_id"], b)].append(g["gate_id"])
    manual_by_point = {(m["light_id"], m["beat"]): m["by"] for m in manual}

    out = []
    for b in range(hi):
        row = {"beat": b, "lights_on": [], "manual": [], "auto": [],
               "forbidden_on": []}
        for light_id in sorted(req_by_light):
            key = (light_id, b)
            if key in auto_by_point:
                row["lights_on"].append(light_id)
                row["auto"].append({"light_id": light_id,
                                    "gates": auto_by_point[key]})
            elif key in manual_by_point:
                row["lights_on"].append(light_id)
                row["manual"].append({"light_id": light_id,
                                      "by": manual_by_point[key]})
            if light_id in row["lights_on"] and b not in req_by_light[light_id]:
                row["forbidden_on"].append(light_id)
        out.append(row)
    return out


def check_plan(plan: PlanResult,
               roster: Mapping[int, Iterable[str]] | None = None) -> dict[str, Any]:
    """独立核对走台单：必亮不漏、禁亮不犯、每门一窗、一人一拍至多一盏。"""
    required = set((l, b) for l, b in plan.required)
    roster = {int(k): list(v) for k, v in (roster or {}).items()}
    problems: list[str] = []

    lit: set[tuple[str, int]] = set()
    for row in plan.beats:
        for l in row["lights_on"]:
            lit.add((l, row["beat"]))
        if row["forbidden_on"]:
            problems.append(f"拍 {row['beat']} 禁亮灯被点亮: {row['forbidden_on']}")

    missing = required - lit
    if missing:
        problems.append(f"必亮点未覆盖: {sorted(missing)}")

    if len({g.gate_id for g in plan.gates}) != len(plan.gates):
        problems.append("自动门 gate_id 重复")
    for g in plan.gates:
        if g.win_end < g.win_start:
            problems.append(f"门 {g.gate_id} 窗口非法")

    seen: dict[tuple[int, str], int] = defaultdict(int)
    for m in plan.manual_presses:
        seen[(m["beat"], m["by"])] += 1
    for (beat, who), cnt in seen.items():
        if cnt > 1:
            problems.append(f"拍 {beat} 值班员 {who} 手按了 {cnt} 盏（应 <= 1）")
        if roster and who not in roster.get(beat, []):
            problems.append(f"拍 {beat} 值班员 {who} 不在当拍名单")

    if plan.uncovered:
        problems.append(f"存在无人能补的必亮点: {plan.uncovered}")

    for m in plan.manual_presses:
        if (m["light_id"], m["beat"]) not in required:
            problems.append(f"手按了非必亮点: {m}")

    return {"valid": not problems, "problems": problems,
            "required_count": len(required),
            "covered_count": len(required & lit),
            "manual_count": plan.manual_count,
            "gate_count": plan.gate_count,
            "uncovered_count": len(plan.uncovered)}
