"""试烧来单接入口：把外部校样来单经旧 ``extcontract`` 摄入后接到工艺台。

本模块不改写任何既有合同逻辑，复用：

* :class:`extcontract.IngestionLibrary` —— 固定读者合同、原子接收/拒绝、
  同号修订（最高 revision 胜出，同 revision 冲突整单退）、重放指纹；
* ``reader-records/v1`` —— 全部数值以最简分数字符串落地，可由外部固定
  读者独立核读。

工艺台只承认**签读**（``signed=1``）的行；未签读行里的 ``observed_*`` 即便
带值也只是旧表排版占位，凭单层会显式忽略，绝不拿它约束比例。
来单行的业务主键是 ``(piece_id, sample_id, direction)``，同一三元组的更高
revision 即母题的更正行。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from extcontract import IngestionLibrary, load_contract

from .errors import CraftError
from .model import Piece, ProofObservation, Scenario, Style


def proof_row_id(piece_id: str, sample_id: str, direction: str) -> str:
    """来单行到工艺台校样号的稳定映射（同三元组即同一母题）。"""
    return f"{piece_id}|{sample_id}|{direction}"


@dataclass
class IntakeResult:
    accepted: bool
    ingestion: Any          # extcontract.IngestResult
    library: IngestionLibrary

    @property
    def errors(self) -> list[str]:
        return list(self.ingestion.errors)

    @property
    def receipt(self) -> dict[str, Any] | None:
        return self.ingestion.receipt


class TrialIntake:
    """固定试烧读者合同的有状态来单库。"""

    def __init__(
        self,
        writer_path: str = "materials/craftbench/trial-writer-contract.json",
        reader_path: str = "materials/craftbench/trial-reader-contract.json",
    ):
        self.writer = load_contract(writer_path, role="writer")
        self.reader = load_contract(reader_path, role="reader")
        self.library = IngestionLibrary(self.reader)

    # -- 写端旧接入口 ------------------------------------------------------

    def ingest_file(self, document: dict[str, Any]) -> IntakeResult:
        result = self.library.ingest(self.writer, document)
        return IntakeResult(accepted=result.accepted, ingestion=result,
                            library=self.library)

    # -- 接到工艺台 --------------------------------------------------------

    def published_proofs(self) -> list[ProofObservation]:
        """把当前发布切片里的行还原成工艺台校样（只看最高修订后的状态）。"""
        snapshot = self.library.publish()
        proofs: list[ProofObservation] = []
        for row in snapshot["rows"]:
            proofs.append(_row_to_proof(row))
        return proofs

    def proofs_for_piece(self, piece_id: str) -> tuple[ProofObservation, ...]:
        return tuple(p for p in self.published_proofs() if _belongs(p, piece_id))

    def apply_to_scenario(self, scenario: Scenario) -> Scenario:
        """把当前发布切片的校样盖写到情景各件上（其余结构原样保留）。

        来单里没有出现的件保留其原有校样（通常为空）；摄入切片中某件的全部
        修订后校样整组替换情景里的同名校样，保证工艺台只依据实际签读下单。
        """
        by_piece: dict[str, tuple[ProofObservation, ...]] = {}
        for proof in self.published_proofs():
            piece_id = proof.proof_id.split("|", 1)[0]
            by_piece.setdefault(piece_id, ())
            by_piece[piece_id] += (proof,)

        new_styles: list[Style] = []
        all_pieces: list[Piece] = []
        for style in scenario.styles:
            new_pieces: list[Piece] = []
            for piece in style.pieces:
                if piece.piece_id in by_piece:
                    piece = Piece(
                        piece_id=piece.piece_id,
                        style_id=piece.style_id,
                        green=piece.green,
                        proofs=tuple(sorted(by_piece[piece.piece_id],
                                            key=lambda p: p.proof_id)),
                        allow_rotation=piece.allow_rotation,
                    )
                new_pieces.append(piece)
                all_pieces.append(piece)
            new_styles.append(
                Style(style.style_id, style.candidate_ratios,
                      style.allow_rotation, tuple(new_pieces))
            )
        return Scenario(scenario.scenario_id, tuple(new_styles), scenario.racks,
                        scenario.objective, tuple(all_pieces))


def _belongs(proof: ProofObservation, piece_id: str) -> bool:
    # proof_id 形如 "<piece_id>|<sample_id>|<direction>"
    return proof.proof_id.split("|", 1)[0] == piece_id


def _row_to_proof(row: dict[str, Any]) -> ProofObservation:
    values = row["values"]
    revision = row["revision"]
    piece_id = values["piece_id"]
    sample_id = values["sample_id"]
    direction = values["direction"]
    if direction not in ("w", "d"):
        raise CraftError(f"来单 {row['row_id']!r}: direction 只能是 w/d")
    signed_raw = values["signed"]
    if signed_raw not in ("0", "1"):
        raise CraftError(f"来单 {row['row_id']!r}: signed 只能是 0/1，收到 {signed_raw!r}")
    signed = signed_raw == "1"
    green = Fraction(values["green_dim"])
    if green <= 0:
        raise CraftError(f"来单 {row['row_id']!r}: green_dim 必须为正")

    lo_raw, hi_raw = values["observed_lo"], values["observed_hi"]
    if signed:
        if lo_raw is None or hi_raw is None:
            raise CraftError(
                f"来单 {row['row_id']!r}: 签读行必须给出 observed_lo/observed_hi"
            )
        lo, hi = Fraction(lo_raw), Fraction(hi_raw)
        if lo < 0 or hi < 0 or lo > hi:
            raise CraftError(f"来单 {row['row_id']!r}: 烧后闭区间非法 [{lo}, {hi}]")
    else:
        # 未签读：占位行不约束比例；端点记 0，凭单显式忽略。
        lo = hi = Fraction(0)

    return ProofObservation(
        proof_id=proof_row_id(piece_id, sample_id, direction),
        sample_id=sample_id,
        direction=direction,
        green_dim=green,
        observed_lo=lo,
        observed_hi=hi,
        signed=signed,
        revision=revision,
    )


def make_trial_document(
    file_id: str,
    observations: list[dict[str, Any]],
    *,
    writer_version: str = "tw1",
    reader_version: str = "tr1",
) -> dict[str, Any]:
    """把人读的观察字典批量转成旧接入口的来单文件信封。

    每条观察::

        {"piece_id", "sample_id", "direction", "green_dim",
         "observed": [lo, hi] | None, "signed": bool, "revision": int}
    """
    rows = []
    for obs in observations:
        direction = obs["direction"]
        piece_id = obs["piece_id"]
        sample_id = obs["sample_id"]
        values: dict[str, Any] = {
            "style_id": obs["style_id"],
            "piece_id": piece_id,
            "sample_id": sample_id,
            "direction": direction,
            "green_dim": _num(obs["green_dim"]),
            "signed": 1 if obs["signed"] else 0,
        }
        if obs.get("observed") is None:
            values["observed_lo"] = None
            values["observed_hi"] = None
        else:
            lo, hi = obs["observed"]
            values["observed_lo"] = _num(lo)
            values["observed_hi"] = _num(hi)
        rows.append({
            "row_id": proof_row_id(piece_id, sample_id, direction),
            "revision": obs.get("revision", 1),
            "values": values,
        })
    return {
        "file_id": file_id,
        "writer_version": writer_version,
        "reader_version": reader_version,
        "rows": rows,
    }


def _num(value: Any) -> str:
    """整数/分数字符串原样走精确合同；显式拒收浮点。"""
    if isinstance(value, bool):
        raise CraftError("布尔不能作为测量值")
    if isinstance(value, float):
        raise CraftError(
            f"测量值 {value!r} 是浮点；试烧合同只收精确整数或 n/d 分数字符串"
        )
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # 让 Fraction 先验一遍合法性，再原样回传（DECIMAL 列收分数字符串）。
        Fraction(value)
        return value
    raise CraftError(f"不支持的测量表示 {type(value).__name__}")
