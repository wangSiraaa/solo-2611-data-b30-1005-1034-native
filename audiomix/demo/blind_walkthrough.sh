#!/usr/bin/env bash
# 盲听端到端走查：案例 JSON -> 匿名实包/封存追溯表 -> 两序排期 -> 逐人播放单与复核
# 纯标准库模块，无 numpy 也可运行。
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=/tmp/audiomix_blind_cli
rm -rf "$ROOT"
PY="python3 -m audiomix.blindcli"

echo "== 1. 默认三目标模式（权重 -> 少跨人 -> 早完场） =="
$PY solve --case demo/blind_case.json --seed 20261122 --out-dir "$ROOT/default" \
  | python3 -c '
import sys, json
r = json.load(sys.stdin)
assert r["verification_errors"] == []
s = r["summary"]
print("  完成项:", s["completed_items"], "权重:", s["total_weight"],
      "跨人:", s["cross_person_count"], "完场段:", s["finish_slot"])
print("  得0项:", s["zero_items"])
for iid, reason in s["rejected_stimuli"].items():
    print("   -", iid, "->", reason)
'

echo "== 2. 跨人额度模式（同权重下最早完场） =="
python3 - "$ROOT/case_quota.json" <<'EOF'
import json, sys
case = json.load(open("demo/blind_case.json", encoding="utf-8"))
case["cross_person_quota"] = 1
json.dump(case, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False)
EOF
$PY solve --case "$ROOT/case_quota.json" --seed 20261122 \
  | python3 -c '
import sys, json
r = json.load(sys.stdin); s = r["summary"]
assert r["verification_errors"] == []
print("  完场段:", s["finish_slot"], "跨人项:", s["cross_person_items"])
'

echo "== 3. 落盘目录：公开面无 A/B，封存表可追溯且两序相反 =="
python3 - "$ROOT" <<'EOF'
import json, os, sys
root = sys.argv[1]
pub = os.path.join(root, "default", "stimuli", "P1_配平可换序", "public")
sealed = os.path.join(root, "default", "stimuli", "P1_配平可换序", "sealed")
public_blob = ""
for dirpath, _, files in os.walk(pub):
    for f in files:
        public_blob += open(os.path.join(dirpath, f), encoding="utf-8").read()
assert '"source_render"' not in public_blob and '"A"' not in public_blob, "公开面泄露身份"
trace = json.load(open(os.path.join(sealed, "trace_table.json"), encoding="utf-8"))
render_of = {e["anon_id"]: e["source_render"] for e in trace["entries"]}
sheets = json.load(open(os.path.join(root, "default", "play_sheets.json"), encoding="utf-8"))
person = next(p for p, rows in sheets.items() if rows)
rows = sorted(sheets[person], key=lambda x: x["slot"])
seq = [[step["anon_id"] for step in row["sequence"]] for row in rows]
ident = [[render_of[a] for a in row] for row in seq]
assert ident[0] == ["A", "B"] and ident[1] == ["B", "A"], ident
print("  公开目录零身份；持封存表复核两序:", ident)
EOF

echo "== 4. verify 子命令复核报告 =="
$PY solve --case demo/blind_case.json --seed 7 > "$ROOT/report.json"
$PY verify --bundle "$ROOT/report.json"

echo
echo "盲听 CLI 走查全部通过，产物目录: $ROOT/default"
