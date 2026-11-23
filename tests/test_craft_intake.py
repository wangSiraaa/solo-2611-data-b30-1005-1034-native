"""试烧来单接入口测试：旧 extcontract 摄入、修订重发、固定读者、原子拒绝。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from craftbench import build_voucher, load_scenario, plan_work_order
from craftbench.intake import TrialIntake, make_trial_document, proof_row_id
from extcontract import write_records

CRAFT = Path("materials/craftbench")
FIX = Path("fixtures/craftbench")


def _client(records_path: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(CRAFT / "reader_client.py"),
         str(CRAFT / "trial-reader-contract.json"), str(records_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_base_then_revision_through_old_interface(tmp_path=None):
    intake = TrialIntake()
    base = json.loads((FIX / "trial-base.json").read_text(encoding="utf-8"))
    r1 = intake.ingest_file(base)
    assert r1.accepted and r1.receipt["status"] == "ACCEPTED"

    revision = json.loads((FIX / "trial-revision.json").read_text(encoding="utf-8"))
    r2 = intake.ingest_file(revision)
    assert r2.accepted
    assert r2.receipt["applied"][0]["action"] == "supersede"
    row = intake.library.get_row(proof_row_id("small", "sm1", "w"))
    assert row.revision == 2
    assert row.canonical["observed_lo"] == row.canonical["observed_hi"] == 5


def test_published_records_pass_real_fixed_reader(tmp_path=None):
    out = Path("/tmp/craft-trial-records.json")
    intake = TrialIntake()
    intake.ingest_file(json.loads((FIX / "trial-base.json").read_text(encoding="utf-8")))
    write_records(intake.library.publish(), out)
    result = _client(out)
    assert result["row_count"] == 1
    assert result["numeric_totals"]["observed_lo"] == "4"


def test_same_revision_conflict_rejects_whole_file_atomically():
    intake = TrialIntake()
    intake.ingest_file(json.loads((FIX / "trial-base.json").read_text(encoding="utf-8")))
    # 同 revision 1 但改了值 => 整单拒绝，目标状态不动。
    clash = {
        "file_id": "clash",
        "writer_version": "tw1",
        "reader_version": "tr1",
        "rows": [{
            "row_id": proof_row_id("small", "sm1", "w"),
            "revision": 1,
            "values": {
                "style_id": "S", "piece_id": "small", "sample_id": "sm1",
                "direction": "w", "green_dim": "5",
                "observed_lo": "3", "observed_hi": "3", "signed": 1,
            },
        }],
    }
    result = intake.ingest_file(clash)
    assert not result.accepted
    row = intake.library.get_row(proof_row_id("small", "sm1", "w"))
    assert row.canonical["observed_lo"] == 4  # 维持原值


def test_replay_same_file_returns_original_receipt():
    intake = TrialIntake()
    doc = json.loads((FIX / "trial-base.json").read_text(encoding="utf-8"))
    first = intake.ingest_file(doc)
    again = intake.ingest_file(doc)
    assert again.accepted and again.receipt["replay"] is True
    assert again.receipt["sequence"] == first.receipt["sequence"]


def test_unsigned_row_is_placeholder_and_does_not_constrain():
    intake = TrialIntake()
    doc = make_trial_document(
        "placeholder",
        [{
            "style_id": "S", "piece_id": "small", "sample_id": "ghost",
            "direction": "d", "green_dim": 5, "observed": None,
            "signed": False, "revision": 1,
        }],
    )
    result = intake.ingest_file(doc)
    assert result.accepted
    proofs = intake.published_proofs()
    ghost = next(p for p in proofs if p.sample_id == "ghost")
    assert ghost.is_signed is False

    sc = load_two_sleeve_with(intake)
    voucher = build_voucher(sc)
    # 占位纵向观察没有排除任何组。
    assert voucher.surviving_pairs("S") == [0, 1]
    assert any(p["sample_id"] == "ghost"
               for p in voucher.style_verdicts["S"].ignored_placeholders)


def test_illegal_writer_value_rejected_by_contract_domain():
    # green_dim 是 DECIMAL(12,4)：分母消不掉的分数无法以该刻度精确表示，
    # 写端域拒绝，整单不收（绝不四舍五入）。
    intake = TrialIntake()
    doc = make_trial_document(
        "bad-domain",
        [{
            "style_id": "S", "piece_id": "small", "sample_id": "z",
            "direction": "w", "green_dim": "1/3", "observed": [5, 5],
            "signed": True, "revision": 1,
        }],
    )
    result = intake.ingest_file(doc)
    assert not result.accepted
    assert any("scale" in e or "DECIMAL" in e for e in result.errors)
    assert intake.library.row_count() == 0


def test_float_measurement_refused_at_document_builder():
    from craftbench.errors import CraftError
    with pytest.raises(CraftError):
        make_trial_document(
            "float",
            [{
                "style_id": "S", "piece_id": "small", "sample_id": "z",
                "direction": "w", "green_dim": 5, "observed": [0.8, 1.0],
                "signed": True, "revision": 1,
            }],
        )


def test_two_work_orders_before_and_after_revision():
    # 端到端：两张各自可兑现的工单，旧单不被修订改写。
    intake = TrialIntake()
    scenario = load_scenario(FIX / "two-sleeve-base.json")

    intake.ingest_file(json.loads((FIX / "trial-base.json").read_text(encoding="utf-8")))
    first = plan_work_order(intake.apply_to_scenario(scenario),
                            build_voucher(intake.apply_to_scenario(scenario)))
    assert (first.box_count, first.rack_fee) == (1, 2)

    intake.ingest_file(json.loads((FIX / "trial-revision.json").read_text(encoding="utf-8")))
    second = plan_work_order(intake.apply_to_scenario(scenario),
                             build_voucher(intake.apply_to_scenario(scenario)))
    assert (second.box_count, second.rack_fee) == (1, 0)
    assert (first.box_count, first.rack_fee) == (1, 2)  # 旧工单不变


def load_two_sleeve_with(intake: TrialIntake):
    from craftbench import load_scenario
    return intake.apply_to_scenario(load_scenario(FIX / "two-sleeve-base.json"))
