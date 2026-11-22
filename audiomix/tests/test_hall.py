"""试听厅校准测试：整数反卷积、板况隔离、全厅最低费用、相消语义、试听 WAV。

覆盖题面全部关键点：
- 真实激励 [1,1] + 有重叠尾部的实录 [2,3,1] -> 响应 [2,1]；
- 首个样本非零，长度 H 的完整整数卷积（含末尾零）；
- 截断/舍入/饱和/超长尾巴都不能被解释成“另一次反射”，返回首处不符；
- 同拍正负响应先求和相消，不能当成两声；
- 主要两座场景：最低费 2（L 延迟两拍）；换实测（R->X 改到拍 0）则 0 费合格；
  保持原实测只改 X 最晚起音需 3 费装板；
- 开板/闭板记录不可互借；单查一路 + 另一真实试音片交叉验证；
- 小样绝对值远离 16 位饱和；导出一份真实 WAV 试听。
"""
from __future__ import annotations

import json
import os
import shutil

import numpy as np

from audiomix.assets import AssetRegistry, sequence, read_wav
from audiomix.hall import (
    BARE, PANEL, CalibrationStore, DeconvFailure, HallInfeasible,
    PanelStateError, SeatSpec, SpeakerOption, build_hall, convolve_int,
    deconvolve_integer, export_audition_wavs,
)

# 主要场景用的激励：4 个非零样本，首样本非零；H=3 时完整卷积 6 拍。
EXCITATION = [1, 1, 1, 1]
H = 3


def _taps_to_observation(x, taps):
    """按“规定长度 H 的整数响应”生产观测（完整卷积含末尾零，长度 len(x)+H-1）。"""
    return convolve_int(x, list(taps))


def _register_obs(reg, x, taps, note=""):
    y = _taps_to_observation(x, taps)
    return reg.register(sequence(y), 16000, note=note).asset_id


# ------------------------------------------------------------------ #
# 1. 反卷积单元测试                                                  #
# ------------------------------------------------------------------ #

def test_deconv_real_example():
    """真实激励 [1,1] 与有重叠尾部的实录 [2,3,1] -> 响应 [2,1]。"""
    r = deconvolve_integer([1, 1], [2, 3, 1], max_len=2)
    assert r.ok and r.taps == (2, 1), r
    # 完整卷积含末尾零（H 长度）也要能处理：[2,1] 已是满长。
    print("[OK] 反卷积：[1,1] * [2,1] = [2,3,1]，还原响应 [2,1]")


def test_deconv_integer_and_trailing_zero():
    """含末尾零的全长响应：h=[3,0,0] -> 观测 [3,3,3,3,0,0]，原样还原。"""
    r = deconvolve_integer(EXCITATION, [3, 3, 3, 3, 0, 0], max_len=H)
    assert r.ok and r.taps == (3, 0, 0), r
    print("[OK] 反卷积保留末尾零，长度恰好 H=3")


def test_deconv_rejects_non_integer():
    """x0=2，分子为奇数 -> 不能整除：第 0 拍即首处不符。"""
    r = deconvolve_integer([2, 1], [1, 1, 1], max_len=2)
    assert not r.ok and r.kind == "non_integer" and r.first_mismatch == 0, r
    print("[OK] 非整数反卷积被拒：首处不符@0")


def test_deconv_rejects_truncation():
    """观测短于完整卷积长度：拒绝把截断当作反射。"""
    r = deconvolve_integer([1, 1], [2, 3, 1], max_len=3)  # 完整长度应为 4
    assert not r.ok and r.kind == "truncated" and r.first_mismatch == 3, r
    print("[OK] 观测截断被拒：首处不符@3")


def test_deconv_rejects_saturation_like_tail():
    """观测尾部与整数卷积不符（如被饱和/舍入改动）：第 2 拍首处不符。"""
    # h=[2,1] 应得 [2,3,1]；把末拍改成 2，不能被当成第三拍反射。
    r = deconvolve_integer([1, 1], [2, 3, 2], max_len=2)
    assert not r.ok and r.kind == "tail_mismatch" and r.first_mismatch == 2, r
    print("[OK] 尾部不符被拒：首处不符@2")


def test_deconv_rejects_extra_tail():
    """完整卷积后仍出现非零样本：响应比规定长度长，拒绝。"""
    r = deconvolve_integer([1, 1], [2, 2, 0, 5], max_len=1)  # h=[2]
    assert not r.ok and r.kind == "extra_tail" and r.first_mismatch == 3, r
    print("[OK] 超长尾被拒：首处不符@3")


def test_deconv_requires_nonzero_first_sample():
    r = deconvolve_integer([0, 1], [0, 2], max_len=2)
    assert not r.ok and r.kind == "bad_excitation", r
    print("[OK] 激励首样本为 0 被拒")


# ------------------------------------------------------------------ #
# 2. 两座主场景的完整装配                                            #
# ------------------------------------------------------------------ #

# 裸机响应（拍 0..H-1）；L->X 在拍0=3；R->X 在拍2=3；R->Y 在拍0=3。
BARE_RESP = {
    ("L", "X"): [3, 0, 0],
    ("L", "Y"): [0, 0, 0],
    ("R", "X"): [0, 0, 3],
    ("R", "Y"): [3, 0, 0],
}
# 装板：只把 R->X 那一路变全零，其余座位响应不变。
PANEL_RESP = {
    ("R", "X"): [0, 0, 0],
    ("R", "Y"): [3, 0, 0],
}


def _build_store(base, resp_bare=BARE_RESP, resp_panel=PANEL_RESP,
                 panel_for=("R",), bare_speakers=("L", "R")):
    shutil.rmtree(base, ignore_errors=True)
    reg = AssetRegistry(os.path.join(base, "registry"))
    store = CalibrationStore(reg)
    x_asset = reg.register(sequence(EXCITATION), 16000, note="excitation").asset_id
    cache = {}

    def obs_for(taps):
        key = tuple(taps)
        if key not in cache:
            cache[key] = _register_obs(reg, EXCITATION, taps,
                                       note=f"obs-{key}")
        return cache[key]

    for s in bare_speakers:
        for seat in ("X", "Y"):
            taps = resp_bare.get((s, seat))
            if taps is None:
                continue
            store.add_record(s, seat, BARE, x_asset, obs_for(taps), H,
                             note="裸机实录")
    for s in panel_for:
        for seat in ("X", "Y"):
            taps = resp_panel.get((s, seat))
            if taps is None:
                continue
            store.add_record(s, seat, PANEL, x_asset, obs_for(taps), H,
                             note="装板实录")
    return reg, store


def _scenario_params():
    speakers = ("L", "R")
    seats = ("X", "Y")
    states = {"L": (BARE,), "R": (BARE, PANEL)}
    delays = {"L": (0, 2), "R": (0,)}
    state_cost = {"L": {}, "R": {BARE: 0, PANEL: 3}}
    delay_cost = {"L": {0: 0, 2: 2}, "R": {0: 0}}
    seat_specs = {
        "X": SeatSpec(threshold=2, latest_onset=2, allowed_gap=1),
        "Y": SeatSpec(threshold=2, latest_onset=2, allowed_gap=1),
    }
    return speakers, seats, states, delays, state_cost, delay_cost, seat_specs


def test_main_scenario_min_cost_2():
    base = "/tmp/audiomix_hall_main"
    reg, store = _build_store(base)
    speakers, seats, states, delays, sc, dc, specs = _scenario_params()

    # 单查一路：R->X 裸机响应还原为拍2=3。
    rr = store.recover("R", "X", BARE)
    assert rr.ok and rr.taps == (0, 0, 3), rr

    problem, recovered = build_hall(store, speakers, seats, states, delays,
                                    sc, dc, specs)

    # 保持原实测（0 费）：X 在拍0、拍2 各听一次 -> 拍差 2 > 1 -> 重影，无效。
    ev0 = problem.evaluate({s: SpeakerOption(states[s][0], 0, 0)
                            for s in speakers})
    assert not ev0.valid and ev0.seats["X"].ghost, ev0.seats["X"].reasons

    sol = problem.solve()
    assert sol.total_cost == 2, sol.summary_text()
    assert sol.config["L"] == SpeakerOption(BARE, 2, 2)
    assert sol.config["R"] == SpeakerOption(BARE, 0, 0)
    # X 直到拍 2 才响一次 6；Y 在 0 响 3。
    assert sol.seats["X"].audible == [(2, 6)], sol.seats["X"].audible
    assert sol.seats["Y"].audible == [(0, 3)], sol.seats["Y"].audible
    assert all(e.valid for e in sol.seats.values())
    print("[OK] 主场景最低费 = 2：", sol.summary_text().splitlines()[0])
    print("     X 实响", sol.seats["X"].audible, " Y 实响", sol.seats["Y"].audible)

    # 交接线：X 的拍 2 由 L(+3) 与 R(+3) 相加得 6。
    ho = sol.handover_line()
    x_beat2 = next(c for c in ho["X"] if c["beat"] == 2)
    assert x_beat2["sum"] == 6 and x_beat2["audible"]
    assert {c["speaker"] for c in x_beat2["contributions"]} == {"L", "R"}

    # 真实 WAV 试听：每座一份 + 混合，绝对值远离饱和。
    paths = export_audition_wavs(sol, os.path.join(base, "out"))
    assert "mixed" in paths and paths["X"].endswith("seat_X.wav")
    for p in paths.values():
        pcm, rate = read_wav(p)
        assert rate == 16000 and pcm.dtype == np.int16 and len(pcm) > 0
        assert int(np.max(np.abs(pcm))) <= 20000, (p, np.max(np.abs(pcm)))
    mixed, _ = read_wav(paths["mixed"])
    assert np.count_nonzero(mixed) > 0
    print("[OK] 真实 WAV 试听已导出（每座 + 混合），峰值远离 16 位饱和")
    return base


def test_alternative_new_measurement_cost_0():
    """只让新的 R->X 实测脉冲改到拍 0：不装板不延迟即合格（0 费）。"""
    resp_bare = dict(BARE_RESP)
    resp_bare[("R", "X")] = [3, 0, 0]   # 新的实测：脉冲到拍 0
    base = "/tmp/audiomix_hall_newmeas"
    reg, store = _build_store(base, resp_bare=resp_bare)
    speakers, seats, states, delays, sc, dc, specs = _scenario_params()
    problem, _ = build_hall(store, speakers, seats, states, delays, sc, dc, specs)
    sol = problem.solve()
    assert sol.total_cost == 0, sol.summary_text()
    assert sol.config["L"] == SpeakerOption(BARE, 0, 0)
    assert sol.config["R"] == SpeakerOption(BARE, 0, 0)
    assert sol.seats["X"].audible == [(0, 6)], sol.seats["X"].audible
    print("[OK] 换实测（R->X 到拍0）：0 费即合格，X 在拍0 响 6")


def test_alternative_panel_only_cost_3():
    """保持原实测，只把 X 最晚起音改到 0：需要花 3 装板。

    这样 L 不能延迟（否则 X 会多出晚拍），只能靠 R 装板把拍2 的 R->X 变零，
    X 仅剩拍0 的 L(+3)。最便宜且合法的方案 = R 装板（3 费）。
    """
    base = "/tmp/audiomix_hall_panel"
    reg, store = _build_store(base)
    speakers, seats, states, delays, sc, dc, _ = _scenario_params()
    diff_specs = {
        "X": SeatSpec(threshold=2, latest_onset=0, allowed_gap=1),
        "Y": SeatSpec(threshold=2, latest_onset=2, allowed_gap=1),
    }
    problem, _ = build_hall(store, speakers, seats, states, delays, sc, dc,
                            diff_specs)
    sol = problem.solve()
    assert sol.total_cost == 3, sol.summary_text()
    assert sol.config["R"] == SpeakerOption(PANEL, 0, 3)
    assert sol.config["L"] == SpeakerOption(BARE, 0, 0)
    assert sol.seats["X"].audible == [(0, 3)], sol.seats["X"].audible
    print("[OK] 改 X 最晚起音到 0：最低费 3（R 装板），X 在拍0 响 3")


# ------------------------------------------------------------------ #
# 3. 相消 / 板况隔离 / 交叉验证 / 不可行                             #
# ------------------------------------------------------------------ #

def _hole_scenario(resp, kmax=3):
    """构造任意响应下的小场景便于验证规则。"""
    speakers = tuple(sorted({s for (s, _) in resp}))
    seats = tuple(sorted({a for (_, a) in resp}))
    states = {s: (BARE,) for s in speakers}
    delays = {s: (0,) for s in speakers}
    zero_cost = {s: {} for s in speakers}
    zero_delay = {s: {0: 0} for s in speakers}
    specs = {a: SeatSpec(2, kmax, 1) for a in seats}
    return speakers, seats, states, delays, zero_cost, zero_delay, specs


def test_same_beat_opposite_cancel():
    """同拍 +3 与 -3 先求和为 0：不算两声，也不达门限。"""
    base = "/tmp/audiomix_hall_cancel"
    shutil.rmtree(base, ignore_errors=True)
    reg = AssetRegistry(os.path.join(base, "registry"))
    store = CalibrationStore(reg)
    x_asset = reg.register(sequence(EXCITATION), 16000).asset_id
    # A->X 拍0=+3；B->X 拍0=-3；A->Y、B->Y 全零（保证每路有记录）。
    resp = {
        ("A", "X"): [3, 0, 0], ("B", "X"): [-3, 0, 0],
        ("A", "Y"): [0, 0, 0], ("B", "Y"): [0, 0, 0],
    }
    for (s, a), taps in resp.items():
        obs = _register_obs(reg, EXCITATION, taps)
        store.add_record(s, a, BARE, x_asset, obs, H)
    speakers, seats, states, delays, sc, dc, specs = _hole_scenario(resp, kmax=2)
    specs = {"X": SeatSpec(2, 2, 1), "Y": SeatSpec(2, 2, 1)}
    problem, _ = build_hall(store, speakers, seats, states, delays, sc, dc, specs)
    sol = problem.evaluate({s: SpeakerOption(BARE, 0, 0) for s in speakers})
    # X 同拍 3-3=0 -> 不达门限；Y 全零 -> 两座都听不到，整体无效。
    assert sol.seats["X"].audible == []
    assert not sol.seats["X"].heard
    line = sol.lines["X"][0]
    assert line["sum"] == 0 and not line["audible"]
    assert len(line["contributions"]) == 2  # 两条贡献都记录，但不算两声
    print("[OK] 同拍相反响应先求和为 0：不算两声、不达门限")


def test_panel_records_not_borrowed():
    """只登记了裸机记录时，请求装板必须报错，不能互借。"""
    reg, store = _build_store("/tmp/audiomix_hall_borrow",
                              panel_for=())  # 不建装板记录
    try:
        store.recover("R", "X", PANEL)
        raise AssertionError("缺装板记录应报错")
    except PanelStateError as e:
        assert "不可互借" in str(e)
    print("[OK] 开板/闭板记录隔离：缺装板记录即拒绝")


def test_cross_validate_with_another_excitation():
    """同一路另一真实试音片（不同激励/实录）应还原出同一响应。"""
    base = "/tmp/audiomix_hall_xval"
    shutil.rmtree(base, ignore_errors=True)
    reg = AssetRegistry(os.path.join(base, "registry"))
    store = CalibrationStore(reg)
    taps = [0, 0, 3]
    x1 = reg.register(sequence([1, 1, 1, 1]), 16000).asset_id
    x2 = reg.register(sequence([2, -1, 3, 1]), 16000).asset_id  # 另一真实试音片
    y1 = reg.register(sequence(convolve_int([1, 1, 1, 1], taps)), 16000).asset_id
    y2 = reg.register(sequence(convolve_int([2, -1, 3, 1], taps)), 16000).asset_id
    store.add_record("R", "X", BARE, x1, y1, H, note="试音片1")
    store.add_record("R", "X", BARE, x2, y2, H, note="试音片2")
    out = store.validate_path("R", "X", BARE)
    assert out["ok"] and out["taps"] == taps and out["records"] == 2, out
    # 若第二片被篡改成不一致，交叉验证应失败并给首处不符。
    y2_bad = reg.register(sequence(convolve_int([2, -1, 3, 1], [0, 0, 4])),
                          16000).asset_id
    store2 = CalibrationStore(reg)
    store2.add_record("R", "X", BARE, x1, y1, H)
    store2.add_record("R", "X", BARE, x2, y2_bad, H)
    bad = store2.validate_path("R", "X", BARE)
    assert not bad["ok"] and bad["kind"] == "inconsistent", bad
    print("[OK] 另一真实试音片交叉验证：一致通过，篡改则给首处不符",
          bad["first_mismatch"])


def test_infeasible_and_deconv_failure():
    """无任何合法配置 -> HallInfeasible；某路反卷积失败 -> DeconvFailure。"""
    base = "/tmp/audiomix_hall_bad"
    shutil.rmtree(base, ignore_errors=True)
    reg = AssetRegistry(os.path.join(base, "registry"))
    store = CalibrationStore(reg)
    x_asset = reg.register(sequence(EXCITATION), 16000).asset_id
    # 让 R->X 观测非整数可还原（截断到 3 拍，而 H=3 需 6 拍）
    obs_bad = reg.register(sequence([3, 3, 3]), 16000).asset_id
    obs_ok = _register_obs(reg, EXCITATION, [3, 0, 0])
    store.add_record("L", "X", BARE, x_asset, obs_ok, H)
    store.add_record("R", "X", BARE, x_asset, obs_bad, H)
    resp = {("L", "X"): [3, 0, 0], ("R", "X"): [3, 0, 0]}
    speakers = ("L", "R"); seats = ("X",)
    states = {s: (BARE,) for s in speakers}
    delays = {s: (0,) for s in speakers}
    sc = {s: {} for s in speakers}; dc = {s: {0: 0} for s in speakers}
    specs = {"X": SeatSpec(2, 2, 1)}
    try:
        build_hall(store, speakers, seats, states, delays, sc, dc, specs)
        raise AssertionError("应抛出 DeconvFailure")
    except DeconvFailure as e:
        assert e.result.first_mismatch == 3
    print("[OK] 某路反卷积失败 -> DeconvFailure（首处不符@3）")

    # 反卷积全部正常但无人可听 -> 不可行
    reg2 = AssetRegistry(os.path.join(base, "reg2"))
    store2 = CalibrationStore(reg2)
    xa = reg2.register(sequence(EXCITATION), 16000).asset_id
    z = reg2.register(sequence([0, 0, 0, 0, 0, 0]), 16000).asset_id
    store2.add_record("L", "X", BARE, xa, z, H)
    try:
        build_hall(store2, ("L",), ("X",), {"L": (BARE,)}, {"L": (0,)},
                   {"L": {}}, {"L": {0: 0}}, specs)[0].solve()
        raise AssertionError("无人可听时应不可行")
    except HallInfeasible:
        pass
    print("[OK] 无合法配置 -> HallInfeasible")


def run_all():
    test_deconv_real_example()
    test_deconv_integer_and_trailing_zero()
    test_deconv_rejects_non_integer()
    test_deconv_rejects_truncation()
    test_deconv_rejects_saturation_like_tail()
    test_deconv_rejects_extra_tail()
    test_deconv_requires_nonzero_first_sample()
    test_main_scenario_min_cost_2()
    test_alternative_new_measurement_cost_0()
    test_alternative_panel_only_cost_3()
    test_same_beat_opposite_cancel()
    test_panel_records_not_borrowed()
    test_cross_validate_with_another_excitation()
    test_infeasible_and_deconv_failure()
    print("\n试听厅校准测试全部通过")


if __name__ == "__main__":
    run_all()
