"""Tests for the registration desk (报名台): two-sided consent ingestion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from extcontract import (
    CONSENT_AGREE,
    CONSENT_WITHDRAW,
    PairingDesk,
    load_contract,
)

ROOT = Path(__file__).resolve().parents[1]
MAT = ROOT / "materials" / "pairing"


def _contract(name, role):
    return load_contract(MAT / name, role=role)


def _form(name):
    return json.loads((MAT / name).read_text(encoding="utf-8"))


@pytest.fixture()
def contracts():
    return {
        "left_reader": _contract("desk-left-reader.json", "reader"),
        "right_reader": _contract("desk-right-reader.json", "reader"),
        "left_writer": _contract("left-form-writer.json", "writer"),
        "right_writer": _contract("right-form-writer.json", "writer"),
        "right_writer_legacy": _contract("right-form-writer-legacy.json", "writer"),
    }


@pytest.fixture()
def desk(contracts):
    return PairingDesk(contracts["left_reader"], contracts["right_reader"])


def left_file(file_id, rows):
    return {
        "file_id": file_id,
        "writer_version": "left-form-w1",
        "reader_version": "pairdesk-left-r1",
        "rows": rows,
    }


def right_file(file_id, rows, writer_version="right-form-w1"):
    return {
        "file_id": file_id,
        "writer_version": writer_version,
        "reader_version": "pairdesk-right-r1",
        "rows": rows,
    }


def left_row(reg, member, group=1, round=1, consent=CONSENT_AGREE, revision=1):
    return {
        "row_id": reg,
        "revision": revision,
        "values": {
            "member_no": member,
            "group_no": group,
            "round": round,
            "consent": consent,
        },
    }


def right_row(reg, member, group=1, round=1, consent=CONSENT_AGREE, revision=1):
    row = {
        "row_id": reg,
        "revision": revision,
        "values": {
            "member_no": member,
            "group_no": group,
            "round": round,
            "consent": consent,
        },
    }
    if consent is None:
        del row["values"]["consent"]
    return row


# ---------------------------------------------------------------------------
# approval and the consent provenance rule
# ---------------------------------------------------------------------------


def test_pair_approved_only_when_both_sides_stand(desk, contracts):
    assert desk.ingest_left(
        contracts["left_writer"], left_file("L1", [left_row("reg-1", "a")])
    ).accepted
    # Right side has not filed: no pair, left row is one-sided.
    assert desk.sign_out() == {}
    assert desk.report().one_sided == ["reg-1"]

    assert desk.ingest_right(
        contracts["right_writer"], right_file("R1", [right_row("reg-1", 5)])
    ).accepted
    assert desk.sign_out() == {1: [("a", 5)]}
    report = desk.report()
    assert report.one_sided == []
    assert report.approved_pairs[0]["registrations"] == [
        {"registration": "reg-1", "left_revision": 1, "right_revision": 1}
    ]


def test_default_filled_consent_is_not_a_signature(desk, contracts):
    """缺列补出的同意字样不能充当本人签认: the legacy right writer omits the
    consent column, the reader default fills 同意, and the pair stays 待签."""
    assert desk.ingest_left(
        contracts["left_writer"], left_file("L1", [left_row("reg-b1", "b")])
    ).accepted
    legacy = right_file(
        "R0", [right_row("reg-b1", 1, consent=None)], writer_version="right-form-w0"
    )
    assert desk.ingest_right(contracts["right_writer_legacy"], legacy).accepted

    assert desk.sign_out() == {}
    report = desk.report()
    assert report.pending_default == ["reg-b1"]
    view = report.registrations[0]
    assert view.right.consent == CONSENT_AGREE  # the row *shows* 同意 ...
    assert view.right.consent_source == "default"  # ... but it is the default
    assert view.left.consent_source == "writer"

    # A new revision through the real consent column signs the pair.
    assert desk.ingest_right(
        contracts["right_writer"], right_file("R2", [right_row("reg-b1", 1, revision=2)])
    ).accepted
    assert desk.sign_out() == {1: [("b", 1)]}
    assert desk.report().pending_default == []


def test_explicit_withdrawal_never_stands(desk, contracts):
    assert desk.ingest_left(
        contracts["left_writer"], left_file("L1", [left_row("reg-1", "a")])
    ).accepted
    assert desk.ingest_right(
        contracts["right_writer"],
        right_file("R1", [right_row("reg-1", 5, consent=CONSENT_WITHDRAW)]),
    ).accepted
    assert desk.sign_out() == {}
    assert desk.report().withdrawn == ["reg-1"]


def test_other_consent_values_do_not_stand(desk, contracts):
    assert desk.ingest_left(
        contracts["left_writer"],
        left_file("L1", [left_row("reg-1", "a", consent="考虑中")]),
    ).accepted
    assert desk.ingest_right(
        contracts["right_writer"], right_file("R1", [right_row("reg-1", 5)])
    ).accepted
    assert desk.sign_out() == {}
    assert desk.report().not_signed == ["reg-1"]


# ---------------------------------------------------------------------------
# merging discipline
# ---------------------------------------------------------------------------


def test_no_splicing_of_two_registrations(desk, contracts):
    """不能拿两笔同组报名的左右端拼一项: a left-only registration and a
    right-only registration in the same group never form a pair."""
    assert desk.ingest_left(
        contracts["left_writer"], left_file("L1", [left_row("reg-p", "m")])
    ).accepted
    assert desk.ingest_right(
        contracts["right_writer"], right_file("R1", [right_row("reg-q", 9)])
    ).accepted
    assert desk.sign_out() == {}
    assert sorted(desk.report().one_sided) == ["reg-p", "reg-q"]


def test_same_round_and_same_group_required(desk, contracts):
    assert desk.ingest_left(
        contracts["left_writer"],
        left_file("L1", [left_row("reg-r", "x", group=1, round=1)]),
    ).accepted
    assert desk.ingest_right(
        contracts["right_writer"],
        right_file("R1", [right_row("reg-r", 4, group=1, round=2)]),
    ).accepted
    assert desk.sign_out() == {}
    report = desk.report()
    assert report.mismatched == ["reg-r"]
    assert "round_mismatch" in report.registrations[0].reasons

    # Matching round in a new revision merges the pair.
    assert desk.ingest_right(
        contracts["right_writer"],
        right_file("R2", [right_row("reg-r", 4, group=1, round=1, revision=2)]),
    ).accepted
    assert desk.sign_out() == {1: [("x", 4)]}


def test_group_mismatch_blocks(desk, contracts):
    assert desk.ingest_left(
        contracts["left_writer"], left_file("L1", [left_row("reg-g", "x", group=1)])
    ).accepted
    assert desk.ingest_right(
        contracts["right_writer"], right_file("R1", [right_row("reg-g", 4, group=2)])
    ).accepted
    assert desk.sign_out() == {}
    assert "group_mismatch" in desk.report().registrations[0].reasons


# ---------------------------------------------------------------------------
# multiple supports and withdrawal
# ---------------------------------------------------------------------------


def test_pair_survives_until_last_support_withdrawn(desk, contracts):
    """两笔同对报名撤一份后仍保留; 最后一份支持消失时才撤出."""
    for file_id, reg in (("L1", "reg-x1"), ("L2", "reg-x2")):
        assert desk.ingest_left(
            contracts["left_writer"], left_file(file_id, [left_row(reg, "c")])
        ).accepted
    for file_id, reg in (("R1", "reg-x1"), ("R2", "reg-x2")):
        assert desk.ingest_right(
            contracts["right_writer"], right_file(file_id, [right_row(reg, 7)])
        ).accepted

    report = desk.report()
    assert report.approved_pairs[0]["support_count"] == 2
    assert desk.sign_out() == {1: [("c", 7)]}

    # Withdraw one support on the left side: the pair is retained.
    assert desk.ingest_left(
        contracts["left_writer"],
        left_file("L3", [left_row("reg-x1", "c", consent=CONSENT_WITHDRAW, revision=2)]),
    ).accepted
    assert desk.sign_out() == {1: [("c", 7)]}
    assert desk.report().approved_pairs[0]["registrations"] == [
        {"registration": "reg-x2", "left_revision": 1, "right_revision": 1}
    ]

    # Withdraw the last support: the pair leaves the relation.
    assert desk.ingest_left(
        contracts["left_writer"],
        left_file("L4", [left_row("reg-x2", "c", consent=CONSENT_WITHDRAW, revision=2)]),
    ).accepted
    assert desk.sign_out() == {}


def test_re_sign_after_withdrawal(desk, contracts):
    assert desk.ingest_left(
        contracts["left_writer"], left_file("L1", [left_row("reg-1", "a")])
    ).accepted
    assert desk.ingest_right(
        contracts["right_writer"], right_file("R1", [right_row("reg-1", 5)])
    ).accepted
    assert desk.sign_out() == {1: [("a", 5)]}
    assert desk.ingest_left(
        contracts["left_writer"],
        left_file("L2", [left_row("reg-1", "a", consent=CONSENT_WITHDRAW, revision=2)]),
    ).accepted
    assert desk.sign_out() == {}
    assert desk.ingest_left(
        contracts["left_writer"],
        left_file("L3", [left_row("reg-1", "a", revision=3)]),
    ).accepted
    assert desk.sign_out() == {1: [("a", 5)]}


# ---------------------------------------------------------------------------
# interpretation of member numbers and versions
# ---------------------------------------------------------------------------


def test_members_keyed_by_canonical_reader_domain(desk, contracts):
    """成员号按稳定字段及真实读者域解释: the right member is the canonical
    INT16 value (widened from the writer's INT8), not a transport spelling."""
    assert desk.ingest_left(
        contracts["left_writer"], left_file("L1", [left_row("reg-1", "a")])
    ).accepted
    assert desk.ingest_right(
        contracts["right_writer"], right_file("R1", [right_row("reg-1", 1)])
    ).accepted
    relation = desk.sign_out()
    (left_member, right_member), = relation[1]
    assert left_member == "a" and isinstance(left_member, str)
    assert right_member == 1 and isinstance(right_member, int)


def test_display_names_do_not_matter(contracts):
    """The writer's display names (会员号/展组号/轮次/签认) differ from the
    reader's; pairing is by stable field_id, so ingestion still works."""
    desk = PairingDesk(contracts["left_reader"], contracts["right_reader"])
    assert desk.ingest_left(
        contracts["left_writer"], left_file("L1", [left_row("reg-1", "a")])
    ).accepted
    assert desk.ingest_right(
        contracts["right_writer"], right_file("R1", [right_row("reg-1", 1)])
    ).accepted
    assert desk.sign_out() == {1: [("a", 1)]}


def test_report_pins_frozen_versions(desk, contracts):
    report = desk.report()
    assert report.left_version == "pairdesk-left-r1"
    assert report.right_version == "pairdesk-right-r1"
    assert report.to_json()["left_version"] == "pairdesk-left-r1"


def test_ingestion_is_atomic_and_validated(desk, contracts):
    """The original entry point's rules still apply inside the desk."""
    bad = left_file("LBAD", [left_row("reg-1", "a", consent="同意"), {"row_id": "reg-2", "values": {"member_no": "x", "group_no": 1, "round": 1, "consent": 5}}])
    result = desk.ingest_left(contracts["left_writer"], bad)
    assert not result.accepted
    assert desk.sign_out() == {}
    assert desk.left_library.get_row("reg-1") is None


def test_mandated_scenario_from_files(desk, contracts):
    """The brief's example: (a,1),(a,2),(b,2) approved; (b,1) pending."""
    assert desk.ingest_left(contracts["left_writer"], _form("forms-left-1.json")).accepted
    assert desk.ingest_right(contracts["right_writer"], _form("forms-right-1.json")).accepted
    assert desk.ingest_right(
        contracts["right_writer_legacy"], _form("forms-right-legacy.json")
    ).accepted
    assert desk.sign_out() == {1: [("a", 1), ("a", 2), ("b", 2)]}
    assert desk.report().pending_default == ["reg-b1"]
    assert desk.ingest_right(contracts["right_writer"], _form("forms-right-2.json")).accepted
    assert desk.sign_out() == {1: [("a", 1), ("a", 2), ("b", 1), ("b", 2)]}
