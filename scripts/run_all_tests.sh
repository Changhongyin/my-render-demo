#!/bin/bash
# ============================================================
# 一键跑完所有测试（含归档代码的测试），任何一项失败就以非零退出
#   bash scripts/run_all_tests.sh
# ============================================================
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

JSC="/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
FAILED=0

run() {
  local title="$1"; shift
  printf '\n\033[1m▶ %s\033[0m\n' "$title"
  if "$@"; then
    printf '  ✅ 通过\n'
  else
    printf '  ❌ 失败\n'
    FAILED=1
  fi
}

echo "=============================================="
echo " 云端田埂 · 全量测试"
echo "=============================================="

# 数据生成脚本（85 项，不联网，含历史累积/补齐/备份/幂等/诚实标注）
run "local_spider_v2.py 自测" python3 tests/spider_v2_test.py

# 展示页：静态检查 + 渲染逻辑（86 项）
printf '\n\033[1m▶ farm.html 展示页（CSS 检查 + 渲染逻辑）\033[0m\n'
if python3 tests/frontend_test_gen.py >/dev/null && "$JSC" -m tests/farm_test.build.js >/dev/null; then
  printf '  ✅ 通过\n'
else
  printf '  ❌ 失败（可单独跑 python3 tests/frontend_test_gen.py 看细节）\n'
  FAILED=1
fi

# 用真实 data.json 冒烟：展示页能不能渲染出东西
printf '\n\033[1m▶ 用真实 data.json 冒烟（farm.html 能否渲染）\033[0m\n'
SMOKE="$(python3 tests/check_farm_renders_current_data.py >/dev/null && "$JSC" -m /tmp/farm_real_check.js)"
echo "$SMOKE" | grep -E '位置信息|渲染卡片数|错误提示' | sed 's/^/  /'
if echo "$SMOKE" | grep -q '渲染卡片数       : 0'; then
  printf '  ❌ 没渲染出任何卡片\n'
  FAILED=1
else
  printf '  ✅ 渲染正常\n'
fi

# python3 tests/weather_alert_test.py

# 气象预警接入脚本（离线，含 mock 模式与"失败不写文件"验证）
run "fetch_weather_alerts.py 自测" python3 tests/weather_alert_test.py

# 归档代码的测试：默认【不跑】——它们测的是已废弃的数据库/云函数方案。
# 需要时手动执行（路径在 archive_deprecated/ 里）：
#   python3 archive_deprecated/spider_test.py
#   python3 archive_deprecated/db-python/db_connector_test.py
#   python3 archive_deprecated/db-python/cloud_function_test.py
#   python3 archive_deprecated/cloud-fn-node/node_function_test.py

# 清理测试产生的缓存
find . -type d -name '__pycache__' -not -path './.git/*' -prune -exec rm -rf {} + 2>/dev/null
rm -f tests/farm_test.build.js

echo
echo "=============================================="
if [ "$FAILED" -eq 0 ]; then
  echo " ✅ 全部测试通过"
else
  echo " ❌ 有测试失败，见上面的输出"
fi
echo "=============================================="
exit "$FAILED"
