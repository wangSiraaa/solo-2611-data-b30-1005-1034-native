"""双盲听审场次排期：每项两序（A后B / B后A）、换序洗耳、耳机台数约束。

硬约束（违反则该比较不得计为完成）：
1. 每项完成 = 两场，一场先 A 后 B、一场先 B 后 A，两序严格相反；
2. 每场占一听者、一台耳机、一个完整时段；同一 (听者, 时段) 至多一场，
   同一 (时段, 耳机) 至多一场；
3. 同一人听同一项的两场之间，至少隔一个完整时段不听任何节目
   （两场时段号之差 >= 2，且中间段此人无任何安排）；
4. 已知道本项任一正式匿名片真实原 render 身份的听者不得听本项；
5. 听者只在其空闲表给出的时段可用；
6. 每项两场必须落在馆方给的最晚结束段之前（含该段）。

优化目标（字典序，可复现）：
- 默认：最大完成权重和 -> 跨人项最少 -> 最早完场（最大时段号最小）；
- 额度模式：在给定跨人额度内，取相同最高权重下最早能完场的一案。
规模上限：8 项 / 6 人 / 8 段，回溯搜索可穷举。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

MAX_ITEMS = 8
MAX_LISTENERS = 6
MAX_SLOTS = 8


class ScheduleError(ValueError):
    pass


# ============================ 问题模型 ============================ #

@dataclass(frozen=True)
class Session:
    """一场播放：时段 slot、听者 listener、耳机台 headphone、先播源 first_render。"""
    item_id: str
    slot: int
    listener: str
    headphone: int
    first_render: str          # "A" -> 该场 A 后 B；"B" -> B 后 A

    def order_key(self) -> str:
        return "AB" if self.first_render == "A" else "BA"


@dataclass
class ItemSpec:
    item_id: str
    weight: int
    deadline: int                              # 最晚结束段（含）
    forbidden_listeners: frozenset[str]       # 已知身份、不得听本项的人
    feasible: bool                             # 刺激是否可制作（build_combos 非空）


@dataclass
class Problem:
    items: list[ItemSpec]
    listeners: list[str]
    n_slots: int
    headphone_capacity: list[int]              # 每段可用耳机台数
    availability: dict[str, frozenset[int]]    # 听者 -> 空闲时段集合
    cross_person_quota: int | None = None      # None = 默认三目标模式

    def validate(self) -> None:
        if not (1 <= len(self.items) <= MAX_ITEMS):
            raise ScheduleError(f"比较项数须在 1..{MAX_ITEMS}")
        if not (1 <= len(self.listeners) <= MAX_LISTENERS):
            raise ScheduleError(f"听者人数须在 1..{MAX_LISTENERS}")
        if not (1 <= self.n_slots <= MAX_SLOTS):
            raise ScheduleError(f"时段数须在 1..{MAX_SLOTS}")
        if len(self.headphone_capacity) != self.n_slots:
            raise ScheduleError("耳机台数表必须逐时段给出")
        if any(c < 1 for c in self.headphone_capacity):
            raise ScheduleError("每段至少要有一台耳机")
        ids = [it.item_id for it in self.items]
        if len(set(ids)) != len(ids):
            raise ScheduleError("比较项 ID 重复")
        if len(set(self.listeners)) != len(self.listeners):
            raise ScheduleError("听者 ID 重复")
        lset = set(self.listeners)
        for it in self.items:
            if it.weight < 0:
                raise ScheduleError(f"项 {it.item_id} 赞助权重为负")
            if not (1 <= it.deadline <= self.n_slots):
                raise ScheduleError(f"项 {it.item_id} 最晚结束段越界")
            unknown = it.forbidden_listeners - lset
            if unknown:
                raise ScheduleError(f"项 {it.item_id} 的回避名单含未知听者: {sorted(unknown)}")
        for person in self.listeners:
            free = self.availability.get(person, frozenset())
            bad = [s for s in free if not (1 <= s <= self.n_slots)]
            if bad:
                raise ScheduleError(f"听者 {person} 空闲表含越界时段 {bad}")
        if self.cross_person_quota is not None and self.cross_person_quota < 0:
            raise ScheduleError("跨人额度不能为负")


# ============================ 排期结果 ============================ #

@dataclass
class Schedule:
    sessions: list[Session]
    completed_items: frozenset[str]
    cross_person_items: frozenset[str]
    total_weight: int
    finish_slot: int                 # 完成项两场的最大时段号；无完成项为 0

    def sessions_of(self, item_id: str) -> list[Session]:
        return sorted((s for s in self.sessions if s.item_id == item_id),
                      key=lambda s: s.slot)

    def per_listener_playlist(self) -> dict[str, list[dict[str, Any]]]:
        """逐人逐时段匿名播放单（按听者、时段排序）。"""
        out: dict[str, list[dict[str, Any]]] = {p: [] for p in _listener_order(self)}
        for s in sorted(self.sessions, key=lambda x: (x.listener, x.slot, x.item_id)):
            out[s.listener].append({
                "slot": s.slot,
                "item_id": s.item_id,
                "headphone": s.headphone,
                "play_order": s.order_key(),       # 仅 AB/BA 序型，配合匿名号单使用
                "first_render_sealed": s.first_render,
            })
        return out


def _listener_order(sch: Schedule) -> list[str]:
    return sorted({s.listener for s in sch.sessions})


# ============================ 独立校验器 ============================ #

def verify_schedule(sch: Schedule, prob: Problem) -> list[str]:
    """逐条核对硬约束，返回错误说明列表（空列表 = 合法）。"""
    errors: list[str] = []
    by_item: dict[str, list[Session]] = {}
    person_slot: dict[tuple[str, int], Session] = {}
    slot_hp: dict[tuple[int, int], Session] = {}

    for s in sch.sessions:
        item = next((it for it in prob.items if it.item_id == s.item_id), None)
        if item is None:
            errors.append(f"场次引用未知比较项 {s.item_id}")
            continue
        if s.listener not in prob.listeners:
            errors.append(f"项 {s.item_id} 场次使用未知听者 {s.listener}")
            continue
        if s.slot not in prob.availability.get(s.listener, frozenset()):
            errors.append(f"听者 {s.listener} 在时段 {s.slot} 不空闲（项 {s.item_id}）")
        if s.listener in item.forbidden_listeners:
            errors.append(f"听者 {s.listener} 已知项 {s.item_id} 身份，按回避规则不得听")
        if not (1 <= s.slot <= prob.n_slots):
            errors.append(f"项 {s.item_id} 场次时段 {s.slot} 越界")
        elif not (1 <= s.headphone <= prob.headphone_capacity[s.slot - 1]):
            errors.append(f"时段 {s.slot} 无 {s.headphone} 号耳机台（项 {s.item_id}）")
        if s.first_render not in ("A", "B"):
            errors.append(f"项 {s.item_id} 场次序型非法: {s.first_render}")
        key1 = (s.listener, s.slot)
        if key1 in person_slot:
            errors.append(f"听者 {s.listener} 时段 {s.slot} 被两项抢占: "
                          f"{person_slot[key1].item_id} 与 {s.item_id}")
        else:
            person_slot[key1] = s
        key2 = (s.slot, s.headphone)
        if key2 in slot_hp:
            errors.append(f"时段 {s.slot} 耳机 {s.headphone} 被两项抢占: "
                          f"{slot_hp[key2].item_id} 与 {s.item_id}")
        else:
            slot_hp[key2] = s
        by_item.setdefault(s.item_id, []).append(s)

    specs = {it.item_id: it for it in prob.items}
    for item_id, sessions in by_item.items():
        if len(sessions) != 2:
            errors.append(f"项 {item_id} 有 {len(sessions)} 场，完成恰好需要两场")
            continue
        s_early, s_late = sorted(sessions, key=lambda x: x.slot)
        if {s.first_render for s in sessions} != {"A", "B"}:
            errors.append(f"项 {item_id} 两场不是一序 AB、一序 BA")
        if s_early.slot == s_late.slot and s_early.listener == s_late.listener:
            errors.append(f"项 {item_id} 同一听者 {s_early.listener} 两场挤在同一时段 "
                          f"{s_early.slot}（跨人并行使用不同耳机台不在此限）")
        if s_late.slot > specs[item_id].deadline:
            errors.append(f"项 {item_id} 完场时段 {s_late.slot} 晚于截止段 "
                          f"{specs[item_id].deadline}")
        same_person = s_early.listener == s_late.listener
        if same_person:
            gap = s_late.slot - s_early.slot
            if gap < 2:
                errors.append(f"项 {item_id} 同一人两场仅隔 {gap} 段，"
                              "须至少留一个完整洗耳时段")
            # 中间段此人不得有任何节目
            mid = [s2.slot for s2 in sch.sessions
                   if s2.listener == s_early.listener
                   and s_early.slot < s2.slot < s_late.slot]
            if mid:
                errors.append(f"项 {item_id} 洗耳空档被听者 {s_early.listener} 的"
                              f"其他场次占用: 时段 {mid}")
        else:
            if item_id not in sch.cross_person_items:
                errors.append(f"项 {item_id} 分两人听却未计入跨人")

    declared = set(sch.cross_person_items)
    actual = {iid for iid, ss in by_item.items()
              if len(ss) == 2 and len({s.listener for s in ss}) == 2}
    if declared != actual:
        errors.append(f"跨人项登记不符: 申报 {sorted(declared)} 实际 {sorted(actual)}")
    if set(sch.completed_items) != set(by_item):
        errors.append("完成项集合与实际两场齐备的项不一致")
    for iid in sch.completed_items:
        if not specs[iid].feasible:
            errors.append(f"项 {iid} 刺激无法制作，不得计为完成")
    return errors


# ============================ 回溯求解 ============================ #

@dataclass
class _Option:
    item_id: str
    sessions: tuple[Session, Session]
    cross: bool
    finish_slot: int


def _enumerate_options(prob: Problem, item: ItemSpec) -> list[_Option]:
    """枚举一项在截止段内全部合法的两场安排（含同一人/跨人、两种序型朝向）。"""
    eligible = [p for p in prob.listeners if p not in item.forbidden_listeners]
    slots = list(range(1, item.deadline + 1))
    options: list[_Option] = []
    # 同一人：s1 < s2 且 s2-s1>=2，中间段此人必须空闲（在可用性表内且无占用由搜索保证）
    for person in eligible:
        free = prob.availability.get(person, frozenset())
        for s1 in slots:
            if s1 not in free:
                continue
            for s2 in slots:
                if s2 <= s1 + 1 or s2 not in free:
                    continue
                if any(mid not in free for mid in range(s1 + 1, s2)):
                    continue
                for first in ("A", "B"):
                    sessions = (
                        Session(item.item_id, s1, person, 0, first),
                        Session(item.item_id, s2, person, 0,
                                "B" if first == "A" else "A"))
                    options.append(_Option(item.item_id, sessions, False, s2))
    # 跨人：两个不同听者各听一序，时段可任意（含同段并行，耳机容量由搜索把关）
    for p1 in eligible:
        for p2 in eligible:
            if p1 == p2:
                continue
            f1, f2 = prob.availability.get(p1, frozenset()), prob.availability.get(p2, frozenset())
            for s1 in slots:
                if s1 not in f1:
                    continue
                for s2 in slots:
                    if s2 not in f2:
                        continue
                    for first in ("A", "B"):
                        sessions = (
                            Session(item.item_id, s1, p1, 0, first),
                            Session(item.item_id, s2, p2, 0,
                                    "B" if first == "A" else "A"))
                        options.append(_Option(
                            item.item_id, sessions, True, max(s1, s2)))
    # 去重（同一人分支与跨人分支不可能重叠；此处按场次元组去重保稳定）
    seen: set[tuple] = set()
    unique: list[_Option] = []
    for opt in options:
        key = tuple((s.slot, s.listener, s.first_render) for s in opt.sessions)
        if key not in seen:
            seen.add(key)
            unique.append(opt)
    return unique


def solve(prob: Problem) -> Schedule:
    """求字典序最优排期；无法完成任何项时返回空 Schedule（不计完成）。"""
    prob.validate()
    options_by_item: dict[str, list[_Option]] = {}
    for item in prob.items:
        if item.feasible:
            options_by_item[item.item_id] = _enumerate_options(prob, item)

    weights = {it.item_id: it.weight for it in prob.items}
    order = sorted(prob.items, key=lambda it: (-it.weight, it.deadline, it.item_id))

    person_busy: dict[str, set[int]] = {p: set() for p in prob.listeners}
    # 洗耳预留：听者两场之间必须整段空闲，中段也登记为该听者不可占用
    washout: dict[str, set[int]] = {p: set() for p in prob.listeners}
    hp_used: list[set[int]] = [set() for _ in range(prob.n_slots)]
    chosen: list[_Option] = []
    best: dict[str, Any] = {"key": None, "chosen": []}

    def current_key() -> tuple:
        total_w = sum(weights[o.item_id] for o, _ in chosen)
        cross = sum(1 for o, _ in chosen if o.cross)
        finish = max((o.finish_slot for o, _ in chosen), default=0)
        if prob.cross_person_quota is not None:
            # 额度模式：权重 -> 完场早 -> 跨人少（跨人数受额度硬约束）
            return (-total_w, finish, cross)
        return (-total_w, cross, finish)

    def compatible(opt: _Option) -> bool:
        needed_hp: dict[int, int] = {}
        for s in opt.sessions:
            if s.slot in person_busy[s.listener] or s.slot in washout[s.listener]:
                return False
            needed_hp[s.slot] = needed_hp.get(s.slot, 0) + 1
        # 同一人方案：中间洗耳段此刻也必须无占用
        if not opt.cross:
            person = opt.sessions[0].listener
            s1, s2 = sorted(s.slot for s in opt.sessions)
            for mid in range(s1 + 1, s2):
                if mid in person_busy[person]:
                    return False
        for slot, need in needed_hp.items():
            cap = prob.headphone_capacity[slot - 1]
            if len(hp_used[slot - 1]) + need > cap:
                return False
        return True

    def apply(opt: _Option, undo: list[Any]) -> dict[int, int]:
        """登记占用并返回每场（按 opt.sessions 顺序）的耳机台号；不修改 opt。"""
        hp_map: dict[int, int] = {}
        for s in opt.sessions:
            person_busy[s.listener].add(s.slot)
            # 分配当前段最小编号的空闲耳机台
            cap = prob.headphone_capacity[s.slot - 1]
            hp = next(h for h in range(1, cap + 1) if h not in hp_used[s.slot - 1])
            hp_used[s.slot - 1].add(hp)
            undo.append(("busy", s.listener, s.slot, hp))
            hp_map[id(s)] = hp
        if not opt.cross:
            person = opt.sessions[0].listener
            s1, s2 = sorted(s.slot for s in opt.sessions)
            for mid in range(s1 + 1, s2):
                washout[person].add(mid)
                undo.append(("wash", person, mid))
        return hp_map

    def rollback(undo: list[Any]) -> None:
        for kind, person, slot, *rest in reversed(undo):
            if kind == "busy":
                hp = rest[0]
                person_busy[person].discard(slot)
                hp_used[slot - 1].discard(hp)
            else:
                washout[person].discard(slot)

    def dfs(idx: int) -> None:
        if idx == len(order):
            key = current_key()
            if best["key"] is None or key < best["key"]:
                best["key"] = key
                best["chosen"] = list(chosen)
            return
        item = order[idx]
        iid = item.item_id
        # 上界剪枝：剩余项全做也追不上当前最优权重则放弃
        remaining_weight = sum(weights[order[j].item_id] for j in range(idx, len(order)))
        cur_weight = sum(weights[o.item_id] for o, _ in chosen)
        if best["key"] is not None and cur_weight + remaining_weight < -best["key"][0]:
            return

        # 分支 1：不做本项（不可制作的项只有这一分支）
        dfs(idx + 1)

        # 分支 2：做本项，按“少跨人、早完场、占用小”的稳定顺序尝试
        if not item.feasible:
            return
        opts = sorted(options_by_item[iid],
                      key=lambda o: (o.cross, o.finish_slot,
                                     tuple((s.slot, s.listener, s.first_render)
                                           for s in o.sessions)))
        for opt in opts:
            if prob.cross_person_quota is not None:
                cross_so_far = sum(1 for o, _ in chosen if o.cross)
                if opt.cross and cross_so_far >= prob.cross_person_quota:
                    continue
            if not compatible(opt):
                continue
            undo: list[Any] = []
            hp_map = apply(opt, undo)
            chosen.append((opt, hp_map))
            dfs(idx + 1)
            chosen.pop()
            rollback(undo)

    dfs(0)
    return _build_schedule(best["chosen"], weights)


def _build_schedule(chosen: Sequence[tuple[_Option, dict[int, int]]],
                    weights: dict[str, int]) -> Schedule:
    sessions: list[Session] = []
    cross: set[str] = set()
    completed: set[str] = set()
    for opt, hp_map in chosen:
        for s in opt.sessions:
            hp = hp_map[id(s)]
            sessions.append(Session(s.item_id, s.slot, s.listener, hp,
                                    s.first_render))
        completed.add(opt.item_id)
        if opt.cross:
            cross.add(opt.item_id)
    sessions.sort(key=lambda s: (s.slot, s.headphone, s.item_id, s.first_render))
    return Schedule(
        sessions=sessions,
        completed_items=frozenset(completed),
        cross_person_items=frozenset(cross),
        total_weight=sum(weights[i] for i in completed),
        finish_slot=max((o.finish_slot for o, _ in chosen), default=0))
