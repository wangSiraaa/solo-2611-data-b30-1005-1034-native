"""Replay the choir brief against real files through extcontract ingestion.

Run with::

    PYTHONPATH=. python3.11 examples/run_choir_demo.py

Every audition set is a real JSON file under ``materials/choir`` ingested by
:class:`extcontract.ingest.IngestionLibrary`.  Evidence is read only from the
library's published ``reader-records/v1`` snapshot, so a reader-filled default
ending can never masquerade as an actual signature.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHOIR = ROOT / "materials" / "choir"
OUT = ROOT / "examples" / "out"
sys.path.insert(0, str(ROOT))

from extcontract import (  # noqa: E402
    IngestionLibrary,
    coach_query,
    load_contract,
    parse_piece,
    parse_scores,
    parse_singers,
    proven_regions,
    solve,
)
from extcontract.choir import program_summary  # noqa: E402

WRITER = load_contract(CHOIR / "writer-contract.json", role="writer")
WRITER_NO_END = load_contract(CHOIR / "writer-contract-omit-ending.json", role="writer")
READER = load_contract(CHOIR / "reader-contract.json", role="reader")

PIECE = parse_piece(json.loads((CHOIR / "piece.json").read_text(encoding="utf-8")))
SCORES = parse_scores(json.loads((CHOIR / "scores.json").read_text(encoding="utf-8")))
SINGERS = parse_singers(json.loads((CHOIR / "singers.json").read_text(encoding="utf-8")))
BAND_FREE = None
BAND_7 = set(
    json.loads((CHOIR / "band-only-7.json").read_text(encoding="utf-8"))[
        "available_overpass_pitches"
    ]
)


def load(name: str) -> dict:
    return json.loads((CHOIR / name).read_text(encoding="utf-8"))


def ingest_all(files, *, no_ending_file=False):
    """Ingest each file atomically; return (library, ingest results)."""
    library = IngestionLibrary(READER)
    results = []
    for name in files:
        writer = WRITER_NO_END if (no_ending_file and name.endswith("ending-omitted.json")) else WRITER
        results.append((name, library.ingest(writer, load(name))))
    return library, results


def region_lines(records: dict) -> list[str]:
    out = []
    for singer, regions in sorted(proven_regions(records).items()):
        out.append(
            f"  {singer}: "
            + ", ".join(f"[{r.low},{r.high}] ({'/'.join(r.sources)})" for r in regions)
        )
    return out


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def show(result: dict) -> None:
    print(program_summary(result))
    for attempt in result.get("attempts", []):
        state = "可行" if attempt.get("feasible") else "不可行"
        extra = (
            f"换人 {attempt.get('switches')}、费 {attempt.get('cost')}"
            if attempt.get("feasible")
            else attempt.get("reason")
        )
        print(f"  [{attempt['score_id']}] {state}: {extra}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    # 0. Contract compatibility + real client round-trip through ingestion.
    banner("0. 合同兼容与 reader_client 真实回读")
    library, ingested = ingest_all(["auditions-original.json"])
    (name, result), = ingested
    assert result.accepted, result.errors
    published = library.publish()
    path = OUT / "choir-published.json"
    path.write_text(json.dumps(published, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rc = subprocess.run(
        [sys.executable, str(ROOT / "materials/745/reader_client.py"),
         str(CHOIR / "reader-contract.json"), str(path)],
        capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stderr
    print("reader_client 读取已发布快照:", rc.stdout.strip())

    # 1. Original auditions and the coach's proven-range query.
    banner("1. 原始试唱：已证区段与教练可查依据")
    query = coach_query(published)
    for audit in query["auditions"]:
        print(
            f"  {audit['audition']}: 实测 {audit['measured']} -> {audit['status']}"
            + (f" 区段 {audit['region']}" if audit["region"] else f" 原因 {audit['reasons']}")
        )
    print("  合并后已证区段:")
    print("\n".join(region_lines(published)))
    regions = proven_regions(published)
    assert regions["甲"] == sorted(regions["甲"], key=lambda r: r.low)
    assert [[r.low, r.high] for r in regions["甲"]] == [[5, 6], [9, 9]]
    assert [[r.low, r.high] for r in regions["乙"]] == [[8, 8]]
    assert [[r.low, r.high] for r in regions["丙"]] == [[5, 8]]
    # The coach cannot paint 6..9 just because 甲 has 6 and 9.
    assert all(not (r.low <= 6 and 9 <= r.high) for r in regions["甲"])

    # 2. Baseline: 甲 hands 乙 at the rest (1 switch) with the free original.
    banner("2. 基线（原调+乙）：无乐器限制下甲接乙，零费换一次")
    r2a = solve(PIECE, [s for s in SCORES if s.score_id == "S0"], SINGERS, regions,
                band=BAND_FREE, mode="switch", cap=1)
    show(r2a)
    assert r2a["feasible"] and r2a["score_id"] == "S0"
    assert r2a["switches"] == 1 and r2a["cost"] == 0
    assert [p["singer"] for p in r2a["phrases"]] == ["甲", "乙"]

    # But when the pianist minimises switches first, +1 lets 甲 sing both
    # (6 and 9 as two separated short phrases): zero switches, cost 1.
    banner("2b. 全曲一谱、零换人最省：+1 谱，费 1、零换人")
    r2b = solve(PIECE, SCORES, SINGERS, regions, band=BAND_FREE)
    show(r2b)
    assert r2b["feasible"] and r2b["score_id"] == "S1"
    assert r2b["switches"] == 0 and r2b["cost"] == 1
    assert [p["singer"] for p in r2b["phrases"]] == ["甲", "甲"]
    assert [p["actual"] for p in r2b["phrases"]] == [[6, 6], [9, 9]]

    # 3. Original key required and no change: must pay guest 丙 (3).
    banner("3. 只好原调和零换人：付 3 请丙")
    r3 = solve(PIECE, [s for s in SCORES if s.score_id == "S0"], SINGERS, regions,
               band=BAND_FREE, mode="switch", cap=0)
    show(r3)
    assert r3["feasible"] and r3["switches"] == 0 and r3["cost"] == 3
    assert {p["singer"] for p in r3["phrases"]} == {"丙"}

    # 4. +1 score: 6 / 9 become two separate short phrases for 甲, cost 1.
    banner("4. 借 +1 谱：6、9 由甲两个分开的短句，费 1、零换人")
    r4 = solve(PIECE, SCORES, SINGERS, regions, band=BAND_FREE, mode="switch", cap=0)
    show(r4)
    assert r4["feasible"] and r4["score_id"] == "S1"
    assert r4["switches"] == 0 and r4["cost"] == 1
    assert [p["actual"] for p in r4["phrases"]] == [[6, 6], [9, 9]]
    assert {p["singer"] for p in r4["phrases"]} == {"甲"}

    # 4b. If a single phrase demanded continuous 6..9 甲 cannot bluff it.
    banner("4b. 若单句要求连续 6 至 9：甲不能冒唱")
    from extcontract.choir import Phrase, Piece, Voice
    one_long = Piece(
        piece_id="one-long", title="连续6-9单句",
        voices=(Voice("V-SOLO", "独唱", (Phrase("V-SOLO", "P1", 6, 9, 0, False),)),),
        overpasses=(),
    )
    r4b = solve(one_long, [s for s in SCORES if s.score_id == "S1"], SINGERS, regions,
                band=BAND_FREE, mode="switch", cap=0)
    show(r4b)
    assert not r4b["feasible"]

    # 5. Give 甲 a complete 5,6,7,8 audition: original key, zero cost/switches.
    banner("5. 给甲补一份完整 5、6、7、8：原调两句，零费零换")
    library5, ing5 = ingest_all(["auditions-original.json", "auditions-jia-supplement.json"])
    for name, res in ing5:
        assert res.accepted, (name, res.errors)
    regions5 = proven_regions(library5.publish())
    print("  补测后甲的区段:", [[r.low, r.high] for r in regions5["甲"]])
    assert [[r.low, r.high] for r in regions5["甲"]] == [[5, 8], [9, 9]]

    r5 = solve(PIECE, [s for s in SCORES if s.score_id == "S0"], SINGERS, regions5,
               band=BAND_FREE, mode="switch", cap=0)
    show(r5)
    assert r5["feasible"] and r5["score_id"] == "S0"
    assert r5["switches"] == 0 and r5["cost"] == 0
    assert {p["singer"] for p in r5["phrases"]} == {"甲"}

    # 6. Original auditions + band only permits the notated 7 at the overpass:
    #    +1 score's 8 is unavailable, so no-switch minimum is 原调 丙, cost 3.
    banner("6. 乐团只给 7：+1 谱过门 8 不可用，零换人只剩原调丙、费 3")
    r6 = solve(PIECE, SCORES, SINGERS, regions, band=BAND_7, mode="switch", cap=0)
    show(r6)
    assert r6["feasible"] and r6["score_id"] == "S0"
    assert r6["switches"] == 0 and r6["cost"] == 3
    assert {p["singer"] for p in r6["phrases"]} == {"丙"}
    assert [o["actual"] for o in r6["overpasses"]] == [7]

    # 7. A singer who can prove only 5 and only 9 (two isolated points).
    #    5->9 looks splittable, but with one pianist on one whole-piece score
    #    and no switches, no score in the library turns 5,9 into one proven
    #    range.  There is no such whole-piece score to borrow.
    banner("7. 只有会 5 和 9 的同一人、无乐器限制、不许换人：无整首总谱")
    lib59, ing59 = ingest_all(["auditions-wu-59.json"])
    (_, res59), = ing59
    assert res59.accepted, res59.errors
    snap59 = lib59.publish()
    regions59 = proven_regions(snap59)
    print("  戊的已证区段:", [[r.low, r.high] for r in regions59["戊"]])
    assert [[r.low, r.high] for r in regions59["戊"]] == [[5, 5], [9, 9]]
    # The coach cannot paint 5..9: they share no pitch.
    assert all(not (r.low <= 5 and 9 <= r.high) for r in regions59["戊"])
    r7 = solve(PIECE, SCORES, SINGERS, regions59, band=BAND_FREE,
               available=["戊"], mode="switch", cap=0)
    show(r7)
    assert not r7["feasible"]

    # 8. Signing basis: default-filled ending vs an actual ending signature.
    banner("8a. 缺列默认完成（reader default）不能充当收尾签字")
    lib_default, ing_default = ingest_all(["auditions-ending-omitted.json"], no_ending_file=True)
    (_, res_default), = ing_default
    assert res_default.accepted, res_default.errors
    snap_default = lib_default.publish()
    end_src = snap_default["rows"][-1]["sources"]["ending"]
    print("  发布的 ending 来源:", json.dumps(end_src, ensure_ascii=False))
    assert end_src.get("conversion") == "fill_default"
    q_default = coach_query(snap_default)
    print("  诊断:", json.dumps(q_default["auditions"], ensure_ascii=False))
    assert q_default["auditions"][0]["status"] == "no_evidence"
    assert proven_regions(snap_default) == {}

    banner("8b. 确有收尾签字：同一形状被认定为已证区段")
    lib_signed, ing_signed = ingest_all(["auditions-ending-signed.json"])
    (_, res_signed), = ing_signed
    assert res_signed.accepted, res_signed.errors
    snap_signed = lib_signed.publish()
    print("  发布的 ending 来源:", json.dumps(
        snap_signed["rows"][-1]["sources"]["ending"], ensure_ascii=False))
    q_signed = coach_query(snap_signed)
    print("  诊断:", json.dumps(q_signed["auditions"], ensure_ascii=False))
    assert q_signed["auditions"][0]["status"] == "proven_interval"
    assert proven_regions(snap_signed)["甲"][0].low == 5
    assert proven_regions(snap_signed)["甲"][0].high == 6

    # 9. Defect files: leap, gap/missing, unsigned, same-audition conflict.
    banner("9. 过跳 / 缺测 / 未签 / 同号同序冲突都不签连续音区")
    lib_bad, ing_bad = ingest_all(["auditions-defects.json"])
    (_, res_bad), = ing_bad
    assert res_bad.accepted, res_bad.errors
    q_bad = coach_query(lib_bad.publish())
    for audit in q_bad["auditions"]:
        print(f"  {audit['audition']}: {audit['status']} {audit['reasons']}")
        assert audit["status"] == "no_evidence"
    assert proven_regions(lib_bad.publish()) == {}

    lib_conflict, ing_conflict = ingest_all(["auditions-conflict.json"])
    (_, res_conflict), = ing_conflict
    print("  同 row_id 同修订冲突文件被摄取层原子拒绝:", not res_conflict.accepted)
    print("  拒绝原因:", res_conflict.errors)
    assert not res_conflict.accepted

    # Same audition id + same position with two different measurements, but
    # distinct row ids, so ingestion accepts it; the evidence layer must still
    # refuse to sign a range from it.
    lib_seq, ing_seq = ingest_all(["auditions-conflict-seq.json"])
    (_, res_seq), = ing_seq
    assert res_seq.accepted, res_seq.errors
    seq_audit = {
        a["audition"]: a for a in coach_query(lib_seq.publish())["auditions"]
    }["A-JIA-SEQ-CLASH"]
    print("  同号同序（不同 row_id）仍不签:", seq_audit["status"], seq_audit["reasons"])
    assert seq_audit["status"] == "no_evidence"
    assert proven_regions(lib_seq.publish()) == {}

    print("\nAll choir assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
