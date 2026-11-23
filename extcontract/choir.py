"""Choir audition evidence, proven ranges, and whole-performance scoring.

This module is deliberately downstream of :mod:`extcontract.ingest`.  It does
not trust the raw source files; it consumes the canonical ``reader-records/v1``
snapshot produced by an :class:`~extcontract.ingest.IngestionLibrary` (including
per-field ``sources`` provenance).  That is what lets it distinguish an actual
ending signature from a reader ``default`` fill.

Evidence rules (the brief)
--------------------------
An audition is one actual onset, an ordered string of measured integer
semitones, and one ending, all tied to a singer, one audition id, and a
position (``seq``).  A *complete* audition proves a continuous range from its
lowest to its highest measured pitch **only when every pair of adjacent
measured pitches differs by at most one semitone**.  A leap, a missing
measurement, a same-audition/same-position conflict, or an unsigned ending
means no continuous range is signed from that audition.

A complete, signed one-note audition may be registered as a single-point
region.  Proven regions that share a pitch (overlap or equal endpoint) join at
that common pitch; two regions that are merely adjacent but share no pitch may
not be bridged.  The coach can therefore query proven ranges but can never
paint everything between two isolated notes as singable.

Performance rules
-----------------
* The pianist uses **one** score for the whole piece.  Each score transposes
  every voice and instrumental overpass by the same integer semitones; the
  original key is one of the scores.
* An overpass may only be played if its *actual* (post-transposition) pitch is
  in the orchestra's available absolute-pitch set.
* A phrase must fall wholly inside **one** proven region of its singer.
* Within a voice, adjacent phrases may change singer only across a hand-over
  rest; each change counts as one switch.
* Regular members are free; a guest pays one fixed appearance fee for the
  whole piece no matter how many phrases they sing.

Optimisation modes
------------------
* ``mode="budget"``: given a total-spend cap, minimise switches, then cost.
* ``mode="switch"``: given a switch cap, minimise cost, then switches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Small value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Region:
    """A proven singable integer-pitch range, with the auditions backing it."""

    low: int
    high: int
    sources: tuple[str, ...] = ()

    def contains(self, low: int, high: int) -> bool:
        return self.low <= low and high <= self.high


@dataclass
class AuditionResult:
    audition: str
    singer: str | None
    measured: list[int]
    status: str  # "proven_interval" | "proven_point" | "no_evidence"
    region: Region | None
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "audition": self.audition,
            "singer": self.singer,
            "measured": list(self.measured),
            "status": self.status,
            "region": None
            if self.region is None
            else [self.region.low, self.region.high],
            "sources": [] if self.region is None else list(self.region.sources),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Phrase:
    voice_id: str
    phrase_id: str
    low: int
    high: int
    order: int
    rest_after: bool


@dataclass(frozen=True)
class Voice:
    voice_id: str
    name: str
    phrases: tuple[Phrase, ...]


@dataclass(frozen=True)
class Overpass:
    overpass_id: str
    pitch: int
    between: tuple[str, ...] = ()


@dataclass(frozen=True)
class Piece:
    piece_id: str
    title: str
    voices: tuple[Voice, ...]
    overpasses: tuple[Overpass, ...]

    @property
    def phrase_count(self) -> int:
        return sum(len(v.phrases) for v in self.voices)


@dataclass(frozen=True)
class Score:
    score_id: str
    label: str
    transpose: int
    fee: int


@dataclass(frozen=True)
class Singer:
    singer: str
    kind: str  # "member" | "guest"
    fee: int
    available: bool = True

    @property
    def effective_fee(self) -> int:
        return 0 if self.kind == "member" else self.fee


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def parse_piece(doc: dict) -> Piece:
    voices = []
    for v in doc["voices"]:
        phrases = tuple(
            Phrase(
                voice_id=v["voice_id"],
                phrase_id=p["phrase_id"],
                low=int(p["low"]),
                high=int(p["high"]),
                order=int(p.get("order", i)),
                rest_after=bool(p.get("rest_after", False)),
            )
            for i, p in enumerate(v["phrases"])
        )
        # Phrase order is explicit; keep it stable for adjacency/switch rules.
        phrases = tuple(sorted(phrases, key=lambda p: p.order))
        voices.append(Voice(voice_id=v["voice_id"], name=v.get("name", v["voice_id"]), phrases=phrases))
    overpasses = tuple(
        Overpass(
            overpass_id=o["overpass_id"],
            pitch=int(o["pitch"]),
            between=tuple(o.get("between", ())),
        )
        for o in doc.get("overpasses", [])
    )
    return Piece(
        piece_id=doc["piece_id"],
        title=doc.get("title", doc["piece_id"]),
        voices=tuple(voices),
        overpasses=overpasses,
    )


def parse_scores(doc: dict) -> list[Score]:
    return [
        Score(
            score_id=s["score_id"],
            label=s.get("label", s["score_id"]),
            transpose=int(s["transpose"]),
            fee=int(s.get("fee", 0)),
        )
        for s in doc["scores"]
    ]


def parse_singers(doc: dict) -> dict[str, Singer]:
    out = {}
    for s in doc["singers"]:
        out[s["singer"]] = Singer(
            singer=s["singer"],
            kind=s["kind"],
            fee=int(s.get("fee", 0)),
            available=bool(s.get("available", True)),
        )
    return out


# ---------------------------------------------------------------------------
# Evidence: auditions -> per-singer proven regions
# ---------------------------------------------------------------------------

def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _ending_is_actually_signed(row: dict) -> bool:
    """True iff the ending is a real writer-supplied signature.

    A reader ``default`` (``fill_default``) is explicitly rejected: it may look
    like the text ``完成`` but it is not an actual arrival signature.
    """
    sources = row.get("sources", {})
    ending_source = sources.get("ending", {})
    if "default" in ending_source:
        return False
    if ending_source.get("conversion") == "fill_default":
        return False
    value = row.get("values", {}).get("ending")
    if value is None:
        return False
    # A writer-supplied column carries its original field id and input value.
    return "field_id" in ending_source and ending_source.get("input") is not None


def _diagnose_audition(audition: str, rows: list[dict]) -> AuditionResult:
    reasons: list[str] = []
    parsed = []
    for row in rows:
        values = row.get("values", {})
        parsed.append(
            {
                "seq": int(values.get("seq")),
                "singer": values.get("singer"),
                "pitch": _int_or_none(values.get("pitch")),
                "kind": values.get("kind"),
                "signed": _ending_is_actually_signed(row),
            }
        )
    parsed.sort(key=lambda r: r["seq"])

    singers = {r["singer"] for r in parsed}
    singer = parsed[0]["singer"] if parsed else None
    if len(singers) != 1:
        reasons.append("audition 绑定了多个歌手（应同一位歌手）")

    # Same audition + same position conflict (defensive; the ingestion layer
    # normally rejects these atomically before they can be published).
    seen_seq: dict[int, int | None] = {}
    for r in parsed:
        if r["seq"] in seen_seq and seen_seq[r["seq"]] != r["pitch"]:
            reasons.append(f"同号同序冲突：seq {r['seq']} 出现不同音高")
        seen_seq.setdefault(r["seq"], r["pitch"])

    seqs = [r["seq"] for r in parsed]
    if seqs and seqs != list(range(seqs[0], seqs[0] + len(seqs))):
        reasons.append("序位不连续（存在缺测）")

    onsets = [r for r in parsed if r["kind"] == "onset"]
    ends = [r for r in parsed if r["kind"] == "end"]
    samples = [r for r in parsed if r["kind"] == "sample"]

    if len(onsets) != 1:
        reasons.append("缺少唯一实到起音")
    elif onsets[0]["pitch"] is None:
        reasons.append("起音未实到（音高缺测）")
    if len(ends) != 1:
        reasons.append("缺少唯一结束位")
    elif not ends[0]["signed"]:
        reasons.append("收尾未签（结束位不是实到签字；reader 默认不算）")
    if onsets and ends and onsets[0]["seq"] != seqs[0]:
        reasons.append("起音不在序位首位")
    if ends and ends[0]["seq"] != seqs[-1]:
        reasons.append("结束位不在序位末位")

    # The continuous-range proof uses the actual onset plus the measured
    # samples.  The ending row is an arrival *signature*; its pitch (if any)
    # is not a separate measurement.
    measured_rows = onsets + samples
    if any(r["pitch"] is None for r in measured_rows):
        reasons.append("缺测：起音/测量音高为空")
    measured = [r["pitch"] for r in measured_rows if r["pitch"] is not None]

    if not measured:
        reasons.append("没有可用实测音高")

    if reasons:
        return AuditionResult(
            audition=audition,
            singer=singer,
            measured=measured,
            status="no_evidence",
            region=None,
            reasons=reasons,
        )

    # Complete, signed audition with all measurements present.
    leaps = [
        (a, b) for a, b in zip(measured, measured[1:]) if abs(b - a) > 1
    ]
    if leaps:
        return AuditionResult(
            audition=audition,
            singer=singer,
            measured=measured,
            status="no_evidence",
            region=None,
            reasons=[
                "过跳：相邻实测相差超过一个半音，"
                "不能据此签连续音区（也不能把两端之间全涂成会唱）"
            ],
        )

    low, high = min(measured), max(measured)
    status = "proven_point" if low == high else "proven_interval"
    return AuditionResult(
        audition=audition,
        singer=singer,
        measured=measured,
        status=status,
        region=Region(low=low, high=high, sources=(audition,)),
        reasons=[],
    )


def certify_auditions(records: dict) -> list[AuditionResult]:
    """Diagnose every audition in a published ``reader-records/v1`` snapshot."""
    rows = records.get("rows", [])
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        audition = row.get("values", {}).get("audition")
        grouped.setdefault(str(audition), []).append(row)
    return [
        _diagnose_audition(audition, grouped[audition])
        for audition in sorted(grouped)
    ]


def merge_regions(regions: Iterable[Region]) -> list[Region]:
    """Merge proven regions only where they share a pitch.

    ``[5,6]`` + ``[6,8]`` merge at 6; ``[5,6]`` + ``[7,8]`` stay separate even
    though they are adjacent, because they share no measured pitch.
    """
    ordered = sorted(regions, key=lambda r: (r.low, r.high))
    merged: list[Region] = []
    for region in ordered:
        if merged and region.low <= merged[-1].high:
            current = merged[-1]
            merged[-1] = Region(
                low=current.low,
                high=max(current.high, region.high),
                sources=tuple(dict.fromkeys(current.sources + region.sources)),
            )
        else:
            merged.append(region)
    return merged


def proven_regions(records: dict) -> dict[str, list[Region]]:
    """Per-singer merged proven regions from a published snapshot."""
    by_singer: dict[str, list[Region]] = {}
    for result in certify_auditions(records):
        if result.region is not None and result.singer is not None:
            by_singer.setdefault(result.singer, []).append(result.region)
    return {singer: merge_regions(regions) for singer, regions in by_singer.items()}


def coach_query(records: dict) -> dict:
    """What the coach can look up: auditions and the proven ranges they support."""
    diagnoses = certify_auditions(records)
    regions = proven_regions(records)
    return {
        "auditions": [d.to_json() for d in diagnoses],
        "regions": {
            singer: [[r.low, r.high, list(r.sources)] for r in rs]
            for singer, rs in sorted(regions.items())
        },
    }


# ---------------------------------------------------------------------------
# Performance optimisation
# ---------------------------------------------------------------------------

@dataclass
class Performance:
    feasible: bool
    score_id: str | None
    transpose: int | None
    switches: int | None
    cost: int | None
    score_fee: int | None
    guest_fees: dict[str, int]
    phrases: list[dict]
    overpasses: list[dict]
    reason: str | None = None

    def to_json(self) -> dict:
        return {
            "feasible": self.feasible,
            "score_id": self.score_id,
            "transpose": self.transpose,
            "switches": self.switches,
            "cost": self.cost,
            "score_fee": self.score_fee,
            "guest_fees": dict(self.guest_fees),
            "phrases": list(self.phrases),
            "overpasses": list(self.overpasses),
            "reason": self.reason,
        }


def _phrase_candidates(
    piece: Piece,
    score: Score,
    regions_by_singer: dict[str, list[Region]],
    singers: dict[str, Singer],
    available: list[str],
) -> dict[tuple[str, str], list[tuple[int, int, str, Region]]]:
    """For each (voice, phrase): candidate (low, high, singer, region)."""
    out: dict[tuple[str, str], list[tuple[int, int, str, Region]]] = {}
    for voice in piece.voices:
        for phrase in voice.phrases:
            low, high = phrase.low + score.transpose, phrase.high + score.transpose
            cands: list[tuple[int, int, str, Region]] = []
            for name in available:
                singer = singers[name]
                if not singer.available:
                    continue
                for region in regions_by_singer.get(name, []):
                    if region.contains(low, high):
                        cands.append((low, high, name, region))
                        break
            out[(voice.voice_id, phrase.phrase_id)] = cands
    return out


def _voice_dp(
    voice: Voice,
    candidates: dict[tuple[str, str], list[tuple[int, int, str, Region]]],
    bit_of: dict[str, int],
) -> list[tuple[tuple[str, ...], int, int]]:
    """Return (assignment, switches, guest_mask) options for one voice."""
    states: dict[tuple[str, int, int], tuple[str, ...]] = {}
    first_phrase = voice.phrases[0]
    first_cands = candidates[(voice.voice_id, first_phrase.phrase_id)]
    if not first_cands:
        return []
    for _low, _high, name, _region in first_cands:
        key = (name, 0, bit_of[name])
        states.setdefault(key, (name,))
    for index in range(1, len(voice.phrases)):
        phrase = voice.phrases[index]
        previous = voice.phrases[index - 1]
        cands = candidates[(voice.voice_id, phrase.phrase_id)]
        if not cands:
            return []
        new_states: dict[tuple[str, int, int], tuple[str, ...]] = {}
        for (prev_name, switches, mask), assignment in states.items():
            for _low, _high, name, _region in cands:
                if name != prev_name:
                    if not previous.rest_after:
                        continue  # no hand-over rest: must keep the same singer
                    next_switches = switches + 1
                else:
                    next_switches = switches
                key = (name, next_switches, mask | bit_of[name])
                new_states.setdefault(key, assignment + (name,))
        states = new_states
        if not states:
            return []
    return [
        (assignment, switches, mask)
        for (prev, switches, mask), assignment in states.items()
    ]


def _guest_fees(mask: int, bit_of: dict[str, int], singers: dict[str, Singer]) -> dict[str, int]:
    return {
        name: singers[name].effective_fee
        for name, bit in bit_of.items()
        if mask & bit and singers[name].kind == "guest" and singers[name].effective_fee
    }


def _choose(
    options: list[tuple[dict[str, tuple[str, ...]], int, int]],
    score_fee: int,
    singers: dict[str, Singer],
    bit_of: dict[str, int],
    mode: str,
    cap: int | None,
) -> tuple[int, int, dict[str, tuple[str, ...]], dict[str, int]] | None:
    """Pick the best combined assignment under the active objective.

    Mode ``"switch"`` minimises cost first and switches second; mode
    ``"budget"`` does the reverse.  When two plan types tie, prefer the one
    that avoids a guest when a regular member can do the job (documented
    tie-break, evaluated only after the primary objectives).
    """
    best = None
    for assignment, switches, mask in options:
        guest_fees = _guest_fees(mask, bit_of, singers)
        cost = score_fee + sum(guest_fees.values())
        if mode == "budget":
            if cap is not None and cost > cap:
                continue
            key = (switches, cost, len(guest_fees))
        else:  # "switch"
            if cap is not None and switches > cap:
                continue
            key = (cost, switches, len(guest_fees))
        if best is None or key < best[0]:
            best = (key, switches, cost, assignment, guest_fees)
    return best


def _bit_map(names: list[str]) -> dict[str, int]:
    return {name: 1 << i for i, name in enumerate(sorted(names))}


def solve(
    piece: Piece,
    scores: list[Score],
    singers: dict[str, Singer],
    regions_by_singer: dict[str, list[Region]],
    *,
    band: set[int] | None = None,
    available: list[str] | None = None,
    mode: str = "budget",
    cap: int | None = None,
) -> dict:
    """Find a whole-piece performance.

    ``band`` is the orchestra's set of available absolute overpass pitches
    (``None`` means unrestricted).  ``available`` restricts the attending
    singers.  ``mode`` is ``"budget"`` (cap is total spend, minimise switches)
    or ``"switch"`` (cap is switches, minimise cost).
    """
    if mode not in ("budget", "switch"):
        raise ValueError("mode must be 'budget' or 'switch'")
    if len(piece.voices) > 3 or piece.phrase_count > 8:
        raise ValueError("piece exceeds the documented limits (<=3 voices, <=8 phrases)")
    if len(scores) > 3:
        raise ValueError("at most three scores are supported")

    roster = list(singers)
    if len(roster) > 6:
        raise ValueError("at most six singers are supported")
    chosen_available = list(available) if available is not None else [
        name for name in roster if singers[name].available
    ]
    for name in chosen_available:
        if name not in singers:
            raise ValueError(f"unknown singer {name!r}")

    bit_of = _bit_map(chosen_available)

    attempts: list[dict] = []
    feasible_by_score: dict[str, dict] = {}

    for score in scores:
        actual_overpasses = [
            {"overpass_id": o.overpass_id, "notation": o.pitch, "actual": o.pitch + score.transpose}
            for o in piece.overpasses
        ]
        if band is not None:
            offending = [o for o in actual_overpasses if o["actual"] not in band]
            if offending:
                attempts.append(
                    {
                        "score_id": score.score_id,
                        "transpose": score.transpose,
                        "feasible": False,
                        "reason": "器乐过门实际音高不在可用集合："
                        + ", ".join(f"{o['overpass_id']}={o['actual']}" for o in offending),
                    }
                )
                continue

        candidates = _phrase_candidates(
            piece, score, regions_by_singer, singers, chosen_available
        )
        missing = [
            f"{voice_id}/{phrase_id}"
            for (voice_id, phrase_id), cands in candidates.items()
            if not cands
        ]
        if missing:
            attempts.append(
                {
                    "score_id": score.score_id,
                    "transpose": score.transpose,
                    "feasible": False,
                    "reason": "没有歌手的单一已证区段能覆盖这些实际音域："
                    + ", ".join(missing),
                }
            )
            continue

        # Per-voice options, then combine voices sharing the guest mask.
        voice_options: list[tuple[str, list[tuple[tuple[str, ...], int, int]]]] = []
        dead = False
        for voice in piece.voices:
            options = _voice_dp(voice, candidates, bit_of)
            if not options:
                dead = True
                attempts.append(
                    {
                        "score_id": score.score_id,
                        "transpose": score.transpose,
                        "feasible": False,
                        "reason": f"声部 {voice.voice_id} 无合法换人安排（休止不足）",
                    }
                )
                break
            pruned: dict[tuple[int, int], tuple[str, ...]] = {}
            for assignment, switches, mask in options:
                pruned.setdefault((switches, mask), assignment)
            voice_options.append(
                (voice.voice_id, [(a, s, m) for (s, m), a in pruned.items()])
            )
        if dead:
            continue

        combined: dict[tuple[int, int], dict[str, tuple[str, ...]]] = {(0, 0): {}}
        for voice_id, options in voice_options:
            new_combined: dict[tuple[int, int], dict[str, tuple[str, ...]]] = {}
            for (total_switches, total_mask), assignment in combined.items():
                for voice_assign, switches, mask in options:
                    key = (total_switches + switches, total_mask | mask)
                    if key not in new_combined:
                        new_combined[key] = {**assignment, voice_id: voice_assign}
            combined = new_combined

        options = [
            (assignment, switches, mask)
            for (switches, mask), assignment in combined.items()
        ]
        best = _choose(options, score.fee, singers, bit_of, mode, cap)
        if best is None:
            attempts.append(
                {
                    "score_id": score.score_id,
                    "transpose": score.transpose,
                    "feasible": False,
                    "reason": "该份总谱在给定上限内无可行演法",
                }
            )
            continue
        _key, switches, cost, assignment, guest_fees = best
        attempts.append(
            {
                "score_id": score.score_id,
                "transpose": score.transpose,
                "feasible": True,
                "switches": switches,
                "cost": cost,
            }
        )
        feasible_by_score[score.score_id] = {
            "score": score,
            "candidates": candidates,
            "switches": switches,
            "cost": cost,
            "assignment": assignment,
            "guest_fees": guest_fees,
            "actual_overpasses": actual_overpasses,
        }

    # Pick the globally best score.
    if not feasible_by_score:
        reason = "没有一份总谱能在约束下演完整首"
        return {
            "feasible": False,
            "mode": mode,
            "cap": cap,
            "reason": reason,
            "attempts": attempts,
            "regions": {
                singer: [[r.low, r.high] for r in rs]
                for singer, rs in sorted(regions_by_singer.items())
            },
        }

    if mode == "budget":
        ordering = lambda rec: (rec["switches"], rec["cost"], rec["score"].score_id)
    else:
        ordering = lambda rec: (rec["cost"], rec["switches"], rec["score"].score_id)
    chosen_rec = min(feasible_by_score.values(), key=ordering)
    score = chosen_rec["score"]
    candidates = chosen_rec["candidates"]

    phrase_lines = []
    for voice in piece.voices:
        assignment = chosen_rec["assignment"][voice.voice_id]
        for phrase, name in zip(voice.phrases, assignment):
            cands = candidates[(voice.voice_id, phrase.phrase_id)]
            region = next(
                r for lo, hi, singer_name, r in cands if singer_name == name
            )
            actual_low, actual_high = (
                phrase.low + score.transpose,
                phrase.high + score.transpose,
            )
            phrase_lines.append(
                {
                    "voice_id": voice.voice_id,
                    "phrase_id": phrase.phrase_id,
                    "notation": [phrase.low, phrase.high],
                    "actual": [actual_low, actual_high],
                    "singer": name,
                    "region": [region.low, region.high],
                    "region_sources": list(region.sources),
                    "rest_after": phrase.rest_after,
                }
            )

    return Performance(
        feasible=True,
        score_id=score.score_id,
        transpose=score.transpose,
        switches=chosen_rec["switches"],
        cost=chosen_rec["cost"],
        score_fee=score.fee,
        guest_fees=chosen_rec["guest_fees"],
        phrases=phrase_lines,
        overpasses=chosen_rec["actual_overpasses"],
        reason=None,
    ).to_json() | {"attempts": attempts, "mode": mode, "cap": cap}


# ---------------------------------------------------------------------------
# Convenience: a short human-readable program
# ---------------------------------------------------------------------------

def program_summary(result: dict) -> str:
    if not result.get("feasible"):
        return f"不可行：{result.get('reason')}"
    lines = [
        f"总谱 {result['score_id']}（移调 {result['transpose']:+d}），"
        f"换人 {result['switches']} 次，总费用 {result['cost']}"
        f"（借谱 {result['score_fee']} + 特邀 {result['guest_fees']}）"
    ]
    for p in result["phrases"]:
        lines.append(
            f"  {p['voice_id']}/{p['phrase_id']}: 记谱 {p['notation']} -> "
            f"实唱 {p['actual']}，{p['singer']} "
            f"(已证区段 {p['region']}, 依据 {p['region_sources']})"
        )
    over = ", ".join(
        f"{o['overpass_id']}:{o['notation']}->{o['actual']}" for o in result["overpasses"]
    )
    lines.append(f"  过门实音: {over or '无'}")
    return "\n".join(lines)
