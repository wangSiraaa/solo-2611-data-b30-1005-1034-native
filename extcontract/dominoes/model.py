"""Domain model for rubbing files and the domino restoration library.

A rubbing is one scan of one physical domino.  It records what the scanner
actually saw:

  * ``left_marks`` / ``right_marks`` are ``None`` for an unobserved written end
    and a non-empty list of dictionary candidate numbers for an observed end;
  * ``rivet_side`` is the reader side on which the physical rivet was seen, or
    ``None`` when its orientation was not distinguished.

The reader-facing display fills a missing ``rivet_side`` with ``"left"`` for
old readers.  That fill is deliberately absent from :class:`Rubbing`, so the
restoration layer can never treat display convenience as physical evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RIVET_SIDES = ("left", "right")
DEFAULT_READER_RIVET_SIDE = "left"


@dataclass(frozen=True)
class Rubbing:
    """Canonical, evidence-only content of one rubbing."""

    tile_id: str
    rubbing_id: str
    left_marks: tuple[str, ...] | None
    right_marks: tuple[str, ...] | None
    rivet_side: str | None
    revision: int = 1

    def to_json(self) -> dict:
        return {
            "tile_id": self.tile_id,
            "rubbing_id": self.rubbing_id,
            "revision": self.revision,
            "left_marks": None if self.left_marks is None else list(self.left_marks),
            "right_marks": None if self.right_marks is None else list(self.right_marks),
            "rivet_side": self.rivet_side,
        }


@dataclass
class RubbingState:
    """Stored state keyed by the stable identity of one rubbing."""

    rubbing: Rubbing
    file_id: str
    sequence: int

    def clone(self) -> "RubbingState":
        return RubbingState(rubbing=self.rubbing, file_id=self.file_id, sequence=self.sequence)


@dataclass
class DominoIngestResult:
    """Atomic accept/reject result for a rubbing file."""

    status: str  # "ACCEPTED" | "REJECTED"
    file_id: str | None
    writer_version: str
    reader_version: str
    receipt: dict | None = None
    errors: list[str] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)
    ignored: list[dict] = field(default_factory=list)
    replayed_rubbings: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status == "ACCEPTED"

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "file_id": self.file_id,
            "writer_version": self.writer_version,
            "reader_version": self.reader_version,
            "receipt": self.receipt,
            "errors": list(self.errors),
            "applied": list(self.applied),
            "ignored": list(self.ignored),
            "replayed_rubbings": list(self.replayed_rubbings),
        }


def require_nonempty_string(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where} must be a non-empty string")
    return value


def normalize_mark_candidates(
    raw: Any, *, dictionary: frozenset[str], where: str
) -> tuple[str, ...] | None:
    """Decode a written end: JSON null means unobserved; a list means evidence."""

    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{where} must be null or a non-empty list of dictionary ids")
    candidates: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{where} contains a non-string or empty candidate")
        if item not in dictionary:
            raise ValueError(f"{where} contains candidate {item!r} outside the fixed dictionary")
        if item not in candidates:
            candidates.append(item)
    return tuple(candidates)


def parse_rubbing(raw: Any, *, index: int, dictionary: frozenset[str]) -> Rubbing:
    """Validate one rubbing envelope and decode it into canonical evidence."""

    if not isinstance(raw, dict):
        raise ValueError(f"rubbing #{index} must be an object")
    unknown = set(raw) - {
        "tile_id",
        "rubbing_id",
        "revision",
        "left_marks",
        "right_marks",
        "rivet_side",
    }
    if unknown:
        raise ValueError(f"rubbing #{index}: unexpected keys {sorted(unknown)}")

    tile_id = require_nonempty_string(raw.get("tile_id"), where=f"rubbing #{index}.tile_id")
    rubbing_id = require_nonempty_string(
        raw.get("rubbing_id"), where=f"rubbing #{index}.rubbing_id"
    )
    revision = raw.get("revision", 1)
    if type(revision) is not int or revision < 1:
        raise ValueError(f"rubbing {rubbing_id!r}: revision must be a positive integer")

    rivet_side = raw.get("rivet_side", None)
    if rivet_side is not None and rivet_side not in RIVET_SIDES:
        raise ValueError(
            f"rubbing {rubbing_id!r}: rivet_side must be one of "
            f"{list(RIVET_SIDES)} or null"
        )

    left = normalize_mark_candidates(
        raw.get("left_marks"), dictionary=dictionary, where=f"rubbing {rubbing_id!r}.left_marks"
    )
    right = normalize_mark_candidates(
        raw.get("right_marks"), dictionary=dictionary, where=f"rubbing {rubbing_id!r}.right_marks"
    )

    return Rubbing(
        tile_id=tile_id,
        rubbing_id=rubbing_id,
        left_marks=left,
        right_marks=right,
        rivet_side=rivet_side,
        revision=revision,
    )
