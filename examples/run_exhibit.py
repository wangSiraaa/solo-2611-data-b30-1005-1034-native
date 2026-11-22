"""Re-runnable demo of the fictional astronomy exhibit on the sealed auction.

Run from the repository root:

    python3 -m examples.run_exhibit

Five independent episodes (each gets a fresh temporary database):

  1. reference field: a team settles 4 units; F/G/H orders -> G+H score 8;
  2. moving G's circle track to y=2 costs G its station pair -> F scores 5;
  3. one station down at exposure 6 breaks H's pair -> F scores 5;
  4. three stations: the same event is provable from different station pairs;
  5. a real partial fill (2 of 4): the forecast is unchanged, the signable
     night shortens.

Everything printed is derived from the stored exhibit model, so the triple
substitutions, plate permissions, pairable stations, execution sheets and
buyer accounts can be re-checked line by line.  All synthetic; no real
sky, money or telescopes.
"""

from __future__ import annotations

import json
import tempfile

from sealed_auction import (
    AuctionService,
    ClearingService,
    Database,
    ExhibitService,
    MarginLedger,
)

TRACK = {1: (1, 0), 2: (2, 0), 3: (3, 0)}


def show(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def dump(label: str, obj) -> None:
    print(f"\n[{label}]")
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


class Episode:
    """One self-contained exhibit episode on a fresh temporary database."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(f"{self._tmp.name}/exhibit.sqlite3")
        self.margin = MarginLedger(self.db)
        self.auctions = AuctionService(self.db, self.margin)
        self.clearing = ClearingService(self.db, self.margin, self.auctions)
        self.exhibit = ExhibitService(self.db)
        self._req = 0

    def req(self, tag: str) -> str:
        self._req += 1
        return f"demo-{tag}-{self._req:03d}"

    def cleanup(self) -> None:
        self._tmp.cleanup()

    def settle(self, buyer: str, bid_qty: int, units: int) -> int:
        """Real auction: deposit, freeze, bid, cutoff, settle.  Returns the
        actually settled quantity (partial fills count by real fill)."""
        self.margin.deposit(self.req("dep"), buyer, 1_000_000)
        self.auctions.create_auction(self.req("new"), "auc-1", units)
        hold = self.margin.freeze(self.req("frz"), buyer, "auc-1", bid_qty * 500)
        self.auctions.place_bid(
            self.req("bid"), "bid-1", "auc-1", buyer, bid_qty, 500, hold["hold_id"]
        )
        frozen = self.auctions.freeze_auction(self.req("cls"), "auc-1")
        self.clearing.settle(self.req("stl"), "auc-1")
        return frozen["allocations"][0]["allocated_qty"]

    def reference_model(self, g_cy: int = 0) -> None:
        """The reference two-station F/G/H exhibit from the problem statement."""
        ex = self.exhibit
        ex.register_station(self.req("st"), "S1", 2, 1, 1)
        ex.register_station(self.req("st"), "S2", 2, -1, 1)
        g_track = {e: (cx, g_cy) for e, (cx, _) in TRACK.items()}
        ex.register_star(self.req("sr"), "F", 1, TRACK, {"S1": 0, "S2": 0})
        ex.register_star(self.req("sr"), "G", 1, g_track, {"S1": 2, "S2": 2})
        ex.register_star(self.req("sr"), "H", 1, {4: (1, 0), 5: (2, 0), 6: (3, 0)},
                         {"S1": 2, "S2": 2})
        ex.register_order(self.req("or"), "ord-F", "terra", "F", 2, 5)
        ex.register_order(self.req("or"), "ord-G", "terra", "G", 2, 4)
        ex.register_order(self.req("or"), "ord-H", "terra", "H", 5, 4)


def _failed_checks(checks: dict) -> str:
    failed = []
    if not checks["ephemeris"]:
        failed.append("缺星历")
    if not checks["pattern_bright_dark_bright"]:
        failed.append("非亮暗亮")
    if not checks["mirror_allowed"]:
        failed.append("镜位不许")
    if not all(checks["duty"]):
        failed.append("值班停机")
    return ",".join(failed)


def print_forecast(predict: dict) -> None:
    for event in predict["events"]:
        print(f"\n  星 {event['star']}  中心拍 {event['center_exposure']}  "
              f"半径 {event['radius']}")
        for block in event["stations"]:
            subs = "  ".join(
                f"拍{s['exposure']}:d²={s['dist2']}{'≤' if s['blocked'] else '>'}"
                f"r²={s['r2']}→{'暗' if s['blocked'] else '亮'}"
                for s in block["substitutions"]
            )
            verdict = "签发" if block["plate"] else f"拒签({_failed_checks(block['checks'])})"
            print(f"    站 {block['station']} 镜位 {block['mirror']}: {subs} "
                  f"=> 底片 {verdict}")
        pairs = [p["stations"] for p in event["pairs"]]
        print(f"    底片许可: {event['plates']}  可成对站名: {pairs}")


def print_plan(plan: dict) -> None:
    print(f"\n  夜班 {plan['night_id']}: 总展览价值 {plan['total_value']}, "
          f"开镜 {plan['openings_used']} 次")
    for s in plan["scored"]:
        print(f"    得分 {s['order_id']} 星 {s['star']} 中心拍 "
              f"{s['center_exposure']} 值 {s['value']} 站对 {s['stations']}")
    for s in plan["skipped"]:
        print(f"    跳过 {s['order_id']} ({s['reason']})")
    print("\n  各站逐拍执行单:")
    for station, sheet in plan["sheets"].items():
        cells = []
        for e in sheet:
            if e["activity"] == "expose":
                light = "亮" if e["light"] == "bright" else "暗"
                cells.append(f"{e['exposure']:>2}:{e['star']}{light}"
                             f"@{e['mirror']}")
            elif e["activity"] == "move":
                cells.append(f"{e['exposure']:>2}:移→{e['mirror']}")
            elif e["activity"] == "down":
                cells.append(f"{e['exposure']:>2}:停机")
            else:
                cells.append(f"{e['exposure']:>2}:·")
        print(f"    {station}: " + " ".join(cells))
    print("\n  买家逐次兑账:")
    for a in plan["accounts"]:
        print(f"    {a['buyer']}: 实得 {a['granted_openings']}  "
          f"此前已花 {a['spent_before']}  本夜花 {a['spent_this_night']}  "
          f"剩余 {a['remaining_openings']}")


def scenario_1() -> None:
    show("场景 1. 真场主例：结清 4 单位；F/G/H -> 应选 G、H 得 8")
    ep = Episode()
    try:
        granted = ep.settle("terra", bid_qty=4, units=4)
        print(f"  terra 实际结清 {granted} 单位 -> {granted} 次开镜")
        ep.reference_model()
        print("\n  预报（三次代入 / 底片许可 / 可成对站名）:")
        print_forecast(ep.exhibit.predict())
        plan = ep.exhibit.plan_night(ep.req("night"), "night-1")
        print_plan(plan)
        print("\n  注: F(镜0)拍完已到拍3，H(镜2)拍4就要开始，中间没有空拍可移镜，"
              "所以 F 与 H 不可兼得；G、H 同镜位 2 连拍，得 8。")
    finally:
        ep.cleanup()


def scenario_2() -> None:
    show("场景 2. 只把 G 的三拍圆心纵坐标改成 2 -> G 失去一对有效站，最优改 F 得 5")
    ep = Episode()
    try:
        ep.settle("terra", bid_qty=4, units=4)
        ep.reference_model(g_cy=2)
        print_forecast(ep.exhibit.predict())
        plan = ep.exhibit.plan_night(ep.req("night"), "night-1")
        print_plan(plan)
        print("\n  注: S2=(2,-1) 看中拍圆心 (2,2)，d²=9>1 始终亮，看不到暗拍，"
              "G 只剩 S1 一张底片，无法成对。")
    finally:
        ep.cleanup()


def scenario_3() -> None:
    show("场景 3. 固定原星历，仅把 S2 第 6 拍改为停机 -> H 不能成对，仍选 F 得 5")
    ep = Episode()
    try:
        ep.settle("terra", bid_qty=4, units=4)
        ep.reference_model()
        ep.exhibit.set_duty(ep.req("duty"), "S2", 6, False)
        print_forecast(ep.exhibit.predict())
        plan = ep.exhibit.plan_night(ep.req("night"), "night-1")
        print_plan(plan)
        print("\n  注: H 需要拍 4/5/6，S2 第 6 拍停机签不出第三张底片，H 无法成对。")
    finally:
        ep.cleanup()


def scenario_4() -> None:
    show("场景 4. 三站同一事件可从不同站对取得：A 在中拍圆心上方，B/C 在下方")
    ep = Episode()
    try:
        ep.settle("terra", bid_qty=2, units=2)
        ex = ep.exhibit
        ex.register_station(ep.req("st"), "A", 3, 2, 1)
        ex.register_station(ep.req("st"), "B", 3, -2, 1)
        ex.register_station(ep.req("st"), "C", 4, -1, 1)
        ex.register_star(ep.req("sr"), "E", 2, {7: (0, 0), 8: (3, 0), 9: (6, 0)},
                         {"A": 0, "B": 0, "C": 0})
        ex.register_order(ep.req("or"), "ord-E", "terra", "E", 8, 6)
        print_forecast(ex.predict())
        plan = ex.plan_night(ep.req("night"), "night-1")
        print_plan(plan)

        print("\n  -- 变体: B 站中拍停机，同一事件改由 (A, C) 证实 --")
        ep2 = Episode()
        try:
            ep2.settle("terra", bid_qty=2, units=2)
            ex2 = ep2.exhibit
            ex2.register_station(ep2.req("st"), "A", 3, 2, 1)
            ex2.register_station(ep2.req("st"), "B", 3, -2, 1)
            ex2.register_station(ep2.req("st"), "C", 4, -1, 1)
            ex2.register_star(ep2.req("sr"), "E", 2,
                              {7: (0, 0), 8: (3, 0), 9: (6, 0)},
                              {"A": 0, "B": 0, "C": 0})
            ex2.register_order(ep2.req("or"), "ord-E", "terra", "E", 8, 6)
            ex2.set_duty(ep2.req("duty"), "B", 8, False)
            print_forecast(ex2.predict())
            print_plan(ex2.plan_night(ep2.req("night"), "night-1"))
        finally:
            ep2.cleanup()
    finally:
        ep.cleanup()


def scenario_5() -> None:
    show("场景 5. 真实部分获配：要 4 只得 2 -> 预报不变，可签夜班缩短")
    full = Episode()
    try:
        full.settle("terra", bid_qty=4, units=4)
        full.reference_model()
        forecast_full = full.exhibit.predict()
        plan_full = full.exhibit.plan_night(full.req("night"), "night-1")
    finally:
        full.cleanup()

    ep = Episode()
    try:
        granted = ep.settle("terra", bid_qty=4, units=2)
        print(f"  terra 投标 4 单位，真实部分获配 {granted} -> {granted} 次开镜")
        ep.reference_model()
        forecast_partial = ep.exhibit.predict()
        print(f"\n  预报星历不因缺券改变: 与 4 单位场景完全一致 = "
              f"{forecast_partial == forecast_full}")
        plan = ep.exhibit.plan_night(ep.req("night"), "night-1")
        print_plan(plan)
        print(f"\n  对照: 4 单位时可签价值 {plan_full['total_value']} 的夜班 "
              f"(G+H)，缺券后只能签价值 {plan['total_value']} 的单镜 (F)。")
    finally:
        ep.cleanup()


def main() -> int:
    scenario_1()
    scenario_2()
    scenario_3()
    scenario_4()
    scenario_5()
    print("\n全部场景完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
