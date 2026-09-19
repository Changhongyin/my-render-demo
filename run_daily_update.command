#!/bin/bash
# ============================================================
# 双击运行入口（macOS）：在 Finder 里双击本文件即可跑完整流水线
#
#   为什么需要它：Finder 双击 .sh 默认是用编辑器打开，只有 .command 才会交给终端执行。
#   效果与 `bash run_daily_update.sh` 完全一致，跑完窗口会停住，方便你看结果。
#
# 想加参数（例如只更新本地不上传）时，请改用终端：
#   SKIP_UPLOAD=1 bash run_daily_update.sh
# ============================================================
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

bash ./run_daily_update.sh
CODE=$?

echo
if [ "$CODE" -eq 0 ]; then
  echo "✅ 完成（退出码 0）。详情：update.log"
else
  echo "❌ 失败（退出码 $CODE）。详情：update_error.log"
fi
echo
echo "本窗口不会自动关闭，按回车键退出。"
read -r _
exit "$CODE"
