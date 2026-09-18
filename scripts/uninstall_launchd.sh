#!/bin/bash
# ============================================================
# 卸载每日抓取定时任务
#   bash scripts/uninstall_launchd.sh
# ============================================================
set -uo pipefail

LABEL="com.yuntian.spider"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null \
  || launchctl unload "$PLIST_DST" 2>/dev/null \
  || true

rm -f "$PLIST_DST"

echo "✅ 已卸载 $LABEL（日志文件 spider.log / spider_error.log 保留，可自行删除）"

# -------------------- 附：crontab 备选方案 --------------------
# 如果你更习惯 cron（注意：cron 在 Mac 睡眠时不会补跑，且需要授权「完全磁盘访问」），
# 可以执行 `crontab -e` 加入下面这一行：
#
# 0 7 * * * /bin/bash /Users/user/Documents/my-render-demo/scripts/run_spider.sh
#
# 查看：crontab -l    删除：crontab -r
