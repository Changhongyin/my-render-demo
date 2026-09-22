#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_weather_alerts_v3.py —— WeatherAPI.com 气象灾害预警接入（v3）

用法：
    python3 fetch_weather_alerts_v3.py                     # 抓一次；有预警就写进 data.json
    python3 fetch_weather_alerts_v3.py --dry-run           # 只打印，不写任何文件
    python3 fetch_weather_alerts_v3.py --lat 40.7128 --lon -74.0060 --dry-run
    python3 fetch_weather_alerts_v3.py --key xxxxx         # 临时换 Key（默认读 .env 的 WEATHER_API_KEY）

退出码（在 run_daily_update.sh 里属于【软步骤】：失败只记录，绝不中断流水线）：
    0  接口连通，且成功把真实预警写进了 data.json
    2  接口连通，但响应里【没有 alerts 字段】→ 该套餐 / 该地区不提供预警数据（即"无预警权限"）
    3  接口连通，alerts 字段存在但【当前没有生效预警】（数组为空，或返回的都已过期）
    4  接口返回错误（HTTP 非 200，或响应体里带 error）
    5  有预警，但影响范围与查询地点对不上 → 【拒绝写入】（详见下面「归属校验」）
    1  网络异常 / 响应无法解析

⚠️ 归属校验（默认开启，这是本脚本最重要的一道闸门）：
    实测（2026-09-22）WeatherAPI 的中国预警**不按坐标严格过滤**：
      查上海(31.23,121.47) → 返回「山东博兴县气象台大雾橙色预警」
      查广州(23.13,113.26) → 返回「贵州碧江区气象台雷电黄色预警」
      查博兴县本地反而 0 条；查寿光/潍坊/济南/青岛/东营/日照/成都/武汉 均 0 条
    也就是说：能返回真实的中国气象台预警（结构、四色等级都对得上），但**可能不属于你查的地方**。
    因此默认只写入"影响范围(areas)与查询地点能互相包含"的预警，其余一律拒绝写入并记录原因，
    宁可少展示，绝不把外地预警冒充成本地预警。确需放行时用 --allow-area-mismatch。

数据契约（为什么前端不用改）：
    写出结构与 fetch_weather_alerts.py **完全一致** —— 直接复用它的 merge_into_data()，
    所以 weather 块 / meta.weather_* / meta.sources / crops[].alerts.risk 的样子一模一样，
    前端 src/lib/weather-live.ts 与 weather-section.tsx 无需任何改动。

字段映射（WeatherAPI → 我们的标准结构）：
    headline             → title
    event / desc 关键词   → type（英文事件名 → 中文灾害类型，见 TYPE_KEYWORDS）
    severity（CAP 标准）  → level（Extreme→红色 / Severe→橙色 / Moderate→黄色 / Minor→蓝色）
                           ⚠️ CAP severity 不是中国气象局四色标准，换算关系会写进 meta.notes，原文保留在 severity_raw
    （接口不提供）        → sender（发布单位）：留空 → 前端自动跳过这一行，不显示假信息
    effective            → pub_time（该接口只给"生效时间"，没有发布时间，故用它顶替并注明）
    effective / expires  → effective / expire
    desc                 → text（预警正文）
    instruction          → advice（防御建议；为空时前端显示"（后端未提供解读文字…）"）
    areas                → area（扩展字段：影响范围，前端暂不渲染但保留可追溯）
    identifier           → id
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

import requests

# 复用现有脚本的契约与工具函数（同一份 merge_into_data，保证前端认得出）
from fetch_weather_alerts import (
    ENV_FILE,
    LEVEL_COLORS,
    NOTE_PREFIX_WEATHER,
    OUTPUT_FILES,
    SOURCE_NOTE,
    alert_to_text,
    backup_current,
    dedupe_and_sort,
    humanize_time,
    load_existing,
    merge_into_data,
    now_str,
    save_json,
)

# ==================== 配置 ====================

API_URL = "https://api.weatherapi.com/v1/forecast.json"
PROVIDER = "weatherapi"
SOURCE_LABEL = "WeatherAPI（weatherapi.com）"
DEFAULT_TIMEOUT = 15
DEFAULT_RETRIES = 2

# CAP（Common Alerting Protocol）severity → 页面用的四色
SEVERITY_TO_LEVEL = {
    "extreme": "红色",
    "severe": "橙色",
    "moderate": "黄色",
    "minor": "蓝色",
}

# 英文事件名 → 中文灾害类型（按顺序匹配：thunderstorm 放在 rain 前面，
# 免得 "Thunderstorm with Heavy Rain" 被误判成暴雨而不是雷暴）
TYPE_KEYWORDS = [
    (("thunderstorm", "lightning", "thunder"), "雷暴"),
    (("tornado", "hurricane", "typhoon", "cyclone"), "台风"),
    (("blizzard", "heavy snow", "snow"), "暴雪"),
    (("hail",), "冰雹"),
    (("heavy rain", "torrential", "rain", "rainfall", "flood"), "暴雨"),
    (("gale", "high wind", "strong wind", "wind"), "大风"),
    (("extreme heat", "heat", "high temperature"), "高温"),
    (("frost",), "霜冻"),
    (("extreme cold", "freeze", "cold", "chill"), "低温"),
    (("fog",), "大雾"),
    (("dust", "sand"), "沙尘"),
    (("haze", "smoke", "air quality"), "霾"),
    (("drought", "dry"), "干旱"),
    (("ice", "slippery", "winter storm"), "道路结冰"),
]

# 中文类型（有些接口/镜像会直接回中文）
ZH_TYPES = ("暴雨", "暴雪", "大风", "高温", "寒潮", "雷电", "冰雹", "大雾", "霾",
            "沙尘", "台风", "干旱", "低温", "道路结冰", "山洪", "霜冻")


# ==================== 小工具 ====================

def read_env_file(key, path=ENV_FILE):
    """从 .env 读一个值（只读，不打印、不外泄）。"""
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def mask(secret):
    """日志里永远不打印完整 Key。"""
    s = str(secret or "")
    return (s[:4] + "…" + s[-4:]) if len(s) > 10 else "***"


def detect_type(*texts):
    """从事件名/标题/正文里认出中文灾害类型；认不出来返回空串（绝不编造）。"""
    haystack = " ".join([str(t or "") for t in texts])
    low = haystack.lower()
    for keys, zh in TYPE_KEYWORDS:
        for k in keys:
            if k in low:
                return zh
    for zh in ZH_TYPES:            # 已经是中文类型就直接用
        if zh in haystack:
            return zh
    return ""


def area_matches(area, location):
    """影响范围与查询地点是否对得上：去掉 省/市/区/县/村 后互相包含即算匹配。

    例：area="寿光市" vs location="山东省寿光市三元朱村" → 核心词「寿光」互相包含 → 匹配
        area="博兴县" vs location="上海"                 → 互不包含 → 不匹配（拒写，避免误报）
    """
    def core(s):
        return re.sub(r"[省市区县村\s（）()]", "", str(s or ""))
    ca, cl = core(area), core(location)
    if not ca or not cl:
        return False
    return ca in cl or cl in ca


def fetch_forecast(lat, lon, key, timeout, retries):
    """请求 forecast.json（alerts=yes）。返回 (status_code, payload_or_None, error_text)。"""
    params = {"key": key, "q": "%s,%s" % (lat, lon), "days": "1", "aqi": "no", "alerts": "yes"}
    session = requests.Session()
    last_err = ""
    for attempt in range(1, retries + 2):
        try:
            resp = session.get(API_URL, params=params, timeout=timeout)
            try:
                payload = resp.json()
            except ValueError:
                return resp.status_code, None, "响应不是 JSON：%s" % resp.text[:300]
            return resp.status_code, payload, ""
        except requests.RequestException as e:       # 网络层异常才重试
            last_err = "%s: %s" % (type(e).__name__, e)
            if attempt <= retries:
                time.sleep(1.5 * attempt)
    return 0, None, last_err


def normalize_weatherapi_alert(raw, location_name=""):
    """WeatherAPI 的单条 alert → 我们的标准结构（字段一个不少，缺的一律空串）。"""
    severity_raw = str(raw.get("severity", "")).strip()
    level = SEVERITY_TO_LEVEL.get(severity_raw.lower(), "")
    event = str(raw.get("event", "")).strip()
    headline = str(raw.get("headline", "")).strip()
    desc = str(raw.get("desc", "")).strip()
    instruction = str(raw.get("instruction", "")).strip()

    # severity 认不出来时，退一步从标题/正文里找颜色词（red/orange/yellow/blue）
    if not level:
        probe = ("%s %s %s" % (headline, event, desc)).lower()
        for en, zh in (("red", "红色"), ("orange", "橙色"), ("yellow", "黄色"), ("blue", "蓝色")):
            if re.search(r"\b%s\b" % en, probe):
                level = zh
                break

    title = headline or event or "气象预警"
    type_zh = detect_type(event, headline, desc) or event    # 认不出就用英文事件名，不编造中文类型
    effective = humanize_time(raw.get("effective"))
    expire = humanize_time(raw.get("expires"))

    alert = {
        "id": str(raw.get("identifier") or "").strip(),
        "title": title,
        "type": type_zh,
        "level": level,                    # 空串 → 前端按最低级上色并提示"等级无法识别"
        "color": LEVEL_COLORS.get(level, ""),
        "sender": "",                      # WeatherAPI 不提供发布单位：留空，让前端跳过这一行
        "pub_time": effective,             # 只有生效时间，用它顶替（并在 notes 里注明）
        "effective": effective,
        "expire": expire,
        "text": desc,
        "advice": instruction,
        "src": SOURCE_LABEL,
        "provider": PROVIDER,
        "data_kind": "live",
    }
    # 扩展字段（前端不渲染，保留原始信息便于追溯 / 以后做详情页）
    alert["area"] = str(raw.get("areas", "")).strip() or location_name
    alert["severity_raw"] = severity_raw
    alert["urgency"] = str(raw.get("urgency", "")).strip()
    alert["certainty"] = str(raw.get("certainty", "")).strip()
    alert["category"] = str(raw.get("category", "")).strip()
    alert["event_raw"] = event

    if not alert["id"]:
        alert["id"] = "%s-%s-%s" % (PROVIDER, alert["type"] or "unknown", (alert["pub_time"] or now_str())[:16])
    return alert


def build_notes_line(alerts, location_name, fetched_at, unmapped_severity_count=0):
    """给 meta.notes 写一行【气象预警】：把这批预警的来路与"等级是怎么换算的"讲清楚。"""
    if not alerts:
        return NOTE_PREFIX_WEATHER + "本次没有生效中的气象预警（来源 %s，抓取于 %s）。" % (
            SOURCE_LABEL, fetched_at)
    top = alerts[0]
    line = NOTE_PREFIX_WEATHER + "当前 %d 条生效预警，最高等级：%s%s预警（来源 %s，抓取于 %s）。" % (
        len(alerts), top.get("type", ""), top.get("level", ""), SOURCE_LABEL, fetched_at)
    line += (" 说明：等级由 WeatherAPI 的 CAP severity 换算（Extreme→红色/Severe→橙色/"
             "Moderate→黄色/Minor→蓝色），不是中国气象局四色预警；发布时间用 effective（生效时间）顶替；"
             "该接口不提供发布单位。")
    if unmapped_severity_count:
        line += " 其中 %d 条 severity 无法识别，已按最低级蓝色展示（原文保留在 severity_raw）。" % unmapped_severity_count
    line += " 覆盖地点：%s。" % (location_name or "—")
    return line


# ==================== 写入（复用现有脚本的合并 / 备份 / 原子写） ====================

def write_alerts(alerts, cfg, out_paths=None):
    """把归一化后的预警合并进 data.json（只动 weather / crops[].alerts / meta 三处）。"""
    data = load_existing(OUTPUT_FILES[0])
    if not (data.get("crops") or []):
        raise RuntimeError("现有 data.json 里没有 crops，拒绝写入（请先运行 local_spider_v2.py 生成行情数据）")
    merged = merge_into_data(data, alerts, cfg)

    meta = merged.setdefault("meta", {})
    keep = [n for n in (meta.get("notes") or [])
            if not str(n).startswith(NOTE_PREFIX_WEATHER + "·来源说明")]
    keep.append(NOTE_PREFIX_WEATHER + "·来源说明：" + cfg["_source_note_detail"])
    meta["notes"] = keep
    meta["weather_api"] = {
        "provider": PROVIDER,
        "endpoint": API_URL,
        "key_masked": cfg["_key_masked"],
        "mapping_note": "level 由 CAP severity 换算；pub_time 取自 effective；接口不提供发布单位",
    }

    backup_dest, removed = backup_current()          # 先备份当前 data.json（写入前的状态）
    written = save_json(merged, out_paths or OUTPUT_FILES)
    return written, backup_dest, removed


# ==================== 主流程 ====================

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="WeatherAPI.com 气象灾害预警接入（v3）：抓取 → 归一化 → 写入 data.json 的 weather 块")
    p.add_argument("--key", help="WeatherAPI Key（默认读环境变量 / .env 的 WEATHER_API_KEY）")
    p.add_argument("--lat", type=float, help="纬度（默认 .env 的 WEATHER_LAT，即寿光 36.88）")
    p.add_argument("--lon", type=float, help="经度（默认 .env 的 WEATHER_LON，即寿光 118.73）")
    p.add_argument("--location", help="地点名称（默认 .env 的 WEATHER_LOCATION）")
    p.add_argument("--bucket", default=None, choices=["risk", "supply", "price"],
                   help="预警镜像到 crops[].alerts 的哪个桶（默认 risk）")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单次请求超时秒数（默认 15）")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="网络异常重试次数（默认 2）")
    p.add_argument("--dry-run", "--no-write", action="store_true", dest="dry_run", help="只打印，不写文件")
    p.add_argument("--include-expired", action="store_true", help="保留已过期预警（仅调试）")
    p.add_argument("--no-mirror", action="store_true", help="不把预警镜像到 crops[].alerts.risk")
    p.add_argument("--allow-area-mismatch", action="store_true",
                   help="跳过「影响范围必须与查询地点对得上」的归属校验（危险，仅调试/非中国地区用）")
    p.add_argument("--no-raw", action="store_true", help="不打印完整 JSON（默认打印）")
    p.add_argument("--out", help="只写到指定文件（调试用；默认写 data.json + public/data.json）")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    key = (args.key or os.environ.get("WEATHER_API_KEY") or read_env_file("WEATHER_API_KEY")).strip()
    lat = args.lat if args.lat is not None else float(read_env_file("WEATHER_LAT") or 36.88)
    lon = args.lon if args.lon is not None else float(read_env_file("WEATHER_LON") or 118.73)
    location = (args.location or os.environ.get("WEATHER_LOCATION")
                or read_env_file("WEATHER_LOCATION") or "山东省寿光市三元朱村")
    bucket = args.bucket or read_env_file("WEATHER_ALERT_BUCKET") or "risk"

    print("=" * 62)
    print(" WeatherAPI 气象预警接入（v3）  %s" % now_str())
    print("=" * 62)
    print("▶ 请求")
    print("   GET  %s" % API_URL)
    print("   q=%s,%s  days=1  aqi=no  alerts=yes" % (lat, lon))
    print("   key=%s（来自 %s）" % (mask(key), "命令行 --key" if args.key else ".env / 环境变量 WEATHER_API_KEY"))
    print("   地点：%s ｜ 镜像桶：crops[].alerts.%s" % (location, bucket))

    if not key:
        print("❌ 没找到 API Key：请在 .env 写一行 WEATHER_API_KEY=你的Key，或用 --key 传入")
        return 4

    status, payload, err = fetch_forecast(lat, lon, key, args.timeout, args.retries)
    if payload is None:
        print("❌ 请求失败：%s" % (err or "未知错误"))
        print("⚠️ WeatherAPI 无预警权限（或网络不通）——已保持现有 data.json 的 weather 块不变，流水线不中断。")
        return 1
    print("   HTTP 状态码：%s" % status)

    if not args.no_raw:
        print("\n▶ 完整 JSON 响应")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    print("\n▶ 响应检查")
    if status != 200 or payload.get("error"):
        print("   ❌ 接口返回错误：%s" % json.dumps(payload.get("error"), ensure_ascii=False))
        print("⚠️ WeatherAPI 无预警权限（接口报错）——已保持现有 data.json 的 weather 块不变，流水线不中断。")
        return 4

    loc = payload.get("location") or {}
    print("   ✅ 接口连通；认到的地点：%s（%s, %s），当地时间 %s" % (
        loc.get("name"), loc.get("lat"), loc.get("lon"), loc.get("localtime")))

    alerts_node = payload.get("alerts")
    if alerts_node is None:
        print("   ❌ 响应里【没有 alerts 字段】→ 该套餐 / 该地区不提供预警数据")
        print("⚠️ WeatherAPI 无预警权限（响应中没有 alerts 字段）——已保持现有 Mock/上一次预警不变，流水线不中断。")
        return 2
    raw_list = alerts_node.get("alert") if isinstance(alerts_node, dict) else None
    if raw_list is None:
        print("   ❌ alerts 字段存在，但没有 alert 数组（结构不符合预期）")
        print("⚠️ WeatherAPI 无预警权限（alerts 结构异常）——已保持现有 data.json 的 weather 块不变，流水线不中断。")
        return 2
    print("   ✅ alerts.alert 是数组；原始条数：%d" % len(raw_list))

    if not raw_list:
        print("   ℹ️ 当前没有生效中的气象预警（alerts.alert = []）")
        print("   ℹ️ 接口本身可用（HTTP 200、字段齐备），只是此地此刻没有预警。")
        print("⚠️ WeatherAPI 无预警权限或该地区无预警数据（alerts.alert 为空；实测中国仅部分城市有数据）")
        print("   ——已保持现有 Mock 兜底数据不变，流水线不中断。")
        return 3

    norm = [normalize_weatherapi_alert(a, location) for a in raw_list if isinstance(a, dict)]
    norm = dedupe_and_sort(norm, include_expired=args.include_expired)
    expired = len(raw_list) - len(norm)

    print("\n▶ 归一化后的标准结构（前端直接认这套字段）")
    print(json.dumps(norm, ensure_ascii=False, indent=2))

    unmapped = sum(1 for a in norm if not a.get("level"))
    print("\n▶ 映射小结")
    print("   原始 %d 条 → 归一化后 %d 条（过期/重复过滤 %d 条）" % (len(raw_list), len(norm), expired))
    for a in norm:
        print("   · [%s] %s ｜ 类型=%s ｜ 原始 severity=%s ｜ 生效=%s ｜ 失效=%s" % (
            a["level"] or "未识别", a["title"], a["type"] or "—",
            a["severity_raw"] or "—", a["effective"] or "—", a["expire"] or "—"))
    if unmapped:
        print("   ⚠️ 有 %d 条 severity 认不出来 → 按最低级蓝色处理（原文保留在 severity_raw）" % unmapped)
    if not norm:
        print("ℹ️ 返回的预警都已过期 → 视为当前无生效预警，不写文件（流水线不中断）。")
        return 3

    # ---------- 归属校验（默认开启）：防止把外地预警冒充成本地预警 ----------
    if not args.allow_area_mismatch:
        matched = [a for a in norm if area_matches(a.get("area"), location)]
        rejected = [a for a in norm if a not in matched]
        if rejected:
            print("\n▶ 归属校验")
            for a in rejected:
                print("   ❌ 拒绝：预警范围「%s」≠ 查询地点「%s」→ %s" % (
                    a.get("area") or "（接口未给 areas）", location, (a.get("title") or "")[:40]))
        if not matched:
            print("⚠️ WeatherAPI 无预警权限/预警不属于本地：返回的预警影响范围与查询地点对不上，已【拒绝写入】，")
            print("   避免把外地预警冒充成本地预警。现有 data.json 的 weather 块保持不变，流水线不中断。")
            print("   （如确认要用，可加 --allow-area-mismatch 放行）")
            return 5
        norm = matched
        print("   ✅ 归属校验通过：%d 条预警属于「%s」" % (len(norm), location))

    if args.dry_run:
        print("\n▶ DRY RUN：以上就是要写入 data.json 的 weather 块内容，本次【不写任何文件】")
        return 0

    cfg = {
        "provider": PROVIDER,
        "location": location,
        "lat": lat,
        "lon": lon,
        "bucket": bucket,
        "mirror": not args.no_mirror,
        "source_label": SOURCE_LABEL,
        "_key_masked": mask(key),
        "_source_note_detail": (
            "数据来自 WeatherAPI.com（alerts 字段，CAP 标准）。等级由 severity 换算为四色"
            "（Extreme→红色/Severe→橙色/Moderate→黄色/Minor→蓝色），不是中国气象局四色预警；"
            "发布时间取自 effective（生效时间）；该接口不提供发布机构。"),
    }
    try:
        written, backup_dest, removed = write_alerts(norm, cfg, [args.out] if args.out else None)
    except Exception as e:
        print("\n❌ 写入失败：%s" % e)
        print("🛡️  已保留现有 data.json 不变（一个字节都没动）。")
        return 1

    base = os.path.dirname(os.path.abspath(__file__))
    print("\n▶ 已写入")
    for path in written:
        print("   ✅ %s" % os.path.relpath(path, base))
    if backup_dest:
        print("   🗂  写入前备份：%s（清理旧备份 %d 份）" % (os.path.relpath(backup_dest, base), removed))
    print("   👉 前端会把这次的预警标成【官方预警 · %s】，并镜像 %d 条进 crops[].alerts.%s" % (
        SOURCE_LABEL, len(norm), bucket))
    print("   ℹ️  等级是「CAP severity 换算」而来，前端 meta.notes 里已写明这一点（可如实展示）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
