# -*- coding: utf-8 -*-
"""
云端田埂 · 行情数据生成端 v2（带历史累积 + 模拟补齐 + 自动备份）

与 v1（archive/local_spider.py）的区别：
  1. 不再"用今天的数据覆盖历史"：每次抓到的当日价会【追加/更新】进历史台账，
     同一天重复运行只会更新当天价格，不会产生重复点（幂等）。
  2. 输出结构升级为 { meta, crops[] }，直接对齐 Next.js 前端需要的 CropPrice。
  3. 历史不足时优雅降级：crops[].series 保留真实长度，crops[].series_padded
     补齐到 90 天供前端图表直接使用。
  4. 抓取失败绝不写文件，并以非零退出码明确报错。

v2.1 新增：
  5. PAD_MODE = "random_walk"：向前生成带轻微波动的【模拟补齐段】让图表更自然，
     并在 meta.source / meta.notes / crops[].series_synthetic* 上明确标注是模拟数据。
  6. 写入前自动备份到 backups/data_YYYY_MM_DD.json，只保留最近 7 份。
  7. 写出的文件权限修正为 644（mkstemp 默认 0600，静态托管/CDN 需要可读）。

v2.3（本次调整）：已**彻底移除数据库相关逻辑**。
     云数据库 + 云函数整套方案放弃，相关产物全部归档到 archive_deprecated/。
     现在这个脚本只做一件事：
         抓取 → 累积历史台账 → 编译 data.json / public/data.json → 备份
     · 不再有任何写库代码，不再需要 --skip-db
     · 也不会因为"连不上数据库"而报错退出（它根本不连数据库）

用法：
    python3 local_spider_v2.py              # 抓取一次并写入 data.json + public/data.json
    python3 local_spider_v2.py --dry-run    # 只预览将要写入的内容，不落盘、不备份
    python3 local_spider_v2.py --no-delay   # 跳过随机延迟（仅本地调试用）
"""

import argparse
import json
import os
import random
import sys
import tempfile
import time
import zlib
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import urllib3

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== 抓取配置（与 v1 完全一致） ====================
TARGET_URL = "https://jgjc.ndrc.gov.cn/viewPage/toQueryQbsjxx"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Safari/605.1.15",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://jgjc.ndrc.gov.cn/ncp/index.jhtml",
    # ⚠️【重要】抓取失败时，去浏览器 F12 -> Network -> toQueryQbsjxx -> 复制最新 Cookie 替换这里
    "Cookie": "SESSION=0c46acb3-f78b-4595-8192-5ecd76cd8daf; _site_id_cookie=1; clientlanguage=zh_CN"
}

POST_DATA = {
    "DATA_TYPE": "2",
    "PRODUCT_ID": "D3802EAE50A44429976CD1AC9184FD24,b7a346db9c824b74a918670b6bef28c9,33B8653F7E8A4E4DA90739B542469D0E,F6CF8B46DE0A45ED9F8DA5FD29EB916AL"
}

# ==================== 防封禁策略 ====================
MIN_DELAY = 1.0          # 每次请求前随机停顿区间
MAX_DELAY = 3.0
MAX_RETRIES = 2          # 失败后再试 2 次，共最多 3 次请求

# ==================== 输出配置 ====================
# 脚本所在目录（= 项目根目录）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 两个输出位置：根目录留档 + public/ 供静态托管访问
OUTPUT_FILES = [
    os.path.join(BASE_DIR, "data.json"),
    os.path.join(BASE_DIR, "public", "data.json"),
]

DISPLAY_LOCATION = "山东省寿光市三元朱村"   # 前端详情页要显示的"精确到村"位置
PUBLISH_TIME = "07:00"                    # 官方数据每天约 7:00 更新，用于拼 stat_time
GENERATOR = "local_spider_v2.py"

UNIT = "元/公斤"
VOLUME_PLACEHOLDER = "—"                  # 暂无成交量数据，给前端占位符而不是 undefined
SERIES_TARGET = 90                        # 前端图表支持近7/30/90日，序列至少要这么长
MAX_HISTORY = 400                         # 台账最多保留最近多少天，防止文件无限膨胀

# 历史不足时 series_padded 的补齐方式：
#   random_walk —— 向前生成一段有轻微波动的模拟数据（默认，图表更自然）
#   repeat      —— 用最早的真实值向前填平线（不生成任何新数值）
#   null        —— 前面补 None（前端类型需支持 number|null）
#   none        —— 不补，保持真实长度
PAD_MODE = "random_walk"
PAD_WALK_SEED = 20260918                  # 固定种子：保证同一品种每次生成的补齐段完全一致
PAD_WALK_VOL = 0.004                      # 单步波动上限 0.4%
PAD_WALK_DRIFT = 0.012                    # 真实段只有 1 个点时，用 ±1.2% 作为"合理带宽"

# 写入前自动备份：backups/data_2026_09_18.json，只保留最近 KEEP_BACKUPS 份
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
KEEP_BACKUPS = 7
BACKUP_NAME_FMT = "data_%Y_%m_%d.json"

# 前端分类下拉需要新增「粮油」这一类；每个品种的等级优先用抓取到的真实值
DEFAULT_CATEGORY = "粮油"
CROP_META = {
    "富强粉": {"grade": "标一"},
    "晚籼米": {"grade": "标一"},
    "标准粉": {"grade": "无"},
    "粳米": {"grade": "标一"},
}

# ==================== 前端 analysis 字段的占位文案 ====================
# 只有 read 是对真实数据的复述；supply / forecast 我们没有官方数据，
# 一律写成"占位说明"，绝不编造行情分析（会写进 meta.notes 里告知前端与用户）。
PLACEHOLDER_SUPPLY = "暂无该品种的官方供需数据，本条为占位说明。"
PLACEHOLDER_FORECAST = "暂无官方后市预测数据，本条为占位说明。"
GENERIC_TIP = "随行就市、分批出售最稳妥，别赌行情；卖前多问两家收购价，注意留存凭证。"

# ==================== 抓取与解析 ====================

def request_once():
    """单次请求。任何失败都抛异常，由 fetch_html 决定要不要重试。"""
    try:
        response = requests.post(TARGET_URL, headers=HEADERS, data=POST_DATA, timeout=10, verify=False)
    except requests.exceptions.RequestException as e:
        raise RuntimeError("网络请求异常: %s" % e)

    if response.status_code != 200:
        raise RuntimeError("请求失败，状态码: %s（IP 可能已被风控拦截）" % response.status_code)

    response.encoding = "utf-8"

    if not response.text.strip():
        raise RuntimeError("返回内容为空（IP 可能已被拦截，或 Cookie 已过期）")

    return response.text


def fetch_html(no_delay=False):
    """带【随机延迟 + 失败重试】的抓取，只有全部失败才抛异常。"""
    last_error = None
    total = MAX_RETRIES + 1

    for attempt in range(1, total + 1):
        if no_delay:
            print("⏱️  第 %d/%d 次请求（已跳过延迟）" % (attempt, total))
        else:
            delay = random.uniform(MIN_DELAY, MAX_DELAY)
            print("⏱️  第 %d/%d 次请求，随机延迟 %.1f 秒..." % (attempt, total, delay))
            time.sleep(delay)

        try:
            html = request_once()
            print("✅ 第 %d 次请求成功。" % attempt)
            return html
        except Exception as e:
            last_error = e
            print("⚠️  第 %d 次请求失败: %s" % (attempt, e))

    raise RuntimeError("连续 %d 次请求均失败: %s" % (total, last_error))


def parse_items(html):
    """解析 HTML，返回 [{name, level, price, date}]。解析不到有效数据同样抛异常。"""
    soup = BeautifulSoup(html, "html.parser")
    items = []

    for li in soup.find_all("li"):
        spans = li.find_all("span")
        if len(spans) < 4:
            continue
        try:
            price_val = float(spans[2].text.strip())
        except ValueError:
            price_val = 0.0

        items.append({
            "name": spans[0].text.strip(),
            "level": spans[1].text.strip(),
            "price": price_val,
            "date": spans[3].text.strip(),
        })

    if not items:
        raise RuntimeError("未抓取到有效数据，Cookie 或 PRODUCT_ID 可能已过期（或触发了人机验证）。")

    return items


def normalize_date(raw):
    """把 2026/09/18、2026-9-8 之类统一成 2026-09-18；无法识别返回空串。"""
    s = str(raw).strip().replace("/", "-").replace(".", "-")
    parts = s.split("-")
    if len(parts) != 3:
        return ""
    try:
        y, m, d = (int(p) for p in parts)
    except ValueError:
        return ""
    if not (2000 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        return ""
    return "%04d-%02d-%02d" % (y, m, d)


# ==================== 历史台账：读取与迁移 ====================

def load_existing(path):
    """读现有 data.json。不存在/损坏都返回 None —— 绝不因为旧文件有问题就崩溃。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("⚠️  现有 %s 无法解析（%s），本次当作没有历史处理。" % (path, e))
        return None


def _legacy_items(raw):
    """把旧格式（v1 的 {location,...,items:[]} 或更早的顶层数组）统一成 items 列表。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        return raw["items"]
    return []


def history_from_existing(raw):
    """把现有文件还原成历史台账。

    返回 (history, notes)：
      history = {品种名: [ {date, price, src, grade}, ... ]}  （按日期升序）
      notes   = 需要写进 meta.notes 的提示信息

    两种来源都支持：
      · v2 新格式：直接读 crops[].history
      · v1 旧格式：把 items 作为"演示数据种子点"导入（src 标记为 demo，如实标注来源）
    """
    history = {}
    notes = []

    # ---- v2 新格式 ----
    if isinstance(raw, dict) and isinstance(raw.get("crops"), list):
        for c in raw["crops"]:
            name = str(c.get("name", "")).strip()
            if not name:
                continue
            points = []
            for p in (c.get("history") or []):
                d = normalize_date(p.get("date"))
                price = p.get("price")
                if d and isinstance(price, (int, float)):
                    points.append({
                        "date": d,
                        "price": float(price),
                        "src": p.get("src", "live"),
                        "grade": str(p.get("grade", "")).strip(),
                    })
            history[name] = sorted(points, key=lambda x: x["date"])
        return history, notes

    # ---- v1 旧格式（含更早的顶层数组） ----
    items = _legacy_items(raw)
    for it in items:
        name = str(it.get("name", "")).strip()
        d = normalize_date(it.get("date"))
        price = it.get("price")
        if not name or not d or not isinstance(price, (int, float)):
            continue
        history.setdefault(name, []).append({
            "date": d,
            "price": float(price),
            "src": "demo",                       # 旧文件里 source 写着"演示数据"，如实标注
            "grade": str(it.get("level", "")).strip(),
        })

    for name in history:
        history[name].sort(key=lambda x: x["date"])

    if history:
        src_txt = raw.get("source", "未知") if isinstance(raw, dict) else "旧版顶层数组格式"
        notes.append(
            "历史首点继承自旧版 data.json（其 source=%s），已按真实来源标记为演示数据种子点。" % src_txt
        )

    return history, notes

def upsert_point(points, date, price, src, grade):
    """按日期 upsert：同一天重复运行只会更新当天价格，不会产生重复点。"""
    for p in points:
        if p["date"] == date:
            p["price"] = price
            p["src"] = src
            if grade:
                p["grade"] = grade
            return "updated"

    points.append({"date": date, "price": price, "src": src, "grade": grade})
    points.sort(key=lambda x: x["date"])
    return "appended"


def prune_history(points, max_days=None):
    """台账只保留最近 max_days 天（默认取 MAX_HISTORY），防止文件无限膨胀。

    ⚠️ 默认值必须在函数内部读取，不能写成 max_days=MAX_HISTORY：
    写成默认参数会在模块导入时被固化，之后再改 MAX_HISTORY 就不生效了
    （这个坑是被自测用例 8 抓出来的）。
    """
    limit = MAX_HISTORY if max_days is None else max_days
    if len(points) <= limit:
        return list(points)
    return points[-limit:]


def _stable_seed(key):
    """把品种名转成稳定的整数种子。
    不用 Python 内置 hash()：它对字符串每个进程都不一样，会导致每次运行结果变化。"""
    return zlib.crc32(key.encode("utf-8")) & 0xFFFFFFFF


def _random_walk_prefix(missing, series, seed_key=""):
    """从最早的真实价向前倒推 missing 个"带轻微波动"的模拟点。

    三条硬约束（保证补齐段不违背真实数据）：
      1. 无缝衔接：生成段的最后一个点紧邻真实段的第一个点
      2. 不越界：所有生成值都落在真实序列的 [最小值, 最大值] 区间内；
         若真实段只有 1 个点（区间宽度为 0），则以该价格 ±PAD_WALK_DRIFT 作为合理带宽
      3. 波动可控：单步波动 ≤ PAD_WALK_VOL，且用固定种子生成
         → 同一品种每次运行得到的补齐段完全一致（不会每天变来变去）

    ⚠️ 这一段是【模拟数据】，build_output 会在 meta.source / meta.notes /
    crops[].series_synthetic 上明确标注，前端必须展示该标注。
    """
    first_real = series[0]
    lo, hi = min(series), max(series)
    if hi - lo < 1e-9:
        lo = first_real * (1 - PAD_WALK_DRIFT)
        hi = first_real * (1 + PAD_WALK_DRIFT)

    rnd = random.Random(PAD_WALK_SEED + _stable_seed(seed_key))

    cur = first_real
    back = []                      # 向前倒推出的一系列点，back[0] 离真实段最近
    for _ in range(missing):
        nxt = cur * (1 + rnd.uniform(-PAD_WALK_VOL, PAD_WALK_VOL))
        if nxt < lo or nxt > hi:
            # 碰到上下边界就把这一步收回一半，避免贴着边界来回抖动
            nxt = cur - (nxt - cur) * 0.5
        nxt = min(max(nxt, lo), hi)
        back.append(round(nxt, 3))
        cur = nxt

    return back[::-1]              # 反转成时间正序：最早的在前，最后一个是紧邻真实段的那天


def pad_series(series, target=None, mode=None, seed_key=""):
    """把真实序列补齐到 target 长度，供前端图表直接使用（前端无需改索引逻辑）。
      random_walk：向前生成带轻微波动的【模拟补齐段】（默认，图表更自然、但会明确标注）
      repeat     ：用最早的真实值向前填平线（不生成任何新数值）
      null       ：前面补 None（前端类型需支持 number|null，recharts 会断线）
      none       ：不补，保持真实长度（前端必须自己处理短序列）

    同理，target/mode 的默认值也在函数内部读取，避免被导入时的快照固化。
    """
    target = SERIES_TARGET if target is None else target
    mode = PAD_MODE if mode is None else mode

    missing = max(0, target - len(series))
    if missing == 0 or mode == "none":
        return list(series)
    if mode == "null":
        return [None] * missing + list(series)
    if mode == "random_walk":
        return _random_walk_prefix(missing, series, seed_key) + list(series)
    return [series[0]] * missing + list(series)


def build_analysis(name, grade, series):
    """前端 analysis 的 4 段文案。read 是对真实数据的复述，其余为占位说明。"""
    latest = series[-1]

    if len(series) >= 2:
        diff = latest - series[-2]
        if diff > 0:
            trend = "较上一期上涨 %.2f %s" % (diff, UNIT)
        elif diff < 0:
            trend = "较上一期下跌 %.2f %s" % (abs(diff), UNIT)
        else:
            trend = "与上一期持平"
    else:
        trend = "历史数据尚在累积，暂无环比"

    grade_txt = ("等级 %s" % grade) if grade and grade != "无" else "无等级标注"

    return {
        "read": "%s（%s）最新价 %.2f %s，%s。数据来自国家发展改革委价格监测中心。"
                % (name, grade_txt, latest, UNIT, trend),
        "supply": PLACEHOLDER_SUPPLY,
        "forecast": PLACEHOLDER_FORECAST,
        "tip": GENERIC_TIP,
    }


def build_output(history, notes, scraped_names):
    """把历史台账编译成前端要的 { meta, crops[] }。"""
    crops = []
    stale_names = []
    demo_points = 0
    total_points = 0
    max_synthetic = 0          # 各品种中最大的一段"模拟补齐"长度，用于生成全局标注

    for name in sorted(history.keys()):
        points = prune_history(history[name])
        if not points:
            continue

        series = [p["price"] for p in points]
        grade = points[-1].get("grade") or CROP_META.get(name, {}).get("grade", "")
        is_stale = name not in scraped_names

        # 补齐到 90 天；random_walk 模式下会额外生成一段【模拟数据】
        padded = pad_series(series, seed_key=name)
        synthetic = (len(padded) - len(series)) if PAD_MODE == "random_walk" else 0
        max_synthetic = max(max_synthetic, synthetic)

        demo_points += sum(1 for p in points if p.get("src") == "demo")
        total_points += len(points)

        crop = {
            "name": name,
            "category": DEFAULT_CATEGORY,
            "grade": grade,
            "unit": UNIT,
            "volume": VOLUME_PLACEHOLDER,
            "latest": series[-1],
            "date": points[-1]["date"],
            "latest_src": points[-1].get("src", ""),   # live / demo —— 让前端知道这个价是哪来的
            "series": series,                          # 真实历史，长度 = 实际累积天数
            "series_padded": padded,                   # 补齐到 90，供前端图表直接画
            "series_synthetic": synthetic > 0,         # ⚠️ 是否含【模拟补齐】数据
            "series_synthetic_prefix": synthetic,      # ⚠️ 前 N 个点是模拟的，真实数据从第 N+1 个点开始
            "history": points,                         # 带日期的台账，便于审计与排错
            "analysis": build_analysis(name, grade, series),
            "alerts": {"price": [], "supply": [], "risk": []},
            "analysis_source": "模板生成（read 为数据复述，supply/forecast 为占位说明）",
        }

        if is_stale:
            crop["stale"] = True
            crop["last_updated"] = points[-1]["date"]
            stale_names.append("%s（沿用 %s 的价格）" % (name, points[-1]["date"]))

        crops.append(crop)

    days = max([len(c["series"]) for c in crops]) if crops else 0
    latest_any = max([c["date"] for c in crops]) if crops else ""

    # ⚠️ stat_time / freshness 必须按【真实抓取】的最新日期算，而不是台账里最新的一条。
    # 否则会出现「统计截止时间写的是演示种子点的日期」这种误导（这在真实数据里发生过：
    # 演示种子点是 09-18、真实抓取只到 09-16，页面却显示"已更新到 09-18"）。
    live_dates = [p["date"] for c in crops for p in c["history"] if p.get("src") == "live"]
    live_date = max(live_dates) if live_dates else ""
    report_date = live_date or latest_any

    # 数据来源必须如实反映序列里到底有多少演示点
    if total_points == 0 or demo_points == 0:
        source = "实时抓取（国家发展改革委价格监测中心）"
    elif demo_points == total_points:
        source = "演示数据"
    else:
        source = "演示数据 + 实时抓取（序列中含 %d 个演示种子点）" % demo_points

    if max_synthetic > 0:
        # 图表历史段是生成的，必须在来源里说清楚
        source += "｜图表历史段含模拟补齐数据"

    if demo_points:
        notes.append("序列中含 %d 个演示数据种子点，建议随真实抓取逐步替换。" % demo_points)
    if live_date and latest_any and latest_any > live_date:
        notes.append(
            "注意：台账里最新的一条（%s）来自演示种子点，真实抓取数据截止 %s —— "
            "stat_time 已按真实数据日期计算，前端不要把它读成 %s。"
            % (latest_any, live_date, latest_any)
        )
    if days < SERIES_TARGET:
        notes.append(
            "历史数据累积中（%d/%d 天）：series 为真实长度，series_padded 已按 %s 方式补齐到 %d 天。"
            % (days, SERIES_TARGET, PAD_MODE, SERIES_TARGET)
        )
    if max_synthetic > 0:
        notes.append(
            "⚠️ 图表历史段为【模拟补齐数据】：每个品种 series_padded 的前 %d 个点是 random_walk 生成的"
            "（固定种子、单步波动≤%.1f%%、且不超出该品种真实价格的区间），仅用于让演示图表更自然，"
            "不是真实抓取结果；真实数据从第 %d 个点开始。前端必须在图表上展示该标注。"
            % (max_synthetic, PAD_WALK_VOL * 100, max_synthetic + 1)
        )
    if stale_names:
        notes.append("本次未抓到的品种：%s。" % "、".join(stale_names))
    notes.append("analysis 的 supply/forecast 为占位说明，待接入官方供需/预测数据后替换；alerts 暂为空数组。")

    return {
        "meta": {
            "location": DISPLAY_LOCATION,
            "source": source,
            "stat_time": (report_date + " " + PUBLISH_TIME) if report_date else "",
            "data_date": report_date,                      # 真实抓取数据的截止日期
            "latest_any_date": latest_any,                 # 台账里最新的一条（可能来自演示种子点）
            "fetched_at": datetime.now().strftime("%Y-%m-%d"),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generator": GENERATOR,
            "series_days": days,
            "series_target": SERIES_TARGET,
            "pad_mode": PAD_MODE,                          # 补齐方式，前端可据此显示对应提示
            "synthetic_prefix_len": max_synthetic,         # ⚠️ series_padded 前 N 个点是模拟数据
            "synthetic_history": max_synthetic > 0,
            "degraded": days < SERIES_TARGET,
            "freshness": ("真实数据已更新到 " + report_date) if report_date else "暂无数据",
            "notes": notes,
        },
        "crops": crops,
    }

# ==================== 备份 ====================

def prune_backups(keep=None):
    """只保留最近 keep 份备份（默认 KEEP_BACKUPS），返回删除掉的数量。
    文件名里的日期是 YYYY_MM_DD 定长格式，所以按文件名排序就等于按日期排序。"""
    keep = KEEP_BACKUPS if keep is None else keep
    try:
        names = sorted(
            n for n in os.listdir(BACKUP_DIR)
            if n.startswith("data_") and n.endswith(".json")
        )
    except OSError:
        return 0

    removable = names[:-keep] if keep > 0 else names
    removed = 0
    for name in removable:
        try:
            os.remove(os.path.join(BACKUP_DIR, name))
            removed += 1
        except OSError:
            pass
    return removed


def backup_current():
    """写入前把当前的 data.json 备份成 backups/data_YYYY_MM_DD.json。

    返回 (备份路径, 清理掉的旧备份数量)；没有可备份内容时返回 (None, 0)。

    两点约定：
      · 同一天多次运行时，当天备份会被覆盖成"本次写入前的状态"——这正是回滚需要的那份
      · 备份失败只告警、不中断主流程（不能因为备份问题导致当天数据拿不到）
    """
    source = next(
        (p for p in OUTPUT_FILES if os.path.exists(p) and os.path.getsize(p) > 0),
        None,
    )
    if source is None:
        print("ℹ️  当前没有可备份的 data.json（首次运行），跳过备份。")
        return None, 0

    target = os.path.join(BACKUP_DIR, datetime.now().strftime(BACKUP_NAME_FMT))

    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        with open(source, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())        # 二进制拷贝，不做任何编码转换
    except Exception as e:
        print("⚠️  备份失败（不影响本次写入）: %s" % e)
        return None, 0

    return target, prune_backups()


# ==================== 写入 ====================

def save_json(payload):
    """原子写入：先写同目录临时文件，再 os.replace 覆盖目标文件。
    这样即使写入过程崩溃，上一次成功的数据也不会被写坏。"""
    written = []
    for path in OUTPUT_FILES:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".data.json.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            try:
                # mkstemp 建出来的是 0600，静态托管/CDN 需要 644 才读得到
                os.chmod(path, 0o644)
            except OSError:
                pass
            written.append(path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    return written


# ==================== 主流程 ====================

# 别的脚本会往 meta 里写的字段前缀（本脚本重建 meta 时要跟着传下去）
FOREIGN_META_PREFIXES = ("weather_", "nbs_", "alert_")


def restore_foreign_meta(output, raw):
    """把其它脚本写在 meta 上的字段跟着传下去（本脚本重建 meta，默认会丢掉它们）。

    气象脚本会写 meta.weather_source / weather_data_kind / weather_fetched_at /
    weather_provider / weather_alert_count，并在 meta.sources 里加一条 weather_alert 署名、
    在 meta.notes 里加一行【气象预警】说明。前端在"没有顶层 weather 块"时会读 meta.weather_*
    兜底，丢了会让预警卡片缺来源标注、"模拟数据"提示也一起丢。

    规则（宁可少补、绝不多搬）：
      · 只按前缀白名单补（weather_ / nbs_ / alert_），不会把旧格式遗留的 items/location 搬进来
      · sources 按 type 去重合并，保留别人写的署名
      · notes 只补以【开头的他人说明，且不重复
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("meta"), dict):
        return []
    old_meta = raw["meta"]
    kept = []

    for key, value in old_meta.items():
        if key in ("sources", "notes"):
            continue
        if key.startswith(FOREIGN_META_PREFIXES) and key not in output["meta"]:
            output["meta"][key] = value
            kept.append(key)

    old_sources = [s for s in (old_meta.get("sources") or []) if isinstance(s, dict)]
    new_sources = [s for s in (output["meta"].get("sources") or []) if isinstance(s, dict)]
    have_types = set(s.get("type") for s in new_sources)
    added_sources = [s for s in old_sources if s.get("type") and s.get("type") not in have_types]
    if added_sources:
        output["meta"]["sources"] = new_sources + added_sources
        kept.append("sources(+%d)" % len(added_sources))

    out_notes = list(output["meta"].get("notes") or [])
    foreign_notes = [n for n in (old_meta.get("notes") or [])
                     if str(n).startswith("【") and n not in out_notes]
    if foreign_notes:
        output["meta"]["notes"] = out_notes + foreign_notes
        kept.append("notes(+%d)" % len(foreign_notes))
    return kept


def restore_crop_alerts(output, raw):
    """把其它脚本写在 crops[].alerts 上的预警字段跟着传下去。

    本脚本用台账重建 crops，会丢掉 fetch_weather_alerts.py 写在每个品种上的
    alerts / alerts_source / alerts_updated_at；前端有一条"没有 weather 块时就读
    crops[].alerts.* 字符串"的兜底路径，丢了会让预警卡片整个消失。
    规则：只在当前值为空/缺失时补，有值的一律不动（谁写的数据谁说话）。
    """
    if not isinstance(raw, dict):
        return []
    old_by_name = {}
    for crop in raw.get("crops") or []:
        if isinstance(crop, dict) and crop.get("name"):
            old_by_name[crop["name"]] = crop

    def is_empty(value):
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, tuple, set)):
            return len(value) == 0
        if isinstance(value, dict):
            return not any(v for v in value.values() if v)
        return False

    kept = []
    for crop in output.get("crops") or []:
        src = old_by_name.get(crop.get("name"))
        if not src:
            continue
        for key in ("alerts", "alerts_source", "alerts_updated_at"):
            if key in src and is_empty(crop.get(key)):
                crop[key] = src[key]
                kept.append("%s.%s" % (crop.get("name"), key))
    return kept


def main():
    parser = argparse.ArgumentParser(description="抓取行情并累积进历史台账")
    parser.add_argument("--dry-run", action="store_true", help="只预览将要写入的内容，不落盘、不备份")
    parser.add_argument("--no-delay", action="store_true", help="跳过随机延迟（仅本地调试用）")
    args = parser.parse_args()

    print("⏳ 开始抓取（v2 · 带历史累积）...")

    # 1. 载入历史台账（旧格式会自动迁移）
    raw = load_existing(OUTPUT_FILES[0])
    history, notes = history_from_existing(raw)
    if history:
        print("📚 已载入历史：%s" % "，".join(
            "%s %d 天" % (k, len(v)) for k, v in sorted(history.items())
        ))
    else:
        print("📚 暂无历史数据，本次将成为第一个数据点。")

    # 2. 抓取：失败就直接退出，绝不写文件
    try:
        html = fetch_html(no_delay=args.no_delay)
        items = parse_items(html)
    except Exception as e:
        print("❌ 抓取失败: %s" % e)
        print("🛡️  已保留现有 %s 不变（历史台账未被破坏）。" % OUTPUT_FILES[0])
        sys.exit(1)

    # 3. 把本次结果 upsert 进台账（同一天重复运行只会更新，不会重复追加）
    scraped_names = set()
    today = datetime.now().strftime("%Y-%m-%d")
    for it in items:
        name = it["name"]
        date = normalize_date(it["date"]) or today
        history.setdefault(name, [])
        action = upsert_point(history[name], date, it["price"], "live", it["level"])
        scraped_names.add(name)
        print("  · %-6s %s  %.2f  （%s）" % (name, date, it["price"], "更新当天" if action == "updated" else "追加新点"))

        if it["price"] <= 0:
            print("     ⚠️ 该品种价格解析异常（<=0），已如实记入台账，请留意")

    # 4. 编译成前端要的结构
    output = build_output(history, notes, scraped_names)

    # 4.5 原样保留「别人的」数据块 —— 本脚本只管 meta / crops，但 data.json 里还有
    #     weather（fetch_weather_alerts.py 写的气象预警）与 nbs（fetch_nbs_prices.py 写的
    #     国家统计局数据）。它们必须跟着一起传下去，否则每天定时一跑就把前端橙色预警卡片、
    #     蓝色官方参考卡片的数据整块吞掉（三个脚本分工写不同键，谁也别吞谁的）。
    #     判定：只认"对象型"顶层键 —— 旧格式遗留的 items/location 等标量或数组不会被误搬回来。
    if isinstance(raw, dict):
        foreign_keys = [k for k, v in raw.items()
                        if k not in ("meta", "crops") and isinstance(v, dict)]
        for key in foreign_keys:
            output[key] = raw[key]
        if foreign_keys:
            print("🤝 原样保留其它脚本写入的数据块：%s" % "、".join(foreign_keys))

    kept_meta = restore_foreign_meta(output, raw)
    if kept_meta:
        print("🤝 原样保留 meta 上其它脚本写的字段：%s" % "、".join(kept_meta))

    kept_alerts = restore_crop_alerts(output, raw)
    if kept_alerts:
        print("🤝 原样保留 crops 上的预警字段：%s" % "、".join(kept_alerts))

    if not output["crops"]:
        print("❌ 台账为空，拒绝写出空文件（避免覆盖上一次的好数据）。")
        sys.exit(1)

    days = output["meta"]["series_days"]
    print("✅ 本次抓到 %d 个品种；台账累积 %d/%d 天。" % (len(scraped_names), days, SERIES_TARGET))
    if output["meta"]["degraded"]:
        print("⚠️  历史不足 %d 天：series 保留真实长度（%d），series_padded 已按 %s 方式补齐供图表使用。"
              % (SERIES_TARGET, days, PAD_MODE))
    for n in output["meta"]["notes"]:
        print("ℹ️  %s" % n)

    # 5. 预览模式到此为止
    if args.dry_run:
        print("\n--- DRY RUN：以下内容没有写入任何文件 ---")
        text = json.dumps(output, ensure_ascii=False, indent=2)
        print(text if len(text) < 3000 else text[:3000] + "\n…（后略）")
        return

    # 6. 写入前先备份当前的 data.json（backups/data_YYYY_MM_DD.json，只留最近 KEEP_BACKUPS 份）
    backup_path, removed = backup_current()
    if backup_path:
        print("🗄️  已备份旧文件 → %s（顺带清理旧备份 %d 个）" % (backup_path, removed))

    # 7. 写入
    try:
        written = save_json(output)
    except Exception as e:
        print("❌ 写入 data.json 失败: %s" % e)
        sys.exit(1)

    for path in written:
        print("📁 已写入: %s" % path)


if __name__ == "__main__":
    main()


