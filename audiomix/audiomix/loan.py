"""磁带馆借展剪接台：实物盘登记、借展段资格核验、批量优化与签单入账。

构建在旧库（timeline 发布产品 + archive 真实混音数据仓 + assets 素材登记仓）之上：

- **实物盘**：馆方用实际 16kHz 单声道素材登记具名实物盘（入藏号），绑定原 PCM
  摘要与样本位置；每盘是一件实物。同一录音的另一副本（字节相同、asset_id 相同）
  必须另有入藏号。最多四盘。
- **借展段**：指向已发布节目中的具名 clip 及输出半开区间。原盘取样必须在整段
  每拍由该次引用独自、按原序、一步一源样本、实际增益 1、无淡化改变地贡献，
  同时核实际归档字节；节目里另一轨碰巧相消为零、或同一素材的别次出现，都不能
  顶替它；零增益的另一轨不算参与制声。
- **剪接师**：独立列出实际可作原件的盘、连续原样本区间与可落刀的两端，或说明
  哪一拍只能从母带翻录。
- **库房**：每盘登记可切边界与以前借走的半开区段；交给一个展方的原段不得含已
  割走部分；已签借的原段与刀口随实物入账，重开原单仍能回读。
- **批量优化**：每项请求有展览价值与翻录一整段的实际录音拍数，复制设备有整批
  额度。优先总价值最大，再少用翻录拍数，再少真落刀：已有断口或盘首尾不花刀，
  同一批相邻两段的同一新切口只算一次。最多十六个请求。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, asdict
from fractions import Fraction
from typing import Any

import numpy as np

from .assets import AssetRegistry, digest_of, write_wav
from .engine import (
    OUTPUT_RATE, output_sample_count, render_reference, validate_product,
    product_clip_view,
)
from .rationals import as_r

MAX_REQUESTS = 16   # 每单最多十六个请求
MAX_REELS = 4       # 库房最多四盘


class VaultError(ValueError):
    """库房/签单类整笔拒绝错误。"""


# ============================== 实物盘与请求 ============================== #

@dataclass(frozen=True)
class Reel:
    """具名实物盘：入藏号唯一，绑定原 PCM 摘要与样本数（样本位置 0..N）。"""
    accession: str       # 入藏号（一件实物一个号）
    asset_id: str        # 所录素材（同一录音的副本 asset_id 相同）
    digest: str          # 原 PCM 摘要
    sample_count: int    # 样本位置数
    note: str = ""

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LoanRequest:
    """借展请求：指向已发布节目中的具名 clip 及输出半开区间 [out_lo, out_hi)。

    accept_rerecord 为真时，展方也接受按该段真实 render 从头到尾翻录的新拷贝；
    翻录一整段的实际录音拍数 == 区间拍数（16kHz 实时从头录到尾）。
    """
    request_id: str
    exhibitor: str
    clip_id: str
    out_lo: int
    out_hi: int
    value: int
    accept_rerecord: bool

    @property
    def beats(self) -> int:
        return self.out_hi - self.out_lo

    @property
    def rerecord_beats(self) -> int:
        return self.beats

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "LoanRequest":
        req = LoanRequest(
            request_id=str(obj["request_id"]),
            exhibitor=str(obj.get("exhibitor", obj["request_id"])),
            clip_id=str(obj["clip_id"]),
            out_lo=int(obj["out_lo"]),
            out_hi=int(obj["out_hi"]),
            value=int(obj["value"]),
            accept_rerecord=bool(obj.get("accept_rerecord", False)),
        )
        if not (0 <= req.out_lo < req.out_hi):
            raise VaultError(f"请求 {req.request_id} 输出区间非法: "
                             f"[{req.out_lo},{req.out_hi})")
        if req.value < 0:
            raise VaultError(f"请求 {req.request_id} 价值不能为负")
        if "rerecord_beats" in obj and int(obj["rerecord_beats"]) != req.beats:
            raise VaultError(
                f"请求 {req.request_id} 翻录拍数 {obj['rerecord_beats']} 与整段 "
                f"{req.beats} 拍不符（翻录必须从头到尾录满整段）")
        return req

    def to_json(self) -> dict:
        return {
            "request_id": self.request_id, "exhibitor": self.exhibitor,
            "clip_id": self.clip_id, "out_lo": self.out_lo, "out_hi": self.out_hi,
            "value": self.value, "accept_rerecord": self.accept_rerecord,
            "rerecord_beats": self.rerecord_beats,
        }


# ============================== 精确逐拍核验 ============================== #

def _fade_factor_exact(c: dict[str, Any], t: Fraction) -> Fraction:
    """与引擎同语义的淡化因子，但全程精确有理数（0 时长 -> 恒 1）。"""
    d = t - c["start"]
    fin = Fraction(1)
    if c["fade_in"] > 0 and d < c["fade_in"]:
        fin = d / c["fade_in"]
    fout = Fraction(1)
    if c["fade_out"] > 0:
        remain = c["end"] - t
        if remain < c["fade_out"]:
            fout = remain / c["fade_out"]
    return fin * fout


def _source_value_exact(x: np.ndarray, a: int, b: int, p: Fraction) -> Fraction:
    """源实数位置 p 的精确样本值（有理插值 + 末样本保持），与引擎语义一致。"""
    i = p.numerator // p.denominator
    if i >= b - 1:
        return Fraction(int(x[b - 1]))
    frac = p - i
    return Fraction(int(x[i])) + frac * (int(x[i + 1]) - int(x[i]))


# ============================== 库房（持久化） ============================== #

class Vault:
    """磁带馆库房：实物盘、可切边界、已签借区段与刀口、历史借展单。

    全部状态持久化在 ``root/warehouse.json``；重开 Vault(root) 即可回读。
    """

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.index_path = os.path.join(root, "warehouse.json")
        self._reels: dict[str, Reel] = {}
        self._cut_bounds: dict[str, list[int]] = {}
        self._cuts_made: dict[str, list[int]] = {}
        self._loans: dict[str, list[dict[str, Any]]] = {}
        self._orders: dict[str, dict[str, Any]] = {}
        if os.path.exists(self.index_path):
            data = json.load(open(self.index_path, encoding="utf-8"))
            for r in data.get("reels", []):
                rec = Reel(**r)
                self._reels[rec.accession] = rec
            self._cut_bounds = {k: list(v) for k, v in
                                data.get("cut_bounds", {}).items()}
            self._cuts_made = {k: list(v) for k, v in
                               data.get("cuts_made", {}).items()}
            self._loans = {k: list(v) for k, v in data.get("loans", {}).items()}
            self._orders = dict(data.get("orders", {}))

    # ------------------------- 实物盘登记 ------------------------- #
    def register_reel(self, registry: AssetRegistry, accession: str,
                      asset_id: str, cut_bounds, note: str = "") -> Reel:
        """用登记仓中的实际 16kHz 单声道素材登记具名实物盘（核实际归档字节）。"""
        accession = str(accession)
        if accession in self._reels:
            raise VaultError(f"入藏号已存在: {accession}")
        if len(self._reels) >= MAX_REELS:
            raise VaultError(f"库房最多 {MAX_REELS} 盘")
        try:
            rec = registry.get(asset_id)
        except KeyError as e:
            raise VaultError(str(e)) from e
        if rec.sample_rate != OUTPUT_RATE:
            raise VaultError(f"实物盘须为 {OUTPUT_RATE}Hz 单声道素材，"
                             f"实际 {rec.sample_rate}Hz")
        pcm = registry.load_pcm(asset_id)          # 读实际字节并核摘要
        if digest_of(pcm.tobytes()) != rec.digest:
            raise VaultError(f"素材 {asset_id} 实际归档字节与登记摘要不符")
        bounds = sorted({int(b) for b in cut_bounds})
        for b in bounds:
            if not (0 <= b <= rec.sample_count):
                raise VaultError(f"可切边界 {b} 越界（0..{rec.sample_count}）")
        reel = Reel(accession, asset_id, rec.digest, rec.sample_count, note)
        self._reels[accession] = reel
        self._cut_bounds[accession] = bounds
        self._cuts_made.setdefault(accession, [])
        self._loans.setdefault(accession, [])
        self._flush()
        return reel

    # ------------------------- 查询 ------------------------- #
    def reel(self, accession: str) -> Reel:
        if accession not in self._reels:
            raise VaultError(f"未知实物盘: {accession}")
        return self._reels[accession]

    def reels(self) -> list[Reel]:
        return [self._reels[k] for k in sorted(self._reels)]

    def cut_bounds(self, accession: str) -> list[int]:
        self.reel(accession)
        return list(self._cut_bounds.get(accession, []))

    def cuts_made(self, accession: str) -> list[int]:
        self.reel(accession)
        return list(self._cuts_made.get(accession, []))

    def loans(self, accession: str) -> list[dict[str, Any]]:
        self.reel(accession)
        return [dict(l) for l in self._loans.get(accession, [])]

    def has_order(self, order_id: str) -> bool:
        return order_id in self._orders

    def get_order(self, order_id: str) -> dict[str, Any]:
        """回读已签借展单（重开库房后仍可读）。"""
        if order_id not in self._orders:
            raise VaultError(f"未知借展单: {order_id}")
        return json.loads(json.dumps(self._orders[order_id]))

    def orders(self) -> list[str]:
        return sorted(self._orders)

    def state_json(self) -> dict[str, Any]:
        return {
            "reels": [r.to_json() for r in self.reels()],
            "cut_bounds": {k: self._cut_bounds[k] for k in sorted(self._cut_bounds)},
            "cuts_made": {k: self._cuts_made[k] for k in sorted(self._cuts_made)},
            "loans": {k: self._loans[k] for k in sorted(self._loans)},
            "orders": sorted(self._orders),
        }

    # ------------------------- 签单入账 ------------------------- #
    def apply_order(self, order: dict[str, Any]) -> None:
        """把已签借展单落账：原段与刀口随实物入账，原单可回读。"""
        oid = order["order_id"]
        if oid in self._orders:
            raise VaultError(f"借展单已存在且不可变: {oid}")
        # 落账前再核：新签原段不得含已割走部分
        for a in order["assignments"]:
            if a["mode"] != "original":
                continue
            acc = a["accession"]
            s0, s1 = a["source_interval"]
            for l in self._loans.get(acc, []):
                if s0 < l["s1"] and s1 > l["s0"]:
                    raise VaultError(
                        f"盘 {acc} 区段 [{s0},{s1}) 含已割走部分 "
                        f"[{l['s0']},{l['s1']})，整笔拒绝")
        for a in order["assignments"]:
            if a["mode"] != "original":
                continue
            acc = a["accession"]
            s0, s1 = a["source_interval"]
            self._loans.setdefault(acc, []).append({
                "s0": s0, "s1": s1, "order_id": oid,
                "request_id": a["request_id"], "exhibitor": a["exhibitor"],
            })
            self._loans[acc].sort(key=lambda l: (l["s0"], l["s1"]))
        for acc, cuts in order["cuts"].items():
            self.reel(acc)
            merged = set(self._cuts_made.get(acc, [])) | {int(c) for c in cuts}
            self._cuts_made[acc] = sorted(merged)
        self._orders[oid] = order
        self._flush()

    def _flush(self) -> None:
        data = {
            "reels": [r.to_json() for r in self.reels()],
            "cut_bounds": self._cut_bounds,
            "cuts_made": self._cuts_made,
            "loans": self._loans,
            "orders": self._orders,
        }
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.index_path)


# ============================== 借展剪接台 ============================== #

class LoanOffice:
    """借展剪接台：资格核验（剪接师）+ 批量优化 + 签单入账 + 翻录新 WAV。"""

    def __init__(self, vault: Vault, registry: AssetRegistry, archive=None):
        self.vault = vault
        self.registry = registry
        self.archive = archive     # ArchiveStore；签单翻录时从归档读真实 render

    # ------------------------- 内部 ------------------------- #
    def _pcm_for(self, product: dict[str, Any]) -> dict[str, np.ndarray]:
        return {a["asset_id"]: self.registry.load_pcm(a["asset_id"])
                for a in product["assets"]}

    def _end_kind(self, reel: Reel, b: int) -> str:
        """可落刀判定：盘首尾/已有断口不花刀；可切边界可落新刀；其余不可落刀。"""
        if b == 0 or b == reel.sample_count:
            return "reel_end"
        if b in set(self.vault.cuts_made(reel.accession)):
            return "existing_break"
        if b in set(self.vault.cut_bounds(reel.accession)):
            return "cuttable"
        return "blocked"

    # ------------------------- 剪接师核验 ------------------------- #
    def analyze_request(self, product: dict[str, Any],
                        pcm_by_asset: dict[str, np.ndarray],
                        req: LoanRequest) -> dict[str, Any]:
        """逐拍核验借展段：列出可作原件的盘、连续原样本区间、可落刀两端，
        或说明哪一拍只能从母带翻录。"""
        clips = {c["clip_id"]: c for c in product_clip_view(product)}
        if req.clip_id not in clips:
            raise VaultError(f"节目中无具名 clip: {req.clip_id}")
        named = clips[req.clip_id]
        asset_digest = {a["asset_id"]: a["digest"]
                        for a in product["assets"]}[named["asset_id"]]

        bad_beats: list[dict[str, Any]] = []
        positions: list[Fraction | None] = []
        for n in range(req.out_lo, req.out_hi):
            t = Fraction(n, OUTPUT_RATE)
            reasons: list[str] = []
            p: Fraction | None = None
            if not (named["start"] <= t < named["end"]):
                reasons.append("该拍不在具名引用存续区间内")
            else:
                if named["gain"] != 1:
                    reasons.append(f"实际增益 {named['gain']} 不为 1")
                if _fade_factor_exact(named, t) != 1:
                    reasons.append("该拍受淡化改变")
                if named["rate"] * named["speed"] != OUTPUT_RATE:
                    reasons.append("非一步一源样本（源采样率×速度 ≠ 16000）")
                p = (named["a"] + (t - named["start"])
                     * named["rate"] * named["speed"])
                if p.denominator != 1:
                    reasons.append("源位置非整数拍（发生插值）")
                # 他轨参与制声检查：零增益不算参与；非零贡献即使相消也算参与
                for c in clips.values():
                    if c["clip_id"] == req.clip_id:
                        continue
                    if not (c["start"] <= t < c["end"]):
                        continue
                    g = c["gain"] * _fade_factor_exact(c, t)
                    if g == 0:
                        continue  # 零增益的另一轨不算参与制声
                    pc = (c["a"] + (t - c["start"]) * c["rate"] * c["speed"])
                    v = _source_value_exact(
                        pcm_by_asset[c["asset_id"]], c["a"], c["b"], pc)
                    if v * g != 0:
                        reasons.append(
                            f"他轨 {c['clip_id']} 在该拍有非零贡献，"
                            f"具名引用并非独自制声")
            if reasons:
                bad_beats.append({"beat": n, "reasons": reasons})
            positions.append(p)

        qualified = not bad_beats
        source_interval = None
        if qualified:
            s0 = positions[0]
            assert s0 is not None and s0.denominator == 1
            source_interval = [int(s0), int(s0) + req.beats]  # 连续原样本区间

        reels_report = []
        if source_interval is not None:
            s0, s1 = source_interval
            for reel in self.vault.reels():
                if reel.digest != asset_digest:
                    continue  # 不是同一录音的实物盘
                # 核实际归档字节：实物盘登记摘要须与节目资产摘要一致（上方已等值），
                # 且登记仓能读出与摘要相符的实际字节
                self.registry.load_pcm(reel.asset_id)
                conflicts = [[l["s0"], l["s1"]]
                             for l in self.vault.loans(reel.accession)
                             if l["s0"] < s1 and l["s1"] > s0]
                ends = {"lo": self._end_kind(reel, s0),
                        "hi": self._end_kind(reel, s1)}
                eligible = (not conflicts
                            and ends["lo"] != "blocked" and ends["hi"] != "blocked")
                reels_report.append({
                    "accession": reel.accession,
                    "eligible": eligible,
                    "conflicts": conflicts,   # 与已签借区段的冲突（已割走部分）
                    "ends": ends,             # 可落刀的两端
                })
        return {
            "request_id": req.request_id,
            "exhibitor": req.exhibitor,
            "clip_id": req.clip_id,
            "output_interval": [req.out_lo, req.out_hi],
            "beats": req.beats,
            "qualified": qualified,
            "source_interval": source_interval,
            "bad_beats": bad_beats,          # 这些拍只能从母带翻录
            "reels": reels_report,           # 实际可作原件的盘
            "accept_rerecord": req.accept_rerecord,
            "rerecord_beats": req.rerecord_beats,
        }

    def analyze(self, product: dict[str, Any],
                requests: list[LoanRequest]) -> list[dict[str, Any]]:
        pcm = self._pcm_for(product)
        validate_product(product, pcm)        # 核实际归档字节 + 产品完整性
        self._check_batch(requests)
        return [self.analyze_request(product, pcm, r) for r in requests]

    @staticmethod
    def _check_batch(requests: list[LoanRequest]) -> None:
        if not (1 <= len(requests) <= MAX_REQUESTS):
            raise VaultError(f"每单请求数须为 1..{MAX_REQUESTS}")
        ids = [r.request_id for r in requests]
        if len(set(ids)) != len(ids):
            raise VaultError("同一单内 request_id 重复")

    # ------------------------- 批量优化 ------------------------- #
    def plan(self, product: dict[str, Any],
             requests: list[LoanRequest], quota: int) -> dict[str, Any]:
        """批量优化：优先总价值，再少翻录拍数，再少真落刀。"""
        quota = int(quota)
        if quota < 0:
            raise VaultError("翻录额度不能为负")
        pcm = self._pcm_for(product)
        validate_product(product, pcm)
        self._check_batch(requests)
        reports = [self.analyze_request(product, pcm, r) for r in requests]

        n = len(requests)
        reels = [r.accession for r in self.vault.reels()]
        n_samples = {acc: self.vault.reel(acc).sample_count for acc in reels}
        cuts_made = {acc: set(self.vault.cuts_made(acc)) for acc in reels}

        # 每个请求的原件选项：(accession, s0, s1)
        orig_opts: list[list[tuple[str, int, int]]] = []
        for rep in reports:
            opts = []
            if rep["qualified"]:
                s0, s1 = rep["source_interval"]
                for r in rep["reels"]:
                    if r["eligible"]:
                        opts.append((r["accession"], s0, s1))
            orig_opts.append(sorted(opts))

        order = sorted(range(n), key=lambda i: (-requests[i].value,
                                                requests[i].request_id))
        # 上界剪枝：未选原件的请求之后仍可能翻录，须计入上界。
        # suffix[k]   = 余下请求中尚可服务者（有原件选项或收翻录）的价值和
        # mand_beats[k] = 余下请求中无原件选项且收翻录者的拍数和（达上界必须翻录）
        servable = [bool(orig_opts[i]) or requests[i].accept_rerecord
                    for i in range(n)]
        suffix = [0] * (n + 1)
        mand_beats = [0] * (n + 1)
        for k in range(n - 1, -1, -1):
            i = order[k]
            suffix[k] = suffix[k + 1] + (requests[i].value if servable[i] else 0)
            mand_beats[k] = mand_beats[k + 1] + (
                requests[i].rerecord_beats
                if (requests[i].accept_rerecord and not orig_opts[i]) else 0)

        occupied: dict[str, list[tuple[int, int]]] = {acc: [] for acc in reels}
        chosen: dict[int, str] = {}
        # 同分时按固定搜索次序（先原件后跳过、入藏号字典序、请求价值降序）
        # 取首先达到最优者，结果确定。
        best: dict[str, Any] = {"key": None, "chosen": None, "rerecord": None}

        def cuts_total() -> int:
            """真落刀数：盘首尾/已有断口不花刀；同批共用切口只算一次。"""
            total = 0
            for acc in reels:
                bs = set()
                for (s0, s1) in occupied[acc]:
                    for b in (s0, s1):
                        if b == 0 or b == n_samples[acc] or b in cuts_made[acc]:
                            continue
                        bs.add(b)
                total += len(bs)
            return total

        def pick_rerecord(chosen_set: set[int]):
            """余下请求在额度内选翻录子集：最大价值、再少拍数（帕累托前沿 DP）。"""
            frontier: list[tuple[int, int, tuple[int, ...]]] = [(0, 0, ())]
            for i in range(n):
                if i in chosen_set or not requests[i].accept_rerecord:
                    continue
                b, v = requests[i].rerecord_beats, requests[i].value
                frontier += [(bb + b, vv + v, m + (i,))
                             for (bb, vv, m) in frontier if bb + b <= quota]
                frontier.sort(key=lambda e: (e[0], -e[1], e[2]))
                merged: list[tuple[int, int, tuple[int, ...]]] = []
                top = -1
                for e in frontier:
                    if e[1] > top:
                        merged.append(e)
                        top = e[1]
                frontier = merged
            return max(frontier, key=lambda e: (e[1], -e[0],
                                                tuple(-x for x in e[2])))

        def dfs(k: int, orig_value: int, skipped_val: int,
                skipped_beats: int) -> None:
            if best["key"] is not None:
                bv, bb, bc = best["key"]
                bound = orig_value + suffix[k] + skipped_val
                if bound < bv:
                    return              # 价值上界已不可能超过最优
                if bound == bv:
                    # 只能追平价值：达上界必须翻录的拍数若已更差，或拍数追平
                    # 而落刀已不更少（同分取先者），则无需展开
                    mb = skipped_beats + mand_beats[k]
                    if mb > -bb:
                        return
                    if mb == -bb and cuts_total() >= -bc:
                        return
            if k == n:
                beats, rval, mask = pick_rerecord(set(chosen))
                key = (orig_value + rval, -beats, -cuts_total())
                if best["key"] is None or key > best["key"]:
                    best.update(key=key, chosen=dict(chosen), rerecord=mask)
                return
            i = order[k]
            for (acc, s0, s1) in orig_opts[i]:
                if all(s1 <= o0 or s0 >= o1 for (o0, o1) in occupied[acc]):
                    chosen[i] = acc
                    occupied[acc].append((s0, s1))
                    dfs(k + 1, orig_value + requests[i].value,
                        skipped_val, skipped_beats)
                    occupied[acc].pop()
                    del chosen[i]
            # 跳过原件：若收翻录，之后仍可能翻录，计入上界与必翻录拍数
            add_v = requests[i].value if requests[i].accept_rerecord else 0
            add_b = requests[i].rerecord_beats if requests[i].accept_rerecord else 0
            dfs(k + 1, orig_value, skipped_val + add_v, skipped_beats + add_b)

        dfs(0, 0, 0, 0)
        assert best["chosen"] is not None

        # ---------------- 汇总方案 ---------------- #
        rerecord_set = set(best["rerecord"])
        assignments = []
        for i, req in enumerate(requests):
            rep = reports[i]
            if i in best["chosen"]:
                acc = best["chosen"][i]
                assignments.append({
                    "request_id": req.request_id, "exhibitor": req.exhibitor,
                    "mode": "original", "accession": acc,
                    "source_interval": list(rep["source_interval"]),
                    "output_interval": [req.out_lo, req.out_hi],
                })
            elif i in rerecord_set:
                assignments.append({
                    "request_id": req.request_id, "exhibitor": req.exhibitor,
                    "mode": "rerecord", "beats": req.rerecord_beats,
                    "output_interval": [req.out_lo, req.out_hi],
                })
            else:
                reason = []
                if not rep["qualified"]:
                    reason.append("整段并非每拍都可原盘取样")
                if rep["qualified"] and not orig_opts[i]:
                    reason.append("无可用实物盘（已割走或不可落刀）")
                if req.accept_rerecord:
                    reason.append("翻录额度不足或不如其他方案合算")
                else:
                    reason.append("展方只收原带")
                assignments.append({
                    "request_id": req.request_id, "exhibitor": req.exhibitor,
                    "mode": "rejected",
                    "output_interval": [req.out_lo, req.out_hi],
                    "reason": "；".join(reason),
                })

        cuts: dict[str, list[int]] = {}
        for acc in reels:
            bs = set()
            for i, a in best["chosen"].items():
                if a != acc:
                    continue
                s0, s1 = reports[i]["source_interval"]
                for b in (s0, s1):
                    if b == 0 or b == n_samples[acc] or b in cuts_made[acc]:
                        continue
                    bs.add(b)
            if bs:
                cuts[acc] = sorted(bs)

        value, neg_beats, neg_cuts = best["key"]
        return {
            "assignments": assignments,
            "cuts": cuts,                       # 各盘新落刀口（先分切再交付）
            "reels_to_cut": sorted(cuts),       # 哪些真带要先分切
            "totals": {"value": value, "rerecord_beats": -neg_beats,
                       "new_cuts": -neg_cuts},
            "quota": quota,
            "analysis": reports,
        }

    # ------------------------- 签单 ------------------------- #
    def book(self, order_id: str, product: dict[str, Any],
             requests: list[LoanRequest], quota: int, T: Any = None) -> dict[str, Any]:
        """优化并签单：原段/刀口随实物入账；翻录段从归档录成新 WAV。"""
        order_id = str(order_id)
        if self.vault.has_order(order_id):
            raise VaultError(f"借展单已存在且不可变: {order_id}")
        pcm = self._pcm_for(product)
        plan = self.plan(product, requests, quota)

        hi = max(r.out_hi for r in requests)
        T = Fraction(hi, OUTPUT_RATE) if T is None else as_r(T)
        if output_sample_count(T) < hi:
            raise VaultError("T 不足以覆盖借展段输出区间")

        # 翻录：按该段真实 render 从头到尾录成新 WAV（从归档读取并逐样本核对）
        wavs: dict[str, Any] = {}
        need = [a for a in plan["assignments"] if a["mode"] == "rerecord"]
        if need:
            ref = render_reference(product, pcm, T)
            render_id = f"loan-{order_id}"
            if self.archive is not None:
                existing = {m["render_id"] for m in self.archive.list_renders()}
                if render_id not in existing:
                    self.archive.render(product, T, pcm, render_id=render_id)
            for a in need:
                lo, hi2 = a["output_interval"]
                if self.archive is not None:
                    seg = self.archive.read_samples(render_id, lo, hi2)
                    if not np.array_equal(seg, ref[lo:hi2]):
                        raise VaultError(f"请求 {a['request_id']} 归档读取与"
                                         f"参考 render 不符，拒绝签单")
                else:
                    seg = ref[lo:hi2]
                if len(seg) != a["beats"]:
                    raise VaultError(f"请求 {a['request_id']} 翻录拍数不符")
                path = os.path.join(self.vault.root, "loans", order_id,
                                    a["request_id"] + ".wav")
                write_wav(path, seg, OUTPUT_RATE)
                wavs[a["request_id"]] = {
                    "path": path, "beats": int(len(seg)),
                    "sha256": hashlib.sha256(seg.tobytes()).hexdigest(),
                }

        order = {
            "order_id": order_id,
            "product_id": product["product_id"],
            "created_at": time.time(),
            "quota": quota,
            "requests": [r.to_json() for r in requests],
            "assignments": plan["assignments"],
            "cuts": plan["cuts"],
            "reels_to_cut": plan["reels_to_cut"],
            "totals": plan["totals"],
            "rerecord_wavs": wavs,
        }
        self.vault.apply_order(order)
        return order
