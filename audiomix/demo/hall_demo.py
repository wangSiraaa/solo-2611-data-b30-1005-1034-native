"""试听厅示例：两排试听席（X/Y）、两只喇叭（L/R）的端到端校准。

流程严格按题面：
1. 先把具名原剪辑（激励）经 audiomix 时间轴发布并用数据仓渲染成短 PCM，
   确认首个样本非零；把该渲染结果登记为“激励素材”。
2. 每份校验录音 = 某 (喇叭, 座位, 板况) 的实录。这里用整数卷积
   ``y = x * h``（完整线性卷积，含末尾零）生成“另一份实录整段 PCM”，
   并按板况分柜登记——开板/闭板两套记录互不借用。
3. 单查一路还原；再用另一真实试音片交叉验证同一路。
4. 装配全厅，求最低安装费；打印屏板、交接线、各座实响序列。
5. 导出真实 WAV 试听（每座 + 混合）。

结果与三条场景结论：
- 保持原实测：最低费 2（L 延迟两拍），X 直到拍 2 响一次 6，Y 在 0 响 3。
- 若新的 R->X 实测脉冲改到拍 0（不装板不延迟）：0 费即合格。
- 若保持原实测、把 X 最晚起音改到 0：需花 3 装板（R 装板）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys

from fractions import Fraction as F

from audiomix.archive import ArchiveStore
from audiomix.assets import AssetRegistry, read_wav, sequence
from audiomix.hall import (
    BARE, PANEL, CalibrationStore, SeatSpec, build_hall, convolve_int,
    export_audition_wavs,
)
from audiomix.timeline import TimelineEditor

BASE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/audiomix_hall_demo"
H = 3                       # 已知响应长度上限
EXC_SEQ = [1, 1, 1, 1]      # 具名原剪辑的素材；首样本非零


# 裸机响应（拍 0..H-1）。L->X 拍0=3；R->X 拍2=3；R->Y 拍0=3；L->Y 全零。
BARE_RESP = {
    ("L", "X"): [3, 0, 0],
    ("L", "Y"): [0, 0, 0],
    ("R", "X"): [0, 0, 3],
    ("R", "Y"): [3, 0, 0],
}
# 装板：只把 R->X 那一路变全零，其余座位不变。
PANEL_RESP = {
    ("R", "X"): [0, 0, 0],
    ("R", "Y"): [3, 0, 0],
}


def render_excitation(root: str, reg: AssetRegistry) -> str:
    """把具名原剪辑发布并渲染成短 PCM，登记后返回激励素材 asset_id。"""
    src = reg.register(sequence(EXC_SEQ), 16000, note="具名原剪辑源素材")
    ed = TimelineEditor(os.path.join(root, "registry"), reg, name="hall")
    ed.insert({"clip_id": "stim", "asset_id": src.asset_id, "a": 0,
               "b": len(EXC_SEQ), "start": "0"})
    product = ed.publish()
    prod_path = os.path.join(root, "product_stim.json")
    ed.save_product(product, prod_path)
    pcm_by_asset = {a["asset_id"]: reg.load_pcm(a["asset_id"])
                    for a in product["assets"]}
    store = ArchiveStore(os.path.join(root, "archive"))
    store.render(product, F(len(EXC_SEQ), 16000), pcm_by_asset,
                 render_id="stim")
    rendered = store.full_pcm("stim")
    store.close()
    assert len(rendered) == len(EXC_SEQ) and int(rendered[0]) != 0, \
        "渲染激励必须等长且首样本非零"
    assert rendered.tolist() == EXC_SEQ, rendered.tolist()
    exc = reg.register(rendered, 16000, note="已渲染激励（来自 stim 剪辑）")
    print(f"[OK] 具名原剪辑渲染为激励 PCM {rendered.tolist()}，asset_id={exc.asset_id[:12]}…")
    return exc.asset_id


def register_recordings(reg: AssetRegistry, exc_asset: str):
    """登记每个 (喇叭, 座位, 板况) 的实测录音；板况分柜。"""
    x = reg.load_pcm(exc_asset).tolist()
    cache: dict[tuple[int, ...], str] = {}

    def obs(taps):
        key = tuple(taps)
        if key not in cache:
            y = convolve_int(x, list(taps))       # 完整线性卷积，含末尾零
            cache[key] = reg.register(
                sequence(y), 16000, note=f"实录 L{taps}").asset_id
        return cache[key]

    store = CalibrationStore(reg)
    for (s, a), taps in BARE_RESP.items():
        store.add_record(s, a, BARE, exc_asset, obs(taps), H, note="裸机实录")
    for (s, a), taps in PANEL_RESP.items():
        store.add_record(s, a, PANEL, exc_asset, obs(taps), H, note="装板实录")
    return store, obs


def scenario_params():
    return dict(
        speakers=("L", "R"), seats=("X", "Y"),
        states={"L": (BARE,), "R": (BARE, PANEL)},
        delays={"L": (0, 2), "R": (0,)},
        state_cost={"L": {}, "R": {BARE: 0, PANEL: 3}},
        delay_cost={"L": {0: 0, 2: 2}, "R": {0: 0}},
    )


def main() -> None:
    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE, exist_ok=True)
    reg = AssetRegistry(os.path.join(BASE, "registry"))
    exc_asset = render_excitation(BASE, reg)
    store, obs = register_recordings(reg, exc_asset)

    # ---- 单查一路 + 另一真实试音片交叉验证 ---- #
    one = store.recover("R", "X", BARE)
    print(f"[OK] 单查 R->X(裸机) 响应 = {list(one.taps)}")
    seen = store.recover("R", "Y", BARE)
    assert seen.taps == (3, 0, 0)

    # 另一真实试音片：不同激励 + 相应实录，须还原同一 R->X(裸机) 响应。
    x2 = [2, -1, 3, 1]
    x2_asset = reg.register(sequence(x2), 16000, note="试音片2").asset_id
    y2 = convolve_int(x2, BARE_RESP[("R", "X")])
    y2_asset = reg.register(sequence(y2), 16000, note="试音片2 实录").asset_id
    store.add_record("R", "X", BARE, x2_asset, y2_asset, H, note="试音片2")
    cross = store.validate_path("R", "X", BARE)
    assert cross["ok"] and cross["taps"] == [0, 0, 3], cross
    print(f"[OK] 另一真实试音片交叉验证 R->X(裸机) = {cross['taps']}")

    # ---- 主场景：最低费 ---- #
    sp = scenario_params()
    specs = {"X": SeatSpec(2, 2, 1), "Y": SeatSpec(2, 2, 1)}
    problem, _ = build_hall(store, sp["speakers"], sp["seats"], sp["states"],
                            sp["delays"], sp["state_cost"], sp["delay_cost"], specs)
    sol = problem.solve()
    assert sol.total_cost == 2
    assert sol.config["L"].state == BARE and sol.config["L"].delay == 2
    assert sol.seats["X"].audible == [(2, 6)]
    assert sol.seats["Y"].audible == [(0, 3)]
    print("\n===== 主场景（保持原实测）=====")
    print(sol.summary_text())

    # 交付物：结果 JSON（含屏板/交接线/各座实响序列）+ 试听 WAV。
    out_dir = os.path.join(BASE, "out")
    result = sol.to_json()
    result["wavs"] = export_audition_wavs(sol, out_dir)
    with open(os.path.join(BASE, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print("\n[OK] 交付物：result.json（屏板/交接线/各座实响），试听 WAV：")
    for name, path in result["wavs"].items():
        pcm, rate = read_wav(path)
        print(f"     {name:<6s} {path}  {len(pcm)} 样本 @ {rate}Hz  "
              f"峰值 {int(abs(pcm).max())}")
        assert int(abs(pcm).max()) <= 20000, "试听须远离 16 位饱和"

    # ---- 两条对照结论 ---- #
    # (a) 新的 R->X 实测改到拍 0：0 费即合格。
    store_alt = CalibrationStore(reg)
    x = reg.load_pcm(exc_asset).tolist()
    alt_bare = dict(BARE_RESP)
    alt_bare[("R", "X")] = [3, 0, 0]
    for (s, a), taps in alt_bare.items():
        o = reg.register(sequence(convolve_int(x, taps)), 16000).asset_id
        store_alt.add_record(s, a, BARE, exc_asset, o, H)
    for (s, a), taps in PANEL_RESP.items():
        o = reg.register(sequence(convolve_int(x, taps)), 16000).asset_id
        store_alt.add_record(s, a, PANEL, exc_asset, o, H)
    p_alt, _ = build_hall(store_alt, sp["speakers"], sp["seats"], sp["states"],
                          sp["delays"], sp["state_cost"], sp["delay_cost"], specs)
    sol_alt = p_alt.solve()
    assert sol_alt.total_cost == 0 and sol_alt.seats["X"].audible == [(0, 6)]
    print(f"\n[OK] 对照(a) R->X 新实测到拍0：最低费 {sol_alt.total_cost}，"
          f"X 在拍0 响 {sol_alt.seats['X'].audible[0][1]}")

    # (b) 保持原实测、X 最晚起音=0：需 3 费装板。
    specs_b = {"X": SeatSpec(2, 0, 1), "Y": SeatSpec(2, 2, 1)}
    p_b, _ = build_hall(store, sp["speakers"], sp["seats"], sp["states"],
                        sp["delays"], sp["state_cost"], sp["delay_cost"], specs_b)
    sol_b = p_b.solve()
    assert sol_b.total_cost == 3 and sol_b.config["R"].state == PANEL
    assert sol_b.seats["X"].audible == [(0, 3)]
    print(f"[OK] 对照(b) X 最晚起音=0：最低费 {sol_b.total_cost}（R 装板），"
          f"X 在拍0 响 {sol_b.seats['X'].audible[0][1]}")

    print("\n试听厅示例全部通过")


if __name__ == "__main__":
    main()
