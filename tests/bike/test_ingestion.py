"""Tests that every bike-show fact really flows through the exact-contract
ingestion machinery: contract safety, atomic files, revisions, and the
supplied external reader client.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from extcontract import check_compatibility, load_contract, write_records
from extcontract.bike import HandoverFiles

from conftest import FIX

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "materials" / "bike" / "contracts"
CLIENT = ROOT / "materials" / "745" / "reader_client.py"


def _compat(writer_file: str, reader_file: str):
    w = load_contract(CONTRACTS / writer_file, role="writer")
    r = load_contract(CONTRACTS / reader_file, role="reader")
    return check_compatibility(w, r)


def test_all_writer_reader_pairs_are_safe():
    pairs = [
        ("writer-bike-trip.json", "reader-handover.json"),
        ("writer-station-full.json", "reader-handover.json"),
        ("writer-station-half.json", "reader-handover.json"),
        ("writer-consent.json", "reader-consent.json"),
        ("writer-reservation.json", "reader-reservation.json"),
        ("writer-station-master.json", "reader-station-master.json"),
    ]
    for wf, rf in pairs:
        report = _compat(wf, rf)
        assert report.safe, (wf, rf, [c.reasons for c in report.columns if not c.safe])


def test_half_station_default_fill_is_in_adaptation_plan():
    report = _compat("writer-station-half.json", "reader-handover.json")
    station_plan = next(c for c in report.columns if c.field_id == "station_sign")
    assert station_plan.source == "default"
    assert station_plan.default_encoded == "good"
    assert "fill_default" in station_plan.conversions


def test_ingested_records_pass_real_reader_client(half_files, tmp_path):
    records = half_files.handover.publish()
    path = tmp_path / "handover.json"
    write_records(records, path)
    proc = subprocess.run(
        [sys.executable, str(CLIENT), str(CONTRACTS / "reader-handover.json"), str(path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    # B2 exit + B2 return (bike & station sides) + B3 exit two sides
    assert result["row_count"] == 5


def test_correction_is_a_higher_revision_not_a_duplicate(corrected_files):
    state = corrected_files.handover.get_row("B2-return-station")
    assert state.revision == 2
    assert state.encoded["dock_idx"] == "1"
    # the real signature survived; it is not a default fill at revision 2
    assert state.sources["station_sign"]["conversion"] != "fill_default"


def test_same_file_id_different_content_is_rejected(half_files):
    doc = json.loads((FIX / "F-HALF-bike.json").read_text())
    # tamper with content but keep the file_id
    doc["rows"][0]["values"]["tick"] = 7
    result = half_files.ingest_handover("writer-bike-trip.json", _temp(doc))
    assert not result.accepted
    assert "already accepted with different" in result.errors[0]


def test_identical_resubmission_is_a_replay(half_files):
    doc = json.loads((FIX / "F-CONSENT.json").read_text())
    first = half_files.ingest_consent(_temp(doc))
    second = half_files.ingest_consent(_temp(doc))
    assert first.accepted and second.accepted
    assert second.receipt["replay"] is True


def test_invalid_row_rejects_whole_file_atomically():
    files = HandoverFiles()
    doc = {
        "file_id": "F-BAD-MASTER",
        "writer_version": "station-master-writer-v1",
        "reader_version": "station-master-reader-v1",
        "rows": [
            {"row_id": "ok", "values": {"station": "A", "dock_idx": 0,
                                        "bike_id": None, "status": "empty"}},
            {"row_id": "bad", "values": {"station": "A", "dock_idx": 9999,
                                         "bike_id": None, "status": "empty"}},
        ],
    }
    result = files.ingest_master(_temp(doc))
    assert not result.accepted
    # atomicity: the good row never lands either
    assert files.master.row_count() == 0


def test_reservation_ingestion_is_typed(half_files):
    reservations = half_files.reservation_list()
    jia = next(r for r in reservations if r.ride_no == "jia")
    assert jia.pickup_tick == 0 and jia.return_tick == 2
    assert jia.return_station == "A"


def _temp(doc: dict) -> Path:
    # simple module-level temp file helper using a fixed location
    path = Path("/tmp") / f"bike-tmp-{abs(hash(json.dumps(doc, sort_keys=True)))}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path
