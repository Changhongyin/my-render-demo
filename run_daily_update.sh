#!/bin/bash
# ============================================================
# 云端田埂 · 数据更新流水线（手动一键执行；终端 `bash run_daily_update.sh` 或 Finder 双击 run_daily_update.command）
#
#   ⚠️ 为什么不做定时任务：macOS 的 launchd 与 cron 都受 TCC 隐私保护限制，读不到 ~/Documents 下的
#      项目目录（实测报错 "Operation not permitted"），而本项目明确不申请"完全磁盘访问权限"、
#      也不迁移目录，所以采用"手动一键执行"。之前装过的 launchd 任务已用
#      scripts/uninstall_dailyupdate.sh 卸载（未做任何系统级改动）。
#
#   [1/5] 抓行情      python3 local_spider_v2.py             → crops（必成功）
#   [2/5] 抓气象预警  python3 fetch_weather_alerts.py --write → weather（默认允许失败，见下）
#   [3/5] 抓统计局    python3 fetch_nbs_prices.py             → nbs（只在每月 5/15/25 日跑）
#   [4/5] 完整性校验  data.json 必须能解析、且 crops/latest 有值
#   [5/5] 云端同步    tcb hosting deploy public/data.json /data.json
#                     tcb hosting deploy public/farm.html /farm.html
#
# 设计铁律：
#   · set -euo pipefail：关键步骤出错立即停止，绝不让半成品上线
#   · 抓取失败绝不覆盖旧 data.json —— 两个 python 脚本内部各自保证这一点；
#     本脚本只做"不越权"的事（失败就退出，不动 data.json、不上传）
#   · 任何失败/跳过都写进 update_error.log（含完整输出），成功摘要写进 update.log
#   · 两个日志只保留最近 2000 行，避免无限膨胀
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
#   硬步骤（失败即停）：行情抓取 → 完整性校验 → 云端上传（上传默认跳过/告警，UPLOAD_STRICT=1 时严格）
#   软步骤（失败只记录）：气象预警、国家统计局
#   想恢复"任何一步出错就停"的极致严格模式：STRICT_ALL=1
#
# 用法（在项目目录下执行）：
#   bash run_daily_update.sh                    # 完整流水线：抓取 → 校验 → 上传（未配置云端则自动跳过上传）
#   STRICT_ALL=1 bash run_daily_update.sh       # 任何一步失败（含气象/统计局/上传）都视为致命
#   FORCE_NBS=1 bash run_daily_update.sh        # 强制抓一次统计局数据（不限日期）
#   SKIP_UPLOAD=1 bash run_daily_update.sh      # 只更新本地，不上传
#   WEATHER_STRICT=1 / NBS_STRICT=1 / UPLOAD_STRICT=1   # 单独把某一步设为致命
#   （Finder 里双击 run_daily_update.command 效果相同，且窗口会保持打开方便看结果）
#
# 云端配置（上传前必须做一次，之后就不用管了）：
#   1) 登录：tcb login（交互式，登录态缓存在本机）或 tcb login --apiKeyId <SecretId> --apiKey <SecretKey>
#   2) 把环境 ID 写进 .env：TCB_ENV_ID=your-env-id   （或运行时 export TCB_ENV_ID=...）
#      取环境 ID：tcb env list
#   3) 验证：bash run_daily_update.sh 的第 [5/5] 步应显示 "✅ 已上传"
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/usr/bin/python3"
cd "$PROJECT_DIR"

LOG_OK="$PROJECT_DIR/update.log"
LOG_ERR="$PROJECT_DIR/update_error.log"
MAX_LINES=2000
TS="$(date '+%Y-%m-%d %H:%M:%S')"

# ---------- 开关（默认值以"流水线能跑完"为准，见文件头说明） ----------
STRICT_ALL="${STRICT_ALL:-0}"
WEATHER_STRICT="${WEATHER_STRICT:-0}"
NBS_STRICT="${NBS_STRICT:-0}"
UPLOAD_STRICT="${UPLOAD_STRICT:-0}"
SKIP_UPLOAD="${SKIP_UPLOAD:-0}"
FORCE_NBS="${FORCE_NBS:-0}"
if [ "$STRICT_ALL" = "1" ]; then WEATHER_STRICT=1; NBS_STRICT=1; UPLOAD_STRICT=1; fi

# ---------- 腾讯云环境 ID：环境变量优先，其次 .env ----------
if [ -z "${TCB_ENV_ID:-}" ] && [ -f "$PROJECT_DIR/.env" ]; then
  TCB_ENV_ID="$(grep -E '^[[:space:]]*TCB_ENV_ID=' "$PROJECT_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs || true)"
fi
TCB_ENV_ID="${TCB_ENV_ID:-}"
TCB_BIN="$(command -v tcb 2>/dev/null || command -v cloudbase 2>/dev/null || true)"

# ---------- 临时文件与清理 ----------
TMP1="$(mktemp)"; TMP2="$(mktemp)"; TMP3="$(mktemp)"; TMP4="$(mktemp)"; TMP5="$(mktemp)"
cleanup() { rm -f "$TMP1" "$TMP2" "$TMP3" "$TMP4" "$TMP5"; }
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
  echo "❌ 失败：$step（详情见 update_error.log）"
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

HASH_BEFORE="$(shasum -a 256 data.json public/data.json 2>/dev/null | awk '{print $1}' | tr '\n' ' ' || true)"

echo "=============================================="
echo " 云端田埂 · 每日数据更新"
echo " 开始时间：$TS"
echo "=============================================="

# ---------- 预检：只读检查，先把"这次会不会上传"说清楚 ----------
echo "▶ 预检"
if [ -n "$TCB_BIN" ]; then
  echo "   · CloudBase CLI ：$TCB_BIN"
else
  echo "   · CloudBase CLI ：未安装 → 本次不会上传（安装：npm install -g @cloudbase/cli）"
fi
if [ -n "$TCB_ENV_ID" ]; then
  echo "   · 云端环境 ID  ：$TCB_ENV_ID"
else
  echo "   · 云端环境 ID  ：未配置 → 本次只更新本地，不上传（在 .env 里加一行 TCB_ENV_ID=你的环境ID）"
fi
if [ -n "$TCB_BIN" ] && [ -n "$TCB_ENV_ID" ]; then
  if "$TCB_BIN" env list >/dev/null 2>&1; then
    echo "   · 登录态        ：✅ 已登录（登录态缓存在本机，无需重复登录）"
  else
    echo "   · 登录态        ：⚠️ 检测不到登录态（可能是未登录或网络不通）"
    echo "                     → 若未登录请先执行：tcb login；第 [5/5] 步会给出真实结果"
  fi
else
  echo "   · 上传前提      ：先在终端执行一次 tcb login（登录态缓存在本机，之后不用重复）"
fi
echo

# ---------- [1/5] 行情（硬步骤） ----------
echo "▶ [1/5] 抓取行情：local_spider_v2.py"
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

# ---------- [2/5] 气象预警（软步骤） ----------
echo "▶ [2/5] 抓取气象预警：fetch_weather_alerts.py --write"
if "$PYTHON" fetch_weather_alerts.py --write >>"$TMP2" 2>&1; then
  WEATHER_RESULT="✅ 已更新"
  echo "   ✅ 气象预警已更新"
else
  if [ "$WEATHER_STRICT" = "1" ]; then
    fail "气象预警抓取（fetch_weather_alerts.py，WEATHER_STRICT=1）" "$TMP2"
  fi
  WEATHER_RESULT="⚠️ 未更新（软步骤，已记录）"
  note "气象预警未更新（可选数据源；data.json 里的旧预警保持原样）" "$TMP2"
  echo "   ⚠️ 气象预警未更新（软步骤，已记录到 update_error.log），继续"
fi

# ---------- [3/5] 国家统计局（软步骤，仅每月 5/15/25 日） ----------
DO_NBS=0
DAY_OF_MONTH="$(date '+%-d')"
case "$DAY_OF_MONTH" in 5|15|25) DO_NBS=1 ;; esac
if [ "$FORCE_NBS" = "1" ]; then DO_NBS=1; fi

echo "▶ [3/5] 抓取国家统计局价格：fetch_nbs_prices.py"
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

# ---------- [4/5] 完整性校验（硬步骤） ----------
echo "▶ [4/5] 校验 data.json 完整性"
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

# ---------- [5/5] 云端同步（上传 public/ 的两个文件） ----------
echo "▶ [5/5] 同步到腾讯云静态托管"
UPLOAD_RESULT=""

deploy_one() {   # deploy_one <本地路径> <云端路径>
  "$TCB_BIN" hosting deploy "$1" "$2" -e "$TCB_ENV_ID" --retry-count 3
}

if [ "$SKIP_UPLOAD" = "1" ]; then
  UPLOAD_RESULT="⏭  跳过（SKIP_UPLOAD=1）"
  echo "   ⏭  已按 SKIP_UPLOAD=1 跳过上传"
elif [ -z "$TCB_BIN" ]; then
  UPLOAD_RESULT="⏭  跳过（未安装 CloudBase CLI）"
  note "未找到 tcb/cloudbase 命令，跳过上传。安装：npm install -g @cloudbase/cli" ""
  echo "   ⏭  未安装 CloudBase CLI，跳过上传（npm install -g @cloudbase/cli）"
elif [ -z "$TCB_ENV_ID" ]; then
  UPLOAD_RESULT="⏭  跳过（未配置 TCB_ENV_ID）"
  note "未配置 TCB_ENV_ID，跳过上传。请在 .env 里加一行：TCB_ENV_ID=你的环境ID" ""
  echo "   ⏭  未配置 TCB_ENV_ID，跳过上传（把这行写进 .env：TCB_ENV_ID=你的环境ID）"
else
  if deploy_one public/data.json /data.json >>"$TMP5" 2>&1 \
     && deploy_one public/farm.html /farm.html >>"$TMP5" 2>&1; then
    UPLOAD_RESULT="✅ 成功（public/data.json → /data.json，public/farm.html → /farm.html）"
    echo "   ✅ 已上传：public/data.json → /data.json、public/farm.html → /farm.html"
  else
    if [ "$UPLOAD_STRICT" = "1" ]; then
      fail "云端同步（tcb hosting deploy，UPLOAD_STRICT=1）" "$TMP5"
    fi
    UPLOAD_RESULT="⚠️ 失败（已跳过，本地数据不受影响）"
    note "云端同步失败。若提示未登录，先执行：tcb login --apiKeyId <SecretId> --apiKey <SecretKey>" "$TMP5"
    echo "   ⚠️ 云端同步失败（已记录到 update_error.log；本地数据完整不受影响）"
  fi
fi

# ---------- 收尾：成功摘要 ----------
HASH_AFTER="$(shasum -a 256 data.json public/data.json | awk '{print $1}' | tr '\n' ' ')"
{
  printf '[%s] ✅ 更新成功\n' "$TS"
  printf '  行情    ：%s\n' "${SPIDER_LINE:-—}"
  printf '  气象预警：%s\n' "$WEATHER_RESULT"
  printf '  统计局  ：%s\n' "$NBS_RESULT"
  printf '  校验    ：%s\n' "$CHECK_LINE"
  printf '  云端    ：%s\n' "$UPLOAD_RESULT"
  printf '  指纹    ：%s→ %s\n' "$HASH_BEFORE" "$HASH_AFTER"
  printf '\n'
} >> "$LOG_OK"

echo
echo "✅ 更新成功   $(date '+%Y-%m-%d %H:%M:%S')"
echo "   行情    ：${SPIDER_LINE:-—}"
echo "   气象预警：${WEATHER_RESULT}"
echo "   统计局  ：${NBS_RESULT}"
echo "   校验    ：${CHECK_LINE}"
echo "   云端    ：${UPLOAD_RESULT}"
echo "   日志    ：update.log（成功摘要）｜ update_error.log（失败/跳过的详情）"
trim_log
exit 0

