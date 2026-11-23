"""Revision-aware, atomic ingestion of rubbing files.

The library is fixed to one small-mark dictionary and one reader version, mirroring
the fixed-contract design of :class:`extcontract.ingest.IngestionLibrary`.
Business identity is a rubbing id.  A file is validated into a staged copy before
any state changes, so a conflict never makes part of a file visible.

The display schema may show a default left rivet for old readers.  Canonical
:class:`~extcontract.dominoes.model.Rubbing` state does not contain that default;
it is attached only during publication.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any

from .model import (
    DEFAULT_READER_RIVET_SIDE,
    DominoIngestResult,
    Rubbing,
    RubbingState,
    parse_rubbing,
)

_DOCUMENT_KEYS = {"file_id", "writer_version", "reader_version", "rubbings"}


@dataclass(frozen=True)
class FixedRubbingContract:
    writer_version: str
    reader_version: str
    dictionary: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.writer_version, str) or not self.writer_version:
            raise ValueError("writer_version must be a non-empty string")
        if not isinstance(self.reader_version, str) or not self.reader_version:
            raise ValueError("reader_version must be a non-empty string")
        if not isinstance(self.dictionary, (tuple, list)) or not self.dictionary:
            raise ValueError("dictionary must be a non-empty sequence")
        seen: set[str] = set()
        for item in self.dictionary:
            if not isinstance(item, str) or not item or item in seen:
                raise ValueError("dictionary entries must be unique non-empty strings")
            seen.add(item)
        object.__setattr__(self, "dictionary", tuple(self.dictionary))

    @property
    def dictionary_set(self) -> frozenset[str]:
        return frozenset(self.dictionary)


class RubbingLibrary:
    """Owns accepted rubbing state under one fixed dictionary/reader version."""

    def __init__(self, contract: FixedRubbingContract):
        self.contract = contract
        self._lock = threading.RLock()
        self._states: dict[str, RubbingState] = {}
        self._receipts: dict[str, dict] = {}
        self._sequence = 0

    # -- read-only views ---------------------------------------------------

    def get_rubbing(self, rubbing_id: str) -> Rubbing | None:
        with self._lock:
            state = self._states.get(rubbing_id)
            return state.rubbing if state else None

    def rubbings_for(self, tile_id: str) -> list[Rubbing]:
        with self._lock:
            return [
                state.rubbing
                for state in self._states.values()
                if state.rubbing.tile_id == tile_id
            ]

    def snapshot(self) -> list[Rubbing]:
        with self._lock:
            return [self._states[key].rubbing for key in sorted(self._states)]

    def tile_ids(self) -> list[str]:
        with self._lock:
            return sorted({state.rubbing.tile_id for state in self._states.values()})

    def freeze(self) -> list[Rubbing]:
        """Return an independent frozen witness list for restoration."""

        return [
            Rubbing(**rubbing.__dict__)
            for rubbing in self.snapshot()
        ]

    def publish(self) -> dict:
        """Publish reader records, including display-only rivet default evidence."""

        with self._lock:
            rows = []
            for rubbing_id in sorted(self._states):
                rubbing = self._states[rubbing_id].rubbing
                display_rivet = (
                    rubbing.rivet_side
                    if rubbing.rivet_side is not None
                    else DEFAULT_READER_RIVET_SIDE
                )
                rows.append(
                    {
                        "tile_id": rubbing.tile_id,
                        "rubbing_id": rubbing.rubbing_id,
                        "revision": rubbing.revision,
                        "left_marks": (
                            None if rubbing.left_marks is None else list(rubbing.left_marks)
                        ),
                        "right_marks": (
                            None if rubbing.right_marks is None else list(rubbing.right_marks)
                        ),
                        "rivet_side": display_rivet,
                        "sources": {
                            "rivet_side": (
                                {"evidence": rubbing.rivet_side}
                                if rubbing.rivet_side is not None
                                else {
                                    "default": DEFAULT_READER_RIVET_SIDE,
                                    "conversion": "display_default",
                                    "usable_for_orientation": False,
                                }
                            )
                        },
                    }
                )
        return {
            "format": "domino-rubbings/v1",
            "reader_version": self.contract.reader_version,
            "dictionary": list(self.contract.dictionary),
            "rows": rows,
        }

    def receipt_for(self, file_id: str) -> dict | None:
        with self._lock:
            receipt = self._receipts.get(file_id)
            return dict(receipt) if receipt else None

    # -- write path --------------------------------------------------------

    def ingest(self, document: Any) -> DominoIngestResult:
        with self._lock:
            return self._ingest_locked(document)

    def _ingest_locked(self, document: Any) -> DominoIngestResult:
        c = self.contract

        def reject(file_id: str | None, errors: list[str]) -> DominoIngestResult:
            return DominoIngestResult(
                status="REJECTED",
                file_id=file_id,
                writer_version=c.writer_version,
                reader_version=c.reader_version,
                errors=list(errors),
            )

        if not isinstance(document, dict):
            return reject(None, ["file document must be a JSON object"])
        unknown = set(document) - _DOCUMENT_KEYS
        if unknown:
            return reject(None, [f"unexpected file keys {sorted(unknown)}"])
        file_id = document.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            return reject(None, ["file_id must be a non-empty string"])
        if document.get("writer_version") != c.writer_version:
            return reject(
                file_id,
                [
                    f"file declares writer {document.get('writer_version')!r}; fixed "
                    f"writer is {c.writer_version!r}"
                ],
            )
        if document.get("reader_version") != c.reader_version:
            return reject(
                file_id,
                [
                    f"file declares reader {document.get('reader_version')!r}; fixed "
                    f"reader is {c.reader_version!r}"
                ],
            )
        raw_rubbings = document.get("rubbings")
        if not isinstance(raw_rubbings, list) or not raw_rubbings:
            return reject(file_id, ["rubbings must be a non-empty list"])

        errors: list[str] = []
        parsed: list[Rubbing] = []
        for index, raw in enumerate(raw_rubbings):
            try:
                parsed.append(parse_rubbing(raw, index=index, dictionary=c.dictionary_set))
            except ValueError as exc:
                errors.append(str(exc))
        if errors:
            return reject(file_id, errors)

        grouped: dict[str, list[Rubbing]] = {}
        for rubbing in parsed:
            grouped.setdefault(rubbing.rubbing_id, []).append(rubbing)

        # Within-file identity and revision conflicts are checked before state
        # changes, including conflicts in low-revision occurrences.
        conflicts: list[str] = []
        for rubbing_id, occurrences in grouped.items():
            by_revision: dict[int, Rubbing] = {}
            for rubbing in occurrences:
                prior = by_revision.get(rubbing.revision)
                if prior is not None and prior != rubbing:
                    conflicts.append(
                        f"rubbing {rubbing_id!r} revision {rubbing.revision}: file "
                        f"contains two different canonical values"
                    )
                else:
                    by_revision[rubbing.revision] = rubbing
            stored = self._states.get(rubbing_id)
            if stored is not None:
                for rubbing in occurrences:
                    if (
                        rubbing.revision == stored.rubbing.revision
                        and rubbing != stored.rubbing
                    ):
                        conflicts.append(
                            f"rubbing {rubbing_id!r} revision {rubbing.revision}: "
                            f"incoming canonical value differs from the accepted value"
                        )
                    if rubbing.tile_id != stored.rubbing.tile_id:
                        conflicts.append(
                            f"rubbing {rubbing_id!r}: cannot move tile "
                            f"{stored.rubbing.tile_id!r} to {rubbing.tile_id!r}"
                        )
        if conflicts:
            return reject(file_id, conflicts)

        fingerprint = _fingerprint(c.writer_version, c.reader_version, grouped)
        existing = self._receipts.get(file_id)
        if existing is not None:
            if existing["fingerprint"] == fingerprint:
                replay_receipt = dict(existing)
                replay_receipt["replay"] = True
                return DominoIngestResult(
                    status="ACCEPTED",
                    file_id=file_id,
                    writer_version=c.writer_version,
                    reader_version=c.reader_version,
                    receipt=replay_receipt,
                    replayed_rubbings=sorted(grouped),
                )
            return reject(
                file_id,
                [
                    f"file_id {file_id!r} was already accepted with different canonical "
                    f"content or contract"
                ],
            )

        staged = dict(self._states)
        applied: list[dict] = []
        ignored: list[dict] = []
        replayed: list[str] = []
        for rubbing_id in sorted(grouped):
            occurrences = grouped[rubbing_id]
            winner = max(occurrences, key=lambda item: item.revision)
            stored = self._states.get(rubbing_id)
            if stored is None:
                staged[rubbing_id] = RubbingState(
                    rubbing=winner, file_id=file_id, sequence=self._sequence + 1
                )
                applied.append(
                    {
                        "rubbing_id": rubbing_id,
                        "tile_id": winner.tile_id,
                        "revision": winner.revision,
                        "action": "insert",
                    }
                )
            elif winner.revision > stored.rubbing.revision:
                staged[rubbing_id] = RubbingState(
                    rubbing=winner, file_id=file_id, sequence=self._sequence + 1
                )
                applied.append(
                    {
                        "rubbing_id": rubbing_id,
                        "tile_id": winner.tile_id,
                        "revision": winner.revision,
                        "previous_revision": stored.rubbing.revision,
                        "action": "supersede",
                    }
                )
            elif winner.revision == stored.rubbing.revision:
                replayed.append(rubbing_id)
            else:
                ignored.append(
                    {
                        "rubbing_id": rubbing_id,
                        "tile_id": winner.tile_id,
                        "revision": winner.revision,
                        "stored_revision": stored.rubbing.revision,
                        "action": "ignored_lower_revision",
                    }
                )

        self._sequence += 1
        self._states = staged
        receipt = {
            "status": "ACCEPTED",
            "file_id": file_id,
            "writer_version": c.writer_version,
            "reader_version": c.reader_version,
            "fingerprint": fingerprint,
            "sequence": self._sequence,
            "rubbing_ids": sorted(grouped),
            "applied": applied,
            "ignored": ignored,
            "replayed_rubbings": replayed,
            "replay": False,
        }
        self._receipts[file_id] = receipt
        return DominoIngestResult(
            status="ACCEPTED",
            file_id=file_id,
            writer_version=c.writer_version,
            reader_version=c.reader_version,
            receipt=dict(receipt),
            applied=applied,
            ignored=ignored,
            replayed_rubbings=replayed,
        )


def _fingerprint(
    writer_version: str, reader_version: str, grouped: dict[str, list[Rubbing]]
) -> str:
    entries = []
    for rubbing_id in sorted(grouped):
        revisions = {}
        for rubbing in grouped[rubbing_id]:
            revisions[rubbing.revision] = rubbing.to_json()
        entries.append([rubbing_id, [[rev, revisions[rev]] for rev in sorted(revisions)]])
    payload = json.dumps(
        {
            "writer_version": writer_version,
            "reader_version": reader_version,
            "rubbings": entries,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
