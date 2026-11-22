#!/usr/bin/env bash
# 一键验证：手算案例 + 数据仓案例 + 边界语义 + 借展剪接台 + 规模案例（30 素材/100 clip/120s）
set -euo pipefail
cd "$(dirname "$0")"
python3 -m tests.test_hand_calc /tmp/audiomix_verify_hand
python3 -m tests.test_archive
python3 -m tests.test_edge
python3 -m tests.test_loan /tmp/audiomix_verify_loan
python3 -m demo.scale_demo /tmp/audiomix_verify_scale
bash demo/loan_walkthrough.sh /tmp/audiomix_verify_loan_demo >/dev/null
echo "借展剪接台 CLI 走查通过"
echo
echo "================ 全量验证通过 ================"
