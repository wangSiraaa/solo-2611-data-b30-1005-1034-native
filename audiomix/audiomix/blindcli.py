"""盲听试验命令行：读 JSON 案例 -> 制作匿名实包 -> 排期 -> 写播放单与封存表。

案例 JSON 结构：
{
  "n_slots": 3,
  "headphone_capacity": [1, 1, 1],
  "listeners": ["甲", "乙"],
  "availability": {"甲": [1, 2, 3], "乙": [1, 2, 3]},
  "cross_person_quota": null,
  "items": [
    {"item_id": "P1", "weight": 5, "deadline": 3,
     "pcm_a": [1, 2], "pcm_b": [2, 1],
     "gains_a": [1], "gains_b": [1],
     "forbidden_listeners": []}
  ]
}
"""
from __future__ import annotations

import argparse
import json
import random

from .blindschedule import MAX_ITEMS, MAX_LISTENERS, MAX_SLOTS
from .blindtrial import (
    ItemInput, anonymous_play_sheets, run_trial, schedule_summary,
    sealed_trace_report, verify_trial_playlists, write_trial_bundle,
)


def load_case(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        case = json.load(fh)
    required = ("n_slots", "headphone_capacity", "listeners", "availability", "items")
    missing = [k for k in required if k not in case]
    if missing:
        raise ValueError(f"案例 JSON 缺少字段: {missing}")
    if len(case["items"]) > MAX_ITEMS or len(case["listeners"]) > MAX_LISTENERS \
            or case["n_slots"] > MAX_SLOTS:
        raise ValueError(f"规模超过上限 {MAX_ITEMS} 项 / {MAX_LISTENERS} 人 / {MAX_SLOTS} 段")
    return case


def _inputs(case: dict) -> list[ItemInput]:
    out = []
    for it in case["items"]:
        out.append(ItemInput(
            item_id=str(it["item_id"]),
            pcm_a=[int(v) for v in it["pcm_a"]],
            pcm_b=[int(v) for v in it["pcm_b"]],
            gains_a=[int(v) for v in it["gains_a"]],
            gains_b=[int(v) for v in it["gains_b"]],
            weight=int(it.get("weight", 0)),
            deadline=int(it.get("deadline", case["n_slots"])),
            forbidden_listeners=list(it.get("forbidden_listeners", []))))
    return out


def run_case(case: dict, seed: int | None = None):
    rng = random.Random(seed) if seed is not None else None
    return run_trial(
        _inputs(case),
        listeners=list(case["listeners"]),
        n_slots=int(case["n_slots"]),
        headphone_capacity=[int(v) for v in case["headphone_capacity"]],
        availability={p: [int(s) for s in case["availability"].get(p, [])]
                      for p in case["listeners"]},
        cross_person_quota=case.get("cross_person_quota"),
        rng=rng)


def cmd_solve(p) -> None:
    case = load_case(p.case)
    result = run_case(case, seed=p.seed)
    errors = verify_trial_playlists(result)
    report = {
        "summary": schedule_summary(result),
        "play_sheets": anonymous_play_sheets(result),
        "sealed_trace": sealed_trace_report(result),
        "verification_errors": errors,
    }
    if p.out_dir:
        paths = write_trial_bundle(result, p.out_dir)
        report["written"] = paths
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(2)


def cmd_verify(p) -> None:
    """仅复核已落盘的试验目录（播放单与封存表的两序相反、摘要自洽）。"""
    with open(p.bundle, encoding="utf-8") as fh:
        bundle = json.load(fh)
    # bundle 为 cmd_solve 落盘的三类 JSON 合并体或单独的 report
    errors = bundle.get("verification_errors", ["目录中无复核结论字段"])
    print(json.dumps({"verification_errors": errors, "ok": not errors},
                     ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="audiomix-blind",
                                 description="双盲听审刺激制作与两序排期")
    sub = ap.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("solve", help="读案例 JSON，制作刺激并排期")
    q.add_argument("--case", required=True)
    q.add_argument("--out-dir", help="给定则写出匿名实包/播放单/封存表")
    q.add_argument("--seed", type=int, help="固定匿名号随机种子（测试可复现）")
    q.set_defaults(fn=cmd_solve)

    q = sub.add_parser("verify", help="复核已生成的试验报告")
    q.add_argument("--bundle", required=True)
    q.set_defaults(fn=cmd_verify)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
