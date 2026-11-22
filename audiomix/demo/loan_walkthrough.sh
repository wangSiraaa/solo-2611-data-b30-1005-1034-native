#!/usr/bin/env bash
# 借展剪接台 CLI 端到端走查：题面三个主情形 + 原单回读。
# 原盘仅一份，样本 [2,4,6,8]，每处边界可切；甲要原带 [0,2)（值5），
# 乙要 [1,3)（值4，也接受翻录 2 拍）。
set -euo pipefail
cd "$(dirname "$0")/.."
BASE=${1:-/tmp/audiomix_loan_demo}
rm -rf "$BASE"; mkdir -p "$BASE"
REG=$BASE/registry   # 素材登记仓 + 时间轴编辑器同目录（与 cli_walkthrough 一致）
VAULT=$BASE/vault
ARCH=$BASE/archive

echo "== 登记素材与实物盘（入藏号 R1，每处边界可切） =="
python3 -m audiomix.cli asset-seq --root "$REG" --rate 16000 --values 2,4,6,8 \
    --note 原带 > "$BASE/asset.json"
AID=$(python3 -c "import json;print(json.load(open('$BASE/asset.json'))['asset_id'])")
python3 -m audiomix.cli reel-register --vault "$VAULT" --registry-root "$REG" \
    --accession R1 --asset-id "$AID" --cut-bounds 0,1,2,3,4 --note 原盘仅一份

echo "== 编排节目并发布（cA 源[0,2) 输出[0,2)；cB 源[1,3) 输出[2,4)） =="
python3 -m audiomix.cli tl-insert --root "$REG" --clip-id cA --asset-id "$AID" \
    --a 0 --b 2 --start 0 >/dev/null
python3 -m audiomix.cli tl-insert --root "$REG" --clip-id cB --asset-id "$AID" \
    --a 1 --b 3 --start 1/8000 >/dev/null
python3 -m audiomix.cli tl-publish --root "$REG" --out "$BASE/product1.json"

cat > "$BASE/req.json" <<'JSON'
[{"request_id":"rA","exhibitor":"甲","clip_id":"cA","out_lo":0,"out_hi":2,"value":5},
 {"request_id":"rB","exhibitor":"乙","clip_id":"cB","out_lo":2,"out_hi":4,"value":4,
  "accept_rerecord":true}]
JSON

echo "== 剪接师核验（可作原件的盘 / 连续原样本区间 / 可落刀两端） =="
python3 -m audiomix.cli loan-analyze --vault "$VAULT" --registry-root "$REG" \
    --product "$BASE/product1.json" --requests "$BASE/req.json"

echo "== 情形一：翻录额度 2 -> 甲真带、乙翻录 [4,6] =="
python3 -m audiomix.cli loan-book --vault "$VAULT" --registry-root "$REG" \
    --archive-root "$ARCH" --product "$BASE/product1.json" \
    --requests "$BASE/req.json" --quota 2 --order-id O1
python3 - <<PY
import wave, numpy as np
with wave.open("$VAULT/loans/O1/rB.wav", "rb") as wf:
    pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
assert list(pcm) == [4, 6], pcm
print("乙的新拷贝 WAV 内容 =", list(pcm), "（源区间 [1,3) 的真实 render）")
PY

echo "== 情形二：乙的源区间改成 [2,4) 重发节目，另立库房 -> 各拿真带、只落一刀 =="
python3 -m audiomix.cli tl-modify --root "$REG" --clip-id cB --set a=2 --set b=4 >/dev/null
python3 -m audiomix.cli tl-publish --root "$REG" --out "$BASE/product2.json"
python3 -m audiomix.cli reel-register --vault "$BASE/vault2" --registry-root "$REG" \
    --accession R1 --asset-id "$AID" --cut-bounds 0,1,2,3,4 >/dev/null
python3 -m audiomix.cli loan-book --vault "$BASE/vault2" --registry-root "$REG" \
    --archive-root "$ARCH" --product "$BASE/product2.json" \
    --requests "$BASE/req.json" --quota 2 --order-id O2 \
    | python3 -c "import json,sys; o=json.load(sys.stdin); print('cuts:', o['cuts'], 'totals:', o['totals'],
          [(a['request_id'], a['mode'], a.get('source_interval')) for a in o['assignments']])"

echo "== 情形三：保持原节目、额度 1 -> 只能优先交甲，值 5 =="
python3 -m audiomix.cli reel-register --vault "$BASE/vault3" --registry-root "$REG" \
    --accession R1 --asset-id "$AID" --cut-bounds 0,1,2,3,4 >/dev/null
python3 -m audiomix.cli loan-book --vault "$BASE/vault3" --registry-root "$REG" \
    --archive-root "$ARCH" --product "$BASE/product1.json" \
    --requests "$BASE/req.json" --quota 1 --order-id O3 \
    | python3 -c "import json,sys; o=json.load(sys.stdin); print('totals:', o['totals'],
          [ (a['request_id'], a['mode']) for a in o['assignments'] ])"

echo "== 重开库房回读原单与实物账 =="
python3 -m audiomix.cli loan-order --vault "$VAULT" --order-id O1 \
    | python3 -c "import json,sys; o=json.load(sys.stdin); print('O1 totals:', o['totals'])"
python3 -m audiomix.cli vault-show --vault "$VAULT"
echo
echo "================ 借展剪接台 CLI 走查通过 ================"
