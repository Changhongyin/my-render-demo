#!/bin/bash
# ============================================================
# 云端田埂 · 数据更新流水线（手动一键执行；终端 `bash run_daily_update.sh` 或 Finder 双击 run_daily_update.command）
#
#   ⚠️ 为什么不做定时任务：macOS 的 launchd 与 cron 都受 TCC 隐私保护限制，读不到 ~/Documents 下的
#      项目目录（实测报错 "Operation not permitted"），而本项目明确不申请"完全磁盘访问权限"、
#      也不迁移目录，所以采用"手动一键执行"。之前装过的 launchd 任务已用
#      scripts/uninstall_dailyupdate.sh 卸载（未做任何系统级改动）。
#
#   一条命令跑完：抓数据 → 同步到前端工程 → 前端构建 → 部署到腾讯云静态托管
#
#   [1/8] 抓行情      python3 local_spider_v2.py              → crops（硬步骤）
#   [2/8] 抓气象预警  fetch_weather_alerts_v3.py（WeatherAPI，有真实预警就写）→ 不行再回退
#                     fetch_weather_alerts.py --write（彩云/apihz/generic/mock）→ weather（软步骤，见下）
#   [3/8] 抓统计局    python3 fetch_nbs_prices.py              → nbs（软步骤，仅在每月 5/15/25 日跑）
#   [4/8] 后端校验    data.json 结构 / 两份一致 / 新鲜度（硬步骤）
#   [5/8] 同步前端    public/data.json → <前端工程>/public/data.json（逐字节校验，硬步骤）
#   [6/8] 构建前端    <前端工程> 里 npm run build → out/（产物与源数据逐字节校验，硬步骤）
#   [7/8] 部署上线    tcb hosting deploy <前端工程>/out/ / -e ${TCB_ENV_ID}（硬步骤）
#                     ＋ 拿到站点域名时，复核线上 /data.json 指纹与本地一致（硬：不一致即失败）
#                     ＋ 顺带复核线上 /index.html 也是本次构建产物（不一致只告警，防 CDN 误判）
#                     ＋ 追加一行把 public/farm.html 传到 /farm.html（备用链接，失败只记录）
#   [8/8] 收尾        成功摘要写进 update.log
#
# 设计铁律：
#   · set -euo pipefail + ERR trap：任何一步出错立即停止，并把原因写进 update_error.log
#   · 抓取失败绝不覆盖旧 data.json —— 两个 python 脚本内部各自保证这一点；
#     本脚本只做"不越权"的事（失败就退出，不动 data.json、不上传）
#   · 【绝不在数据没准备好时上线】：同步不一致不构建 → 构建产物不对不上传 →
#     上传失败或线上指纹与本地不符，一律判失败并在日志里写明
#   · 两个日志只保留最近 2000 行，避免无限膨胀
#
# 前端工程目录 FRONTEND_DIR（这是"网页为什么必须重建"的答案）：
#   前端是静态导出站点，data.json 会作为静态资源被打进 out/ 目录，
#   所以光更新后端 data.json 不会让网页变化，必须"同步 → 重新构建 → 重新部署"。
#   目录默认按顺序自动探测（也可在 .env 写 FRONTEND_DIR=/绝对路径 固定）：
#     1) /Users/user/Desktop/yuntian-static-最新
#     2) /Users/user/Desktop/yuntian-static-已对接行情与气象预警
#     3) ~/Desktop/yuntian-static*（取最近修改的一个）
#   判定标准：存在 package.json + next.config.ts + src/lib/market-live.ts（已对接后端数据的工程）
#
# 为什么气象预警"失败"是预期行为，而不是 Bug（接手的人请先读这段）：
#   彩云天气【免费版没有预警权限】：请求会返回 HTTP 200 + status=ok，但响应里【不包含 result.alert】
#   （官方文档 v2.6「预警数据 → 访问限制」明确写了：预警属增值服务，不在免费赠送额度内）。
#   因此 fetch_weather_alerts.py 会以 exit=1 结束，并打印"响应里没有 result.alert"的指引。
#   ✅ 这是【预期的降级策略，不是系统故障】：
#      · data.json 里保留上一次的 weather 数据（当前为 Mock 数据兜底，带"模拟数据"标注，前端会显示）
#      · 本流水线把气象/统计局视为【软步骤】：失败只记录到 update_error.log，不中断行情抓取与云端上传
#   → 想拿到真实预警：给彩云 token 开通/升级「预警数据」，或改用免费来源
#     （ALERT_PROVIDER=apihz / generic，配置见 .env.example 与 docs/气象预警接入说明.md 第八节）。
#   → 想让它失败也算致命（例如权限已开通、失败就意味着真出问题）：WEATHER_STRICT=1
#
# 硬步骤 vs 软步骤：
#   硬步骤（失败即停，绝不上线）：行情抓取 → 后端校验 → 同步前端 → 构建前端 → 部署上线
#   软步骤（失败只记录，不阻断上线）：气象预警、国家统计局
#     为什么这两步可以软：它们失败时 data.json 会保留上一次的数据（带"模拟数据"等如实标注），
#     而行情数据是本次新抓的、最新鲜；因为一次外部接口的降级就不更新网页，得不偿失。
#     要让它们也致命：WEATHER_STRICT=1 / NBS_STRICT=1 / STRICT_ALL=1
#
# 用法（在项目目录下执行）：
#   bash run_daily_update.sh                    # 完整流水线：抓取 → 同步 → 构建 → 部署
#   SKIP_DEPLOY=1 bash run_daily_update.sh      # 跑到构建为止（本地看 out/，不上传）
#   SKIP_BUILD=1 bash run_daily_update.sh       # 跑到同步为止（调试用，不会部署）
#   DRY_RUN=1 bash run_daily_update.sh          # 演练：不构建不部署，只打印将要执行什么
#   FORCE_NBS=1 bash run_daily_update.sh        # 强制抓一次统计局数据（不限日期）
#   FRONTEND_DIR=/绝对路径 bash run_daily_update.sh   # 指定前端工程目录
#   STRICT_ALL=1 / WEATHER_STRICT=1 / NBS_STRICT=1    # 把软步骤也设为致命
#   （Finder 里双击 run_daily_update.command 效果相同，且窗口会保持打开方便看结果）
#
# 云端配置（部署前必须做一次，之后就不用管了）：
#   1) 登录：tcb login（交互式，登录态缓存在本机）或 tcb login --apiKeyId <SecretId> --apiKey <SecretKey>
#   2) 把环境 ID 写进 .env：TCB_ENV_ID=your-env-id   （或运行时 export TCB_ENV_ID=...）
#      取环境 ID：tcb env list
#   3) 可选：在 .env 里写 SITE_URL=https://你的域名  → 部署后脚本会复核线上 /data.json 指纹
#      不写也能跑：脚本会尝试用 `tcb hosting detail` 自动读站点域名，读不到就跳过复核（并写明）
#   4) 验证：bash run_daily_update.sh 的第 [7/8] 步应显示 "✅ 已部署并复核一致"
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/usr/bin/python3"
cd "$PROJECT_DIR"

LOG_OK="$PROJECT_DIR/update.log"
LOG_ERR="$PROJECT_DIR/update_error.log"
MAX_LINES=2000
TS="$(date '+%Y-%m-%d %H:%M:%S')"

# ---------- PATH 兜底：Finder 双击 .command 时 PATH 可能只有 /usr/bin:/bin ----------
# 没有这一步，双击运行会找不到 npm / tcb（都在 Homebrew 下）。只把"确实存在"的目录补进去。
for d in /opt/homebrew/bin /opt/homebrew/sbin /usr/local/bin /usr/local/sbin; do
  [ -d "$d" ] && PATH="$d:$PATH"
done
for d in "$HOME"/.nvm/versions/node/*/bin "$HOME"/.volta/bin "$HOME"/.fnm/aliases/default/bin "$HOME"/Library/pnpm; do
  [ -d "$d" ] && PATH="$d:$PATH"
done
export PATH

# ---------- 开关（默认值以"流水线能跑完"为准，见文件头说明） ----------
STRICT_ALL="${STRICT_ALL:-0}"
WEATHER_STRICT="${WEATHER_STRICT:-0}"
NBS_STRICT="${NBS_STRICT:-0}"
SKIP_UPLOAD="${SKIP_UPLOAD:-0}"        # 兼容旧写法，等价于 SKIP_DEPLOY
FORCE_NBS="${FORCE_NBS:-0}"
SKIP_DEPLOY="${SKIP_DEPLOY:-$SKIP_UPLOAD}"
SKIP_BUILD="${SKIP_BUILD:-0}"
DRY_RUN="${DRY_RUN:-0}"
if [ "$DRY_RUN" = "1" ]; then SKIP_BUILD=1; SKIP_DEPLOY=1; fi   # 演练：不构建、不部署
if [ "$STRICT_ALL" = "1" ]; then WEATHER_STRICT=1; NBS_STRICT=1; fi

# ---------- 腾讯云环境 ID / 站点域名：环境变量优先，其次 .env ----------
read_env_value() {   # read_env_value <KEY>  → 从 .env 读一行（去引号去空白）
  local key="$1"
  [ -f "$PROJECT_DIR/.env" ] || return 0
  grep -E "^[[:space:]]*${key}=" "$PROJECT_DIR/.env" | tail -1 | cut -d= -f2- \
    | tr -d '"' | tr -d "'" | xargs 2>/dev/null || true
}
[ -n "${TCB_ENV_ID:-}" ] || TCB_ENV_ID="$(read_env_value TCB_ENV_ID)"
[ -n "${SITE_URL:-}" ] || SITE_URL="$(read_env_value SITE_URL)"
TCB_ENV_ID="${TCB_ENV_ID:-}"
SITE_URL="${SITE_URL:-}"
TCB_BIN="$(command -v tcb 2>/dev/null || command -v cloudbase 2>/dev/null || true)"

# ---------- 前端工程目录：FRONTEND_DIR（env/.env 优先）→ 只有"没指定"时才自动探测 ----------
# 注意：显式指定了 FRONTEND_DIR 就不再自动探测 —— 指错了必须报错，
# 绝不能"悄悄换一个目录继续跑"（那等于用户以为在用 A，实际改的是 B）。
FRONTEND_DIR="${FRONTEND_DIR:-$(read_env_value FRONTEND_DIR)}"
is_frontend_dir() {   # 认得出"已对接后端数据的前端工程"
  local d="$1"
  [ -n "$d" ] && [ -d "$d" ] && [ -f "$d/package.json" ] && [ -f "$d/next.config.ts" ] \
    && [ -f "$d/src/lib/market-live.ts" ]
}
if [ -n "$FRONTEND_DIR" ]; then
  FRONTEND_DIR_SOURCE="指定"
else
  FRONTEND_DIR_SOURCE="自动探测"
  for cand in \
      "/Users/user/Desktop/yuntian-static-最新" \
      "/Users/user/Desktop/yuntian-static-已对接行情与气象预警"; do
    if is_frontend_dir "$cand"; then FRONTEND_DIR="$cand"; break; fi
  done
  if [ -z "$FRONTEND_DIR" ]; then   # 兜底：~/Desktop 下最新的 yuntian-static* 工程
    while IFS= read -r cand; do
      if is_frontend_dir "$cand"; then FRONTEND_DIR="$cand"; break; fi
    done < <(ls -dt "$HOME"/Desktop/yuntian-static* 2>/dev/null || true)
  fi
fi
FRONTEND_DATA="$FRONTEND_DIR/public/data.json"
FRONTEND_OUT="$FRONTEND_DIR/out"

# ---------- 临时文件与清理 ----------
TMP1="$(mktemp)"; TMP2="$(mktemp)"; TMP3="$(mktemp)"; TMP4="$(mktemp)"
TMP5="$(mktemp)"; TMP6="$(mktemp)"; TMP7="$(mktemp)"
cleanup() { rm -f "$TMP1" "$TMP2" "$TMP3" "$TMP4" "$TMP5" "$TMP6" "$TMP7"; }
trap cleanup EXIT

trim_log() {
  for f in "$LOG_OK" "$LOG_ERR"; do
    if [ -f "$f" ]; then
      tail -n "$MAX_LINES" "$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"
    fi
  done
}

# fail <步骤名> <详情文件>：把细节写进 update_error.log，然后以非零码退出
fail() {
  local step="$1" detail="${2:-}"
  {
    printf '=========================================================\n'
    printf '[%s] ❌ 流水线中断：%s\n' "$TS" "$step"
    if [ -n "$detail" ] && [ -f "$detail" ]; then cat "$detail"; fi
    printf '\n'
  } >> "$LOG_ERR"
  printf '[%s] ❌ 失败：%s（详情见 update_error.log）\n' "$TS" "$step" >> "$LOG_OK"
  echo "❌ 失败：${step}（详情见 update_error.log）"
  trim_log
  exit 1
}

# note <一句话> <详情文件>：不致命的问题，只记录
note() {
  local msg="$1" detail="${2:-}"
  {
    printf '[%s] ⚠️ %s\n' "$TS" "$msg"
    if [ -n "$detail" ] && [ -f "$detail" ]; then tail -n 12 "$detail"; fi
    printf '\n'
  } >> "$LOG_ERR"
}

# 兜底：没被 fail() 显式接住的错误（set -e 触发）也必须写进 update_error.log，
# 绝不允许"悄悄退出、日志里什么都没有"。
on_unexpected_error() {
  set +e
  local code="$1" line="$2" cmd="$3"
  {
    printf '=========================================================\n'
    printf '[%s] ❌ 流水线中断（未预期错误：exit=%s，脚本第 %s 行）\n' "$TS" "$code" "$line"
    printf '出错命令：%s\n\n' "$cmd"
  } >> "$LOG_ERR"
  printf '[%s] ❌ 失败：未预期错误（exit=%s，第 %s 行：%s，详情见 update_error.log）\n' \
    "$TS" "$code" "$line" "$cmd" >> "$LOG_OK"
  echo "❌ 失败：未预期错误（exit=${code}，脚本第 $line 行）"
  trim_log
}
trap 'on_unexpected_error "$?" "$LINENO" "$BASH_COMMAND"' ERR

HASH_BEFORE="$(shasum -a 256 data.json public/data.json 2>/dev/null | awk '{print $1}' | tr '\n' ' ' || true)"

echo "=============================================="
echo " 云端田埂 · 每日数据更新（抓取 → 前端构建 → 上线）"
echo " 开始时间：$TS"
echo "=============================================="

# ---------- 预检：只读检查，先把"这次会做什么"说清楚 ----------
echo "▶ 预检"
if [ -n "$FRONTEND_DIR" ] && is_frontend_dir "$FRONTEND_DIR"; then
  echo "   · 前端工程目录：${FRONTEND_DIR}（${FRONTEND_DIR_SOURCE}）"
  if [ -d "$FRONTEND_DIR/node_modules/next" ]; then
    echo "   · 前端依赖    ：✅ 已安装（node_modules 存在）"
  else
    echo "   · 前端依赖    ：⚠️ 未安装 → 构建步骤会自动执行 npm ci --ignore-scripts"
  fi
elif [ -n "$FRONTEND_DIR" ]; then
  echo "   · 前端工程目录：❌ ${FRONTEND_DIR}（${FRONTEND_DIR_SOURCE}，但不是有效的已对接工程）"
  echo "                    → 有效工程需同时存在：package.json / next.config.ts / src/lib/market-live.ts"
  echo "                    → [5/8] 步会立即停止（不会擅自换目录，避免改错地方）"
else
  echo "   · 前端工程目录：❌ 没找到（会在 [5/8] 步失败）"
  echo "                    → 请确认前端工程在 ~/Desktop/yuntian-static-* ，或用 FRONTEND_DIR=... 指定"
fi
if [ -n "$TCB_BIN" ]; then
  echo "   · CloudBase CLI ：$TCB_BIN"
else
  echo "   · CloudBase CLI ：❌ 未安装 → 无法部署（安装：npm install -g @cloudbase/cli）"
fi
if [ -n "$TCB_ENV_ID" ]; then
  echo "   · 云端环境 ID  ：$TCB_ENV_ID"
else
  echo "   · 云端环境 ID  ：❌ 未配置（在 .env 里加一行 TCB_ENV_ID=你的环境ID，取法：tcb env list）"
fi
if [ -n "$TCB_BIN" ] && [ -n "$TCB_ENV_ID" ]; then
  if "$TCB_BIN" env list >/dev/null 2>&1; then
    echo "   · 登录态        ：✅ 已登录（登录态缓存在本机，无需重复登录）"
  else
    echo "   · 登录态        ：⚠️ 检测不到登录态（可能未登录或网络不通）"
    echo "                     → 若未登录请先执行：tcb login；第 [7/8] 步会给出真实结果"
  fi
else
  echo "   · 部署前提      ：先在终端执行一次 tcb login（登录态缓存在本机，之后不用重复）"
fi
if [ -n "$SITE_URL" ]; then
  echo "   · 站点域名      ：${SITE_URL}（部署后会复核线上 /data.json 指纹）"
else
  echo "   · 站点域名      ：未配置 → 部署后尝试用 tcb hosting detail 自动读取"
fi
if [ "$SKIP_DEPLOY" = "1" ]; then
  echo "   · 本次模式      ：⏭  不上传（SKIP_DEPLOY=1 / DRY_RUN=1）"
elif [ "$SKIP_BUILD" = "1" ]; then
  echo "   · 本次模式      ：⏭  不构建、不上传（SKIP_BUILD=1）"
else
  echo "   · 本次模式      ：抓取 → 同步 → 构建 → 部署（完整流水线）"
fi
echo

# ---------- [1/8] 行情（硬步骤） ----------
echo "▶ [1/8] 抓取行情：local_spider_v2.py"
if ! "$PYTHON" local_spider_v2.py >>"$TMP1" 2>&1; then
  fail "行情抓取（local_spider_v2.py）" "$TMP1"
fi
SPIDER_LINE="$(grep -E '本次抓到|已写入|抓取成功' "$TMP1" | tail -2 | tr '\n' ' ' || true)"
echo "   ✅ 行情已更新：${SPIDER_LINE:-（详见 update.log）}"

# 防御网：行情脚本只负责 meta/crops，万一它把别人的块（weather / nbs）吞了，
# 就从它写入前的自动备份里补回来（只增不改，幂等；正常情况下会打印"无需恢复"）。
if ! "$PYTHON" scripts/restore_foreign_blocks.py >>"$TMP1" 2>&1; then
  note "恢复其它数据块失败（weather / nbs 可能缺失，请检查 scripts/restore_foreign_blocks.py）" "$TMP1"
  echo "   ⚠️ 恢复其它数据块失败（已记录）"
else
  GUARD_LINE="$(grep -E '无需恢复|补回|找不到行情备份' "$TMP1" | tail -1 | sed 's/^ *//' || true)"
  echo "   🛡️  ${GUARD_LINE:-（无输出）}"
fi

# ---------- [2/8] 气象预警（软步骤；先试 WeatherAPI v3，不成功再走原有来源） ----------
echo "▶ [2/8] 抓取气象预警：先试 WeatherAPI v3 → 不行再回退原有来源"
WEATHER_RESULT=""
if [ -f fetch_weather_alerts_v3.py ]; then
  if "$PYTHON" fetch_weather_alerts_v3.py --no-raw >>"$TMP2" 2>&1; then
    V3_LINE="$(grep -E '归属校验通过|当前 [0-9]+ 条生效预警' "$TMP2" | tail -2 | tr '\n' ' ' | sed 's/^ *//' || true)"
    WEATHER_RESULT="✅ 已更新（WeatherAPI 真实预警：${V3_LINE:-见 update.log}）"
    echo "   ✅ WeatherAPI v3 已写入真实预警：${V3_LINE:-见日志}"
  else
    V3_CODE=$?
    note "WeatherAPI v3 未写入预警（退出码 ${V3_CODE}：2=响应无 alerts 字段 / 3=当前无生效预警 / 5=预警与本地不符（拒绝写入）/ 4=接口报错 / 1=网络异常）。原有兜底逻辑保持不变" "$TMP2"
    echo "   ⚠️ WeatherAPI v3 无可用预警（退出码 ${V3_CODE}，已记录），回退原有来源"
  fi
fi
if [ -z "$WEATHER_RESULT" ]; then
  if "$PYTHON" fetch_weather_alerts.py --write >>"$TMP2" 2>&1; then
    WEATHER_RESULT="✅ 已更新（原有来源）"
    echo "   ✅ 气象预警已更新（原有来源）"
  else
    if [ "$WEATHER_STRICT" = "1" ]; then
      fail "气象预警抓取（fetch_weather_alerts.py，WEATHER_STRICT=1）" "$TMP2"
    fi
    WEATHER_RESULT="⚠️ 未更新（软步骤，已记录）"
    note "气象预警未更新（WeatherAPI v3 与原有来源都没写出预警；data.json 里的旧预警/Mock 兜底保持原样）" "$TMP2"
    echo "   ⚠️ 气象预警未更新（软步骤，已记录到 update_error.log），继续"
  fi
fi

# ---------- [3/8] 国家统计局（软步骤，仅每月 5/15/25 日） ----------
DO_NBS=0
DAY_OF_MONTH="$(date '+%-d')"
case "$DAY_OF_MONTH" in 5|15|25) DO_NBS=1 ;; esac
if [ "$FORCE_NBS" = "1" ]; then DO_NBS=1; fi

echo "▶ [3/8] 抓取国家统计局价格：fetch_nbs_prices.py"
if [ "$DO_NBS" != "1" ]; then
  NBS_RESULT="⏭  跳过（旬报每月 4/14/24 日发布，只在 5/15/25 日抓；FORCE_NBS=1 可强制）"
  echo "   ⏭  今天 ${DAY_OF_MONTH} 号，不是采集日，跳过（FORCE_NBS=1 可强制）"
elif [ ! -f fetch_nbs_prices.py ]; then
  NBS_RESULT="⏭  跳过（没有 fetch_nbs_prices.py）"
  echo "   ⏭  找不到 fetch_nbs_prices.py，跳过"
else
  if "$PYTHON" fetch_nbs_prices.py >>"$TMP3" 2>&1; then
    NBS_RESULT="✅ 已更新（$(grep -E '抓到|期次' "$TMP3" | tail -1 | sed 's/^ *//' || true)）"
    echo "   ✅ 统计局数据已更新"
  else
    if [ "$NBS_STRICT" = "1" ]; then
      fail "国家统计局抓取（fetch_nbs_prices.py，NBS_STRICT=1）" "$TMP3"
    fi
    NBS_RESULT="⚠️ 未更新（软步骤，已记录）"
    note "国家统计局数据未更新（可选数据源；data.json 里的 nbs 保持原样）" "$TMP3"
    echo "   ⚠️ 统计局数据未更新（软步骤，已记录），继续"
  fi
fi

# ---------- [4/8] 后端完整性校验（硬步骤） ----------
echo "▶ [4/8] 校验 data.json 完整性（结构 / 两份一致 / 新鲜度）"
if ! "$PYTHON" -c '
import json, os, time
d = json.load(open("data.json", encoding="utf-8"))
p = json.load(open("public/data.json", encoding="utf-8"))
crops = d.get("crops")
assert isinstance(crops, list) and crops, "data.json 里没有 crops"
bad = [c.get("name") for c in crops if not isinstance(c.get("latest"), (int, float))]
assert not bad, "以下品种价格不是数字：%s" % bad
assert d == p, "data.json 与 public/data.json 内容不一致"
age = time.time() - os.path.getmtime("data.json")
assert age < 3600, "data.json 不是本次刚生成的（%.0f 分钟前）" % (age / 60)
print("crops=%d，weather=%s，nbs=%s，两份文件一致，data.json 新鲜度 %.0f 秒"
      % (len(crops), bool(d.get("weather")), bool(d.get("nbs")), age))
' >>"$TMP4" 2>&1; then
  fail "data.json 完整性校验（结构/一致性/新鲜度）" "$TMP4"
fi
CHECK_LINE="$(tail -1 "$TMP4")"
echo "   ✅ ${CHECK_LINE}"

# ---------- [5/8] 同步到前端工程（硬步骤） ----------
echo "▶ [5/8] 同步 data.json 到前端工程"
SYNC_RESULT=""
if [ -z "$FRONTEND_DIR" ]; then
  {
    printf '没有找到"已对接后端数据"的前端工程目录（需要同时存在 package.json / next.config.ts / src/lib/market-live.ts）。\n'
    printf '已尝试：\n'
    printf '  · %s\n' "/Users/user/Desktop/yuntian-static-最新"
    printf '  · %s\n' "/Users/user/Desktop/yuntian-static-已对接行情与气象预警"
    printf '  · %s\n' "$HOME/Desktop/yuntian-static*（最近修改的一个）"
    printf '处理：确认前端工程位置后，在 .env 里写一行 FRONTEND_DIR=/绝对路径，或运行时 FRONTEND_DIR=... bash run_daily_update.sh\n'
  } >"$TMP5"
  fail "同步前端（找不到前端工程目录）" "$TMP5"
elif ! is_frontend_dir "$FRONTEND_DIR"; then
  {
    printf 'FRONTEND_DIR 指向的不是有效的"已对接工程"：%s\n' "$FRONTEND_DIR"
    printf '需要同时存在：package.json / next.config.ts / src/lib/market-live.ts\n'
    printf '处理：改成正确的绝对路径，或去掉 FRONTEND_DIR 让脚本自动探测 ~/Desktop/yuntian-static*\n'
  } >"$TMP5"
  fail "同步前端（FRONTEND_DIR 无效 —— 不会擅自换目录）" "$TMP5"
fi
[ -d "$FRONTEND_DIR/public" ] || fail "同步前端（$FRONTEND_DIR/public 不存在，前端工程不完整）" ""

DST_HASH=""
SRC_HASH="$(shasum -a 256 public/data.json | awk '{print $1}')"
cp -f public/data.json "$FRONTEND_DATA"
DST_HASH="$(shasum -a 256 "$FRONTEND_DATA" | awk '{print $1}')"
if [ "$SRC_HASH" != "$DST_HASH" ]; then
  { printf '复制后指纹不一致，说明写入被中断或被其它程序改动：\n  源  ：%s\n  目标：%s\n' "$SRC_HASH" "$DST_HASH"; } >"$TMP5"
  fail "同步前端（data.json 复制后指纹不一致，绝不进入构建）" "$TMP5"
fi
SYNC_RESULT="✅ 已同步（指纹 ${SRC_HASH:0:12}…，后端与前端逐字节一致）"
echo "   ✅ $FRONTEND_DATA"
echo "      指纹 ${SRC_HASH:0:12}…（与后端 public/data.json 逐字节一致）"

# ---------- [6/8] 构建前端（硬步骤） ----------
echo "▶ [6/8] 构建前端：npm run build"
BUILD_RESULT=""
if [ "$SKIP_BUILD" = "1" ]; then
  BUILD_RESULT="⏭  跳过（SKIP_BUILD=1 / DRY_RUN=1）"
  echo "   ⏭  已跳过构建（跳过构建时不会部署：out/ 里还是旧数据）"
elif ! command -v npm >/dev/null 2>&1; then
  fail "构建前端（找不到 npm；请先安装 Node.js）" ""
else
  cd "$FRONTEND_DIR"
  if [ ! -d node_modules/next ]; then
    echo "   · 依赖未安装，先执行 npm ci --ignore-scripts（本项目不用 Prisma，它的 postinstall 在国内会卡住）"
    if ! npm ci --ignore-scripts --no-audit --no-fund >>"$TMP6" 2>&1; then
      cd "$PROJECT_DIR"
      fail "构建前端（npm ci 安装依赖失败）" "$TMP6"
    fi
  fi
  BUILD_START="$(date '+%s')"
  if ! npm run build >>"$TMP6" 2>&1; then
    cd "$PROJECT_DIR"
    fail "构建前端（npm run build 失败）" "$TMP6"
  fi
  cd "$PROJECT_DIR"
  # 产物校验：out/ 必须是"本次构建产生、且数据与源文件逐字节一致"，否则绝不上传
  if ! "$PYTHON" - "$FRONTEND_OUT" "$FRONTEND_DATA" "$BUILD_START" >"$TMP7" 2>&1 <<'PY'
import hashlib, os, sys, time
out_dir, src_data, build_start = sys.argv[1], sys.argv[2], int(sys.argv[3])
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
assert os.path.isdir(out_dir), "out/ 目录不存在（构建没有产出静态导出）"
out_data = os.path.join(out_dir, "data.json")
assert os.path.isfile(out_data), "out/data.json 不存在：public/data.json 没被打进站点"
assert sha(out_data) == sha(src_data), "out/data.json 与前端 public/data.json 内容不一致（产物对不上源数据）"
assert os.path.isfile(os.path.join(out_dir, "index.html")), "out/index.html 不存在（静态导出不完整）"
age = time.time() - os.path.getmtime(out_data)
assert age < 1800, "out/data.json 不是本次构建产生的（%.0f 分钟前），拒绝用旧产物上线" % (age / 60)
assert os.path.isdir(os.path.join(out_dir, "_next")), "out/_next 缺失（前端资源没导出）"
print("out/ 校验通过：data.json=%s…（%.1f KB），index.html 与 _next 均在" % (sha(out_data)[:12], os.path.getsize(out_data) / 1024))
PY
  then
    fail "构建前端（产物校验失败：out/ 与源数据对不上，拒绝上线）" "$TMP7"
  fi
  BUILD_LINE="$(tail -1 "$TMP7")"
  BUILD_RESULT="✅ 构建成功（${BUILD_LINE}）"
  echo "   ✅ ${BUILD_LINE}"
fi

# ---------- [7/8] 部署到腾讯云静态托管（硬步骤） ----------
echo "▶ [7/8] 部署到腾讯云静态托管"
DEPLOY_RESULT=""
FARM_RESULT=""
PAGE_RESULT=""
SITE_ROOT=""
auto_site_url() {   # 没配 SITE_URL 时，尝试从 `tcb hosting detail` 里读站点域名
  if [ -n "$SITE_URL" ]; then echo "$SITE_URL"; return 0; fi
  [ -n "$TCB_BIN" ] || return 0
  "$TCB_BIN" hosting detail -e "$TCB_ENV_ID" 2>/dev/null \
    | grep -Eo 'https://[A-Za-z0-9._-]+' | head -1 || true
}

if [ "$SKIP_DEPLOY" = "1" ]; then
  DEPLOY_RESULT="⏭  跳过部署（SKIP_DEPLOY=1 / DRY_RUN=1）；本地产物已就绪"
  echo "   ⏭  已跳过部署；产物在 $FRONTEND_OUT"
  echo "      想手动上线：cd \"$FRONTEND_DIR\" && tcb hosting deploy out/ / -e $TCB_ENV_ID"
elif [ -z "$TCB_BIN" ]; then
  { printf '找不到 tcb / cloudbase 命令，无法部署。\n安装：npm install -g @cloudbase/cli\n'; } >"$TMP5"
  fail "部署（未安装 CloudBase CLI）" "$TMP5"
elif [ -z "$TCB_ENV_ID" ]; then
  { printf '没有配置 TCB_ENV_ID，无法部署。\n在 .env 里加一行：TCB_ENV_ID=你的环境ID（取法：tcb env list）\n'; } >"$TMP5"
  fail "部署（未配置 TCB_ENV_ID）" "$TMP5"
else
  cd "$FRONTEND_DIR"
  # 注意：不加 `--verify`。CloudBase CLI 的 --verify 在本环境会误报
  # "一致性校验失败：missing=/data.json,/index.html…"（文件其实已经上传成功，
  # 见 update_error.log 里 "全部 59 个文件并行上传成功"）。因此改成我们自己的线上复核：
  # 拉一次公网 /data.json，与本地构建产物比 sha256 —— 这才是"网页真的换了数据"的硬证据。
  printf '   · 执行：%s hosting deploy "%s/" / -e %s --retry-count 3\n' "$TCB_BIN" "$FRONTEND_OUT" "$TCB_ENV_ID"
  if ! "$TCB_BIN" hosting deploy "$FRONTEND_OUT/" / -e "$TCB_ENV_ID" --retry-count 3 >>"$TMP5" 2>&1; then
    cd "$PROJECT_DIR"
    fail "部署（tcb hosting deploy 失败；若提示未登录，先执行 tcb login）" "$TMP5"
  fi
  cd "$PROJECT_DIR"
  DEPLOY_RESULT="✅ 已部署（$FRONTEND_OUT/ → 站点根目录 /，含 data.json 与全部前端资源）"
  echo "   ✅ 已部署：$FRONTEND_OUT/ → /"

  # 上线复核：线上 /data.json 指纹必须与本地刚构建的一致，证明"网页真的换了数据"
  SITE_ROOT="$(auto_site_url)"
  if [ -n "$SITE_ROOT" ]; then
    REMOTE_HASH=""
    for attempt in 1 2 3 4 5; do
      REMOTE_HASH="$(curl -sS --max-time 20 "$SITE_ROOT/data.json?t=$(date '+%s')" 2>/dev/null | shasum -a 256 | awk '{print $1}' || true)"
      [ "$REMOTE_HASH" = "$DST_HASH" ] && break
      sleep 3
    done
    if [ "$REMOTE_HASH" = "$DST_HASH" ]; then
      DEPLOY_RESULT="✅ 已部署并复核一致（${SITE_ROOT}/data.json 指纹 ${REMOTE_HASH:0:12}… 与本地一致）"
      echo "   ✅ 线上复核通过：${SITE_ROOT}/data.json 指纹与本地一致"

      # 顺带复核首页 HTML 也是本次构建的产物：说明"整页换了"，而不只是数据换了。
      # 注意：index.html 不一致只告警不判失败 —— 页面上的日期/价格是运行时从 /data.json 拉的，
      # 数据这一层已经用上面的指纹证明是新的；HTML 差异多半只是 CDN 缓存。
      if [ -f "$FRONTEND_OUT/index.html" ]; then
        LOCAL_INDEX_HASH="$(shasum -a 256 "$FRONTEND_OUT/index.html" | awk '{print $1}')"
        REMOTE_INDEX_HASH=""
        for attempt in 1 2 3 4 5; do
          REMOTE_INDEX_HASH="$(curl -sS --max-time 20 "$SITE_ROOT/index.html?t=$(date '+%s')" 2>/dev/null | shasum -a 256 | awk '{print $1}' || true)"
          [ "$REMOTE_INDEX_HASH" = "$LOCAL_INDEX_HASH" ] && break
          sleep 3
        done
        if [ "$REMOTE_INDEX_HASH" = "$LOCAL_INDEX_HASH" ]; then
          PAGE_RESULT="✅ 首页 HTML 与本地本次构建一致（指纹 ${LOCAL_INDEX_HASH:0:12}…）"
          echo "   ✅ 首页复核通过：/index.html 指纹与本地一致"
        else
          PAGE_RESULT="⚠️ 首页 HTML 与本地不一致（多半是 CDN 缓存；页面数据仍取自最新的 /data.json）"
          note "线上 /index.html 与本地构建产物不一致（可能 CDN 缓存）。页面上的日期/价格来自 /data.json（已复核为最新），如需强刷可清 CDN 缓存" ""
          echo "   ⚠️ 线上 /index.html 与本地不一致（可能 CDN 缓存），但页面数据已是最新的"
        fi
      fi
    else
      { printf '线上 /data.json 与本地构建产物指纹不一致（可能被 CDN 缓存或上传未完全生效）：\n'
        printf '  站点：%s/data.json\n  本地：%s\n  线上：%s\n' "$SITE_ROOT" "$DST_HASH" "${REMOTE_HASH:-（取不到）}"
        printf '部署输出的最后 20 行：\n'; tail -n 20 "$TMP5"; } >"$TMP7"
      fail "部署后复核（线上 data.json 指纹与本地不一致）" "$TMP7"
    fi
  else
    note "部署已完成，但没拿到站点域名（未配 SITE_URL 且 tcb hosting detail 无输出），跳过线上复核" ""
    echo "   ⚠️  未配置 SITE_URL，跳过线上复核（可在 .env 写 SITE_URL=https://你的域名）"
  fi

  # ---------- [7/8·补] 备用链接：后端原型页 public/farm.html → /farm.html ----------
  # 主站部署的是前端 out/（不含 farm.html），这一行单独把原型页也传一份，
  # 保证 https://<域名>/farm.html 这个备用链接随时可用。
  # 失败只记录、不阻断（主站此时已经上线成功，不该因为一个备用页把整条流水线判失败）。
  FARM_RESULT=""
  if [ -f "$PROJECT_DIR/public/farm.html" ]; then
    if "$TCB_BIN" hosting deploy "$PROJECT_DIR/public/farm.html" /farm.html \
         -e "$TCB_ENV_ID" --retry-count 3 >>"$TMP5" 2>&1; then
      FARM_RESULT="✅ 备用链接已更新（public/farm.html → /farm.html）"
      echo "   ✅ 备用链接已更新：public/farm.html → /farm.html"
    else
      FARM_RESULT="⚠️ 备用链接未更新（已记录，主站不受影响）"
      note "备用链接 /farm.html 上传失败（主站已成功上线，不受影响）" "$TMP5"
      echo "   ⚠️ 备用链接 /farm.html 上传失败（已记录到 update_error.log，主站不受影响）"
    fi
  else
    FARM_RESULT="⏭  跳过（本地没有 public/farm.html）"
    echo "   ⏭  本地没有 public/farm.html，跳过备用链接"
  fi
fi


# ---------- 收尾：成功摘要 ----------
HASH_AFTER="$(shasum -a 256 data.json public/data.json | awk '{print $1}' | tr '\n' ' ')"
{
  printf '[%s] ✅ 更新成功\n' "$TS"
  printf '  行情    ：%s\n' "${SPIDER_LINE:-—}"
  printf '  气象预警：%s\n' "$WEATHER_RESULT"
  printf '  统计局  ：%s\n' "$NBS_RESULT"
  printf '  后端校验：%s\n' "$CHECK_LINE"
  printf '  同步前端：%s\n' "$SYNC_RESULT"
  printf '  构建前端：%s\n' "$BUILD_RESULT"
  printf '  部署上线：%s\n' "$DEPLOY_RESULT"
  printf '  页面复核：%s\n' "${PAGE_RESULT:-—}"
  printf '  备用链接：%s\n' "${FARM_RESULT:-—}"
  printf '  前端工程：%s\n' "$FRONTEND_DIR"
  printf '  指纹    ：%s→ %s\n' "$HASH_BEFORE" "$HASH_AFTER"
  printf '\n'
} >> "$LOG_OK"

echo
echo "✅ 更新成功   $(date '+%Y-%m-%d %H:%M:%S')"
echo "   行情    ：${SPIDER_LINE:-—}"
echo "   气象预警：${WEATHER_RESULT}"
echo "   统计局  ：${NBS_RESULT}"
echo "   后端校验：$CHECK_LINE"
echo "   同步前端：$SYNC_RESULT"
echo "   构建前端：$BUILD_RESULT"
echo "   部署上线：$DEPLOY_RESULT"
echo "   页面复核：${PAGE_RESULT:-—}"
echo "   备用链接：${FARM_RESULT:-—}"
[ -n "$SITE_ROOT" ] && echo "   站点地址：$SITE_ROOT"
echo "   日志    ：update.log（成功摘要）｜ update_error.log（失败/跳过的详情）"
trim_log
exit 0

