"""磁带馆借展剪接台案例：实物盘登记、逐拍资格核验、批量优化、签单入账。

覆盖题面情形：
1. 原盘仅一份 [2,4,6,8]：甲要原带 [0,2)（值5），乙要 [1,3)（值4，也接受翻录
   2 拍），额度 2 -> 甲真带、乙翻录 [4,6]；
2. 乙的具名引用源区间改成 [2,4) 并重发真实节目 -> 甲乙各拿真带，只在共同边界
   2 落一刀，价值 9、翻录 0；
3. 保持原节目、额度 1 -> 只能优先交甲，值 5；
4. 另办一单：同一实物上已签走的中段截断另一请求（仓库 PCM 还能读也不能重交）；
   已有断口/盘首尾不花刀；
5. 同一位置两个非零贡献刚好相消 -> 无原件资格；零增益轨不算参与制声；
   同一素材的别次出现不能顶替；
6. 同一录音的另一副本另有入藏号，两件实物可各自出件；
7. 重开库房、原单回读；
8. 批量上限与整笔拒绝；归档字节被改 -> 核验失败；
9. 翻录额度内的背包选择；
10. 多盘多请求综合优化（同一录音两副本同时出件）。
"""
import os
import shutil
import sys

import numpy as np

from audiomix.archive import ArchiveStore
from audiomix.assets import AssetRegistry, read_wav, sequence, write_wav
from audiomix.loan import (
    MAX_REELS, LoanOffice, LoanRequest, Vault, VaultError,
)
from audiomix.timeline import TimelineEditor

BASE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/audiomix_loan"


def _env(name):
    root = os.path.join(BASE, name)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    reg = AssetRegistry(os.path.join(root, "registry"))
    vault = Vault(os.path.join(root, "vault"))
    archive = ArchiveStore(os.path.join(root, "archive"))
    ed = TimelineEditor(os.path.join(root, "editor"), reg, name="main")
    return reg, vault, archive, ed


def _req(rid, exhibitor, clip, lo, hi, value, rr=False):
    return LoanRequest.from_json({
        "request_id": rid, "exhibitor": exhibitor, "clip_id": clip,
        "out_lo": lo, "out_hi": hi, "value": value, "accept_rerecord": rr})


def _amap(order):
    return {a["request_id"]: a for a in order["assignments"]}


# ---------------------------------------------------------------- 场景 1 #
def test_scenario_1_rerecord_with_quota2():
    """额度2：甲真带 [0,2)、乙翻录 [4,6]；原段与刀口随实物入账。"""
    reg, vault, archive, ed = _env("s1")
    aid = reg.register(sequence([2, 4, 6, 8]), 16000, note="原带").asset_id
    vault.register_reel(reg, "R1", aid, [0, 1, 2, 3, 4], note="原盘仅一份")
    ed.insert([
        {"clip_id": "cA", "asset_id": aid, "a": 0, "b": 2, "start": "0"},
        {"clip_id": "cB", "asset_id": aid, "a": 1, "b": 3, "start": "1/8000"},
    ])
    product = ed.publish()
    office = LoanOffice(vault, reg, archive)
    reqA = _req("rA", "甲", "cA", 0, 2, 5)                 # 只要原带
    reqB = _req("rB", "乙", "cB", 2, 4, 4, rr=True)        # 也接受翻录 2 拍

    # 剪接师独立核验：连续原样本区间与可落刀两端
    repA, repB = office.analyze(product, [reqA, reqB])
    assert repA["qualified"] and repA["source_interval"] == [0, 2]
    assert repB["qualified"] and repB["source_interval"] == [1, 3]
    assert repA["reels"] == [{"accession": "R1", "eligible": True, "conflicts": [],
                              "ends": {"lo": "reel_end", "hi": "cuttable"}}]
    assert repB["reels"][0]["ends"] == {"lo": "cuttable", "hi": "cuttable"}

    order = office.book("O1", product, [reqA, reqB], quota=2)
    am = _amap(order)
    assert am["rA"]["mode"] == "original" and am["rA"]["accession"] == "R1"
    assert am["rA"]["source_interval"] == [0, 2]
    assert am["rB"]["mode"] == "rerecord" and am["rB"]["beats"] == 2
    assert order["totals"] == {"value": 9, "rerecord_beats": 2, "new_cuts": 1}
    assert order["cuts"] == {"R1": [2]} and order["reels_to_cut"] == ["R1"]

    # 翻录新 WAV == 该段真实 render 从头到尾（源 [1,3) -> 样本值 [4,6]）
    wav = order["rerecord_wavs"]["rB"]
    pcm, rate = read_wav(wav["path"])
    assert rate == 16000 and list(int(v) for v in pcm) == [4, 6]
    assert archive.render_meta("loan-O1")["n_samples"] == 4

    # 已签借的原段与刀口随实物入账
    assert [(l["s0"], l["s1"]) for l in vault.loans("R1")] == [(0, 2)]
    assert vault.cuts_made("R1") == [2]

    # 重开库房，原单仍能回读
    vault2 = Vault(vault.root)
    back = vault2.get_order("O1")
    assert back["totals"]["value"] == 9 and _amap(back)["rB"]["mode"] == "rerecord"
    assert [(l["s0"], l["s1"]) for l in vault2.loans("R1")] == [(0, 2)]
    assert vault2.cuts_made("R1") == [2]

    # 同一单不可重复签
    try:
        office.book("O1", product, [reqA], quota=2)
        raise AssertionError("重复签单应拒绝")
    except VaultError:
        pass
    archive.close()
    print("[OK] 场景1：额度2 -> 甲真带、乙翻录[4,6]，价值9，落刀{2}，原单可回读")


# ---------------------------------------------------------------- 场景 2 #
def test_scenario_2_shared_cut_after_republish():
    """乙的源区间改成 [2,4) 重发节目：甲乙各拿真带，只在共同边界 2 落一刀。"""
    reg, vault, archive, ed = _env("s2")
    aid = reg.register(sequence([2, 4, 6, 8]), 16000).asset_id
    vault.register_reel(reg, "R1", aid, [0, 1, 2, 3, 4])
    ed.insert([
        {"clip_id": "cA", "asset_id": aid, "a": 0, "b": 2, "start": "0"},
        {"clip_id": "cB", "asset_id": aid, "a": 1, "b": 3, "start": "1/8000"},
    ])
    p1 = ed.publish()
    ed.modify("cB", {"a": 2, "b": 4})          # 只改乙具名引用的源区间
    p2 = ed.publish()                          # 重发真实节目
    assert p2["product_id"] != p1["product_id"]

    office = LoanOffice(vault, reg, archive)
    reqA = _req("rA", "甲", "cA", 0, 2, 5)
    reqB = _req("rB", "乙", "cB", 2, 4, 4, rr=True)   # 仍要两拍
    repB = office.analyze(p2, [reqB])[0]
    assert repB["source_interval"] == [2, 4]

    order = office.book("O2", p2, [reqA, reqB], quota=2)
    am = _amap(order)
    assert am["rA"]["mode"] == "original" and am["rA"]["source_interval"] == [0, 2]
    assert am["rB"]["mode"] == "original" and am["rB"]["source_interval"] == [2, 4]
    assert order["totals"] == {"value": 9, "rerecord_beats": 0, "new_cuts": 1}
    assert order["cuts"] == {"R1": [2]}        # 同批相邻两段共用切口只算一次
    assert not order["rerecord_wavs"]
    assert [(l["s0"], l["s1"]) for l in vault.loans("R1")] == [(0, 2), (2, 4)]
    archive.close()
    print("[OK] 场景2：改源区间重发 -> 各拿真带，只落一刀，价值9、翻录0")


# ---------------------------------------------------------------- 场景 3 #
def test_scenario_3_quota1_only_first():
    """保持原节目、额度1：乙翻录要2拍超额 -> 只能优先交甲，值5。"""
    reg, vault, archive, ed = _env("s3")
    aid = reg.register(sequence([2, 4, 6, 8]), 16000).asset_id
    vault.register_reel(reg, "R1", aid, [0, 1, 2, 3, 4])
    ed.insert([
        {"clip_id": "cA", "asset_id": aid, "a": 0, "b": 2, "start": "0"},
        {"clip_id": "cB", "asset_id": aid, "a": 1, "b": 3, "start": "1/8000"},
    ])
    product = ed.publish()
    office = LoanOffice(vault, reg, archive)
    reqA = _req("rA", "甲", "cA", 0, 2, 5)
    reqB = _req("rB", "乙", "cB", 2, 4, 4, rr=True)
    order = office.book("O3", product, [reqA, reqB], quota=1)
    am = _amap(order)
    assert am["rA"]["mode"] == "original"
    assert am["rB"]["mode"] == "rejected"
    assert order["totals"]["value"] == 5 and order["totals"]["rerecord_beats"] == 0
    archive.close()
    print("[OK] 场景3：额度1 -> 只能优先交甲，值5")


# ---------------------------------------------------------------- 场景 4 #
def test_scenario_4_loaned_middle_blocks_later_order():
    """另办一单：已签走的中段截断另一请求；PCM 还能读也不能重交；断口不花刀。"""
    reg, vault, archive, ed = _env("s4")
    aid = reg.register(sequence([2, 4, 6, 8]), 16000).asset_id
    vault.register_reel(reg, "R1", aid, [0, 1, 2, 3, 4])
    ed.insert([
        {"clip_id": "cA", "asset_id": aid, "a": 0, "b": 2, "start": "0"},
        {"clip_id": "cB", "asset_id": aid, "a": 1, "b": 3, "start": "1/8000"},
    ])
    p1 = ed.publish()
    office = LoanOffice(vault, reg, archive)
    office.book("O1", p1, [_req("rA", "甲", "cA", 0, 2, 5),
                           _req("rB", "乙", "cB", 2, 4, 4, rr=True)], quota=2)
    assert [(l["s0"], l["s1"]) for l in vault.loans("R1")] == [(0, 2)]

    # 另一节目：丙要整段 [0,4)，丁要 [2,4)
    ed2 = TimelineEditor(os.path.join(BASE, "s4", "editor2"), reg, name="main")
    ed2.insert([
        {"clip_id": "cC", "asset_id": aid, "a": 0, "b": 4, "start": "0"},
        {"clip_id": "cD", "asset_id": aid, "a": 2, "b": 4, "start": "1/4000"},
    ])
    p2 = ed2.publish()
    reqC = _req("rC", "丙", "cC", 0, 4, 10)              # 只收原带
    reqC2 = _req("rC2", "丙", "cC", 0, 4, 10, rr=True)   # 也接受翻录
    reqD = _req("rD", "丁", "cD", 4, 6, 3)               # 只收原带

    repC = office.analyze(p2, [reqC])[0]
    assert repC["qualified"] and repC["source_interval"] == [0, 4]
    assert repC["reels"][0]["eligible"] is False
    assert repC["reels"][0]["conflicts"] == [[0, 2]]     # 含已割走部分
    # 仓库 PCM 还能读，但实物中段已签走，不能重交
    assert list(int(v) for v in reg.load_pcm(aid)) == [2, 4, 6, 8]

    order = office.book("O4", p2, [reqC, reqC2, reqD], quota=4)
    am = _amap(order)
    assert am["rC"]["mode"] == "rejected"                # 只收原带 -> 截断
    assert am["rC2"]["mode"] == "rerecord" and am["rC2"]["beats"] == 4
    assert am["rD"]["mode"] == "original" and am["rD"]["source_interval"] == [2, 4]
    # 已有断口 2 与盘首尾 4 不花刀：本单真落刀 0
    assert order["totals"] == {"value": 13, "rerecord_beats": 4, "new_cuts": 0}
    assert order["cuts"] == {}
    pcm, _ = read_wav(order["rerecord_wavs"]["rC2"]["path"])
    assert list(int(v) for v in pcm) == [2, 4, 6, 8]
    assert [(l["s0"], l["s1"]) for l in vault.loans("R1")] == [(0, 2), (2, 4)]
    assert vault.cuts_made("R1") == [2]

    # 实物 [0,4) 已整段签出后，再要原带只能拒
    reqE = _req("rE", "戊", "cC", 0, 4, 10)
    order5 = office.book("O5", p2, [reqE], quota=0)
    assert _amap(order5)["rE"]["mode"] == "rejected"
    archive.close()
    print("[OK] 场景4：已签走中段截断新请求；PCM 可读也不能重交；断口不花刀")


# ---------------------------------------------------------------- 场景 5 #
def test_scenario_5_cancellation_and_substitution():
    """两非零贡献刚好相消 -> 无原件资格；零增益轨不算参与；别次出现不能顶替。"""
    reg, vault, archive, ed = _env("s5")
    aid = reg.register(sequence([2, 4, 6, 8]), 16000).asset_id
    vault.register_reel(reg, "R1", aid, [0, 1, 2, 3, 4])
    ed.insert([
        {"clip_id": "cX", "asset_id": aid, "a": 0, "b": 2, "start": "0"},
        {"clip_id": "cY", "asset_id": aid, "a": 0, "b": 2, "start": "0",
         "gain": "-1"},                       # 同一位置非零贡献刚好相消
    ])
    p_cancel = ed.publish()
    office = LoanOffice(vault, reg, archive)
    reqX = _req("rX", "甲", "cX", 0, 2, 5)
    rep = office.analyze(p_cancel, [reqX])[0]
    assert rep["qualified"] is False and rep["source_interval"] is None
    assert [b["beat"] for b in rep["bad_beats"]] == [0, 1]
    assert all("独自" in b["reasons"][-1] or "他轨" in b["reasons"][-1]
               for b in rep["bad_beats"])
    # 相消段的“原件”无资格；接受翻录则录到真实 render（全 0），而非原带样本
    order = office.book("O5a", p_cancel, [_req("rX", "甲", "cX", 0, 2, 5, rr=True)],
                        quota=2)
    am = _amap(order)
    assert am["rX"]["mode"] == "rerecord"
    pcm, _ = read_wav(order["rerecord_wavs"]["rX"]["path"])
    assert list(int(v) for v in pcm) == [0, 0]

    # 零增益的另一轨不算参与制声：改为增益 0 后具备原件资格
    ed.modify("cY", {"gain": 0})
    p_zero = ed.publish()
    rep = office.analyze(p_zero, [reqX])[0]
    assert rep["qualified"] and rep["source_interval"] == [0, 2]
    assert rep["reels"][0]["eligible"] is True

    # 同一素材的别次出现不能顶替：具名引用增益为 2，他处另有增益 1 的同素材
    ed3 = TimelineEditor(os.path.join(BASE, "s5", "editor3"), reg, name="main")
    ed3.insert([
        {"clip_id": "cN", "asset_id": aid, "a": 0, "b": 2, "start": "0",
         "gain": "2"},
        {"clip_id": "cAlt", "asset_id": aid, "a": 0, "b": 2, "start": "1/8000"},
    ])
    p_other = ed3.publish()
    rep = office.analyze(p_other, [_req("rN", "乙", "cN", 0, 2, 5)])[0]
    assert rep["qualified"] is False
    assert any("增益" in r for r in rep["bad_beats"][0]["reasons"])

    # 同素材两次出现叠在同一位置：具名引用并非独自制声
    ed4 = TimelineEditor(os.path.join(BASE, "s5", "editor4"), reg, name="main")
    ed4.insert([
        {"clip_id": "cM", "asset_id": aid, "a": 0, "b": 2, "start": "0"},
        {"clip_id": "cDup", "asset_id": aid, "a": 0, "b": 2, "start": "0"},
    ])
    p_dup = ed4.publish()
    rep = office.analyze(p_dup, [_req("rM", "丙", "cM", 0, 2, 5)])[0]
    assert rep["qualified"] is False
    archive.close()
    print("[OK] 场景5：相消无原件资格；零增益不算参与；别次出现不能顶替")


# ---------------------------------------------------------------- 场景 6 #
def test_scenario_6_second_copy_needs_own_accession():
    """同一录音的另一副本另有入藏号；一件实物签走后另一副本仍可出件。"""
    reg, vault, archive, ed = _env("s6")
    aid = reg.register(sequence([2, 4, 6, 8]), 16000).asset_id
    vault.register_reel(reg, "R1", aid, [0, 1, 2, 3, 4], note="副本一")
    vault.register_reel(reg, "R2", aid, [0, 1, 2, 3, 4], note="副本二")
    ed.insert({"clip_id": "cA", "asset_id": aid, "a": 0, "b": 2, "start": "0"})
    product = ed.publish()
    office = LoanOffice(vault, reg, archive)
    o1 = office.book("P1", product, [_req("r1", "甲", "cA", 0, 2, 5)], quota=0)
    o2 = office.book("P2", product, [_req("r2", "乙", "cA", 0, 2, 5)], quota=0)
    assert _amap(o1)["r1"]["accession"] == "R1"
    assert _amap(o2)["r2"]["accession"] == "R2"   # R1 已签走，落 R2
    assert [(l["s0"], l["s1"]) for l in vault.loans("R1")] == [(0, 2)]
    assert [(l["s0"], l["s1"]) for l in vault.loans("R2")] == [(0, 2)]
    # 两副本都签走后，第三家只能翻录或被拒
    o3 = office.book("P3", product, [_req("r3", "丙", "cA", 0, 2, 5)], quota=0)
    assert _amap(o3)["r3"]["mode"] == "rejected"
    archive.close()
    print("[OK] 场景6：同录音两副本各入藏号，可各自出件；第三家无原件")


# ---------------------------------------------------------------- 场景 7 #
def test_scenario_7_limits_and_rejections():
    """批量上限、非法请求、非法实物盘、归档字节被改 -> 整笔拒绝/核验失败。"""
    reg, vault, archive, ed = _env("s7")
    aid = reg.register(sequence([2, 4, 6, 8]), 16000).asset_id
    aid8 = reg.register(sequence([1, 2, 3, 4]), 8000, note="8k 素材").asset_id
    ed.insert({"clip_id": "cA", "asset_id": aid, "a": 0, "b": 2, "start": "0"})
    product = ed.publish()
    office = LoanOffice(vault, reg, archive)

    # 非 16kHz 不能登记为实物盘
    try:
        vault.register_reel(reg, "R8", aid8, [0, 1, 2, 3, 4])
        raise AssertionError("8kHz 应拒绝")
    except VaultError:
        pass
    # 可切边界越界
    try:
        vault.register_reel(reg, "RB", aid, [0, 5])
        raise AssertionError("边界越界应拒绝")
    except VaultError:
        pass
    # 入藏号唯一；最多四盘
    vault.register_reel(reg, "R1", aid, [0, 1, 2, 3, 4])
    try:
        vault.register_reel(reg, "R1", aid, [0])
        raise AssertionError("入藏号重复应拒绝")
    except VaultError:
        pass
    for i in range(2, MAX_REELS + 1):
        vault.register_reel(reg, f"R{i}", aid, [0, 1, 2, 3, 4])
    try:
        vault.register_reel(reg, "R5", aid, [0, 1, 2, 3, 4])
        raise AssertionError("第五盘应拒绝")
    except VaultError:
        pass

    # 翻录拍数必须与整段一致（从头到尾）
    try:
        LoanRequest.from_json({"request_id": "x", "clip_id": "cA", "out_lo": 0,
                               "out_hi": 2, "value": 1, "accept_rerecord": True,
                               "rerecord_beats": 1})
        raise AssertionError("翻录拍数不符应拒绝")
    except VaultError:
        pass
    # 请求数上限 16；同单 request_id 唯一
    many = [_req(f"r{i}", "甲", "cA", 0, 2, 1) for i in range(17)]
    try:
        office.plan(product, many, quota=0)
        raise AssertionError("17 个请求应拒绝")
    except VaultError:
        pass
    dup = [_req("r", "甲", "cA", 0, 2, 1), _req("r", "乙", "cA", 0, 2, 1)]
    try:
        office.plan(product, dup, quota=0)
        raise AssertionError("request_id 重复应拒绝")
    except VaultError:
        pass

    # 核实际归档字节：素材正文被改 -> 核验失败
    reg2 = AssetRegistry(os.path.join(BASE, "s7", "registry2"))
    vault2 = Vault(os.path.join(BASE, "s7", "vault2"))
    aid2 = reg2.register(sequence([2, 4, 6, 8]), 16000).asset_id
    vault2.register_reel(reg2, "R1", aid2, [0, 1, 2, 3, 4])
    ed2 = TimelineEditor(os.path.join(BASE, "s7", "editor2"), reg2, name="m")
    ed2.insert({"clip_id": "cA", "asset_id": aid2, "a": 0, "b": 2, "start": "0"})
    product2 = ed2.publish()
    write_wav(reg2.get(aid2).path, sequence([9, 9, 9, 9]), 16000)  # 篡改正文
    office2 = LoanOffice(vault2, reg2, None)
    try:
        office2.analyze(product2, [_req("r", "甲", "cA", 0, 2, 1)])
        raise AssertionError("归档字节被改应核验失败")
    except RuntimeError as e:
        assert "摘要" in str(e)
    archive.close()
    print("[OK] 场景7：上限/非法整笔拒绝；归档字节被改 -> 摘要核验失败")


# ---------------------------------------------------------------- 场景 8 #
def test_scenario_8_rerecord_knapsack():
    """库房无盘时纯翻录：额度内做背包选择（价值优先、再少拍数）。"""
    reg, vault, archive, ed = _env("s8")
    aid = reg.register(sequence([2, 4, 6, 8]), 16000).asset_id
    ed.insert([
        {"clip_id": "cE", "asset_id": aid, "a": 0, "b": 2, "start": "0"},
        {"clip_id": "cF", "asset_id": aid, "a": 0, "b": 3, "start": "1/8000"},
        {"clip_id": "cG", "asset_id": aid, "a": 0, "b": 2, "start": "5/16000"},
    ])
    product = ed.publish()
    office = LoanOffice(vault, reg, archive)   # 未登记任何实物盘
    reqs = [_req("rE", "甲", "cE", 0, 2, 3, rr=True),    # 2 拍
            _req("rF", "乙", "cF", 2, 5, 4, rr=True),    # 3 拍
            _req("rG", "丙", "cG", 5, 7, 3, rr=True)]    # 2 拍
    # 额度 4：E+G（值6、4拍）优于 F（值4）
    o1 = office.book("K1", product, reqs, quota=4)
    am = _amap(o1)
    assert [am[r]["mode"] for r in ("rE", "rF", "rG")] == [
        "rerecord", "rejected", "rerecord"]
    assert o1["totals"] == {"value": 6, "rerecord_beats": 4, "new_cuts": 0}
    # 额度 3：F（值4、3拍）最优
    o2 = office.book("K2", product, reqs, quota=3)
    am = _amap(o2)
    assert [am[r]["mode"] for r in ("rE", "rF", "rG")] == [
        "rejected", "rerecord", "rejected"]
    assert o2["totals"]["value"] == 4 and o2["totals"]["rerecord_beats"] == 3
    archive.close()
    print("[OK] 场景8：翻录额度内背包选择正确（值6/4拍、值4/3拍）")


# ---------------------------------------------------------------- 场景 9 #
def test_scenario_9_multi_reel_optimization():
    """多盘多请求：同一录音两副本同时出件；整盘借出不花刀。"""
    reg, vault, archive, ed = _env("s9")
    aid1 = reg.register(sequence([2, 4, 6, 8]), 16000, note="录音一").asset_id
    aid2 = reg.register(sequence([10, 20, 30, 40]), 16000, note="录音二").asset_id
    vault.register_reel(reg, "R1", aid1, [0, 1, 2, 3, 4])
    vault.register_reel(reg, "R2", aid1, [0, 1, 2, 3, 4])
    vault.register_reel(reg, "R3", aid2, [0, 1, 2, 3, 4])
    ed.insert([
        {"clip_id": "c1", "asset_id": aid1, "a": 0, "b": 2, "start": "0"},
        {"clip_id": "c2", "asset_id": aid1, "a": 1, "b": 3, "start": "1/8000"},
        {"clip_id": "c3", "asset_id": aid1, "a": 0, "b": 2, "start": "1/4000"},
        {"clip_id": "c4", "asset_id": aid2, "a": 0, "b": 4, "start": "3/8000"},
    ])
    product = ed.publish()
    office = LoanOffice(vault, reg, archive)
    reqs = [_req("r1", "甲", "c1", 0, 2, 5),          # 录音一 [0,2)
            _req("r2", "乙", "c2", 2, 4, 4),          # 录音一 [1,3)
            _req("r3", "丙", "c3", 4, 6, 6),          # 录音一 [0,2)
            _req("r4", "丁", "c4", 6, 10, 7, rr=True)]  # 录音二整盘
    order = office.book("M1", product, reqs, quota=4)
    am = _amap(order)
    # 两副本同时出 [0,2)：甲、丙各拿一盘（两盘对称，分到哪盘均可）；乙被拒
    assert am["r1"]["mode"] == "original" and am["r3"]["mode"] == "original"
    assert {am["r1"]["accession"], am["r3"]["accession"]} == {"R1", "R2"}
    assert am["r2"]["mode"] == "rejected"
    # 录音二整盘 [0,4) 借出：盘首尾不花刀
    assert am["r4"]["mode"] == "original" and am["r4"]["accession"] == "R3"
    assert order["totals"] == {"value": 18, "rerecord_beats": 0, "new_cuts": 2}
    assert order["cuts"] == {"R1": [2], "R2": [2]}
    assert order["reels_to_cut"] == ["R1", "R2"]
    assert [(l["s0"], l["s1"]) for l in vault.loans("R3")] == [(0, 4)]
    archive.close()
    print("[OK] 场景9：两副本同时出件，价值18、落刀2，整盘借出不花刀")


def run_all(base_path):
    global BASE
    BASE = base_path
    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE, exist_ok=True)
    test_scenario_1_rerecord_with_quota2()
    test_scenario_2_shared_cut_after_republish()
    test_scenario_3_quota1_only_first()
    test_scenario_4_loaned_middle_blocks_later_order()
    test_scenario_5_cancellation_and_substitution()
    test_scenario_6_second_copy_needs_own_accession()
    test_scenario_7_limits_and_rejections()
    test_scenario_8_rerecord_knapsack()
    test_scenario_9_multi_reel_optimization()
    print("借展剪接台测试全部通过")


if __name__ == "__main__":
    run_all(sys.argv[1] if len(sys.argv) > 1 else "/tmp/audiomix_loan")
