"""双盲听审手算案例：题面给定的全部情形逐条落地。

运行：python3 -m tests.test_blind [临时目录]
不依赖 numpy/pytest，纯标准库，便于与 run_all_checks.sh 同级复跑。
"""
import json
import os
import random
import shutil

from audiomix.blindstim import (
    INT16_MAX, build_combos, make_item_packages, verify_bundle_integrity,
    write_stimulus_bundle,
)
from audiomix.blindschedule import (
    ItemSpec, Problem, Schedule, Session, solve, verify_schedule,
)
from audiomix.blindtrial import (
    ItemInput, anonymous_play_sheets, run_trial, schedule_summary,
    sealed_trace_report, verify_trial_playlists,
)

SEED = 20261122


def _case_p1(deadline=3, listeners=("甲", "乙"), availability=None,
             pcm_b=(2, 1), quota=None, n_slots=None, capacity=None):
    n_slots = n_slots or deadline
    availability = availability or {p: list(range(1, n_slots + 1)) for p in listeners}
    return run_trial(
        [ItemInput("P1", [1, 2], list(pcm_b), [1], [1], weight=5,
                   deadline=deadline)],
        listeners=list(listeners), n_slots=n_slots,
        headphone_capacity=capacity or [1] * n_slots,
        availability=availability, cross_person_quota=quota,
        rng=random.Random(SEED))


# ----------------------- 题面主例：配平 + 零跨人换序 ----------------------- #

def test_case_1_same_person_swap():
    """A=[1,2], B=[2,1], 增益 1：平方和都 5，甲在 1/3 段前后换序，零跨人。"""
    result = _case_p1()
    combos = build_combos("P1", [1, 2], [2, 1], [1], [1])
    assert len(combos) == 1
    c = combos[0]
    assert c.sum_sq == 5 and c.differ_positions == 2
    assert c.scaled_a == (1, 2) and c.scaled_b == (2, 1)

    sch = result.schedule
    assert sch.total_weight == 5
    assert set(sch.completed_items) == {"P1"}
    assert sch.cross_person_items == frozenset()          # 零跨人
    assert sch.finish_slot == 3                            # 第 3 段收场
    sessions = sch.sessions_of("P1")
    assert [s.slot for s in sessions] == [1, 3]            # 中间留第 2 段洗耳
    assert len({s.listener for s in sessions}) == 1        # 同一人，零跨人
    assert {s.first_render for s in sessions} == {"A", "B"}  # 两序相反
    assert sessions[0].headphone == sessions[1].headphone == 1
    assert verify_schedule(sch, result.problem) == []
    print("  [OK] 案例1：值 5、零跨人、甲第1/3段换序，中间整段洗耳")


def test_case_2_cross_person_finish_early():
    """要在第 2 段收场：甲乙各听一序，值 5、跨 1；截止第3段则默认偏好零跨人。"""
    # 截止第 2 段：同一人 gap>=2 不可行，唯一出路是跨人，第 2 段收场
    early = _case_p1(deadline=2, n_slots=3)
    sch = early.schedule
    assert sch.total_weight == 5
    assert set(sch.cross_person_items) == {"P1"}
    assert sch.finish_slot == 2
    persons = {s.listener for s in sch.sessions_of("P1")}
    assert persons == {"甲", "乙"}
    slots = sorted(s.slot for s in sch.sessions_of("P1"))
    assert slots == [1, 2]
    assert verify_schedule(sch, early.problem) == []

    # 额度模式（额度1）下同样第 2 段收场、跨 1，结论一致且可复现
    quota_res = _case_p1(deadline=2, n_slots=3, quota=1)
    assert quota_res.schedule.finish_slot == 2
    assert set(quota_res.schedule.cross_person_items) == {"P1"}

    # 额度=0：禁止跨人，截止第2段排不开 -> 此项得 0
    zero_res = _case_p1(deadline=2, n_slots=3, quota=0)
    assert zero_res.schedule.completed_items == frozenset()
    assert zero_res.schedule.total_weight == 0

    # 截止放宽到第 3 段：默认三目标（跨人最少优先）-> 零跨人，第 3 段收场
    relaxed = _case_p1(deadline=3)
    assert relaxed.schedule.cross_person_items == frozenset()
    assert relaxed.schedule.finish_slot == 3
    print("  [OK] 案例2：截止2段 -> 跨1第2段收场；额度0 -> 得0；截止3段 -> 零跨人")


# ----------------------- 题面反例：得 0 的三种情形 ----------------------- #

def test_case_3_unbalanceable_gets_zero():
    """B 换成真实新片 [2,2]：平方和 8 != 5，无法配平，此项得 0。"""
    result = _case_p1(pcm_b=(2, 2))
    assert "P1" in result.rejected
    assert result.schedule.completed_items == frozenset()
    assert result.zero_items() == ["P1"]
    assert result.schedule.total_weight == 0
    # 数值依据：A 平方和 5，B 平方和 8
    assert build_combos("P1", [1, 2], [2, 2], [1], [1]) == []
    print("  [OK] 案例3：平方和 5≠8 配不平，刺激不可制作，得 0 ->",
          result.rejected["P1"])


def test_case_4_no_washout_gap_gets_zero():
    """限定前两段、听者仅剩甲：同一人没有空档，两序不能齐，得 0。"""
    result = _case_p1(deadline=2, listeners=("甲",), n_slots=2,
                      availability={"甲": [1, 2]})
    assert result.schedule.completed_items == frozenset()
    assert result.schedule.total_weight == 0
    # 刺激本身是合规的，失败 purely 是排期：枚举不到 gap>=2 的两人/一人方案
    assert "P1" not in result.rejected
    print("  [OK] 案例4：刺激合规但仅 2 段 1 人，洗耳空档不存在，得 0")


def test_case_5_identical_after_gain_rejected():
    """片段不同、经许可增益后字节完全相同：不得印成有效问卷。"""
    # A=[1,2]×2=[2,4]，B=[2,4]×1=[2,4]：平方和都 20，但逐字节相同
    combos = build_combos("DUP", [1, 2], [2, 4], [2], [1])
    assert combos == []
    result = run_trial(
        [ItemInput("DUP", [1, 2], [2, 4], [2], [1], weight=9, deadline=3)],
        listeners=["甲"], n_slots=3, headphone_capacity=[1, 1, 1],
        availability={"甲": [1, 2, 3]}, rng=random.Random(SEED))
    assert result.schedule.completed_items == frozenset()
    assert "完全相同" in result.rejected["DUP"]
    print("  [OK] 案例5：增益后字节全同（differ=0），拒绝印片，得 0 ->",
          result.rejected["DUP"])


def test_case_6_washout_slot_contention():
    """两项抢同一人的洗耳空段：重用一张排期不能算完成两项。"""
    items = [
        ItemInput("P1", [1, 2], [2, 1], [1], [1], weight=5, deadline=3),
        ItemInput("P2", [1, 2], [2, 1], [1], [1], weight=5, deadline=3),
    ]
    # 只有甲、每段一台：甲的两场只能是 1/3 段，第 2 段是洗耳空档；
    # 第二项若也想占 1、3 段，人与耳机都冲突 -> 只能完成一项
    result = run_trial(items, listeners=["甲"], n_slots=3,
                       headphone_capacity=[1, 1, 1],
                       availability={"甲": [1, 2, 3]}, rng=random.Random(SEED))
    sch = result.schedule
    assert set(sch.completed_items) == {"P1"} or set(sch.completed_items) == {"P2"}
    assert len(sch.completed_items) == 1
    assert sch.total_weight == 5
    assert verify_schedule(sch, result.problem) == []

    # 若有人手工把同一张 (甲,1)/(甲,3) 排期同时挂给两项，独立校验必须报错
    fake = Schedule(
        sessions=[
            Session("P1", 1, "甲", 1, "A"), Session("P1", 3, "甲", 1, "B"),
            Session("P2", 1, "甲", 1, "A"), Session("P2", 3, "甲", 1, "B"),
        ],
        completed_items=frozenset({"P1", "P2"}),
        cross_person_items=frozenset(), total_weight=10, finish_slot=3)
    errors = verify_schedule(fake, result.problem)
    assert any("抢占" in e for e in errors), errors
    print("  [OK] 案例6：洗耳/耳机冲突下只完成一项；伪造复用排期被独立校验拒绝")


# ----------------------- 增益与 16 位边界 ----------------------- #

def test_case_7_overflow_rejects_whole_combo():
    """任一采样乘增益越界，整个组合退回（不是削顶单个样本）。"""
    big = INT16_MAX  # 32767
    combos = build_combos("OV", [big, 1], [1, big], [1, 2], [1, 2])
    # 增益 2 全部越界退回；增益 1 时两侧平方和相等(32767²+1)但采样不同 -> 唯一合规
    assert len(combos) == 1
    assert combos[0].gain_a == 1 and combos[0].gain_b == 1
    # 负向边界同样整组退回
    assert build_combos("OV2", [-32768], [-16384], [1, 2], [1]) == []
    print("  [OK] 案例7：越界增益整组退回，仅保留全部样本在 16 位内的组合")


def test_case_8_multi_gain_balance_picks_deterministic():
    """多档增益可配平：(A×2,B×1) 平方和相等时合规，选择确定性可复现。"""
    # A=[1,2]×2=[2,4] 平方和 20；B=[2,4]×1=[2,4] 全同 -> 拒
    # B=[4,2]×1 平方和 20 且与 [2,4] 不同 -> 合规
    combos = build_combos("MG", [1, 2], [4, 2], [1, 2], [1])
    assert len(combos) == 1
    assert combos[0].gain_a == 2 and combos[0].gain_b == 1
    r1 = run_trial([ItemInput("MG", [1, 2], [4, 2], [1, 2], [1], 5, 3)],
                   listeners=["甲"], n_slots=3, headphone_capacity=[1, 1, 1],
                   availability={"甲": [1, 2, 3]}, rng=random.Random(SEED))
    r2 = run_trial([ItemInput("MG", [1, 2], [4, 2], [1, 2], [1], 5, 3)],
                   listeners=["甲"], n_slots=3, headphone_capacity=[1, 1, 1],
                   availability={"甲": [1, 2, 3]}, rng=random.Random(SEED))
    s1 = sealed_trace_report(r1)["MG"]["entries"]
    s2 = sealed_trace_report(r2)["MG"]["entries"]
    assert [e["anon_id"] for e in s1] == [e["anon_id"] for e in s2]
    print("  [OK] 案例8：多增益档配平枚举正确，同种子匿名号/选组稳定可复现")


# ----------------------- 匿名封包、追溯表与播放单 ----------------------- #

def test_case_9_anonymity_and_sealed_trace(tmp_path):
    """公开实包无 A/B 明文；持封存表可复核匿名号 -> 源 render，两序相反。"""
    result = _case_p1()
    errors = verify_trial_playlists(result)
    assert errors == []
    stim = result.stimulus_sets["P1"]
    verify_bundle_integrity(stim)

    paths = write_stimulus_bundle(stim, os.path.join(tmp_path, "P1_bundle"))
    manifest = json.load(open(paths["manifest"], encoding="utf-8"))
    sealed = json.load(open(paths["sealed_table"], encoding="utf-8"))

    # 公开面：文件名、清单正文不得出现 A/B 源身份
    public_text = json.dumps(manifest, ensure_ascii=False)
    for fname in os.listdir(paths["public_dir"]):
        assert not any(tag in fname for tag in ("_A", "_B", "renderA", "renderB"))
        if fname.endswith(".json"):
            public_text += open(os.path.join(paths["public_dir"], fname),
                                encoding="utf-8").read()
    assert '"source_render"' not in public_text
    assert '"A"' not in public_text and '"B"' not in public_text

    # 封存面：匿名号可追溯到 A/B，且两序严格相反
    render_of = {e["anon_id"]: e["source_render"] for e in sealed["entries"]}
    assert set(render_of.values()) == {"A", "B"}
    ab = [render_of[x] for x in stim.order_ab]
    ba = [render_of[x] for x in stim.order_ba]
    assert ab == ["A", "B"] and ba == ["B", "A"]

    # 逐人播放单只含匿名号；持表人能验证同一人的两场整序严格相反
    sheets = anonymous_play_sheets(result)
    sheet_text = json.dumps(sheets, ensure_ascii=False)
    assert "render" not in sheet_text and '"A"' not in sheet_text
    person = next(p for p, rows in sheets.items() if rows)
    rows = sorted(sheets[person], key=lambda row: row["slot"])
    seq1 = [step["anon_id"] for step in rows[0]["sequence"]]
    seq2 = [step["anon_id"] for step in rows[1]["sequence"]]
    assert len(rows) == 2 and seq1 == list(reversed(seq2))
    # FIRST/SECOND 位置上的匿名号互换，听者无法从标签识别 A/B
    assert rows[0]["sequence"][0]["anon_id"] == rows[1]["sequence"][1]["anon_id"]
    print("  [OK] 案例9：公开面零身份信息；封存表可追溯，播放单两序匿名且相反")


# ----------------------- 回避规则、并行耳机、独立校验 ----------------------- #

def test_case_10_forbidden_listener():
    """已知道本项匿名片真实身份的人不得听本项。"""
    result = run_trial(
        [ItemInput("P1", [1, 2], [2, 1], [1], [1], 5, 3,
                   forbidden_listeners=["甲"])],
        listeners=["甲", "乙"], n_slots=3, headphone_capacity=[1, 1, 1],
        availability={"甲": [1, 2, 3], "乙": [1, 2, 3]},
        rng=random.Random(SEED))
    assert set(result.schedule.completed_items) == {"P1"}
    assert all(s.listener == "乙" for s in result.schedule.sessions)

    # 两人都回避 -> 排不出，得 0
    result2 = run_trial(
        [ItemInput("P1", [1, 2], [2, 1], [1], [1], 5, 3,
                   forbidden_listeners=["甲", "乙"])],
        listeners=["甲", "乙"], n_slots=3, headphone_capacity=[1, 1, 1],
        availability={"甲": [1, 2, 3], "乙": [1, 2, 3]},
        rng=random.Random(SEED))
    assert result2.schedule.completed_items == frozenset()
    print("  [OK] 案例10：知情者被回避；全员知情则无法完成")


def test_case_11_parallel_headphones_and_capacity():
    """不同比较可并行：两台耳机允许同段两场；一台则串行受容量限制。"""
    items = [ItemInput("P1", [1, 2], [2, 1], [1], [1], 5, 2),
             ItemInput("P2", [1, 2], [2, 1], [1], [1], 5, 2)]
    # 跨人才能在截止 2 段内完成；2 台耳机 + 2 人 -> 两项同段并行，权重 10
    res2 = run_trial(items, listeners=["甲", "乙"], n_slots=2,
                     headphone_capacity=[2, 2],
                     availability={"甲": [1, 2], "乙": [1, 2]},
                     rng=random.Random(SEED))
    assert set(res2.schedule.completed_items) == {"P1", "P2"}
    assert res2.schedule.total_weight == 10
    assert res2.schedule.finish_slot == 2
    # 同段耳机占用确实达到 2 台但不超
    hp_by_slot = {}
    for s in res2.schedule.sessions:
        hp_by_slot.setdefault(s.slot, set()).add(s.headphone)
    assert all(len(hps) <= 2 for hps in hp_by_slot.values())

    # 每段仅 1 台：两项无法在 2 段内各占两序 -> 只能完成一项
    res1 = run_trial(items, listeners=["甲", "乙"], n_slots=2,
                     headphone_capacity=[1, 1],
                     availability={"甲": [1, 2], "乙": [1, 2]},
                     rng=random.Random(SEED))
    assert len(res1.schedule.completed_items) == 1
    assert res1.schedule.total_weight == 5
    print("  [OK] 案例11：耳机容量 2 -> 并行完成两项；容量 1 -> 仅一项")


def test_case_12_independent_verifier_catches_manipulation():
    """独立校验器对洗耳不足、序型重复、越界耳机、超截止段逐条报错。"""
    base = _case_p1()
    prob = base.problem
    bad_schedule = Schedule(
        sessions=[
            Session("P1", 1, "甲", 1, "A"),
            Session("P1", 2, "甲", 1, "B"),   # 只隔 0 个整段，洗耳不足
        ],
        completed_items=frozenset({"P1"}), cross_person_items=frozenset(),
        total_weight=5, finish_slot=2)
    errors = verify_schedule(bad_schedule, prob)
    assert any("洗耳" in e for e in errors)

    bad2 = Schedule(
        sessions=[Session("P1", 1, "甲", 1, "A"),
                  Session("P1", 3, "甲", 1, "A")],  # 两序同向
        completed_items=frozenset({"P1"}), cross_person_items=frozenset(),
        total_weight=5, finish_slot=3)
    assert any("AB" in e for e in verify_schedule(bad2, prob))

    bad3 = Schedule(
        sessions=[Session("P1", 1, "甲", 5, "A"),   # 耳机台号超容量
                  Session("P1", 3, "甲", 1, "B")],
        completed_items=frozenset({"P1"}), cross_person_items=frozenset(),
        total_weight=5, finish_slot=3)
    assert any("耳机" in e for e in verify_schedule(bad3, prob))
    print("  [OK] 案例12：洗耳不足/序型重复/耳机越界均被独立校验器拦截")


def test_case_13_weight_priority_then_cross_then_finish():
    """字典序目标：先保高权重和，再少跨人，再早完场。"""
    # P1 权重 9 只能跨人（2 段内），P2 权重 5 可零跨人但需 3 段
    items = [
        ItemInput("HI", [1, 2], [2, 1], [1], [1], weight=9, deadline=2),
        ItemInput("LO", [1, 2], [2, 1], [1], [1], weight=5, deadline=3),
    ]
    res = run_trial(items, listeners=["甲", "乙"], n_slots=3,
                    headphone_capacity=[1, 1, 1],
                    availability={"甲": [1, 2, 3], "乙": [1, 2, 3]},
                    rng=random.Random(SEED))
    # 高权重优先：HI 必做（跨1）；余下容量 LO 若可插则做，总权重取最大
    assert "HI" in res.schedule.completed_items
    assert res.schedule.total_weight >= 9
    assert verify_schedule(res.schedule, res.problem) == []
    print("  [OK] 案例13：高权重项优先保完成，目标字典序生效 ->",
          schedule_summary(res))


def test_case_14_quota_mode_finish_first():
    """同一组可行解：默认模式少跨人优先；额度模式同权重下早完场优先。"""
    items = [ItemInput("P1", [1, 2], [2, 1], [1], [1], weight=5, deadline=4)]
    common = dict(listeners=["甲", "乙"], n_slots=4,
                  headphone_capacity=[2, 2, 2, 2],
                  availability={"甲": [1, 2, 3, 4], "乙": [1, 2, 3, 4]})
    # 默认：零跨人（第1/3段，第3段收场）胜过跨人（第1/2段，第2段收场）
    default_res = run_trial(items, **common, rng=random.Random(SEED))
    assert default_res.schedule.cross_person_items == frozenset()
    assert default_res.schedule.finish_slot == 3
    # 额度 1：同权重 5 下改为最早完场 -> 跨人并行，不晚于第 2 段收场
    quota_res = run_trial(items, cross_person_quota=1, **common,
                          rng=random.Random(SEED))
    assert quota_res.schedule.finish_slot <= 2
    assert set(quota_res.schedule.cross_person_items) == {"P1"}
    assert len({s.listener for s in quota_res.schedule.sessions_of("P1")}) == 2
    # 额度 0：跨人被禁，退回零跨人第 3 段收场
    quota0 = run_trial(items, cross_person_quota=0, **common,
                       rng=random.Random(SEED))
    assert quota0.schedule.cross_person_items == frozenset()
    assert quota0.schedule.finish_slot == 3
    print("  [OK] 案例14：默认少跨人(3段)；额度1早完场(2段,跨1)；额度0禁跨回退(3段)")


def test_case_15_mid_gap_must_stay_idle():
    """同一人两场之间的中段若被另一项占用，洗耳规则不允许。"""
    items = [
        ItemInput("P1", [1, 2], [2, 1], [1], [1], weight=5, deadline=3),
        ItemInput("P2", [1, 2], [2, 1], [1], [1], weight=1, deadline=3),
    ]
    # 两台耳机、一人：P1 若占 1/3 段，第 2 段是其洗耳空档；
    # P2 不能把任何一场塞进该空档给同一人（可以并行给第二人，但此处只有甲）
    res = run_trial(items, listeners=["甲"], n_slots=3,
                    headphone_capacity=[2, 2, 2],
                    availability={"甲": [1, 2, 3]}, rng=random.Random(SEED))
    assert "P1" in res.schedule.completed_items
    p1_slots = sorted(s.slot for s in res.schedule.sessions_of("P1"))
    if "P2" in res.schedule.completed_items:
        p2_slots = sorted(s.slot for s in res.schedule.sessions_of("P2"))
        # P2 的任何场次都不得落在 P1 两场之间
        assert not any(p1_slots[0] < t < p1_slots[1] for t in p2_slots)
    assert verify_schedule(res.schedule, res.problem) == []
    print("  [OK] 案例15：洗耳空档内同听者不会被插入其他节目")


def run_all(base_path: str) -> None:
    shutil.rmtree(base_path, ignore_errors=True)
    os.makedirs(base_path, exist_ok=True)
    print("== 双盲听审：刺激制作 / 匿名封包 / 两序排期 手算案例 ==")
    cases = [
        test_case_1_same_person_swap,
        test_case_2_cross_person_finish_early,
        test_case_3_unbalanceable_gets_zero,
        test_case_4_no_washout_gap_gets_zero,
        test_case_5_identical_after_gain_rejected,
        test_case_6_washout_slot_contention,
        test_case_7_overflow_rejects_whole_combo,
        test_case_8_multi_gain_balance_picks_deterministic,
        test_case_10_forbidden_listener,
        test_case_11_parallel_headphones_and_capacity,
        test_case_12_independent_verifier_catches_manipulation,
        test_case_13_weight_priority_then_cross_then_finish,
        test_case_14_quota_mode_finish_first,
        test_case_15_mid_gap_must_stay_idle,
    ]
    for i, fn in enumerate(cases, 1):
        fn()
    test_case_9_anonymity_and_sealed_trace(os.path.join(base_path, "case_09"))
    print("\n全部盲听案例通过")


if __name__ == "__main__":
    import sys
    run_all(sys.argv[1] if len(sys.argv) > 1 else "/tmp/audiomix_blind")
