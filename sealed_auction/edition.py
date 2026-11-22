"""Turn settled auction units into a singable two-voice proofreading edition.

The auction decides *who* may sign (one settled unit buys one complete sung
note), the pre-registered permissions decide *where* they may sign, and this
module decides *which* numbered proposals can be printed together.

Model
-----
* A score has an upper ("high") and a lower ("low") voice, one integer pitch
  per beat.
* Adjacent equal pitches in one voice may be joined by an explicit tie into a
  single sustained note.  A tied run is one *original item*: a proposal must
  name **every** beat of it and a change is written into every one of those
  beats.  Two equal pitches with no tie are two separate original items.
* A proposal spends one settled unit, names one complete item and one new
  pitch, and must lie entirely inside that buyer's registered voice/measure
  permissions.
* An edition is singable when every beat's vertical interval (high - low) is
  allowed, both voices stay in range, and no two adjacent beats keep the same
  perfect fifth/octave while both voices move in the same direction.

Selection maximises total editorial value; a tie is broken by changing fewer
original score symbols (beats), then by a stable proposal-id order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .core import DomainError, require_int, require_nonempty

HIGH = "high"
LOW = "low"
VOICES = (HIGH, LOW)
MAX_BEATS = 12
MAX_PROPOSALS = 12


# --------------------------------------------------------------------------- #
# Score
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Item:
    """One original note: a single beat, or a whole explicit tie."""

    voice: str
    beats: tuple[int, ...]
    pitch: int
    tied: bool


@dataclass(frozen=True)
class Score:
    """The manuscript to be corrected.

    ``high_ties``/``low_ties`` are inclusive ``(start, end)`` beat spans.  Only
    adjacent, equal pitches may be tied.  ``allowed_intervals`` and the two
    ranges are inclusive.
    """

    high: tuple[int, ...]
    low: tuple[int, ...]
    measures: tuple[int, ...]
    allowed_intervals: frozenset[int]
    high_range: tuple[int, int] = (0, 12)
    low_range: tuple[int, int] = (0, 12)
    high_ties: tuple[tuple[int, int], ...] = ()
    low_ties: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        # Accept lists and normalise into immutable tuples.
        object.__setattr__(self, "high", tuple(self.high))
        object.__setattr__(self, "low", tuple(self.low))
        object.__setattr__(self, "measures", tuple(self.measures))
        object.__setattr__(self, "allowed_intervals", frozenset(self.allowed_intervals))
        object.__setattr__(self, "high_range", tuple(self.high_range))
        object.__setattr__(self, "low_range", tuple(self.low_range))

        n = len(self.high)
        if not 1 <= n <= MAX_BEATS:
            raise DomainError(f"score must have 1..{MAX_BEATS} beats, got {n}")
        if len(self.low) != n:
            raise DomainError("both voices must have the same number of beats")
        if len(self.measures) != n:
            raise DomainError("measures must name one measure per beat")
        for voice, pitches in ((HIGH, self.high), (LOW, self.low)):
            for i, pitch in enumerate(pitches):
                require_int(pitch, f"{voice} pitch at beat {i}")
        for i, measure in enumerate(self.measures):
            require_int(measure, f"measure at beat {i}", minimum=0)
        if any(
            self.measures[i] > self.measures[i + 1] for i in range(n - 1)
        ):
            raise DomainError("measures must be non-decreasing over beats")
        if not self.allowed_intervals:
            raise DomainError("allowed_intervals must not be empty")
        for interval in self.allowed_intervals:
            require_int(interval, "allowed interval")
        for voice, bounds in ((HIGH, self.high_range), (LOW, self.low_range)):
            if len(bounds) != 2:
                raise DomainError(f"{voice}_range must be (low, high)")
            lo, hi = bounds
            require_int(lo, f"{voice} range lower bound")
            require_int(hi, f"{voice} range upper bound")
            if lo > hi:
                raise DomainError(f"{voice}_range lower bound exceeds upper bound")
        object.__setattr__(
            self, "high_ties", self._validated_ties(self.high, HIGH, self.high_ties)
        )
        object.__setattr__(
            self, "low_ties", self._validated_ties(self.low, LOW, self.low_ties)
        )

    @staticmethod
    def _validated_ties(
        pitches: tuple[int, ...], voice: str, ties: Iterable[tuple[int, int]]
    ) -> tuple[tuple[int, int], ...]:
        spans: list[tuple[int, int]] = []
        seen: set[int] = set()
        for span in ties:
            if len(tuple(span)) != 2:
                raise DomainError(f"{voice} tie must be a (start, end) pair")
            start, end = span
            require_int(start, f"{voice} tie start", minimum=0)
            require_int(end, f"{voice} tie end", minimum=0)
            if start >= end:
                raise DomainError(f"{voice} tie must join at least two beats")
            if end >= len(pitches):
                raise DomainError(f"{voice} tie {span} exceeds the score")
            if any(pitches[i] != pitches[start] for i in range(start, end + 1)):
                raise DomainError(
                    f"{voice} tie {span} joins different pitches; only equal "
                    "adjacent pitches may be tied"
                )
            for i in range(start, end + 1):
                if i in seen:
                    raise DomainError(f"{voice} ties overlap at beat {i}")
                seen.add(i)
            spans.append((start, end))
        return tuple(sorted(spans))

    def items(self) -> tuple[Item, ...]:
        """All original items: each declared tie plus every untied beat."""
        result: list[Item] = []
        for voice, pitches, ties in (
            (HIGH, self.high, self.high_ties),
            (LOW, self.low, self.low_ties),
        ):
            tied: set[int] = set()
            for start, end in ties:
                beats = tuple(range(start, end + 1))
                result.append(Item(voice, beats, pitches[start], True))
                tied.update(beats)
            for i, pitch in enumerate(pitches):
                if i not in tied:
                    result.append(Item(voice, (i,), pitch, False))
        return tuple(result)

    def item_index(self) -> dict[tuple[str, tuple[int, ...]], Item]:
        return {(item.voice, item.beats): item for item in self.items()}


# --------------------------------------------------------------------------- #
# Permissions, allocations and proposals
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Permissions:
    """Buyer -> set of ``(voice, measure)`` pairs they registered to edit."""

    entries: Mapping[str, frozenset[tuple[str, int]]] = field(default_factory=dict)

    @classmethod
    def from_pairs(
        cls, pairs: Iterable[tuple[str, str, int]]
    ) -> "Permissions":
        entries: dict[str, set[tuple[str, int]]] = {}
        for buyer, voice, measure in pairs:
            require_nonempty(buyer, "buyer")
            if voice not in VOICES:
                raise DomainError(f"unknown voice {voice!r}")
            require_int(measure, "measure", minimum=0)
            entries.setdefault(buyer, set()).add((voice, measure))
        return cls({buyer: frozenset(v) for buyer, v in entries.items()})

    def allows(self, buyer: str, voice: str, measure: int) -> bool:
        return (voice, measure) in self.entries.get(buyer, frozenset())


@dataclass(frozen=True)
class Allocation:
    """A settled auction result: units actually won, plus the winning bid."""

    buyer: str
    units: int
    bid_id: str | None = None
    hold_id: str | None = None
    unit_price_cents: int | None = None

    def __post_init__(self) -> None:
        require_nonempty(self.buyer, "buyer")
        require_int(self.units, "units", minimum=0)


@dataclass(frozen=True)
class Proposal:
    """One numbered editorial proposal signed by one settled buyer."""

    proposal_id: str
    buyer: str
    voice: str
    beats: tuple[int, ...]
    new_pitch: int
    value: int

    def __post_init__(self) -> None:
        require_nonempty(self.proposal_id, "proposal_id")
        require_nonempty(self.buyer, "buyer")
        if self.voice not in VOICES:
            raise DomainError(f"proposal {self.proposal_id}: unknown voice {self.voice!r}")
        beats = tuple(sorted(int(b) for b in self.beats))
        if len(set(beats)) != len(beats):
            raise DomainError(f"proposal {self.proposal_id}: duplicate beats")
        require_int(self.new_pitch, "new_pitch")
        require_int(self.value, "value")
        object.__setattr__(self, "beats", beats)


def _coerce_permissions(value: Any) -> Permissions:
    if isinstance(value, Permissions):
        return value
    if isinstance(value, Mapping):
        # {buyer: {(voice, measure), ...}}
        pairs = [
            (buyer, voice, measure)
            for buyer, allowed in value.items()
            for voice, measure in allowed
        ]
        return Permissions.from_pairs(pairs)
    return Permissions.from_pairs(value)


def _coerce_allocations(value: Any) -> dict[str, Allocation]:
    """Accept {buyer: Allocation}, {buyer: units}, or an iterable of Allocation."""
    result: dict[str, Allocation] = {}
    if isinstance(value, Mapping):
        for buyer, allocation in value.items():
            if isinstance(allocation, Allocation):
                result[allocation.buyer] = allocation
            else:
                result[buyer] = Allocation(buyer, int(allocation))
        return result
    for allocation in value:
        if not isinstance(allocation, Allocation):
            raise DomainError("allocations must be Allocation objects")
        result[allocation.buyer] = allocation
    return result


def allocations_from_auction(
    auctions: Any, auction_id: str, require_cleared: bool = True
) -> dict[str, Allocation]:
    """Read settled winning units and bid ids out of ``AuctionService``.

    A buyer who won nothing is omitted: a signature without a completed
    transaction may not enter the collation table.
    """
    result = auctions.read_result(auction_id)
    if result.get("status") in (None, "OPEN"):
        raise DomainError(f"auction {auction_id} has no frozen result yet")
    if require_cleared and result.get("settlement_status") != "CLEARED":
        raise DomainError(
            f"auction {auction_id} is not settled; winning units are not yet real"
        )
    allocations: dict[str, Allocation] = {}
    for entry in result.get("allocations", []):
        units = int(entry.get("allocated_qty", 0))
        if units <= 0:
            continue
        allocations[entry["buyer"]] = Allocation(
            buyer=entry["buyer"],
            units=units,
            bid_id=entry.get("bid_id"),
            hold_id=entry.get("hold_id"),
            unit_price_cents=entry.get("unit_price_cents"),
        )
    return allocations


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProposalCheck:
    proposal: Proposal
    ok: bool
    reason: str | None
    item: Item | None


def _target_item(
    score: Score, proposal: Proposal, item_index: Mapping[tuple[str, tuple[int, ...]], Item]
) -> tuple[Item | None, str | None]:
    """Resolve a proposal's beats to exactly one complete original item."""
    key = (proposal.voice, proposal.beats)
    if key in item_index:
        return item_index[key], None

    voice_items = [item for item in score.items() if item.voice == proposal.voice]
    beats = set(proposal.beats)
    containing = [item for item in voice_items if beats < set(item.beats)]
    if len(containing) == 1 and containing[0].tied:
        return None, "half of a tie: a change must cover every tied note"
    overlapping = [item for item in voice_items if beats & set(item.beats)]
    if not overlapping:
        return None, "proposal names no original note of this voice"
    if len(overlapping) > 1:
        return (
            None,
            "proposal spans several original notes; one settled unit changes "
            "one complete sung note",
        )
    return None, "not a complete original note of the manuscript"


def check_proposals(
    score: Score,
    permissions: Permissions | Any,
    allocations: Mapping[str, Allocation] | Any,
    proposals: Sequence[Proposal],
) -> list[ProposalCheck]:
    """Decide which numbered proposals may enter the collation table."""
    perms = _coerce_permissions(permissions)
    allocs = _coerce_allocations(allocations)
    item_index = score.item_index()
    seen: set[str] = set()
    checks: list[ProposalCheck] = []

    for proposal in proposals:
        if not isinstance(proposal, Proposal):
            raise DomainError("proposals must be Proposal objects")
        item: Item | None = None
        reason: str | None = None

        if proposal.proposal_id in seen:
            reason = "duplicate proposal number"
        else:
            seen.add(proposal.proposal_id)
            item, reason = _target_item(score, proposal, item_index)
            if item is not None:
                allocation = allocs.get(proposal.buyer)
                if allocation is None or allocation.units <= 0:
                    reason = (
                        "signature has no completed transaction: the buyer won "
                        "no settled unit"
                    )
                    item = None
                else:
                    for beat in proposal.beats:
                        measure = score.measures[beat]
                        if not perms.allows(proposal.buyer, proposal.voice, measure):
                            reason = (
                                f"not authorized for {proposal.voice} measure "
                                f"{measure} (all measures of the note must be "
                                "registered)"
                            )
                            item = None
                            break
            if item is not None and reason is None:
                if proposal.new_pitch == item.pitch:
                    reason = "proposed pitch is unchanged"
                    item = None

        checks.append(
            ProposalCheck(
                proposal=proposal,
                ok=reason is None and item is not None,
                reason=reason,
                item=item,
            )
        )
    return checks


# --------------------------------------------------------------------------- #
# Singability and search
# --------------------------------------------------------------------------- #


def _apply(score: Score, selected: Sequence[Proposal]) -> dict[str, list[int]]:
    pitches = {HIGH: list(score.high), LOW: list(score.low)}
    for proposal in selected:
        for beat in proposal.beats:
            pitches[proposal.voice][beat] = proposal.new_pitch
    return pitches


def _violations(score: Score, pitches: Mapping[str, Sequence[int]]) -> list[str]:
    found: list[str] = []
    ranges = {HIGH: score.high_range, LOW: score.low_range}
    for voice in VOICES:
        lo, hi = ranges[voice]
        for beat, pitch in enumerate(pitches[voice]):
            if not lo <= pitch <= hi:
                found.append(
                    f"beat {beat}: {voice} pitch {pitch} outside range [{lo},{hi}]"
                )
    intervals: list[int | None] = []
    for beat in range(len(score.high)):
        interval = pitches[HIGH][beat] - pitches[LOW][beat]
        intervals.append(interval)
        if interval not in score.allowed_intervals:
            found.append(
                f"beat {beat}: vertical interval {interval} not in "
                f"{sorted(score.allowed_intervals)}"
            )
    for beat in range(len(score.high) - 1):
        first, second = intervals[beat], intervals[beat + 1]
        if first != second or first not in (7, 12):
            continue
        high_move = pitches[HIGH][beat + 1] - pitches[HIGH][beat]
        low_move = pitches[LOW][beat + 1] - pitches[LOW][beat]
        both_move = high_move != 0 and low_move != 0
        if both_move and (high_move > 0) == (low_move > 0):
            kind = "fifth" if first == 7 else "octave"
            found.append(
                f"beats {beat}-{beat + 1}: parallel {kind} with both voices "
                "moving in the same direction"
            )
    return found


# --------------------------------------------------------------------------- #
# Public builder
# --------------------------------------------------------------------------- #


def collation_table(
    score: Score,
    permissions: Permissions | Any,
    allocations: Mapping[str, Allocation] | Any,
    proposals: Sequence[Proposal],
) -> dict[str, Any]:
    """Admissibility of one signed draft, per person, before any selection.

    Answers "how many strokes does each signatory use" and traces every
    admitted item to its winning bid and original note, without deciding
    which competing proposals the printed edition keeps.
    """
    if not isinstance(score, Score):
        raise DomainError("score must be a Score")
    perms = _coerce_permissions(permissions)
    allocs = _coerce_allocations(allocations)
    proposals = tuple(proposals)
    if len(proposals) > MAX_PROPOSALS:
        raise DomainError(f"at most {MAX_PROPOSALS} proposals are allowed")

    checks = check_proposals(score, perms, allocs, proposals)

    rows: list[dict[str, Any]] = []
    admitted_by_note: dict[tuple[str, tuple[int, ...]], list[str]] = {}
    per_buyer: dict[str, dict[str, Any]] = {}
    for check in checks:
        proposal = check.proposal
        if check.ok:
            admitted_by_note.setdefault((proposal.voice, proposal.beats), []).append(
                proposal.proposal_id
            )
            allocation = allocs[proposal.buyer]
            usage = per_buyer.setdefault(
                proposal.buyer,
                {
                    "allocated_units": allocation.units,
                    "admitted_items": 0,
                    "proposal_ids": [],
                },
            )
            usage["admitted_items"] += 1
            usage["proposal_ids"].append(proposal.proposal_id)
        rows.append(
            {
                "proposal_id": proposal.proposal_id,
                "buyer": proposal.buyer,
                "admitted": check.ok,
                "reason": check.reason,
                "voice": proposal.voice,
                "beats": list(proposal.beats),
                "measures": [score.measures[b] for b in proposal.beats],
                "original_pitches": (
                    [check.item.pitch] * len(proposal.beats) if check.item else None
                ),
                "tied": check.item.tied if check.item else None,
                "new_pitch": proposal.new_pitch,
                "value": proposal.value,
                "bid_id": (
                    allocs[proposal.buyer].bid_id
                    if proposal.buyer in allocs
                    else None
                ),
            }
        )

    for buyer, usage in per_buyer.items():
        usage["within_units"] = usage["admitted_items"] <= usage["allocated_units"]

    duplicates = [
        {
            "voice": voice,
            "beats": list(beats),
            "proposal_ids": ids,
        }
        for (voice, beats), ids in admitted_by_note.items()
        if len(ids) > 1
    ]

    return {
        "rows": rows,
        "per_buyer": per_buyer,
        "duplicate_note_claims": duplicates,
        "all_admitted": all(row["admitted"] for row in rows),
    }


def build_edition(
    score: Score,
    permissions: Permissions | Any,
    allocations: Mapping[str, Allocation] | Any,
    proposals: Sequence[Proposal],
) -> dict[str, Any]:
    """Choose proposals, then report the beat-by-beat singing edition.

    The result is JSON-serialisable.  ``feasible`` is false when the manuscript
    has no allowed complete version; ``reason`` then explains why.
    """
    if not isinstance(score, Score):
        raise DomainError("score must be a Score")
    perms = _coerce_permissions(permissions)
    allocs = _coerce_allocations(allocations)
    proposals = tuple(proposals)
    if len(proposals) > MAX_PROPOSALS:
        raise DomainError(f"at most {MAX_PROPOSALS} proposals are allowed")

    checks = check_proposals(score, perms, allocs, proposals)
    valid = [check.proposal for check in checks if check.ok]
    rejected = [
        {"proposal_id": check.proposal.proposal_id, "reason": check.reason}
        for check in checks
        if not check.ok
    ]

    # Proposals that revise the same original note are mutually exclusive.
    by_item: dict[tuple[str, tuple[int, ...]], list[str]] = {}
    for proposal in valid:
        by_item.setdefault((proposal.voice, proposal.beats), []).append(
            proposal.proposal_id
        )
    conflicts = [
        {
            "voice": voice,
            "beats": list(beats),
            "proposal_ids": ids,
        }
        for (voice, beats), ids in by_item.items()
        if len(ids) > 1
    ]

    failure_samples: set[str] = set()
    best: dict[str, Any] | None = None
    searched = 1 << len(valid)

    for mask in range(searched):
        selected = [valid[i] for i in range(len(valid)) if mask & (1 << i)]

        counts: dict[str, int] = {}
        skip = False
        for proposal in selected:
            counts[proposal.buyer] = counts.get(proposal.buyer, 0) + 1
            if counts[proposal.buyer] > allocs[proposal.buyer].units:
                failure_samples.add(
                    f"buyer {proposal.buyer} exceeds their settled units"
                )
                skip = True
                break
        if skip:
            continue

        keys = [(proposal.voice, proposal.beats) for proposal in selected]
        if len(set(keys)) != len(keys):
            failure_samples.add("the same original note is revised twice")
            continue

        pitches = _apply(score, selected)
        violations = _violations(score, pitches)
        if violations:
            failure_samples.update(violations)
            continue

        value = sum(proposal.value for proposal in selected)
        changed_notes = sum(len(proposal.beats) for proposal in selected)
        ids = tuple(sorted(proposal.proposal_id for proposal in selected))
        key = (value, -changed_notes)
        if (
            best is None
            or key > best["key"]
            or (key == best["key"] and ids < best["ids"])
        ):
            best = {
                "key": key,
                "ids": ids,
                "selected": selected,
                "pitches": pitches,
                "value": value,
                "changed_notes": changed_notes,
            }

    diagnostics = {
        "searched_subsets": searched,
        "valid_proposals": len(valid),
        "rejected_proposals": len(rejected),
        "constraint_failures": sorted(failure_samples),
    }

    if best is None:
        if not valid:
            base_violations = _violations(
                score, {HIGH: score.high, LOW: score.low}
            )
            reason = (
                "no proposal may enter the collation table, and the manuscript "
                "itself is not singable: " + "; ".join(base_violations[:5])
            )
        else:
            reason = (
                "no allowed complete version exists: every combination of "
                "valid proposals (including no change) violates "
                + "; ".join(sorted(failure_samples)[:5])
            )
        return {
            "feasible": False,
            "reason": reason,
            "total_value": None,
            "changed_notes": None,
            "beats": None,
            "selections": [],
            "receipts": [],
            "collation": {},
            "buyer_usage": {},
            "rejected_proposals": rejected,
            "conflicting_proposals": conflicts,
            "unselected_proposals": [],
            "diagnostics": diagnostics,
        }

    selected = best["selected"]
    pitches = best["pitches"]
    selected_ids = {proposal.proposal_id for proposal in selected}

    # Beat-by-beat printed music.
    beats: list[dict[str, Any]] = []
    for beat in range(len(score.high)):
        changed_voices = [
            voice
            for voice in VOICES
            if pitches[voice][beat] != (score.high if voice == HIGH else score.low)[beat]
        ]
        beats.append(
            {
                "beat": beat,
                "measure": score.measures[beat],
                "high": pitches[HIGH][beat],
                "low": pitches[LOW][beat],
                "interval": pitches[HIGH][beat] - pitches[LOW][beat],
                "changed_voices": changed_voices,
            }
        )

    def receipt_for(proposal: Proposal, units_before: int, units_after: int) -> dict[str, Any]:
        allocation = allocs[proposal.buyer]
        item = score.item_index()[(proposal.voice, proposal.beats)]
        return {
            "proposal_id": proposal.proposal_id,
            "buyer": proposal.buyer,
            "bid_id": allocation.bid_id,
            "hold_id": allocation.hold_id,
            "unit_price_cents": allocation.unit_price_cents,
            "allocated_units": allocation.units,
            "units_used": 1,
            "units_before": units_before,
            "units_after": units_after,
            "voice": proposal.voice,
            "beats": list(proposal.beats),
            "measures": [score.measures[b] for b in proposal.beats],
            "original_pitches": [item.pitch] * len(proposal.beats),
            "tied": item.tied,
            "new_pitch": proposal.new_pitch,
            "value": proposal.value,
        }

    usage: dict[str, dict[str, Any]] = {
        buyer: {
            "bid_id": allocation.bid_id,
            "hold_id": allocation.hold_id,
            "allocated_units": allocation.units,
            "written_items": 0,
            "value": 0,
            "items": [],
        }
        for buyer, allocation in allocs.items()
        if allocation.units > 0
    }
    selections: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for proposal in sorted(selected, key=lambda p: (p.buyer, p.proposal_id)):
        entry = usage[proposal.buyer]
        before = entry["written_items"]
        after = before + 1
        item = score.item_index()[(proposal.voice, proposal.beats)]
        entry["written_items"] = after
        entry["value"] += proposal.value
        receipt = receipt_for(proposal, before, after)
        entry["items"].append(receipt)
        receipts.append(receipt)
        selections.append(
            {
                "proposal_id": proposal.proposal_id,
                "buyer": proposal.buyer,
                "voice": proposal.voice,
                "beats": list(proposal.beats),
                "measures": [score.measures[b] for b in proposal.beats],
                "original_pitches": [item.pitch] * len(proposal.beats),
                "tied": item.tied,
                "new_pitch": proposal.new_pitch,
                "value": proposal.value,
            }
        )

    for buyer, entry in usage.items():
        entry["remaining_units"] = entry["allocated_units"] - entry["written_items"]

    # Explain every valid proposal that was not printed.
    unselected: list[dict[str, Any]] = []
    for proposal in valid:
        if proposal.proposal_id in selected_ids:
            continue
        used = usage.get(proposal.buyer, {}).get("written_items", 0)
        allocation = allocs[proposal.buyer]
        if any(
            (other.voice, other.beats) == (proposal.voice, proposal.beats)
            for other in selected
        ):
            reason = "the same original note is already revised by another proposal"
        elif used >= allocation.units:
            reason = (
                f"buyer {proposal.buyer} has only {allocation.units} settled "
                "unit(s) and they are already used"
            )
        else:
            trial = list(selected) + [proposal]
            trial_violations = _violations(score, _apply(score, trial))
            if trial_violations:
                reason = "printing it would make the edition unsingable: " + (
                    trial_violations[0]
                )
            elif proposal.value <= 0:
                reason = "no positive editorial value"
            else:
                reason = "not needed for the highest-value singable edition"
        unselected.append(
            {"proposal_id": proposal.proposal_id, "reason": reason}
        )

    return {
        "feasible": True,
        "reason": None,
        "total_value": best["value"],
        "changed_notes": best["changed_notes"],
        "beats": beats,
        "selections": selections,
        "receipts": receipts,
        "collation": usage,
        "buyer_usage": {
            buyer: {
                "allocated_units": entry["allocated_units"],
                "written_items": entry["written_items"],
                "remaining_units": entry["remaining_units"],
                "value": entry["value"],
            }
            for buyer, entry in usage.items()
        },
        "rejected_proposals": rejected,
        "conflicting_proposals": conflicts,
        "unselected_proposals": unselected,
        "diagnostics": diagnostics,
    }
