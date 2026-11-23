"""装窑 × 装箱联合工单（烧前可兑现）。

搜索分两层，规模都被题面硬上限压住（最多 6 件、3 张付费承架、每款 ≤3 组
比例），因此全部用穷举，结论可逐条回放：

1. **装窑枚举** ``iter_kiln_assignments``：每件要么进自己的普通单件炉位
   （免费，一件一位），要么进一张它有资格坐的付费同炉承架；承架限同款、
   限指定件、限席数，空席允许，一件只占一次。
2. **套叠森林** ``best_nesting_forest``：在给定分炉下枚举所有满足"一件最多
   一个直接内件、不能成环"的套边，每条边必须对**所有允许的实际炉况**且在
   许可朝向下严格套入（擦边即不许）；链数 = 箱数，用带备忘的 DFS 求最少链。

同炉同款的两件锁同一组比例对；异炉（或不同款）比例独立。相邻套筒若共架，
检查的是同炉的相关比例；分炉比例独立——这层区别体现在
:func:`joint_kiln_conditions`。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from typing import Any, Iterator

from .errors import PlanningError
from .model import Piece, Rack, Scenario
from .voucher import CraftVoucher, FiredDims, fired_dims

# 每件相对箱体的朝向：as_is 横放（原 w/d），rotated 转九十度（w/d 互换）。
AS_IS = "as_is"
ROTATED = "rotated"


# ---------------------------------------------------------------------------
# 装窑分炉
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KilnGroup:
    """一个实际炉次：要么是普通单件炉位，要么是一张付费承架。"""

    group_id: str
    style_id: str
    rack_id: str | None          # None => 普通单件炉位（免费）
    members: frozenset[str]

    @property
    def is_rack(self) -> bool:
        return self.rack_id is not None


@dataclass(frozen=True)
class RackUse:
    rack_id: str
    seats_used: tuple[str, ...]
    fee: int


@dataclass(frozen=True)
class KilnAssignment:
    groups: tuple[KilnGroup, ...]
    rack_uses: tuple[RackUse, ...]
    group_of: dict[str, str]
    total_fee: int

    def group(self, piece_id: str) -> KilnGroup:
        return next(g for g in self.groups if piece_id in g.members)


def _eligible_racks(scenario: Scenario, piece: Piece) -> tuple[Rack, ...]:
    return tuple(r for r in scenario.racks if r.admits(piece))


def iter_kiln_assignments(scenario: Scenario) -> Iterator[KilnAssignment]:
    """枚举全部合法装窑法（含零承架的普通分炉）。

    不同选择顺序可能落出同一个"承架占用划分"（空席允许），这里按最终划分
    去重，保证每张实际承架方案只出现一次。
    """
    pieces = scenario.pieces
    rack_list = scenario.racks

    # choices[piece_id] = None（普通炉位）或 rack_id
    choices: dict[str, str | None] = {}
    # 同一"件 -> 炉位"最终划分只产出一次（不同选择顺序可能殊途同归）。
    seen_partitions: set[tuple[tuple[str, str | None], ...]] = set()

    def rec(index: int, occupancy: dict[str, list[str]]) -> Iterator[KilnAssignment]:
        if index == len(pieces):
            partition = tuple(
                sorted((pid, choices[pid]) for pid in choices)
            )
            if partition in seen_partitions:
                return
            seen_partitions.add(partition)
            yield _build_assignment(scenario, choices, occupancy)
            return
        piece = pieces[index]
        # 先放普通单件炉位，再试各张有资格的承架，保证输出顺序确定。
        options: list[str | None] = [None]
        for rack in _eligible_racks(scenario, piece):
            if len(occupancy.get(rack.rack_id, ())) < rack.seats:
                options.append(rack.rack_id)
        for choice in options:
            choices[piece.piece_id] = choice
            if choice is not None:
                occupancy.setdefault(choice, []).append(piece.piece_id)
            yield from rec(index + 1, occupancy)
            if choice is not None:
                occupancy[choice].remove(piece.piece_id)
                if not occupancy[choice]:
                    del occupancy[choice]

    yield from rec(0, {})


def _build_assignment(
    scenario: Scenario, choices: dict[str, str | None], occupancy: dict[str, list[str]]
) -> KilnAssignment:
    groups: list[KilnGroup] = []
    rack_uses: list[RackUse] = []
    group_of: dict[str, str] = {}
    solo_serial = 0

    for rack in scenario.racks:
        seated = tuple(sorted(occupancy.get(rack.rack_id, ())))
        if not seated:
            continue
        gid = f"kiln-rack-{rack.rack_id}"
        groups.append(KilnGroup(gid, rack.style_id, rack.rack_id, frozenset(seated)))
        rack_uses.append(RackUse(rack.rack_id, seated, rack.fee))
        for pid in seated:
            group_of[pid] = gid

    for piece in scenario.pieces:
        if choices[piece.piece_id] is None:
            gid = f"kiln-solo-{solo_serial}"
            solo_serial += 1
            groups.append(KilnGroup(gid, piece.style_id, None,
                                    frozenset({piece.piece_id})))
            group_of[piece.piece_id] = gid

    total_fee = sum(use.fee for use in rack_uses)
    # 稳定排序：承架炉次在前，再按 group_id。
    groups.sort(key=lambda g: (not g.is_rack, g.group_id))
    rack_uses.sort(key=lambda u: u.rack_id)
    return KilnAssignment(tuple(groups), tuple(rack_uses), group_of, total_fee)


# ---------------------------------------------------------------------------
# 炉况（允许的实际比例组合）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KilnCondition:
    """一个允许的实际炉况：内件取 inner_pair 组、外件取 outer_pair 组。

    同炉时两者必为同一组；异炉时两槽独立。显式分槽，调用方无需按炉号反查。
    """

    label: str
    same_kiln: bool
    inner_pair: int
    outer_pair: int


def joint_kiln_conditions(
    scenario: Scenario,
    voucher: CraftVoucher,
    assignment: KilnAssignment,
    inner_id: str,
    outer_id: str,
) -> list[KilnCondition]:
    """两件相关的全部允许炉况。

    * 同炉（共架，且同款——承架本身限同款）：只枚举该炉次两件允许组交集中的
      同一组比例，横纵被同一组锁死；
    * 异炉：两件各自允许组做笛卡尔积，比例独立。
    """
    g_inner = assignment.group(inner_id)
    g_outer = assignment.group(outer_id)
    if g_inner.group_id == g_outer.group_id:
        style_id = g_inner.style_id
        ia = voucher.allowed_pairs_for_piece(inner_id, style_id)
        ib = voucher.allowed_pairs_for_piece(outer_id, style_id)
        return [
            KilnCondition(g_inner.group_id, True, pair_index, pair_index)
            for pair_index in sorted(set(ia) & set(ib))
        ]

    ia = voucher.allowed_pairs_for_piece(
        inner_id, scenario.piece_by_id()[inner_id].style_id
    )
    ib = voucher.allowed_pairs_for_piece(
        outer_id, scenario.piece_by_id()[outer_id].style_id
    )
    label = f"{g_inner.group_id}|{g_outer.group_id}"
    return [
        KilnCondition(label, False, x, y)
        for x in ia
        for y in ib
    ]


# ---------------------------------------------------------------------------
# 朝向与严格套入
# ---------------------------------------------------------------------------

def oriented_dims(dims: FiredDims, orientation: str) -> tuple[Fraction, Fraction, Fraction, Fraction]:
    if orientation == AS_IS:
        return dims.outer_w, dims.outer_d, dims.inner_w, dims.inner_d
    return dims.outer_d, dims.outer_w, dims.inner_d, dims.inner_w


def orientations_for(piece: Piece) -> tuple[str, ...]:
    return (AS_IS, ROTATED) if piece.allow_rotation else (AS_IS,)


@dataclass(frozen=True)
class NestEdge:
    inner_id: str
    outer_id: str
    inner_orientation: str
    outer_orientation: str
    all_orientation_options: tuple[tuple[str, str], ...]
    same_kiln: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "inner_piece": self.inner_id,
            "outer_piece": self.outer_id,
            "inner_orientation": self.inner_orientation,
            "outer_orientation": self.outer_orientation,
            "all_orientation_options": [list(o) for o in self.all_orientation_options],
            "same_kiln": self.same_kiln,
        }


def _orientation_fits(
    scenario: Scenario,
    voucher: CraftVoucher,
    assignment: KilnAssignment,
    inner_id: str,
    outer_id: str,
    inner_ori: str,
    outer_ori: str,
    conditions: list[KilnCondition],
) -> bool:
    """该朝向组合是否对**每一个**允许炉况都严格套入。"""
    pieces = scenario.piece_by_id()
    styles = scenario.style_by_id()
    inner, outer = pieces[inner_id], pieces[outer_id]
    for condition in conditions:
        d_inner = oriented_dims(
            fired_dims(inner, styles[inner.style_id], condition.inner_pair), inner_ori
        )
        d_outer = oriented_dims(
            fired_dims(outer, styles[outer.style_id], condition.outer_pair), outer_ori
        )
        in_ow, in_od, _iw, _id = d_inner
        _ow, _od, out_iw, out_id = d_outer
        # 内件烧后外宽/外深必须分别严格小于外件同向烧后内宽/内深。
        if not (in_ow < out_iw and in_od < out_id):
            return False
    return True


def evaluate_edge(
    scenario: Scenario,
    voucher: CraftVoucher,
    assignment: KilnAssignment,
    inner_id: str,
    outer_id: str,
) -> NestEdge | None:
    """返回对所有允许炉况都合格的套边（含全部可行朝向）；不合格返回 None。"""
    pieces = scenario.piece_by_id()
    inner, outer = pieces[inner_id], pieces[outer_id]
    conditions = joint_kiln_conditions(scenario, voucher, assignment, inner_id, outer_id)
    options: list[tuple[str, str]] = []
    for inner_ori in orientations_for(inner):
        for outer_ori in orientations_for(outer):
            if _orientation_fits(scenario, voucher, assignment, inner_id, outer_id,
                                 inner_ori, outer_ori, conditions):
                options.append((inner_ori, outer_ori))
    if not options:
        return None
    same_kiln = assignment.group(inner_id).group_id == assignment.group(outer_id).group_id
    # 默认取最简单的朝向（能不转就不转），其余选项保留备查。
    chosen = options[0]
    return NestEdge(inner_id, outer_id, chosen[0], chosen[1], tuple(options), same_kiln)


def axiswise_worst_check(
    scenario: Scenario,
    voucher: CraftVoucher,
    inner_id: str,
    outer_id: str,
) -> bool:
    """**故意错误**的对照算法：横、纵各自独立取最坏边界。

    当两件分炉且存活多组比例时，横向最坏可能来自比例组合 A，纵向最坏来自
    组合 B，而 A、B 拼起来根本不是任何一炉能实现的炉况。本函数把这种虚构
    炉况当真，只用于测试与凭单里对照，工单从不使用它。
    """
    pieces = scenario.piece_by_id()
    styles = scenario.style_by_id()
    inner, outer = pieces[inner_id], pieces[outer_id]
    inner_pairs = voucher.allowed_pairs_for_piece(inner_id, inner.style_id)
    outer_pairs = voucher.allowed_pairs_for_piece(outer_id, outer.style_id)
    if not inner_pairs or not outer_pairs:
        return False
    worst_in_w = worst_in_d = None
    worst_out_iw = worst_out_id = None
    for ia in inner_pairs:
        di = fired_dims(inner, styles[inner.style_id], ia)
        worst_in_w = di.outer_w if worst_in_w is None else max(worst_in_w, di.outer_w)
        worst_in_d = di.outer_d if worst_in_d is None else max(worst_in_d, di.outer_d)
    for ib in outer_pairs:
        do = fired_dims(outer, styles[outer.style_id], ib)
        worst_out_iw = do.inner_w if worst_out_iw is None else min(worst_out_iw, do.inner_w)
        worst_out_id = do.inner_d if worst_out_id is None else min(worst_out_id, do.inner_d)
    return worst_in_w < worst_out_iw and worst_in_d < worst_out_id


# ---------------------------------------------------------------------------
# 最少套叠链（= 最少运输箱）
# ---------------------------------------------------------------------------

def _edge_candidates(
    scenario: Scenario,
    voucher: CraftVoucher,
    assignment: KilnAssignment,
) -> dict[tuple[str, str], NestEdge]:
    edges: dict[tuple[str, str], NestEdge] = {}
    ids = [p.piece_id for p in scenario.pieces]
    for inner_id in ids:
        for outer_id in ids:
            if inner_id == outer_id:
                continue
            edge = evaluate_edge(scenario, voucher, assignment, inner_id, outer_id)
            if edge is not None:
                edges[(inner_id, outer_id)] = edge
    return edges


def best_nesting_forest(
    scenario: Scenario,
    voucher: CraftVoucher,
    assignment: KilnAssignment,
) -> tuple[list[NestEdge], list[list[str]]]:
    """求最少套叠链；返回 (套边, 每条链从内到外的件号序列)。

    一件最多一个直接内件、最多一个直接外件（森林），自然无环。用位掩码备忘
    的 DFS 求最大边数，边按字典序尝试，输出确定。
    """
    edge_map = _edge_candidates(scenario, voucher, assignment)
    ids = tuple(p.piece_id for p in scenario.pieces)
    n = len(ids)
    index = {pid: i for i, pid in enumerate(ids)}

    # 按 (内件, 外件) 字典序排列，保证选边确定。
    ordered = sorted(edge_map)

    @lru_cache(maxsize=None)
    def best(mask: int, used_as_inner: int, used_as_outer: int) -> int:
        best_count = 0
        for inner_id, outer_id in ordered:
            i, o = index[inner_id], index[outer_id]
            bit_i, bit_o = 1 << i, 1 << o
            if not (mask & bit_i) or not (mask & bit_o):
                continue
            if used_as_inner & bit_i or used_as_outer & bit_o:
                continue
            # mask 内的森林加一条 inner->outer 边不会成环：inner 在当前
            # 子森林里没有外件（bit_i 不在 used_as_outer），而 outer 没有
            # 内件；任何环都要求某节点同时有两条同向链入边，度约束已排除。
            gained = 1 + best(
                mask,
                used_as_inner | bit_i,
                used_as_outer | bit_o,
            )
            if gained > best_count:
                best_count = gained
        return best_count

    full = (1 << n) - 1
    target = best(full, 0, 0)

    # 回放最优边集（ordered 已按字典序排列，等价最优时输出确定）。
    chosen_edges: list[NestEdge] = []
    u_inner, u_outer = 0, 0
    remaining = target
    while remaining > 0:
        progressed = False
        for inner_id, outer_id in ordered:
            i, o = index[inner_id], index[outer_id]
            bit_i, bit_o = 1 << i, 1 << o
            if u_inner & bit_i or u_outer & bit_o:
                continue
            # 选这条边后，剩余子问题还能拿到 remaining-1 条边 => 它属于某最优解。
            if best(full, u_inner | bit_i, u_outer | bit_o) == remaining - 1:
                chosen_edges.append(edge_map[(inner_id, outer_id)])
                u_inner |= bit_i
                u_outer |= bit_o
                remaining -= 1
                progressed = True
                break
        if not progressed:  # pragma: no cover - 与 best() 同源，不会发生
            raise PlanningError("套叠森林回放失败")
    best.cache_clear()
    return chosen_edges, _chains_from_edges(ids, chosen_edges)


def _chains_from_edges(
    ids: tuple[str, ...], edges: list[NestEdge]
) -> list[list[str]]:
    # inner -> outer；outer -> inner。链根 = 最内件（从不作外件的件）。
    inner_map: dict[str, str] = {e.inner_id: e.outer_id for e in edges}
    is_outer = {e.outer_id for e in edges}
    chains: list[list[str]] = []
    for pid in ids:
        if pid in is_outer:
            continue  # 不是最内链根
        chain = [pid]
        cur = pid
        while cur in inner_map:
            cur = inner_map[cur]
            chain.append(cur)
        chains.append(chain)
    chains.sort(key=lambda chain: chain[0])
    return chains


# ---------------------------------------------------------------------------
# 工单
# ---------------------------------------------------------------------------

@dataclass
class WorkOrder:
    scenario_id: str
    mode: str
    assignment: KilnAssignment
    edges: list[NestEdge]
    chains: list[list[str]]
    box_count: int
    rack_fee: int
    box_cap: int | None
    fee_budget: int | None
    feasible: bool
    note: str
    voucher: CraftVoucher

    def to_json(self) -> dict[str, Any]:
        return {
            "format": "work-order/v1",
            "scenario_id": self.scenario_id,
            "mode": self.mode,
            "feasible": self.feasible,
            "note": self.note,
            "objective": {
                "box_cap": self.box_cap,
                "fee_budget": self.fee_budget,
            },
            "kiln_plan": {
                "groups": [
                    {
                        "group_id": g.group_id,
                        "style_id": g.style_id,
                        "rack_id": g.rack_id,
                        "members": sorted(g.members),
                        "kind": "paid_rack" if g.is_rack else "solo_slot",
                    }
                    for g in self.assignment.groups
                ],
                "racks_used": [
                    {"rack_id": u.rack_id, "seats_used": list(u.seats_used),
                     "seats_count": len(u.seats_used), "fee": u.fee}
                    for u in self.assignment.rack_uses
                ],
                "total_rack_fee": self.rack_fee,
            },
            "packing_plan": {
                "boxes": [
                    {"box_index": i + 1, "chain_inner_to_outer": chain}
                    for i, chain in enumerate(self.chains)
                ],
                "box_count": self.box_count,
                "nest_edges": [e.to_json() for e in self.edges],
            },
        }


def _assignment_signature(assignment: KilnAssignment, chains: list[list[str]]) -> tuple:
    return (
        tuple(sorted((u.rack_id, u.seats_used) for u in assignment.rack_uses)),
        tuple(tuple(chain) for chain in chains),
    )


def plan_work_order(
    scenario: Scenario,
    voucher: CraftVoucher,
) -> WorkOrder:
    """按情景目标出一份装窑+装箱联合工单；找不到合格固定套法即报无解。"""
    voucher.raise_if_rejected()
    obj = scenario.objective

    candidates: list[tuple[int, int, KilnAssignment, list[NestEdge], list[list[str]]]] = []
    for assignment in iter_kiln_assignments(scenario):
        if obj.fee_budget is not None and assignment.total_fee > obj.fee_budget:
            continue
        edges, chains = best_nesting_forest(scenario, voucher, assignment)
        box_count = len(chains)
        if obj.mode == "min_fee" and obj.box_cap is not None and box_count > obj.box_cap:
            continue
        candidates.append((box_count, assignment.total_fee, assignment, edges, chains))

    if not candidates:
        raise PlanningError(
            "不存在对所有允许炉况都合格的固定套法"
            + (f"（箱位上限 {obj.box_cap}）" if obj.box_cap else "")
        )

    if obj.mode == "min_fee":
        # 先满足箱位上限，再把架费压到最低；架费相同则少箱、再字典序。
        candidates.sort(key=lambda c: (c[1], c[0],
                                       _signature_tuple(c[2], c[4])))
    else:
        # 最少箱数，再省架费，再按确定签名。
        candidates.sort(key=lambda c: (c[0], c[1],
                                       _signature_tuple(c[2], c[4])))

    box_count, fee, assignment, edges, chains = candidates[0]
    note = (
        f"最少 {box_count} 箱、架费 {fee}" if obj.mode == "min_boxes"
        else f"箱位上限 {obj.box_cap} 内最低架费 {fee}，实际 {box_count} 箱"
    )
    return WorkOrder(
        scenario_id=scenario.scenario_id,
        mode=obj.mode,
        assignment=assignment,
        edges=edges,
        chains=chains,
        box_count=box_count,
        rack_fee=fee,
        box_cap=obj.box_cap,
        fee_budget=obj.fee_budget,
        feasible=True,
        note=note,
        voucher=voucher,
    )


def _signature_tuple(assignment: KilnAssignment, chains: list[list[str]]) -> tuple:
    sig = _assignment_signature(assignment, chains)
    return tuple(str(x) for x in sig)
