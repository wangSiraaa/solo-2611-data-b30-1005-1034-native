"""工艺台端到端演示：试烧来单 -> 工艺凭单 -> 装窑/装箱两张工单。

运行：

    PYTHONPATH=. python3.11 examples/run_craft_demo.py

全程复用旧 extcontract 接入口与固定读者（reader_client 以真实子进程核读），
覆盖题面的每一条纪律：

1. 两件同款、禁止转向；比例候选 {1, 4/5}，校样实读 [4,5] 两组都活；
2. 基准工单：分炉时 1 与 4/5 的组合会出现 5 > 24/5 擦边失败，零架费只能
   两箱；合租费 2 的双位架同炉同比例 => 一箱（最少箱数优先，架费 2）；
3. 母题修订（仅该实样署名实读改为 [5,5]）经旧接入口 supersede 重发：
   4/5 被凭单凭实际签读排除，普通分炉即一箱；旧工单不被改写；
4. 横纵反向比例的对照情景：成对的炉况检查给一箱，横纵各取最坏边界会虚构
   一个任何单组比例都产生不了的炉况（共架时尤甚）；
5. 箱位上限模式：免费分炉达不到上限时，求出最低须租架费；
6. 全组矛盾退校、未签读占位不约束比例、真实固定读者核读。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRAFT = ROOT / "materials" / "craftbench"
FIX = ROOT / "fixtures" / "craftbench"
OUT = ROOT / "examples" / "out"
sys.path.insert(0, str(ROOT))

from extcontract import check_compatibility, load_contract, write_records  # noqa: E402

from craftbench import build_voucher, load_scenario, plan_work_order  # noqa: E402
from craftbench.errors import CraftError  # noqa: E402
from craftbench.intake import TrialIntake  # noqa: E402
from craftbench.model import Objective  # noqa: E402
from craftbench.planner import axiswise_worst_check  # noqa: E402
from craftbench.voucher import joint_dimensions  # noqa: E402


def banner(n: str, title: str) -> None:
    print(f"\n=== {n}. {title} ===")


def fixed_reader(records_path: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(CRAFT / "reader_client.py"),
         str(CRAFT / "trial-reader-contract.json"), str(records_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"固定读者核读失败: {proc.stderr}")
    return json.loads(proc.stdout)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    # 0. 旧接入口的行无关合同兼容性（写端域 -> 固定读者域）。 ----------------
    banner("0", "旧接入口合同兼容性（行无关全域证明）")
    writer = load_contract(CRAFT / "trial-writer-contract.json", role="writer")
    reader = load_contract(CRAFT / "trial-reader-contract.json", role="reader")
    report = check_compatibility(writer, reader)
    print("compat:", report.summary())
    assert report.safe, report.to_json()

    intake = TrialIntake(
        writer_path=str(CRAFT / "trial-writer-contract.json"),
        reader_path=str(CRAFT / "trial-reader-contract.json"),
    )

    # 1-3. 基准来单 [4,5]：两组都活 -> 第一张工单。 -------------------------
    banner("1", "基准试烧来单 small|sm1|w 实读 [4,5]（旧接入口摄入）")
    base = json.loads((FIX / "trial-base.json").read_text(encoding="utf-8"))
    r1 = intake.ingest_file(base)
    assert r1.accepted, r1.errors
    base_records = intake.library.publish()
    base_pub = OUT / "trial-base-records.json"
    write_records(base_records, base_pub)
    print("固定读者核读:", fixed_reader(base_pub))

    scenario = load_scenario(FIX / "two-sleeve-base.json")
    sc_base = intake.apply_to_scenario(scenario)
    voucher_base = build_voucher(sc_base)
    assert voucher_base.accepted
    assert voucher_base.surviving_pairs("S") == [0, 1]
    print("凭单存活比例组:", voucher_base.surviving_pairs("S"),
          "（1 与 4/5 都能解释 [4,5]）")

    banner("2", "第一张工单：零架费两箱 vs 架费 2 一箱")
    order_base = plan_work_order(sc_base, voucher_base)
    print(order_base.note)
    assert (order_base.box_count, order_base.rack_fee) == (1, 2)
    edge = order_base.edges[0]
    assert edge.same_kiln and edge.inner_orientation == "as_is"
    print(json.dumps(order_base.to_json(), ensure_ascii=False, indent=2))

    # 独查：同炉/异炉联合尺寸，以及 5 > 24/5 的擦边失败回放。
    joint = joint_dimensions(sc_base, voucher_base, "small", "large")
    assert len(joint["same_kiln"]) == 2 and len(joint["separate_kiln"]) == 4
    bad = next(c for c in joint["separate_kiln"]
               if c["pair_index_a"] == 0 and c["pair_index_b"] == 1)
    print("异炉反例炉况 small@1, large@4/5: 小外口",
          bad["small"]["outer_w"], "vs 大内口", bad["large"]["inner_w"],
          "=> 5 > 24/5，擦边即不许，分炉不能保证一箱")

    # 4. 母题修订 [5,5]：supersede 重发，旧工单不动 -> 第二张工单。 ---------
    banner("3", "更正行 [5,5] 经旧接入口 supersede（同 row_id 更高 revision）")
    revision = json.loads((FIX / "trial-revision.json").read_text(encoding="utf-8"))
    r2 = intake.ingest_file(revision)
    assert r2.accepted and r2.receipt["applied"][0]["action"] == "supersede"
    rev_pub = OUT / "trial-revised-records.json"
    write_records(intake.library.publish(), rev_pub)
    print("固定读者核读修订切片:", fixed_reader(rev_pub))

    sc_rev = intake.apply_to_scenario(scenario)
    voucher_rev = build_voucher(sc_rev)
    assert voucher_rev.surviving_pairs("S") == [0]
    excluded = next(pv for pv in voucher_rev.style_verdicts["S"].samples[0].pair_verdicts
                    if pv.pair_index == 1)
    print("被排除组 4/5 的签读依据:", excluded.checks[0].reason)

    banner("4", "第二张工单：普通分炉即一箱、零架费；第一张工单保持原样")
    order_rev = plan_work_order(sc_rev, voucher_rev)
    print(order_rev.note)
    assert (order_rev.box_count, order_rev.rack_fee) == (1, 0)
    assert all(not g.is_rack for g in order_rev.assignment.groups)
    # 旧工单是已下凭证，不被新修订改写。
    assert (order_base.box_count, order_base.rack_fee) == (1, 2)
    print("旧工单仍为:", order_base.box_count, "箱、架费", order_base.rack_fee,
          "| 新工单:", order_rev.box_count, "箱、架费", order_rev.rack_fee)
    print(json.dumps(order_rev.to_json(), ensure_ascii=False, indent=2))

    # 5. 横纵反向比例：成对炉况 vs 横纵各取最坏（虚构炉况）。 ---------------
    banner("5", "横纵反向比例：整组炉况检查与错误的逐轴最坏对照")
    cross = load_scenario(FIX / "cross-axis-demo.json")
    voucher_cross = build_voucher(cross)
    assert voucher_cross.accepted
    assert voucher_cross.style_verdicts["T"].ignored_placeholders  # 缺测占位被忽略
    order_cross = plan_work_order(cross, voucher_cross)
    print("成对炉况工单:", order_cross.note)
    assert (order_cross.box_count, order_cross.rack_fee) == (1, 1)
    assert order_cross.edges[0].same_kiln is True
    print("逐轴最坏（错误对照）判可套:",
          axiswise_worst_check(cross, voucher_cross, "inner", "outer"),
          "—— 它把 pair0 的横向最坏与 pair1 的纵向最坏拼成 (2,2)，"
          "而任何单组比例都产生不了这个内件开口")
    assert axiswise_worst_check(cross, voucher_cross, "inner", "outer") is False

    # 6. 箱位上限模式：求最低须租架费。 -------------------------------------
    banner("6", "车辆箱位上限 1：免费分炉两箱不达标，求最低须租架费")
    cap = load_scenario(FIX / "three-sleeve-cap.json")
    voucher_cap = build_voucher(cap)
    order_cap = plan_work_order(cap, voucher_cap)
    print(order_cap.note, "| 链:", order_cap.chains)
    assert order_cap.box_count == 1 and order_cap.rack_fee == 1
    assert [u.rack_id for u in order_cap.assignment.rack_uses] == ["RB"]

    # 7. 退校与占位纪律。 ---------------------------------------------------
    banner("7", "全组矛盾退校；未签读占位不约束比例")
    reject_doc = {
        "scenario_id": "reject-demo",
        "styles": [{
            "style_id": "X", "allow_rotation": False,
            "candidate_ratios": [["1", "1"], ["2", "2"]],
            "pieces": [{
                "piece_id": "x1",
                "green": {"outer_w": "3", "outer_d": "3",
                          "inner_w": "1", "inner_d": "1"},
                "proofs": [
                    {"proof_id": "x1|q|w", "sample_id": "q", "direction": "w",
                     "green_dim": "3", "observed": ["9", "9"], "signed": True,
                     "revision": 1},
                    {"proof_id": "x1|q|d", "sample_id": "q", "direction": "d",
                     "green_dim": "3", "observed": None, "signed": False,
                     "revision": 1},
                ],
            }],
        }],
        "racks": [],
        "objective": {"mode": "min_boxes", "box_cap": None, "fee_budget": None},
    }
    from craftbench import parse_scenario
    rejected = build_voucher(parse_scenario(reject_doc))
    assert not rejected.accepted
    print("退校原因:", rejected.rejection_reasons)
    print("未签读纵向占位:",
          rejected.style_verdicts["X"].ignored_placeholders)
    try:
        plan_work_order(parse_scenario(reject_doc), rejected)
        raise AssertionError("退校款不得下单")
    except CraftError as exc:
        print("下单被拒:", exc)

    print("\n全部工艺台演示断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
