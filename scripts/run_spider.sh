#!/bin/bash
# ============================================================
# 每日定时任务的执行入口（由 launchd 调用，也可以手动跑）
#
#   成功 → 结果追加到 spider.log
#   失败 → 完整输出追加到 spider_error.log（并把一行摘要写进 spider.log）
#
# 手动验证：bash scripts/run_spider.sh; echo "exit=$?"
# ============================================================
set -uo pipefail          # 注意：故意不用 set -e，否则抓不到 python 的失败退出码

PROJECT_DIR="/Users/user/Documents/my-render-demo"
PYTHON="/usr/bin/python3"

cd "$PROJECT_DIR" || { echo "[$(date '+%F %T')] 无法进入项目目录 $PROJECT_DIR"; exit 1; }

LOG_OK="$PROJECT_DIR/spider.log"
LOG_ERR="$PROJECT_DIR/spider_error.log"
TS="$(date '+%Y-%m-%d %H:%M:%S')"
MAX_LINES=2000            # 每个日志只保留最近 2000 行，避免无限膨胀

OUTPUT="$("$PYTHON" "$PROJECT_DIR/local_spider_v2.py" 2>&1)"
CODE=$?

if [ "$CODE" -eq 0 ]; then
  {
    printf '[%s] ✅ 抓取成功 (exit=0)\n' "$TS"
    printf '%s\n\n' "$OUTPUT"
  } >> "$LOG_OK"
else
  {
    printf '=========================================================\n'
    printf '[%s] ❌ 抓取失败 (exit=%s)\n' "$TS" "$CODE"
    printf '%s\n\n' "$OUTPUT"
  } >> "$LOG_ERR"
  printf '[%s] ❌ 抓取失败 (exit=%s)，详情见 spider_error.log\n' "$TS" "$CODE" >> "$LOG_OK"
fi

# 日志瘦身
for f in "$LOG_OK" "$LOG_ERR"; do
  if [ -f "$f" ]; then
    tail -n "$MAX_LINES" "$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"
  fi
done

exit "$CODE"
