"""End-to-end demonstration of the joint-exhibition pairing service.

Run with:

    PYTHONPATH=. python3.11 examples/run_pairing_demo.py

Two branch libraries each hold half of the roster; the joint operator never
lets either branch see the whole pairing.  The story told here, all through
real writer/reader contracts and files:

  1. the registration desk (报名台) receives both sides' consent forms
     (认同单) through the original ingestion interface — one fixed-reader
     IngestionLibrary per side (its frozen version);
  2. pair (b,1)'s right-side form shows 同意 only because the reader filled
     the missing consent column with its default — that is not a personal
     signature, so the pair stays 待签 and the relation is
     {(a,1), (a,2), (b,2)};
  3. the distribution desk (发放台) needs at least 2 stamps for it; one
     stamp per person is impossible at any stamp count, and a site budget
     of one stamp is refused — each refusal names the blocking pairs and
     the extra mispair they would force;
  4. the right side re-signs reg-b1 at revision 2 through the real consent
     column; all four pairs stand and one stamp {a,b}×{1,2} covers them;
  5. the two issue files are produced through real contracts, read back by
     the supplied materials/745/reader_client.py, and an independent
     witness proves the two lists rejoin to exactly the approved relation;
  6. a pair supported by two registrations survives withdrawing one of
     them and disappears only when the last support is withdrawn;
  7. the original 745 adapter output is still read by the original client.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extcontract import (  # noqa: E402
    PairingDesk,
    adapt_file_document,
    adapt_rows,
    issue_file_documents,
    load_contract,
    plan_distribution,
    verify_issued_files,
    write_records,
)

MAT = ROOT / "materials" / "pairing"
MAT745 = ROOT / "materials" / "745"
OUT = ROOT / "examples" / "out" / "pairing"


def contract(name, role):
    return load_contract(MAT / name, role=role)


def form(name):
    return json.loads((MAT / name).read_text(encoding="utf-8"))


def client(contract_path, records_path):
    proc = subprocess.run(
        [sys.executable, str(MAT745 / "reader_client.py"), str(contract_path), str(records_path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"reader_client failed: {proc.stderr}")
    return json.loads(proc.stdout)


def banner(n, title):
    print(f"\n=== {n}. {title} ===")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    left_reader = contract("desk-left-reader.json", "reader")
    right_reader = contract("desk-right-reader.json", "reader")
    left_writer = contract("left-form-writer.json", "writer")
    right_writer = contract("right-form-writer.json", "writer")
    right_writer_legacy = contract("right-form-writer-legacy.json", "writer")

    # 1. Receive both sides' consent forms ----------------------------------
    banner(1, "报名台: ingest both sides' consent forms (frozen versions "
           f"{left_reader.version!r} / {right_reader.version!r})")
    desk = PairingDesk(left_reader, right_reader)
    assert desk.ingest_left(left_writer, form("forms-left-1.json")).accepted
    assert desk.ingest_right(right_writer, form("forms-right-1.json")).accepted
    assert desk.ingest_right(right_writer_legacy, form("forms-right-legacy.json")).accepted

    report = desk.report()
    print("approved pairs:")
    for pair in report.approved_pairs:
        supporters = ", ".join(
            f"{s['registration']}(L{s['left_revision']}/R{s['right_revision']})"
            for s in pair["registrations"]
        )
        print(f"  group {pair['group']}: "
              f"{pair['left_member']!r} <-> {pair['right_member']!r}  supported by {supporters}")
    print("pending (consent from reader default, 待签):", report.pending_default)
    print("one-sided (缺另一方):", report.one_sided or "none")
    relation = desk.sign_out()
    print("signed-out relation:", relation)
    assert relation == {1: [("a", 1), ("a", 2), ("b", 2)]}
    assert report.pending_default == ["reg-b1"]

    # 2. Distribution trials on the three-pair relation ----------------------
    banner(2, "发放台: two stamps minimum; personal cards of one stamp cannot "
              "rejoin; a one-stamp site budget is refused")
    plan_right = plan_distribution(relation, prefer="right")
    print("prefer=right (按左端分办):", plan_right.stamp_count, "stamps, loads",
          plan_right.loads)
    assert plan_right.stamp_count == 2
    assert plan_right.loads["right"][2] == 2  # right end 2 carries two stamps
    plan_left = plan_distribution(relation, prefer="left")
    print("prefer=left  (按右端分办):", plan_left.stamp_count, "stamps, loads",
          plan_left.loads)
    assert plan_left.loads["left"]["a"] == 2  # left end a carries two stamps

    tight = plan_distribution(relation, left_slots=1, right_slots=1)
    print("one stamp per person:", tight.status)
    for reason in tight.reasons:
        print("   -", reason)
    assert tight.status == "INFEASIBLE" and tight.details["kind"] == "slot_limits"

    refused = plan_distribution(relation, total_stamps=1)
    print("site budget of one stamp:", refused.status)
    for reason in refused.reasons:
        print("   -", reason)
    assert refused.status == "INFEASIBLE" and refused.details["kind"] == "stamp_budget"
    conflict = refused.details["pairwise_conflicts"][0]
    assert conflict["would_add"] == [("b", 1)]

    # 3. The right side signs reg-b1 for real --------------------------------
    banner(3, "right side re-signs reg-b1 @rev2 with the real consent column")
    assert desk.ingest_right(right_writer, form("forms-right-2.json")).accepted
    relation = desk.sign_out()
    print("signed-out relation:", relation)
    assert relation == {1: [("a", 1), ("a", 2), ("b", 1), ("b", 2)]}

    # 4. Issue the two files --------------------------------------------------
    banner(4, "发放台: one stamp covers K(2,2); issue the two files")
    plan = plan_distribution(relation)
    print("plan:", plan.status, plan.stamp_count, "stamp(s), loads", plan.loads)
    assert plan.status == "ISSUED" and plan.stamp_count == 1

    il_writer = contract("issue-left-writer.json", "writer")
    il_reader = contract("issue-left-reader.json", "reader")
    ir_writer = contract("issue-right-writer.json", "writer")
    ir_reader = contract("issue-right-reader.json", "reader")
    left_doc, right_doc = issue_file_documents(
        plan,
        left_writer=il_writer,
        left_reader=il_reader,
        right_writer=ir_writer,
        right_reader=ir_reader,
    )
    left_records = adapt_file_document(il_writer, il_reader, left_doc).records
    right_records = adapt_file_document(ir_writer, ir_reader, right_doc).records
    left_path = OUT / "left-branch-records.json"
    right_path = OUT / "right-branch-records.json"
    write_records(left_records, str(left_path))
    write_records(right_records, str(right_path))
    il_contract_path = OUT / "issue-left-reader.json"
    ir_contract_path = OUT / "issue-right-reader.json"
    il_contract_path.write_text(json.dumps(il_reader.canonical_json(), ensure_ascii=False))
    ir_contract_path.write_text(json.dumps(ir_reader.canonical_json(), ensure_ascii=False))
    print("left branch file :", [r["values"] for r in left_records["rows"]])
    print("right branch file:", [r["values"] for r in right_records["rows"]])
    print("reader_client left :", client(il_contract_path, left_path))
    print("reader_client right:", client(ir_contract_path, right_path))

    witness = verify_issued_files(
        left_records, right_records, il_reader, ir_reader, relation
    )
    print("witness ok:", witness["ok"], "| rejoined exactly:", witness["equal"],
          "| stamps:", witness["stamp_count"])
    assert witness["ok"] and witness["equal"] and witness["stamp_count"] == 1
    witness_path = OUT / "rejoin-witness.json"
    witness_path.write_text(
        json.dumps(witness, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("witness written to", witness_path)

    # 5. A pair survives losing one of its two supports ----------------------
    banner(5, "two registrations support one pair; withdrawing one keeps it")
    desk2 = PairingDesk(left_reader, right_reader)
    assert desk2.ingest_left(left_writer, form("forms-left-x1.json")).accepted
    assert desk2.ingest_right(right_writer, form("forms-right-x1.json")).accepted
    assert desk2.sign_out() == {1: [("c", 7)]}
    assert desk2.ingest_left(left_writer, form("forms-left-x2.json")).accepted
    print("after withdrawing reg-x1:", desk2.sign_out())
    assert desk2.sign_out() == {1: [("c", 7)]}  # reg-x2 still supports it
    assert desk2.ingest_left(left_writer, form("forms-left-x3.json")).accepted
    print("after withdrawing reg-x2:", desk2.sign_out())
    assert desk2.sign_out() == {}

    # 6. The original client still reads the original adapter output ---------
    banner(6, "regression: original reader_client reads original 745 records")
    writer745 = load_contract(MAT745 / "writer-contract.json", role="writer")
    reader745 = load_contract(MAT745 / "reader-contract.json", role="reader")
    rows745 = json.loads((MAT745 / "input.json").read_text(encoding="utf-8"))["rows"]
    records745 = adapt_rows(writer745, reader745, rows745).records
    path745 = OUT / "base-745-records.json"
    write_records(records745, str(path745))
    totals = client(MAT745 / "reader-contract.json", path745)
    print("reader_client:", totals)
    assert totals == {"numeric_totals": {"amount": "-1"}, "row_count": 2}

    print("\nAll pairing demo assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
