#!/bin/bash
# ============================================================
# 把 cloud_get_data.py 打包成可直接上传腾讯云云函数的 zip
#
#   bash scripts/package_cloud_function.sh
#
# ⚠️ 关键点：psycopg2 是 C 扩展，macOS 上直接 pip install 装出来的是 Mach-O 二进制，
#    传到 Linux 云函数上会报 "invalid ELF header"，所以这里用 --platform 强制下载
#    Linux 轮子（manylinux）。PY_TARGET 必须和你在云函数控制台选的 Python 版本一致：
#        Python 3.9  → PY_TARGET=39（默认）
#        Python 3.10 → PY_TARGET=310
#        Python 3.8  → PY_TARGET=38
#    换版本就这样跑：PY_TARGET=310 bash scripts/package_cloud_function.sh
# ============================================================
set -euo pipefail

PROJECT_DIR="/Users/user/Documents/my-render-demo"
PYTHON="/usr/bin/python3"
PY_TARGET="${PY_TARGET:-39}"
PLATFORM="${PLATFORM:-manylinux2014_x86_64}"

BUILD_DIR="$PROJECT_DIR/build/cloud_get_data"
ZIP_PATH="$PROJECT_DIR/cloud_get_data.zip"

echo "▶ 清理构建目录…"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo "▶ 复制函数代码…"
cp "$PROJECT_DIR/cloud_get_data.py" "$BUILD_DIR/"

echo "▶ 写 requirements.txt（方便控制台「在线安装依赖」识别）…"
printf 'psycopg2-binary\n' > "$BUILD_DIR/requirements.txt"

echo "▶ 下载 Linux 版依赖（Python ${PY_TARGET} / ${PLATFORM}）…"
PIP_ARGS=(--target "$BUILD_DIR" --platform "$PLATFORM" --python-version "$PY_TARGET"
          --implementation cp --only-binary=:all: --upgrade --timeout 60 --retries 3)
if [ -n "${PIP_INDEX:-}" ]; then
  PIP_ARGS+=(-i "$PIP_INDEX")
  echo "  使用镜像源：$PIP_INDEX"
fi

if ! "$PYTHON" -m pip install "${PIP_ARGS[@]}" psycopg2-binary; then
  echo
  echo "❌ 依赖下载失败（多为网络问题）。国内网络建议加镜像重跑："
  echo "   PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple bash scripts/package_cloud_function.sh"
  exit 1
fi

echo "▶ 清理无用文件…"
find "$BUILD_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name '*.pyc' -delete 2>/dev/null || true
rm -rf "$BUILD_DIR"/psycopg2_binary-*.dist-info 2>/dev/null || true

echo "▶ 打成 zip…"
rm -f "$ZIP_PATH"
( cd "$BUILD_DIR" && zip -r -q "$ZIP_PATH" . )

echo
echo "✅ 打包完成：$ZIP_PATH"
echo "   大小：$(du -h "$ZIP_PATH" | cut -f1)"
echo
echo "接下来：云函数控制台 → 上传 zip → 运行时选 Python ${PY_TARGET:0:1}.${PY_TARGET:1} → 入口填 cloud_get_data.main_handler"
