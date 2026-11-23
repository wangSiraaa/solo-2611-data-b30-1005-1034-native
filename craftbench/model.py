"""情景模型：款、件、校样行、可租承架。

输入是一份 JSON 情景（合成尺寸规则，不对应真实窑炉）：

.. code-block:: json

    {
      "scenario_id": "two-sleeve-base",
      "styles": [
        {
          "style_id": "S",
          "candidate_ratios": [["1", "1"], ["4/5", "4/5"]],
          "allow_rotation": false,
          "pieces": [
            {"piece_id": "small",
             "green": {"outer_w": "5", "outer_d": "5",
                       "inner_w": "1", "inner_d": "1"},
             "proofs": [
               {"proof_id": "p1", "sample_id": "sm1", "direction": "w",
                "green_dim": "5", "observed": ["4", "5"], "signed": true,
                "revision": 1}
             ]}
          ]
        }
      ],
      "racks": [
        {"rack_id": "R1", "style_id": "S", "seats": 2,
         "allowed_pieces": ["small", "large"], "fee": 2}
      ],
      "objective": {"mode": "min_boxes"},
      "box_cap": null
    }

校样行的关键纪律：

* 只有 ``signed: true``（写端实际给出并签读）的测量才约束比例；
  ``signed: false`` 或缺测列只是给旧表排版的占位，一律忽略；
* 同一实样（``sample_id``）的横、纵观察必须共同作用于**同一组候选比例对**，
  不允许把 A 组的横值与 B 组的纵值拼成新材料；
* 一个款的全部校样都矛盾时整款退校（情景拒绝），仅横向回单时可保留多组。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import CraftError
from .ratio import as_fraction, require_positive

# 题面硬上限：最多六件、三张付费承架、每款至多三组比例。
MAX_PIECES = 6
MAX_RACKS = 3
MAX_RATIOS_PER_STYLE = 3
DIRECTIONS = ("w", "d")
# 坯件的四个矩形尺寸：外宽/外深、内宽/内深。
GREEN_KEYS = ("outer_w", "outer_d", "inner_w", "inner_d")


@dataclass(frozen=True)
class ProofObservation:
    """一条签读校样：绑定 款 + 实样号 + 测向。"""

    proof_id: str
    sample_id: str
    direction: str  # "w"（横向）或 "d"（纵向）
    green_dim: Fraction
    observed_lo: Fraction
    observed_hi: Fraction
    signed: bool
    revision: int

    @property
    def is_signed(self) -> bool:
        return bool(self.signed)


@dataclass(frozen=True)
class Piece:
    piece_id: str
    style_id: str
    green: dict[str, Fraction]
    proofs: tuple[ProofObservation, ...]
    allow_rotation: bool

    def green_dim(self, direction: str) -> Fraction:
        return self.green[f"outer_{direction}"]

    def inner_green(self, direction: str) -> Fraction:
        return self.green[f"inner_{direction}"]


@dataclass(frozen=True)
class Style:
    style_id: str
    # 每组是 (横向比例 rw, 纵向比例 rd)，正有理数对。
    candidate_ratios: tuple[tuple[Fraction, Fraction], ...]
    allow_rotation: bool
    pieces: tuple[Piece, ...]


@dataclass(frozen=True)
class Rack:
    """一张可租同炉承架：限同一款、限指定件、限席数；空席允许。"""

    rack_id: str
    style_id: str
    seats: int
    allowed_pieces: frozenset[str]
    fee: int

    def admits(self, piece: Piece) -> bool:
        return piece.style_id == self.style_id and piece.piece_id in self.allowed_pieces


@dataclass(frozen=True)
class Objective:
    # "min_boxes"：给可用架费，最少箱数（再省架费）；
    # "min_fee"：给车辆箱位上限 box_cap，求最低须租架费。
    mode: str
    box_cap: int | None = None
    fee_budget: int | None = None


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    styles: tuple[Style, ...]
    racks: tuple[Rack, ...]
    objective: Objective
    pieces: tuple[Piece, ...] = field(default_factory=tuple)

    def piece_by_id(self) -> dict[str, Piece]:
        return {p.piece_id: p for p in self.pieces}

    def style_by_id(self) -> dict[str, Style]:
        return {s.style_id: s for s in self.styles}


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _expect_keys(raw: dict, allowed: set[str], where: str) -> None:
    extra = set(raw) - allowed
    if extra:
        raise CraftError(f"{where}: 未知字段 {sorted(extra)}")
    missing = allowed - set(raw)
    if missing:
        raise CraftError(f"{where}: 缺字段 {sorted(missing)}")


def _parse_proof(raw: Any, *, style_id: str, piece_id: str) -> ProofObservation:
    where = f"款 {style_id} 件 {piece_id} 的校样"
    if not isinstance(raw, dict):
        raise CraftError(f"{where}: 必须是对象")
    allowed = {"proof_id", "sample_id", "direction", "green_dim",
               "observed", "signed", "revision"}
    _expect_keys(raw, allowed, where)
    proof_id = raw["proof_id"]
    sample_id = raw["sample_id"]
    if not isinstance(proof_id, str) or not proof_id:
        raise CraftError(f"{where}: proof_id 必须是非空字符串")
    if not isinstance(sample_id, str) or not sample_id:
        raise CraftError(f"{where}: sample_id 必须是非空字符串")
    direction = raw["direction"]
    if direction not in DIRECTIONS:
        raise CraftError(f"{where}: direction 只能是 {DIRECTIONS}，收到 {direction!r}")
    green_dim = require_positive(
        as_fraction(raw["green_dim"], where=f"{where}.green_dim"),
        where=f"{where}.green_dim",
    )
    observed = raw["observed"]
    signed = raw["signed"]
    if not isinstance(signed, bool):
        raise CraftError(f"{where}: signed 必须是布尔")
    revision = raw.get("revision", 1)
    if type(revision) is not int or revision < 1:
        raise CraftError(f"{where}: revision 必须是正整数")
    if not signed:
        # 缺测/未签读：旧表占位，不产生任何比例约束。仍校验 observed 形状，
        # 让损坏的占位行显式报错而不是静默吞掉。
        lo = hi = None
        if observed is not None:
            lo, hi = _parse_observed(observed, where)
        return ProofObservation(proof_id, sample_id, direction, green_dim,
                                lo if lo is not None else Fraction(0),
                                hi if hi is not None else Fraction(0),
                                False, revision)
    lo, hi = _parse_observed(observed, where)
    return ProofObservation(proof_id, sample_id, direction, green_dim, lo, hi, True, revision)


def _parse_observed(observed: Any, where: str) -> tuple[Fraction, Fraction]:
    if not isinstance(observed, list) or len(observed) != 2:
        raise CraftError(f"{where}: observed 必须是 [lo, hi] 二元数组")
    lo = as_fraction(observed[0], where=f"{where}.observed.lo")
    hi = as_fraction(observed[1], where=f"{where}.observed.hi")
    if lo < 0 or hi < 0 or lo > hi:
        raise CraftError(f"{where}: 烧后闭区间非法 [{lo}, {hi}]")
    return lo, hi


def _parse_green(raw: Any, *, where: str) -> dict[str, Fraction]:
    if not isinstance(raw, dict):
        raise CraftError(f"{where}: green 必须是对象")
    _expect_keys(raw, set(GREEN_KEYS), where)
    out: dict[str, Fraction] = {}
    for key in GREEN_KEYS:
        out[key] = require_positive(
            as_fraction(raw[key], where=f"{where}.{key}"), where=f"{where}.{key}"
        )
    return out


def _parse_piece(raw: Any, *, style_id: str, allow_rotation: bool) -> Piece:
    where = f"款 {style_id} 的件"
    if not isinstance(raw, dict):
        raise CraftError(f"{where}: 必须是对象")
    _expect_keys(raw, {"piece_id", "green", "proofs"}, where)
    piece_id = raw["piece_id"]
    if not isinstance(piece_id, str) or not piece_id:
        raise CraftError(f"{where}: piece_id 必须是非空字符串")
    green = _parse_green(raw["green"], where=f"{where} {piece_id}.green")
    proofs_raw = raw["proofs"]
    if not isinstance(proofs_raw, list):
        raise CraftError(f"{where} {piece_id}: proofs 必须是数组")
    proofs = tuple(
        _parse_proof(item, style_id=style_id, piece_id=piece_id) for item in proofs_raw
    )
    return Piece(piece_id, style_id, green, proofs, allow_rotation)


def _parse_style(raw: Any) -> Style:
    where = "款"
    if not isinstance(raw, dict):
        raise CraftError(f"{where}: 必须是对象")
    _expect_keys(raw, {"style_id", "candidate_ratios", "allow_rotation", "pieces"}, where)
    style_id = raw["style_id"]
    if not isinstance(style_id, str) or not style_id:
        raise CraftError(f"{where}: style_id 必须是非空字符串")
    allow_rotation = raw["allow_rotation"]
    if not isinstance(allow_rotation, bool):
        raise CraftError(f"款 {style_id}: allow_rotation 必须是布尔")
    ratios_raw = raw["candidate_ratios"]
    if not isinstance(ratios_raw, list) or not 1 <= len(ratios_raw) <= MAX_RATIOS_PER_STYLE:
        raise CraftError(
            f"款 {style_id}: 候选比例组数必须在 1..{MAX_RATIOS_PER_STYLE} 之间"
        )
    ratios: list[tuple[Fraction, Fraction]] = []
    seen_pairs: set[tuple[Fraction, Fraction]] = set()
    for index, pair in enumerate(ratios_raw):
        if not isinstance(pair, list) or len(pair) != 2:
            raise CraftError(f"款 {style_id}: 候选比例 #{index} 必须是 [rw, rd]")
        rw = require_positive(
            as_fraction(pair[0], where=f"款 {style_id} 比例 #{index}.rw"),
            where=f"款 {style_id} 比例 #{index}.rw",
        )
        rd = require_positive(
            as_fraction(pair[1], where=f"款 {style_id} 比例 #{index}.rd"),
            where=f"款 {style_id} 比例 #{index}.rd",
        )
        if (rw, rd) in seen_pairs:
            raise CraftError(f"款 {style_id}: 候选比例 {rw, rd} 重复")
        seen_pairs.add((rw, rd))
        ratios.append((rw, rd))
    pieces_raw = raw["pieces"]
    if not isinstance(pieces_raw, list) or not pieces_raw:
        raise CraftError(f"款 {style_id}: 至少要有一件")
    pieces = tuple(_parse_piece(item, style_id=style_id, allow_rotation=allow_rotation)
                   for item in pieces_raw)
    return Style(style_id, tuple(ratios), allow_rotation, pieces)


def _parse_rack(raw: Any, known_styles: set[str], known_pieces: dict[str, Piece]) -> Rack:
    where = "承架"
    if not isinstance(raw, dict):
        raise CraftError(f"{where}: 必须是对象")
    _expect_keys(raw, {"rack_id", "style_id", "seats", "allowed_pieces", "fee"}, where)
    rack_id = raw["rack_id"]
    style_id = raw["style_id"]
    if not isinstance(rack_id, str) or not rack_id:
        raise CraftError(f"{where}: rack_id 必须是非空字符串")
    if style_id not in known_styles:
        raise CraftError(f"承架 {rack_id}: 引用了未知款 {style_id!r}")
    seats = raw["seats"]
    fee = raw["fee"]
    if type(seats) is not int or seats < 1:
        raise CraftError(f"承架 {rack_id}: seats 必须是正整数")
    if type(fee) is not int or fee < 0:
        raise CraftError(f"承架 {rack_id}: fee 必须是非负整数")
    allowed = raw["allowed_pieces"]
    if not isinstance(allowed, list) or not allowed:
        raise CraftError(f"承架 {rack_id}: allowed_pieces 必须是非空数组")
    allowed_set: set[str] = set()
    for pid in allowed:
        if not isinstance(pid, str):
            raise CraftError(f"承架 {rack_id}: allowed_pieces 只能含件号字符串")
        piece = known_pieces.get(pid)
        if piece is None:
            raise CraftError(f"承架 {rack_id}: 引用了未知件 {pid!r}")
        if piece.style_id != style_id:
            raise CraftError(f"承架 {rack_id}: 件 {pid!r} 不属同款 {style_id!r}")
        allowed_set.add(pid)
    if len(allowed_set) > seats:
        raise CraftError(f"承架 {rack_id}: 指定件数超过席数 {seats}")
    return Rack(rack_id, style_id, seats, frozenset(allowed_set), fee)


def _parse_objective(raw: Any) -> Objective:
    where = "objective"
    if not isinstance(raw, dict):
        raise CraftError(f"{where}: 必须是对象")
    mode = raw.get("mode", "min_boxes")
    if mode not in ("min_boxes", "min_fee"):
        raise CraftError(f"{where}.mode 只能是 min_boxes/min_fee，收到 {mode!r}")
    box_cap = raw.get("box_cap")
    fee_budget = raw.get("fee_budget")
    if box_cap is not None and (type(box_cap) is not int or box_cap < 1):
        raise CraftError(f"{where}.box_cap 必须是正整数或 null")
    if fee_budget is not None and (type(fee_budget) is not int or fee_budget < 0):
        raise CraftError(f"{where}.fee_budget 必须是非负整数或 null")
    unknown = set(raw) - {"mode", "box_cap", "fee_budget"}
    if unknown:
        raise CraftError(f"{where}: 未知字段 {sorted(unknown)}")
    if mode == "min_fee" and box_cap is None:
        raise CraftError("min_fee 模式必须给车辆箱位上限 box_cap")
    return Objective(mode=mode, box_cap=box_cap, fee_budget=fee_budget)


def parse_scenario(raw: Any) -> Scenario:
    if not isinstance(raw, dict):
        raise CraftError("情景文件必须是 JSON 对象")
    _expect_keys(raw, {"scenario_id", "styles", "racks", "objective"}, "情景")
    scenario_id = raw["scenario_id"]
    if not isinstance(scenario_id, str) or not scenario_id:
        raise CraftError("scenario_id 必须是非空字符串")
    styles_raw = raw["styles"]
    if not isinstance(styles_raw, list) or not styles_raw:
        raise CraftError("styles 必须是非空数组")
    styles = tuple(_parse_style(item) for item in styles_raw)
    style_ids = [s.style_id for s in styles]
    if len(set(style_ids)) != len(style_ids):
        raise CraftError("款号重复")

    all_pieces: list[Piece] = [p for s in styles for p in s.pieces]
    if len(all_pieces) > MAX_PIECES:
        raise CraftError(f"件数超过硬上限 {MAX_PIECES}")
    piece_ids = [p.piece_id for p in all_pieces]
    if len(set(piece_ids)) != len(piece_ids):
        raise CraftError("件号重复")
    piece_map = {p.piece_id: p for p in all_pieces}

    racks_raw = raw["racks"]
    if not isinstance(racks_raw, list):
        raise CraftError("racks 必须是数组")
    if len(racks_raw) > MAX_RACKS:
        raise CraftError(f"付费承架超过硬上限 {MAX_RACKS}")
    racks = tuple(_parse_rack(item, set(style_ids), piece_map) for item in racks_raw)
    if len({r.rack_id for r in racks}) != len(racks):
        raise CraftError("承架号重复")

    objective = _parse_objective(raw["objective"])
    return Scenario(scenario_id, styles, racks, objective, tuple(all_pieces))


def load_scenario(path: str | Path) -> Scenario:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CraftError(f"情景文件不存在: {p}") from None
    except json.JSONDecodeError as exc:
        raise CraftError(f"情景文件 {p} 不是合法 JSON: {exc}") from None
    return parse_scenario(raw)
