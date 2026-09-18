#!/bin/bash
# ============================================================
# 安装每日 07:00 的抓取定时任务（macOS launchd）
#   bash scripts/install_launchd.sh
# ============================================================
set -euo pipefail

PROJECT_DIR="/Users/user/Documents/my-render-demo"
LABEL="com.yuntian.spider"
PLIST_SRC="$PROJECT_DIR/scripts/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "▶ 检查环境…"
[ -f "$PROJECT_DIR/local_spider_v2.py" ] || { echo "❌ 找不到 local_spider_v2.py，路径不对？"; exit 1; }
[ -f "$PLIST_SRC" ] || { echo "❌ 找不到 $PLIST_SRC"; exit 1; }
command -v launchctl >/dev/null || { echo "❌ 这不是 macOS？"; exit 1; }

# 校验 plist 里的路径是否与当前项目位置一致
if ! grep -q "$PROJECT_DIR" "$PLIST_SRC"; then
  echo "❌ plist 里的路径与当前项目目录不一致，请先把 $PLIST_SRC 里的路径改成：$PROJECT_DIR"
  exit 1
fi

echo "▶ 准备目录与权限…"
mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs" "$PROJECT_DIR/backups"
chmod +x "$PROJECT_DIR/scripts/run_spider.sh"

echo "▶ 安装 plist…"
cp "$PLIST_SRC" "$PLIST_DST"

echo "▶ 重新加载任务…"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if ! launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>/dev/null; then
  # 老版本 macOS 的兜底写法
  launchctl unload "$PLIST_DST" 2>/dev/null || true
  launchctl load -w "$PLIST_DST"
fi
launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null || true

echo
echo "✅ 已完成。任务信息："
launchctl list 2>/dev/null | grep -E "^PID|$LABEL" || launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E 'state|next' || true
echo
echo "下次自动运行：每天 07:00（睡眠中会等唤醒后补跑）"
echo "立即试跑一次：launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "查看失败日志：cat $PROJECT_DIR/spider_error.log"
echo "查看运行日志：cat $PROJECT_DIR/spider.log"
