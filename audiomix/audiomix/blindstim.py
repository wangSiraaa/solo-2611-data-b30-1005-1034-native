"""双盲听审刺激制作：等长 render 区间 × 批准正整数增益档 -> 可盲听组合。

规则（全部精确整数运算，绝不引入浮点）：
- 逐样本取旧混音的整数 PCM，乘各自批准的正整数增益；
- 只要有一个样本乘完超出有符号 16 位范围 [-32768, 32767]，整个组合退回
  （不是只削顶超界样本，而是该 (增益档A, 增益档B) 组合整体不可用）；
- 两份放大后的整数平方和必须相等（音量配平）；
- 且至少一个对应采样不同（两份全相同的声音不能印成有效问卷）；
- 两项长度必须相等（每项是等长区间）。

输出的两份“实包”只带匿名编号，文件名/标签里不出现 A、B 或源 render 身份；
身份只写进另外封存的追溯表。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

INT16_MIN = -32768
INT16_MAX = 32767

# 同一比较两份实包的区分标签：刻意不使用 A/B 字样，避免听者从文件名猜出身份
SIDE_TOKENS = ("X", "Y")


class StimulusError(ValueError):
    """刺激制作/拆封校验失败。"""


# ============================ 纯整数刺激校验 ============================ #

def _as_int_list(pcm: Iterable[int], label: str) -> list[int]:
    vals = [int(v) for v in pcm]
    for v in vals:
        if not (INT16_MIN <= v <= INT16_MAX):
            raise StimulusError(f"{label}: 源 PCM 样本 {v} 超出有符号 16 位范围")
    if not vals:
        raise StimulusError(f"{label}: 区间为空，不允许零样本刺激")
    return vals


def _scale_or_none(pcm: Sequence[int], gain: int) -> list[int] | None:
    """逐样本乘正整数增益；任一结果越界则返回 None（整个组合退回）。"""
    if gain <= 0:
        raise StimulusError(f"增益档必须为正整数，收到 {gain}")
    out: list[int] = []
    for v in pcm:
        s = v * gain
        if not (INT16_MIN <= s <= INT16_MAX):
            return None
        out.append(s)
    return out


def sum_of_squares(pcm: Sequence[int]) -> int:
    return sum(int(v) * int(v) for v in pcm)


@dataclass(frozen=True)
class StimulusCombo:
    """一个通过全部规则的可用盲听组合。"""
    item_id: str
    gain_a: int
    gain_b: int
    scaled_a: tuple[int, ...]
    scaled_b: tuple[int, ...]
    sum_sq: int
    differ_positions: int

    def sides_differ(self) -> bool:
        return self.differ_positions > 0


def build_combos(item_id: str, pcm_a: Iterable[int], pcm_b: Iterable[int],
                 gains_a: Iterable[int], gains_b: Iterable[int]) -> list[StimulusCombo]:
    """枚举一项比较的全部批准增益档组合，返回所有合规组合。

    合规条件：两份等长、各自放大后均不越界、整数平方和相等、至少一个采样不同。
    结果按 (增益档A, 增益档B) 排序，保证上层“任选一组”可稳定复现。
    """
    a = _as_int_list(pcm_a, f"比较 {item_id} 的 render A")
    b = _as_int_list(pcm_b, f"比较 {item_id} 的 render B")
    if len(a) != len(b):
        raise StimulusError(
            f"比较 {item_id}: 两份区间不等长（{len(a)} != {len(b)}），无法配平")
    ga = sorted({int(g) for g in gains_a})
    gb = sorted({int(g) for g in gains_b})
    if not ga or not gb:
        raise StimulusError(f"比较 {item_id}: 两侧都必须至少给出一个批准增益档")

    combos: list[StimulusCombo] = []
    for g_a in ga:
        scaled_a = _scale_or_none(a, g_a)
        if scaled_a is None:
            continue
        sqa = sum_of_squares(scaled_a)
        for g_b in gb:
            scaled_b = _scale_or_none(b, g_b)
            if scaled_b is None:
                continue
            if sum_of_squares(scaled_b) != sqa:
                continue  # 音量未配平
            diff = sum(1 for x, y in zip(scaled_a, scaled_b) if x != y)
            if diff == 0:
                continue  # 字节完全相同 -> 不能印成有效问卷
            combos.append(StimulusCombo(
                item_id=item_id, gain_a=g_a, gain_b=g_b,
                scaled_a=tuple(scaled_a), scaled_b=tuple(scaled_b),
                sum_sq=sqa, differ_positions=diff))
    return combos


# ============================ 匿名实包 + 封存追溯表 ============================ #

def _pcm_digest(samples: Sequence[int]) -> str:
    """对放大后的整数 PCM 正文（小端 int16 字节）取 SHA-256。"""
    import array
    buf = array.array("h", samples)
    if array.array("h").itemsize != 2:  # pragma: no cover - 平台保护
        raise RuntimeError("当前平台原生 short 非 16 位")
    return hashlib.sha256(bytes(buf)).hexdigest()


def _anon_token(rng: secrets.SystemRandom | Any, alphabet: str, length: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(length))


@dataclass
class BlindPackage:
    """一份无 A/B 明文标签的匿名实包。"""
    anon_id: str                 # 全局唯一匿名号，不含身份信息
    side: str                    # X / Y，仅区分同一比较的两份实包
    item_id: str
    source_render: str           # "A" / "B"，仅写入封存追溯表
    gain: int
    samples: tuple[int, ...]
    sample_digest: str
    package_file: str | None = None

    def public_manifest(self) -> dict[str, Any]:
        """可发给听审现场的明文清单：只有匿名号与样本摘要，无身份。"""
        return {
            "anon_id": self.anon_id,
            "side": self.side,
            "item_id": self.item_id,
            "sample_count": len(self.samples),
            "sample_digest": self.sample_digest,
            "package_file": self.package_file,
        }


@dataclass
class SealedTrace:
    """封存追溯表条目：匿名号 <-> 源 render/片段/增益 的唯一映射来源。"""
    item_id: str
    anon_id: str
    side: str
    source_render: str
    gain: int
    sum_sq: int
    sample_digest: str


@dataclass
class ItemStimulusSet:
    """一项比较选定的一组刺激：两份匿名实包 + 封存追溯表条目。"""
    item_id: str
    combo: StimulusCombo
    packages: tuple[BlindPackage, BlindPackage]
    traces: tuple[SealedTrace, SealedTrace]
    # 两种播放序各自的匿名号序列；现场只看得到匿名号
    order_ab: tuple[str, str]    # 源 A 先、源 B 后
    order_ba: tuple[str, str]    # 源 B 先、源 A 后

    def package_by_anon(self, anon_id: str) -> BlindPackage:
        for pkg in self.packages:
            if pkg.anon_id == anon_id:
                return pkg
        raise KeyError(anon_id)

    def order_for(self, first_render: str) -> tuple[str, str]:
        """first_render 为该场先播的源 render（'A' 或 'B'）。"""
        if first_render == "A":
            return self.order_ab
        if first_render == "B":
            return self.order_ba
        raise StimulusError(f"非法源 render 标识: {first_render!r}")


def make_item_packages(combo: StimulusCombo,
                       rng: Any | None = None,
                       id_length: int = 12) -> ItemStimulusSet:
    """把一个合规组合封成两份匿名实包与封存追溯条目。

    匿名号由随机字节生成（默认 ``secrets.SystemRandom``），与 A/B 身份无关；
    追溯表是匿名号到源身份的唯一映射，单独封存，不随实包下发。
    """
    if rng is None:
        rng = secrets.SystemRandom()
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混字符 0/O/1/I
    used: set[str] = set()

    def fresh_id() -> str:
        for _ in range(100):
            token = _anon_token(rng, alphabet, id_length)
            if token not in used:
                used.add(token)
                return token
        raise StimulusError("匿名号反复撞号，检查随机源")  # pragma: no cover

    pkg_specs = (("X", "A", combo.gain_a, combo.scaled_a),
                 ("Y", "B", combo.gain_b, combo.scaled_b))
    packages: list[BlindPackage] = []
    traces: list[SealedTrace] = []
    anon: dict[str, str] = {}
    for side, render, gain, samples in pkg_specs:
        anon_id = fresh_id()
        digest = _pcm_digest(samples)
        packages.append(BlindPackage(
            anon_id=anon_id, side=side, item_id=combo.item_id,
            source_render=render, gain=gain, samples=tuple(samples),
            sample_digest=digest, package_file=f"{anon_id}.pcm.json"))
        traces.append(SealedTrace(
            item_id=combo.item_id, anon_id=anon_id, side=side,
            source_render=render, gain=gain, sum_sq=combo.sum_sq,
            sample_digest=digest))
        anon[render] = anon_id

    return ItemStimulusSet(
        item_id=combo.item_id, combo=combo,
        packages=(packages[0], packages[1]), traces=(traces[0], traces[1]),
        order_ab=(anon["A"], anon["B"]), order_ba=(anon["B"], anon["A"]))


# ============================ 落盘 / 拆封验证 ============================ #

def write_stimulus_bundle(stim: ItemStimulusSet, out_dir: str) -> dict[str, str]:
    """写两份匿名实包（公开目录）与一份封存追溯表（sealed 目录）。

    返回 {"public_dir", "sealed_dir", "manifest", "sealed_table"} 路径。
    公开目录的任何文件名/正文都不含 A/B 或源 render 信息。
    """
    public_dir = os.path.join(out_dir, stim.item_id, "public")
    sealed_dir = os.path.join(out_dir, stim.item_id, "sealed")
    os.makedirs(public_dir, exist_ok=True)
    os.makedirs(sealed_dir, exist_ok=True)

    public_records = []
    for pkg in stim.packages:
        path = os.path.join(public_dir, pkg.package_file)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "anon_id": pkg.anon_id,
                "side": pkg.side,
                "item_id": stim.item_id,
                "sample_count": len(pkg.samples),
                "sample_digest": pkg.sample_digest,
                "samples": list(pkg.samples),
            }, fh, ensure_ascii=False, indent=2)
        public_records.append(pkg.public_manifest())

    manifest_path = os.path.join(public_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump({"item_id": stim.item_id, "packages": public_records,
                   "orders": {"FIRST_THEN_SECOND_AB": list(stim.order_ab),
                              "FIRST_THEN_SECOND_BA": list(stim.order_ba)}},
                  fh, ensure_ascii=False, indent=2)

    sealed_path = os.path.join(sealed_dir, "trace_table.json")
    with open(sealed_path, "w", encoding="utf-8") as fh:
        json.dump({
            "item_id": stim.item_id,
            "sum_sq": stim.combo.sum_sq,
            "differ_positions": stim.combo.differ_positions,
            "gain_a": stim.combo.gain_a,
            "gain_b": stim.combo.gain_b,
            "entries": [t.__dict__ for t in stim.traces],
        }, fh, ensure_ascii=False, indent=2)
    return {"public_dir": public_dir, "sealed_dir": sealed_dir,
            "manifest": manifest_path, "sealed_table": sealed_path}


def verify_bundle_integrity(stim: ItemStimulusSet) -> None:
    """持封存表复核：实包摘要、平方和配平、两序确实相反、身份映射自洽。"""
    digests = {p.anon_id: p.sample_digest for p in stim.packages}
    renders = {p.anon_id: p.source_render for p in stim.packages}
    if len(set(digests.values())) != 2:
        raise StimulusError("两份实包正文摘要相同，违反“至少一个采样不同”")
    for pkg in stim.packages:
        if _pcm_digest(pkg.samples) != pkg.sample_digest:
            raise StimulusError(f"实包 {pkg.anon_id} 正文与摘要不符")
    sqs = {sum_of_squares(p.samples) for p in stim.packages}
    if len(sqs) != 1:
        raise StimulusError("两份实包整数平方和不相等，音量未配平")
    if set(stim.order_ab) != set(stim.order_ba):
        raise StimulusError("两种播放序没有使用同一对匿名实包")
    if stim.order_ab == stim.order_ba:
        raise StimulusError("两种播放序相同，未做换序")
    if stim.order_ab[0] != stim.order_ba[1] or stim.order_ab[1] != stim.order_ba[0]:
        raise StimulusError("两种播放序不是严格相反的顺序")
    for anon_id, render in renders.items():
        trace = next((t for t in stim.traces if t.anon_id == anon_id), None)
        if trace is None or trace.source_render != render:
            raise StimulusError(f"匿名号 {anon_id} 的封存追溯条目缺失或身份不符")


def sign_manifest(manifest_bytes: bytes, secret: bytes) -> str:
    """封存表防篡改 HMAC（持表人可对公开播放单做独立验签）。"""
    return hmac.new(secret, manifest_bytes, hashlib.sha256).hexdigest()


def field_report(stim: ItemStimulusSet) -> dict[str, Any]:
    """供测试/CLI 输出的结构化报告（含是否合规的全部数值依据）。"""
    return {
        "item_id": stim.item_id,
        "gain_a": stim.combo.gain_a,
        "gain_b": stim.combo.gain_b,
        "sum_sq": stim.combo.sum_sq,
        "differ_positions": stim.combo.differ_positions,
        "anon_ids": [p.anon_id for p in stim.packages],
        "order_ab": list(stim.order_ab),
        "order_ba": list(stim.order_ba),
    }
