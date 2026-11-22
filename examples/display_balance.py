"""End-to-end display-balance run on top of the real catalog and publisher.

The whole chain uses the production slabwater artifacts:

1. the :class:`~slabwater.catalog.Catalog` freezes per-cell ground heights ``h``
   (two frozen slices -- the second raises the right cell),
2. the :class:`~slabwater.publisher.Publisher` publishes a real no-outlet water
   plan per slice (no opened outlet => every cell's level is the ceiling, so the
   plan's per-cell levels give the frozen water ceiling ``l``), and each plan is
   re-opened by id,
3. :class:`~slabwater.tray.Pan` calibrates the two model trays from those
   frozen h/l values and answers water<->mark-line/moment queries exactly,
4. :func:`~slabwater.balance.plan_balance` produces the minimum-cost loading
   sheet for all three scenarios from the task statement.

Run with::

    python3 -m examples.display_balance
"""

from __future__ import annotations

import tempfile
from fractions import Fraction
from pathlib import Path

from slabwater import Catalog, Publisher
from slabwater.balance import InfeasibleBalance, Weight, plan_balance
from slabwater.tray import Pan


def f(x) -> Fraction:
    return x if isinstance(x, Fraction) else Fraction(x)


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_pan(pan: Pan) -> None:
    print(f"\n浅盘 {pan.name}（计划 {pan.plan_id[:10]}…，切面 v{pan.catalog_version}，"
          f"哈希 {pan.elevation_hash[:10]}…）")
    print(f"  刻线合法范围 z ∈ [{pan.z_min}, {pan.z_max}]，满盘容量 W* = {pan.capacity}，"
          f"转折数 {len(pan.kinks) - 1}（≤8）")
    print("  格来源：")
    for c in pan.cells:
        src = ", ".join(f"{s.tile_id}@r{s.tile_revision}" for s in c.sources)
        print(f"    格{c.cell}：面积={c.area} 有向力臂={c.arm} 地面 h={c.ground} "
              f"水位上限 l={c.level_ceiling} 来源=[{src}]")
    print("  精确 水量—力矩 关系（按刻线段）：")
    for row in pan.response_table():
        tag = "  [平台 dW=0]" if row["plateau"] else ""
        print(f"    z∈{row['z']}  W∈{row['water']}  T∈{row['moment']}  "
              f"吃水格={row['active_cells']}{tag}")


def show_plan(title: str, plan) -> None:
    banner(title)
    print(f"目标总力矩 = {plan.target_moment}")
    print("最终各盘：")
    for t in plan.trays:
        z = f"[{t.z_lo}, {t.z_hi}]" if t.z_lo != t.z_hi else str(t.z_lo)
        print(f"  {t.name}: 起 {t.start} → 终 {t.water}（容量内），刻线 z={z}，"
              f"盘力矩={t.moment}（第 {t.segment_index} 段）")
    if plan.transfers:
        print("移水（每份水只计费一次）：")
        for a, b, x in plan.transfers:
            print(f"  {x} 单位：{a} → {b}")
    else:
        print("移水：无")
    print(f"移水费 = {plan.transfer_cost}")
    if plan.hung:
        print("挂码：" + "，".join(f"{w.label}(力矩 {w.moment}, 挂费 {w.fee})" for w in plan.hung))
    else:
        print("挂码：不挂")
    print(f"挂码费 = {plan.weight_fee}")
    print(f"★ 最低总费用 = {plan.total_cost}")
    print("两侧力矩验算：")
    print(f"  正向力矩合计 = {plan.positive_moment}；负向力矩合计 = {plan.negative_moment}；"
          f"含砝码净力矩 = {plan.net_moment}（应等于目标 {plan.target_moment}）")
    assert plan.net_moment == plan.target_moment


def build_world(tmp: Path):
    catalog = Catalog(tmp / "catalog.db", height=1, width=3, ceiling=3)
    publisher = Publisher(tmp / "plans.db", catalog)

    # 切面 v1：地面 [0, 0, 0]（平坦，右边界开口才能真正把门槛传遍全表面）；
    # 切面 v2：只把中格抬到 2（新块修订，旧切面不动）。展柜用左、中两格成盘，
    # 右格是出口通道；“高台导致突然多一格吃水”由浅盘层的教学盘演示。
    r1 = catalog.apply_batch(
        [{"tile_id": "ground", "revision": 1, "row": 0, "col": 0, "values": [[0, 0, 0]]}]
    )
    assert r1.ok and r1.version == 1
    r2 = catalog.apply_batch(
        [{"tile_id": "ground", "revision": 2, "row": 0, "col": 0, "values": [[0, 2, 0]]}]
    )
    assert r2.ok and r2.version == 2

    # “无开口”：候选出口存在但 opens=0，求解器只枚举空集，水位全部顶到 C=3。
    # 放宽 total/cell 上限使该真实计划必然可发布，逐格 l 由发布单冻结。
    outlet = [{"id": "E", "cell": [0, 2], "sill": 0}]
    req1 = {
        "id": "REQ-SLICE1", "source_revision": 1, "opens": 0,
        "total_max": 99, "cell_max": [], "outlets": outlet,
    }
    req2 = {**req1, "id": "REQ-SLICE2", "source_revision": 2}
    pub1 = publisher.publish(req1)
    pub2 = publisher.publish(req2)
    assert pub1.ok and pub2.ok
    # 幂等复读与重开：拿到的是同一份冻结工件。
    assert publisher.publish(req1).plan_id == pub1.plan_id
    reopened1 = publisher.reopen(pub1.plan_id).artifact
    assert reopened1["levels"] == pub1.artifact["levels"]

    # 额外的真实发布单：右边界开口的门槛经 minimax 传遍平坦表面，逐格冻结 l。
    # 注意 opens 只是上限（空集也合法），所以把 total_max 收紧到空集蓄水 9
    # 必然超标，求解器只能选开口方案，门槛水位才真正出现在发布单里。
    # sill=1 → l=1；sill=2 → l=2。
    req_lcap = {
        "id": "REQ-OPEN-SILL1", "source_revision": 1, "opens": 1,
        "total_max": 3, "cell_max": [],
        "outlets": [{"id": "R", "cell": [0, 2], "sill": 1}],
    }
    req_dry = {
        "id": "REQ-OPEN-SILL2", "source_revision": 1, "opens": 1,
        "total_max": 6, "cell_max": [],
        "outlets": [{"id": "R", "cell": [0, 2], "sill": 2}],
    }
    # 留一份真实拒绝证据：total_max=4 时空集蓄水 9 超标，sill1 方案蓄水 3
    # 可行——因此改用 sill3 候选（水位仍顶到 3、蓄水 9）使两个集合都超标。
    req_reject = {
        "id": "REQ-REJECTED", "source_revision": 1, "opens": 1,
        "total_max": 4, "cell_max": [],
        "outlets": [{"id": "R", "cell": [0, 2], "sill": 3}],
    }
    pub_lcap = publisher.publish(req_lcap)
    pub_dry = publisher.publish(req_dry)
    rejected = publisher.publish(req_reject)
    assert pub_lcap.ok and pub_dry.ok and not rejected.ok
    art_sill1 = publisher.reopen(pub_lcap.plan_id).artifact
    art_sill2 = publisher.reopen(pub_dry.plan_id).artifact
    assert art_sill1["levels"] == [[1, 1, 1]]
    assert art_sill2["levels"] == [[2, 2, 2]]
    assert rejected.solve.exhaustive
    return (
        catalog,
        publisher,
        pub1.artifact,
        pub2.artifact,
        pub_lcap.artifact,
        pub_dry.artifact,
        rejected,
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        catalog, publisher, plan1, plan2, plan_lcap, plan_dry, rejected = build_world(tmp)

        banner("发布单（真实 Publisher 工件，逐格冻结水位上限 l）")
        for art in (plan1, plan2, plan_lcap, plan_dry):
            print(f"  {art['plan_id']}: 请求 {art['request_id']}，切面 v{art['catalog_version']}，"
                  f"开启出口={art['opened']}，逐格水位={art['levels']}，"
                  f"逐格蓄水={art['storage']}")
        print(f"  拒绝证据：请求 {rejected.request_id} 状态={rejected.status}，"
              f"穷尽={rejected.solve.exhaustive}，尝试集合数={rejected.solve.candidate_subsets}")

        surface1 = catalog.get_slice(1)
        surface2 = catalog.get_slice(2)

        # 两格各自成盘：左臂 0、右臂 2；面积均为 1。
        left = Pan.from_plan("左盘", surface1, plan1, [((0, 0), 0)])
        right = Pan.from_plan("右盘", surface1, plan1, [((0, 1), 2)])
        show_pan(left)
        show_pan(right)

        weight_m2 = Weight("W+2", moment=Fraction(2), fee=Fraction(2))

        # 场景一：原切面，左起 3 右起 0，目标力矩 4。
        plan_a = plan_balance([left, right], [3, 0], [weight_m2], target_moment=4)
        show_plan("场景一：原切面 h=[0,0,0]，展柜两盘起始 (3,0)，目标力矩 4，挂费 2", plan_a)
        assert plan_a.total_cost == 2 and not plan_a.hung
        assert (plan_a.trays[0].water, plan_a.trays[1].water) == (1, 2)

        # 同一份总水量 3，分配不同则力矩完全不同 —— 只固定总量决定不了哪边重。
        print("同总量 3 的不同分配（左臂0、右臂2）：")
        for wl, wr in ((3, 0), (1, 2), (0, 3)):
            tl = left.moment_at(left.lookup_water(Fraction(wl)).z_lo)
            tr = right.moment_at(right.lookup_water(Fraction(wr)).z_lo)
            print(f"  左 {wl} / 右 {wr} → 力矩 {tl + tr}（左 {tl}，右 {tr}）")

        # 场景二：右格地面抬到 2，新申请号在新切面重发同样的“无开口”要求。
        # 旧盘标定保持原样；新右盘容量变为 1。
        left_old = Pan.from_plan("左盘(旧标定仍有效)", surface1, plan1, [((0, 0), 0)])
        right2 = Pan.from_plan("右盘(新切面)", surface2, plan2, [((0, 1), 2)])
        show_pan(right2)
        assert left_old.elevation_hash == left.elevation_hash  # 旧标定未被重发改动
        plan_b = plan_balance([left_old, right2], [3, 0], [weight_m2], target_moment=4)
        show_plan("场景二：新切面 h=[0,2,0]，右盘最多 1，起始 (3,0)，目标 4，挂费 2", plan_b)
        assert plan_b.total_cost == 3 and len(plan_b.hung) == 1
        assert (plan_b.trays[0].water, plan_b.trays[1].water) == (2, 1)

        # 场景三：仍用原计划/旧盘标定，只把砝码挂费改为 1/2。
        weight_cheap = Weight("W+2", moment=Fraction(2), fee=Fraction(1, 2))
        plan_c = plan_balance([left, right], [3, 0], [weight_cheap], target_moment=4)
        show_plan("场景三：原盘不动，砝码挂费改为 1/2，起始 (3,0)，目标 4", plan_c)
        assert plan_c.total_cost == Fraction(3, 2) and len(plan_c.hung) == 1
        assert (plan_c.trays[0].water, plan_c.trays[1].water) == (2, 1)

        # 反例：同一浅盘中格先后进水，按总量均分会报错力矩。
        # 用真实切面 v2：左格 h=0、中格 h=2（高台），两格 l 都被 v2 的无开口
        # 计划冻结为 3；刻线没过 2 之前中格一直干着。
        banner("反例：同盘先后进水，按总量均分会报错力矩")
        seq = Pan.from_plan(
            "顺序进水盘(切面v2)",
            surface2,
            plan2,
            [((0, 0), 3), ((0, 1), -1)],
        )
        show_pan(seq)
        for w in (Fraction(1), Fraction(2), Fraction(3)):
            lookup = seq.lookup_water(w)
            exact = seq.moment_at(lookup.z_lo)
            # 错误做法：无视高台，把总量平均摊到两格（各自 W/2）。
            wrong = Fraction(w, 2) * 3 + Fraction(w, 2) * (-1)
            print(f"  W={w}: 刻线 z={lookup.z_lo}，精确力矩 T={exact}；"
                  f"按总量均分两格的错误力矩={wrong}，偏差={exact - wrong}")
        # W≤2 时水全部落在低格（高格 h=2 仍干），均分却给高格摊了一半。
        lookup2 = seq.lookup_water(Fraction(2))
        assert lookup2.z_lo == 2 and seq.moment_at(lookup2.z_lo) == 6
        assert Fraction(2, 2) * 3 + Fraction(2, 2) * (-1) == 2  # 均分答案错
        # W=3：刻线 5/2，低格 2.5、高格刚进 0.5 —— 精确 7，均分仍报 3。
        lookup3 = seq.lookup_water(Fraction(3))
        assert (lookup3.z_lo, seq.moment_at(lookup3.z_lo)) == (Fraction(5, 2), 7)
        assert Fraction(3, 2) * 3 + Fraction(3, 2) * (-1) == 3

        # 平台演示：刻线走过干格/满格时，同一水量对应一段合法刻线。
        # l 全部来自真实发布单：plan_lcap(sill0) 两格 l=1；
        # plan_dry(sill2) 两格 l=2；plan1(无开口) 两格 l=3。
        banner("平台：同一水量的合法刻线给出区间")
        # 满台盘（真实工件，v1 平坦 h=0）：左格 l=1 先满，中格 l=3 继续吃水。
        full = Pan.from_cell_plans(
            "满台盘",
            surface1,
            [
                ((0, 0), 2, plan_lcap),  # h=0, l=1：先满
                ((0, 1), 5, plan1),      # h=0, l=3：后吃水
            ],
        )
        show_pan(full)
        r_cap = full.lookup_water(full.capacity)
        r_kink = full.lookup_water(Fraction(2))   # 左格在 z=1 封顶的拐角
        print(f"  满台盘容量 W*={full.capacity}；封顶拐角 W=2 → z={r_kink.z_lo}，"
              f"满盘 W={full.capacity} → z={r_cap.z_lo}")
        assert r_kink.z_lo == r_kink.z_hi == 1 and r_cap.z_lo == 3
        # 第二段左格已满、只有中格吃水：同样走 1 单位刻线，水量只增 1。
        second = full.z_segments[1]
        assert second.is_plateau is False
        assert (second.w_hi - second.w_lo, second.z_hi - second.z_lo) == (2, 2)

        # 宽干台（真实工件，v2 高台切面 h=[0,2]，无开口计划 l=3）：
        # 左格 z∈[0,2] 吃水到 2 即遇高格地面；z∈[2,?] 高格才开始进水，
        # 拐角 W=2 的刻线唯一，分段表精确标出高台转折。
        wide = Pan.from_plan(
            "宽干台盘(切面v2)",
            surface2,
            plan2,
            [((0, 0), 2), ((0, 1), 5)],
        )
        show_pan(wide)
        r_kink = wide.lookup_water(Fraction(2))
        print(f"  反查 W=2（高格刚要进水的拐角）→ z={r_kink.z_lo}，"
              f"力矩={r_kink.moment_lo}")
        assert r_kink.z_lo == r_kink.z_hi == 2 and r_kink.moment_lo == 4

        # 有宽度的内部干台/满台区间：同一 v1 表面，左格引用 l=1 的发布单、
        # 中格引用 l=2 的发布单。z∈[1,2] 左格已满、中格(l=2)还在吃水，
        # 而把两格上限取成相同档时，满盘点 W* 对应 z 到达 z_max 的端点；
        # 这里再用 l=1 的两格盘展示“满盘后刻线可停在任意 ≥1 的合法上限端点”。
        capped = Pan.from_cell_plans(
            "双封顶盘",
            surface1,
            [
                ((0, 0), 2, plan_lcap),  # l=1
                ((0, 1), 5, plan_dry),   # l=2
            ],
        )
        show_pan(capped)
        r_half = capped.lookup_water(Fraction(1))
        r_corner = capped.lookup_water(Fraction(2))
        r_c2 = capped.lookup_water(capped.capacity)
        print(f"  反查 W=1 → z={r_half.z_lo}；封顶拐角 W=2 → z={r_corner.z_lo}；"
              f"满盘 W*={capped.capacity} → z={r_c2.z_lo}")
        assert r_half.z_lo == Fraction(1, 2)
        assert r_corner.z_lo == r_corner.z_hi == 1 and r_c2.z_lo == 2
        # 干区端点：v2 高格盘零水量只对应最低地面 z=0。
        assert wide.lookup_water(Fraction(0)).z_lo == 0

        # 不可行也要能明确报错（右盘只有 1 容量却硬要 3 单位）。
        banner("不可行场景")
        try:
            plan_balance([left_old, right2], [3, 0], [], target_moment=10)
        except InfeasibleBalance as exc:
            print(f"  如预期无解：{exc}")

        publisher.close()
        catalog.close()
        print("\n全部断言通过。")


if __name__ == "__main__":
    main()
