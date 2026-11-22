"""试听厅离散整数卷积校准与最低安装费配置。

本模块沿用 audiomix 的素材/档案语义，解决两类问题：

1. **单路还原（deconvolution）**
   激励 ``x`` 是从具名原剪辑发布并已渲染的短 PCM，``x[0] != 0``；观测 ``y``
   是另一份整段实录 PCM。模型是本地离散整数卷积
   ``y = x * h``（完整线性卷积，含末尾零），响应长度上限 ``H`` 已知。
   我们做**精确整数长除**，逐拍还原有符号整数响应 ``h[0..H-1]``。
   不用浮点、不把截断/舍入/饱和当成额外反射：只要不能逐样本用长度 H 的
   整数响应还原，就返回第一处不符（``first_mismatch``）。

2. **全厅配置（hall configuration）**
   展厅开幕时各喇叭播同一单位脉冲。每只喇叭可任选一种已标定工作状态
   （裸机 ``bare`` / 装自己的板 ``panel``）与一个允许的非负整数电延迟，
   各带安装费。延迟把该喇叭对各座位的整条响应整体右移；装板换用该机对
   各座位已核出的相应响应。某座位在同一拍收到全部有符号贡献后**先求和**，
   ``|和| >= 门限`` 才算响（同拍相反响应必须相消，不能算两声）。
   每座第一声不得晚于 ``latest_onset``；第一声之后，仍有达门限声响与第一声
   的拍差 ``> allowed_gap``，即算重影。求让所有座位都听得到且无重影的最低
   总安装费。

校准记录按 ``(喇叭, 座位, 板况)`` 归属；开板/闭板是两套互不借用的记录。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from itertools import product
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .assets import AssetRegistry, write_wav
from .engine import OUTPUT_RATE

BARE = "bare"
PANEL = "panel"
STATES = (BARE, PANEL)

MAX_SPEAKERS = 6
MAX_SEATS = 4
MAX_DELAYS = 3
MAX_STATES = 2


# ====================================================================== #
# 1. 精确整数卷积 / 反卷积                                                  #
# ====================================================================== #

def convolve_int(x: Sequence[int], h: Sequence[int]) -> list[int]:
    """整数线性卷积（完整，长度 len(x)+len(h)-1）。纯 Python 整数，无舍入。"""
    x = [int(v) for v in x]
    h = [int(v) for v in h]
    out = [0] * (len(x) + len(h) - 1) if (x and h) else []
    for i, xv in enumerate(x):
        if xv == 0:
            continue
        for j, hv in enumerate(h):
            if hv:
                out[i + j] += xv * hv
    return out


@dataclass(frozen=True)
class DeconvResult:
    """单路还原结论。

    ``first_mismatch`` 是首个无法用“规定长度为 H 的整数响应”解释的**观测样本
    下标**（从 0 起）。``ok`` 为真时 ``taps`` 长度恰为 ``max_len``（含末尾零）。
    """
    ok: bool
    taps: tuple[int, ...] | None
    max_len: int
    excitation_len: int
    observation_len: int
    first_mismatch: int | None
    kind: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["taps"] = list(self.taps) if self.taps is not None else None
        d["mismatch_unit"] = "observation_sample"
        return d


def deconvolve_integer(x: Sequence[int], y: Sequence[int], max_len: int) -> DeconvResult:
    """用精确整数长除还原长度上限 ``max_len`` 的整数响应 ``h``。

    约束：``x`` 非空且 ``x[0] != 0``（题面约定）。若 ``y`` 长度小于完整卷积
    长度 ``len(x)+max_len-1``，说明观测截断，不能补造反射；若观测在完整卷积
    之后仍有非零样本，说明真实响应比 ``max_len`` 更长，同样不行。两种情况都
    返回 ``ok=False`` 与第一处不符下标。
    """
    x = [int(v) for v in x]
    y = [int(v) for v in y]
    if max_len <= 0:
        raise ValueError("max_len 必须为正")
    if not x:
        raise ValueError("激励不能为空")
    Lx, Ly = len(x), len(y)
    full_len = Lx + max_len - 1

    if x[0] == 0:
        return DeconvResult(False, None, max_len, Lx, Ly, 0, "bad_excitation",
                            "激励首个样本为 0，无法唯一还原整数响应")

    if Ly < full_len:
        return DeconvResult(
            False, None, max_len, Lx, Ly, Ly, "truncated",
            f"观测长度 {Ly} < 完整卷积长度 {full_len}；第 {Ly} 拍起缺失，"
            f"拒绝把截断补造成另一路反射")

    x0 = x[0]
    taps: list[int] = []
    for k in range(max_len):
        acc = y[k]
        # acc -= sum_{i=1..k} h[k-i] * x[i]
        for i in range(1, min(k, Lx - 1) + 1):
            acc -= taps[k - i] * x[i]
        if acc % x0 != 0:
            return DeconvResult(
                False, None, max_len, Lx, Ly, k, "non_integer",
                f"第 {k} 拍无法用整数响应还原：分子 {acc} 不能被 x[0]={x0} 整除"
                "（拒绝把舍入/饱和当成反射）")
        taps.append(acc // x0)

    # 前 max_len 拍由构造必然吻合；这里核对全部完整卷积并定位首个不符。
    conv = convolve_int(x, taps)
    for n in range(full_len):
        if conv[n] != y[n]:
            return DeconvResult(
                False, None, max_len, Lx, Ly, n, "tail_mismatch",
                f"第 {n} 拍观测 {y[n]} != 整数卷积 {conv[n]}")
    # 完整卷积之后：允许观测额外带全零尾巴，出现非零即超长响应。
    for n in range(full_len, Ly):
        if y[n] != 0:
            return DeconvResult(
                False, None, max_len, Lx, Ly, n, "extra_tail",
                f"第 {n} 拍观测非零（{y[n]}），但长度 {max_len} 的响应到 "
                f"{full_len - 1} 拍已结束；不把多余尾巴当作额外反射")

    return DeconvResult(True, tuple(taps), max_len, Lx, Ly, None, "ok",
                        f"逐样本吻合，响应 {list(taps)}")


# ====================================================================== #
# 2. 校准记录仓（按 喇叭×座位×板况 归属；开板/闭板不互借）                  #
# ====================================================================== #

class PanelStateError(ValueError):
    """请求了未标定的板况，或试图借用另一板况的记录。"""


@dataclass(frozen=True)
class PathKey:
    speaker: str
    seat: str
    state: str


@dataclass(frozen=True)
class CalibrationRecord:
    """一份校验录音：绑定 (喇叭, 座位, 板况) 与一对激励/观测素材。"""
    excitation_asset: str
    observation_asset: str
    max_len: int
    note: str = ""


class CalibrationStore:
    """校验录音仓。记录严格按板况分柜，不跨板况回退。"""

    def __init__(self, registry: AssetRegistry):
        self.registry = registry
        self._records: dict[PathKey, list[CalibrationRecord]] = {}

    def add_record(self, speaker: str, seat: str, state: str,
                   excitation_asset: str, observation_asset: str,
                   max_len: int, note: str = "") -> CalibrationRecord:
        if state not in STATES:
            raise ValueError(f"未知板况: {state!r}，只能是 {STATES}")
        if max_len <= 0:
            raise ValueError("max_len 必须为正")
        # 素材必须真实存在于登记仓（绑定正文与样本数）。
        self.registry.get(excitation_asset)
        self.registry.get(observation_asset)
        rec = CalibrationRecord(excitation_asset, observation_asset, int(max_len), note)
        self._records.setdefault(PathKey(speaker, seat, state), []).append(rec)
        return rec

    def records(self, speaker: str, seat: str, state: str) -> list[CalibrationRecord]:
        key = PathKey(speaker, seat, state)
        if key not in self._records or not self._records[key]:
            raise PanelStateError(
                f"缺少 ({speaker},{seat},{state}) 的校验录音；"
                f"开板/闭板两套记录不可互借")
        return list(self._records[key])

    def recover(self, speaker: str, seat: str, state: str,
                which: int = 0) -> DeconvResult:
        """单查一路：还原指定 (喇叭, 座位, 板况) 的整数响应。"""
        recs = self.records(speaker, seat, state)
        rec = recs[which]
        x = self.registry.load_pcm(rec.excitation_asset)
        y = self.registry.load_pcm(rec.observation_asset)
        return deconvolve_integer(x.tolist(), y.tolist(), rec.max_len)

    def validate_path(self, speaker: str, seat: str, state: str) -> dict[str, Any]:
        """用**另一个真实试音片**交叉验证同一路：多份记录须还原出同一响应。

        这是独立校验手段：同一路用不同激励/实录各还原一次，响应必须逐拍一致。
        任一份不能用整数响应还原，即返回其首处不符。
        """
        recs = self.records(speaker, seat, state)
        results = [self.recover(speaker, seat, state, i) for i in range(len(recs))]
        failures = [r for r in results if not r.ok]
        if failures:
            bad = failures[0]
            return {"ok": False, "kind": bad.kind,
                    "first_mismatch": bad.first_mismatch, "detail": bad.detail}
        taps = [r.taps for r in results]
        if any(t != taps[0] for t in taps[1:]):
            return {"ok": False, "kind": "inconsistent",
                    "first_mismatch": _first_tap_diff(taps),
                    "detail": f"不同试音片还原出的响应不一致: {taps}"}
        return {"ok": True, "taps": list(taps[0]), "records": len(recs)}


def _first_tap_diff(tapsets: list[tuple[int, ...]]) -> int:
    for k in range(min(len(t) for t in tapsets)):
        if len({t[k] for t in tapsets}) > 1:
            return k
    return min(len(t) for t in tapsets)


# ====================================================================== #
# 3. 全厅离散模型与最低费用求解                                            #
# ====================================================================== #

@dataclass(frozen=True)
class SpeakerOption:
    """某只喇叭的一种可选工作状态 + 电延迟 + 安装费。"""
    state: str
    delay: int
    cost: int

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError(f"未知板况: {self.state!r}")
        if self.delay < 0:
            raise ValueError("电延迟必须为非负整数")
        if self.cost < 0:
            raise ValueError("安装费必须非负")


@dataclass(frozen=True)
class SeatSpec:
    threshold: int
    latest_onset: int
    allowed_gap: int


@dataclass
class HallProblem:
    """全厅问题。

    ``responses[(speaker, state, seat)]`` 是该状态下该喇叭到该座位的整数响应
    拍序列（长度 H）；延迟会把整条序列右移。
    """
    speakers: tuple[str, ...]
    seats: tuple[str, ...]
    responses: dict[tuple[str, str, str], tuple[int, ...]]
    options: dict[str, tuple[SpeakerOption, ...]]
    seat_specs: dict[str, SeatSpec]

    def __post_init__(self):
        _validate_hall_shape(self.speakers, self.seats, self.options,
                             self.seat_specs, self.responses)

    # ---- 单配置求值 ---- #
    def seat_lines(self, config: Mapping[str, SpeakerOption]) -> dict[str, dict[str, Any]]:
        """逐座逐拍汇总：先求有符号和，再判定是否达门限。

        返回 {seat: {beat: {"sum", "audible", "contributions"}}}；同一拍即使
        正负相消为 0 也保留（用于展示“相消不是两声”）。
        """
        lines: dict[str, dict[int, dict[str, Any]]] = {
            a: {} for a in self.seats}
        for s in self.speakers:
            opt = config[s]
            for a in self.seats:
                taps = self.responses[(s, opt.state, a)]
                for d, v in enumerate(taps):
                    if v == 0:
                        continue
                    b = d + opt.delay
                    cell = lines[a].setdefault(
                        b, {"sum": 0, "contributions": []})
                    cell["sum"] += v
                    cell["contributions"].append({"speaker": s, "value": v})
        for a in self.seats:
            spec = self.seat_specs[a]
            for b, cell in lines[a].items():
                cell["audible"] = abs(cell["sum"]) >= spec.threshold
                cell["contributions"].sort(key=lambda c: c["speaker"])
            lines[a] = {b: lines[a][b] for b in sorted(lines[a])}
        return lines

    def evaluate(self, config: Mapping[str, SpeakerOption]) -> "HallEvaluation":
        lines = self.seat_lines(config)
        seats: dict[str, SeatEval] = {}
        total_cost = 0
        for s in self.speakers:
            total_cost += config[s].cost
        for a in self.seats:
            spec = self.seat_specs[a]
            audible = [(b, cell["sum"]) for b, cell in lines[a].items()
                       if cell["audible"]]
            first = audible[0][0] if audible else None
            heard = first is not None and first <= spec.latest_onset
            ghost = (first is not None and
                     any(b - first > spec.allowed_gap for b, _ in audible))
            reasons = []
            if first is None:
                reasons.append("无达门限声响")
            elif not heard:
                reasons.append(f"第一声在拍 {first}，晚于最晚 {spec.latest_onset}")
            if ghost:
                late = [b for b, _ in audible if b - first > spec.allowed_gap]
                reasons.append(f"拍 {first} 之后仍于 {late} 达门限（拍差超过 "
                               f"{spec.allowed_gap}）")
            seats[a] = SeatEval(
                seat=a, audible=audible, first_beat=first,
                heard=heard, ghost=ghost, valid=heard and not ghost,
                reasons=reasons)
        return HallEvaluation(total_cost=total_cost, config=dict(config),
                              seats=seats, lines=lines,
                              valid=all(e.valid for e in seats.values()))

    # ---- 最低费用搜索 ---- #
    def solve(self) -> "HallSolution":
        """遍历全部 (状态, 延迟) 组合，取最低总费用；并列时按给定顺序取首个。"""
        order = [self.options[s] for s in self.speakers]
        best: HallEvaluation | None = None
        n_optimal = 0
        n_total = 1
        for opts in order:
            n_total *= len(opts)
        for combo in product(*order):
            config = dict(zip(self.speakers, combo))
            ev = self.evaluate(config)
            if not ev.valid:
                continue
            if best is None or ev.total_cost < best.total_cost:
                best = ev
                n_optimal = 1
            elif ev.total_cost == best.total_cost:
                n_optimal += 1
        if best is None:
            raise HallInfeasible(
                f"在 {n_total} 种配置中没有让全部座位听到且无重影的方案")
        return HallSolution.from_evaluation(best, n_optimal, n_total)


def _validate_hall_shape(speakers, seats, options, seat_specs, responses) -> None:
    if not speakers or len(speakers) > MAX_SPEAKERS:
        raise ValueError(f"喇叭数须在 1..{MAX_SPEAKERS}")
    if not seats or len(seats) > MAX_SEATS:
        raise ValueError(f"座位数须在 1..{MAX_SEATS}")
    if len(set(speakers)) != len(speakers):
        raise ValueError("喇叭名重复")
    if len(set(seats)) != len(seats):
        raise ValueError("座位名重复")
    for s in speakers:
        opts = options.get(s, ())
        if not opts:
            raise ValueError(f"喇叭 {s} 没有任何可选配置")
        if len({(o.state, o.delay) for o in opts}) != len(opts):
            raise ValueError(f"喇叭 {s} 存在重复的 (状态, 延迟)")
        delays = {o.delay for o in opts}
        states = {o.state for o in opts}
        if len(delays) > MAX_DELAYS:
            raise ValueError(f"喇叭 {s} 的延迟数超过 {MAX_DELAYS}")
        if len(states) > MAX_STATES:
            raise ValueError(f"喇叭 {s} 的板况数超过 {MAX_STATES}")
        for o in opts:
            for a in seats:
                if (s, o.state, a) not in responses:
                    raise ValueError(
                        f"缺少响应 ({s},{o.state},{a})；开板/闭板不互借")
    for a in seats:
        if a not in seat_specs:
            raise ValueError(f"座位 {a} 缺少门限/时窗参数")


class HallInfeasible(RuntimeError):
    pass


@dataclass(frozen=True)
class SeatEval:
    seat: str
    audible: list[tuple[int, int]]
    first_beat: int | None
    heard: bool
    ghost: bool
    valid: bool
    reasons: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "seat": self.seat,
            "audible_sequence": [{"beat": b, "value": v} for b, v in self.audible],
            "first_beat": self.first_beat,
            "heard": self.heard,
            "ghost": self.ghost,
            "valid": self.valid,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class HallEvaluation:
    total_cost: int
    config: dict[str, SpeakerOption]
    seats: dict[str, SeatEval]
    lines: dict[str, dict[int, dict[str, Any]]]
    valid: bool


@dataclass(frozen=True)
class HallSolution:
    total_cost: int
    config: dict[str, SpeakerOption]
    seats: dict[str, SeatEval]
    lines: dict[str, dict[int, dict[str, Any]]]
    n_optimal: int
    n_configs: int

    @classmethod
    def from_evaluation(cls, ev: HallEvaluation, n_optimal: int,
                        n_configs: int) -> "HallSolution":
        return cls(ev.total_cost, ev.config, ev.seats, ev.lines,
                   n_optimal, n_configs)

    # ---- 人类可读交付物 ---- #
    def handover_line(self) -> dict[str, list[dict[str, Any]]]:
        """交接线：每座逐拍的“先求和”明细，标出是否达门限及各喇叭贡献。"""
        out: dict[str, list[dict[str, Any]]] = {}
        for a, line in self.lines.items():
            out[a] = [
                {"beat": b, "sum": cell["sum"],
                 "audible": cell["audible"],
                 "contributions": cell["contributions"]}
                for b, cell in line.items()
            ]
        return out

    def panel_board(self) -> list[dict[str, Any]]:
        """屏板：每只喇叭选用的板况、延迟与安装费。"""
        board = []
        for s, opt in self.config.items():
            board.append({"speaker": s, "state": opt.state,
                          "delay": opt.delay, "cost": opt.cost})
        return board

    def to_json(self) -> dict[str, Any]:
        return {
            "total_cost": self.total_cost,
            "n_optimal_configs": self.n_optimal,
            "n_configs_searched": self.n_configs,
            "panel_board": self.panel_board(),
            "handover_line": self.handover_line(),
            "seats": {a: e.to_json() for a, e in self.seats.items()},
            "seat_sequences": {
                a: [{"beat": b, "value": v} for b, v in e.audible]
                for a, e in self.seats.items()
            },
        }

    def summary_text(self) -> str:
        rows = ["屏板（各喇叭工作状态 / 电延迟 / 安装费）:"]
        for s, opt in self.config.items():
            rows.append(f"  {s}: {opt.state:<5s} delay={opt.delay}  cost={opt.cost}")
        rows.append(f"  合计安装费 = {self.total_cost}"
                    f"（并列最优 {self.n_optimal} / 搜索 {self.n_configs}）")
        rows.append("交接线（每座逐拍：先求和，再判门限）:")
        for a, line in self.lines.items():
            parts = []
            for b, cell in line.items():
                mark = "*" if cell["audible"] else " "
                contrib = "+".join(f"{c['speaker']}:{c['value']:+d}"
                                   for c in cell["contributions"])
                parts.append(f"[{b}]{cell['sum']:+d}{mark}({contrib})")
            rows.append(f"  {a}: " + "  ".join(parts))
        rows.append("各座实响序列:")
        for a, e in self.seats.items():
            seq = " ".join(f"{b}:{v}" for b, v in e.audible) or "（无）"
            rows.append(f"  {a}: {seq}  first={e.first_beat}  "
                        f"heard={e.heard} ghost={e.ghost}")
        return "\n".join(rows)


# ====================================================================== #
# 4. 从校准仓装配全厅                                                    #
# ====================================================================== #

def build_hall(store: CalibrationStore,
               speakers: Sequence[str],
               seats: Sequence[str],
               states: Mapping[str, Sequence[str]],
               delays: Mapping[str, Sequence[int]],
               state_cost: Mapping[str, Mapping[str, int]],
               delay_cost: Mapping[str, Mapping[int, int]],
               seat_specs: Mapping[str, SeatSpec]) -> tuple[HallProblem, dict[str, DeconvResult]]:
    """对每只喇叭的每个允许板况、每个座位调用单路还原，装配 HallProblem。

    记录只从同板况分柜取（``store.recover``），绝不回退到另一板况。
    返回 (problem, 还原明细)。任一路无法用整数响应还原则抛 ``DeconvFailure``。
    """
    speakers = tuple(speakers)
    seats = tuple(seats)
    responses: dict[tuple[str, str, str], tuple[int, ...]] = {}
    recovered: dict[str, DeconvResult] = {}
    failures: list[tuple[str, DeconvResult]] = []

    for s in speakers:
        for st in states[s]:
            for a in seats:
                res = store.recover(s, a, st)
                recovered[f"{s}/{st}/{a}"] = res
                if not res.ok:
                    failures.append((f"{s}/{st}/{a}", res))
                    continue
                responses[(s, st, a)] = res.taps  # type: ignore[assignment]
    if failures:
        path, res = failures[0]
        raise DeconvFailure(
            f"路径 {path} 无法用规定长度的整数响应逐样本还原："
            f"首处不符@{res.first_mismatch} ({res.kind}) {res.detail}",
            path=path, result=res)

    options: dict[str, tuple[SpeakerOption, ...]] = {}
    for s in speakers:
        opts = []
        for st in states[s]:
            for d in delays[s]:
                opts.append(SpeakerOption(st, int(d),
                                          int(state_cost.get(s, {}).get(st, 0))
                                          + int(delay_cost.get(s, {}).get(int(d), 0))))
        options[s] = tuple(opts)

    problem = HallProblem(
        speakers=speakers, seats=seats, responses=responses,
        options=options,
        seat_specs={a: seat_specs[a] for a in seats},
    )
    return problem, recovered


class DeconvFailure(RuntimeError):
    def __init__(self, message: str, path: str, result: DeconvResult):
        super().__init__(message)
        self.path = path
        self.result = result


# ====================================================================== #
# 5. 真实 WAV 试听                                                       #
# ====================================================================== #

def render_seat_audition(beat_line: Mapping[int, dict[str, Any]],
                         beat_stride: int = 800, tone_hz: float = 440.0,
                         tone_len: int = 600, amp_scale: float = 400.0,
                         rate: int = OUTPUT_RATE) -> np.ndarray:
    """把一座的“逐拍有符号和”渲染成可听的短音串，返回 int16 波形。

    每拍对应一段加窗正弦短音，位置在 ``beat*beat_stride``，振幅由该拍有符号和
    决定（正负号即相位极性，相消的拍子是静音）。``amp_scale`` 取小值并统一
    归一化，保证绝对值远离 16 位饱和。
    """
    if not beat_line:
        return np.zeros(0, dtype=np.int16)
    beats = list(beat_line.keys())
    hi = max(beats)
    total = hi * beat_stride + tone_len + 1
    wave = np.zeros(total, dtype=np.float64)
    n = np.arange(tone_len, dtype=np.float64)
    env = np.sin(np.pi * (n + 0.5) / tone_len) ** 2  # 汉宁窗，避免爆音
    carrier = np.sin(2 * np.pi * tone_hz * n / rate)
    burst = env * carrier
    for b in beats:
        v = beat_line[b]["sum"]
        start = b * beat_stride
        wave[start:start + tone_len] += (v * amp_scale) * burst
    peak = float(np.max(np.abs(wave))) if len(wave) else 0.0
    if peak > 16000.0:                 # 统一回缩，远低于 16 位饱和（32767）
        wave *= 16000.0 / peak
    return np.rint(wave).astype(np.int16)


def export_audition_wavs(solution: HallSolution, out_dir: str,
                         beat_stride: int = 800,
                         mixed_name: str = "audition.wav") -> dict[str, str]:
    """导出每座试听 WAV + 一份各座混合试听 WAV；返回文件路径表。"""
    import os
    os.makedirs(out_dir, exist_ok=True)
    paths: dict[str, str] = {}
    per_seat: dict[str, np.ndarray] = {}
    for a, line in solution.lines.items():
        wav = render_seat_audition(line, beat_stride=beat_stride)
        path = os.path.join(out_dir, f"seat_{a}.wav")
        write_wav(path, wav, OUTPUT_RATE)
        paths[a] = path
        per_seat[a] = wav.astype(np.float64)
    if per_seat:
        n = max(len(w) for w in per_seat.values())
        mix = np.zeros(n, dtype=np.float64)
        for w in per_seat.values():
            mix[:len(w)] += w
        peak = float(np.max(np.abs(mix))) if n else 0.0
        if peak > 16000.0:
            mix *= 16000.0 / peak
        mix_path = os.path.join(out_dir, mixed_name)
        write_wav(mix_path, np.rint(mix).astype(np.int16), OUTPUT_RATE)
        paths["mixed"] = mix_path
    return paths


__all__ = [
    "BARE", "PANEL", "STATES", "MAX_SPEAKERS", "MAX_SEATS", "MAX_DELAYS",
    "MAX_STATES",
    "convolve_int", "deconvolve_integer", "DeconvResult",
    "CalibrationRecord", "CalibrationStore", "PathKey", "PanelStateError",
    "SpeakerOption", "SeatSpec", "HallProblem", "HallEvaluation", "HallSolution",
    "SeatEval", "HallInfeasible", "DeconvFailure", "build_hall",
    "render_seat_audition", "export_audition_wavs",
]
