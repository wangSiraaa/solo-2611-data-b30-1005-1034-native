#!/usr/bin/env bash
# 一键验证：手算案例 + 数据仓案例 + 边界语义 + 规模案例（30 素材/100 clip/120s）
#          + 双盲听审刺激制作与两序排期（纯标准库，无 numpy 亦可运行）
set -euo pipefail
cd "$(dirname "$0")"
python3 -m tests.test_hand_calc /tmp/audiomix_verify_hand
python3 -m tests.test_archive
python3 -m tests.test_edge
python3 -m demo.scale_demo /tmp/audiomix_verify_scale
python3 -W ignore -m tests.test_blind /tmp/audiomix_verify_blind
echo
echo "================ 全量验证通过 ================"
