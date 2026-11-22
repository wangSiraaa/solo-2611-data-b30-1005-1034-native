"""Fictional print shop service: plate registration, imposition work orders.

The shop only manufactures labels for originals that were actually allocated
and whose auction has cleared the original settlement; the plate maker cannot
register a plate for an unawarded or unsettled bid.  Each winning original
gets exactly one unique plate (enforced by a unique index), so a plate can
never be duplicated into two heads.

Work orders are planned once and persisted; ``pull_job`` then materialises one
label row per non-empty head per sheet, marking surplus copies TORN on the
spot.  Empty heads produce no label and no waste count.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .core import (
    DomainError,
    InvalidState,
    NotFound,
    get_receipt,
    now_utc,
    put_receipt,
    require_int,
    require_nonempty,
)
from .press_plan import (
    HEADS,
    MAX_PLATES_PER_JOB,
    MAX_SHEETS_PER_BATCH,
    HeadMount,
    JobPlan,
    Plate,
    plan_job,
    prove_mount,
)


def _row_to_plate(row) -> Plate:
    return Plate(
        plate_id=row["plate_id"],
        auction_id=row["auction_id"],
        buyer=row["buyer"],
        qty=row["qty"],
        allowed_heads=frozenset(json.loads(row["allowed_heads"])),
        holes=tuple(json.loads(row["holes"])),
        flippable=bool(row["flippable"]),
    )


class PressShop:
    def __init__(self, db, auctions):
        self.db = db
        # Used only to read the immutable frozen/settled allocation result.
        self.auctions = auctions

    # ------------------------------------------------------------ plates

    def register_plate(
        self,
        request_id: str,
        plate_id: str,
        auction_id: str,
        buyer: str,
        allowed_heads: list[str],
        holes: list[int],
        flippable: bool,
    ) -> dict[str, Any]:
        require_nonempty(request_id, "request_id")
        require_nonempty(plate_id, "plate_id")
        require_nonempty(auction_id, "auction_id")
        require_nonempty(buyer, "buyer")
        heads = self._validate_heads(allowed_heads)
        hole_tuple = self._validate_holes(holes)
        if not isinstance(flippable, bool):
            raise DomainError("flippable must be a boolean")
        payload = {
            "plate_id": plate_id, "auction_id": auction_id, "buyer": buyer,
            "allowed_heads": sorted(heads), "holes": list(hole_tuple),
            "flippable": flippable,
        }
        operation = "press.register_plate"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            existing = conn.execute(
                "SELECT * FROM press_plates WHERE plate_id=?", (plate_id,)
            ).fetchone()
            if existing is not None:
                response = self._plate_response(_row_to_plate(existing))
                put_receipt(conn, request_id, operation, payload, response)
                return response

            # The plate maker can only register the units the buyer actually
            # won, and only after the original settlement has been done.
            result = self.auctions.read_result(auction_id)
            if result["status"] == "OPEN":
                raise InvalidState(
                    f"auction {auction_id} is still OPEN; freeze/settle it first"
                )
            if result.get("settlement_status") != "CLEARED":
                raise InvalidState(
                    f"auction {auction_id} is {result['status']} but the original "
                    "clearing is not done; the plate maker cannot register plates yet"
                )
            allocation = next(
                (a for a in result["allocations"] if a["buyer"] == buyer), None
            )
            if allocation is None or allocation["allocated_qty"] <= 0:
                raise DomainError(
                    f"buyer {buyer} holds no settled winning original in {auction_id}; "
                    "unawarded originals cannot be put on press"
                )
            qty = allocation["allocated_qty"]
            at = now_utc()
            try:
                conn.execute(
                    "INSERT INTO press_plates(plate_id, auction_id, buyer, qty, "
                    "allowed_heads, holes, flippable, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        plate_id, auction_id, buyer, qty,
                        json.dumps(sorted(heads)), json.dumps(list(hole_tuple)),
                        1 if flippable else 0, at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError(
                    f"buyer {buyer} already has a unique plate for auction {auction_id}; "
                    "an original cannot be duplicated"
                ) from exc
            plate = Plate(plate_id, auction_id, buyer, qty, heads, hole_tuple, flippable)
            response = self._plate_response(plate)
            put_receipt(conn, request_id, operation, payload, response)
        return response

    @staticmethod
    def _validate_heads(allowed_heads: Any) -> frozenset[str]:
        if not isinstance(allowed_heads, (list, tuple, set, frozenset)) or not allowed_heads:
            raise DomainError("allowed_heads must be a non-empty list of 'L'/'R'")
        heads = frozenset(allowed_heads)
        if not heads or any(h not in HEADS for h in heads):
            raise DomainError(f"allowed_heads must be a subset of {list(HEADS)}")
        return heads

    @staticmethod
    def _validate_holes(holes: Any) -> tuple[int, ...]:
        if not isinstance(holes, (list, tuple)) or not holes:
            raise DomainError("holes must be a non-empty sequence of 0/1")
        if len(holes) > 10:
            raise DomainError("a hole train has at most 10 positions")
        if any(isinstance(v, bool) or not isinstance(v, int) or v not in (0, 1) for v in holes):
            raise DomainError("every hole position must be 0 or 1")
        return tuple(holes)

    def _load_plates(self, conn, plate_ids: list[str]) -> list[Plate]:
        plates: list[Plate] = []
        for plate_id in plate_ids:
            row = conn.execute(
                "SELECT * FROM press_plates WHERE plate_id=?", (plate_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"plate {plate_id!r} is not registered for press")
            plates.append(_row_to_plate(row))
        return plates

    @staticmethod
    def _plate_response(plate: Plate) -> dict[str, Any]:
        return {
            "plate_id": plate.plate_id,
            "auction_id": plate.auction_id,
            "buyer": plate.buyer,
            "qty": plate.qty,
            "allowed_heads": sorted(plate.allowed_heads),
            "holes": list(plate.holes),
            "flippable": plate.flippable,
        }

    def get_plate(self, plate_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM press_plates WHERE plate_id=?", (plate_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"plate {plate_id!r} not found")
            return self._plate_response(_row_to_plate(row))

    # ------------------------------------------------------- trial proof

    def trial_proof(
        self,
        left: dict[str, str] | None,
        right: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Read-only trial imposition: plate id, head, direction, hole train
        and the exact pin positions that fail to register."""
        wanted = [spec["plate_id"] for spec in (left, right) if spec is not None]
        with self.db.read() as conn:
            plates = {p.plate_id: p for p in self._load_plates(conn, wanted)}
        return prove_mount(plates, left, right)

    # ------------------------------------------------------------ planning

    def plan_job(
        self,
        request_id: str,
        job_id: str,
        plate_ids: list[str],
        paper_limit: int,
        max_changes: int,
    ) -> dict[str, Any]:
        require_nonempty(request_id, "request_id")
        require_nonempty(job_id, "job_id")
        require_int(paper_limit, "paper_limit", minimum=1)
        require_int(max_changes, "max_changes", minimum=0)
        if not isinstance(plate_ids, (list, tuple)) or not plate_ids:
            raise DomainError("plate_ids must be a non-empty list")
        if len(plate_ids) != len(set(plate_ids)):
            raise DomainError("a plate can appear only once in a job")
        if len(plate_ids) > MAX_PLATES_PER_JOB:
            raise DomainError(f"at most {MAX_PLATES_PER_JOB} plates per job")
        payload = {
            "job_id": job_id, "plate_ids": list(plate_ids),
            "paper_limit": paper_limit, "max_changes": max_changes,
        }
        operation = "press.plan_job"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            existing = conn.execute(
                "SELECT plan_json, status FROM press_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if existing is not None:
                response = json.loads(existing["plan_json"])
                put_receipt(conn, request_id, operation, payload, response)
                return response
            plates = self._load_plates(conn, list(plate_ids))
            # An original maps to one unique plate and is manufactured once:
            # a plate already pulled in another job cannot be re-imposed, or
            # the buyer would receive duplicates of the same original.
            placeholders = ",".join("?" for _ in plate_ids)
            reused = conn.execute(
                f"SELECT DISTINCT plate_id FROM press_labels "
                f"WHERE plate_id IN ({placeholders})",
                list(plate_ids),
            ).fetchall()
            if reused:
                raise InvalidState(
                    "plate(s) already pulled in another job: "
                    + ", ".join(r["plate_id"] for r in reused)
                )
            plan = plan_job(plates, paper_limit, max_changes)
            self._persist_plan(conn, job_id, paper_limit, max_changes, plan)
            response = self._job_response(job_id, "PLANNED", paper_limit, max_changes, plan)
            put_receipt(conn, request_id, operation, payload, response)
        return response

    def _persist_plan(self, conn, job_id, paper_limit, max_changes, plan: JobPlan) -> None:
        at = now_utc()
        conn.execute(
            "INSERT INTO press_jobs(job_id, paper_limit, max_changes, paper_used, "
            "changes_used, torn_total, status, plan_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'PLANNED', ?, ?)",
            (
                job_id, paper_limit, max_changes, plan.paper_used,
                plan.changes_used, plan.torn_total,
                json.dumps(
                    self._job_response(job_id, "PLANNED", paper_limit, max_changes, plan),
                    sort_keys=True, ensure_ascii=False,
                ),
                at,
            ),
        )
        for batch in plan.batches:
            conn.execute(
                "INSERT INTO press_batches(job_id, batch_seq, head_left, head_right, "
                "orient_left, orient_right, sheets, is_change) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id, batch.seq,
                    batch.mount.left.plate.plate_id if batch.mount.left else None,
                    batch.mount.right.plate.plate_id if batch.mount.right else None,
                    batch.mount.left.orientation if batch.mount.left else None,
                    batch.mount.right.orientation if batch.mount.right else None,
                    batch.sheets, 1 if batch.is_change else 0,
                ),
            )

    def _head_json(self, hm: HeadMount | None) -> dict[str, Any] | None:
        if hm is None:
            return None
        return {
            "plate_id": hm.plate.plate_id,
            "buyer": hm.plate.buyer,
            "orientation": hm.orientation,
            "effective_holes": list(hm.holes()),
        }

    def _job_response(
        self, job_id: str, status: str, paper_limit: int, max_changes: int, plan: JobPlan
    ) -> dict[str, Any]:
        batches = []
        for batch in plan.batches:
            batches.append(
                {
                    "seq": batch.seq,
                    "sheets": batch.sheets,
                    "is_change": batch.is_change,
                    "left": self._head_json(batch.mount.left),
                    "right": self._head_json(batch.mount.right),
                    "production": batch.production,
                    "torn_in_batch": batch.torn,
                }
            )
        return {
            "job_id": job_id,
            "status": status,
            "paper_limit": paper_limit,
            "max_changes": max_changes,
            "paper_used": plan.paper_used,
            "changes_used": plan.changes_used,
            "torn_total": plan.torn_total,
            "delivered": dict(plan.delivered),
            "torn_by_plate": dict(plan.torn_by_plate),
            "batches": batches,
        }

    def read_job(self, job_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT plan_json FROM press_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"press job {job_id!r} not found")
            return json.loads(row["plan_json"])

    # ------------------------------------------------------------ execution

    def pull_job(self, request_id: str, job_id: str) -> dict[str, Any]:
        """Lock the plan onto the press and pull every batch.

        One label row is written per non-empty head per sheet.  Each buyer
        receives exactly their awarded quantity; every extra copy of an
        already-complete buyer is marked TORN and can never be transferred.
        Idempotent: a repeated call reads back the same pulled result.
        """
        require_nonempty(request_id, "request_id")
        require_nonempty(job_id, "job_id")
        payload = {"job_id": job_id}
        operation = "press.pull_job"

        with self.db.tx() as conn:
            prior = get_receipt(conn, request_id, operation, payload)
            if prior is not None:
                return prior
            row = conn.execute(
                "SELECT * FROM press_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"press job {job_id!r} not found")
            plan = json.loads(row["plan_json"])
            if row["status"] == "PULLED":
                put_receipt(conn, request_id, operation, payload, plan)
                return plan

            at = now_utc()
            delivered_running = {pid: 0 for pid in plan["delivered"]}
            torn_running = {pid: 0 for pid in plan["torn_by_plate"]}
            for batch in plan["batches"]:
                for plate_id, acc in batch["production"].items():
                    plate = conn.execute(
                        "SELECT buyer FROM press_plates WHERE plate_id=?", (plate_id,)
                    ).fetchone()
                    for _ in range(acc["delivered"]):
                        self._insert_label(
                            conn, request_id, job_id, batch["seq"], plate_id,
                            acc["head"], plate["buyer"], "DELIVERED", at,
                        )
                    for _ in range(acc["torn"]):
                        self._insert_label(
                            conn, request_id, job_id, batch["seq"], plate_id,
                            acc["head"], plate["buyer"], "TORN", at,
                        )
                    delivered_running[plate_id] += acc["delivered"]
                    torn_running[plate_id] += acc["torn"]

            conn.execute(
                "UPDATE press_jobs SET status='PULLED', pulled_at=? WHERE job_id=?",
                (at, job_id),
            )
            plan["status"] = "PULLED"
            conn.execute(
                "UPDATE press_jobs SET plan_json=? WHERE job_id=?",
                (json.dumps(plan, sort_keys=True, ensure_ascii=False), job_id),
            )
            put_receipt(conn, request_id, operation, payload, plan)
        return plan

    @staticmethod
    def _insert_label(conn, request_id, job_id, batch_seq, plate_id, head, buyer, outcome, at):
        conn.execute(
            "INSERT INTO press_labels(job_id, batch_seq, plate_id, head, buyer, "
            "outcome, request_id, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, batch_seq, plate_id, head, buyer, outcome, request_id, at),
        )

    # ------------------------------------------------------------ audit

    def buyer_account(self, buyer: str) -> dict[str, Any]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT outcome, COUNT(*) AS n FROM press_labels WHERE buyer=? "
                "GROUP BY outcome",
                (buyer,),
            ).fetchall()
            plates = [
                dict(r)
                for r in conn.execute(
                    "SELECT plate_id, auction_id, qty FROM press_plates WHERE buyer=? "
                    "ORDER BY plate_id",
                    (buyer,),
                )
            ]
        delivered = sum(r["n"] for r in rows if r["outcome"] == "DELIVERED")
        torn = sum(r["n"] for r in rows if r["outcome"] == "TORN")
        return {
            "buyer": buyer,
            "plates": plates,
            "delivered_labels": delivered,
            "torn_labels": torn,
        }


def reconcile_press(db) -> dict[str, Any]:
    """Audit the print shop from raw rows, without trusting cached plan JSON.

    Checks plate provenance (settled allocation, exact awarded qty), batch
    limits, change accounting, one label per non-empty head per sheet, exact
    delivery per buyer, torn labels never reassigned, and that a unique plate
    is never pulled in more than one job.
    """
    violations: list[str] = []
    with db.read() as conn:
        plate_rows = [dict(r) for r in conn.execute("SELECT * FROM press_plates")]
        plate_by_id = {r["plate_id"]: r for r in plate_rows}

        # --- plate provenance against the immutable settlement snapshot ---
        for plate in plate_rows:
            snap_row = conn.execute(
                "SELECT allocations_json, status FROM settlements WHERE auction_id=?",
                (plate["auction_id"],),
            ).fetchone()
            if snap_row is None:
                violations.append(
                    f"plate {plate['plate_id']}: auction {plate['auction_id']} was never settled"
                )
                continue
            snap = json.loads(snap_row["allocations_json"])
            alloc = next(
                (a for a in snap["allocations"] if a["buyer"] == plate["buyer"]), None
            )
            if alloc is None or alloc["allocated_qty"] <= 0:
                violations.append(
                    f"plate {plate['plate_id']}: buyer {plate['buyer']} won no units"
                )
            elif plate["qty"] != alloc["allocated_qty"]:
                violations.append(
                    f"plate {plate['plate_id']}: qty {plate['qty']} != awarded "
                    f"{alloc['allocated_qty']}"
                )

        job_rows = [dict(r) for r in conn.execute("SELECT * FROM press_jobs")]
        for job in job_rows:
            job_id = job["job_id"]
            batches = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM press_batches WHERE job_id=? ORDER BY batch_seq",
                    (job_id,),
                )
            ]
            if not batches:
                violations.append(f"job {job_id}: has no batches")
            total_sheets = sum(b["sheets"] for b in batches)
            if total_sheets != job["paper_used"]:
                violations.append(
                    f"job {job_id}: paper_used {job['paper_used']} != batch sheets {total_sheets}"
                )
            if total_sheets > job["paper_limit"]:
                violations.append(f"job {job_id}: exceeds paper limit")
            changes = sum(b["is_change"] for b in batches)
            if changes != job["changes_used"] or changes > job["max_changes"]:
                violations.append(f"job {job_id}: plate-change accounting mismatch")
            if any(b["sheets"] > MAX_SHEETS_PER_BATCH for b in batches):
                violations.append(f"job {job_id}: a batch exceeds {MAX_SHEETS_PER_BATCH} sheets")

            # One label per non-empty head per sheet; empty heads produce none.
            expected_per_plate: dict[str, int] = {}
            torn_expected: dict[str, int] = {}
            for b in batches:
                heads_used = [h for h in (b["head_left"], b["head_right"]) if h is not None]
                if len(heads_used) != len(set(heads_used)):
                    violations.append(
                        f"job {job_id} batch {b['batch_seq']}: same plate on both heads"
                    )
                for pid in heads_used:
                    expected_per_plate[pid] = expected_per_plate.get(pid, 0) + b["sheets"]

            label_counts: dict[str, dict[str, int]] = {}
            for r in conn.execute(
                "SELECT plate_id, buyer, outcome, COUNT(*) AS n FROM press_labels "
                "WHERE job_id=? GROUP BY plate_id, buyer, outcome",
                (job_id,),
            ):
                label_counts.setdefault(r["plate_id"], {})[r["outcome"]] = r["n"]
                # A label can only ever belong to the plate's own buyer.
                if plate_by_id.get(r["plate_id"], {}).get("buyer") != r["buyer"]:
                    violations.append(
                        f"job {job_id}: label for {r['plate_id']} reassigned to {r['buyer']}"
                    )

            for pid, expected_labels in expected_per_plate.items():
                got = sum(label_counts.get(pid, {}).values())
                # A merely PLANNED job has not printed anything yet.
                if job["status"] == "PULLED" and got != expected_labels:
                    violations.append(
                        f"job {job_id}/{pid}: {got} labels != {expected_labels} sheet pulls"
                    )
                if job["status"] == "PULLED":
                    delivered = label_counts.get(pid, {}).get("DELIVERED", 0)
                    if delivered != plate_by_id[pid]["qty"]:
                        violations.append(
                            f"job {job_id}/{pid}: delivered {delivered} != awarded "
                            f"{plate_by_id[pid]['qty']}"
                        )
                torn = label_counts.get(pid, {}).get("TORN", 0)
                torn_expected[pid] = torn
            if job["status"] == "PULLED" and sum(torn_expected.values()) != job["torn_total"]:
                violations.append(f"job {job_id}: torn total does not match label rows")

            # Plates never pulled in a pulled job cannot be considered delivered.
            if job["status"] == "PULLED":
                for pid in expected_per_plate:
                    if "DELIVERED" not in label_counts.get(pid, {}):
                        violations.append(f"job {job_id}/{pid}: no delivered labels after pull")

        # --- cross-job: a unique plate is manufactured exactly once, so the
        # total delivered labels for a plate across every job equal its award.
        for r in conn.execute(
            "SELECT plate_id, outcome, COUNT(*) AS n FROM press_labels "
            "GROUP BY plate_id, outcome"
        ):
            if r["outcome"] != "DELIVERED":
                continue
            awarded = plate_by_id.get(r["plate_id"], {}).get("qty")
            if awarded is not None and r["n"] != awarded:
                violations.append(
                    f"plate {r['plate_id']}: delivered {r['n']} across jobs != awarded {awarded}"
                )
        jobs_per_plate = conn.execute(
            "SELECT plate_id, COUNT(DISTINCT job_id) AS n FROM press_labels "
            "GROUP BY plate_id HAVING COUNT(DISTINCT job_id) > 1"
        ).fetchall()
        for r in jobs_per_plate:
            violations.append(
                f"plate {r['plate_id']}: appears in {r['n']} jobs (unique plate pulled twice)"
            )

    return {
        "ok": not violations,
        "violations": violations,
        "plates": len(plate_rows),
        "jobs": len(job_rows),
    }
