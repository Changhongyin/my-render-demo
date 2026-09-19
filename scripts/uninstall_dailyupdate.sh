#!/bin/bash
# ============================================================
# 卸载每日 07:00 的「数据更新 + 云端同步」定时任务
#   bash scripts/uninstall_dailyupdate.sh
# ============================================================
set -uo pipefail

LABEL="com.yuntian.dailyupdate"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "▶ 卸载 $LABEL …"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST_DST" 2>/dev/null || true
rm -f "$PLIST_DST"

echo "✅ 已卸载（日志文件 update.log / update_error.log 保留，需要时手动删除）"
echo "   检查残留：launchctl list | grep yuntian"
