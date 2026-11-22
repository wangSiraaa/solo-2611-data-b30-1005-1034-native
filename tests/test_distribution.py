"""Tests for the distribution desk (发放台): minimum-stamp issue planning."""

from __future__ import annotations

import pytest

from extcontract import (
    plan_distribution,
    verify_rejoin,
)
from extcontract.errors import ContractError, LimitError

# The brief's worked example: {(a,1), (a,2), (b,2)} in one group.
EXAMPLE = {1: [("a", 1), ("a", 2), ("b", 2)]}
FULL = {1: [("a", 1), ("a", 2), ("b", 1), ("b", 2)]}


def test_minimum_two_stamps_either_preference():
    plan = plan_distribution(EXAMPLE, prefer="right")
    assert plan.status == "ISSUED"
    assert plan.stamp_count == 2
    # 按左端分办: right end 2 carries both stamps.
    assert plan.loads["right"] == {1: 1, 2: 2}
    assert plan.loads["left"] == {"a": 1, "b": 1}

    plan = plan_distribution(EXAMPLE, prefer="left")
    assert plan.status == "ISSUED"
    assert plan.stamp_count == 2
    # 按右端分办: left end a carries both stamps.
    assert plan.loads["left"] == {"a": 2, "b": 1}
    assert plan.loads["right"] == {1: 1, 2: 1}


def test_full_square_needs_one_stamp():
    plan = plan_distribution(FULL)
    assert plan.status == "ISSUED"
    assert plan.stamp_count == 1
    stamp = plan.stamps[0]
    assert stamp.lefts == ("a", "b") and stamp.rights == (1, 2)
    assert plan.loads["left"] == {"a": 1, "b": 1}
    assert plan.loads["right"] == {1: 1, 2: 1}
    assert plan.details["witness"]["ok"]


def test_one_stamp_per_person_is_impossible():
    """各人最多一枚无法无损复联 — at any stamp count."""
    plan = plan_distribution(EXAMPLE, left_slots=1, right_slots=1)
    assert plan.status == "INFEASIBLE"
    assert plan.details["kind"] == "slot_limits"
    assert plan.details["min_stamps"] == 2
    text = "\n".join(plan.reasons)
    assert "'a'" in text and "2" in text  # names the overloaded members
    # Every minimum-stamp attempt is listed with its violations.
    assert plan.details["attempts"]
    for attempt in plan.details["attempts"]:
        assert attempt["violations"]


def test_one_stamp_budget_is_refused_with_mispair_witness():
    """维持原登记、只把可刻章总数改为一枚则拒发."""
    plan = plan_distribution(EXAMPLE, total_stamps=1)
    assert plan.status == "INFEASIBLE"
    assert plan.details["kind"] == "stamp_budget"
    assert plan.details["min_stamps"] == 2
    assert plan.details["budget"] == 1
    conflicts = plan.details["pairwise_conflicts"]
    assert conflicts
    # (a,1) and (b,2) cannot share a stamp: it would add the unapproved (b,1).
    assert conflicts[0]["would_add"] == [("b", 1)]
    assert ("a", 1) in conflicts[0]["pairs"] and ("b", 2) in conflicts[0]["pairs"]


def test_slots_may_be_tightened_per_person():
    # Tighten only right member 2: the left-star plan is blocked but the
    # right-star plan still works with two stamps.
    plan = plan_distribution(EXAMPLE, right_slots={2: 1})
    assert plan.status == "ISSUED"
    assert plan.stamp_count == 2
    assert plan.loads["right"][2] == 1
    assert plan.loads["left"]["a"] == 2


def test_tight_slots_can_raise_the_minimum_stamp_count():
    """调紧个人可携数后，最少可行章数可以高于自由最少章数."""
    chain = {1: [("a", 1), ("a", 2), ("b", 2), ("b", 3)]}
    free = plan_distribution(chain)
    assert free.stamp_count == 2 and free.loads["right"][2] == 2
    tight = plan_distribution(chain, right_slots={2: 1})
    assert tight.status == "ISSUED"
    assert tight.stamp_count == 3  # one more stamp is the only way
    assert tight.loads["right"][2] == 1
    assert tight.details["witness"]["ok"]


def test_personal_card_counts_across_groups():
    """同一馆同一人的章位跨组合计: one person in two groups shares one card."""
    relation = {1: [("a", 1)], 2: [("a", 9)]}
    plan = plan_distribution(relation, left_slots=1)
    assert plan.status == "INFEASIBLE"
    assert plan.details["kind"] == "slot_limits"

    plan = plan_distribution(relation, left_slots=2)
    assert plan.status == "ISSUED"
    assert plan.stamp_count == 2
    assert plan.loads["left"] == {"a": 2}
    # 刻给一组的章不能印给另一组: stamps are numbered per group.
    assert {(s.group, s.stamp_no) for s in plan.stamps} == {(1, 1), (2, 1)}


def test_site_budget_sums_across_groups():
    relation = {1: [("a", 1)], 2: [("b", 2)]}
    plan = plan_distribution(relation, total_stamps=1)
    assert plan.status == "INFEASIBLE"
    assert plan.details["kind"] == "stamp_budget"
    assert plan.details["min_stamps"] == 2
    plan = plan_distribution(relation, total_stamps=2)
    assert plan.status == "ISSUED" and plan.stamp_count == 2


def test_witness_rejoin_exact_and_tamper_evident():
    plan = plan_distribution(EXAMPLE)
    witness = verify_rejoin(plan.left_rows, plan.right_rows, EXAMPLE)
    assert witness["ok"] and witness["equal"]
    assert witness["missing_pairs"] == [] and witness["extra_pairs"] == []
    assert witness["stamp_count"] == 2

    # A dropped row loses a pair; an added row invents one.
    dropped = verify_rejoin(plan.left_rows, plan.right_rows[:-1], EXAMPLE)
    assert not dropped["ok"] and dropped["missing_pairs"]
    added = verify_rejoin(
        plan.left_rows + [(1, "b", 9)], plan.right_rows + [(1, 1, 9)], EXAMPLE
    )
    assert not added["ok"]
    assert added["extra_pairs"] == [[1, "b", 1]]


def test_witness_merges_duplicate_hits():
    """重复命中归一: duplicated rows in a file merge harmlessly."""
    plan = plan_distribution(FULL)
    witness = verify_rejoin(
        plan.left_rows + plan.left_rows, plan.right_rows + plan.right_rows, FULL
    )
    assert witness["ok"] and witness["equal"]
    assert witness["duplicate_rows_merged"] == len(plan.left_rows) + len(
        plan.right_rows
    )


def test_witness_checks_slots_and_budget():
    plan = plan_distribution(EXAMPLE, prefer="right")  # right member 2 carries 2
    witness = verify_rejoin(
        plan.left_rows, plan.right_rows, EXAMPLE, right_slots={2: 1}
    )
    assert not witness["ok"] and witness["slot_violations"]
    witness = verify_rejoin(
        plan.left_rows, plan.right_rows, EXAMPLE, total_stamps=1
    )
    assert not witness["ok"] and not witness["budget_ok"]


def test_envelope_limits():
    with pytest.raises(LimitError):  # more than two groups
        plan_distribution({1: [("a", 1)], 2: [("b", 2)], 3: [("c", 3)]})
    with pytest.raises(LimitError):  # six left ends
        plan_distribution({1: [(f"l{i}", 1) for i in range(6)]})
    with pytest.raises(LimitError):  # six right ends
        plan_distribution({1: [("a", i) for i in range(6)]})
    with pytest.raises(LimitError):  # thirteen true pairs
        plan_distribution({1: [(l, r) for l in "abc" for r in (1, 2, 3, 4, 5)][:13]})


def test_malformed_inputs_rejected():
    with pytest.raises(ContractError):
        plan_distribution(EXAMPLE, prefer="middle")
    with pytest.raises(ContractError):
        plan_distribution(EXAMPLE, left_slots=-1)
    with pytest.raises(ContractError):
        plan_distribution(EXAMPLE, left_slots={"nobody": 1})
    with pytest.raises(ContractError):
        plan_distribution(EXAMPLE, total_stamps=-1)
    with pytest.raises(ContractError):
        plan_distribution({1: [("a", 1, 2)]})
    with pytest.raises(ContractError):
        plan_distribution([("a", 1)])
    with pytest.raises(ContractError):
        verify_rejoin([(1, "a")], [(1, 1, 1)], EXAMPLE)


def test_empty_relation_issues_zero_stamps():
    plan = plan_distribution({})
    assert plan.status == "ISSUED"
    assert plan.stamp_count == 0


def test_no_virtual_double_printing():
    """不可把一枚章虚印成两个章号: the witness flags two stamps in one group
    with identical member sets."""
    left_rows = [(1, "a", 1), (1, "a", 2)]
    right_rows = [(1, 5, 1), (1, 5, 2)]
    witness = verify_rejoin(left_rows, right_rows, {1: [("a", 5)]})
    assert witness["duplicate_stamps"]
    assert not witness["ok"]


def test_search_handles_dense_and_chain_shapes():
    # Chain of 5 pairs needs 3 stamps (path graph biclique cover).
    chain = {1: [("a", 1), ("b", 1), ("b", 2), ("c", 2), ("c", 3)]}
    plan = plan_distribution(chain)
    assert plan.status == "ISSUED"
    assert plan.stamp_count == 3
    assert plan.details["witness"]["ok"]

    # 12-pair relation at the envelope edge, two groups.
    big = {
        1: [("a", 1), ("a", 2), ("a", 3), ("b", 1), ("b", 2), ("c", 3)],
        2: [("x", 4), ("x", 5), ("y", 4), ("y", 5), ("z", 5), ("z", 6)],
    }
    plan = plan_distribution(big, total_stamps=6)
    assert plan.status == "ISSUED"
    assert plan.details["witness"]["ok"]
    assert plan.stamp_count <= 6
