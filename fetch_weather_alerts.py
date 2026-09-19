# -*- coding: utf-8 -*-
"""
云端田埂 · 气象预警接入端（多来源可插拔 + mock 模拟模式）

它只干一件事：把「气象预警」抓回来 → 归一化 → 合并进 data.json：
    · 顶层 weather.*            —— 结构化预警（供以后的预警卡片/详情页用）
    · crops[].alerts.risk[]     —— 前端【现在就能渲染】的字符串（farm.html / Next 前端都是数组迭代）
    · meta.weather / meta.notes / meta.sources —— 来源、抓取时间、是否模拟，如实标注

与 local_spider_v2.py 的分工（两个脚本互不覆盖对方的字段，可以各自独立跑）：
    local_spider_v2.py   → 负责 crops[].history / series / latest / analysis（行情）
    fetch_weather_alerts.py → 负责 weather / crops[].alerts（预警）

设计原则（与 local_spider_v2.py 完全一致）：
  1. 抓取失败绝不写文件：退出码 1，现有 data.json 一个字节都不动。
  2. 写入前自动备份到 backups/data_YYYY_MM_DD.json（只留最近 7 份），原子写入 + 权限 644。
  3. 如实标注：来源、抓取时间、是否 mock 全部写进 meta，前端必须能看到。
  4. mock 数据默认【不覆盖】线上 data.json：写到 public/data.mock.json 供联调，
     必须显式加 --write 才允许替换线上文件，免得演示数据被当成真实预警发出去。
  5. "接口通了但没有预警"（返回空数组）算【成功】，会如实写 alert_count=0 并摘掉旧字符串；
     但"响应里根本没有预警容器"（如彩云 token 没有预警权限）算【失败】，明确报错、不写文件——
     不能把"没拿到数据"伪装成"当前没有预警"。

用法：
    python3 fetch_weather_alerts.py --mock                 # 模拟预警 → public/data.mock.json（不碰线上）
    python3 fetch_weather_alerts.py --mock --dry-run        # 只打印，什么都不写
    python3 fetch_weather_alerts.py --mock --write          # 模拟预警覆盖 data.json（会先自动备份）
    python3 fetch_weather_alerts.py                         # 真实抓取（需先按 .env.example 配好 .env）
    python3 fetch_weather_alerts.py --dry-run               # 真实抓取，只打印不写文件（联调第一步）
    python3 fetch_weather_alerts.py --provider apihz --lat 36.88 --lon 118.73
    python3 fetch_weather_alerts.py --mock --out /tmp/x.json   # 只写指定文件（调试用）
"""

import argparse
import copy
import json
import os
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta

# ==================== 基础配置 ====================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")

# 线上文件（真实模式会写这两个；mock 模式默认只写 MOCK_OUTPUT）
OUTPUT_FILES = [
    os.path.join(BASE_DIR, "data.json"),
    os.path.join(BASE_DIR, "public", "data.json"),
]
MOCK_OUTPUT = os.path.join(BASE_DIR, "public", "data.mock.json")

BACKUP_DIR = os.path.join(BASE_DIR, "backups")
BACKUP_KEEP = 7

# 默认坐标：山东省寿光市三元朱村（与行情数据 meta.location 一致）
DEFAULT_LAT = 36.88
DEFAULT_LON = 118.73
DEFAULT_LOCATION = "山东省寿光市三元朱村"

GENERATOR = "fetch_weather_alerts.py"

# 预警写进 crops[].alerts 的哪个桶：risk（灾害风险）/ supply / price
DEFAULT_BUCKET = "risk"

# 我们写入的备注前缀，重复运行时先删旧行再加新行（保证幂等，不越积越多）
NOTE_PREFIX_WEATHER = "【气象预警】"

# 归一化后的预警等级顺序（红 > 橙 > 黄 > 蓝 > 白）
SEVERITY_ORDER = {"红色": 4, "橙色": 3, "黄色": 2, "蓝色": 1, "白色": 0}
LEVEL_COLORS = {
    "红色": "#E60012",
    "橙色": "#FF8C00",
    "黄色": "#FFD700",
    "蓝色": "#1E90FF",
    "白色": "#FFFFFF",
}

# 有的平台（如彩云 v3 预警）用英文色名描述等级：统一折算成中文颜色词
LEVEL_ALIASES_EN = {
    "red": "红色", "orange": "橙色", "yellow": "黄色", "blue": "蓝色", "white": "白色",
}

# 归一化后的预警字段（所有 provider 都必须产出这套字段）
ALERT_FIELDS = [
    "id", "title", "type", "level", "color",
    "sender", "pub_time", "effective", "expire",
    "text", "advice", "src", "provider", "data_kind",
]


# ==================== .env 读取（不引入 python-dotenv 依赖） ====================

def load_env_file(path):
    """极简 .env 解析：支持 # 注释、export 前缀、单双引号、空行。"""
    env = {}
    if not os.path.exists(path):
        return env
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key:
                    env[key] = val
    except OSError:
        pass
    return env


def build_config(args):
    """配置优先级：命令行参数 > 真实环境变量 > .env 文件 > 默认值。"""
    file_env = load_env_file(ENV_FILE)

    def pick(name, default=None):
        val = os.environ.get(name)
        if val is None or val == "":
            val = file_env.get(name)
        if val is None or val == "":
            return default
        return val

    def pick_float(name, default):
        try:
            return float(pick(name, default))
        except (TypeError, ValueError):
            return float(default)

    def pick_int(name, default):
        try:
            return int(str(pick(name, default)).strip())
        except (TypeError, ValueError):
            return int(default)

    provider = args.provider or pick("ALERT_PROVIDER", "apihz")
    if args.mock:
        provider = "mock"

    cfg = {
        "provider": provider,
        "lat": args.lat if args.lat is not None else pick_float("WEATHER_LAT", DEFAULT_LAT),
        "lon": args.lon if args.lon is not None else pick_float("WEATHER_LON", DEFAULT_LON),
        "location": pick("WEATHER_LOCATION", DEFAULT_LOCATION),
        "bucket": pick("WEATHER_ALERT_BUCKET", DEFAULT_BUCKET),

        # 凭证（真实 provider 才需要）
        "apihz_id": pick("APIHZ_ID"),
        "apihz_key": pick("APIHZ_KEY"),
        "caiyun_token": pick("CAIYUN_TOKEN"),
        "qweather_key": pick("QWEATHER_KEY"),

        # 通用 HTTP 配置（provider=generic 时用它对接任何平台，包括政府开放数据）
        "api_url": pick("ALERT_API_URL"),
        "api_method": (pick("ALERT_API_METHOD", "GET") or "GET").upper(),
        "api_params": pick("ALERT_API_PARAMS"),
        "api_headers": pick("ALERT_API_HEADERS"),
        "api_body": pick("ALERT_API_BODY"),
        "json_path": pick("ALERT_JSON_PATH"),
        "field_map": pick("ALERT_FIELD_MAP"),
        "source_label": pick("ALERT_SOURCE_LABEL"),

        # apihz 专用：预警接口的路径名（注册后在站内「天气预报」分类里找）
        "apihz_endpoint": pick("ALERT_APIHZ_ENDPOINT", "tianqi"),

        # 网络与礼貌性延迟
        "timeout": args.timeout if args.timeout else pick_float("ALERT_TIMEOUT", 15),
        "retries": args.retries if args.retries is not None else pick_int("ALERT_RETRIES", 2),
        "no_delay": bool(args.no_delay),
    }
    return cfg


# ==================== 工具函数 ====================

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_time(value):
    """尽量宽松地解析各家返回的时间字符串；解析不出来返回 None（不抛异常）。"""
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value))
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    text = (
        text.replace("T", " ")
        .replace("/", "-")
        .replace("年", "-")
        .replace("月", "-")
        .replace("日", " ")
    )
    text = re.sub(r"(\.\d+)?(Z|[+-]\d{2}:?\d{2})$", "", text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%m-%d %H:%M":            # 只有月日时补上今年
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    return None


def humanize_time(value):
    """把各家五花八门的时间统一成「YYYY-MM-DD HH:MM:SS」。

    · 秒级时间戳（彩云 v2.6 的 pubtimestamp=1640733900）→ 本地时间字符串
    · 毫秒级时间戳（13 位）→ 先 /1000 再换算
    · 字符串时间 → 能解析就规范化，解析不出来原样返回（绝不丢信息、绝不抛异常）
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str) and re.match(r"^\d{9,14}$", value.strip()):
        value = int(value.strip())          # 字符串形式的纯数字时间戳
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value)
        if ts > 1e11:                       # 13 位毫秒 → 秒
            ts /= 1000.0
        dt = parse_time(ts)
        return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else str(value)
    dt = parse_time(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else str(value).strip()


def is_expired(alert, now=None):
    """已过期的预警不再对外展示（--include-expired 可保留，仅调试用）。"""
    expire = parse_time(alert.get("expire"))
    if expire is None:
        return False
    return expire < (now or datetime.now())


def severity_of(alert):
    return SEVERITY_ORDER.get(str(alert.get("level", "")).strip(), 0)


def pick_field(item, names, default=""):
    """按候选字段名取值（支持 a.b.c 点路径），返回第一个非空值。"""
    for name in names:
        cur = item
        ok = True
        for part in str(name).split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None and str(cur).strip() != "":
            return cur.strip() if isinstance(cur, str) else cur
    return default


def dig(obj, path):
    """按点路径取嵌套值，取不到返回 None。"""
    if not path:
        return obj
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def alert_to_text(alert, limit=220):
    """转成前端【现在就能渲染】的一句话（farm.html / Next 前端都把 alerts 当字符串数组迭代）。

    注意：来源标注、模拟数据警示属于合规信息，**任何情况下都不能被截断掉**，
    所以截断的是正文，头部与尾部始终完整保留。
    """
    type_name = str(alert.get("type", "")).strip()
    level = str(alert.get("level", "")).strip()
    head = "【%s%s预警】" % (type_name, level) if (type_name or level) else "【气象预警】"

    body = str(alert.get("text", "")).strip() or str(alert.get("title", "")).strip()
    body = re.sub(r"\s+", " ", body)

    bits = []
    if alert.get("sender"):
        bits.append(str(alert["sender"]))
    if alert.get("pub_time"):
        bits.append("%s 发布" % alert["pub_time"])
    meta_bits = ("（%s）" % " ".join(bits)) if bits else ""

    tail = ""
    if alert.get("advice"):
        tail += " 防御建议：" + re.sub(r"\s+", " ", str(alert["advice"]).strip())
    if alert.get("src"):
        tail += "｜来源：%s" % alert["src"]
    if alert.get("data_kind") == "mock":
        tail += "｜⚠️ 模拟数据，仅供联调"

    text = head + body + meta_bits + tail
    if len(text) > limit:                      # 只截正文，保证尾部合规标注完整
        over = len(text) - limit
        room = max(0, len(body) - over - 1)
        body = body[:room] + ("…" if room < len(body) else "")
        text = head + body + meta_bits + tail
    return text


def dedupe_and_sort(alerts, include_expired=False):
    """按 id 去重（同 id 保留信息更全的那条）→ 过滤过期 → 等级高的在前、同级按发布时间新的在前。"""
    now = datetime.now()
    by_id = {}
    for a in alerts:
        if not include_expired and is_expired(a, now):
            continue
        key = a.get("id") or alert_to_text(a, 60)
        old = by_id.get(key)
        if old is None or len(json.dumps(a, ensure_ascii=False)) > len(json.dumps(old, ensure_ascii=False)):
            by_id[key] = a
    result = list(by_id.values())
    result.sort(key=lambda x: str(x.get("pub_time", "")), reverse=True)   # 先按时间倒序
    result.sort(key=lambda x: -severity_of(x))                            # 再稳定按等级倒序
    return result


def normalize_alert(raw, provider, field_map=None, source_label=None, data_kind="live"):
    """把任意平台的原始字段归一化成统一结构（缺字段一律补空串，绝不出现 None/undefined）。"""
    fm = field_map or {}

    def get(field, candidates):
        paths = fm.get(field)
        if paths:                       # ALERT_FIELD_MAP 里显式指定的路径优先
            if isinstance(paths, str):
                paths = [paths]
            val = pick_field(raw, paths)
            if val != "":
                return val
        return pick_field(raw, candidates)

    level = str(get("level", ["level", "severity", "severityColor", "levelName", "alertLevel", "color"])).strip()
    level = level.replace("预警", "").replace("信号", "").strip()
    for known in SEVERITY_ORDER:        # 把"橙色预警II级"之类清理成"橙色"
        if known in level:
            level = known
            break
    else:                               # 一个中文颜色词都没认出来 → 试试英文色名（彩云 v3 用 red/orange/…）
        m_en = re.match(r"^\s*(red|orange|yellow|blue|white)\b", level, re.I)
        if m_en:
            level = LEVEL_ALIASES_EN[m_en.group(1).lower()]

    raw_color = str(get("color", ["color", "severityColor", "levelColor"])).strip()
    if re.match(r"^#?[0-9a-fA-F]{3,8}$", raw_color):
        color = raw_color                      # 接口直接给了 hex
    else:
        color = ""                             # 接口给的是"橙色"这类词 → 换算成颜色
        for known in SEVERITY_ORDER:
            if known in raw_color:
                color = LEVEL_COLORS[known]
                break
        if not color:
            color = LEVEL_COLORS.get(level, "")

    alert = {
        "id": str(get("id", ["id", "alertId", "alert_id", "uuid", "code", "identifier"])).strip(),
        "title": str(get("title", ["title", "headline", "typeName", "event", "name"])).strip(),
        "type": str(get("type", ["type", "typeName", "eventType", "category", "warningType"])).strip(),
        "level": level,
        "color": color,
        "sender": str(get("sender", ["sender", "senderName", "publisher", "org", "office", "source"])).strip(),
        "pub_time": humanize_time(get("pub_time", ["pubTime", "pub_time", "pubtime", "pubtimestamp",
                                                   "pubTimestamp", "publishTime", "publish_time",
                                                   "fabutime", "issueTime", "time"])),
        "effective": humanize_time(get("effective", ["effectiveTime", "effective", "startTime",
                                                     "start_time", "starttime", "onset"])),
        "expire": humanize_time(get("expire", ["expireTime", "expire", "endTime", "end_time",
                                               "endtime", "expires"])),
        "text": str(get("text", ["text", "content", "description", "detail", "msg", "body", "message"])).strip(),
        "advice": str(get("advice", ["advice", "defense", "instruction", "guide", "suggestion", "tips"])).strip(),
        "src": str(source_label or get("src", ["source", "sender", "publisher"])).strip(),
        "provider": provider,
        "data_kind": data_kind,
    }

    # 类型缺失时，从标题里认（暴雨/大风/高温…）
    if not alert["type"] and alert["title"]:
        for known in ("暴雨", "暴雪", "大风", "高温", "寒潮", "雷电", "冰雹", "大雾", "霾",
                      "沙尘", "台风", "干旱", "低温", "道路结冰", "山洪", "霜冻"):
            if known in alert["title"]:
                alert["type"] = known
                break
    # 等级缺失时，从标题/正文里认颜色词（彩云 v2.6 的预警记录只有 title，没有 level 字段）
    if not alert["level"]:
        probe = alert["title"] or alert["text"]
        for known in SEVERITY_ORDER:
            if known in probe:
                alert["level"] = known
                break
        if alert["level"] and not alert["color"]:
            alert["color"] = LEVEL_COLORS.get(alert["level"], "")
    if not alert["src"]:
        alert["src"] = provider
    if not alert["id"]:
        alert["id"] = "%s-%s-%s" % (provider, alert["type"] or "unknown", (alert["pub_time"] or now_str())[:16])
    return alert


# ==================== mock：还没注册账号时用它先把前端渲染跑通 ====================

def build_mock_alerts():
    """模拟一条「暴雨橙色预警」。时间相对"现在"生成，免得过一阵子变成"已过期"看不见。"""
    now = datetime.now()
    pub = now - timedelta(minutes=30)
    expire = now + timedelta(hours=12)
    return [
        normalize_alert(
            {
                "id": "MOCK-SG-%s" % pub.strftime("%Y%m%d%H%M"),
                "title": "寿光市气象台发布暴雨橙色预警[II级/严重]",
                "type": "暴雨",
                "level": "橙色",
                "sender": "寿光市气象台",
                "pubTime": pub.strftime("%Y-%m-%d %H:%M:%S"),
                "effectiveTime": pub.strftime("%Y-%m-%d %H:%M:%S"),
                "expireTime": expire.strftime("%Y-%m-%d %H:%M:%S"),
                "text": (
                    "寿光市气象台%s发布暴雨橙色预警信号：预计今天白天到夜间，我市全部镇（街、区）"
                    "将出现50毫米以上降水，局部超过100毫米，并伴有雷电和7～9级雷雨大风，"
                    "低洼地块可能出现积水，请注意防范。" % pub.strftime("%m月%d日%H时%M分")
                ),
                "advice": (
                    "1. 立即清沟排水，防止田间积水沤根；2. 大棚加固压膜绳、关闭通风口；"
                    "3. 抢收已成熟蔬菜，尤其是低洼地块；4. 暂停棚内喷药施肥，避免药害与流失；"
                    "5. 不要在河渠边、低洼地长时间作业。"
                ),
            },
            provider="mock",
            source_label="模拟数据（mock，非真实预警）",
            data_kind="mock",
        )
    ]


# ==================== 数据抓取（多来源可插拔） ====================
# 各平台接口路径与字段名差异很大、且都要注册后才有正式额度，所以做成「预设 + .env 覆盖」：
# 注册完把 URL / 字段映射填进 .env 即可，不用改代码。

PROVIDER_PRESETS = {
    # 接口盒子（apihz.cn）：注册用户 10 次/分钟、每日无上限、免费；开发调试可用公开测试值 88888888
    # ⚠️ endpoint 需在站内「天气预报」分类里确认预警接口的实际路径名后填入
    "apihz": {
        "url": "https://cn.apihz.cn/api/tianqi/{endpoint}.php",
        "params": {"id": "{id}", "key": "{key}", "lat": "{lat}", "lon": "{lon}"},
        "list_path": "data",
        "source": "接口盒子 apihz.cn",
    },
    # 彩云天气：官方文档确认有「气象预警 单点查询/批量/Webhook + CAP 标准」，v2.6 预警位于 result.alert
    "caiyun": {
        "url": "https://api.caiyunapp.com/v2.6/{token}/{lon},{lat}/realtime",
        "params": {"alert": "true"},
        "list_path": "result.alert",
        "source": "彩云天气 Caiyun",
    },
    # 通用 HTTP：任何返回 JSON 的平台（含各地政府开放数据平台）都能接，URL/字段全靠 .env 配
    "generic": {
        "url": None,
        "params": {},
        "list_path": "data",
        "source": None,
    },
}

LIST_KEYS = ("content", "alerts", "alert", "data", "list", "items",
             "details", "warnings", "warning", "records", "result")


class ConfigError(Exception):
    """配置缺失（还没注册 / 还没填 .env）——直接退出，不去联网瞎试。"""


def fill_template(value, mapping):
    if value is None:
        return None
    out = str(value)
    for key, val in mapping.items():
        out = out.replace("{%s}" % key, "" if val is None else str(val))
    return out


def load_json_env(raw, what):
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except ValueError:
        raise ConfigError("%s 不是合法 JSON：%s" % (what, raw[:60]))
    if not isinstance(obj, dict):
        raise ConfigError("%s 必须是 JSON 对象" % what)
    return obj


def first_list(obj, path):
    """取出预警数组：支持 list_path 直接命中，也支持 {"content": [...]} 这类包一层的情况。"""
    node = dig(obj, path) if path else obj
    if node is None and path:
        node = obj
    if isinstance(node, list):
        return node
    if isinstance(node, dict):
        for key in LIST_KEYS:
            val = node.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                for key2 in LIST_KEYS:
                    if isinstance(val.get(key2), list):
                        return val[key2]
    return []


def http_json(url, method="GET", params=None, headers=None, body=None,
              timeout=15, retries=2, no_delay=False):
    """带重试与随机延迟的 JSON 请求（防封禁策略与 local_spider_v2.py 保持一致）。"""
    try:
        import requests
    except ImportError:
        raise ConfigError("缺少 requests 依赖：/usr/bin/python3 -m pip install --user requests urllib3")

    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    headers = dict(headers or {})
    headers.setdefault("User-Agent",
                       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15")
    last_err = None
    for attempt in range(int(retries) + 1):
        if not no_delay:
            time.sleep(random.uniform(1.0, 3.0))
        try:
            if method == "POST":
                resp = requests.post(url, params=params, json=body, headers=headers,
                                     timeout=timeout, verify=False)
            else:
                resp = requests.get(url, params=params, headers=headers,
                                    timeout=timeout, verify=False)
            if resp.status_code != 200:
                raise RuntimeError("HTTP %s" % resp.status_code)
            return resp.json()
        except Exception as exc:                 # noqa: BLE001 —— 网络层什么都可能抛
            last_err = exc
    raise RuntimeError("请求失败（已重试 %d 次）：%s" % (retries, last_err))


def means_no_alerts(payload, list_path):
    """判断这是不是"接口通了、只是当前没有预警"（而不是"结构不对 / 没权限"）。

    命中两种情况：
      1) 预警容器本身就在响应里，只是空的：{"result": {"alert": {"status": "ok", "content": []}}}
      2) 顶层明确写着成功，且确实带了空数组字段：{"status": "ok", "warning": []}
    """
    if isinstance(payload, dict) and list_path and dig(payload, list_path) is not None:
        return True
    if not isinstance(payload, dict):
        return False
    ok = (str(payload.get("status", "")).strip().lower() in ("ok", "success", "true")
          or payload.get("code") in (200, "200", 0, "0")
          or payload.get("success") is True)
    if not ok:
        return False
    return any(isinstance(payload.get(key), list) for key in LIST_KEYS)


def no_alerts_hint(provider, payload, list_path):
    """响应里找不到预警容器时，给出"到底为什么"的可执行提示（绝不静默当成"没有预警"）。"""
    if provider == "caiyun":
        return (
            "彩云接口是通的（status=%s，api_status=%s），但响应里没有 result.alert ——\n"
            "   彩云「预警数据」属于【增值服务】，不在免费赠送额度内：只有 token 具备预警权限时，\n"
            "   带 alert=true 的请求才会附带 result.alert（官方文档：v2.6 API → 预警数据 → 访问限制；\n"
            "   v3 单点查询更是「仅提供给企业套餐开发者」）。\n"
            "   → 开通：https://dashboard.caiyunapp.com 找到本应用的 token，为其开通/升级预警数据后重试；\n"
            "   → 或改用免费来源：ALERT_PROVIDER=apihz / generic（见 .env.example）；\n"
            "   → 或先用 --mock 把前端渲染跑通。"
            % (payload.get("status"), payload.get("api_status"))
        )
    return ("接口没返回预警数组（list_path=%s）。响应结构可能和你配的 ALERT_JSON_PATH 不一致，"
            "或该来源当前不提供预警数据；可用 --out /tmp/x.json 把原始响应抓下来看一眼。" % list_path)


def fetch_alerts(cfg):
    """按 provider 抓取预警并归一化；返回 list[alert]。失败一律抛异常（由上层决定不写文件）。

    注意区分三种"看起来都像没数据"的情况：
      · 预警容器存在但为空 → 正常返回 []（接口通、当前无预警，算成功）
      · 容器根本不存在（如彩云 token 没有预警权限）→ 抛异常，提示去开通
      · 平台整体报错（如限流 status=failed）→ 抛异常，原样带出平台的错误信息
    """
    provider = cfg["provider"]
    if provider == "mock":
        return build_mock_alerts()

    preset = PROVIDER_PRESETS.get(provider)
    if preset is None:
        raise ConfigError("未知 provider：%s（可选：mock / apihz / caiyun / generic）" % provider)

    mapping = {
        "id": cfg["apihz_id"], "key": cfg["apihz_key"], "token": cfg["caiyun_token"],
        "endpoint": cfg["apihz_endpoint"], "lat": cfg["lat"], "lon": cfg["lon"],
    }

    url = cfg["api_url"] or fill_template(preset["url"], mapping)
    # endpoint 还是占位值（默认 tianqi）→ 说明"预警接口地址"根本没配，直接给指引，不去瞎试
    endpoint_is_placeholder = str(cfg["apihz_endpoint"] or "").strip() in ("", "tianqi")
    if not url or (provider == "apihz" and not cfg["api_url"] and endpoint_is_placeholder):
        raise ConfigError(
            "还没有可用的预警接口地址（ALERT_APIHZ_ENDPOINT 还是占位值），请在 .env 里二选一：\n"
            "   · ALERT_API_URL=<平台文档里的完整接口地址>\n"
            "   · ALERT_APIHZ_ENDPOINT=<接口盒子站内确认过的预警接口路径名>"
        )
    url = fill_template(url, mapping)

    if provider == "apihz" and not (cfg["apihz_id"] and cfg["apihz_key"]):
        raise ConfigError(
            "缺少 APIHZ_ID / APIHZ_KEY：注册 https://www.apihz.cn 后在「个人资料」查看\n"
            "（开发调试可先用公开测试值：APIHZ_ID=88888888、APIHZ_KEY=88888888）"
        )
    if provider == "caiyun" and not cfg["caiyun_token"]:
        raise ConfigError("缺少 CAIYUN_TOKEN：登录 https://dashboard.caiyunapp.com 创建应用后获取")
    if provider == "generic" and not cfg["api_url"]:
        raise ConfigError("provider=generic 必须设置 ALERT_API_URL")

    params = {}
    for k, v in (preset.get("params") or {}).items():
        params[k] = fill_template(v, mapping)
    params.update(load_json_env(cfg["api_params"], "ALERT_API_PARAMS"))
    params = dict((k, v) for k, v in params.items() if v not in (None, ""))

    headers = load_json_env(cfg["api_headers"], "ALERT_API_HEADERS")
    body = load_json_env(cfg["api_body"], "ALERT_API_BODY") or None
    field_map = load_json_env(cfg["field_map"], "ALERT_FIELD_MAP")
    list_path = cfg["json_path"] or preset["list_path"]
    source_label = cfg["source_label"] or preset.get("source") or provider

    payload = http_json(url, method=cfg["api_method"], params=params, headers=headers, body=body,
                        timeout=cfg["timeout"], retries=cfg["retries"], no_delay=cfg["no_delay"])

    # 平台整体性报错：HTTP 200 但 body 里写着 failed（典型：彩云 "Rate limit exceeded" 限流）
    if isinstance(payload, dict):
        failed = str(payload.get("status", "")).strip().lower() in ("failed", "fail", "error")
        bad_code = payload.get("code") not in (None, 200, "200", 0, "0")
        if failed or bad_code:
            raise RuntimeError("接口返回异常：status=%s code=%s msg=%s"
                               % (payload.get("status"), payload.get("code"),
                                  payload.get("error") or payload.get("errmsg")
                                  or payload.get("msg") or "（平台未给说明）"))

    items = first_list(payload, list_path)
    if not items and not means_no_alerts(payload, list_path):
        raise RuntimeError(no_alerts_hint(provider, payload, list_path))
    return [normalize_alert(it, provider, field_map=field_map, source_label=source_label) for it in items]


# ==================== 合并进 data.json ====================

SOURCE_NOTE = "预警数据版权归发布机构所有，展示时必须注明来源，不得篡改预警等级与正文"


def previous_mirrored_strings(data):
    """上一次合并写进 crops[].alerts 的字符串（重新运行时先摘掉，保证幂等不累积）。"""
    prev = (data.get("weather") or {}).get("active_alerts") or []
    result = set()
    for item in prev:
        if isinstance(item, dict):
            result.add(alert_to_text(item))
    return result


def merge_into_data(data, alerts, cfg, include_expired=False):
    """把预警合并进现有 data.json 结构（只动 weather / crops[].alerts / meta 三处，其余原样保留）。"""
    out = copy.deepcopy(data)
    meta = out.setdefault("meta", {})
    crops = out.get("crops") or []
    if not crops:
        raise RuntimeError("现有 data.json 里没有 crops，拒绝写入（请先跑 local_spider_v2.py 生成行情数据）")

    now = now_str()
    kinds = set([a.get("data_kind") for a in alerts]) or set(["live"])
    data_kind = "mock" if "mock" in kinds else "live"
    src_label = " + ".join(sorted(set([str(a.get("src", "")).strip() for a in alerts if a.get("src")])))
    if not src_label:        # 一条预警都没有时也要写清"问的是哪个来源"，别让前端只看到 provider 代号
        src_label = (cfg.get("source_label")
                     or (PROVIDER_PRESETS.get(cfg["provider"]) or {}).get("source")
                     or cfg["provider"])
    bucket = cfg["bucket"]

    # 1) 顶层结构化预警（供以后的预警卡片 / 详情页 / 知识库引用）
    out["weather"] = {
        "location": cfg["location"],
        "lat": cfg["lat"],
        "lon": cfg["lon"],
        "provider": cfg["provider"],
        "data_kind": data_kind,
        "fetched_at": now,
        "alert_count": len(alerts),
        "source": src_label or "（本次没有生效预警）",
        "mirrored_to": ("crops[].alerts.%s" % bucket) if cfg.get("mirror") else None,
        "active_alerts": alerts,
    }

    # 2) meta：让前端和任何人都一眼看出这批预警的来源、时间、真假
    meta["weather_alert_count"] = len(alerts)
    meta["weather_source"] = src_label or "（本次没有生效预警）"
    meta["weather_fetched_at"] = now
    meta["weather_data_kind"] = data_kind
    meta["weather_provider"] = cfg["provider"]

    # 来源署名（合规：开源/商业数据都要写清出处）
    sources = [s for s in (meta.get("sources") or []) if not (isinstance(s, dict) and s.get("type") == "weather_alert")]
    sources.append({
        "type": "weather_alert",
        "name": src_label or cfg["provider"],
        "provider": cfg["provider"],
        "data_kind": data_kind,
        "fetched_at": now,
        "note": SOURCE_NOTE,
    })
    meta["sources"] = sources

    # notes：先摘掉上一次的气象预警说明，再写新的（合并成一行，保证幂等不累积）
    notes = [n for n in (meta.get("notes") or []) if not str(n).startswith(NOTE_PREFIX_WEATHER)]
    if not alerts:
        line = "本次没有生效中的气象预警（来源 %s，抓取于 %s）。" % (src_label or cfg["provider"], now)
    else:
        top = alerts[0]
        line = "当前 %d 条生效预警，最高等级：%s%s预警（来源 %s，抓取于 %s）。" % (
            len(alerts), top.get("type", ""), top.get("level", ""), src_label or cfg["provider"], now)
    if data_kind == "mock":
        line = "⚠️【模拟数据】仅供前端联调，严禁对外发布；" + line
    notes.append(NOTE_PREFIX_WEATHER + line)
    meta["notes"] = notes

    # 3) crops[].alerts：填前端【现在就能渲染】的字符串数组
    prev_texts = previous_mirrored_strings(data)
    new_texts = [alert_to_text(a) for a in alerts]
    for crop in crops:
        block = crop.setdefault("alerts", {})
        for key in ("price", "supply", "risk"):
            if not isinstance(block.get(key), list):
                block[key] = []
        kept = [t for t in block.get(bucket, []) if t not in prev_texts]      # 摘掉上一次的，保留别人写的
        block[bucket] = (kept + new_texts) if cfg.get("mirror") else kept
        crop["alerts_source"] = src_label or cfg["provider"]
        crop["alerts_updated_at"] = now
    return out


# ==================== 读取 / 备份 / 写入（与 local_spider_v2.py 一致的做法） ====================

def load_existing(path):
    if not os.path.exists(path):
        raise RuntimeError("找不到 %s —— 请先运行 local_spider_v2.py 生成行情数据" % path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def backup_current():
    """备份现有 data.json（用独立的文件名前缀，避免和行情脚本的备份互相覆盖）。"""
    src = OUTPUT_FILES[0]
    if not os.path.exists(src):
        return None, 0
    if not os.path.isdir(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)
    stamp = datetime.now().strftime("%Y_%m_%d")
    dest = os.path.join(BACKUP_DIR, "data_weather_%s.json" % stamp)
    with open(src, "r", encoding="utf-8") as f:
        content = f.read()
    with open(dest, "w", encoding="utf-8") as f:
        f.write(content)
    # 只保留最近 BACKUP_KEEP 份（按文件名倒序 = 按日期倒序）
    names = sorted([n for n in os.listdir(BACKUP_DIR)
                    if n.startswith("data_weather_") and n.endswith(".json")], reverse=True)
    removed = 0
    for old in names[BACKUP_KEEP:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
            removed += 1
        except OSError:
            pass
    return dest, removed


def save_json(payload, paths):
    """原子写入 + 权限 644（mkstemp 默认 0600，静态托管读不到）。"""
    written = []
    for path in paths:
        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        fd, tmp_path = tempfile.mkstemp(dir=folder or ".", prefix=".data.json.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            try:
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

def main():
    parser = argparse.ArgumentParser(
        description="抓取气象预警并合并进 data.json（行情脚本管价格，本脚本只管预警）")
    parser.add_argument("--mock", action="store_true",
                        help="用内置模拟预警（暴雨橙色），不联网；默认只写 public/data.mock.json")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写任何文件")
    parser.add_argument("--write", action="store_true", help="mock 模式下允许覆盖线上 data.json（会先备份）")
    parser.add_argument("--out", help="只写这一个文件（调试/预览，不碰线上 data.json）")
    parser.add_argument("--provider", choices=["mock", "apihz", "caiyun", "generic"],
                        help="预警来源（默认取 .env 的 ALERT_PROVIDER）")
    parser.add_argument("--lat", type=float, help="纬度（默认 36.88 = 寿光三元朱村）")
    parser.add_argument("--lon", type=float, help="经度（默认 118.73）")
    parser.add_argument("--bucket", choices=["risk", "supply", "price"],
                        help="预警写进 crops[].alerts 的哪个桶（默认 risk）")
    parser.add_argument("--no-crop-mirror", action="store_true", help="只写顶层 weather，不动 crops[].alerts")
    parser.add_argument("--no-delay", action="store_true", help="跳过随机延迟（仅本地调试）")
    parser.add_argument("--include-expired", action="store_true", help="保留已过期预警（调试用）")
    parser.add_argument("--timeout", type=float, help="单次请求超时秒数（默认 15）")
    parser.add_argument("--retries", type=int, help="失败重试次数（默认 2）")
    args = parser.parse_args()

    cfg = build_config(args)
    cfg["mirror"] = not args.no_crop_mirror
    if args.bucket:
        cfg["bucket"] = args.bucket

    print("⏳ 开始抓取气象预警（provider=%s，坐标=%s,%s）..." % (cfg["provider"], cfg["lat"], cfg["lon"]))
    if cfg["provider"] == "mock":
        print("🧪 MOCK 模式：使用内置模拟预警（暴雨橙色），全程不访问网络。")
        print("   默认只写 %s，不会覆盖线上 data.json（要覆盖请加 --write）。"
              % os.path.relpath(MOCK_OUTPUT, BASE_DIR))

    try:
        data = load_existing(OUTPUT_FILES[0])
    except Exception as e:
        print("❌ %s" % e)
        sys.exit(1)

    # 抓取：失败就直接退出，绝不写文件（与行情脚本同一条铁律）
    try:
        alerts = fetch_alerts(cfg)
    except (ConfigError, RuntimeError, ValueError) as e:
        print("❌ 抓取失败：%s" % e)
        print("🛡️  已保留 %s 不变（一个字节都没动）。" % os.path.relpath(OUTPUT_FILES[0], BASE_DIR))
        sys.exit(1)

    raw_count = len(alerts)
    alerts = dedupe_and_sort(alerts, include_expired=args.include_expired)
    expired = raw_count - len(alerts)

    if alerts:
        for a in alerts:
            print("  · %s%s预警   %s   [%s]" % (a["type"], a["level"], a["pub_time"] or "时间未知", a["src"]))
    else:
        print("  · 本次没有生效中的预警（抓取成功，结果为空）")
    if expired:
        print("  ⚠️ 已过滤 %d 条过期预警（--include-expired 可保留，仅调试）" % expired)

    try:
        merged = merge_into_data(data, alerts, cfg, include_expired=args.include_expired)
    except Exception as e:
        print("❌ 合并失败：%s" % e)
        sys.exit(1)

    if args.dry_run:
        print("\n--- DRY RUN：以下内容没有写入任何文件 ---")
        text = json.dumps(merged.get("weather", {}), ensure_ascii=False, indent=2)
        print(text if len(text) < 3000 else text[:3000] + "\n…（后略）")
        crop0 = (merged.get("crops") or [{}])[0]
        sample = (crop0.get("alerts") or {}).get(cfg["bucket"], [])
        print("\n第一条 crop 的 alerts.%s：" % cfg["bucket"])
        print("  " + (sample[0] if sample else "（空）"))
        return

    # 输出目标：--out 优先；mock 且未 --write → 只写预览文件；否则写线上两份
    if args.out:
        targets, is_mock_preview = [args.out], False
    elif cfg["provider"] == "mock" and not args.write:
        targets, is_mock_preview = [MOCK_OUTPUT], True
    else:
        targets, is_mock_preview = list(OUTPUT_FILES), False

    if any(os.path.abspath(t) == os.path.abspath(OUTPUT_FILES[0]) for t in targets):
        backup_path, removed = backup_current()
        if backup_path:
            print("🗄️  已备份旧文件 → %s（顺带清理旧备份 %d 个）"
                  % (os.path.relpath(backup_path, BASE_DIR), removed))

    try:
        written = save_json(merged, targets)
    except Exception as e:
        print("❌ 写入失败：%s" % e)
        sys.exit(1)

    for path in written:
        print("📁 已写入: %s" % os.path.relpath(path, BASE_DIR))

    if is_mock_preview:
        print("\n💡 这是【预览文件】，线上 data.json 一个字节都没动。前端联调两种方式：")
        print("   1) 本地预览：python3 -m http.server 8080 --directory public")
        print("      打开 http://localhost:8080/farm.html（把其中的 /data.json 临时指到 /data.mock.json）")
        print("   2) 直接覆盖线上文件联调：python3 fetch_weather_alerts.py --mock --write")


if __name__ == "__main__":
    main()
