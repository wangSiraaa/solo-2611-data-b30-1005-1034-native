#!/usr/bin/env bash
# 一键验证：手算案例 + 数据仓案例 + 边界语义 + 试听厅校准 + 规模案例
set -euo pipefail
cd "$(dirname "$0")"
python3 -m tests.test_hand_calc /tmp/audiomix_verify_hand
python3 -m tests.test_archive
python3 -m tests.test_edge
python3 -m tests.test_hall
python3 -m demo.hall_demo /tmp/audiomix_verify_hall
bash demo/hall_walkthrough.sh > /tmp/audiomix_verify_hall_walk.log
python3 -m demo.scale_demo /tmp/audiomix_verify_scale
echo
echo "================ 全量验证通过 ================"
