#!/bin/bash
# ============================================================
# 把 node_get_data 打包成可上传腾讯云云函数的 zip
#
#   bash scripts/package_node_function.sh
#
# 入口文件就是 node_get_data/index.js（PostgreSQL 版，用 pg 驱动走内网直连）。
# 不需要选文件，也不依赖 @cloudbase/node-sdk。
#
# 前置条件：本机要有 Node.js + npm（用来装 node_modules）。
#   这台 Mac 目前【没装】Node.js，任选一种解决：
#     ① 装 Node（推荐，一次搞定，以后打包都方便）
#          brew install node        或到 https://nodejs.org 下 macOS 安装包
#     ② 不装 Node：只把 index.js + package.json 上传，
#        在云函数控制台找「在线安装依赖 / 云端安装依赖」让它装（有这个功能才行）
#     ③ 复用你现有的 get-crop-data 函数：如果它里面已有 node_modules，直接替换 index.js 即可
#
# ⚠️ 关键点：zip 里 index.js 必须在【根目录】
#    （腾讯云云函数要求入口文件在压缩包根层级，否则「执行方法」填 index.main_handler 找不到文件）
#    所以脚本是"复制到 build 目录再打包"，绝不改你的源码。
# ============================================================
set -euo pipefail

PROJECT_DIR="/Users/user/Documents/my-render-demo"
SRC_DIR="$PROJECT_DIR/node_get_data"
BUILD_DIR="$PROJECT_DIR/build/node_get_data"
ZIP_PATH="$PROJECT_DIR/node_get_data.zip"

echo "▶ 检查源文件…"
[ -f "$SRC_DIR/package.json" ] || { echo "❌ 找不到 $SRC_DIR/package.json"; exit 1; }
[ -f "$SRC_DIR/index.js" ] || { echo "❌ 找不到入口文件 $SRC_DIR/index.js"; exit 1; }

echo "▶ 检查 Node 环境…"
if ! command -v npm >/dev/null 2>&1; then
  cat <<'MSG'
❌ 本机没找到 npm，无法安装 node_modules。

   你想怎么继续？
   ① 装 Node.js 后重跑本脚本：
        brew install node
   ② 不装 Node：只上传 index.js + package.json，
      在云函数控制台找「在线安装依赖 / 依赖安装」让云端装（控制台有这项才行）
   ③ 用你那个现成的 get-crop-data 函数：如果它里面已经有 node_modules，
      可以直接把 index.js 替换进去，不用重新打包
MSG
  exit 1
fi
echo "   node 版本：$(node -v 2>/dev/null || echo '未知')"
echo "   npm  版本：$(npm -v 2>/dev/null || echo '未知')"

echo "▶ 准备构建目录（不动源码）…"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cp "$SRC_DIR/package.json" "$BUILD_DIR/"
cp "$SRC_DIR/index.js" "$BUILD_DIR/index.js"

echo "▶ 安装生产依赖…"
cd "$BUILD_DIR"
if ! npm install --omit=dev --no-audit --no-fund; then
  echo "⚠️  --omit=dev 不被支持（npm 版本较老），改用 --production 重试…"
  npm install --production --no-audit --no-fund
fi

echo "▶ 打包…"
rm -f "$ZIP_PATH"
zip -r -q "$ZIP_PATH" . -x '*.DS_Store' -x '*/__pycache__/*' -x '*.log' -x 'README.md'

echo
echo "✅ 打包完成：$ZIP_PATH"
echo "   大小：$(du -h "$ZIP_PATH" | cut -f1)"
echo "   zip 根目录的关键文件："
unzip -l "$ZIP_PATH" | grep -E ' index.js|package.json|node_modules/$' | head -5 | sed 's/^/   /'
echo
echo "接下来在云函数控制台填："
echo "   运行环境：Node.js 16.13（或 18.x）"
echo "   执行方法：index.main_handler"
echo "   提交方法：上传 zip → 选 $ZIP_PATH"

