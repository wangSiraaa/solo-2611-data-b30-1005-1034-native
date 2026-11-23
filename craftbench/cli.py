"""工艺台命令行入口。

    python -m craftbench voucher SCENARIO.json OUT.json
    python -m craftbench order   SCENARIO.json OUT.json [--mode min_boxes|min_fee]
                                            [--box-cap N] [--fee-budget N]
    python -m craftbench intake  WRITER.json READER.json TRIAL_FILE.json OUT.json

``voucher`` 只出可独查的比例凭单；``order`` 在凭单合格时出装窑+装箱联合工单；
``intake`` 走旧 extcontract 接入口摄入试烧来单并发布 reader-records/v1。
全部数值精确，输出 UTF-8 JSON。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from extcontract import IngestionLibrary, load_contract, write_records

from .errors import CraftError
from .model import Objective, load_scenario
from .planner import plan_work_order
from .voucher import build_voucher, joint_dimensions


def _dump(doc: dict, path: str) -> None:
    Path(path).write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def cmd_voucher(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario)
    voucher = build_voucher(scenario)
    _dump(voucher.to_json(), args.output)
    print(f"凭单 accepted={voucher.accepted}，写入 {args.output}")
    if not voucher.accepted:
        for reason in voucher.rejection_reasons:
            print("  退校:", reason)
        return 2
    return 0


def cmd_order(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario)
    if args.mode is not None or args.box_cap is not None or args.fee_budget is not None:
        scenario = type(scenario)(
            scenario.scenario_id, scenario.styles, scenario.racks,
            Objective(
                mode=args.mode or scenario.objective.mode,
                box_cap=args.box_cap if args.box_cap is not None else scenario.objective.box_cap,
                fee_budget=(args.fee_budget if args.fee_budget is not None
                            else scenario.objective.fee_budget),
            ),
            scenario.pieces,
        )
    voucher = build_voucher(scenario)
    order = plan_work_order(scenario, voucher)
    _dump(order.to_json(), args.output)
    print(f"工单：{order.note}，写入 {args.output}")
    return 0


def cmd_joint(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario)
    voucher = build_voucher(scenario)
    voucher.raise_if_rejected()
    _dump(joint_dimensions(scenario, voucher, args.inner, args.outer), args.output)
    print(f"同炉/异炉联合尺寸写入 {args.output}")
    return 0


def cmd_intake(args: argparse.Namespace) -> int:
    writer = load_contract(args.writer, role="writer")
    reader = load_contract(args.reader, role="reader")
    document = json.loads(Path(args.trial_file).read_text(encoding="utf-8"))
    library = IngestionLibrary(reader)
    result = library.ingest(writer, document)
    if not result.accepted:
        print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
        print("来单被拒，目标状态不变", file=__import__("sys").stderr)
        return 1
    write_records(library.publish(), args.output)
    print(f"接收 {len(result.receipt['row_ids'])} 行，发布 {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="craftbench")
    sub = parser.add_subparsers(dest="command", required=True)

    p_v = sub.add_parser("voucher", help="出工艺凭单")
    p_v.add_argument("scenario")
    p_v.add_argument("output")
    p_v.set_defaults(func=cmd_voucher)

    p_o = sub.add_parser("order", help="出装窑+装箱联合工单")
    p_o.add_argument("scenario")
    p_o.add_argument("output")
    p_o.add_argument("--mode", choices=["min_boxes", "min_fee"])
    p_o.add_argument("--box-cap", type=int, default=None)
    p_o.add_argument("--fee-budget", type=int, default=None)
    p_o.set_defaults(func=cmd_order)

    p_j = sub.add_parser("joint", help="任意两件同炉/异炉联合尺寸")
    p_j.add_argument("scenario")
    p_j.add_argument("inner")
    p_j.add_argument("outer")
    p_j.add_argument("output")
    p_j.set_defaults(func=cmd_joint)

    p_i = sub.add_parser("intake", help="经旧接入口摄入试烧来单")
    p_i.add_argument("writer")
    p_i.add_argument("reader")
    p_i.add_argument("trial_file")
    p_i.add_argument("output")
    p_i.set_defaults(func=cmd_intake)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CraftError as exc:
        print(f"工艺台拒绝: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
