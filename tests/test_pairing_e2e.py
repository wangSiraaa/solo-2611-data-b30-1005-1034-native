"""End-to-end test of the joint-exhibition pairing service.

Drives the brief's scenario through real writer/reader contracts and files:
consent ingestion on the original entry point, the default-consent trap,
re-signing, minimum-stamp planning, issuing the two branch files, reading
them with the supplied materials/745/reader_client.py, and the withdrawal
rule.  Also re-checks that the original 745 adapter output is still
readable by the original client.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from extcontract import (
    PairingDesk,
    adapt_file_document,
    adapt_rows,
    issue_file_documents,
    load_contract,
    plan_distribution,
    verify_issued_files,
    write_records,
)

ROOT = Path(__file__).resolve().parents[1]
MAT = ROOT / "materials" / "pairing"
MAT745 = ROOT / "materials" / "745"
CLIENT = MAT745 / "reader_client.py"


def _contract(name, role):
    return load_contract(MAT / name, role=role)


def _form(name):
    return json.loads((MAT / name).read_text(encoding="utf-8"))


def _client(contract_path, records_path):
    proc = subprocess.run(
        [sys.executable, str(CLIENT), str(contract_path), str(records_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture()
def contracts():
    return {
        name: _contract(f"{name}.json", role)
        for name, role in [
            ("desk-left-reader", "reader"),
            ("desk-right-reader", "reader"),
            ("left-form-writer", "writer"),
            ("right-form-writer", "writer"),
            ("right-form-writer-legacy", "writer"),
            ("issue-left-writer", "writer"),
            ("issue-left-reader", "reader"),
            ("issue-right-writer", "writer"),
            ("issue-right-reader", "reader"),
        ]
    }


@pytest.fixture()
def desk(contracts):
    return PairingDesk(contracts["desk-left-reader"], contracts["desk-right-reader"])


def _ingest_mandated_forms(desk, contracts):
    assert desk.ingest_left(contracts["left-form-writer"], _form("forms-left-1.json")).accepted
    assert desk.ingest_right(
        contracts["right-form-writer"], _form("forms-right-1.json")
    ).accepted
    assert desk.ingest_right(
        contracts["right-form-writer-legacy"], _form("forms-right-legacy.json")
    ).accepted


def test_mandated_scenario_end_to_end(desk, contracts, tmp_path):
    _ingest_mandated_forms(desk, contracts)

    # Three pairs approved; (b,1) pending because its 同意 is the reader's
    # own default for a missing column, not a personal signature.
    relation = desk.sign_out()
    assert relation == {1: [("a", 1), ("a", 2), ("b", 2)]}

    # With ample cards at least two stamps are needed, on either side.
    assert plan_distribution(relation, prefer="left").stamp_count == 2
    assert plan_distribution(relation, prefer="right").stamp_count == 2
    # One stamp per person cannot losslessly rejoin, at any stamp count.
    tight = plan_distribution(relation, left_slots=1, right_slots=1)
    assert tight.status == "INFEASIBLE"
    assert tight.details["kind"] == "slot_limits"
    # Keeping the original registration and allowing only one stamp refuses.
    refused = plan_distribution(relation, total_stamps=1)
    assert refused.status == "INFEASIBLE"
    assert refused.details["kind"] == "stamp_budget"
    assert refused.details["pairwise_conflicts"][0]["would_add"] == [("b", 1)]

    # The right side re-signs reg-b1 with the real consent column: four
    # pairs, one stamp.
    assert desk.ingest_right(
        contracts["right-form-writer"], _form("forms-right-2.json")
    ).accepted
    relation = desk.sign_out()
    assert relation == {1: [("a", 1), ("a", 2), ("b", 1), ("b", 2)]}
    plan = plan_distribution(relation)
    assert plan.status == "ISSUED" and plan.stamp_count == 1

    # Issue the two files through real contracts; the real client reads them.
    left_doc, right_doc = issue_file_documents(
        plan,
        left_writer=contracts["issue-left-writer"],
        left_reader=contracts["issue-left-reader"],
        right_writer=contracts["issue-right-writer"],
        right_reader=contracts["issue-right-reader"],
    )
    left_result = adapt_file_document(
        contracts["issue-left-writer"], contracts["issue-left-reader"], left_doc
    )
    right_result = adapt_file_document(
        contracts["issue-right-writer"], contracts["issue-right-reader"], right_doc
    )
    assert left_result.status == right_result.status == "ADAPTED"

    left_path = tmp_path / "left-records.json"
    right_path = tmp_path / "right-records.json"
    write_records(left_result.records, str(left_path))
    write_records(right_result.records, str(right_path))
    left_contract_path = tmp_path / "issue-left-reader.json"
    right_contract_path = tmp_path / "issue-right-reader.json"
    left_contract_path.write_text(
        json.dumps(contracts["issue-left-reader"].canonical_json(), ensure_ascii=False)
    )
    right_contract_path.write_text(
        json.dumps(contracts["issue-right-reader"].canonical_json(), ensure_ascii=False)
    )
    assert _client(left_contract_path, left_path)["row_count"] == 2
    assert _client(right_contract_path, right_path)["row_count"] == 2

    # The witness over the emitted files proves the exact rejoin.
    witness = verify_issued_files(
        left_result.records,
        right_result.records,
        contracts["issue-left-reader"],
        contracts["issue-right-reader"],
        relation,
    )
    assert witness["ok"] and witness["equal"] and witness["stamp_count"] == 1
    assert witness["missing_pairs"] == [] and witness["extra_pairs"] == []


def test_pair_survives_until_last_support_withdrawn(desk, contracts):
    assert desk.ingest_left(contracts["left-form-writer"], _form("forms-left-x1.json")).accepted
    assert desk.ingest_right(
        contracts["right-form-writer"], _form("forms-right-x1.json")
    ).accepted
    assert desk.sign_out() == {1: [("c", 7)]}
    assert desk.report().approved_pairs[0]["support_count"] == 2

    assert desk.ingest_left(contracts["left-form-writer"], _form("forms-left-x2.json")).accepted
    assert desk.sign_out() == {1: [("c", 7)]}  # one support remains

    assert desk.ingest_left(contracts["left-form-writer"], _form("forms-left-x3.json")).accepted
    assert desk.sign_out() == {}


def test_original_client_still_reads_original_adapter_files(tmp_path):
    writer = load_contract(MAT745 / "writer-contract.json", role="writer")
    reader = load_contract(MAT745 / "reader-contract.json", role="reader")
    rows = json.loads((MAT745 / "input.json").read_text(encoding="utf-8"))["rows"]
    records = adapt_rows(writer, reader, rows).records
    path = tmp_path / "records.json"
    write_records(records, str(path))
    assert _client(MAT745 / "reader-contract.json", path) == {
        "numeric_totals": {"amount": "-1"},
        "row_count": 2,
    }
