#!/usr/bin/env bash
# 声控走台端到端走查：真实 WAV -> 渲染入仓 -> 逐样本脉冲 -> 走台单 -> 锁定原因查询
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=/tmp/audiomix_light_cli
rm -rf "$ROOT"
REG="$ROOT/registry"
ARCH="$ROOT/archive"
PROD="$ROOT/product.json"
PY="python3 -m audiomix.cli"

echo "== 1. 造短 WAV 素材 [0,3,0,3,0,3,0]，另加真正 -3 相消片段 =="
AID=$($PY asset-seq --root "$REG" --rate 16000 --values 0,3,0,3,0,3,0 \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["asset_id"])')
NEG=$($PY asset-seq --root "$REG" --rate 16000 --values -3 \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["asset_id"])')
echo "  AID=$AID  NEG=$NEG"

echo "== 2. 原档发布 + 渲染（真实混音 PCM = 0,3,0,3,0,3,0） =="
$PY tl-insert --root "$REG" --clip-id beeps --asset-id "$AID" --a 0 --b 7 --start 0 >/dev/null
$PY tl-publish --root "$REG" --out "$PROD" >/dev/null
$PY render --archive-root "$ARCH" --product "$PROD" --registry-root "$REG" \
    --T 7/16000 --render-id base >/dev/null
$PY read --archive-root "$ARCH" --render-id base --start 0 --end 7 \
    | python3 -c 'import sys,json;print("  PCM =",json.load(sys.stdin)["samples"])'

echo "== 3. 从实际入仓 PCM 检测：脉冲应为 1、3、5 =="
$PY lights-pulses --archive-root "$ARCH" --contacts "pickup:base:2:0:1:1" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  pulses =",d["contacts"][0]["pulse_beats"])'

echo "== 4. 一个门：灯1 必亮 1、5 -> 最少手按 1 次 =="
$PY lights-plan --archive-root "$ARCH" --contacts "pickup:base:2:0:1:1" \
    --required "L1@1,L1@5" --roster "1=alice;5=alice" --gates 1 --beats-total 7 \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  manual",d["manual_count"],"gates",[(g["window"]) for g in d["gates"]],"check",d["check"]["valid"])'

echo "== 5. 加真正相消的 -3 短 clip -> 脉冲只剩 1、5 -> 长门零手按 =="
$PY tl-insert --root "$REG" --clip-id cancel --asset-id "$NEG" --a 0 --b 1 --start 3/16000 >/dev/null
$PY tl-publish --root "$REG" --out "$ROOT/product_cancel.json" >/dev/null
$PY render --archive-root "$ARCH" --product "$ROOT/product_cancel.json" \
    --registry-root "$REG" --T 7/16000 --render-id cancel >/dev/null
$PY read --archive-root "$ARCH" --render-id cancel --start 0 --end 7 \
    | python3 -c 'import sys,json;print("  PCM =",json.load(sys.stdin)["samples"])'
$PY lights-pulses --archive-root "$ARCH" --contacts "pickup:cancel:2:0:1:1" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  pulses =",d["contacts"][0]["pulse_beats"])'
$PY lights-plan --archive-root "$ARCH" --contacts "pickup:cancel:2:0:1:1" \
    --required "L1@1,L1@5" --gates 1 --beats-total 7 \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  manual",d["manual_count"],"window",d["gates"][0]["window"])'

echo "== 6. 固定原档 + 两个门 -> 分开两窗，零手按 =="
$PY lights-plan --archive-root "$ARCH" --contacts "pickup:base:2:0:1:1" \
    --required "L1@1,L1@5" --gates 2 --beats-total 7 \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  manual",d["manual_count"],"windows",[g["window"] for g in d["gates"]])'

echo "== 7. 声音落空说明：若把长门 [1,3] 接到灯1，会在 3 误亮（被模型拒绝） =="
echo "  自动推导的候选只切出窗口内脉冲全为必亮点的窗；3 不是灯1 必亮，故不会出现 [1,3]。"

echo "声控 CLI 走查通过"
