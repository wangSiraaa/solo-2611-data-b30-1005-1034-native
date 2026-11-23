"""可独查的工艺凭单。

凭单回答两个问题，且每个结论都能回放到具体的签读校样：

1. 每个实样的哪几组候选比例对仍然成立、哪几组被排除（附排除它的那条实读）；
2. 任意两件在**同炉**（共用同一未知比例对）与**异炉**（比例各自独立）时，
   烧后的联合外口/内口尺寸各有哪些。

纪律（与题面逐条对应）：

* 只有 ``signed`` 的校样约束比例；未签读的缺测列只是旧表排版占位，记为
  ``ignored_placeholders``，不参与任何判断——哪怕占位里印着像样的区间；
* 校样行绑定**款 + 实样号 + 测向**（行里的件号只是来样出处，不缩小约束
  范围）：同一 ``sample_id``（同一次试烧实样）的横、纵观察对**同一候选比例
  对**整体生效——筛选器一次拿整组 ``(rw, rd)`` 去撞该样的全部观察，结构上
  就不可能拿甲组的横值去拼乙组的纵值；不同实样是不同炉次，可各自印证不同
  的组；
* 一个实样把全部候选组都矛盾掉 => 该款 ``REJECTED``（退校），凭单整体不可用；
* 只有横向回单（该样没有纵向观察）时纵向不约束，多组可同时保留；
* 款级存活组 = 各实样存活组的并集；该款**所有件**未来炉次都从这同一组存活
  集合里取（同炉锁同一组，异炉各自独立取）。没有任何签读时全部候选组可用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .errors import CraftError
from .model import Piece, ProofObservation, Scenario, Style
from .ratio import Interval, fraction_text, ratio_bounds


# ---------------------------------------------------------------------------
# 校样修订（母题修订重发；同号最高 revision 胜出，同 revision 冲突即报错）
# ---------------------------------------------------------------------------

def resolve_proof_revisions(proofs: tuple[ProofObservation, ...]) -> tuple[ProofObservation, ...]:
    winners: dict[str, ProofObservation] = {}
    for proof in proofs:
        old = winners.get(proof.proof_id)
        if old is None or proof.revision > old.revision:
            winners[proof.proof_id] = proof
        elif proof.revision == old.revision and proof != old:
            raise CraftError(
                f"校样 {proof.proof_id!r} 同一 revision {proof.revision} "
                f"出现两份不同内容，来单冲突，整单退校"
            )
    return tuple(sorted(winners.values(), key=lambda p: p.proof_id))


# ---------------------------------------------------------------------------
# 单组候选比例对在一个实样上的逐条核对
# ---------------------------------------------------------------------------

@dataclass
class RatioCheck:
    direction: str
    proof_id: str
    sample_id: str
    green_dim: Fraction
    observed: Interval
    bounds: Interval          # 该实读允许的比例闭区间
    ratio_value: Fraction     # 本组在该测向上的候选比例
    holds: bool
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "proof_id": self.proof_id,
            "sample_id": self.sample_id,
            "green_dim": fraction_text(self.green_dim),
            "observed": self.observed.to_json(),
            "ratio_bounds": self.bounds.to_json(),
            "ratio_value": fraction_text(self.ratio_value),
            "holds": self.holds,
            "reason": self.reason,
        }


@dataclass
class PairVerdict:
    pair_index: int
    rw: Fraction
    rd: Fraction
    status: str               # "KEPT" | "EXCLUDED"
    checks: list[RatioCheck] = field(default_factory=list)

    @property
    def kept(self) -> bool:
        return self.status == "KEPT"

    def to_json(self) -> dict[str, Any]:
        return {
            "pair_index": self.pair_index,
            "ratio_pair": [fraction_text(self.rw), fraction_text(self.rd)],
            "status": self.status,
            "checks": [c.to_json() for c in self.checks],
        }


@dataclass
class SampleVerdict:
    """一个实样（一次试烧炉次）的整组核对结论。"""

    sample_id: str
    proof_ids: list[str]
    directions: list[str]
    pair_verdicts: list[PairVerdict]
    surviving_indices: list[int]

    def to_json(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "proof_ids": list(self.proof_ids),
            "directions": list(self.directions),
            "surviving_indices": list(self.surviving_indices),
            "pair_verdicts": [pv.to_json() for pv in self.pair_verdicts],
        }


@dataclass
class StyleVerdict:
    style_id: str
    samples: list[SampleVerdict]
    # 款级存活组 = 各实样存活组的并集；无任何签读时为全部候选组。该款每件
    # 未来炉次都从这同一集合里取比例组。
    surviving_indices: list[int]
    # 每条校样出自哪件（来样出处，仅用于可独查溯源）。
    proof_source_piece: dict[str, str]
    ignored_placeholders: list[dict[str, Any]]
    status: str                    # "ACCEPTED" | "REJECTED"

    def to_json(self) -> dict[str, Any]:
        return {
            "style_id": self.style_id,
            "status": self.status,
            "surviving_indices": list(self.surviving_indices),
            "proof_source_piece": dict(sorted(self.proof_source_piece.items())),
            "samples": [s.to_json() for s in self.samples],
            "ignored_placeholders": list(self.ignored_placeholders),
        }


@dataclass
class CraftVoucher:
    scenario_id: str
    style_verdicts: dict[str, StyleVerdict]
    accepted: bool
    rejection_reasons: list[str] = field(default_factory=list)

    def surviving_pairs(self, style_id: str) -> list[int]:
        return list(self.style_verdicts[style_id].surviving_indices)

    def allowed_pairs_for_piece(self, piece_id: str, style_id: str) -> list[int]:
        # 校样约束的是款；同一款的件共享同一组存活比例集合。
        return self.surviving_pairs(style_id)

    def raise_if_rejected(self) -> None:
        if not self.accepted:
            raise CraftError("工艺凭单退校: " + "; ".join(self.rejection_reasons))

    def to_json(self) -> dict[str, Any]:
        return {
            "format": "craft-voucher/v1",
            "scenario_id": self.scenario_id,
            "accepted": self.accepted,
            "rejection_reasons": list(self.rejection_reasons),
            "styles": [
                self.style_verdicts[sid].to_json()
                for sid in sorted(self.style_verdicts)
            ],
        }


def _check_pair_on_sample(
    pair_index: int,
    rw: Fraction,
    rd: Fraction,
    sample_proofs: list[ProofObservation],
) -> PairVerdict:
    """整组 (rw, rd) 同时撞该实样的横、纵观察；任一不成立即排除整组。"""
    checks: list[RatioCheck] = []
    excluded = False
    for proof in sample_proofs:
        ratio_value = rw if proof.direction == "w" else rd
        observed = Interval(proof.observed_lo, proof.observed_hi)
        bounds = ratio_bounds(proof.green_dim, observed)
        holds = bounds.contains(ratio_value)
        if holds:
            reason = (
                f"{fraction_text(ratio_value)} 落在签读闭区间 "
                f"{fraction_text(bounds.lo)}..{fraction_text(bounds.hi)} 内"
            )
        else:
            excluded = True
            reason = (
                f"坯尺寸 {fraction_text(proof.green_dim)} 乘比例 "
                f"{fraction_text(ratio_value)} = "
                f"{fraction_text(proof.green_dim * ratio_value)}，"
                f"不在签读烧后区间 {fraction_text(observed.lo)}.."
                f"{fraction_text(observed.hi)} 内"
            )
        checks.append(
            RatioCheck(
                direction=proof.direction,
                proof_id=proof.proof_id,
                sample_id=proof.sample_id,
                green_dim=proof.green_dim,
                observed=observed,
                bounds=bounds,
                ratio_value=ratio_value,
                holds=holds,
                reason=reason,
            )
        )
    return PairVerdict(
        pair_index=pair_index,
        rw=rw,
        rd=rd,
        status="EXCLUDED" if excluded else "KEPT",
        checks=checks,
    )


def build_voucher(scenario: Scenario) -> CraftVoucher:
    """按情景内全部签读校样，逐实样筛选各款候选比例对。"""
    verdicts: dict[str, StyleVerdict] = {}
    rejection_reasons: list[str] = []

    for style in scenario.styles:
        all_proofs = resolve_proof_revisions(
            tuple(p for piece in style.pieces for p in piece.proofs)
        )
        signed = [p for p in all_proofs if p.is_signed]
        placeholders = [
            {
                "proof_id": p.proof_id,
                "sample_id": p.sample_id,
                "direction": p.direction,
                "note": "未签读/缺测，旧表排版占位，不约束比例",
            }
            for p in all_proofs
            if not p.is_signed
        ]

        # 按实样号归组：同一样的横纵观察必须共同作用于同一整组比例对。
        by_sample: dict[str, list[ProofObservation]] = {}
        proof_source_piece: dict[str, str] = {}
        for piece in style.pieces:
            for proof in piece.proofs:
                proof_source_piece[proof.proof_id] = piece.piece_id
        for proof in signed:
            by_sample.setdefault(proof.sample_id, []).append(proof)

        sample_verdicts: list[SampleVerdict] = []
        style_union: set[int] = set()
        for sample_id in sorted(by_sample):
            sample_proofs = sorted(by_sample[sample_id], key=lambda p: (p.direction, p.proof_id))
            pair_verdicts = [
                _check_pair_on_sample(index, rw, rd, sample_proofs)
                for index, (rw, rd) in enumerate(style.candidate_ratios)
            ]
            surviving = [pv.pair_index for pv in pair_verdicts if pv.kept]
            if not surviving:
                rejection_reasons.append(
                    f"款 {style.style_id!r} 实样 {sample_id!r}: 全部 "
                    f"{len(pair_verdicts)} 组候选比例都与该样签读矛盾，退校"
                )
            style_union.update(surviving)
            sample_verdicts.append(
                SampleVerdict(
                    sample_id=sample_id,
                    proof_ids=[p.proof_id for p in sample_proofs],
                    directions=[p.direction for p in sample_proofs],
                    pair_verdicts=pair_verdicts,
                    surviving_indices=surviving,
                )
            )

        all_indices = list(range(len(style.candidate_ratios)))
        # 校样约束的是款：款级存活组对该款所有件生效；无任何签读时全部可用。
        surviving_overall = sorted(style_union) if signed else list(all_indices)
        status = "ACCEPTED" if all(s.surviving_indices for s in sample_verdicts) else "REJECTED"
        verdicts[style.style_id] = StyleVerdict(
            style_id=style.style_id,
            samples=sample_verdicts,
            surviving_indices=surviving_overall,
            proof_source_piece=proof_source_piece,
            ignored_placeholders=placeholders,
            status=status,
        )

    return CraftVoucher(
        scenario_id=scenario.scenario_id,
        style_verdicts=verdicts,
        accepted=not rejection_reasons,
        rejection_reasons=rejection_reasons,
    )


# ---------------------------------------------------------------------------
# 烧后尺寸（固定比例对下是精确点值）与任意两件的联合尺寸
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FiredDims:
    """一件在某组比例对下的烧后矩形尺寸（精确点值）。"""

    outer_w: Fraction
    outer_d: Fraction
    inner_w: Fraction
    inner_d: Fraction

    def to_json(self) -> dict[str, Any]:
        return {
            "outer_w": fraction_text(self.outer_w),
            "outer_d": fraction_text(self.outer_d),
            "inner_w": fraction_text(self.inner_w),
            "inner_d": fraction_text(self.inner_d),
        }


def fired_dims(piece: Piece, style: Style, pair_index: int) -> FiredDims:
    rw, rd = style.candidate_ratios[pair_index]
    return FiredDims(
        outer_w=piece.green["outer_w"] * rw,
        outer_d=piece.green["outer_d"] * rd,
        inner_w=piece.green["inner_w"] * rw,
        inner_d=piece.green["inner_d"] * rd,
    )


def joint_dimensions(scenario: Scenario, voucher: CraftVoucher,
                     id_a: str, id_b: str) -> dict[str, Any]:
    """任意两件的同炉/异炉联合烧后尺寸。

    * 同炉（仅同款才有共架意义）：两件锁同一 ``pair_index``，取两件允许组的
      交集逐组列出；交集为空说明没有能同时解释两件的同炉比例，炉况列表为空；
    * 异炉：两件各自允许组做笛卡尔积，每组联合都是一个真实可能的炉况。
    """
    pieces = scenario.piece_by_id()
    styles = scenario.style_by_id()
    a, b = pieces[id_a], pieces[id_b]
    sa, sb = styles[a.style_id], styles[b.style_id]
    idx_a = voucher.allowed_pairs_for_piece(id_a, a.style_id)
    idx_b = voucher.allowed_pairs_for_piece(id_b, b.style_id)

    same_kiln: list[dict[str, Any]] = []
    if a.style_id == b.style_id:
        common = sorted(set(idx_a) & set(idx_b))
        for pair_index in common:  # 同炉 => 同一未知比例对
            same_kiln.append({
                "pair_index": pair_index,
                "ratio_pair": [
                    fraction_text(sa.candidate_ratios[pair_index][0]),
                    fraction_text(sa.candidate_ratios[pair_index][1]),
                ],
                id_a: fired_dims(a, sa, pair_index).to_json(),
                id_b: fired_dims(b, sb, pair_index).to_json(),
            })

    separate_kiln: list[dict[str, Any]] = []
    for ia in idx_a:
        for ib in idx_b:
            separate_kiln.append({
                "pair_index_a": ia,
                "pair_index_b": ib,
                id_a: fired_dims(a, sa, ia).to_json(),
                id_b: fired_dims(b, sb, ib).to_json(),
            })
    return {
        "piece_a": id_a,
        "piece_b": id_b,
        "same_kiln": same_kiln,
        "separate_kiln": separate_kiln,
    }
