"""Restore physical domino end pairs from rubbing witnesses.

A rubbing is an oriented view: its left and right written observations must be
flipped together when the physical tile is turned over.  The rivet selects which
physical side occupies the reader's left.  If the rivet was not distinguished,
both orientations are hypotheses of that one rubbing.

Each witness contributes a set of possible *directed physical end pairs*
``(rivet_end_mark, other_end_mark)``.  Restoration intersects all witness sets.
An unobserved written end contributes no evidence, represented by the whole fixed
dictionary rather than by a manufactured default.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Rubbing

DirectedEndPair = tuple[str, str]
UnorderedEndPair = tuple[str, str]


@dataclass(frozen=True)
class WitnessPossibilities:
    rubbing_id: str
    tile_id: str
    rivet_side: str | None
    possibilities: tuple[DirectedEndPair, ...]


@dataclass(frozen=True)
class TileReconstruction:
    tile_id: str
    dictionary: tuple[str, ...]
    witnesses: tuple[WitnessPossibilities, ...]
    directed_pairs: tuple[DirectedEndPair, ...]
    unordered_pairs: tuple[UnorderedEndPair, ...]

    @property
    def empty(self) -> bool:
        """True when the accepted witnesses are mutually incompatible."""

        return not self.directed_pairs

    @property
    def exhibit_ready(self) -> bool:
        """Only a unique unordered physical end pair may enter an exhibition."""

        return len(self.unordered_pairs) == 1

    @property
    def unique_unordered_pair(self) -> UnorderedEndPair | None:
        return self.unordered_pairs[0] if self.exhibit_ready else None

    @property
    def rivet_orientation_determined(self) -> bool:
        """Whether the rivet side can be fixed from real evidence.

        A double tile has the same mark on both ends, so a unique directed pair
        can still have two physically indistinguishable reader orientations.
        """

        if self.empty or not self.exhibit_ready:
            return False
        pair = self.unordered_pairs[0]
        return len(self.directed_pairs) == 1 and pair[0] != pair[1]

    def to_json(self) -> dict:
        return {
            "tile_id": self.tile_id,
            "witness_count": len(self.witnesses),
            "witnesses": [
                {
                    "rubbing_id": item.rubbing_id,
                    "rivet_side": item.rivet_side,
                    "possibilities": [list(pair) for pair in item.possibilities],
                }
                for item in self.witnesses
            ],
            "possible_directed_pairs": [list(pair) for pair in self.directed_pairs],
            "possible_unordered_pairs": [list(pair) for pair in self.unordered_pairs],
            "empty": self.empty,
            "exhibit_ready": self.exhibit_ready,
            "rivet_orientation_determined": self.rivet_orientation_determined,
        }


def rubbing_possibilities(
    rubbing: Rubbing, dictionary: tuple[str, ...]
) -> WitnessPossibilities:
    """Return all physical directed pairs compatible with one rubbing."""

    dictionary_set = set(dictionary)
    left = set(rubbing.left_marks) if rubbing.left_marks is not None else set(dictionary_set)
    right = (
        set(rubbing.right_marks) if rubbing.right_marks is not None else set(dictionary_set)
    )

    hypotheses: list[tuple[str, set[str], set[str]]] = []
    if rubbing.rivet_side is None:
        # Both whole-tile orientations remain possible; left and right are
        # never independently swapped.
        hypotheses.append(("left", left, right))
        hypotheses.append(("right", right, left))
    elif rubbing.rivet_side == "left":
        hypotheses.append(("left", left, right))
    else:
        hypotheses.append(("right", right, left))

    possibilities: set[DirectedEndPair] = set()
    for _hypothesis, rivet_candidates, other_candidates in hypotheses:
        for rivet_mark in sorted(rivet_candidates):
            for other_mark in sorted(other_candidates):
                possibilities.add((rivet_mark, other_mark))
    return WitnessPossibilities(
        rubbing_id=rubbing.rubbing_id,
        tile_id=rubbing.tile_id,
        rivet_side=rubbing.rivet_side,
        possibilities=tuple(sorted(possibilities)),
    )


def _unordered_pair(pair: DirectedEndPair) -> UnorderedEndPair:
    return tuple(sorted(pair))  # type: ignore[return-value]


def reconstruct_tile(
    tile_id: str, witnesses: list[Rubbing], dictionary: tuple[str, ...]
) -> TileReconstruction:
    """Intersect every accepted witness for one physical tile."""

    tile_witnesses = [w for w in witnesses if w.tile_id == tile_id]
    analyzed = tuple(rubbing_possibilities(w, dictionary) for w in tile_witnesses)
    if not analyzed:
        directed: tuple[DirectedEndPair, ...] = ()
    else:
        possible = set(analyzed[0].possibilities)
        for item in analyzed[1:]:
            possible &= set(item.possibilities)
        directed = tuple(sorted(possible))

    unordered = tuple(sorted({_unordered_pair(pair) for pair in directed}))
    return TileReconstruction(
        tile_id=tile_id,
        dictionary=tuple(dictionary),
        witnesses=analyzed,
        directed_pairs=directed,
        unordered_pairs=unordered,
    )


def reconstruct_all(
    witnesses: list[Rubbing], dictionary: tuple[str, ...]
) -> dict[str, TileReconstruction]:
    """Reconstruct every tile represented by an accepted rubbing."""

    tile_ids = sorted({w.tile_id for w in witnesses})
    return {
        tile_id: reconstruct_tile(tile_id, witnesses, dictionary)
        for tile_id in tile_ids
    }


def possible_directed_end_pairs(
    tile_id: str, witnesses: list[Rubbing], dictionary: tuple[str, ...]
) -> tuple[DirectedEndPair, ...]:
    """Librarian query: the still-possible ordered ``(rivet, other)`` pairs."""

    return reconstruct_tile(tile_id, witnesses, dictionary).directed_pairs
