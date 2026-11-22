"""Workshop ledger for foam-mold work orders and physical positioning stickers.

A :class:`~slabwater.molding.MoldSpec` describes the knives and approved strokes
for one mold; this module adds the *physical* side of the workshop:

* positioning stickers are real, single-use inventory.  A plain quote (trial
  calculation) never touches inventory; only a confirmed work order peels the
  sticker its chosen strokes use;
* a later order cannot select a sticker an earlier confirmed order already
  peeled.  A sticker can additionally be *reserved* (kept on the roll for a
  different mold) without consuming it;
* the same opening conditions (the canonical spec plus sticker state) can be
  quoted more than once with self-chosen stroke sets;
* confirmed work orders freeze their full input, so an old order can be
  recomputed after the catalog has moved on to other source drawings.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model import canonical_json, sha256_hex
from .molding import (
    MoldError,
    MoldSpec,
    Selection,
    SelectionReport,
    analyze_all,
    evaluate_selection,
    optimize,
    review_grid,
    spec_from_dict,
    spec_hash,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stickers (
    sticker_id TEXT PRIMARY KEY,
    status     TEXT NOT NULL CHECK (status IN ('on_roll','reserved','peeled')),
    note       TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS work_orders (
    order_id     TEXT PRIMARY KEY,
    request_key  TEXT NOT NULL,
    input_hash   TEXT NOT NULL,
    spec         TEXT NOT NULL,
    selection    TEXT NOT NULL,
    review       TEXT NOT NULL,
    peeled       TEXT NOT NULL,
    tools        TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('confirmed')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS quote_log (
    quote_id   TEXT PRIMARY KEY,
    request_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    payload    TEXT NOT NULL,
    consumed   INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@dataclass(frozen=True)
class QuoteResult:
    ok: bool
    quote_id: str | None
    report: SelectionReport | None
    consumed_inventory: bool
    peeled: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "quote_id": self.quote_id,
            "report": self.report.to_dict() if self.report else None,
            "consumed_inventory": self.consumed_inventory,
            "peeled": list(self.peeled),
        }


@dataclass(frozen=True)
class WorkOrder:
    order_id: str
    request_key: str
    input_hash: str
    spec: MoldSpec
    selection: Selection
    review: list[list[dict[str, Any]]]
    peeled: tuple[str, ...]
    tools: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "request_key": self.request_key,
            "input_hash": self.input_hash,
            "spec": self.spec.to_dict(),
            "selection": self.selection.to_dict(),
            "review": self.review,
            "peeled": list(self.peeled),
            "tools": list(self.tools),
        }


class MoldShop:
    """SQLite-backed sticker inventory and confirmed-order ledger."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MoldShop":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- sticker inventory -------------------------------------------------
    def register_sticker(self, sticker_id: str, note: str = "") -> None:
        if not isinstance(sticker_id, str) or not sticker_id:
            raise MoldError("sticker id must be a non-empty string")
        self._conn.execute(
            "INSERT OR IGNORE INTO stickers(sticker_id,status,note) VALUES (?,?,?)",
            (sticker_id, "on_roll", note),
        )

    def register_stickers(self, sticker_ids: Sequence[str]) -> None:
        for sticker_id in sticker_ids:
            self.register_sticker(sticker_id)

    def sticker_status(self, sticker_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM stickers WHERE sticker_id=?", (sticker_id,)
        ).fetchone()
        return row[0] if row else None

    def list_stickers(self) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT sticker_id, status FROM stickers ORDER BY sticker_id"
        ).fetchall()
        return {sticker_id: status for sticker_id, status in rows}

    def reserve_sticker(self, sticker_id: str, note: str = "reserved for another mold") -> None:
        """Keep a physical sticker on the roll but forbid its use here."""

        if self.sticker_status(sticker_id) is None:
            raise MoldError(f"unknown sticker: {sticker_id}")
        self._conn.execute(
            "UPDATE stickers SET status='reserved', note=? WHERE sticker_id=? AND status='on_roll'",
            (note, sticker_id),
        )
        if self.sticker_status(sticker_id) != "reserved":
            raise MoldError(f"sticker {sticker_id} is not available to reserve")

    def release_reservation(self, sticker_id: str) -> None:
        if self.sticker_status(sticker_id) != "reserved":
            raise MoldError(f"sticker {sticker_id} is not reserved")
        self._conn.execute(
            "UPDATE stickers SET status='on_roll', note='' WHERE sticker_id=?",
            (sticker_id,),
        )

    def _peeled(self) -> frozenset[str]:
        rows = self._conn.execute(
            "SELECT sticker_id FROM stickers WHERE status='peeled'"
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def _reserved(self) -> frozenset[str]:
        rows = self._conn.execute(
            "SELECT sticker_id FROM stickers WHERE status='reserved'"
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def sync_inventory(self, spec: MoldSpec) -> None:
        """Make sure every sticker referenced by the spec exists on the roll."""

        for sticker_id in spec.sticker_ids():
            if self.sticker_status(sticker_id) is None:
                self.register_sticker(sticker_id)

    # -- quotes (never peel) -----------------------------------------------
    def quote(
        self,
        request_key: str,
        spec: MoldSpec,
        selection: Selection | None = None,
        reserve: Sequence[str] = (),
    ) -> QuoteResult:
        """Trial calculation under the current physical state -- no peeling.

        With ``selection=None`` the cheapest feasible set is searched.  A
        self-chosen stroke set can be evaluated instead; either way inventory is
        untouched and the attempt is logged as a non-consuming quote.
        """

        reserve_set = frozenset(reserve)
        unknown = [s for s in reserve_set if self.sticker_status(s) is None]
        if unknown:
            raise MoldError(f"cannot reserve unknown stickers: {sorted(unknown)}")
        consumed = self._peeled()
        forbidden = self._reserved() | reserve_set
        if selection is None:
            report = optimize(spec, forbidden_stickers=sorted(forbidden),
                              consumed_stickers=sorted(consumed))
        else:
            evaluated = evaluate_selection(
                spec, selection.stroke_ids,
                forbidden_stickers=forbidden, consumed_stickers=consumed,
            )
            report = SelectionReport(
                ok=evaluated.feasible,
                reason=None if evaluated.feasible else "self_chosen_set_infeasible",
                best=evaluated if evaluated.feasible else None,
                candidates_evaluated=1,
                forbidden_stickers=forbidden,
                consumed_stickers=consumed,
            )
        payload = {
            "request_key": request_key,
            "spec_hash": spec_hash(spec),
            "reserve": sorted(reserve_set),
            "report": report.to_dict(),
        }
        quote_id = "Q-" + sha256_hex(canonical_json(payload))[:16]
        self._conn.execute(
            "INSERT OR IGNORE INTO quote_log(quote_id,request_key,input_hash,payload,consumed) "
            "VALUES (?,?,?,?,0)",
            (quote_id, request_key, spec_hash(spec), json.dumps(payload, sort_keys=True)),
        )
        return QuoteResult(
            ok=report.ok, quote_id=quote_id, report=report,
            consumed_inventory=False, peeled=(),
        )

    # -- confirmation (peels real stickers) --------------------------------
    def confirm(
        self,
        request_key: str,
        spec: MoldSpec,
        selection: Selection | None = None,
        reserve: Sequence[str] = (),
    ) -> WorkOrder:
        """Confirm a work order and physically peel its sticker (if any).

        The cheapest set is used when ``selection`` is not supplied.  The
        confirmation is atomic: an unavailable/peeled sticker, a sticker group
        conflict or a rack violation rejects the whole order and leaves the roll
        untouched.  Replaying the same ``request_key`` with the identical input
        returns the stored order instead of double-peeling.
        """

        reserve_set = frozenset(reserve)
        input_hash = spec_hash(spec)
        replay = self._replay_order(request_key, input_hash)
        if replay is not None:
            return replay
        self.sync_inventory(spec)
        consumed = self._peeled()
        forbidden = self._reserved() | reserve_set
        if selection is None:
            report = optimize(spec, forbidden_stickers=sorted(forbidden),
                              consumed_stickers=sorted(consumed))
            if not report.ok or report.best is None:
                raise MoldError(report.reason or "no feasible stroke set")
            chosen = report.best
        else:
            chosen = evaluate_selection(
                spec, selection.stroke_ids,
                forbidden_stickers=forbidden, consumed_stickers=consumed,
            )
            if not chosen.feasible:
                raise MoldError(
                    "self-chosen stroke set is infeasible: "
                    f"missing={sorted(chosen.missing_cells)} "
                    f"overcut={sorted(chosen.overcut_cells)} "
                    f"sticker_conflicts={list(chosen.sticker_conflicts)} "
                    f"rack_ok={chosen.rack_ok}"
                )
        peeled = tuple(
            sorted({
                sticker
                for sid in chosen.stroke_ids
                if (sticker := self._sticker_for(spec, sid)) is not None
            })
        )
        # Defensive re-check inside the transaction: no earlier confirmed order
        # may have peeled the same physical sticker.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for sticker_id in peeled:
                status = self.sticker_status(sticker_id)
                if status != "on_roll":
                    raise MoldError(
                        f"sticker {sticker_id} is not on the roll (status={status})"
                    )
            order_id = "WO-" + sha256_hex(
                canonical_json(
                    {
                        "request_key": request_key,
                        "input_hash": input_hash,
                        "stroke_ids": list(chosen.stroke_ids),
                        "peeled": list(peeled),
                    }
                )
            )[:16]
            if self._conn.execute(
                "SELECT 1 FROM work_orders WHERE order_id=?", (order_id,)
            ).fetchone() is None:
                review = review_grid(spec, chosen)
                for sticker_id in peeled:
                    self._conn.execute(
                        "UPDATE stickers SET status='peeled', "
                        "note=COALESCE(note,'')||? WHERE sticker_id=?",
                        (f" peeled by {order_id}", sticker_id),
                    )
                self._conn.execute(
                    "INSERT INTO work_orders(order_id,request_key,input_hash,spec,selection,"
                    "review,peeled,tools,status) VALUES (?,?,?,?,?,?,?,?,'confirmed')",
                    (
                        order_id,
                        request_key,
                        input_hash,
                        json.dumps(spec.to_dict(), sort_keys=True),
                        json.dumps(chosen.to_dict(), sort_keys=True),
                        json.dumps(review, sort_keys=True),
                        json.dumps(list(peeled), sort_keys=True),
                        json.dumps(list(chosen.tool_ids), sort_keys=True),
                    ),
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO quote_log(quote_id,request_key,input_hash,"
                    "payload,consumed) VALUES (?,?,?,?,1)",
                    (
                        "Q-" + order_id[3:],
                        request_key,
                        input_hash,
                        json.dumps({"order_id": order_id, "peeled": list(peeled)},
                                   sort_keys=True),
                    ),
                )
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")
        return self.get_order(order_id)

    @staticmethod
    def _sticker_for(spec: MoldSpec, stroke_id: str) -> str | None:
        for stroke in spec.strokes:
            if stroke.id == stroke_id:
                return stroke.sticker
        raise MoldError(f"unknown stroke: {stroke_id}")

    # -- retrieval / recompute ---------------------------------------------
    def _replay_order(self, request_key: str, input_hash: str) -> WorkOrder | None:
        row = self._conn.execute(
            "SELECT input_hash, order_id FROM work_orders WHERE request_key=?",
            (request_key,),
        ).fetchone()
        if row is None:
            return None
        if row[0] != input_hash:
            raise MoldError(
                f"request key {request_key!r} already confirmed with different input"
            )
        return self.get_order(row[1])

    def get_order(self, order_id: str) -> WorkOrder:
        row = self._conn.execute(
            "SELECT request_key, input_hash, spec, selection, review, peeled, tools "
            "FROM work_orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"no such work order: {order_id}")
        spec = spec_from_dict(json.loads(row[2]))
        selection_raw = json.loads(row[3])
        selection = evaluate_selection(spec, selection_raw["stroke_ids"])
        return WorkOrder(
            order_id=order_id,
            request_key=row[0],
            input_hash=row[1],
            spec=spec,
            selection=selection,
            review=json.loads(row[4]),
            peeled=tuple(json.loads(row[5])),
            tools=tuple(json.loads(row[6])),
        )

    def list_orders(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT order_id, request_key, peeled, tools, created_at "
            "FROM work_orders ORDER BY order_id"
        ).fetchall()
        return [
            {
                "order_id": r[0],
                "request_key": r[1],
                "peeled": json.loads(r[2]),
                "tools": json.loads(r[3]),
                "created_at": r[4],
            }
            for r in rows
        ]

    def recompute_order(self, order_id: str) -> dict[str, Any]:
        """Re-run every analysis for a stored order against its frozen spec.

        The catalog may have moved to other source drawings; the stored spec
        still carries the frozen plan/elevation hash, so the recomputation is
        independent of the current catalog state.
        """

        order = self.get_order(order_id)
        spec = order.spec
        analyses = analyze_all(spec)
        reeval = evaluate_selection(spec, order.selection.stroke_ids)
        fresh = optimize(spec)
        return {
            "order_id": order_id,
            "frozen_plan_id": spec.target.plan_id,
            "catalog_version": spec.target.catalog_version,
            "elevation_hash": spec.target.elevation_hash,
            "stroke_analyses": [
                a.to_dict(spec.target.height, spec.target.width) for a in analyses
            ],
            "stored_selection": order.selection.to_dict(),
            "recomputed_selection": reeval.to_dict(),
            "recomputed_matches_stored": reeval.stroke_ids == order.selection.stroke_ids
            and reeval.total_fee == order.selection.total_fee,
            "unconstrained_cheapest_today": (
                fresh.best.to_dict() if fresh.best else None
            ),
            "review": review_grid(spec, reeval),
            "peeled": list(order.peeled),
        }
