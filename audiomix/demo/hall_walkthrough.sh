#!/usr/bin/env bash
# 试听厅校准端到端走查（全 CLI）：
#   激励/实录登记 -> 单路反卷积 -> 另一试音片交叉验证
#   -> 构造场景 -> 最低安装费求解 -> 屏板/交接线/各座实响 -> 真实 WAV 试听
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=/tmp/audiomix_hall_walk
rm -rf "$ROOT"; mkdir -p "$ROOT"
REG="$ROOT/registry"
PY="python3 -m audiomix.cli"

jsonid() { python3 -c 'import sys,json;print(json.load(sys.stdin)["asset_id"])'; }

echo "== 1. 登记激励与实录（短整数序列，16k） =="
# 主激励 [1,1,1,1]；两条真实试音片（不同激励）用于交叉验证
X1=$($PY asset-seq --root "$REG" --rate 16000 --values 1,1,1,1 --note exc1 | jsonid)
X2=$($PY asset-seq --root "$REG" --rate 16000 --values 2,-1,3,1 --note exc2 | jsonid)

# 硬编码每条路径的观测（= 完整整数卷积，含末尾零；与 tests/test_hall.py 一致）
#   L->X [3,0,0]；L->Y [0,0,0]；R->X [0,0,3]；R->Y [3,0,0]；装板 R->X [0,0,0]
# x1=[1,1,1,1] 的观测：
L_X=$($PY asset-seq --root "$REG" --rate 16000 --values 3,3,3,3,0,0 --note L-X | jsonid)
L_Y=$($PY asset-seq --root "$REG" --rate 16000 --values 0,0,0,0,0,0 --note L-Y | jsonid)
R_X=$($PY asset-seq --root "$REG" --rate 16000 --values 0,0,3,3,3,3 --note R-X | jsonid)
R_Y=$($PY asset-seq --root "$REG" --rate 16000 --values 3,3,3,3,0,0 --note R-Y | jsonid)
R_XP=$($PY asset-seq --root "$REG" --rate 16000 --values 0,0,0,0,0,0 --note R-X-panel | jsonid)
# x2=[2,-1,3,1] 对 h=[0,0,3] 的观测（完整卷积，含末尾零）：[0,0,6,-3,9,3]
R_X_ALT=$($PY asset-seq --root "$REG" --rate 16000 \
    --values 0,0,6,-3,9,3 --note R-X-exc2 | jsonid)

echo "== 2. 单路反卷积：R->X(裸机) 应为 [0,0,3] =="
$PY hall-deconv --registry-root "$REG" --excitation "$X1" --observation "$R_X" --max-len 3

echo "== 3. 题面真实例：[1,1] 激励、[2,3,1] 实录、H=2 -> 响应 [2,1] =="
RX=$($PY asset-seq --root "$REG" --rate 16000 --values 1,1 | jsonid)
RY=$($PY asset-seq --root "$REG" --rate 16000 --values 2,3,1 | jsonid)
$PY hall-deconv --registry-root "$REG" --excitation "$RX" --observation "$RY" --max-len 2

echo "== 4. 另一真实试音片交叉验证同一路 =="
$PY hall-validate --registry-root "$REG" --speaker R --seat X --state bare \
    --record "R,X,bare,$X1,$R_X,3" --record "R,X,bare,$X2,$R_X_ALT,3"

echo "== 5. 构造两排试听席场景并求最低安装费 =="
python3 - "$ROOT" "$X1" "$L_X" "$L_Y" "$R_X" "$R_Y" "$R_XP" <<'PY'
import json, sys
root, x1, lx, ly, rx, ry, rxp = sys.argv[1:8]
def p(spk, seat, state, obs):
    return {"speaker": spk, "seat": seat, "state": state,
            "excitation": x1, "observation": obs, "max_len": 3}
spec = {
    "speakers": ["L", "R"], "seats": ["X", "Y"],
    "states": {"L": ["bare"], "R": ["bare", "panel"]},
    "delays": {"L": [0, 2], "R": [0]},
    "state_cost": {"L": {}, "R": {"bare": 0, "panel": 3}},
    "delay_cost": {"L": {"0": 0, "2": 2}, "R": {"0": 0}},
    "seat_specs": {"X": {"threshold": 2, "latest_onset": 2, "allowed_gap": 1},
                   "Y": {"threshold": 2, "latest_onset": 2, "allowed_gap": 1}},
    "paths": [p("L", "X", "bare", lx), p("L", "Y", "bare", ly),
              p("R", "X", "bare", rx), p("R", "Y", "bare", ry),
              p("R", "X", "panel", rxp), p("R", "Y", "panel", ry)],
}
json.dump(spec, open(root + "/scenario.json", "w"), ensure_ascii=False, indent=2)
print("  场景写入", root + "/scenario.json")
PY

$PY hall-solve --registry-root "$REG" --scenario "$ROOT/scenario.json" \
    --out "$ROOT/result.json" --wav-dir "$ROOT/out" --summary

echo "== 6. 交付物：result.json + 真实 WAV 试听 =="
python3 - "$ROOT" <<'PY'
import json, sys, wave
root = sys.argv[1]
d = json.load(open(root + "/result.json"))
print("  最低安装费:", d["total_cost"])
print("  屏板:", [(b["speaker"], b["state"], b["delay"], b["cost"]) for b in d["panel_board"]])
for a, seq in d["seat_sequences"].items():
    print(f"  {a} 实响序列:", seq)
for name, path in d["wavs"].items():
    with wave.open(path) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        print(f"  WAV {name}: {path}  {w.getnframes()} frames @ {w.getframerate()}Hz")
PY
echo "试听厅 CLI 走查通过"
